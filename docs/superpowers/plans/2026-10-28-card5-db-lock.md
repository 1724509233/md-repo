# Card 5 — vault.db：mutation functions 持有锁，`_db_lock` 停止可 import，facade 退役

Spec: `architecture-review-20261028-1711.html` card 5 · branch `feat/memory-runtime`
Cards 1/2/3/4/6 已落地。基线：**714 passed, 1 skipped**。
**oracle 评审已过门（approve-with-changes），8 项必须修改已全部并入本 plan。**

## Problem（证据）

公开 facade `server/vault/service.py`（12 个 re-export、零逻辑）shallow；子系统实际 interface 是按名字私有的 `threading.Lock`（`vault/db/schema.py:11`）。**7 个生产文件 + 1 个测试**跨包 import（snapshots.py:59、proposals.py:54、bgagent/{scheduler:40, executor:73, notify×3 延迟 :54/:153/:172}.py、vault/orchestration/file_ops.py:19 包内、vault/db/recall.py:16 包内先例）+ test_bgagent_notify.py:215。「所有写都持锁」是 prose（notify.py:45、relatedness.py:18-21、vault/db/README.md:40-41）。

**9 处复合临界区（read-then-write，封装时保持原子）**：snapshots.gc、proposals.create、proposals._advance_sql、scheduler._record_run、scheduler._armed_check×2、notify.GuardedNotificationHook.notify、notify.cleanup_old_notifications、file_ops.apply_change、executor._merge_relatedness。

## 设计决定（含 oracle 修正）

1. **锁的家不动**：`_db_lock` 留在 `vault/db/schema.py`。「停止可 import」= **server/vault 之外 `_db_lock` 标识符零出现**（grep 标识符，非仅 import 语句——`schema._db_lock` 属性访问会溜过 import-only grep）。包内 import 是 recall.py 既得先例。正当地界：包外 import `vault.db.<domain>` 的**具名读/写函数**就是合法接口（「不再伸进 vault 内部」= 零 `vault.db.schema`/`_db_lock` import，不是零 vault.db import）。
2. **vault.db 新增 4 个域模块**（mutation functions 锁内持锁；纯读 helper 保持**无锁**——单把非重入锁，读函数若持锁会与写路径互斥竞争并在锁内调用时同线程死锁；oracle 已对 9 处复合临界区逐一核实此切法处处成立）：
   - `vault/db/trigger_state.py`：`ensure_row(conn, alias)`、`get_state(conn, alias)->dict`（读无锁）、`budget_left(conn, alias, budget)->bool`（读无锁）、`record_run(conn, alias)`（复合：日 rollover 读+写，一锁）、`arm_change(conn, alias, ops, exclude_sources, now)`（复合：state 读+`get_changes_sources` scan+UPDATE **一个锁内**，返回 gate 所需新值；scan 函数无锁故锁内调用安全）、`arm_recall(conn, alias, count, now, new_qs, new_hs)`（锁内 UPDATE；count 由调用方锁外 `recall_db.scan_recall_arming` 算得——维持现状 arming 语义）。scheduler._has_human_activity_since 迁移后变无锁读，无行为变化。
   - `vault/db/notifications.py`：`insert_run(conn, ...)`、`guarded_insert(conn, report_fields, daily_cap, collapsed_msg_factory) -> action`（**count-then-act 全在一个锁内**，insert/collapsed/merged 三分支；⚠️ **insert 分支锁内内联执行，不得委托 InAppHook.notify**（其自身 `with _db_lock`，同线程非重入必死锁）；collapsed 文案经 callable 传入，UI 措辞留在 bgagent）、`count_today(conn, since)`（读）、`cleanup_old(conn, max_age_s)`（复合 count+DELETE 一锁）。
     ⚠️ **行为变更**：现状 GuardedNotificationHook 锁内判定后**释放锁再锁外 insert**（软 cap，并发可双通过）；`guarded_insert` 收紧为**硬 cap**。这是本卡的行为变更，commit message 与 docstring 明说。
   - `vault/db/proposal_records.py`：吸收 proposals.py 的**写面 SQL**——`create`（复合：INSERT→lastrowid→relpath UPDATE→transition INSERT）、`advance`（=现 _advance_sql：conditional UPDATE + rowcount 判定 + transition INSERT）、`revive_rearm`（created_at 条件 UPDATE）。⚠️ **import 方向钉死：proposal_records 不得 import server/proposals.py**（否则与 proposals→proposal_records 成环）；只返回 id/row，**record dict 塑形留在 proposals.py**（mutation 返回后走既有 lock-free `record()` 读）。⚠️ expire_overdue **无批量 UPDATE**——它是 scan() 循环调 advance()；编排留在 proposals.py，逐条走 proposal_records.advance。revive 同理：编排留 proposals.py，仅 :402-407 的锁内 UPDATE 下沉。
   - `vault/db/snapshot_records.py`：snapshots.py 4 个函数的 SQL 半（gc 的 SELECT+DELETE 复合一锁；blob 文件删除留锁外、留 server/snapshots.py 调用方）。
3. **bgagent 不再伸进 vault 内部**：scheduler 删 `_db_lock` import + 15 处裸 `conn.execute`（scheduler 7 + notify 8）全部进具名函数（gate/编排逻辑留 scheduler/notify）；notify.py 删 3 处延迟 import，`_upsert_collapsed`/`_merged_today` 的「caller holds lock」约定取消；executor._merge_relatedness 复合操作移入 `vault/db/relatedness.py` 持锁函数（包内），executor 删 import。
   ⚠️ **server/vault 内零 bgagent 运行时 import**（可 grep 验收）：guarded_insert 的 RunReport 走 `TYPE_CHECKING`-only 或调用方字段解包传参——否则 bgagent.notify ↔ vault.db.notifications 成环（现状 vault 对 bgagent 仅 schema.py:137 一条注释引用）。
   ⚠️ **GuardedNotificationHook.inner 处置**：guarded_insert 自己落库后 `inner` 成为死重（锁内调=死锁，锁外调=TOCTOU）→ **构造简化**（删 inner 参数），连带：main.py:96-97 wiring 改、test_notify_guard.py 7 处构造 + :120 `inspect.signature` 断言 + 模块 docstring「wraps InAppHook」改写。InAppHook 本体保留（agent 工具面还在用它？以 grep 为准——若仅 Guarded 用则保留其独立单测面）。
4. **vault 内部保持不变（card 图面即此）**：file_ops.apply_change 保持单一大临界区（跨 files/change_log/segments/graph/refs 五域 orchestration，vault 内持锁 = recall 先例）；changes.py/file_index.py/relatedness.py 的无锁写函数**保持无锁**（锁由 vault 内调用方持有）；relatedness docstring 约定文本随 executor 改造更新。ledger 约束不动（apply_change 仅 ledger + reconcile import）。
5. **facade 退役（deletion test 通过：shallow 零逻辑）**：⚠️ **生产 import 者 9 个模块**（相对/绝对形式全查）：main.py:18、mcp_server.py:123/171/233、recovery.py:33、ai.py:92、snapshot_watcher.py:22/301、routes/console.py:20/255/261、routes/sync.py:9、agent/tools/edit_doc.py:37、agent/tools/get_related_docs.py:33（+ 测试 test_recovery.py、test_file_ops_folder.py:62）——**全部重定向到真实模块后方可删 service.py**。根 shim `server/vault/__init__.py` 的 `from .service import *` 改为从真实模块直接 re-export：12 公开名 + **26 个 test-only 唯一名（总面 38 名）**一个不能少（test_db.py ~40 处走 init_db 等）。**新增 shim 面钉住测试**：显式 38 名列表对 `dir(server.vault)` 断言（「shim 面不回归」不可 grep，必须落成测试）。DeprecationWarning 保留。根 shim docstring 指向将删的 service.py——同步改。
6. **测试**：`test_inapp_hook_holds_db_lock`（现状锁探测很弱，:217 只断言测试线程能拿到锁）改行为断言（notify 后 notifications 表恰一行 + 归属正确）；新增：trigger_state/notifications 持锁函数单测、**多线程 cap 竞态测试**（≥8 线程并发 notify、cap 在边界 → 普通行 ≤ cap、恰一条 collapsed 行——notify 域「并发规则不可违反」的唯一真正钉住）、**card 3 两条 grep 加入本卡验证**（`proposal_transitions` 在 routes/memory.py+api.py 零输出；`FROM proposals` 在 routes/memory.py 零输出——写 SQL 下沉不得破坏 card 3 验收）、proposal_records/snapshot_records 复合操作由既有测试兜底。
7. **文档**：vault/db/README.md 并发段反转（「所有写路径持锁」→「vault.db mutation functions 内部持锁；`_db_lock` 包外不可 import」）+ 文件表补新模块 + 现状滞后项；server/README.md:94 同步；AGENTS.md 明确覆盖两处：:26 db 模块清单（补 recall/relatedness/trigger_state/notifications/proposal_records/snapshot_records）+ :44 shim gotcha（「re-exports server.vault.service」将变假）；proposals.py:413-415「schema 只被 proposals 认识」注释**记录 rescope**（card 5 后写 SQL 被两个 module 认识——沿锁边界切分：proposals.py 读面+状态机，vault.db 写面持锁）。

## 验收（可验证化）

- [ ] 并发规则不可违反：server/vault 外 grep 标识符 `_db_lock` 零出现；例外清单（注明）：init_db 的 DDL 语境、recovery._rebuild_files_table 裸 `upsert_file`（进程启动单线程 recovery drill）
- [ ] bgagent 不再伸进 vault 内部：scheduler/notify/executor 零 `vault.db.schema`/`_db_lock` import；**server/vault 内零 bgagent 运行时 import**
- [ ] 裸 SQL 全部有名：bgagent `conn.execute` 归零（全部在 vault.db 具名函数内）；bgagent 零 `with _db_lock`
- [ ] facade 深化或死亡：service.py 删除；38 名 shim 面钉住测试绿
- [ ] cap 软→硬行为变更有测试 + commit message 明示
- [ ] 714 基线不回归（新测试只增不减）

## 边界（不做）

- 不动 DDL（init_db 全表 + 4 个 _ensure_* 迁移留 schema.py）。
- 不动 events / ledger / 路由面 / agent tools 逻辑（工具文件仅 import 重定向）。
- `get_changes_sources` 读包锁的既有不一致**不顺带改**（行为面，另开项）。
- refs._update_references_batch 下划线跨包 import（routes/console.py:91、agent/tools/move_docs.py:78 锁外裸调）：**pre-existing、out-of-scope**，plan 记录待后续改名/收编卡，防未来评审误判为 card 5 回归。
- apply_change 锁内磁盘 I/O（read_bytes/rglob 阻塞写者）：立为独立性能卡，本卡不动。
- v2.2 debt #1（proposals 与 file ledger 隔离）不在本卡。

## 执行

单 fixer lane，顺序：新域模块（4 个）→ 调用方迁移（bgagent → executor → proposals/snapshots）→ facade 退役（9 模块重定向 → 删 service.py → shim 重写 → 38 名钉住测试）→ 测试改写/新增（含多线程竞态）→ 文档（README×2、AGENTS×2、proposals 注释 rescope）。
验证（fixer 自跑，我复核）：全量 pytest + 验收 grep 全集 + card 3 两条 grep + 冒烟（config.yaml 存在则起服 8s）。
