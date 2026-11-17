# 回滚语义修复（审查 F7/F8）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 人工回滚（block ⑦）的账本记账与副作用对齐真实写语义——按 snapshot op 分派记账 op（create→delete、move→restore+delete 双行），move 回滚逆转 move 时的引用改写与 relatedness 路径改写。

**Architecture:** 路由层（`/api/memory/rollback`）从 `snapshots.by_call` 取每路径 op 分派 `ledger.record`；快照层（`snapshots.rollback`）move 分支在恢复后调 `update_references_batch(new→old)` + `relatedness.rewrite_paths(new→old)`（模块句柄，None-safe，与既有 G1 块同口径）。

**Tech Stack:** Python 3.12 / FastAPI / SQLite。无新依赖。

**Spec:** 2026-11-16 审查报告 F7/F8（证据：`routes/memory.py:265-267` 统一记 restore；`snapshots.py:530-534` move 回滚无 refs/relatedness 逆转）；路线图 `2026-11-17-review-followups-roadmap.md` Plan 4 节。

## Global Constraints

- 测试夹具 `write_bytes`；change_log 唯一 INSERT 面是 `log_change`（经 `ledger.record` 间接）。
- op 词汇：`"restore"` 是第 5 规范 op（CHECK 内），`"deleted"` 经 `_ACTION_TO_OP` 归一为 `"delete"`——调用方不手写归一。
- `apply_change` 的 delete 分支不查盘上文件（可对已 unlink 路径记账）；restore/created 分支查——这是 op 分派的依据。
- 既有测试 `test_rollback_route_books_restore_op_not_created`（delete-op 情形）必须保持绿。
- 每任务 RED→GREEN→commit；代理只跑 `tests/test_deletion_rollback.py` 与 `tests/test_snapshots.py`。

## Review Focus

1. **create-op 回滚的恰好一行**：delete 行同步落库 + skip 标记压制 watcher → simulate_consumer 驱动后该路径 change_log 恰 1 行——Task 1 钉。
2. **move-op 双行**：旧路径 restore + 新路径 delete，source 均 'web'——Task 1 钉。
3. **move 回滚的 refs 逆转**：referrer 正文 `[[new]]` 回到 `[[old]]`（含短名情形）——Task 2 钉。
4. **move 回滚的 relatedness 逆转**：D19「关系属文档」承诺在回滚方向闭合——Task 2 钉。
5. **零回归**：既有 delete-op 回滚记账、G1 反链复活测试不破——Task 2 收尾全量该两文件钉。

---

### Task 1: 路由按 op 分派记账（F7）

**Files:**
- Modify: `server/routes/memory.py`（rollback 路由的成功分支，现 :265-267 `for path in result["restored"]: ledger.record(..., "restore", "web")`）
- Test: `tests/test_deletion_rollback.py`（跟随 ：554 `test_rollback_route_books_restore_op_not_created` 的夹具惯例：client + capture/finalize 直建快照 + `simulate_consumer`）

**Interfaces:**
- Consumes: `snap_mod.by_call(ledger_conn, call_id)`（已有查询，返回含 op/new_path 的行）。
- Produces: 路由记账语义——`create`→`record(path,"deleted","web")`；`move`→`record(path,"restore","web")` + `record(new_path,"deleted","web")`；`delete`/`update`→`record(path,"restore","web")`（不变）。仅 `result["ok"]` 时记账（部分失败不记，现状保持）。

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_deletion_rollback.py`，夹具/断言风格跟随该文件既有 rollback-route 测试）

```python
def test_rollback_route_books_delete_for_create_op(client, vault_dir, db_conn, ledger_conn):
    # F7：create-op 回滚 = unlink。路由必须记 op='delete'（旧行为记 'restore'
    # → apply_change 查盘无文件 → 零行，且 skip 标记压制 watcher → 幽灵行）。
    from server import snapshots as snap_mod
    from tests.conftest import simulate_consumer

    (vault_dir / "notes").mkdir(exist_ok=True)
    fp = vault_dir / "notes" / "created.md"
    fp.write_bytes(b"new content")
    snap = snap_mod.capture(ledger_conn, vault_dir, "notes/created.md", "create", "agent")
    snap_mod.finalize(ledger_conn, snap, snap_mod.hash_bytes(b"new content"))

    r = client.post("/api/memory/rollback", json={"call_id": snap["call_id"]})
    assert r.status_code == 200
    assert not fp.exists()
    rows = {(row["path"], row["op"], row["source"]) for row in db_conn.execute(
        "SELECT path, op, source FROM change_log")}
    assert ("notes/created.md", "delete", "web") in rows
    assert ("notes/created.md", "restore", "web") not in rows
    # 恰好一行：skip 标记压制后续 watcher 事件
    simulate_consumer(db_conn, vault_dir, "notes/created.md", "deleted")
    assert db_conn.execute(
        "SELECT count(*) FROM change_log WHERE path='notes/created.md'"
    ).fetchone()[0] == 1


def test_rollback_route_books_move_pair(client, vault_dir, db_conn, ledger_conn):
    # F7：move-op 回滚 = 旧路径 restore + 新路径 delete。
    from server import snapshots as snap_mod

    (vault_dir / "notes").mkdir(exist_ok=True)
    (vault_dir / "notes" / "a.md").write_bytes(b"payload")
    snap = snap_mod.capture(ledger_conn, vault_dir, "notes/a.md", "move", "agent",
                            new_path="notes/b.md")
    (vault_dir / "notes" / "a.md").rename(vault_dir / "notes" / "b.md")
    snap_mod.finalize(ledger_conn, snap, snap_mod.hash_bytes(b"payload"))

    r = client.post("/api/memory/rollback", json={"call_id": snap["call_id"]})
    assert r.status_code == 200
    assert (vault_dir / "notes" / "a.md").is_file()
    assert not (vault_dir / "notes" / "b.md").exists()
    rows = {(row["path"], row["op"]) for row in db_conn.execute(
        "SELECT path, op FROM change_log WHERE source='web'")}
    assert ("notes/a.md", "restore") in rows
    assert ("notes/b.md", "delete") in rows
```

（`simulate_consumer` 的确切签名以 conftest / 既有测试调用为准——如参数序不同按既有惯例适配，断言语义不变。）

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_deletion_rollback.py -k "books_delete_for_create or books_move_pair" -v`
Expected: FAIL——create 用例断言 `("notes/created.md","delete","web") in rows` 失败（现状零行或 restore 行）；move 用例缺 `("notes/b.md","delete")` 行

- [ ] **Step 3: 实现**（路由成功分支替换）：

```python
        if result["ok"]:
            # F7 (review 2026-11-16): book per REAL op — create-rollback is an
            # unlink ('delete' rows need no disk file), move-rollback is a
            # pair (restore the old path, delete the new), delete/update
            # rollback is a content restore ('restore'). The old blanket
            # 'restore' booked zero rows for create/move-restore (apply_change
            # requires the file on disk) while the skip mark suppressed the
            # watcher — ghost rows until the periodic reconcile.
            for s in snap_mod.by_call(ctx.ledger_conn, call_id):
                if s["op"] == "create":
                    ledger.record(ctx.db_conn, ctx.vault_dir, s["path"], "deleted", "web")
                elif s["op"] == "move":
                    ledger.record(ctx.db_conn, ctx.vault_dir, s["path"], "restore", "web")
                    ledger.record(ctx.db_conn, ctx.vault_dir, s["new_path"], "deleted", "web")
                else:
                    ledger.record(ctx.db_conn, ctx.vault_dir, s["path"], "restore", "web")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_deletion_rollback.py tests/test_snapshots.py -q`
Expected: 全部 PASS（含既有 `test_rollback_route_books_restore_op_not_created`）

- [ ] **Step 5: Commit**

```bash
git add server/routes/memory.py tests/test_deletion_rollback.py
git commit -m "fix(rollback): per-op ledger dispatch — create→delete, move→restore+delete pair (review F7)"
```

### Task 2: move 回滚逆转 refs 与 relatedness（F8）

**Files:**
- Modify: `server/snapshots.py`（rollback 的 G1 块区域，现 :545-557 之后）
- Test: `tests/test_deletion_rollback.py`

**Interfaces:**
- Consumes: `update_references_batch(conn, vault_dir, moves=[(old,new)])`（现签名，refs.py）；`relatedness.rewrite_paths(conn, from_path, to_path)`；模块句柄 `get_index_conn()`/`get_ledger_conn()`（None-safe，同 G1 块口径）。
- Produces: move-op 回滚成功后，referrer 正文 `[[new]]→[[old]]`、relatedness 行路径 new→old。

- [ ] **Step 1: 写失败测试**（追加；模块句柄注册方式跟随该文件既有测试——若 conftest 已注册则省略显式 init）

```python
def test_rollback_move_reverses_refs_and_relatedness(vault_dir, db_conn, ledger_conn):
    # F8：move 的回滚必须逆转 move 时的两处副作用——referrer 正文改写
    # （[[a]]→[[b]]）与 relatedness 路径改写（D19）。
    from server import ledger, snapshots as snap_mod
    from server.vault.db.relatedness import merge_edge

    (vault_dir / "notes").mkdir(exist_ok=True)
    (vault_dir / "notes" / "a.md").write_bytes(b"a content")
    (vault_dir / "notes" / "referrer.md").write_bytes(b"see [[notes/a.md]] here")
    ledger.record(db_conn, vault_dir, "notes/a.md", "created", "agent")
    ledger.record(db_conn, vault_dir, "notes/referrer.md", "created", "agent")
    merge_edge(ledger_conn, "notes/a.md", "notes/referrer.md",
               basis="b", actor="t", run_id="r")

    # move 调用的真实时序：capture（旧路径仍在盘）→ rename → record（同步
    # 记账，refs/relatedness 在此改写）→ finalize。
    snap = snap_mod.capture(ledger_conn, vault_dir, "notes/a.md", "move", "agent",
                            new_path="notes/b.md")
    (vault_dir / "notes" / "a.md").rename(vault_dir / "notes" / "b.md")
    ledger.record(db_conn, vault_dir, "notes/b.md", "moved", "agent",
                  from_path="notes/a.md")
    snap_mod.finalize(ledger_conn, snap,
                      snap_mod.hash_bytes((vault_dir / "notes" / "b.md").read_bytes()))
    assert b"[[notes/b.md]]" in (vault_dir / "notes" / "referrer.md").read_bytes()
    assert ledger_conn.execute(
        "SELECT source FROM relatedness").fetchone()["source"] == "notes/b.md"

    result = snap_mod.rollback(ledger_conn, vault_dir, snap["call_id"])
    assert result["ok"]
    assert b"[[notes/a.md]]" in (vault_dir / "notes" / "referrer.md").read_bytes()
    assert ledger_conn.execute(
        "SELECT source FROM relatedness").fetchone()["source"] == "notes/a.md"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_deletion_rollback.py::test_rollback_move_reverses_refs_and_relatedness -v`
Expected: FAIL——referrer 正文仍是 `[[notes/b.md]]`（refs 未逆转）

- [ ] **Step 3: 实现**（rollback 内 G1 块之后追加）：

```python
    # F8 (review 2026-11-16): a move's rollback reverses the move-time side
    # effects — referrer link text ([[new]] → [[old]]) and the relatedness
    # ledger's path rewrite (D19's relation-follows-document runs both ways).
    # Module handles, None-safe: same discipline as the G1 block above.
    move_snaps = [s for s in snaps if s["op"] == "move" and s["path"] in restored_set]
    if move_snaps:
        from .vault.db.conn import get_index_conn, get_ledger_conn
        from .vault.orchestration.refs import update_references_batch

        index_conn = get_index_conn()
        handle_ledger_conn = get_ledger_conn()
        for s in move_snaps:
            if index_conn is not None:
                update_references_batch(index_conn, vault, [(s["new_path"], s["path"])])
            if handle_ledger_conn is not None:
                from .vault.db.relatedness import rewrite_paths

                rewrite_paths(handle_ledger_conn, s["new_path"], s["path"])
```

- [ ] **Step 4: 跑测试确认通过 + 零回归**

Run: `.venv/bin/python -m pytest tests/test_deletion_rollback.py tests/test_snapshots.py tests/test_snapshots_degraded.py -q`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add server/snapshots.py tests/test_deletion_rollback.py
git commit -m "fix(rollback): move rollback reverses referrer links and relatedness paths (review F8)"
```

## Self-Review 记录

- **覆盖**：F7（Task 1 分派 + 恰好一行 + move 双行）、F8（Task 2 refs+relatedness 逆转）全落点。
- **一致性**：Task 2 复用 `update_references_batch` 现签名（Plan 3 的 prepare/apply 重构保持兼容包装，届时本调用点无需变更）；`restored_set` 为既有变量。
- **Review Focus**：5 条全部有钉子（Task 1 两条 + Task 2 一条 + 收尾全量两文件）。
- **顺序**：F7 先（路由层），F8 后（快照层）——F8 测试直调 `snapshots.rollback`，与 F7 的路由记账互不依赖，但同文件顺序执行避免纠缠。
