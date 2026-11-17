# 同步 lane 修复：修改传播基线 + 复活防护（审查 F1–F4）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复同步协议的两个结构性缺陷——①任一侧对已同步文件的修改永远不传播（只产冲突，审查 F1）；②已删文档可经三条路径复活（200 行窗口截断 F2、move→delete 复合序列 F3、recovery drill 清空 change_log F4）。

**Architecture:** 客户端在 `/api/sync` 请求中携带上次同步的文件清单（`baseline_files`，来自已保存但从未使用的 `state["files"]`），服务端据此做三方分类（服务端改→to_pull / 客户端改→to_push / 双方改→conflict；无基线维持现状）。删除防护改为**全量重放**：新纯函数 `derive_deleted_hashes` 按 seq ASC 重放 change_log 的全部 move/delete 行，把「被删内容」沿 move 链解析回每个客户端可能持有的原始路径 → `{原始路径: pre_hash}`，compare 的删除门直接消费这张无界映射（彻底脱离 200 行窗口）。冷重建后由 `seed_deleted_prehashes` 从 trash meta.json + ledger.db snapshots 重播种删除行。客户端拉取前加哈希护栏，防止同步周期内的本地编辑被覆盖。

**Tech Stack:** Python 3.12 / FastAPI / SQLite（WAL）/ httpx。无新依赖。

**Spec:** 协议原文 `~/md-vault/projects/md-repo/specs/2026-09-27-md-sync-design.md`（§同步流程：to_pull/to_push 应含「过期」文件）；2026-11-15 actor-separation spec §2（pre_hash 无界语义）。本计划实现 **2026-11-16 全量代码审查报告 F1–F4**（会话内交付；每条发现的证据行号已内嵌到对应任务）。

## Global Constraints

- 测试夹具写样例文件一律 `write_bytes`（Windows 换行翻译会破坏哈希断言——AGENTS.md 已知坑）。
- change_log 全仓唯一 INSERT 点是 `log_change`（`server/vault/db/changes.py:33`）；op 词汇表在 `log_change` 入口经 `_ACTION_TO_OP` 归一——**任何调用方不得手写 op 字符串绕过它**（播种任务必须走 `log_change`）。
- `compare_files` 返回四键 `to_pull / to_push / to_delete / conflicts`，全部 sorted。
- 协议向后兼容：`baseline_files` 为可选字段；不带该字段的旧客户端行为必须与现状逐字节一致。
- 删除 pre_hash 映射**无界**（spec 2026-11-15 §2：必须活过快照 GC 窗口）——重放查询不加 LIMIT / 不按 last_sync 截断。
- 不新增路由（`tests/test_routes_order.py` 钉住路由表）；不引入鉴权；前端不在本计划范围。
- 每个任务结束跑一次该任务测试 + `git commit`；任务间接口以各任务 Interfaces 块为准。

## Review Focus

1. **同步周期内客户端又编辑了待拉取文件**（scan 之后、pull 写盘之前）：期望保留本地编辑（服务端版本存冲突副本），而不是覆盖丢稿——Task 5 `test_pull_guard_conflict_saves_instead_of_overwrite`。
2. **旧客户端不带 `baseline_files`**：期望行为与现状一致（hash 不同 → conflict，绝不误判方向）——Task 3 `test_baseline_none_conflict` + Task 4 路由级不带字段测试。
3. **baseline 缺项/损坏**（首次同步 `{}`、非 dict、值非 str）：期望退化为 conflict——Task 3 `test_baseline_empty_first_sync_conflict` + Task 4 类型校验测试。
4. **同一路径删除两次**（删除→服务端重建→再删除）：期望最新 pre_hash 生效——Task 1 `test_derive_latest_delete_wins`。
5. **move→edit→delete 链下客户端持 move 前内容**（别名解析出的 pre_hash 是删除时内容）：期望保持 to_push（hash 证据不足不误删客户端重写），旧版本复活是**文档化接受的残余盲区**——Task 2 `test_alias_content_drift_stays_to_push`。

---

### Task 1: `derive_deleted_hashes` —— 全量 move/delete 别名重放（F2+F3 的核心纯函数）

**Files:**
- Modify: `server/sync.py`（文件末尾新增函数）
- Test: `tests/test_sync.py`

**Interfaces:**
- Consumes: 无（纯函数，std lib only）。
- Produces: `derive_deleted_hashes(rows: list[dict]) -> dict[str, str]`。`rows` 为 change_log 行、**seq ASC 序**，每行 `{"op": "move"|"delete", "path": str, "old_path": str, "pre_hash": str}`（move 行 `path`=新路径、`old_path`=来源路径；delete 行 `old_path` 恒为 `""`）。返回 `{客户端可能持有的原始路径: pre_hash}`，**值 `""` 表示无 pre-hash 记录的外部删除**（同样进 map、同样判 stale）。Task 4 的路由查询按此行形状供数。

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_sync.py`）

```python
# ── derive_deleted_hashes：全量重放（F2/F3）──

def test_derive_direct_delete():
    rows = [{"op": "delete", "path": "a.md", "old_path": "", "pre_hash": "h1"}]
    assert derive_deleted_hashes(rows) == {"a.md": "h1"}


def test_derive_external_delete_empty_prehash():
    rows = [{"op": "delete", "path": "a.md", "old_path": "", "pre_hash": ""}]
    assert derive_deleted_hashes(rows) == {"a.md": ""}


def test_derive_move_then_delete_compound():
    # F3 主场景：X moved→Y，Y deleted。客户端持有的 X 旧副本必须被映射覆盖。
    rows = [
        {"op": "move", "path": "Y.md", "old_path": "X.md", "pre_hash": ""},
        {"op": "delete", "path": "Y.md", "old_path": "", "pre_hash": "h1"},
    ]
    assert derive_deleted_hashes(rows) == {"Y.md": "h1", "X.md": "h1"}


def test_derive_chain_move_then_delete():
    # X→Y→Z 链式 move 后删除 Z：三个历史路径全部命中。
    rows = [
        {"op": "move", "path": "Y.md", "old_path": "X.md", "pre_hash": ""},
        {"op": "move", "path": "Z.md", "old_path": "Y.md", "pre_hash": ""},
        {"op": "delete", "path": "Z.md", "old_path": "", "pre_hash": "h1"},
    ]
    assert derive_deleted_hashes(rows) == {"Z.md": "h1", "Y.md": "h1", "X.md": "h1"}


def test_derive_latest_delete_wins():
    rows = [
        {"op": "delete", "path": "a.md", "old_path": "", "pre_hash": "h1"},
        {"op": "delete", "path": "a.md", "old_path": "", "pre_hash": "h2"},
    ]
    assert derive_deleted_hashes(rows) == {"a.md": "h2"}


def test_derive_delete_after_alias_occupied_pins_alias_hash():
    # move X→Y 之后 X 被重建又删除：X 的别名槽已指向 Y，重建内容的删除
    # 不覆盖别名（文档化歧义——见计划 Review Focus 第 5 条同源）。
    rows = [
        {"op": "move", "path": "Y.md", "old_path": "X.md", "pre_hash": ""},
        {"op": "delete", "path": "Y.md", "old_path": "", "pre_hash": "h1"},
        {"op": "delete", "path": "X.md", "old_path": "", "pre_hash": "h2"},
    ]
    assert derive_deleted_hashes(rows) == {"Y.md": "h1", "X.md": "h1"}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_sync.py -k derive -v`
Expected: FAIL，`ImportError: cannot import name 'derive_deleted_hashes'`

- [ ] **Step 3: 实现**（`server/sync.py` 末尾；算法非显然，给出全函数）

```python
def derive_deleted_hashes(rows: list[dict]) -> dict[str, str]:
    """Replay all-time move/delete change_log rows (seq ASC) into
    {client-held original path: pre-delete hash}.

    ``loc`` tracks where each historical path's content now lives; a delete
    row marks every path whose content ended up at the deleted path (the
    path itself + all move-aliases). Value "" = deleted with no recorded
    pre-hash (external delete) — still a stale-copy signal.
    """
    loc: dict[str, str] = {}
    out: dict[str, str] = {}
    for r in rows:
        op, p = r["op"], r["path"]
        if op == "move":
            f = r.get("old_path") or ""
            if not f:
                continue
            for k in [k for k, v in loc.items() if v == f]:
                loc[k] = p
            loc[f] = p
        elif op == "delete":
            h = r.get("pre_hash") or ""
            for k in [k for k, v in loc.items() if v == p]:
                out[k] = h
            if p not in loc:
                out[p] = h
            # p 已被 move 走（p in loc）时，p 处的删除行指向的是重建的新内容，
            # 不改写别名（Review Focus 5 的文档化歧义）。
    return out
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_sync.py -k derive -v`
Expected: 6 PASS

- [ ] **Step 5: Commit**

```bash
git add server/sync.py tests/test_sync.py
git commit -m "feat(sync): derive_deleted_hashes — all-time move/delete alias replay (review F2/F3)"
```

---

### Task 2: compare_files 删除门脱离 200 行窗口（F2）

**Files:**
- Modify: `server/sync.py`（`compare_files` 删除检测段，现为 :81-93）
- Test: `tests/test_sync.py`

**Interfaces:**
- Consumes: Task 1 的 `derive_deleted_hashes` 语义（本任务只在 compare 层消费 map，不调用它）。
- Produces: `compare_files` 的新语义——`deleted_hashes`（非 None 时）是删除门的**唯一**依据，与 `recent_deletes` 是否在窗口内无关；`recent_deletes` 参数保留但不再参与判定（docstring 标 deprecated）。签名不变：`compare_files(local, server, recent_moves=None, recent_deletes=None, deleted_hashes=None)`（Task 3 再加 `baseline`）。

- [ ] **Step 1: 写失败测试**（追加 + 一处修改）

追加：

```python
def test_delete_gate_ignores_window():
    # F2：删除行滑出 recent_deletes 窗口（传 None），只要 deleted_hashes
    # 有记录就必须拦下复活。
    local = {"X.md": "h1"}
    server = {}
    result = compare_files(local, server, recent_deletes=None,
                           deleted_hashes={"X.md": "h1"})
    assert result["to_push"] == []
    assert result["to_delete"] == ["X.md"]


def test_alias_content_drift_stays_to_push():
    # Review Focus 5：move→edit→delete 链下，客户端持 move 前内容
    # （≠ 删除时 pre_hash）→ 重写例外生效，保持 to_push（文档化残余盲区）。
    result = compare_files({"X.md": "h-pre-move"}, {},
                           deleted_hashes={"X.md": "h-at-delete"})
    assert result["to_push"] == ["X.md"]
    assert result["to_delete"] == []
```

修改既有测试 `test_external_delete_without_pre_hash_goes_to_delete`（tests/test_sync.py:163-174）：`deleted_hashes={}` 改为 `deleted_hashes={"a.md": ""}`（外部删除现在以空串哨兵进 map）。

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_sync.py -v`
Expected: `test_delete_gate_ignores_window` FAIL（to_push 仍含 X.md）；`test_external_delete_without_pre_hash_goes_to_delete` FAIL

- [ ] **Step 3: 实现**：把 `compare_files` 的删除检测段（现 :81-93）替换为

```python
    # Deletion detection (F2): the all-time deleted_hashes map ("" = external
    # delete with no pre-hash) is the SOLE gate — window membership no longer
    # matters. recent_deletes is accepted for signature compat and ignored.
    if deleted_hashes is not None:
        new_to_push = []
        for p in to_push:
            pre_hash = deleted_hashes.get(p)
            if pre_hash is None:
                new_to_push.append(p)          # 无删除记录：普通新文件
            elif pre_hash and local.get(p) != pre_hash:
                new_to_push.append(p)          # 删除后的真实重写/重建
            else:
                to_delete.append(p)            # 陈旧副本（含 "" 外部删除）
        to_push = new_to_push
```

同步更新 `compare_files` docstring 的 `deleted_hashes`/`recent_deletes` 两段说明（recent_deletes 标 deprecated：仅旧调用方兼容，不参与判定）。

- [ ] **Step 4: 跑整个 test_sync.py 确认通过**

Run: `.venv/bin/python -m pytest tests/test_sync.py -v`
Expected: 全部 PASS（`test_no_recent_deletes_backward_compatible` 不传 deleted_hashes → to_push，应原样通过；`test_stale_copy_after_trash_delete_goes_to_delete` / `test_local_rewrite_after_trash_delete_stays_to_push` / `test_move_rule_wins_over_delete_rule` / `test_delete_rule_only_touches_to_push_paths` 均传了 deleted_hashes，新门下语义不变）

- [ ] **Step 5: Commit**

```bash
git add server/sync.py tests/test_sync.py
git commit -m "feat(sync): deletion gate driven by all-time deleted_hashes map, decoupled from the 200-row window (review F2)"
```

---

### Task 3: compare_files 三方基线分类（F1 服务端半边）

**Files:**
- Modify: `server/sync.py`（`compare_files`：新增 `baseline` 参数，重构 in-both 分类段，现 :45-52）
- Test: `tests/test_sync.py`

**Interfaces:**
- Consumes: 无新依赖。
- Produces: `compare_files(local, server, recent_moves=None, recent_deletes=None, deleted_hashes=None, baseline=None) -> dict`。`baseline` = 客户端上次同步清单 `{path: hash}`；**None = 旧客户端/无基线 → in-both hash 差异一律 conflict（现状）**。返回四键不变、仍 sorted。Task 4/5 按此签名与字段名 `baseline_files` 接线。

- [ ] **Step 1: 写失败测试**（追加）

```python
# ── 三方基线分类（F1）──

BASE = {"a.md": "h1"}


def test_baseline_server_modified_goes_to_pull():
    # 上次同步双方 h1；服务端现为 h2、客户端未动 → 客户端应拉取。
    result = compare_files({"a.md": "h1"}, {"a.md": "h2"}, baseline={"a.md": "h2"})
    assert result["to_pull"] == ["a.md"]
    assert result["to_push"] == []
    assert result["conflicts"] == []


def test_baseline_client_modified_goes_to_push():
    result = compare_files({"a.md": "h2"}, {"a.md": "h1"}, baseline=BASE)
    assert result["to_push"] == ["a.md"]
    assert result["to_pull"] == []
    assert result["conflicts"] == []


def test_baseline_both_modified_conflict():
    result = compare_files({"a.md": "h2"}, {"a.md": "h3"}, baseline={"a.md": "h0"})
    assert result["conflicts"] == ["a.md"]
    assert result["to_pull"] == []
    assert result["to_push"] == []


def test_baseline_none_conflict():
    # 旧客户端兼容：不带基线 → 现状（conflict）。
    result = compare_files({"a.md": "h1"}, {"a.md": "h2"}, baseline=None)
    assert result["conflicts"] == ["a.md"]


def test_baseline_empty_first_sync_conflict():
    # 首次同步：基线为 {} → 与现状一致（conflict）。
    result = compare_files({"a.md": "h1"}, {"a.md": "h2"}, baseline={})
    assert result["conflicts"] == ["a.md"]


def test_baseline_both_directions_coexist():
    local = {"a.md": "hA2", "b.md": "hB1"}
    server = {"a.md": "hA1", "b.md": "hB2"}
    baseline = {"a.md": "hA1", "b.md": "hB1"}
    result = compare_files(local, server, baseline=baseline)
    assert result["to_pull"] == ["b.md"]   # 服务端改了 b
    assert result["to_push"] == ["a.md"]   # 客户端改了 a
    assert result["conflicts"] == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_sync.py -k baseline -v`
Expected: 6 FAIL（`compare_files() got an unexpected keyword argument 'baseline'`）

- [ ] **Step 3: 实现**：签名加 `baseline: dict[str, str] | None = None`；in-both 分类段（现 :50-52 的 conflicts 推导）替换为

```python
    to_pull = sorted(server_paths - local_paths)
    to_push = sorted(local_paths - server_paths)
    conflicts: list[str] = []
    for p in local_paths & server_paths:
        if local[p] == server[p]:
            continue
        if baseline is None:
            conflicts.append(p)                 # 旧客户端：维持现状
        elif baseline.get(p) == server[p]:
            to_pull.append(p)                   # 服务端改、客户端未动
        elif baseline.get(p) == local[p]:
            to_push.append(p)                   # 客户端改、服务端未动
        else:
            conflicts.append(p)                 # 双方都改（或基线缺项）
```

后续 move 规则与删除门（Task 2）保持在该分类**之后**执行，作用于已扩展的 to_push——顺序不变，只把 to_push/to_pull 的来源扩了。docstring 补 `baseline` 参数说明。

- [ ] **Step 4: 跑整个 test_sync.py 确认通过**

Run: `.venv/bin/python -m pytest tests/test_sync.py -v`
Expected: 全部 PASS（既有非 baseline 用例不传该参数，走 conflict 现状分支）

- [ ] **Step 5: Commit**

```bash
git add server/sync.py tests/test_sync.py
git commit -m "feat(sync): three-way baseline classification — server-modified pulls, client-modified pushes (review F1)"
```

---

### Task 4: /api/sync 路由接线（全量重放 + baseline）

**Files:**
- Modify: `server/routes/sync.py`（:52-81 的窗口查询与 compare 调用段）
- Test: `tests/test_api.py`（追加到既有 sync 测试附近）

**Interfaces:**
- Consumes: Task 1 `derive_deleted_hashes(rows)`（rows 形状见 Task 1）；Task 3 `compare_files(..., baseline=...)`。
- Produces: 路由行为——请求体新增**可选**字段 `baseline_files`（`{path: hash}`，非 dict 或含非 str 值 → 按 None 处理）；删除门改为常开全量重放（不再 gated by `last_sync`）；`recent_moves` 保持现状（last_sync 窗口 200 行，move 规则用）；不再计算/传递 `recent_deletes`。Task 5 的客户端按字段名 `baseline_files` 上送。

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_api.py`；conftest 的 `client` fixture 预置 `notes/test.md` = SAMPLE_CONTENT，SAMPLE_HASH = 其 sha256）

```python
def test_sync_baseline_server_modified_to_pull(client) -> None:
    # 服务端版本 = SAMPLE（客户端上次同步到的哈希），客户端报了另一个哈希
    # → 判定服务端改 → to_pull（现状是 conflict）。
    r = client.post("/api/sync", json={
        "device_name": "mac",
        "local_files": {"notes/test.md": "client-edited-hash"},
        "baseline_files": {"notes/test.md": SAMPLE_HASH},
        "logs": [],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["to_pull"] == ["notes/test.md"]
    assert body["conflicts"] == []


def test_sync_no_baseline_conflict_compat(client) -> None:
    # 不带 baseline_files 的旧客户端 → 现状（conflict）。
    r = client.post("/api/sync", json={
        "device_name": "mac",
        "local_files": {"notes/test.md": "different-hash"},
        "logs": [],
    })
    assert r.json()["conflicts"] == ["notes/test.md"]


def test_sync_baseline_malformed_degrades_to_conflict(client) -> None:
    # Review Focus 3：baseline 非 dict / 值非 str → 按 None 处理。
    for bad in ("not-a-dict", {"notes/test.md": 123}, [1, 2]):
        r = client.post("/api/sync", json={
            "device_name": "mac", "local_files": {"notes/test.md": "x"},
            "baseline_files": bad, "logs": [],
        })
        assert r.json()["conflicts"] == ["notes/test.md"], bad


def test_sync_alltime_delete_beats_window(db_conn, client, vault_dir) -> None:
    # F2 头条场景：删除行之上先堆 250 条无关变更（挤爆 200 行窗口），
    # 删除防护仍须生效（全量重放不按窗口截断）。
    from server.vault.db.changes import log_change
    from server.vault.db.file_index import delete_file, upsert_file

    upsert_file(db_conn, "notes/gone.md", "h1", 0.0)
    log_change(db_conn, "notes/gone.md", "deleted", source="watchdog",
               pre_hash="h1")
    for i in range(250):  # 把删除行推到 200 行窗口之外
        log_change(db_conn, f"noise/f{i}.md", "created", source="web")

    r = client.post("/api/sync", json={
        "device_name": "mac",
        "local_files": {"notes/gone.md": "h1"},  # 客户端的陈旧副本
        "logs": [],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["to_delete"] == ["notes/gone.md"]
    assert body["to_push"] == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_api.py -k "sync_baseline or sync_alltime or sync_no_baseline" -v`
Expected: FAIL（`to_pull` 为空 / `to_delete` 为空 —— 现状是 conflict 与窗口截断）

- [ ] **Step 3: 实现**（`server/routes/sync.py`）：把 :52-74 的窗口查询段替换为

```python
            # F2: the deletion gate is ALL-TIME — replay every move/delete
            # row (seq ASC, unbounded per spec 2026-11-15 §2) into
            # {client-held path: pre_hash}. Not gated by last_sync.
            from ..sync import derive_deleted_hashes

            replay_rows = ctx.db_conn.execute(
                "SELECT op, path, old_path, pre_hash FROM change_log "
                "WHERE op IN ('move', 'delete') ORDER BY seq ASC"
            ).fetchall()
            deleted_hashes = derive_deleted_hashes([dict(r) for r in replay_rows])

            # Move rule keeps its windowed recent_moves (rename detection).
            recent_moves = None
            if last_sync:
                from ..vault.db.changes import get_changes as _get_changes

                all_changes = _get_changes(ctx.db_conn, since=float(last_sync), limit=200)
                recent_moves = [
                    c for c in all_changes
                    if c["action"] == "moved" and c.get("from_path")
                ]

            baseline = body.get("baseline_files")
            if not isinstance(baseline, dict) or not all(
                    isinstance(k, str) and isinstance(v, str)
                    for k, v in baseline.items()):
                baseline = None
```

compare 调用改为 `compare_files(local_files, server_files, recent_moves=recent_moves, deleted_hashes=deleted_hashes, baseline=baseline)`（不再传 `recent_deletes`）。原 :67-74 的 `deleted_hashes` SQL 查询整段删除（被重放取代）。

- [ ] **Step 4: 跑受影响测试面确认通过**

Run: `.venv/bin/python -m pytest tests/test_api.py tests/test_sync.py tests/test_deletion_rollback.py -v`
Expected: 全部 PASS——重点确认 test_deletion_rollback.py 的四个既有路由级 sync 测试（:267/:291/:309/:329/:355）在新重放下语义不变：stale→to_delete、重写→to_push、外部删除（pre_hash=''）→to_delete、30 天窗口→to_push、latest-wins。

- [ ] **Step 5: Commit**

```bash
git add server/routes/sync.py tests/test_api.py
git commit -m "feat(sync): /api/sync wires all-time replay delete gate + optional baseline_files (review F1/F2)"
```

---

### Task 5: 客户端基线上送 + 拉取护栏（F1 客户端半边 + Review Focus 1）

**Files:**
- Modify: `client/sync.py`（:96-213 `sync_once`）
- Test: `tests/test_client_sync.py`（MockTransport 模式已有，见 :34-48）

**Interfaces:**
- Consumes: Task 4 的请求字段 `baseline_files`。
- Produces: 客户端行为——① 请求体新增 `"baseline_files": state["files"]`（缺省 `{}`）；② to_pull 写盘前哈希护栏：磁盘内容 ≠ 本轮上报的 `local_files[path]`（同步周期内被编辑）→ 服务端内容存冲突副本（与 conflicts 分支同命名规则），不覆盖本地。抽取模块级帮助函数 `_conflict_path(p: Path, device_name: str) -> Path`，conflicts 分支与护栏共用。

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_client_sync.py`）

```python
def test_sync_once_sends_baseline_files(tmp_path, monkeypatch):
    (tmp_path / "vault").mkdir()
    captured = {}

    def handler(request: httpx.Request):
        import json
        captured.update(json.loads(request.content.decode()))
        return httpx.Response(200, json={
            "to_pull": [], "to_push": [], "to_delete": [], "conflicts": []})

    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs.pop("timeout", None)
        return real_client(transport=httpx.MockTransport(handler), *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr("client.sync.httpx.Client", side_effect=fake_client)
        sync_once(_client_config(tmp_path), {"last_sync": "", "files": {"a.md": "h1"}})

    assert captured["baseline_files"] == {"a.md": "h1"}


def test_pull_guard_conflict_saves_instead_of_overwrite(tmp_path, monkeypatch):
    # Review Focus 1：scan 之后磁盘文件又被编辑（真实哈希 ≠ 上报哈希）
    # → 拉取改为存冲突副本，本地编辑不被覆盖。
    import hashlib
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_bytes(b"user edit during cycle")   # 磁盘真实态
    reported = {"a.md": hashlib.sha256(b"stale scanned bytes").hexdigest()}

    def fake_scan(vault_dir):
        return dict(reported)                                 # scan 时还没编辑

    def handler(request: httpx.Request):
        return httpx.Response(200, json={
            "to_pull": ["a.md"], "to_push": [], "to_delete": [],
            "conflicts": []})

    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs.pop("timeout", None)
        return real_client(transport=httpx.MockTransport(handler), *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr("client.sync.httpx.Client", side_effect=fake_client)
        m.setattr("client.sync.scan_local_files", fake_scan)
        sync_once(_client_config(tmp_path), {"last_sync": ""})

    assert (vault / "a.md").read_bytes() == b"user edit during cycle"   # 本地保住
    conflict = vault / "a.conflict-test.md"
    assert conflict.read_bytes() == b"server version"                    # 服务端版另存
```

（`handler` 返回的 to_pull 内容由 MockTransport 决定——本测试需要 pull 分支真正走 GET：给 handler 加 `if request.method == "GET": return httpx.Response(200, content=b"server version")`。）

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_client_sync.py -k "baseline or guard" -v`
Expected: FAIL（请求体无 `baseline_files` / `a.md` 被覆盖为 server version）

- [ ] **Step 3: 实现**（`client/sync.py`）：

1. 模块级加：
```python
def _conflict_path(p: Path, device_name: str) -> Path:
    return p.with_name(f"{p.stem}.conflict-{device_name}{p.suffix}")
```
2. `sync_once` 里 POST body 增加 `"baseline_files": dict(state.get("files", {}))`（放在构造 json 的 dict 里）。
3. to_pull 循环（现 :145-154）写盘前加护栏：
```python
                dest = vault_dir / path
                on_disk = dest.read_bytes() if dest.is_file() else None
                if on_disk is not None and _hash_bytes(on_disk) != local_files.get(path):
                    conflict_path = vault_dir / _conflict_path(Path(path), config.device_name)
                    conflict_path.parent.mkdir(parents=True, exist_ok=True)
                    conflict_path.write_bytes(r.content)
                    logger.warning("pull skipped (edited during cycle), server copy saved as %s",
                                   conflict_path.relative_to(vault_dir).as_posix())
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(r.content)
```
4. conflicts 分支（现 :170-186）的命名表达式改用 `_conflict_path(Path(path), config.device_name)`（行为不变）。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_client_sync.py tests/test_sync.py -v`
Expected: 全部 PASS（既有 test_sync_once_logs_notifications / test_scan_local_files_excludes_trash_and_sync 不受影响）

- [ ] **Step 5: Commit**

```bash
git add client/sync.py tests/test_client_sync.py
git commit -m "feat(client): send baseline_files manifest + pull guard saves conflict on mid-cycle edit (review F1)"
```

---

### Task 6: 冷重建重播种删除行（F4）

**Files:**
- Modify: `server/recovery.py`（新增函数 + drill 接入 + notes 文案）
- Modify: `server/main.py`（:179 `ensure_indexed` 之后接入）
- Test: `tests/test_recovery.py`

**Interfaces:**
- Consumes: `server.vault.db.changes.log_change`（唯一 INSERT 面，op 归一）；`server.vault.orchestration.trash.list_trash`（meta 含 files[].path/.hash + source + deleted_at）；ledger.db `snapshots` 表（op='delete' 行的 path/pre_hash/ts/actor）。
- Produces: `seed_deleted_prehashes(conn, vault_dir: Path, ledger_conn=None) -> int`——change_log 为空（冷重建/全新库）时，从 trash 批次 meta + 快照表把删除行重播种进 change_log（经 `log_change`，source 取 meta source / snapshot actor，ts 取 deleted_at / snapshot ts，解析失败退 `time.time()`），返回播种行数；change_log 非空 → 幂等 no-op 返回 0。main.py 与 drill 都调用它。

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_recovery.py`；复用 conftest 的 `vault_dir/db_conn/ledger_conn`）

```python
def test_seed_deleted_prehashes_from_trash_and_snapshots(vault_dir, db_conn, ledger_conn):
    import hashlib

    from server.recovery import seed_deleted_prehashes
    from server.sync import derive_deleted_hashes
    from server.vault.db.schema import init_db
    from server.vault.orchestration.trash import move_to_trash

    def _sha(b):
        return hashlib.sha256(b).hexdigest()

    # 现实删除记录的两个幸存源：workbench trash 批次 + 快照表删除行。
    (vault_dir / "notes").mkdir(exist_ok=True)
    (vault_dir / "notes" / "a.md").write_bytes(b"aaa")
    move_to_trash(db_conn, vault_dir, ["notes/a.md"], "web")
    ledger_conn.execute(
        "INSERT INTO snapshots (ts, call_id, path, new_path, op, pre_hash, actor,"
        " run_id, blob_name, live, finalized) VALUES (?, 'c1', 'notes/b.md', '',"
        " 'delete', 'h-b', 'organizer', '', '', 1, 1)",
        (1000.0,))
    ledger_conn.commit()

    # 冷重建态：全新 index.db（模拟 drill / 真实冷丢失后的新库）。
    fresh = init_db(vault_dir / ".sync" / "index-seed-test.db")
    n = seed_deleted_prehashes(fresh, vault_dir, ledger_conn)
    assert n >= 2
    rows = {(r["path"], r["pre_hash"], r["source"])
            for r in fresh.execute(
                "SELECT path, pre_hash, source FROM change_log WHERE op='delete'")}
    assert ("notes/a.md", _sha(b"aaa"), "web") in rows
    assert ("notes/b.md", "h-b", "organizer") in rows

    # 播种产物可被重放函数消费（sync lane 闭环）。
    replay = fresh.execute(
        "SELECT op, path, old_path, pre_hash FROM change_log "
        "WHERE op IN ('move','delete') ORDER BY seq ASC").fetchall()
    assert derive_deleted_hashes([dict(r) for r in replay])["notes/a.md"] == _sha(b"aaa")

    # 幂等：change_log 非空后再调 → no-op。
    assert seed_deleted_prehashes(fresh, vault_dir, ledger_conn) == 0
```

```python
def test_seed_noop_when_change_log_nonempty(vault_dir, db_conn, ledger_conn):
    from server.recovery import seed_deleted_prehashes
    from server.vault.db.changes import log_change

    log_change(db_conn, "notes/live.md", "created", source="web")
    assert seed_deleted_prehashes(db_conn, vault_dir, ledger_conn) == 0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_recovery.py -k seed -v`
Expected: FAIL（`ImportError: cannot import name 'seed_deleted_prehashes'`）

- [ ] **Step 3: 实现**（`server/recovery.py`，放在 `_rebuild_files_table` 之后）：

```python
def seed_deleted_prehashes(conn, vault_dir: Path, ledger_conn=None) -> int:
    """Cold-rebuild seeding: change_log is wiped with index.db, but the
    deletion records the sync lane's stale-copy gate needs survive elsewhere
    (trash meta.json for workbench deletes; the snapshots table in ledger.db
    for bgagent/mcp deletes). Re-book those delete rows (via log_change —
    the sole INSERT surface) so deleted_hashes resurrects with the index.
    Idempotent: runs only when change_log is empty (fresh rebuild / new
    vault); a vault with live history is a no-op. Returns rows seeded."""
    if conn.execute("SELECT count(*) FROM change_log").fetchone()[0]:
        return 0
    records: list[tuple[float, str, str, str]] = []  # (ts, path, pre_hash, source)
    for meta in list_trash(Path(vault_dir)):
        try:
            ts = datetime.strptime(meta.get("deleted_at", ""),
                                   "%Y-%m-%dT%H:%M:%SZ").replace(
                                       tzinfo=timezone.utc).timestamp()
        except ValueError:
            ts = time.time()
        for f in meta.get("files", []):
            if f.get("hash"):
                records.append((ts, f["path"], f["hash"],
                                meta.get("source") or "web"))
    if ledger_conn is not None:
        for r in ledger_conn.execute(
                "SELECT path, pre_hash, ts, actor FROM snapshots "
                "WHERE op = 'delete' AND pre_hash != ''"):
            records.append((r["ts"], r["path"], r["pre_hash"], r["actor"] or "watchdog"))
    records.sort(key=lambda x: x[0])
    for ts, path, pre_hash, source in records:
        log_change(conn, path, "deleted", ts=ts, source=source, pre_hash=pre_hash)
    if records:
        logger.warning("recovery: re-seeded %d delete rows into change_log "
                       "(from trash meta + snapshots)", len(records))
    return len(records)
```

imports 补：`from datetime import datetime, timezone`、`import time`、`from server.vault.db.changes import log_change`、`from server.vault.orchestration.trash import list_trash`（trash 不 import recovery，无环）。

drill 接入：`run_recovery_drill` 的 `_rebuild_files_table(conn, vault_dir)` 之后加——

```python
    drill_ledger_conn = None
    ledger_path = vault_dir / ".sync" / "ledger.db"
    if ledger_path.is_file():
        drill_ledger_conn = sqlite3.connect(str(ledger_path))
    seeded = seed_deleted_prehashes(conn, vault_dir, drill_ledger_conn)
    if drill_ledger_conn is not None:
        drill_ledger_conn.close()
```

并把 notes 里 `"change_log empty is expected: the event stream is not replayed..."` 那条替换为：
`f"delete rows re-seeded into change_log: {seeded}（rename/move 历史是接受的丢失——复活防护已恢复，改名检测没有）"`。

main.py 接入：`ensure_indexed(conn, vault_dir)`（:179）之后：

```python
    from .recovery import seed_deleted_prehashes
    seeded = seed_deleted_prehashes(conn, vault_dir, ledger_conn)
    if seeded:
        logging.getLogger("md-vault.recovery").info(
            "cold rebuild: re-seeded %d delete rows into change_log", seeded)
```

（冷丢失场景 ledger.db 幸存 → snapshots 源生效；trash 批次在盘上 → meta 源生效。快照表被 30 天 GC 清掉的删除与已 purge 的 trash 批次无法恢复——文档化接受，见报告 F4。）

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_recovery.py tests/test_deletion_rollback.py -v`
Expected: 全部 PASS。既有 `test_recovery_drill`（:101）与 `test_recovery_cli_main`（:134）断言 `change_log_rows == 0`：其夹具 vault 无任何删除记录 → 播种 0 行 → 断言不变；若实际运行发现夹具带删除记录导致非 0，把断言改为 `== report["notes"] 中 re-seeded 行数` 而非硬编码 0（以运行结果为准，二选一必须落定）。

- [ ] **Step 5: Commit**

```bash
git add server/recovery.py server/main.py tests/test_recovery.py
git commit -m "feat(recovery): seed change_log delete rows from trash meta + snapshots on cold rebuild (review F4)"
```

---

### Task 7: 全量回归 + AGENTS.md 契约更新

**Files:**
- Modify: `AGENTS.md`（Change ledger / Trash 两节各加一句）

**Interfaces:**
- Consumes: 全部前序任务。
- Produces: 与现实一致的 AGENTS.md 契约描述（防止下次审查按旧契约误判）。

- [ ] **Step 1: 全量回归**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: 除 4 个既有基线失败（`test_bgagent_runner.py` 时间炸弹 ×3 + `test_config_legal_layer.py` 机器状态 ×1，见审查 F14，不在本计划范围）外全部通过；本计划新增/修改的测试全绿。

- [ ] **Step 2: 更新 AGENTS.md**（两处，各一句，措辞可微调但事实不变）：

Change ledger 节末尾追加：
`- **Sync lane (2026-11-16)**: /api/sync does three-way classification against the client's uploaded baseline (`baseline_files`, optional — absent = legacy conflict behavior); the deletion gate replays ALL change_log move/delete rows (seq ASC, unbounded) via `derive_deleted_hashes` into {original path: pre_hash}, resolving move chains — recent_deletes/200-row window no longer gate it. Cold rebuilds re-seed delete rows via `recovery.seed_deleted_prehashes` (trash meta + ledger snapshots).`

Trash 节 pre_hash 句后追加：
`The map keys are move-chain-resolved original paths (a client-held pre-move copy of a moved-then-deleted file is still caught); a move→edit→delete chain leaves the client's pre-move copy in to_push (hash evidence insufficient — documented residual blind spot).`

- [ ] **Step 3: Commit**

```bash
git add AGENTS.md
git commit -m "docs: AGENTS.md sync-lane contract update (baseline three-way, all-time replay, cold-rebuild seeding)"
```

---

## Self-Review 记录

- **Spec coverage**: F1（Task 3 服务端 + Task 5 客户端）、F2（Task 1/2/4）、F3（Task 1 复合场景 test_derive_move_then_delete_compound + 链式 test_derive_chain_move_then_delete，Task 4 路由供数）、F4（Task 6）——四条发现均有落点；F5/F9-F15 明确不在本计划（范围经用户确认）。
- **Step scan**: 每步单动作可验收；Task 1 Step 3 与 Task 6 Step 3 的函数体是测试无法唯一确定的算法（重放状态机、播种来源合并），属计划应携带的内容；其余任务只给签名与断言。
- **Type consistency**: `derive_deleted_hashes`（Task 1 定义 → Task 4 消费）、`baseline` 参数与 `baseline_files` 字段名（Task 3/4/5 一致）、`seed_deleted_prehashes(conn, vault_dir, ledger_conn=None)`（Task 6 定义 → main/drill 调用一致）已交叉核对。
- **Review Focus**: 5 条全部有钉死测试（Task 5 护栏 / Task 3 旧客户端与坏基线 / Task 1 latest-wins 与别名歧义 / Task 2 残余盲区）。
- **Proportion**: 计划 7 个任务、两处算法代码块、其余为签名+断言——未超出发现的体量。
