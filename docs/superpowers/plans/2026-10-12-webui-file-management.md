# Web UI 文件管理 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Web UI 文件树上提供文档/文件夹的新建、重命名、移动（拖拽）、递归删除，后端统一走 `POST /api/docs/manage`。

**Architecture:** 后端在 `server/api.py` 新增单一 manage 路由（5 个 action 分支），复用 `_safe_rel_path` / `register_write` / `_update_references_batch`，写盘后**同步**调用 `apply_change`（不依赖 1.5s debounce 的 watcher，保证前端立即 `loadDocs()` 读到新 DB）。前端在 `api.js` 加 `manage()` 封装，`app.js` 加 `manage()` 方法并把当前文档受影响逻辑从 `pollChanges` 抽成 `_applyChangeToCurrentDoc(changes)` 共用，`FileTree.js` 加操作菜单、行内编辑、拖拽。

**Tech Stack:** FastAPI + SQLite（`server.vault` 子系统）、Vue 3（CDN，无构建步骤）+ Tailwind Play CDN + Flowbite。

**Spec:** `docs/superpowers/specs/2026-10-09-webui-file-management-design.md`（2026-10-12 已审查更新，引用与测试策略以该版为准）

## Global Constraints

- 无认证、无 HTTPS（LAN-only）——不要加 auth。
- 所有用户路径必须过 `_safe_rel_path()`（`server/api.py:49`）；拒绝 `..` 与前导 `/`。
- 写盘**前**调用 `register_write(rel_path, "web")`（`server/write_registry.py:18`，docstring 契约是 before-write）。
- DB 更新走 `apply_change`（公共入口 `from .vault.service import apply_change`），source 一律 `"web"`；moved 场景传 `update_refs=False`（引用更新已由 `_update_references_batch` 批量完成）。
- 多文件 DB 操作（文件夹 rename/delete）包在 `with fts_batch():` 里（与 `reconcile` 一致，抑制逐次 FTS rebuild）。
- vault 导入一律走 `server.vault.service` 公共 API；`_update_references_batch` 是私有符号，在 handler 内局部导入（与 agent 工具 `move_docs.py:61` 同模式）。
- 测试 fixture 写文件用 `write_bytes`（Windows 换行转换会破坏 SHA-256 断言）。
- 前端动态构造的 Tailwind class 字符串必须完整（Play CDN 运行时生成，无部分匹配）。
- `server.vault` 顶层 shim 已 deprecated——新代码不要从 `server.vault` 直接 import 内部符号（测试文件里现有的 `from server.vault import get_all_files` 是既有写法，沿用即可）。

## Review Focus

spec 未明说、但真实使用会踩的输入/失败模式，各钉到所属 task：

1. **把文件夹拖进自己的子目录**（前端校验漏掉时后端兜底）→ 期望 400，不产生半完成 rename。钉到 Task 2 `test_manage_move_into_own_subdir`。
2. **递归删除含非 `.md` 文件的文件夹**（如 `a.md` + `b.txt`）→ 期望只删 `.md`，`b.txt` 保留，目录因非空**不**被 rmdir。钉到 Task 3 `test_manage_delete_folder_keeps_nonmd`。
3. **`create_file` 带 `content`** → DB hash 必须等于 content 的 sha256（不是空文件 hash），否则多端 sync 会误判冲突。钉到 Task 1 `test_manage_create_file_with_content`。
4. **重命名/移动后 `to_path` 恰好等于另一个已存在路径** → 409，源文件不动。钉到 Task 2 `test_manage_rename_conflict`。
5. **删除/移动当前正在编辑的文档**（前端状态）→ 编辑器清空或跟随新路径，不残留指向已删路径的 `currentPath`。钉到 Task 4 手动验证 + Task 6 删除确认。

## File Structure

| 文件 | 责任 |
|------|------|
| `server/api.py` | 新增 `POST /api/docs/manage` 路由（5 个 action 分支，全部在一个 handler 内） |
| `tests/test_api.py` | 17 个 manage 用例（Task 1-3） |
| `server/templates/js/api.js` | `manage(action, payload)` 封装 |
| `server/templates/js/app.js` | `manage()` 方法 + `_applyChangeToCurrentDoc(changes)` 抽取 |
| `server/templates/js/components/FileTree.js` | 操作菜单、行内编辑、拖拽、删除确认（改 `TreeNode` + `FileTree` 两处） |
| `server/templates/style.css` | 菜单浮层定位、drop 高亮（如需） |

---

### Task 1: manage 端点 — create_file / create_folder

**Files:**
- Modify: `server/api.py`（import 在 line 24；新路由插在 `get_doc` 结束 line 150 与 `upload_doc` line 152 **之间**）
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: `_safe_rel_path(raw: str) -> Path | None`（api.py:49）；`register_write(rel_path: str, source: str) -> None`（write_registry.py:18）；`apply_change(conn, vault_dir, path, action, source="unknown", from_path="", new_hash="", update_refs=True) -> None`（service 公共 API，action 取 `"created"`）；`get_all_files(db_conn) -> list[dict]`（测试断言用，test_api.py:10 已 import）
- Produces: `POST /api/docs/manage`，body `{action, ...}`；成功 `{"ok": true, "path": str}`；错误 400（路径非法/JSON 非法/未知 action）、409（已存在）。Task 2/3 在同一 handler 的 dispatch 上加分支。

- [ ] **Step 1: Write the failing tests**

在 `tests/test_api.py` 末尾追加（`hashlib`、`get_all_files` 已在文件头 import）：

```python
def test_manage_create_file(client, vault_dir, db_conn) -> None:
    r = client.post("/api/docs/manage", json={"action": "create_file", "path": "new/file.md"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "path": "new/file.md"}
    on_disk = vault_dir / "new" / "file.md"
    assert on_disk.is_file()
    assert on_disk.read_bytes() == b""
    paths = [f["path"] for f in get_all_files(db_conn)]
    assert "new/file.md" in paths


def test_manage_create_file_with_content(client, vault_dir, db_conn) -> None:
    content = "# Hello\n"
    r = client.post("/api/docs/manage", json={"action": "create_file", "path": "doc.md", "content": content})
    assert r.status_code == 200
    assert (vault_dir / "doc.md").read_bytes() == content.encode("utf-8")
    entry = next(f for f in get_all_files(db_conn) if f["path"] == "doc.md")
    assert entry["hash"] == hashlib.sha256(content.encode("utf-8")).hexdigest()


def test_manage_create_file_conflict(client) -> None:
    r = client.post("/api/docs/manage", json={"action": "create_file", "path": "notes/test.md"})
    assert r.status_code == 409


def test_manage_create_folder(client, vault_dir) -> None:
    r = client.post("/api/docs/manage", json={"action": "create_folder", "path": "new/sub"})
    assert r.status_code == 200
    assert (vault_dir / "new" / "sub").is_dir()


def test_manage_invalid_path(client) -> None:
    for path in ("../x.md", "/abs.md"):
        r = client.post("/api/docs/manage", json={"action": "create_file", "path": path})
        assert r.status_code == 400
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_api.py -k manage -v`
Expected: 5 个全 FAIL（404，路由不存在）

- [ ] **Step 3: Implement the route**

`server/api.py` line 24 改为：

```python
from .vault.service import apply_change, fts_batch, get_all_files, upsert_file
```

在 `get_doc`（line 150 结束）与 `upload_doc`（line 152）之间插入路由。**顺序是硬约束**：`POST /api/docs/{path:path}` 的 path 转换器会吞掉 `/api/docs/manage`，必须让 manage 先注册（现有 `/api/docs/changes` 也是靠注册顺序躲开的）：

```python
    @app.post("/api/docs/manage")
    async def manage_docs(request: Request) -> dict:
        # 必须注册在 POST /api/docs/{path:path}（upload）之前，否则 path 转换器吞掉 "manage"
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="invalid JSON body")
        action = body.get("action")
        if action == "create_file":
            rel = _safe_rel_path(body.get("path") or "")
            if rel is None:
                raise HTTPException(status_code=400, detail="invalid path")
            file_path = vault_dir / rel
            if file_path.exists():
                raise HTTPException(status_code=409, detail="already exists")
            content = (body.get("content") or "").encode("utf-8")
            from .write_registry import register_write

            register_write(rel.as_posix(), "web")
            try:
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_bytes(content)
            except OSError as e:
                raise HTTPException(status_code=500, detail=f"failed to create file: {e}")
            apply_change(db_conn, vault_dir, rel.as_posix(), "created", source="web")
            return {"ok": True, "path": rel.as_posix()}
        if action == "create_folder":
            rel = _safe_rel_path(body.get("path") or "")
            if rel is None:
                raise HTTPException(status_code=400, detail="invalid path")
            dir_path = vault_dir / rel
            if dir_path.exists():
                raise HTTPException(status_code=409, detail="already exists")
            try:
                dir_path.mkdir(parents=True)
            except OSError as e:
                raise HTTPException(status_code=500, detail=f"failed to create folder: {e}")
            return {"ok": True, "path": rel.as_posix()}
        raise HTTPException(status_code=400, detail=f"unknown action: {action}")
```

要点：`register_write` 在写盘**前**调用（registry docstring 契约）；`create_folder` 不写 DB（空目录不进索引，与现状一致）。

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_api.py -k manage -v`
Expected: 5 个全 PASS

- [ ] **Step 5: Run full API suite for regressions**

Run: `pytest tests/test_api.py -v`
Expected: 全部 PASS（重点确认 upload/save 路由未被新路由抢匹配）

- [ ] **Step 6: Commit**

```bash
git add server/api.py tests/test_api.py
git commit -m "feat(api): POST /api/docs/manage — create_file/create_folder"
```

---

### Task 2: manage 端点 — rename / move（文件 + 文件夹）

**Files:**
- Modify: `server/api.py`（`manage_docs` dispatch 内，替换 Task 1 末尾的 `raise ... unknown action` 之前插入分支）
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: Task 1 的 `manage_docs` handler；`_update_references_batch(conn, vault_dir, moves: list[tuple[str, str]], pre_rename: bool = False) -> int`（`server/vault/orchestration/refs.py:38`，handler 内局部导入）；`fts_batch()`（Task 1 已加入 line 24 import）
- Produces: `rename`/`move` 两 action（实现相同，move 仅语义为跨目录）；成功 `{"ok": true, "path": to_path}`；404（from 不存在）、409（to 已存在）、400（to 在 from 子目录内）。Task 3 继续在同一 dispatch 加分支。

- [ ] **Step 1: Write the failing tests**

在 `tests/test_api.py` 追加：

```python
def test_manage_rename_file_updates_refs(client, vault_dir, db_conn) -> None:
    (vault_dir / "notes" / "old.md").write_bytes(b"# Old")
    (vault_dir / "notes" / "ref.md").write_bytes(b"See [[notes/old.md]]")
    r = client.post("/api/docs/manage", json={"action": "rename", "from_path": "notes/old.md", "to_path": "notes/new.md"})
    assert r.status_code == 200
    assert (vault_dir / "notes" / "new.md").is_file()
    assert not (vault_dir / "notes" / "old.md").exists()
    assert "[[notes/new.md]]" in (vault_dir / "notes" / "ref.md").read_text()
    paths = [f["path"] for f in get_all_files(db_conn)]
    assert "notes/new.md" in paths and "notes/old.md" not in paths


def test_manage_rename_conflict(client, vault_dir) -> None:
    (vault_dir / "notes" / "other.md").write_bytes(b"# Other")
    r = client.post("/api/docs/manage", json={"action": "rename", "from_path": "notes/test.md", "to_path": "notes/other.md"})
    assert r.status_code == 409
    assert (vault_dir / "notes" / "test.md").is_file()  # 源文件不动


def test_manage_move_file(client, vault_dir) -> None:
    r = client.post("/api/docs/manage", json={"action": "move", "from_path": "notes/test.md", "to_path": "docs/test.md"})
    assert r.status_code == 200
    assert (vault_dir / "docs" / "test.md").is_file()
    assert not (vault_dir / "notes" / "test.md").exists()


def test_manage_move_into_nonexistent_dir(client, vault_dir) -> None:
    r = client.post("/api/docs/manage", json={"action": "move", "from_path": "notes/test.md", "to_path": "a/b/c/test.md"})
    assert r.status_code == 200
    assert (vault_dir / "a" / "b" / "c" / "test.md").is_file()


def test_manage_rename_folder_updates_refs(client, vault_dir, db_conn) -> None:
    sub = vault_dir / "notes" / "sub"
    sub.mkdir()
    (sub / "a.md").write_bytes(b"# A")
    (vault_dir / "notes" / "ref.md").write_bytes(b"See [[notes/sub/a.md]]")
    r = client.post("/api/docs/manage", json={"action": "rename", "from_path": "notes/sub", "to_path": "notes/sub2"})
    assert r.status_code == 200
    assert (vault_dir / "notes" / "sub2" / "a.md").is_file()
    assert not (vault_dir / "notes" / "sub").exists()
    assert "[[notes/sub2/a.md]]" in (vault_dir / "notes" / "ref.md").read_text()
    paths = [f["path"] for f in get_all_files(db_conn)]
    assert "notes/sub2/a.md" in paths and "notes/sub/a.md" not in paths


def test_manage_move_into_own_subdir(client) -> None:
    r = client.post("/api/docs/manage", json={"action": "rename", "from_path": "notes", "to_path": "notes/inner"})
    assert r.status_code == 400


def test_manage_not_found(client) -> None:
    r = client.post("/api/docs/manage", json={"action": "rename", "from_path": "nope.md", "to_path": "x.md"})
    assert r.status_code == 404
```

（`test_manage_move_into_own_subdir` 只断言 400——校验在 FS 操作前返回，无需验证磁盘状态。）

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_api.py -k "manage_rename or manage_move or manage_not_found" -v`
Expected: 7 个全 FAIL（400 unknown action）

- [ ] **Step 3: Implement rename/move 分支**

在 `manage_docs` 的 `create_folder` 分支之后、`raise ... unknown action` 之前插入：

```python
        if action in ("rename", "move"):
            src_rel = _safe_rel_path(body.get("from_path") or "")
            dst_rel = _safe_rel_path(body.get("to_path") or "")
            if src_rel is None or dst_rel is None:
                raise HTTPException(status_code=400, detail="invalid path")
            src, dst = vault_dir / src_rel, vault_dir / dst_rel
            if not src.exists():
                raise HTTPException(status_code=404, detail="not found")
            if dst.exists():
                raise HTTPException(status_code=409, detail="already exists")
            if dst_rel.as_posix().startswith(src_rel.as_posix() + "/"):
                raise HTTPException(status_code=400, detail="cannot move into own subdirectory")
            from .vault.orchestration.refs import _update_references_batch

            if src.is_file():
                moves = [(src_rel.as_posix(), dst_rel.as_posix())]
            else:  # 文件夹：展开为内部 .md 文件
                moves = [
                    (p.relative_to(vault_dir).as_posix(), dst_rel.as_posix() + "/" + p.name)
                    for p in sorted(src.rglob("*.md")) if p.is_file()
                ]
            from .write_registry import register_write

            for _, new in moves:
                register_write(new, "web")
            # pre_rename=True：rename 前更新引用，唯一性检查才正确（与 move_docs 工具一致）
            _update_references_batch(db_conn, vault_dir, moves, pre_rename=True)
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                src.rename(dst)
            except OSError as e:
                raise HTTPException(status_code=500, detail=f"failed to rename: {e}")
            with fts_batch():
                for old, new in moves:
                    apply_change(db_conn, vault_dir, new, "moved", source="web",
                                 from_path=old, update_refs=False)
            return {"ok": True, "path": dst_rel.as_posix()}
```

要点：`update_refs=False` 避免 `apply_change("moved")` 内部再跑一遍单文件引用更新（批量已做）；文件夹 rename 的 moves 列表即 spec「风险与注意」的方案（先 rglob 收集、批量更新引用、再 rename 目录）。

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_api.py -k manage -v`
Expected: 12 个全 PASS

- [ ] **Step 5: Commit**

```bash
git add server/api.py tests/test_api.py
git commit -m "feat(api): manage rename/move with batch wikilink ref updates"
```

---

### Task 3: manage 端点 — delete（文件 + 递归文件夹）

**Files:**
- Modify: `server/api.py`（`manage_docs` dispatch 内加 `delete` 分支）
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: Task 1/2 的 handler；`apply_change(..., action="deleted", source="web")`（deleted 分支内部自动调 `_remove_dead_links` 清理指向被删文件的 wikilink，无需额外调用）
- Produces: `delete` action；成功 `{"ok": true, "path": str, "deleted_count": int}`（文件=1，文件夹=内部 .md 数）；404（不存在）、400（路径非法）。manage 端点至此完整，Task 4+ 只动前端。

- [ ] **Step 1: Write the failing tests**

在 `tests/test_api.py` 追加：

```python
def test_manage_delete_file(client, vault_dir, db_conn) -> None:
    r = client.post("/api/docs/manage", json={"action": "delete", "path": "notes/test.md"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "path": "notes/test.md", "deleted_count": 1}
    assert not (vault_dir / "notes" / "test.md").exists()
    paths = [f["path"] for f in get_all_files(db_conn)]
    assert "notes/test.md" not in paths


def test_manage_delete_folder_recursive(client, vault_dir, db_conn) -> None:
    sub = vault_dir / "notes" / "sub"
    (sub / "deep").mkdir(parents=True)
    (sub / "a.md").write_bytes(b"# A")
    (sub / "deep" / "b.md").write_bytes(b"# B")
    r = client.post("/api/docs/manage", json={"action": "delete", "path": "notes/sub"})
    assert r.status_code == 200
    assert r.json()["deleted_count"] == 2
    assert not (vault_dir / "notes" / "sub").exists()  # 空目录自底向上清理
    paths = [f["path"] for f in get_all_files(db_conn)]
    assert "notes/sub/a.md" not in paths and "notes/sub/deep/b.md" not in paths


def test_manage_delete_folder_keeps_nonmd(client, vault_dir) -> None:
    sub = vault_dir / "notes" / "mixed"
    sub.mkdir()
    (sub / "a.md").write_bytes(b"# A")
    (sub / "b.txt").write_bytes(b"keep me")
    r = client.post("/api/docs/manage", json={"action": "delete", "path": "notes/mixed"})
    assert r.status_code == 200
    assert r.json()["deleted_count"] == 1
    assert (sub / "b.txt").is_file()      # 非 .md 保留
    assert sub.is_dir()                   # 目录非空，不删


def test_manage_delete_not_found(client) -> None:
    r = client.post("/api/docs/manage", json={"action": "delete", "path": "nope.md"})
    assert r.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_api.py -k "manage_delete" -v`
Expected: 4 个全 FAIL（400 unknown action）

- [ ] **Step 3: Implement delete 分支**

在 `manage_docs` 的 rename/move 分支之后、`raise ... unknown action` 之前插入：

```python
        if action == "delete":
            rel = _safe_rel_path(body.get("path") or "")
            if rel is None:
                raise HTTPException(status_code=400, detail="invalid path")
            target = vault_dir / rel
            if not target.exists():
                raise HTTPException(status_code=404, detail="not found")
            from .write_registry import register_write

            if target.is_file():
                register_write(rel.as_posix(), "web")
                try:
                    target.unlink()
                except OSError as e:
                    raise HTTPException(status_code=500, detail=f"failed to delete: {e}")
                apply_change(db_conn, vault_dir, rel.as_posix(), "deleted", source="web")
                return {"ok": True, "path": rel.as_posix(), "deleted_count": 1}
            # 文件夹：逐个删 .md，自底向上清理空目录（非空目录保留）
            md_files = sorted(p for p in target.rglob("*.md") if p.is_file())
            rels = []
            for p in md_files:
                r = p.relative_to(vault_dir).as_posix()
                register_write(r, "web")
                p.unlink()
                rels.append(r)
            for d in sorted((x for x in target.rglob("*") if x.is_dir()),
                            key=lambda x: len(x.parts), reverse=True):
                try:
                    d.rmdir()
                except OSError:
                    pass  # 非空（含非 .md 文件）则保留
            with fts_batch():
                for r in rels:
                    apply_change(db_conn, vault_dir, r, "deleted", source="web")
            return {"ok": True, "path": rel.as_posix(), "deleted_count": len(rels)}
```

要点：`apply_change("deleted")` 内部已调 `_remove_dead_links`（refs.py:119）清理指向被删文件的 wikilink，不要重复处理；`rmdir` 失败即目录非空，静默保留。

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_api.py -k manage -v`
Expected: 16 个全 PASS

- [ ] **Step 5: Run full suite**

Run: `pytest tests/ -v`
Expected: 全部 PASS（~72+16 个测试）

- [ ] **Step 6: Commit**

```bash
git add server/api.py tests/test_api.py
git commit -m "feat(api): manage delete — file + recursive folder with empty-dir cleanup"
```

---

### Task 4: 前端基础 — api.manage() + app.js manage() + 当前文档逻辑抽取

**Files:**
- Modify: `server/templates/js/api.js`（在 `saveDoc` 方法后，line 31 附近）
- Modify: `server/templates/js/app.js`（`saveDoc` line 123-131 附近加 `manage`；`pollChanges` line 157-188 重构）

**Interfaces:**
- Consumes: Task 1-3 的 `POST /api/docs/manage`（成功 `{"ok": true, "path", "deleted_count"?}`，失败 HTTP 4xx）
- Produces: `api.manage(action, payload) -> Promise<object>`（失败 throw `Error("manage <action> → <status>")`）；app 层 `manage(action, payload)`（成功后 `loadDocs()` + 当前文档受影响处理，失败 `alert`）；`_applyChangeToCurrentDoc(changes)`（changes 元素形如 `{path, from_path?, action}`，与 `/api/docs/changes` 返回同形）。Task 5/6 的 FileTree 通过 `inject('manage')` 调用。

- [ ] **Step 1: api.js 加 manage 封装**

在 `api` 对象的 `saveDoc` 方法（line 31 `},` 之后）插入：

```js
  /** POST /api/docs/manage — 文件管理（create_file/create_folder/rename/move/delete） */
  async manage(action, payload) {
    const res = await fetch('/api/docs/manage', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action, ...payload })
    })
    if (!res.ok) throw new Error(`manage ${action} → ${res.status}`)
    return res.json()
  },
```

- [ ] **Step 2: app.js 抽取 `_applyChangeToCurrentDoc` 并接入 `pollChanges`**

在 `saveDoc`（line 131）之后新增：

```js
// 当前文档受变更影响时的状态同步（pollChanges 与 manage 共用）
async function _applyChangeToCurrentDoc(changes) {
  const curAffected = changes.some(c =>
    c.path === state.currentPath || (c.action === 'moved' && c.from_path === state.currentPath)
  )
  if (!curAffected || !state.currentPath) return
  const deleted = changes.some(c => c.path === state.currentPath && c.action === 'deleted')
  const moved = changes.find(c => c.action === 'moved' && c.from_path === state.currentPath)
  if (deleted) {
    state.docContent = ''
    state.currentPath = null
  } else {
    if (moved) state.currentPath = moved.path
    try {
      state.docContent = await api.getDoc(state.currentPath)
    } catch (e) { }
  }
}
```

注意：相比原 `pollChanges` 内联版（line 175-186），删除分支**新增 `state.currentPath = null`**（spec 要求；原代码只清内容，会残留指向已删文件的路径）。

把 `pollChanges` 中 line 171-186 的「当前文档受影响？」整块替换为：

```js
    // 当前文档受影响？
    await _applyChangeToCurrentDoc(changes)
```

- [ ] **Step 3: app.js 加 `manage()` 方法并 provide**

在 `_applyChangeToCurrentDoc` 之后新增：

```js
// 文件管理操作（FileTree 通过 inject('manage') 调用）
async function manage(action, payload) {
  try {
    await api.manage(action, payload)
    await loadDocs()
    // 构造合成 change 走共用逻辑（后端已同步 apply_change，loadDocs 读到的是新 DB）
    const changes = []
    if (action === 'delete' && payload.path) {
      changes.push({ path: payload.path, action: 'deleted' })
    } else if ((action === 'rename' || action === 'move') && payload.from_path) {
      changes.push({ path: payload.to_path, from_path: payload.from_path, action: 'moved' })
    }
    if (changes.length) await _applyChangeToCurrentDoc(changes)
  } catch (e) {
    alert('操作失败: ' + e.message)
  }
}
```

在 `setup()` 内（`provide('api', api)` line 238 之后）加 `provide('manage', manage)`。

- [ ] **Step 4: 手动验证**

启动 `python -m server.main config.yaml`，浏览器打开 UI：
- 现有功能不回归：打开文档、编辑保存、轮询（pollChanges 重构后，用另一设备/终端 `curl -X POST` 触发外部变更，确认当前文档跟随逻辑仍工作）
- 控制台无报错；`state.currentPath` 在文档被外部删除后变为 `null`（原行为是残留旧路径）

- [ ] **Step 5: Commit**

```bash
git add server/templates/js/api.js server/templates/js/app.js
git commit -m "feat(ui): api.manage + app manage() with shared current-doc change handling"
```

---

### Task 5: FileTree — 操作菜单 + 行内重命名/新建

**Files:**
- Modify: `server/templates/js/components/FileTree.js`（`TreeNode` 组件 line 7-61 + `FileTree` 组件 setup/template）
- Modify: `server/templates/style.css`（菜单浮层定位，如需）

**Interfaces:**
- Consumes: Task 4 的 `manage`（`inject('manage')`）；`state.files`（`inject('state')`，元素 `{path, hash, mtime}`）；TopBar more-menu 浮层样式模式（`TopBar.js:89-107`：`absolute ... bg-surface border border-border rounded-lg shadow-lg z-50`，hover 项 `hover:bg-[color-mix(in_srgb,var(--brass)_10%,transparent)]`）
- Produces: 行级操作菜单（新建文件/新建文件夹/重命名/删除）+ 行内 input 编辑（Enter 确认、Esc 取消）。Task 6 在同一菜单里加「移动」项并复用行级 drag 属性。

- [ ] **Step 1: FileTree 加菜单状态与操作函数**

在 `FileTree` 的 `setup()` 中（`selectFile` line 113-115 附近）加：

```js
    const manage = inject('manage')
    const state = inject('state')

    // 操作菜单：{ path, isDir, x, y } | null（x/y 为右键坐标，hover 按钮触发时为 null → 贴行右侧）
    const menu = ref(null)
    // 行内编辑：{ path, isDir, name, mode: 'rename' | 'new-file' | 'new-folder' } | null
    const editing = ref(null)

    function openMenu(e, path, isDir) {
      menu.value = { path, isDir, x: e?.clientX ?? null, y: e?.clientY ?? null }
    }

    function closeMenu() { menu.value = null }

    function startRename(path, isDir) {
      const name = path.split('/').pop()
      editing.value = { path, isDir, name: isDir ? name : (name.endsWith('.md') ? name.slice(0, -3) : name), mode: 'rename' }
      menu.value = null
    }

    function startNew(dirPath, mode) {
      editing.value = { path: dirPath, isDir: mode === 'new-folder', name: mode === 'new-file' ? '未命名' : '新建文件夹', mode }
      menu.value = null
    }

    async function confirmEdit() {
      const ed = editing.value
      if (!ed) return
      editing.value = null
      const name = ed.name.trim()
      if (!name) return
      if (ed.mode === 'rename') {
        const to = ed.path.replace(/[^/]+$/, name + (ed.isDir ? '' : '.md'))
        await manage('rename', { from_path: ed.path, to_path: to })
      } else if (ed.mode === 'new-file') {
        await manage('create_file', { path: (ed.path ? ed.path + '/' : '') + name + '.md' })
      } else {
        await manage('create_folder', { path: (ed.path ? ed.path + '/' : '') + name })
      }
    }

    function onEditKeydown(e) {
      if (e.key === 'Enter') confirmEdit()
      else if (e.key === 'Escape') editing.value = null
    }

    // 删除确认（Task 6 的拖拽/移动不经过这里）
    function confirmDelete() {
      const m = menu.value
      if (!m) return
      const count = state.files.filter(f => f.path === m.path || f.path.startsWith(m.path + '/')).length
      const msg = m.isDir
        ? `将删除该文件夹及其中 ${count} 个文档，确定？`
        : `确定删除「${m.path.split('/').pop()}」？`
      if (confirm(msg)) {
        manage('delete', { path: m.path })
        menu.value = null
      }
    }

    // 菜单浮层定位：click/contextmenu 事件都带 clientX/clientY，统一 fixed 定位 + 视口钳制
    // （openMenu 内：menu.value = { path, isDir, x: Math.min(e.clientX, innerWidth - 170), y: Math.min(e.clientY, innerHeight - 180) }）
```

`treeOps` 对象（供 TreeNode 递归层 `inject('treeOps')`）：`{ menu, editing, openMenu, closeMenu, startRename, startNew, confirmEdit, onEditKeydown, confirmDelete }`，在 setup 中 `provide('treeOps', treeOps)`。FileTree 自身模板（菜单浮层）用到的 `menu, editing, closeMenu, startNew, startRename, confirmDelete` 同时加入 `return` 对象（line 236-240）。

- [ ] **Step 2: TreeNode 模板加 ⋯ 按钮、右键、行内 input**

`TreeNode` 的 `setup` 中加 `const treeOps = inject('treeOps')`（FileTree 已在 Step 1 provide）。

目录行（line 32-41）：外层 `div.tree-group` 加 `group` class；`button.tree-row` 加 `@contextmenu.prevent="treeOps.openMenu($event, dirPath + '/' + name, true)"`，并在 `span.tree-count` 前插入：

```html
            <button class="tree-more ml-auto opacity-0 group-hover:opacity-100 w-5 h-5 flex items-center justify-center rounded text-muted hover:text-text"
              @click.stop="treeOps.openMenu($event, dirPath + '/' + name, true)" aria-label="操作">
              <svg class="icon icon-xs" viewBox="0 0 24 24"><circle cx="12" cy="5" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="12" cy="19" r="1"/></svg>
            </button>
```

（`tree-count` 的 `ml-auto` 相应移到 `tree-more` 之后，保持徽标贴右。）

文件行（line 53-58）：`button.tree-file` 外层包一层 `div class="group"`（保持 `w-full`），加 `@contextmenu.prevent="treeOps.openMenu($event, f.path, false)"`，行内加同样的 `tree-more` 按钮（`@click.stop="treeOps.openMenu($event, f.path, false)"`）。

行内 input：目录行与文件行的 label 文本（`{{ name }}` / `{{ f.name }}`）改为条件渲染——当 `treeOps.editing.path === <该行路径>` 时渲染 input，否则渲染原 label：

```html
<input v-if="treeOps.editing && treeOps.editing.path === '<该行路径>'"
  class="tree-edit w-full px-1 py-0.5 text-sm border border-border rounded outline-none"
  v-model="treeOps.editing.name" @keydown="treeOps.onEditKeydown($event)"
  @blur="treeOps.confirmEdit()" @click.stop v-focus-select>
<span v-else class="font-medium">{{ name }}</span>
```

FileTree 组件注册小指令（focus + 全选，满足 spec「默认选中文件名部分」）：

```js
const vFocusSelect = { mounted(el) { el.focus(); el.select() } }
// FileTree 组件定义加 directives: { 'focus-select': vFocusSelect }
```

- [ ] **Step 3: FileTree 模板加菜单浮层**

在 `#file-tree` 根 div 内（`<nav>` 之后）加：

```html
      <!-- 操作菜单浮层：fixed 定位在触发点（openMenu 已做视口钳制） -->
      <div v-if="menu" class="fixed inset-0 z-40" @click="closeMenu" @contextmenu.prevent="closeMenu"></div>
      <div v-if="menu" class="fixed z-50 w-40 bg-surface border border-border rounded-lg shadow-lg py-1"
        :style="{ left: menu.x + 'px', top: menu.y + 'px' }">
        <button v-if="menu.isDir" class="w-full text-left px-3 py-2 text-xs hover:bg-[color-mix(in_srgb,var(--brass)_10%,transparent)]"
          @click="startNew(menu.path, 'new-file')">新建文件</button>
        <button v-if="menu.isDir" class="w-full text-left px-3 py-2 text-xs hover:bg-[color-mix(in_srgb,var(--brass)_10%,transparent)]"
          @click="startNew(menu.path, 'new-folder')">新建文件夹</button>
        <button class="w-full text-left px-3 py-2 text-xs hover:bg-[color-mix(in_srgb,var(--brass)_10%,transparent)]"
          @click="startRename(menu.path, menu.isDir)">重命名</button>
        <button class="w-full text-left px-3 py-2 text-xs text-red-700 hover:bg-[color-mix(in_srgb,var(--brass)_10%,transparent)]"
          @click="confirmDelete()">删除</button>
      </div>
```

（Task 5 不需要改 style.css；浮层样式完全复用 TopBar more-menu 的 class 模式。）

- [ ] **Step 4: 手动验证**

浏览器打开 UI：
- hover 文件行/文件夹行出现 `⋯`；点击与右键都能打开菜单，位置正确，点外部/再按右键关闭
- 文件夹菜单有「新建文件/新建文件夹」，文件菜单没有
- 「重命名」→ 行变 input、全选；Enter 确认后树即时刷新（新名出现）；Esc 取消恢复
- 「新建文件」默认名 `未命名`，「新建文件夹」默认名 `新建文件夹`；Enter 后树出现新条目
- 删除：文件弹「确定删除…」，文件夹弹「将删除该文件夹及其中 N 个文档」；确认后树刷新
- 移动端（<1024px）：菜单可点、浮层不溢出屏幕

- [ ] **Step 5: Commit**

```bash
git add server/templates/js/components/FileTree.js server/templates/style.css
git commit -m "feat(ui): FileTree action menu + inline rename/create"
```

---

### Task 6: FileTree — 拖拽移动 + 移动端降级

**Files:**
- Modify: `server/templates/js/components/FileTree.js`（TreeNode 行级 drag 属性 + FileTree 菜单加「移动」项）
- Modify: `server/templates/style.css`（drop 高亮 class，如需）

**Interfaces:**
- Consumes: Task 4 的 `manage`；Task 5 的 `menu`/`openMenu`/菜单浮层、`treeOps` provide
- Produces: 桌面拖拽移动（drop 到文件夹行）+ 移动端菜单「移动」项（prompt 输入目标目录）。功能完整，无后续 task。

- [ ] **Step 1: 行级拖拽（TreeNode）**

目录行 `button.tree-row` 与文件行外层 div 加：

```html
draggable="true"
@dragstart="onDragStart($event, '<path>', <isDir>)"
```

`treeOps` 中加：

```js
    // 拖拽移动
    const dragPath = ref(null)
    const dropTarget = ref(null)   // 高亮的文件夹路径
    let dragCounter = 0

    function onDragStart(e, path, isDir) {
      dragPath.value = path
      e.dataTransfer.effectAllowed = 'move'
      e.dataTransfer.setData('text/plain', path)
    }

    function canDropOn(dirPath) {
      const p = dragPath.value
      return p && p !== dirPath && !p.startsWith(dirPath + '/')  // 禁止 drop 到自身/自身子目录
    }

    function onDragEnter(e, dirPath) {
      if (!canDropOn(dirPath)) return
      e.preventDefault()
      dragCounter++
      dropTarget.value = dirPath
    }

    function onDragOver(e, dirPath) {
      if (canDropOn(dirPath)) e.preventDefault()  // 仅合法目标允许 drop
    }

    function onDragLeave(e) {
      if (!dropTarget.value) return
      e.preventDefault()
      if (--dragCounter <= 0) { dragCounter = 0; dropTarget.value = null }
    }

    async function onDrop(e, dirPath) {
      e.preventDefault()
      dragCounter = 0
      dropTarget.value = null
      const from = e.dataTransfer.getData('text/plain') || dragPath.value
      dragPath.value = null
      if (!from || !canDropOn(dirPath)) return
      const name = from.split('/').pop()
      await manage('move', { from_path: from, to_path: dirPath + '/' + name })
    }
```

目录行加 `@dragenter="treeOps.onDragEnter($event, dirPath + '/' + name)" @dragleave="treeOps.onDragLeave($event)" @dragover="treeOps.onDragOver($event, dirPath + '/' + name)" @drop="treeOps.onDrop($event, dirPath + '/' + name)"`，行 class 加 `:class="{ 'tree-drop-target': treeOps.dropTarget === dirPath + '/' + name }"`。

style.css 加（brass 未注册进 Tailwind，用 color-mix 任意值/原生写法）：

```css
/* 拖拽 drop target 高亮 */
.tree-drop-target { box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--brass) 40%, transparent); background: color-mix(in srgb, var(--brass) 10%, transparent); }
```

- [ ] **Step 2: 移动端「移动」菜单项**

Task 5 的菜单浮层中，「重命名」与「删除」之间加（仅移动端显示；桌面用拖拽）：

```html
        <button class="w-full text-left px-3 py-2 text-xs lg:hidden hover:bg-[color-mix(in_srgb,var(--brass)_10%,transparent)]"
          @click="moveItem()">移动</button>
```

`treeOps` 中加：

```js
    // 移动端降级：prompt 输入目标目录（复用 spec「弹窗选目标文件夹」的最低实现）
    function moveItem() {
      const m = menu.value
      if (!m) return
      const target = prompt('移动到哪个目录？（留空 = 根目录）', '')
      menu.value = null
      if (target === null) return
      const dir = target.trim().replace(/\/+$/, '')
      const name = m.path.split('/').pop()
      manage('move', { from_path: m.path, to_path: (dir ? dir + '/' : '') + name })
    }
```

`moveItem` 加入 `treeOps`（TreeNode 不用，但保持单一来源）并加入 FileTree 的 `return` 对象（菜单浮层模板调用）。

- [ ] **Step 3: 手动验证**

浏览器打开 UI：
- 拖文件到文件夹行：dragover 时目标行高亮（brass ring + 浅底），drop 后树刷新、文件出现在目标目录
- 拖文件夹到文件夹：同上；拖到自身或自身子目录时**不高亮、drop 无效**
- 拖出窗口/取消（Esc）：高亮消失，无请求发出
- 当前打开的文档被拖走：编辑器跟随新路径（Task 4 的 `_applyChangeToCurrentDoc`）；被删除：编辑器清空、`currentPath` 为 null
- 移动端（<1024px）：菜单出现「移动」项，prompt 输入目录后移动成功；留空移到根目录
- 失败路径：目标重名时后端 409 → `alert('操作失败: manage move → 409')`，树不变

- [ ] **Step 4: Commit**

```bash
git add server/templates/js/components/FileTree.js server/templates/style.css
git commit -m "feat(ui): FileTree drag-move with drop highlight + mobile move fallback"
```

---

## 收尾（全部 task 完成后）

- [ ] 跑全量测试：`pytest tests/ -v`，确认无回归
- [ ] 浏览器过一遍 spec「测试计划 → 前端（手动验证）」全部 7 条
- [ ] 多端同步冒烟：Web UI 重命名一个文档 → 确认 `/api/sync` 的 `recent_moves` 链路生效（客户端不会回推旧路径）；`/api/docs/changes` 轮询能看到 source="web" 的 moved 事件
