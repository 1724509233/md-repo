# 计划：Card 2 — api.py 拆 domain route modules（+ Card 3：proposals 读面下沉）

分支 `feat/memory-runtime`（不切分支）。解释器一律 `.venv/bin/python`。

## 上下文

- **卡片来源**：架构评审 2026-10-28（`/var/folders/j1/cnqh9jhs7tq47sslcqyj0pz80000gn/T/opencode/architecture-review-20261028-1711.html`）Card 2「api.py：30 个闭包共享一个 app 级 seam」+ Card 3「proposals 的 DB 形状漏进 api.py」。卡片明文：read module「与 Card 3 同一次落地」。
- **设计评审**：oracle grilling（ses_e7d157fd3ffeHhADFeIw2S0uLX）——决策表 D1–D8、风险 R1–R8（路由匹配行为已在 FastAPI 0.128.8/Starlette 0.49.3 实测）、冻结决策 F1–F9 为本计划依据。
- **用户拍板（2026-10-28）**：① 一个计划、三 commit（A+B+C），Card 3 同批落地；② 域模块命名 `console`；③ SyncLock 最小面（`locked_at` 字段可直戳，不做时间注入）。
- **基线**：`703 passed, 1 skipped`（2026-10-28 实测；Card 1 收官态）。

## 关键实测事实（reviewer 复核依据）

1. **路由乱序 = 静默故障**：Starlette 匹配 = 注册顺序第一个 FULL match；重复 path+method 不报错，先注册者赢、后者静默遮蔽；`{relpath:path}` 贪婪 `.*`（含 `/`）。后果实测：manage 乱序 → manage 面板全 422（upload 闭包接走）；detail 抢在 trail 前 → 400 "not a top-level proposal file" 静默漂移。**顺序约束全部在模块内**（6 域 URL 前缀互不重叠，跨模块顺序无约束）。
2. **契约消费方两个**：Web UI（templates/js/，31 条 URL 一一对应）+ client 侧（client/sync.py：`/api/sync` + `GET/PUT /api/docs/{path}`）。
3. **测试戳 global 共 3 处**：test_api.py:28（`_safe_rel_path` 别名断言）、:151/:164（`_sync_lock`）。全库 `server.api` importer 除 create_app 外无其他符号依赖。
4. 模块级死 import `upsert_file`（api.py:27，Card 1 前已死）——Task B 搬家时 console.py 不引入。

## Global Constraints

1. **零行为改动（Task A/B）**：31 条路由的 path、method、注册顺序、返回类型注解、response_class、media_type、status 语义、ETag/304 块、两个 StreamingResponse 形状（ai_chat `yield f"{line}\n\n"` vs agent_chat `yield sse(...)` 不得统一）、503 语义（ai/search **没有** 503 检查，别顺手对齐）、`sort_keys=True`（ETag 契约）——全部逐字。注释是行为史（双行协议、D25、P1 可逆层），随代码逐字搬家。
2. **冻结区（只搬不改）**：memory_status 的 4 路非 proposals SQL（trigger_state :970-982、executor 两查询 :1008-1015、recall 降级 try/except :986-998、`snapshots.degraded_count()` 调用 :1027）；recall telemetry try/except（ai_search :599-605）；`_proposal_trails` 的 D25 注释（:86-101）；manage 的 ledger.record/record_folder 调用形状（from_path/update_refs 参数 + skip 注释）；`snapshots.degraded_count()` 模块 global（Card 5 范围）；`_update_references_batch` 下划线跨包 import（函数内延迟 import 原样保留，**记给 Card 5 批次不修**）。
3. **前端/客户端契约**：31 条 URL 字符串与 body 形状一字不动；路由表顺序快照测试为 pin。
4. 测试夹具写文件一律 `write_bytes`（Windows newline 纪律）。
5. **git hygiene**：每组 commit 前先 `git reset` 清空 index，再逐组 `git add` + commit（Card 1 实测踩过的混卷陷阱）。
6. 三个 commit 顺序 A → B → C，每个 commit 后跑全量 pytest。
7. 不得触碰：`server/ledger.py`、`server/model.py`、`server/config/*`、`server/proposals.py`（Task C 例外）、`docs/*.html`、用户本地 `config.yaml`、`server/snapshots.py`。

---

### Task A：SyncLock 注入 + WebContext + app.state.ctx（seam 建立）

**Files:**
- Create: `server/sync_lock.py`
- Create: `server/routes/__init__.py`（Task A 只放 WebContext；Task B 补 ROUTE_MODULES）
- Modify: `server/api.py`（删 :32-49 模块 global + `_acquire_lock`/`_release_lock`；create_app 构造 ctx；sync 闭包换调用）
- Modify: `tests/test_api.py`（重写 `test_sync_lock` :149-164）

**Interfaces:**
- Produces（B/C 全部依赖的精确签名）:
  - `SyncLock`：`__init__(self) -> None`；`locked_at: float | None`（字段）；`try_acquire(self) -> bool`；`release(self) -> None`
  - `WebContext`（dataclass，`server/routes/__init__.py`）：字段 `db_conn: object` / `vault_dir: Path` / `ai_config: object | None` / `sync_lock: SyncLock` / `bg_targets: dict[str, dict] | None = None`
  - create_app 签名**不变**：`(vault_dir: Path, db_conn, ai_config=None, bg_targets: dict[str, dict] | None = None) -> FastAPI`
  - 测试 seam：`app.state.ctx`（TestClient 下 `client.app.state.ctx`）

- [ ] **Step 1: 基线测试**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: `703 passed, 1 skipped`（若不一致，停下报告）

- [ ] **Step 2: 创建 `server/sync_lock.py`（逐字）**

```python
"""In-memory sync lock: one sync at a time per server process.

A SyncLock instance is constructed by create_app and injected into the
WebContext (app.state.ctx) — tests drive it through that seam instead of
poking a module global (the old server.api._sync_lock dict). A lock
older than the timeout is stale and re-acquirable (a crashed sync must
not wedge the server).
"""
from __future__ import annotations

import time

_TIMEOUT = 300  # 5 minutes


class SyncLock:
    """State is ``locked_at`` (the acquire timestamp, or None)."""

    def __init__(self) -> None:
        self.locked_at: float | None = None

    def try_acquire(self) -> bool:
        """True if acquired (or the previous holder is stale)."""
        now = time.time()
        if self.locked_at is not None and (now - self.locked_at) < _TIMEOUT:
            return False
        self.locked_at = now
        return True

    def release(self) -> None:
        self.locked_at = None
```

- [ ] **Step 3: 创建 `server/routes/__init__.py`（逐字；Task B 在其后补两段）**

```python
"""Web route package: one module per URL-prefix domain.

WebContext is the per-create_app shared state handed to every route
module's ``register()`` — config/handle fields only, no domain-private
objects. create_app exposes it at ``app.state.ctx`` (the test seam —
tests drive injected objects, never module globals).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..sync_lock import SyncLock


@dataclass
class WebContext:
    """Per-app shared state for the route modules.

    ``bg_targets``: optional per-alias caps from the background-agent
    config (wired in main.py); only routes/memory reads it.
    """

    db_conn: object
    vault_dir: Path
    ai_config: object | None
    sync_lock: SyncLock
    bg_targets: dict[str, dict] | None = None
```

- [ ] **Step 4: api.py 三处改动**

1. 删 :32-49 整块（`# In-memory sync lock...` 注释 + `_sync_lock` + `_SYNC_LOCK_TIMEOUT` + `_acquire_lock` + `_release_lock`）。
2. create_app 内 `app.mount("/static", ...)` 行之后插入（templates/session_mgr/agent_cache 等保持原位，Task B 再动）：

```python
    ctx = WebContext(
        db_conn=db_conn,
        vault_dir=vault_dir,
        ai_config=ai_config,
        sync_lock=SyncLock(),
        bg_targets=bg_targets,
    )
    app.state.ctx = ctx
```

   同时模块级新增 `from .routes import WebContext`、`from .sync_lock import SyncLock`。
3. sync 闭包（现 :539/:587）：`if not _acquire_lock():` → `if not ctx.sync_lock.try_acquire():`；`finally: _release_lock()` → `finally: ctx.sync_lock.release()`。闭包捕获 create_app 局部 `ctx`，零其他改动。

- [ ] **Step 5: 重写 `tests/test_api.py::test_sync_lock`（:149-164 整函数替换，逐字）**

```python
def test_sync_lock(client) -> None:
    # Simulate another client holding the lock (fresh timestamp), via the
    # injected lock (app.state.ctx) — no module-global poking.
    lock = client.app.state.ctx.sync_lock
    lock.locked_at = time.time()
    try:
        r = client.post(
            "/api/sync",
            json={
                "device_name": "mac",
                "local_files": {},
                "logs": [],
            },
        )
        assert r.status_code == 409
        assert r.json() == {"error": "sync in progress"}
    finally:
        lock.locked_at = None
```

（`from server import api as api_module` 的 import :9 此时仍被 :28 使用，保留到 Task B。）

- [ ] **Step 6: 验证门槛**

```bash
.venv/bin/python -m pytest tests/ -q                                    # 期望 703 passed, 1 skipped
grep -n "_sync_lock\|_acquire_lock\|_release_lock" server/api.py        # 期望零输出
grep -rn "api_module._sync_lock" tests/                                 # 期望零输出
# Gate 4（2026-10-28 自检修正）：create_app 签名未变。
# 原逐字命令断言 `str(inspect.signature(...))` 等于未字符串化的注解串——
# api.py 既有 `from __future__ import annotations`（PEP 563），渲染结果带引号
# （'Path'）且含返回注解，期望串逐字不可能命中（改动前同样跑不过）。
# 改为源码级 def 行对比 HEAD：
diff <(git show HEAD:server/api.py | grep -A1 "^def create_app") \
     <(grep -A1 "^def create_app" server/api.py) && echo "sig unchanged"   # 期望两行全等
```

- [ ] **Step 7: commit（reset 后）**

```bash
git reset
git add server/sync_lock.py server/routes/__init__.py server/api.py tests/test_api.py
git commit -m "feat: sync 锁注入化 — SyncLock 经 WebContext（app.state.ctx）注入，测试不再戳模块 global"
```

---

### Task B：6 模块抽取 + WebContext 接线 + 路由表顺序快照测试（纯搬家）

**Files:**
- Create: `server/routes/{console,notifications,sync,recall,memory,agent}.py`
- Modify: `server/routes/__init__.py`（补 ROUTE_MODULES + register_all）
- Modify: `server/api.py`（收缩为 wiring，终态 ~60 行）
- Create: `tests/test_routes_order.py`
- Modify: `tests/test_api.py`（:23-28 别名断言重指 routes.console；删 :9 的 api_module import）
- Modify: `AGENTS.md`（Architecture 节 server/ 行，见 Step 7）

**搬家映射表**（现 api.py 行号 → 目标；模块内注册顺序 = 现状全局相对顺序，逐位不变）：

`routes/console.py`（10 条，注册顺序 1→10）：

| # | 路由 | 现状行号 | 注 |
|---|---|---|---|
| 1 | GET `/` | 231-233 | `Jinja2Templates(directory=...)` 在 register 闭包内自建（server/templates 目录不动）；`response_class=HTMLResponse` |
| 2 | GET `/api/health` | 235-238 | |
| 3 | GET `/api/docs` | 240-264 | ETag/304 块 + `sort_keys=True` 字节级保留 |
| 4 | GET `/api/docs/changes` | 266-270 | 必须在 get_doc（catchall）前注册 |
| 5 | GET `/api/graph/backlinks` | 272-276 | |
| 6 | GET `/api/graph/related` | 278-282 | |
| 7 | GET `/api/docs/{path:path}` | 284-293 | `PlainTextResponse(..., media_type="text/markdown; charset=utf-8")` 保留 |
| 8 | POST `/api/docs/manage` | 295-477 | dispatcher + 4 action 函数（下）；:297-298 顺序注释保留 |
| 9 | POST `/api/docs/{path:path}`（upload） | 479-494 | 必须在 manage 后（现状即然） |
| 10 | PUT `/api/docs/{path:path}`（save_doc） | 1159-1173 | **不搬 `existed = file_path.is_file()`（:1166 死变量）** |

   - 模块级随迁：`_hash_bytes`（:52-53）→ console 私有；`safe_rel_path as _safe_rel_path` 与 `EXCLUDED_INDEX_DIRS` import（:28）→ console；函数内延迟 import（refs/snapshots/vault.db.changes）随 action 体原样。
   - import 行 `from .vault.service import get_all_files`（**不含 `upsert_file`** —— Card 1 前已死的 import，自然消失）。

   **manage_docs 拆分（F5）**：dispatcher 保留 body 解析、action 分发、`unknown action` 400；4 个私有函数签名 `(db_conn, vault_dir, body) -> dict`（可直抛 HTTPException）：`create_file`（:304-321 体）、`create_folder`（:322-334 体）、`move`（:335-385 体，`rename` 与 `move` 两个 action 字符串共用）、`delete`（:386-476 体）。分支内注释、ledger 调用形状、快照调用逐字进对应函数。dispatcher 内 `from .vault.orchestration.refs import _update_references_batch` 等延迟 import 随 move/delete 体走。

`routes/notifications.py`（2 条）：496-520 原样（raw SQL 冻结，本域唯一数据源）。

`routes/sync.py`（1 条）：522-587 原样；`from ..sync import compare_files`、`re`、`get_all_files`；`ctx.sync_lock` 调用保持 Task A 形态。

`routes/recall.py`（3 条）：589-632 原样（含 recall telemetry try/except，冻结）。

`routes/memory.py`（11 条，注册顺序：list → adopt → revive → reject → trail → detail → rollback → rollback-calls → reports → report_detail → status）：
- 634-637 节注释（constitution）与 876-879 节注释（report channel）随迁。
- 模块级 helper 迁入 memory.py 顶部（Task C 再把形状读 4 件下沉 proposals.py）：`_PROPOSAL_STATUS_ORDER`（:56-70 含注释）、`_proposal_title`（:73-78）、`_proposal_sort_key`（:81-83）、`_proposal_trails`（:86-140，**D25 注释随迁**）、`_guard_proposal_relpath`（:143-161）、`_guard_report_relpath`（:164-181）。
- **顺序敏感注释**（模块头 + detail 路由上各一条）：`{relpath}/adopt|revive|reject|trail` 四条必须先于 `GET {relpath:path}`（detail）注册——`:path` 贪婪，实测乱序 → 400 静默漂移。现状顺序已满足，冻结。
- memory_status（:930-1027）四路非 proposals SQL **原样保留**（F9）。

`routes/agent.py`（4 条：ai_chat 1029-1077、agent_chat 1079-1136、agent_confirm 1138-1152、agent_new_session 1154-1157）：
- `session_mgr = SessionManager(db_conn, vault_dir)`（:199）、`agent_cache` dict（:202）、`_build_agent_spec`（:204-221）、`_get_agent`（:223-229）**全部在 register 闭包内**——chat/confirm/new-session 三端点共享 session_mgr 与 agent_cache，抽函数时不得拆到不同作用域（R8.3）。
- import 随迁：AgentSpec/ModelConfig/SYSTEM_PROMPT/sse/TOOL_REGISTRY/Agent/SessionManager/TimeSystemReminder/DocPathUserReminder/asyncio/sys。

`routes/__init__.py` 补（Step 3 文件之后追加）：

```python
from . import agent, console, memory, notifications, recall, sync

# Fixed order. Cross-domain URL prefixes do not overlap today, so this
# tuple is an explicit freeze, not a functional requirement; the
# order-sensitive registrations are within each module (see module docs).
# Pinned by tests/test_routes_order.py.
ROUTE_MODULES = (console, notifications, sync, recall, memory, agent)


def register_all(app, ctx: WebContext) -> None:
    for mod in ROUTE_MODULES:
        mod.register(app, ctx)
```

`server/api.py` 终态（逐字骨架；import 行以实际为准）：

```python
"""FastAPI application factory (wiring only).

The route domains live in server/routes/ — one module per URL-prefix
domain, each registering thin dispatcher closures over the WebContext.
create_app builds the context, mounts /static, and registers the routes
in ROUTE_MODULES order. The route table (paths, methods, order) is
pinned by tests/test_routes_order.py.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .routes import WebContext, register_all
from .sync_lock import SyncLock


def create_app(vault_dir: Path, db_conn, ai_config=None,
               bg_targets: dict[str, dict] | None = None) -> FastAPI:
    """Create the FastAPI application.

    ``bg_targets``: optional per-alias caps from the background-agent
    config, ``{alias: {"armed_target": int | None, "runs_cap": int | None}}``
    (wired in main.py from BackgroundAgentConfig: trigger.arm_count /
    budget_runs_per_day). None → the status endpoint reports the
    config-derived caps as null (pre-memory-console shape, backward
    compatible).
    """
    app = FastAPI(title="md-sync server")
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "templates")), name="static")
    ctx = WebContext(
        db_conn=db_conn,
        vault_dir=vault_dir,
        ai_config=ai_config,
        sync_lock=SyncLock(),
        bg_targets=bg_targets,
    )
    app.state.ctx = ctx
    register_all(app, ctx)
    return app
```

`tests/test_routes_order.py`（新建；`EXPECTED` 的执行真值由 Step 2 从 Task A 后的 app 实测取回并逐条写入）：

```python
"""Route-table order snapshot: the 31 routes' (methods, path) sequence,
the /static mount, and the no-duplicate invariant.

Starlette matches the FIRST fully-matching route in registration order
and silently shadows later duplicates (reviewer-measured), so order IS
behavior: within console, manage must precede the upload catchall and
docs/changes must precede get_doc; within memory, the {relpath}/subpath
routes must precede the detail catchall. This snapshot pins the whole
table — a missing, moved, or duplicated route turns it red.
"""
from __future__ import annotations

from fastapi.routing import APIRoute
from starlette.routing import Mount

from server.api import create_app  # noqa: F401  (api 面 import 存活证明；app fixture 来自 conftest)


# (methods, path) — in registration order. 31 routes. 执行时填回 Step 2 实测表。
EXPECTED = (
    # (("GET",), "/"),
    # ... 31 条 ...
)


def _table(app):
    return [
        (frozenset(r.methods), r.path)
        for r in app.router.routes
        if isinstance(r, APIRoute)
    ]


def test_route_order_snapshot(app):
    assert _table(app) == [(frozenset(m), p) for m, p in EXPECTED]
    mounts = [r for r in app.router.routes if isinstance(r, Mount)]
    assert [m.path for m in mounts] == ["/static"]


def test_no_duplicate_routes(app):
    seen = set()
    for methods, path in _table(app):
        for m in methods:
            assert (m, path) not in seen, f"duplicate {m} {path} (silent shadowing)"
            seen.add((m, path))
```

（两条测试的断言语义 = 31 条序列全等 + mount 单列 + 无重复，不得弱化；`app` fixture 由 conftest 提供，无需新增。）

- [ ] **Step 1: 基线复核**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: `703 passed, 1 skipped`

- [ ] **Step 2: 取回当前路由表（快照基准）+ 填 EXPECTED（新布局序）**

```bash
.venv/bin/python -c "
import tempfile
from pathlib import Path
from server.api import create_app
from server.vault import init_db
from fastapi.routing import APIRoute
with tempfile.TemporaryDirectory() as td:
    v = Path(td) / 'vault'; v.mkdir()
    conn = init_db(v / 'index.db')
    app = create_app(v, conn)
    for i, r in enumerate(x for x in app.router.routes if isinstance(x, APIRoute)):
        print(i, sorted(r.methods), r.path)
"
```

核对三点：
1. 共 31 条；`(methods, path)` 多重集与搬家映射表的 31 条全等（**不看顺序**）。
2. 已知唯一顺序差异：现状表里 `PUT /api/docs/{path:path}`（save_doc）在**末位**（agent 域 4 条之后，现 api.py :1151 > :1146）；新布局中它是 console 域第 10 条（agent 域之前）。可证零行为影响：与 agent 域 4 条（`POST /api/ai/chat`、`POST /api/agent/chat|confirm|new-session`）path 与 method 均不重叠，注册顺序不参与匹配结果。
3. 模块内顺序敏感三点在现状表成立（manage⪯upload、changes⪯get_doc、memory subpath 四条⪯detail），新布局保持。

`EXPECTED` 按**新布局序**填写（= ROUTE_MODULES 顺序展开：console 10 条按映射表 1→10（save_doc 第 10 位）、notifications 2、sync 1、recall 3、memory 11、agent 4），**不是**上面实测的旧序。测试断言的是 B 之后 app 的表 == EXPECTED（新序）。

- [ ] **Step 3: 创建 6 个 route 模块**

按搬家映射表逐模块创建。纪律：
- 闭包体逐字搬（含注释）；仅改 3 处：`@app.X` 装饰器保留（register 参数仍叫 `app`）、`db_conn`/`vault_dir`/`ai_config`/`bg_targets` 闭包变量改为 `ctx.db_conn` 等、agent 域 4 件 setup 进 register 闭包。
- **例外**：console 的 save_doc 不搬 `existed` 死变量；console 的 import 不含 `upsert_file`。
- manage dispatcher 拆 4 action 函数（映射表 F5 规格）；其余 30 条闭包不拆。
- 每模块头部 docstring 一行：域名 + URL 前缀 + 顺序敏感约束（console：manage⪯upload、changes⪯get_doc；memory：subpath⪯detail）。

- [ ] **Step 4: routes/__init__.py 补 ROUTE_MODULES + register_all（逐字见上）**

- [ ] **Step 5: api.py 收缩为终态骨架（逐字见上）；删除全部闭包、helper、import 残渣**

验证 `wc -l server/api.py` < 80。

- [ ] **Step 6: 测试两处改写**

1. `tests/test_api.py:23-28`（`test_safe_rel_path_alias_is_shared_vault_guard`）：断言重指：

```python
def test_safe_rel_path_alias_is_shared_vault_guard() -> None:
    # _safe_rel_path moved to server.vault.fs.vault_ops (shared by the
    # executor and the API); the console route module keeps the private alias.
    from server.routes import console
    from server.vault.fs.vault_ops import safe_rel_path

    assert console._safe_rel_path is safe_rel_path
```

   同时删除 :9 `from server import api as api_module`（3 个使用点至此全改完）。
2. `tests/test_routes_order.py` 的 `EXPECTED` 与断言填实（Step 2 真值）。

- [ ] **Step 7: AGENTS.md Architecture 节 server/ 行刷新（逐字）+ server/README.md 锁描述刷新**

旧：`- **server/** — FastAPI app. \`api.py\` has the \`create_app(vault_dir, db_conn, ai_config)\` factory; all routes are closures inside it. \`main.py\` is the entry point that wires config → db → app → uvicorn.`
新：`- **server/** — FastAPI app. \`api.py\` has the \`create_app(vault_dir, db_conn, ai_config, bg_targets)\` factory (wiring only); routes live in \`server/routes/\` (one module per URL-prefix domain: console/notifications/sync/recall/memory/agent), each registering via \`register(app, ctx)\` on the \`WebContext\` (exposed at \`app.state.ctx\`). Route-table order is pinned by \`tests/test_routes_order.py\`. \`main.py\` is the entry point that wires config → db → app → uvicorn.`

`server/README.md`「注意事项」的 `_sync_lock` 行（Task A oracle 过门发现，文档漂移）：
- 旧：`- **\`_sync_lock\` 是内存全局变量**: 进程重启后重置，测试中可直接设置模拟锁定状态。`
- 新：`- **sync 锁是注入的**: \`SyncLock\` 实例由 create_app 构造并注入 \`WebContext\`（\`app.state.ctx.sync_lock\`），测试经该 seam 直设模拟锁定；进程重启后重置。`

- [ ] **Step 8: 验证门槛**

```bash
.venv/bin/python -m pytest tests/ -q            # 期望 705 passed, 1 skipped（703 + 2 快照测试）
.venv/bin/python -c "
from server.api import create_app
from server.routes import ROUTE_MODULES, WebContext
import server.routes.console, server.routes.notifications, server.routes.sync
import server.routes.recall, server.routes.memory, server.routes.agent
assert len(ROUTE_MODULES) == 6
print('modules ok')
"
grep -c "@app\." server/api.py                 # 期望 0（无残留路由装饰器；grep 计数 0 时 exit 1）
grep -n "_proposal_trails\|_PROPOSAL_STATUS_ORDER\|_proposal_title\|_proposal_sort_key\|_hash_bytes\|_safe_rel_path" server/api.py   # 期望零输出（helper 全迁）
wc -l server/api.py                            # 期望 < 80
```

- [ ] **Step 9: commit（reset 后）**

```bash
git reset
git add server/api.py server/routes/ tests/test_api.py tests/test_routes_order.py AGENTS.md server/README.md
git commit -m "refactor: api.py 拆 6 个 domain route 模块（server/routes/）+ WebContext 注入 + 路由表顺序快照测试（零行为改动）"
```

---

### Task C：Card 3 — proposals 读面下沉（routes/memory 变薄 dispatcher）

**Files:**
- Modify: `server/proposals.py`（新增 5 件读面，SQL 从 routes/memory.py 逐字搬入）
- Modify: `server/routes/memory.py`（trail 端点 / memory_proposals 列表 / memory_status 的 proposals 段改调用；删本地形状读 helper）
- Modify: `tests/test_memory_api.py`（新增 3 个读面单元测试）

**Interfaces（新读面，形状按代码库惯例显式收 conn）：**

| 新符号 | 吸收的 routes/memory.py 代码 | 返回形状 |
|---|---|---|
| `STATUS_ORDER`（模块常量） | `_PROPOSAL_STATUS_ORDER`（:56-70 含注释） | 8+1 状态 → rank dict，逐字 |
| `title(record: dict) -> str` | `_proposal_title`（:73-78） | 首 source_ref，回退 type |
| `list_trails(conn, items: list[dict]) -> list[list[dict]]` | `_proposal_trails`（:86-140，**D25 注释随迁**） | 与 items 平行；row dict `{seq, ts, source, op, path}` 不变 |
| `trail(conn, relpath: str) -> list[dict]` | trail 端点 SQL 全块（:774-796 两分支：有记录 → transitions + batch 行；无记录 → change_log tail fallback） | **未截断** new-first 行列表（`[:12]` 截断是路由 presentation，留路由） |
| `status_summary(conn, now: float \| None = None) -> dict` | memory_status 的 proposals 段（:938-962：scan 驱动 + title + 全 9 状态 by_status + awaiting 队列 age 降序） | 即端点 `data["proposals"]` 段完整形状 `{"by_status": {...}, "awaiting_review": [...]}` |

**留 routes/memory.py 的**：`_guard_proposal_relpath`/`_guard_report_relpath`（抛 HTTPException = HTTP 语义，路由层）；trigger_state 查询、executor 两查询、recall 降级块、`snapshots.degraded_count()` 调用（F9 冻结区）；`_proposal_sort_key` 改名 `_sort_key` 保留在 memory.py（薄函数，引用 `proposals.STATUS_ORDER`——排序键是 console 列表的 presentation，不下沉）。

**调用点改造（routes/memory.py）：**
- `memory_proposal_trail` 路由体：SQL 块 → `rows = proposals.trail(conn, rel)`；`rows.sort(key=lambda d: d["ts"], reverse=True)` 若已在函数内实现则随迁，路由只留 guard + `rows[:12]` + 响应拼装。
- `memory_proposals` 列表端点：`_proposal_title(p)` → `proposals.title(p)`；`_proposal_trails(db_conn, items)` → `proposals.list_trails(db_conn, items)`；`_proposal_sort_key` → `_sort_key`（引用 `proposals.STATUS_ORDER`）。
- `memory_status`：proposals 段（:938-962）→ `data["proposals"] = proposals.status_summary(conn)`（now 取端点局部 `time.time()`，经参数传入）。

- [ ] **Step 1: 基线复核**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: `705 passed, 1 skipped`

- [ ] **Step 2: proposals.py 新增 5 件读面**

SQL 文本、row dict 形状、D25 注释从 routes/memory.py **逐字**搬入（搬家 + 去下划线 = 成为 interface，不重写）。函数 docstring 说明形状与「schema 只被 proposals 认识」的意图。proposals.py 现有 import 不动（读面只用 conn.execute，无新依赖）。

- [ ] **Step 3: routes/memory.py 调用点改造 + 删本地 helper**

删 `_PROPOSAL_STATUS_ORDER`/`_proposal_title`/`_proposal_trails`（已下沉）；`_proposal_sort_key` 改名 `_sort_key` 并改引用 `proposals.STATUS_ORDER`；guards 与其他冻结区不动。memory 路由内不得再出现 `proposal_transitions` 表名（Step 5 门槛）。

- [ ] **Step 4: tests/test_memory_api.py 新增 3 个读面单元测试**

经 interface 直测（文件现有 fixtures：`db_conn`、`vault_dir` + 现有 `_create`/`_to_awaiting` helper 复用）：
1. `test_trail_shape_and_order`：建一条 pending→processing→awaiting_review 记录，`proposals.trail(conn, rel)` 返回 new-first、row 键集合 == `{seq, ts, source, op, path}`、含 3 条 transitions 行；record-less relpath 的 change_log tail fallback 分支各 1 断言。
2. `test_list_trails_parallel`：两条记录（其一共享 run_id 的 batch 行场景至少断言平行性与空 items 返回 `[]`）。
3. `test_status_summary_shape`：by_status 键集合 == 9 状态全集（stable shape）、awaiting_review 队列元素键 == `{relpath, type, actor, age_days, title}`、age 降序。

（断言值以现有端点测试已钉的形状为准——端点形状不变，单元测试只是把「经 interface 可测」做实。）

- [ ] **Step 5: 验证门槛**

```bash
.venv/bin/python -m pytest tests/ -q                                    # 期望 708 passed, 1 skipped（705 + 3 新）
grep -n "proposal_transitions" server/routes/memory.py server/api.py    # 期望零输出（该表只被 proposals 认识）
grep -n "FROM proposals" server/routes/memory.py                        # 期望零输出
grep -c "def register" server/routes/memory.py                          # 期望 1（模块形态未变）
```

- [ ] **Step 6: commit（reset 后）**

```bash
git reset
git add server/proposals.py server/routes/memory.py tests/test_memory_api.py
git commit -m "refactor: proposals 读面下沉（trail/list_trails/status_summary/STATUS_ORDER/title）— memory 路由变薄 dispatcher，schema 只被 proposals 认识"
```

---

## 完成定义（Definition of Done）

- 三个 commit 落定：A（sync 锁注入）→ B（6 模块 + 快照测试）→ C（proposals 读面下沉）。
- `git status --short`：仅既有 3 个未跟踪 docs 文件 + 本计划文件。
- 终态全量测试 `708 passed, 1 skipped`；Task A 后 703、Task B 后 705 各自留痕。
- grep 门槛零输出：`_sync_lock|_acquire_lock|_release_lock` @ server/api.py；`api_module._sync_lock` @ tests/；`@app.` @ server/api.py（计数 0）；`proposal_transitions` @ routes/memory.py + api.py；`FROM proposals` @ routes/memory.py。
- `server/api.py` < 80 行，create_app = wiring only；阅读任一 `server/routes/<domain>.py` 即可完整理解该域路由（locality）。
- 路由表 31 条 (methods, path) 序列快照 + `/static` mount + 无重复，三条 pin 全绿。
- 前端/客户端契约零改动：31 条 URL 字符串 + body 形状（快照测试 + 既有 703 测试覆盖）。
- AGENTS.md server/ 行已刷新；用户侧提醒：本地 + Windows 侧 `pip install -e .`（egg-info 陈旧为既有问题）。

## Review Focus（独立 reviewer 过门/终审用）

1. **零行为核对（B）**：31 条路由 1:1（path/method/注解/response_class/media_type/status 语义）；快照 `EXPECTED` 与 Task A 后实测表逐位一致；注释逐字（抽 5 处行为史注释 diff 比对）。
2. **顺序敏感三点**：manage⪯upload、changes⪯get_doc、subpath 四条⪯detail——快照序列中位置与现状一致；模块内 docstring 注释在位。
3. **SyncLock 语义等价**：300s 窗口、持锁时 try_acquire False、release in finally；test_sync_lock 新写与原戳 global 等价（语义映射：dict `{"locked_at": t}` ↔ 字段 `locked_at = t`）。
4. **WebContext 恰好 5 字段**，无 domain 私有物（agent_cache/session_mgr/templates 在各自 register 闭包内；bg_targets 注释标明唯一读者）。
5. **C 的下沉 = 搬家 + 去下划线**：SQL 文本、D25 注释、row dict 形状与 routes/memory.py 原块 diff 一致；路由保留 presentation 截断（list `[:3]`、trail `[:12]`）；端点响应形状零改动（既有端点测试绿 = 证明）。
6. **client 侧契约**：/api/sync + docs GET/PUT 的既有测试覆盖未弱化。
7. **commit 卫生**：三个 commit 文件集纯净（A=4 文件、B=12 文件：api.py + routes/__init__ + 6 模块 + 2 测试 + AGENTS.md + server/README.md、C=3 文件）、消息逐字。

## 后续（不在本计划内）

- 4 项 Card 1 非阻塞发现（GLOSSARY 措辞 / ledger docstring 悬指 / archive 死遗留 / test_ledger TTL 残留）仍等用户发话。
- Card 4（main.py build_runtime + exclusion 去重）与 Card 5（vault _db_lock 再封）——各需新一轮 grilling；Card 5 批次接收本计划记下的两条：`snapshots.degraded_count()` 模块 global、`_update_references_batch` 下划线跨包 import。
