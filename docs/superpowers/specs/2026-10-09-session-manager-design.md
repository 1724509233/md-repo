# Agent Session Manager 下沉 + Per-Session Lock 设计

## 背景与动机

当前 agent session 管理散落在 `server/api.py` 的 `create_app` 闭包中：

| 职责 | 当前位置 | 问题 |
|------|---------|------|
| 内存缓存 `_agent_sessions` | `api.py:67` 闭包变量 | 无法独立测试，和 API 路由耦合 |
| 创建/恢复 session | `api.py:274-294`（agent_chat 内） | 逻辑埋在 handler 里 |
| TTL 清理 | `api.py:72-76`（闭包函数） | 同上 |
| new-session 创建 | `api.py:384-411`（独立 handler） | 和 agent_chat 的创建逻辑重复 |
| per-session lock | —（待加） | 无并发防护，客户端连续请求会导致多个 agent_loop 并行操作同一 session |

**目标：**
1. 将 session 生命周期管理下沉到 `server/agent/session.py` 的 `SessionManager` 类
2. 加入 per-session `asyncio.Lock`，保证同一 session 同时只有一个 `agent_loop` 在跑（防御性设计，不信任客户端串行化）
3. `api.py` 变为薄路由层

**非目标：**
- doc_context 提示词注入留在 `api.py`（与 SessionManager 解耦，未来单独封装）
- 不修改 `agent_loop` 内部逻辑
- 不修改前端

## 文件结构

```
server/agent/
├── __init__.py      # 新增 re-export: SessionManager
├── session.py       # 新增: SessionManager 类
├── loop.py          # 不变
├── prompts.py       # 不变
├── tools.py         # 不变
├── validate.py      # 不变
└── execute.py       # 不变
```

## SessionManager API

```python
class SessionManager:
    """Agent session lifecycle: create, restore, lock, cleanup, persist."""

    def __init__(self, db_conn: sqlite3.Connection | None, vault_dir: Path):
        self._db = db_conn
        self._vault = vault_dir
        self._sessions: dict[str, dict] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._ttl = 1800  # 30 minutes

    def get_or_create(self, session_id: str) -> dict:
        """Return session dict. Restores from DB if not in memory.

        Session dict shape:
        {
            "messages": list[dict],
            "pending_confirms": dict,
            "last_active": float,
            "token_count": int,
            "last_read_ts": dict,  # lazy-init via setdefault
        }
        """
        ...

    def create_new(self) -> str:
        """Create a fresh session with system prompt. Returns new session_id (uuid4)."""
        ...

    def get_lock(self, session_id: str) -> asyncio.Lock:
        """Return per-session asyncio.Lock (creates if not exists)."""
        ...

    def cleanup(self) -> None:
        """Evict sessions idle > TTL. Does NOT evict locked sessions."""
        ...

    def save(self, session_id: str) -> None:
        """Persist session messages to DB (no-op if db_conn is None)."""
        ...

    def get(self, session_id: str) -> dict | None:
        """Return session dict if in memory, else None. For read-only access (e.g. agent_confirm)."""
        ...
```

### 设计决策

1. **`get_or_create` 不做 lock**：lock 的粒度是"agent_loop 执行期间"，不是"session 访问期间"。`get_or_create` 本身是幂等的读操作。
2. **`cleanup` 跳过 locked sessions**：lock 被持有的 session 不清理，避免清理掉正在使用的 session。
3. **`create_new` 同时写内存和 DB**：和当前 `agent_new_session` handler 行为一致。
4. **lock 不随 session 清理而删除**：`asyncio.Lock` 对象很轻，留着避免竞态。
5. **`get()` 公开只读访问**：`agent_confirm` 等只读场景通过 `session_mgr.get(session_id)` 访问 session，不直接访问 `_sessions` 私有属性。

## api.py 改动

### agent_chat handler（重构后）

```python
@app.post("/api/agent/chat")
async def agent_chat(request: Request):
    if ai_config is None:
        raise HTTPException(status_code=503, detail="AI not configured")
    body = await request.json()
    message = body.get("message", "")
    session_id = body.get("session_id", "default")
    doc_path = body.get("doc_path", "")

    session_mgr.cleanup()
    session = session_mgr.get_or_create(session_id)
    session["last_active"] = time.time()

    # Pre-check doc_path（不修改 session，只验证；409 走 HTTP 异常）
    doc_context = None
    if doc_path:
        rel = _safe_rel_path(doc_path)
        if rel is None:
            pass  # skip
        elif not (vault_dir / rel).is_file():
            raise HTTPException(status_code=409, detail=f"doc_path '{rel.as_posix()}' not on disk")
        else:
            doc_context = rel

    lock = session_mgr.get_lock(session_id)
    await lock.acquire()

    async def event_generator():
        try:
            # doc_context 注入（在 lock 内，修改 session["messages"]）
            if doc_context:
                rel_str = doc_context.as_posix()
                last_read_ts = session.get("last_read_ts", {}).get(rel_str, 0)
                warning = ""
                if last_read_ts > 0 and db_conn:
                    from .db import get_changes
                    changes = get_changes(db_conn, since=last_read_ts, path=rel_str, limit=1)
                    if changes:
                        warning = " ⚠ 自上次读取后有修改"
                doc_ctx = f"[当前打开: {rel_str}{warning}]"
                session["messages"] = [
                    m for m in session["messages"]
                    if not (m.get("role") == "user" and m.get("content", "").startswith("[当前打开:"))
                ]
                session["messages"].append({"role": "user", "content": doc_ctx})

            # user message append（在 lock 内）
            session["messages"].append({"role": "user", "content": message})

            async for event in agent_loop(
                messages=session["messages"],
                tools=TOOLS,
                ai_config=ai_config,
                pending_confirms=session["pending_confirms"],
                vault_dir=vault_dir,
                db_conn=db_conn,
                last_read_ts=session.setdefault("last_read_ts", {}),
                context_window=ai_config.context_window,
                max_rounds=ai_config.max_rounds,
                read_doc_max_chars=ai_config.read_doc_max_chars,
            ):
                yield event
            session_mgr.save(session_id)
        except (GeneratorExit, asyncio.CancelledError):
            logger.error(
                "SSE stream disconnected (session=%s, pending_confirms=%d)",
                session_id, len(session["pending_confirms"]),
                exc_info=True,
            )
            raise
        finally:
            lock.release()

    from fastapi.responses import StreamingResponse
    return StreamingResponse(event_generator(), media_type="text/event-stream")
```

**关键设计：**
- 所有对 `session["messages"]` 的修改（doc_context 注入 + user message append）都在 lock 内
- doc_path 的 pre-check（`_safe_rel_path` + `is_file`）在 lock 外，409 错误仍走 HTTP 异常
- `lock.release()` 在 generator 的 `finally`，保证 StreamingResponse 消费完毕后释放
- 请求 B 在 `await lock.acquire()` 阻塞，直到请求 A 的 generator 完全结束

### agent_new_session handler（重构后）

```python
@app.post("/api/agent/new-session")
async def agent_new_session(request: Request) -> dict:
    new_id = session_mgr.create_new()
    return {"session_id": new_id}
```

### 删除的代码

- `_agent_sessions` 闭包变量（:67）
- `_SESSION_TTL` 常量（:68）
- `_cleanup_sessions()` 闭包函数（:72-76）
- agent_chat 内的 if/else session 创建逻辑（:274-294）
- agent_new_session 内的内联创建逻辑（:386-410）

## agent/__init__.py 改动

新增 re-export：
```python
from .session import SessionManager
```

## 测试

新增 `tests/test_session_manager.py`：

| 测试 | 验证 |
|------|------|
| `test_get_or_create_new` | 不存在的 session_id → 创建新 session（含 system prompt） |
| `test_get_or_create_restore` | DB 有记录、内存无 → 从 DB 恢复 |
| `test_create_new` | 返回新 uuid，内存和 DB 都有记录 |
| `test_cleanup_expired` | 超过 TTL 的 session 被清理 |
| `test_cleanup_skips_locked` | lock 被持有的 session 不被清理 |
| `test_get_lock_same_instance` | 同一 session_id 返回同一个 Lock 对象 |
| `test_save` | 调用后 DB 有更新 |

现有测试兼容性：
- `tests/test_api.py` 中直接操作 `_agent_sessions` 的测试需要改为通过 `session_mgr` 访问
- 检查是否有测试 mock 了 `_cleanup_sessions`

## 验证

1. `pytest tests/ -v` 全部通过
2. 手动验证：两个终端同时 curl `/api/agent/chat`（同一 session_id）→ 第二个请求阻塞直到第一个完成
3. 前端连续 Enter → 后端不出现并行 agent_loop
