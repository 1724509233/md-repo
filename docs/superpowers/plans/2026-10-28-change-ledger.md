# 变更账本（server/ledger.py）Implementation Plan

> **For agentic workers:** 本计划按 task 逐条执行，步骤用 checkbox（`- [ ]`）跟踪。Task N 的 commit 落定前不得开始 Task N+1。每个 task 的 grep 门槛必须在该 task 的 commit 之前通过。

**Goal:** 把变更记录账本协议（register-before-write → watcher consumer 恰好记一行）从 AGENTS.md 的 prose 调用约定提升为一个 module —— `server/ledger.py`，change_log + files 表的唯一写面。

**Architecture:** 新顶层 module `server/ledger.py` 拥有写注册状态（TTL dict）、两个 writer 入口（`register` = watcher 记账，`record` = web 同步落账）、folder 变体（`record_folder`）与消费端（`consume`）。row mutators `apply_change`/`apply_folder_change` 留在 `server/vault/orchestration/file_ops.py`；迁移后**没有其他 module import 它们**（硬强制）。两个 ledger commit：先零行为改动的抽取（旧名变薄委托），再全量调用点迁移 + 硬强制 + 文档刷新。

**Tech Stack:** Python 3.12、仅 stdlib、pytest（`.venv/bin/python -m pytest`）。

**Spec:** 2026-10-28 架构评审 Card 1 + grilling 两轮冻结决策（9 项）：module 落点 `server/ledger.py`；接口 `register`/`record`/`record_folder`/`consume`；`consumer_skip` 从 writer 面消失（方法名即选择）；硬强制（无其他 module import mutators）；`vault/service.py` facade 移除 3 个 re-export；main.py 四个 `_on_*` 闭包保形换身体；`consume_file_event` 更名 `ledger.consume`；`tests/test_write_registry.py` → `tests/test_ledger.py` + 4 个新协议 pin 测试；两 commit（抽取 → 迁移）；AGENTS.md 段重写为 4 条；GLOSSARY 新术语「变更账本」+ 改写 write registry + 随迁 write source/delete（watchdog 不动）；folder 方法命名 `record_folder`。

## Global Constraints

- 解释器只用 `.venv/bin/python`（`python` 不在 PATH）。
- 测试基线：开工前 `699 passed, 1 skipped`（skip 既有；含旧 test_write_registry.py 的 5 个测试）；Task 1 后 `708`（+9 pin）；Task 2 后 `703 passed, 1 skipped`（−5 旧文件删除，净 +4）。
- Task 1–2 零行为改动：699 个既有测试逐字通过（Task 2 的 `git mv` 仅改文件名）。
- import 方向单边：`server.ledger` → `file_ops`；**file_ops 不得 import ledger**（循环守卫）。write_registry 委托 ledger；ledger 不 import write_registry。
- 测试夹具写文本文件一律 `write_bytes`（AGENTS.md Windows 换行 gotcha）。
- 不得触碰（代码）：`server/model.py`、`server/config/*`、用户本地 `config.yaml`（ModelConfig 去重范围，已于 2026-10-28 完成）；未跟踪的 `docs/*.html` ×2。`server/agent/spec.py`：仅允许 Task 2 中 `write_source` 注释里 `(write_registry source)` → `(ledger source)` 一行注释刷新（reviewer F1），代码不动。
- Commit message 逐字使用本计划给定的文本。
- 每个 task 列出的 grep 门槛，在该 task 的 commit **之前**执行并通过。

## Review Focus

1. **folder 事件与 registry**：`record_folder` 不得触碰 registry（watcher 无 folder 回调，登记的 folder 路径只会留下 TTL 僵尸条目）。期望行为：folder 行同步落定、registry 零接触。→ Task 1 step 2：`test_record_folder_does_not_register`。
2. **skip 标记的 TTL 窗口**：`record` 的 skip 标记与所有登记共用 10s TTL；FS 事件在过期后到达会兜底 `source="watchdog"` 重记一行（双行窗口）——这是同步记账设计的**既有**窗口，本次重构保持等价而非修复。期望行为：标记经 `_pop` 像普通条目一样弹出（证明机制等价）。→ Task 1 step 1：`test_record_skip_mark_consumed_by_consumer`。
3. **move 的 from_path 归因**：writer 只登记目标路径（tools/move_docs 注册 `to_path`），`from_path` 由 watcher 事件携带。期望行为：moved 行带 from_path 列。→ Task 1 step 2：`test_consumer_books_moved_with_from_path`。
4. **main.py 死 import**：`apply_change` 在 main.py:17 被 import 但零使用。期望行为：迁移后消失，`ensure_indexed`/`init_db` 保留。→ Task 2 step 3 + grep 门槛。
5. **deprecated shim 面**：`server/vault/__init__.py` 从 `server.vault.service` 重导出——facade 减 3 名后 shim 对剩余名字保持可用。期望行为：`from server.vault import init_db, upsert_file, ...` 不回归。→ Task 2 step 5，由全量测试（shim 被测试 import）验证。

---

### Task 0: 提交两块已验证的待办工作（前置：干净树）✅ 已完成 2026-10-28（53d5181 日志配置 / ecc71d9 config 拆包）

**Files:** 无代码改动（git hygiene）

**Interfaces:**
- Consumes: 无
- Produces: 干净工作树 —— 后续 task 的 commit 不与既有 staged/unstaged 状态冲突

- [x] **Step 1: 基线测试**（已验证：699 passed, 1 skipped）

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: `699 passed, 1 skipped`（若不一致，停下报告，不得带病继续）

- [x] **Step 2: commit 日志配置工作**

⚠️ 陷阱（2026-10-28 实测踩中）：若 index 中已有其他 staged 工作（如 config 拆包），`git add` 本组文件后裸 `git commit` 会把两组混进一个 commit。安全序列 = 先 `git reset` 清空 index，再逐组 add + commit。

```bash
git reset
git add server/main.py server/logrotate.py tests/test_config.py config.example.yaml tests/test_logrotate.py
git commit -m "feat: server 日志配置（级别/路径/保留天数 + TimedRotatingFileHandler 滚动 + LogConfig）"
```

- [x] **Step 3: commit config 拆包**（reset 后需重新暂存；`git add -A` 记录 rename 的删除半边）

```bash
git add -A server/config.py server/config/
git commit -m "refactor: server/config.py 按域拆包为 server/config/（11 个公开符号经 __init__ 重导出，import 站点零改动）"
```

- [x] **Step 4: 验证树状态**

Run: `git status --short`
Expected（执行时实况）：仅剩未跟踪 docs 文件（本计划 `2026-10-28-change-ledger.md` + 两个 `docs/*.html`，不动）。`server/model.py` 已由 Card 6（ModelConfig 单一化）commit，不再未跟踪。

---

### Task 1: 创建 `server/ledger.py` + 新 pin 测试（抽取，零行为改动）

**Files:**
- Create: `server/ledger.py`
- Modify: `server/write_registry.py`（72 行 → 薄委托过渡层）
- Test: Create `tests/test_ledger.py`（9 个测试；Task 2 删除旧 `tests/test_write_registry.py`，其 5 个测试由本文件 5 个 pin 替代）

**Interfaces:**
- Consumes: `file_ops.apply_change(conn, vault_dir, path, action, source="unknown", from_path="", new_hash="", update_refs=True, batch_id="")`；`file_ops.apply_folder_change(conn, vault_dir, relpath, action, source="web", from_path="")`
- Produces（后续 task 全部依赖的精确签名）:
  - `register(rel_path: str, source: str, batch_id: str = "") -> None`
  - `record(conn, vault_dir, rel_path: str, action: str, source: str, *, from_path: str = "", new_hash: str = "", batch_id: str = "", **passthrough) -> None`
  - `record_folder(conn, vault_dir, relpath: str, action: str, source: str = "web", from_path: str = "") -> None`
  - `consume(conn, vault_dir, rel_path: str, action: str, from_path: str = "") -> tuple[str, str, bool]`
  - 私有 `_mark(rel_path, source, batch_id, skip) -> None` / `_pop(rel_path) -> tuple[str, str, bool]`（Task 2 的薄委托与 pin 测试使用）

**`server/ledger.py` 的 module docstring（逐字）：**

```
"""Change ledger: the sole write surface for the change_log + files tables.

Writers call register() BEFORE writing (in-process channels: agent tools,
executor, bgagent runner, async web paths) — the watcher consumer books
exactly one correctly-attributed row. Writers call record() when the row
must land synchronously (web UI: the console feed must respond while the
user watches); the consumer then books nothing for that path. Folders use
record_folder() — folder rows never touch the registry (the watcher has no
folder callbacks, so there is no double-row risk).

apply_change / apply_folder_change (server/vault/orchestration/file_ops.py)
are the row mutators: no module other than this one imports them. Unregistered
writes (external processes such as MCP stdio, direct disk edits) fall back to
source="watchdog" on the consumer side.

Note: skip marks share the 10s TTL; an FS event arriving after expiry
re-books the row under source="watchdog" — a pre-existing window of the
synchronous-record design, preserved here, not fixed.
"""
```

- [x] **Step 1: 写失败的测试文件 `tests/test_ledger.py`**

9 个测试 = 5 个适配旧测试 + 4 个新 pin 测试。骨架（断言值即规格；`conn`/`vault_dir` 用 conftest 夹具，写文件用 `write_bytes`）：

```python
"""Pin tests for the change ledger (server/ledger.py)."""
from __future__ import annotations

import time

import pytest

from server import ledger


def test_register_pop_returns_tuple():
    ledger.register("a/b.md", "executor", batch_id="run-1")
    assert ledger._pop("a/b.md") == ("executor", "run-1", False)


def test_pop_unregistered_falls_back_watchdog():
    assert ledger._pop("nope.md") == ("watchdog", "", False)


def test_expired_entry_falls_back_watchdog(monkeypatch):
    # 移植旧 test_write_registry.py::test_expired_entry_falls_back_watchdog，
    # 把 write_registry._REGISTRY_TTL 换成 ledger._REGISTRY_TTL


def test_record_books_row_synchronously(vault_dir, db_conn):
    # 写文件（write_bytes）→ ledger.record(db_conn, vault_dir, "notes/x.md", "created", "web")
    # 断言：change_log 恰好一行，source="web"，且 consume 之前就已存在（同步语义）


def test_record_skip_mark_consumed_by_consumer(vault_dir, db_conn):
    # record 之后调 ledger.consume(db_conn, vault_dir, "notes/y.md", "created")
    # 断言：change_log 仍恰好一行（consumer 未重记 = skip 标记生效，Review Focus 2）


def test_consumer_books_one_row_with_batch(vault_dir, db_conn):
    # 写文件 → ledger.register("notes/z.md", "executor", batch_id="run-9")
    # → ledger.consume(db_conn, vault_dir, "notes/z.md", "created")
    # 断言：恰好一行，source="executor"，batch_id="run-9"（旧 test_consumer_passes_batch_id_through 的账本版）


def test_consumer_books_moved_with_from_path(vault_dir, db_conn):
    # 写 old.md → ledger.register("new.md", "agent") → consume(..., "moved", from_path="old.md")
    # 断言：行带 from_path="old.md"（Review Focus 3）


def test_record_folder_books_row(vault_dir, db_conn):
    # mkdir "index/tmp" → ledger.record_folder(db_conn, vault_dir, "index/tmp", "created", "web")
    # 断言：change_log 一行，source="web"


def test_record_folder_does_not_register():
    # ledger.record_folder 前后：ledger._pop("f") == ("watchdog", "", False)（registry 零接触，Review Focus 1）
```

- [x] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m pytest tests/test_ledger.py -q`
Expected: FAIL（`ModuleNotFoundError: server.ledger`）

- [x] **Step 3: 实现 `server/ledger.py`**

从 write_registry.py **逐字搬运**（不重写）：`_registry` dict、`_registry_lock`、`_REGISTRY_TTL = 10.0`、以及现有 `register_write`/`pop_source` 的函数体 → `_mark`/`_pop`（含过期清理逻辑；`_mark` 保留 `skip: bool = False` 参数，`_pop` 语义不变）。四个公开函数：
- `register`：`_mark(rel_path, source, batch_id, skip=False)`
- `record`：`_mark(rel_path, source, batch_id, skip=True)` 然后 `apply_change(conn, vault_dir, rel_path, action, source=source, from_path=from_path, new_hash=new_hash, batch_id=batch_id, **passthrough)`
- `record_folder`：仅 `apply_folder_change(conn, vault_dir, relpath, action, source=source, from_path=from_path)` —— **不得**调用任何 registry 函数
- `consume`：file_ops.consume_file_event 的函数体原样移植，把 `pop_source` 换成 `_pop`、`apply_change` 保持（同参数）；返回 `(source, batch_id, skip)` 三元组
- import：`from .vault.orchestration.file_ops import apply_change, apply_folder_change`（模块级）。ledger **不得** import write_registry。

- [x] **Step 4: 改写 `server/write_registry.py` 为薄委托**

保留模块名作过渡 alias（Task 2 删除）。docstring 缩为一行 + 过渡注记：「change ledger（server/ledger.py）owns the write registry; this module is a transitional alias, removed in the migration commit」。`from .ledger import _mark, _pop`；`register_write(rel_path, source, batch_id="", consumer_skip=False)` → `_mark(rel_path, source, batch_id, skip=consumer_skip)`（`consumer_skip` 参数为过渡保留，最后调用者 Task 2 移除）；`pop_source(path)` → `return _pop(path)`。

- [x] **Step 5: 新旧测试双绿**

Run: `.venv/bin/python -m pytest tests/test_ledger.py tests/test_write_registry.py -q`
Expected: `14 passed`（9 新 + 5 旧经委托链通过）

- [x] **Step 6: 全量测试**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: `708 passed, 1 skipped`（699 基线 + 9 新；旧文件未删）

- [x] **Step 7: import 冒烟 + 循环检查**

Run: `.venv/bin/python -c "import server.ledger, server.write_registry, server.api, server.main; print('ok')"`
Expected: `ok`（无 ImportError / 循环）

- [x] **Step 8: grep 门槛**

Run: `grep -n "import write_registry" server/ledger.py`
Expected: 无输出（ledger 不依赖 write_registry）

- [x] **Step 9: commit**

```bash
git add server/ledger.py server/write_registry.py tests/test_ledger.py
git commit -m "feat: 变更账本 module（server/ledger.py）— write registry + consumer 抽入一个 module，旧名薄委托过渡，9 个 pin 测试"
```

---

### Task 2: 迁移全部调用点 + 硬强制 + 文档刷新

> reviewer（oracle，Task 1 过门）的前向风险 F1–F9 已吸收进本 task 各 step（正文以「reviewer F#」标注）；其中 gate 3 从全字符串匹配收窄为 import-only（F7），否则计划自身的文档替换文本会让 step 7 必然红灯。

**Files:**
- Modify: `server/api.py`（10 处调用点 + import 块）
- Modify: `server/main.py`（4 个 `_on_*` 闭包 + import 行 :17）
- Modify: `server/agent/tools/{write_doc,edit_doc,move_docs,delete_doc}.py`（各 1 处注册 + docstring）
- Modify: `server/bgagent/executor.py`（:74 import + 3 处 + docstrings :43-44,181-184）
- Modify: `server/bgagent/runner.py`（:22 import + 2 处 + 注释 :448）
- Modify: `server/vault/orchestration/file_ops.py`（删 `consume_file_event` :34-51 + `from ...write_registry import pop_source` :31）
- Modify: `server/vault/service.py`（import 行 + `__all__` 减 3 名 + docstring 示例行）
- Delete: `server/write_registry.py`
- Delete: `tests/test_write_registry.py`（其 5 个测试的账本版已在 `tests/test_ledger.py`，保留会留下 import 死代码）
- Modify: `tests/conftest.py`（:13 import + simulate_consumer docstring）
- Modify: `AGENTS.md`（:45 段，全文替换）
- Modify: `GLOSSARY.md`（:26 delete、:47-49 write registry、:172 write source；新增「变更账本」；watchdog :44 不动）
- Modify: `server/proposals.py`（:77-78 docstring）
- Modify: `server/agent/spec.py`（`write_source` 字段注释 `(write_registry source)` → `(ledger source)`；reviewer F1，一行注释）
- Modify: `server/ledger.py`（`_mark` docstring 中的 `write_registry` 字面量 → "the transitional alias (removed in the migration commit)"；reviewer F2，一行）
- Modify: `tests/test_agent.py`（`test_write_source_attribution`：3 × `patch("server.write_registry.register_write")` → `patch("server.ledger.register")` + docstring 一处；断言形状不变——`ledger.register` 签名与 `register_write` 逐位兼容，工具函数内 `from ... import ledger` 是运行时属性查找，patch 机制不变；reviewer F5）
- Modify: `tests/test_api.py`（`test_upload_doc` :110-111：`from server.write_registry import _registry, _registry_lock` → `from server.ledger import ...` + 注释；Task 2 后 upload 走 `ledger.register` 写同一个 `ledger._registry`，断言 `"new/file.md" in _registry` 成立；reviewer F4）

**Interfaces:**
- Consumes: Task 1 的 `ledger.register` / `ledger.record` / `ledger.record_folder` / `ledger.consume`（签名见 Task 1 Produces）
- Produces: 硬强制终态 —— `apply_change`/`apply_folder_change` 的 importer 集合 = {`server/ledger.py`}；`simulate_consumer` 驱动 `ledger.consume`

**api.py 10 处调用点映射（逐字）：**

| 位置 | 现状 | 改为 |
|---|---|---|
| ~:316-327 create_file | 局部 `from .write_registry import register_write` + `register_write(rel, "web", consumer_skip=True)` + `apply_change(db_conn, vault_dir, rel.as_posix(), "created", source="web")` | `ledger.record(db_conn, vault_dir, rel.as_posix(), "created", "web")`（删局部 import） |
| ~:365-386 move | `register_write(new, "web", consumer_skip=True)` + `apply_change(..., "moved", ..., from_path=...)`（以 :386 实际参数为准） | `ledger.record(db_conn, vault_dir, new, "moved", "web", from_path=<:386 的 from_path 值>)` |
| :340 folder create | `apply_folder_change(db_conn, vault_dir, rel.as_posix(), "created", source="web")` | `ledger.record_folder(db_conn, vault_dir, rel.as_posix(), "created", "web")` |
| :381 folder move | `apply_folder_change(...)`（以实际参数为准） | `ledger.record_folder(...)` 同参数 |
| :425 delete | `register_write(..., consumer_skip=True)` + `apply_change(..., "deleted", source="web")` | `ledger.record(db_conn, vault_dir, rel.as_posix(), "deleted", "web")` |
| :446 + :488 fold-delete 循环 | 循环内 `register_write(r, "web", consumer_skip=True)` + `apply_change(db_conn, vault_dir, r, "deleted", source="web")` | 循环内 `ledger.record(db_conn, vault_dir, r, "deleted", "web")` |
| :485 folder delete | `apply_folder_change(...)` | `ledger.record_folder(...)` 同参数 |
| ~:506-508 upload | `register_write(rel_str, "web")` | `ledger.register(rel_str, "web")` |
| ~:872-875 rollback restore 循环 | 局部 `from .write_registry import register_write as _rw` + 循环 `_rw(path, "web")` | 循环 `ledger.register(path, "web")`（删局部 import） |
| ~:1189-1191 save_doc | `register_write(rel.as_posix(), "web")` | `ledger.register(rel.as_posix(), "web")` |

- [x] **Step 1: api.py 迁移**

上表 10 处逐一替换；import 块：从 `from .vault.service import (...)` 中移除 `apply_change`、`apply_folder_change`（保留 `get_all_files`、`upsert_file`）；新增模块级 `from . import ledger`。另有 4 条紧邻调用点的注释含 `consumer_skip=True stops...` 字样（reviewer F8，在 api.py 内 `grep -n consumer_skip` 定位，约 :319/:367/:411/:445）必须一并改写——gate 4 对 `consumer_skip` 字面量零容忍；建议措辞 "record() marks skip — consumer books nothing"。
验证：`.venv/bin/python -c "import server.api"` → 无错。

- [x] **Step 2: tools ×4 迁移**

每个工具：函数内局部 import `from ...write_registry import register_write` → `from ... import ledger`；调用 `register_write(<path>, ctx.write_source, batch_id=ctx.run_id)` → `ledger.register(<path>, ctx.write_source, batch_id=ctx.run_id)`（move_docs 注册的是 `to_path`，保持）；docstring 中「register_write」→「ledger.register」、「watcher via _on_moved → apply_change」→「ledger.consume → apply_change」。
验证：`.venv/bin/python -c "from server.agent.tools import TOOL_REGISTRY; print(len(TOOL_REGISTRY))"` → 15。

- [x] **Step 3: main.py 迁移**

import 行 :17 → `from .vault.service import ensure_indexed, init_db`（**移除** `apply_change`（死 import，Review Focus 4）与 `consume_file_event`）；新增 `from . import ledger`。四个闭包身体：
```python
def _on_created(path: str) -> None:
    ledger.consume(conn, vault_dir, path, "created")
def _on_modified(path: str) -> None:
    ledger.consume(conn, vault_dir, path, "modified")
def _on_moved(old: str, new: str) -> None:
    ledger.consume(conn, vault_dir, new, "moved", from_path=old)
def _on_deleted(path: str) -> None:
    ledger.consume(conn, vault_dir, path, "deleted")
```
验证：`.venv/bin/python -c "import server.main"` → 无错。

- [x] **Step 4: executor + runner 迁移**

executor.py：:74 `from ..write_registry import register_write` → `from .. import ledger`；:184/:302/:376 `register_write(...)` → `ledger.register(...)`；docstrings 范围 :41-46（:42 有 `write_registry` 字面量，reviewer F6 行号漂移修正；「the writer never calls apply_change itself」保留，改前半句引用）与 :181-184 注记同步更新。
runner.py：:22 同换；:296/:525 同换；:448 注释「工具层 register_write 读」→「工具层 ledger.register 读」；另模块 docstring :5 与 `_write_report` docstring :519 各有一处 `register_write` 字面量（reviewer F3），同步改为 `ledger.register`。
验证：`.venv/bin/python -c "import server.bgagent.executor, server.bgagent.runner; print('ok')"`。

- [x] **Step 5: 删除旧面（write_registry / consume_file_event / facade re-exports / 旧测试文件）+ conftest 换源**

- `git rm server/write_registry.py`
- file_ops.py：删 `consume_file_event` 函数（:34-51）与 `from ...write_registry import pop_source`（:31）；模块 docstring :1 不动（apply_change 仍在）
- service.py：import 行 → `from .orchestration.file_ops import reconcile`；`__all__` 移除 `"apply_change"`、`"apply_folder_change"`、`"consume_file_event"`；docstring 示例行改为 `from server.vault.service import reconcile, ensure_indexed, ...`
- `git rm tests/test_write_registry.py`（5 个测试的账本版已在 tests/test_ledger.py）
- conftest.py：:13 → `from server.ledger import consume`；`simulate_consumer` 身体 → `return consume(conn, vault_dir, path, action, from_path=from_path)`；docstring 中 ``consume_file_event`` in server.vault.orchestration.file_ops, exposed via server.vault.service`` → ``consume`` in server/ledger.py（server/main.py 的四个 _on_* 回调调的同一个）；顺手刷新 docstring 中「never call apply_change themselves」措辞为账本协议措辞（reviewer F9）
验证：`.venv/bin/python -c "from server.vault import init_db, upsert_file; from server.vault.service import reconcile, ensure_indexed; print('ok')"`（Review Focus 5：shim 面不回归）

- [x] **Step 6: 全量测试**（⚖️ orchestrator 裁决：fixer 首跑红灯——计划遗漏 `tests/test_file_ops_folder.py`（清单外）与 facade 冻结决策矛盾（:11 从 service import `apply_folder_change` + 断言 re-export 存在）。裁决修复 3 处：该文件 import 换源 file_ops、`test_service_shim_reexports_same_object` 反转为 `test_facade_no_longer_reexports_mutators`（把硬强制不变量钉进测试）、test_api.py 两处过时 `consumer_skip` 措辞。复跑绿。）

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: `703 passed, 1 skipped`（708 − 5 旧文件删除）；若出现失败，逐一定位（最可疑：某处调用点漏换导致 NameError）。

- [x] **Step 7: grep 门槛（全部在 commit 前通过）**

```bash
grep -rn "write_registry\|register_write\|pop_source" server/ tests/ --include="*.py"   # 期望：零输出
grep -rn "consume_file_event" server/ tests/ --include="*.py"                            # 期望：零输出
grep -rn "import.*apply_change\|import.*apply_folder_change" server/ --include="*.py" | grep -v "file_ops.py\|server/ledger.py"  # 期望：零输出（硬强制 = mutators 无外部 importer）
grep -n "consumer_skip" server/ --include="*.py"                                         # 期望：零输出（writer 面消失）
```

gate 3 收窄为 import-only（reviewer F7）：计划自身的文档替换文本在 5 处保留 `apply_change` 字样（proposals.py:78、move_docs.py:95、delete_doc.py:46-47、executor.py:43-44、executor.py:181-183），收窄不损失硬强制语义。跑完 grep 后另枚举 server/ 中 file_ops/ledger 之外的全部 `apply_change` 残留命中，逐一确认均为注释/docstring（期望恰好上述 5 处，不多不少）。

- [x] **Step 8: AGENTS.md :45 段全文替换**

新文本（逐字）：

```markdown
- **Change ledger (server/ledger.py)**: the sole write surface for `change_log` + `files`. Writers call `ledger.register(path, source[, batch_id])` BEFORE writing (in-process channels: agent tools, executor, bgagent runner, async web) — the watcher consumer (`ledger.consume`, main.py's four `_on_*` callbacks) books exactly one correctly-attributed row. Web direct writes (manage_docs, where the console feed must respond instantly) call `ledger.record(...)` — synchronous row, consumer books nothing. Folder rows go through `ledger.record_folder(...)` (the watcher has no folder callbacks — no double-row risk). `apply_change` / `apply_folder_change` (file_ops.py) are the row mutators: **no module other than ledger imports them**; the only in-module caller is `reconcile` (offline startup, no watcher running). Unregistered writes (external processes such as MCP stdio, direct disk edits) fall back to `source="watchdog"` on the consumer side — that is the consumer's fallback, not a writer exception. op strings are normalized to the canonical 4-set by `_ACTION_TO_OP` at the `log_change` entry — do not "fix" op vocabulary in callers.
```

- [x] **Step 9: GLOSSARY.md 四处更新**

1. **新增术语**（放在 write registry 条目之前，同层级）：
```markdown
**变更账本**：
change_log + files 表的唯一写面（module `server/ledger.py`）：`register`（watcher 记账，恰好一行）/ `record`（web 直写同步落账）/ `record_folder`（folder 行，不碰 registry）；row mutator `apply_change` 住 ledger 内部，无其他 module import。write registry 是其内部登记状态。
_Avoid_: ledger module（模块名）、write registry（内部状态，不是写面）
```
2. **write registry（:47-49）定义改写**（术语保留）：「变更账本内部登记（server/ledger.py 模块级 dict，TTL 10s）：writer 落盘前 `register(path, source[, batch_id])` 登记（batch_id = 执行批次标识，executor / run 透传）；snapshot watcher 观察到变更后 pop 取走 `(source, batch_id)` 记入 change_log；`record()` 路径标记 skip，watcher 不重复记账（防双行 = 方法选择，不是布尔）；查不到 = 非项目内修改 → 兜底 `source="watchdog"`。」_Avoid_ 行不变。
3. **write source（:172）**：「4 个 ask 工具经 `register_write` 走 write registry」→「4 个 ask 工具经 `ledger.register` 走 write registry」。
4. **delete（:26）**：「经 `apply_change` 唯一变更入口落账」→「经变更账本（`server/ledger.py`）落账」。
5. watchdog（:44）不动。

- [x] **Step 10: proposals.py :77-78 docstring 更新**

「The file channel's writer-side convention (register_write → watcher consumer → exactly ONE apply_change row) is untouched」→「The file channel's writer-side convention (ledger.register → watcher consumer → ledger.consume → exactly ONE apply_change row) is untouched」。

- [x] **Step 11: 终验**

Run:
```bash
.venv/bin/python -m pytest tests/ -q          # 703 passed, 1 skipped
.venv/bin/python -c "import server.ledger, server.api, server.main, server.vault; print('ok')"   # ok
```
再跑 Step 7 四条 grep（文档改完后 write_registry 字样可能仍出现在 AGENTS.md/GLOSSARY.md 的过渡注记中 —— 门槛只查 server/ tests/ 的 .py）。

- [x] **Step 12: commit**

```bash
git add server/api.py server/main.py server/agent/tools/write_doc.py server/agent/tools/edit_doc.py server/agent/tools/move_docs.py server/agent/tools/delete_doc.py server/bgagent/executor.py server/bgagent/runner.py server/vault/orchestration/file_ops.py server/vault/service.py tests/conftest.py tests/test_agent.py tests/test_api.py server/agent/spec.py server/ledger.py AGENTS.md GLOSSARY.md server/proposals.py
git commit -m "refactor: 全部变更账本写面迁至 server/ledger + 硬强制（mutators 无外部 import）+ AGENTS/GLOSSARY 刷新"
```

（`server/write_registry.py` 与 `tests/test_write_registry.py` 的删除在 step 5 已用 `git rm` 暂存，无需再 add。）

---

## 完成定义（Definition of Done）

- 四个 commit 落定：日志配置 ✅ → config 拆包 ✅ → ledger 抽取 → ledger 迁移
- `git status --short`：仅剩 `?? docs/superpowers/plans/2026-10-28-change-ledger.md`（本计划）与 `?? docs/*.html` ×2（model.py 已随 Card 6 落定）
- 全量测试 `703 passed, 1 skipped`；四条 grep 门槛零输出
- 阅读 `server/ledger.py` 单文件即可完整理解防双行协议（prose 不再承担合同）
- 提醒用户：本地 + Windows 侧 `pip install -e .`（egg-info 陈旧为既有问题）

## 后续（不在本计划内）

- ~~ModelConfig 去重~~ ✅ 已完成 2026-10-28（`server/model.py` 单一 leaf；190521d + eb6d2f9）
- 架构评审 Card 2–5（api.py 拆 route module、proposals 读路径、build_runtime、_db_lock 再封）—— 需新一轮 grilling
