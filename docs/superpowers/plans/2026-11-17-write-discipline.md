# 写面纪律统一（审查 F6/F9/F10/F11/F12）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 收编所有用户正文写面到同一纪律：register-before-write（F6）、原子写 tmp+os.replace（F11）、编码保真 strict-decode（F10）、引用改写延后到 rename 全部成功后落盘 + 失败可弃（F9）、trash 入站回滚绝不销毁唯一副本（F12）。

**Architecture:** `refs.py` 拆出 `prepare_reference_updates`（rename 前算好每个 referrer 的新内容，含 pre-rename 唯一性判定）与 `apply_reference_updates`（rename 成功后落盘，redirect 表处理「referrer 自身也在 move 批内」的重定向）；`update_references_batch` 保留为 prepare+apply 即时组合的兼容包装（file_ops/回滚调用点零变更）。`edit_file` strict decode + 原子写。trash 回滚失败保留 batch 目录。

**Tech Stack:** Python 3.12 / pathlib / sqlite3。无新依赖。

**Spec:** 2026-11-16 审查报告 F6/F9/F10/F11/F12（证据行号内嵌各任务）；路线图 Plan 3 节。已知既定行为（不在本计划范围）：refs 改写是账本外写（watcher 记 watchdog 行）——归因收编另立卡。

## Global Constraints

- 测试夹具 `write_bytes`。
- `atomic_write_bytes`（vault_ops.py 既有）是服务端唯一原子写原语；客户端自实现 `tmp + os.replace`（仅 stdlib）。
- 兼容性红线：`file_ops.apply_change` 经 `_update_references → update_references_batch` 的调用点签名与行为不变（兼容包装）；Plan 4（若已合并）在 snapshots.py 新增的回滚调用点同样依赖该包装。
- refs 改写遇非 UTF-8 referrer：跳过该文件 + log warning，不写回、不中断批次。
- `edit_file` 是 agent edit_doc 与 MCP edit_doc 的共同底层——一处修两处受益，不重复实现。
- 每任务 RED→GREEN→commit；代理只跑自己任务面内的测试文件。

## Review Focus

1. **rename 中途失败的 refs 零残留**：第 N 个 rename 失败 → 已成功子项落盘、referrer 正文**逐字节**不变（web move 500 / move_docs 抛错两条路径都钉）——Task 4 钉。
2. **referrer 在 move 批内**（r1 链接 r2、两者同批移动）：prepare 读 r1 旧路径内容，apply 落到 r1 **新路径**——Task 4 redirect 钉。
3. **非 UTF-8 往返**：edit_file 对非法 UTF-8 返回 Error 且文件逐字节不变；refs 改写跳过该 referrer 且内容不变——Task 2/4 钉。
4. **原子写无残渣**：成功路径后目录内无 `.*.writing-tmp`；注入 os.replace 失败 → 目标文件原样、tmp 清理——Task 2/4/5 钉。
5. **trash 双重失败**：forward 第 2 个 rename 失败 + rename-back 也失败 → TrashError 且 batch 目录与文件**仍在盘上**，绝不 rmtree——Task 3 钉。
6. **register 先于写盘**：upload/save 对新路径，register 被调用时刻目标文件尚不存在——Task 1 spy 钉。

---

### Task 1: web upload/save register-before-write（F6）

**Files:**
- Modify: `server/routes/console.py`（upload_doc :268-283 与 save_doc :285-298）
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: `ledger.register(rel, "web", vault_dir)`（现签名）。
- Produces: 两端点顺序为 `read content → mkdir(parents) → register → write`。digest/返回值不变。

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_api.py`）

```python
def test_upload_and_save_register_before_write(client, vault_dir, monkeypatch) -> None:
    # F6：契约 = register 先于写盘（register 后置时，先跑的 watcher 消费者
    # 弹空 → watchdog 误归因 + 注册表残留误标下一次外部写）。
    from server.routes import console

    calls = []
    real_register = console.ledger.register

    def spy_register(rel, source, vault, *a, **k):
        assert not (vault / rel).exists(), f"register must run BEFORE the write: {rel}"
        calls.append(rel)
        return real_register(rel, source, vault, *a, **k)

    monkeypatch.setattr(console.ledger, "register", spy_register)

    r = client.post("/api/docs/order/new.md",
                    files={"file": ("new.md", b"content", "text/markdown")})
    assert r.status_code == 200
    assert (vault_dir / "order" / "new.md").is_file()

    r = client.put("/api/docs/order/saved.md", content=b"saved body")
    assert r.status_code == 200
    assert (vault_dir / "order" / "saved.md").is_file()

    assert calls == ["order/new.md", "order/saved.md"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_api.py::test_upload_and_save_register_before_write -v`
Expected: FAIL——upload 的 spy 断言触发（register 时文件已存在）

- [ ] **Step 3: 实现**：upload_doc 与 save_doc 内把 `ledger.register(...)` 移到 `file_path.write_bytes(content)` 之前（mkdir 之后）。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_api.py -q`
Expected: 全部 PASS（含既有 upload/save/registry 测试）

- [ ] **Step 5: Commit** `fix(console): register before write in upload_doc/save_doc (review F6)`

### Task 2: edit_file 编码保真 + 原子写（F10a/F11a）

**Files:**
- Modify: `server/vault/fs/vault_ops.py`（edit_file :83-111）
- Test: `tests/test_edit_file.py`（新建，纯函数单元测试，无 app 夹具）

**Interfaces:**
- Produces: `edit_file(vault_dir, path, old, new) -> str`——非 UTF-8 文件 → `Error: {path} is not valid UTF-8 — cannot edit safely (re-encode the file first)`，文件逐字节不变；成功路径经 `atomic_write_bytes`。edit_doc 工具与 MCP `_mcp_edit` 自动受益（零改动）。

- [ ] **Step 1: 写失败测试**（新建 `tests/test_edit_file.py`）

```python
"""edit_file: strict-decode fidelity + atomic write (review F10/F11)."""
from pathlib import Path

from server.vault.fs.vault_ops import edit_file


def test_edit_file_rejects_non_utf8_without_touching_bytes(tmp_path):
    fp = tmp_path / "gbk.md"
    raw = b"name \xd6\xd0\xce\xc4\nold_marker\n"  # GBK 字节，非合法 UTF-8
    fp.write_bytes(raw)
    out = edit_file(tmp_path, "gbk.md", "old_marker", "new_marker")
    assert out.startswith("Error")
    assert "UTF-8" in out
    assert fp.read_bytes() == raw  # 逐字节不变


def test_edit_file_atomic_no_tmp_stray(tmp_path):
    (tmp_path / "a.md").write_bytes(b"body old_marker tail")
    out = edit_file(tmp_path, "a.md", "old_marker", "new_marker")
    assert out.startswith("Edited")
    assert (tmp_path / "a.md").read_bytes() == b"body new_marker tail"
    strays = [p.name for p in tmp_path.iterdir()
              if p.name.startswith(".") and p.name.endswith(".writing-tmp")]
    assert strays == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_edit_file.py -v`
Expected: 第 1 例 FAIL（现状 errors="replace" 把 GBK 解成 U+FFFD 并成功替换，文件被重编码损坏）；第 2 例的 stray 断言 FAIL（write_bytes 非原子）

- [ ] **Step 3: 实现**：edit_file 的读段改 strict——

```python
    try:
        content = fp.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return (
            f"Error: {path} is not valid UTF-8 — cannot edit safely "
            "(re-encode the file first)"
        )
```

写段 `fp.write_bytes(new_content.encode("utf-8"))` → `atomic_write_bytes(fp, new_content.encode("utf-8"))`。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_edit_file.py tests/test_agent.py -q`
Expected: 全部 PASS（edit_doc 工具既有用例零回归）

- [ ] **Step 5: Commit** `fix(vault-ops): edit_file strict UTF-8 decode + atomic write (review F10/F11)`

### Task 3: trash 入站回滚非破坏（F12）

**Files:**
- Modify: `server/vault/orchestration/trash.py`（move_to_trash 的 except 块 :207-215）
- Test: `tests/test_trash.py`

**Interfaces:**
- Produces: 任一 rename-back 失败 → **保留 batch 目录**，抛 `TrashError("move to trash failed: …; rollback incomplete — batch kept at <bdir>")`；全部 rename-back 成功才 rmtree（现状）。

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_trash.py`；monkeypatch Path.rename 的 fake 需先存 `real_rename = Path.rename`）

```python
def test_move_to_trash_double_failure_keeps_batch(vault_dir, db_conn, monkeypatch):
    # F12：forward 第 2 个 rename 失败 + 第 1 个文件的 rename-back 也失败
    # → 旧行为 rmtree 销毁 batch 里仅存的唯一副本；新行为保留 batch 并抛错。
    import pathlib
    from server.vault.orchestration import trash

    (vault_dir / "notes").mkdir(exist_ok=True)
    f1 = vault_dir / "notes" / "one.md"
    f2 = vault_dir / "notes" / "two.md"
    f1.write_bytes(b"one")
    f2.write_bytes(b"two")
    real_rename = pathlib.Path.rename

    state = {"forward": 0}

    def fake_rename(self, target):
        if str(target).startswith(str(trash.trash_root(vault_dir))):
            state["forward"] += 1
            if state["forward"] >= 2:  # forward 第 2 个失败
                raise OSError("disk full (forward #2)")
            return real_rename(self, target)
        # rename-back 阶段：one.md 的回滚也失败
        if self.name == "one.md":
            raise OSError("disk full (rename-back)")
        return real_rename(self, target)

    monkeypatch.setattr(pathlib.Path, "rename", fake_rename)
    try:
        exc = None
        try:
            trash.move_to_trash(db_conn, vault_dir,
                                ["notes/one.md", "notes/two.md"], "web")
        except trash.TrashError as e:
            exc = e
        assert exc is not None and "rollback incomplete" in str(exc)
        batches = list(trash.trash_root(vault_dir).iterdir())
        assert len(batches) == 1
        assert (batches[0] / "notes" / "one.md").is_file()  # 唯一副本幸存
    finally:
        monkeypatch.undo()
```

（断言「two.md 仍在原位」可追加：`(vault_dir / "notes" / "two.md").is_file()`。）

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_trash.py::test_move_to_trash_double_failure_keeps_batch -v`
Expected: FAIL——旧代码 rmtree 后 batches == 0（副本被销毁）

- [ ] **Step 3: 实现**（except 块重写）：

```python
    except (OSError, TrashError) as e:
        rollback_failed = False
        for src, dest in moved:
            try:
                dest.rename(src)
            except OSError:
                rollback_failed = True  # best-effort；绝不为它销毁 batch
        if rollback_failed:
            # The batch holds the only surviving copies — keep it on disk
            # for manual recovery; destroying it would lose user content.
            raise TrashError(
                f"move to trash failed: {e}; rollback incomplete — "
                f"batch kept at {bdir}"
            ) from e
        shutil.rmtree(bdir, ignore_errors=True)
        _drop_empty_root(vault_dir)
        raise TrashError(f"move to trash failed: {e}") from e
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_trash.py -q`
Expected: 全部 PASS

- [ ] **Step 5: Commit** `fix(trash): partial-failure rollback keeps the batch instead of destroying the only copies (review F12)`

### Task 4: refs prepare/apply 拆分 + 解码跳过 + 原子写（F9/F10b/F11b）

**Files:**
- Modify: `server/vault/orchestration/refs.py`（update_references_batch :42-121、remove_dead_links :181-198）
- Modify: `server/routes/console.py`（move :77-113）、`server/agent/tools/move_docs.py`（:77-128）
- Test: `tests/test_refs_discipline.py`（新建，直调 refs + 模拟 rename 失败）+ `tests/test_trash.py`/`tests/test_move_detect.py`（回归面）

**Interfaces:**
- Produces:
  - `prepare_reference_updates(conn, vault_dir, moves) -> list[tuple[str, bytes]]`——referrer 当前路径 → 改写后全文字节（pre-rename 唯一性判定，现 update_references_batch 的 pre_rename=True 逻辑）；
  - `apply_reference_updates(vault_dir, prepared, redirect=None) -> int`——`redirect: {referrer旧路径: 新路径}`，落盘用 `atomic_write_bytes`，返回写入文件数；
  - `update_references_batch(conn, vault_dir, moves, pre_rename=False)` 保留为兼容包装（prepare + apply 无 redirect；pre_rename=False 的现行为 = apply 前已 rename，唯一性判定照旧——file_ops/_update_references/回滚调用点零变更）；
  - remove_dead_links 的写回改 `atomic_write_bytes`，decode 失败跳过该文件。

- [ ] **Step 1: 写失败测试**（新建 `tests/test_refs_discipline.py`）

```python
"""refs: prepare/apply split — rename-failure leaves referrers untouched
(F9); in-batch referrer redirect; decode skip; atomic writes (F10/F11)."""
import pathlib

from server.vault.fs.vault_ops import safe_rel_path
from server.vault.orchestration.refs import (
    apply_reference_updates,
    prepare_reference_updates,
)


def _seed(vault_dir):
    (vault_dir / "notes").mkdir(parents=True, exist_ok=True)
    (vault_dir / "notes" / "r1.md").write_bytes(b"links [[notes/moved.md]]")
    (vault_dir / "notes" / "moved.md").write_bytes(b"payload")


def test_failed_rename_leaves_referrers_untouched(vault_dir, db_conn, monkeypatch):
    _seed(vault_dir)
    real_rename = pathlib.Path.rename

    def fake_rename(self, target):
        if self.name == "moved.md":  # web move 的 src.rename(dst) 失败
            raise OSError("locked by obsidian")
        return real_rename(self, target)

    monkeypatch.setattr(pathlib.Path, "rename", fake_rename)
    moves = [("notes/moved.md", "notes/renamed.md")]
    prepared = prepare_reference_updates(db_conn, vault_dir, moves)
    try:
        (vault_dir / "notes" / "moved.md").rename(vault_dir / "notes" / "renamed.md")
    except OSError:
        pass  # 与 console.move/move_docs 的失败路径同构：不 apply
    monkeypatch.undo()
    assert b"[[notes/moved.md]]" in (vault_dir / "notes" / "r1.md").read_bytes()


def test_in_batch_referrer_redirect(vault_dir, db_conn):
    # r1 链接 r2，两者同批移动：prepare 读 r1 旧路径，apply 落 r1 新路径。
    (vault_dir / "notes").mkdir(parents=True, exist_ok=True)
    (vault_dir / "notes" / "r1.md").write_bytes(b"see [[notes/r2.md]]")
    (vault_dir / "notes" / "r2.md").write_bytes(b"payload")
    moves = [("notes/r1.md", "notes/dst/r1.md"), ("notes/r2.md", "notes/dst/r2.md")]
    prepared = prepare_reference_updates(db_conn, vault_dir, moves)
    assert prepared, "r1 must be a referrer of r2"
    for old, new in moves:  # rename 全部成功
        (vault_dir / old).parent.joinpath(
            pathlib.Path(new).name).parent.mkdir(parents=True, exist_ok=True)
        (vault_dir / old).rename(vault_dir / new)
    n = apply_reference_updates(
        vault_dir, prepared, redirect={o: n_ for o, n_ in moves})
    assert n == 1
    assert b"[[notes/dst/r2.md]]" in (vault_dir / "notes" / "dst" / "r1.md").read_bytes()


def test_prepare_skips_non_utf8_referrer(vault_dir, db_conn):
    _seed(vault_dir)
    (vault_dir / "notes" / "gbk.md").write_bytes(b"also [[notes/moved.md]] \xd6\xd0")
    prepared = prepare_reference_updates(db_conn, vault_dir,
                                         [("notes/moved.md", "notes/new.md")])
    rels = [rel for rel, _ in prepared]
    assert "notes/r1.md" in rels
    assert "notes/gbk.md" not in rels  # 跳过，不写回
    assert (vault_dir / "notes" / "gbk.md").read_bytes() == \
        b"also [[notes/moved.md]] \xd6\xd0"
```

（refs 的 referrer 查找走 edges 表——vault_dir/db_conn 夹具下 graph 需已建：若夹具不建 graph，测试改走 `update_references_batch` 的 fallback 全扫描路径或在 seed 后对两个文件 `graph_extract_file`；以实现时实际为准，断言语义不变。）

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_refs_discipline.py -v`
Expected: FAIL——`ImportError: cannot import name 'prepare_reference_updates'`

- [ ] **Step 3: 实现**：把 update_references_batch 的主体（patterns 构建 + referrer 收集 + 逐文件读改写）拆为 prepare（返回 [(rel, new_bytes)]，decode 失败跳过）+ apply（redirect 解析 + atomic_write_bytes）；兼容包装组合两者；remove_dead_links 同口径（strict decode 跳过 + atomic 写回）。

- [ ] **Step 4: 调用点改造**：
  - `console.py move`：`prepared = prepare_reference_updates(db_conn, vault_dir, moves)`（替代 update_references_batch(pre_rename=True)）→ rename → 失败即 500（自然零残留）→ 成功后 `apply_reference_updates(vault_dir, prepared, redirect={src: dst for moves})`（放在 per-file record 循环之后）。
  - `move_docs.py`：同样 prepare → rename 循环（失败路径现有 discard + raise，refs 零残留）→ 全部成功后 apply（redirect= moves dict）。

- [ ] **Step 5: 跑测试确认通过 + 回归面**

Run: `.venv/bin/python -m pytest tests/test_refs_discipline.py tests/test_move_detect.py tests/test_trash.py tests/test_agent.py tests/test_db.py -q`
Expected: 全部 PASS（apply_change 的 move-refs 用例经兼容包装零回归）

- [ ] **Step 6: Commit** `refactor(refs): prepare/apply split — rename-failure leaves referrers untouched, decode skip, atomic writes (review F9/F10/F11)`

### Task 5: 客户端原子写（F11c）

**Files:**
- Modify: `client/sync.py`（to_pull 写盘 :167-169、conflict 写盘 :193-194、护栏 conflict 写盘 :162）
- Test: `tests/test_client_sync.py`

**Interfaces:**
- Produces: 模块级 `_atomic_write(fp: Path, data: bytes) -> None`（同目录点前缀 tmp + `os.replace`，失败清理 tmp 并 raise）；三处 `write_bytes` 换用。

- [ ] **Step 1: 写失败测试**（追加）

```python
def test_atomic_write_no_stray_on_replace_failure(tmp_path, monkeypatch):
    import os

    from client.sync import _atomic_write

    fp = tmp_path / "a.md"
    fp.write_bytes(b"original")
    real_replace = os.replace

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr("client.sync.os.replace", boom)
    try:
        _atomic_write(fp, b"new")
    except OSError:
        pass
    monkeypatch.undo()
    assert fp.read_bytes() == b"original"          # 原文件原样
    assert list(tmp_path.iterdir()) == [fp]        # tmp 已清理
```

- [ ] **Step 2: 跑测试确认失败**——`ImportError/AttributeError: _atomic_write`
- [ ] **Step 3: 实现**（tmp 命名 `.{name}.writing-tmp`，与服务端惯例一致）
- [ ] **Step 4: 跑测试确认通过**：`.venv/bin/python -m pytest tests/test_client_sync.py -q`
- [ ] **Step 5: Commit** `fix(client): atomic writes for pulls and conflict copies (review F11)`

## Self-Review 记录

- **覆盖**：F6（Task 1）、F10 两半（Task 2 edit_file / Task 4 refs）、F11 三半（Task 2/4/5）、F12（Task 3）、F9（Task 4）全落点。
- **并行分组**（按文件不相交）：Wave 1 = Task 1+2（console upload/save + vault_ops）∥ Task 3（trash）∥ Task 5（client）；Wave 2 = Task 4（refs + console move + move_docs）——与 Wave 1 的 console.py 改动不同函数但同文件，必须串行。
- **一致性**：`update_references_batch` 兼容包装保 file_ops/回滚调用点；redirect 契约 {(旧referrer路径): 新路径}；tmp 命名与服务端一致。
- **Review Focus**：6 条全部有钉子（Task 4 两例失败注入 + redirect + decode；Task 2/5 往返与 stray；Task 3 双重失败；Task 1 spy）。
