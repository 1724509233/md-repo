# 双账本文件隔离：权威状态与可重建缓存分文件（L4）

**日期：2026-10-31 · 状态：已实施（2026-10-31 用户批准 A 案，5 步实施序落地并全量验证：735 passed + 1 预存失败；收官文档 = db/README 并发节 + AGENTS.md + backlog L4 注记 + v2.1 spec §二分层表回写）**

**文档定位**：架构评审 backlog（`docs/superpowers/plans/2026-10-29-arch-review-leftovers.md` L4）的实施 spec。基于 2026-10-31 @oracle 裁决（会话 `ses_e736f16dfffe7NWNul4egigHix`，A 方案胜出 + 范围扩至四表）。上游宪法 = `2026-10-27-reversible-automation-design-v2.1.md`：其 §二分层表（:35）早把「提案记录表 + relatedness 表 + 快照索引」划进账本层——本 spec 不发明新边界，只把那条线从「一个文件里的三张表」兑现成「一个独立的文件」。card 5（`2026-10-28-card5-db-lock.md` 系）沿锁边界切了写面，逻辑隔离没动，本 spec 补上文件这一层。

---

## 一、问题：产品唯一的恢复程序对权威状态是假的

`proposals`/`proposal_transitions`（提案状态账本）与 `change_log`/`files`/`segments` 等（文件账本）共享同一 SQLite 文件 `.sync/index.db`、同一连接、同一把非重入 `_db_lock`。落档表述是「seam 是并发原语而非接口」；代码核实后**主矛盾是生命周期耦合，不是并发**——并发是派生的（一个文件 → 一个 conn → 一把锁，文件一拆它作为副产品消失）。

数据的正确分类：

| 类 | 表 | 重建来源 |
|---|---|---|
| **可重建投影**（缓存） | files / segments / edges / entities / change_log / trigger_state / recall_log / notifications / sessions | vault 磁盘重灌，或已声明 accepted loss / 易失 |
| **权威状态**（不可重建） | `proposals` / `proposal_transitions` / `snapshots` / `relatedness` | **无**——行没了就没了 |

这个分类今天只活在注释里。三个具体后果（代码核实）：

1. **recovery drill 对权威状态失明**。`server/recovery.py:63` `_wipe_db` 删整个 DB；:162 重建 = `init_db` + `ensure_indexed` + files 重灌；:179 ok 判据 = `files_match and fts_ok`。报告措辞对 change_log 说 "accepted, documented loss"（:184）、对 trigger_state/recall_log 说 "acceptable telemetry-cache loss"（:188）——**四张权威表没有任何重建来源，却被同一把 wipe 抹掉，报告仍 `ok: true`**。
2. **更常见的灭失路径甚至不用跑 drill**：index.db 丢失/损坏 → server 重启走 `main.py:160-173`（`init_db` 新建空 DB + `ensure_indexed` + reconcile）→ 四表**静默为空**，无报错、无告警、无重建来源。drill 是这个产品**唯一成文的「index.db 丢了怎么办」程序**，它的判据认证的是一个保证丢失权威状态的恢复。
3. **drill CLI 对 live vault 无守卫**（`recovery.py:208-221` 接受任意 `vault_dir`）——活的 footgun。docstring（:14-16）说生产 drill 跑在临时副本上，那是建议不是强制。

**并发面（次矛盾，不独立修）**：L3 已量化（`docs/benchmarks/2026-10-30-apply-change-lock-hold-benchmark.md`）：真实规模（302 文件）`_db_lock` 持锁中位 0.4–3ms、max <6ms；跨域写者排队成本可忽略。本 spec 不为此做任何并发工程。

---

## 二、裁决：A 胜出，范围 = 四表同批

### 2.1 四表为何都必须进

| 表 | 权威性证据 |
|---|---|
| `proposals` / `proposal_transitions` | schema.py:231-236 明文 DB 行是提案状态 "single source of truth — and its only carrier"；v2a 零文件落地（D25，`relpath` 是合成寻址键，背后没有文件）。`proposal_transitions` 是 ask-gate 人决策（adopt/reject 走 permission=ask）的审计凭证，invariant 11 = 条件 UPDATE + rowcount 判定——凭证的用途是事后举证「人当时决定了什么」 |
| `snapshots` | `snapshots.py:151` `blob_name = uuid4().hex`——blob 文件名是随机 UUID，call_id→path→op→pre/post hash 映射**只存在于行里**。行灭 = 回滚单元失效、blob 变无地址孤儿、`gc_sweep` 查表停摆。快照库 = 全系统唯一内容凭据（v2 Q1），凭据的索引本身不可重建 |
| `relatedness` | DB-only（spec §四：永不投影进用户文件），其中 `mark_ignored`（`relatedness.py:104`）= 人的「一键忽略」决策，一行 DB flag 而非文件变更；spec §5.3 忽略→遥测→回读链路依赖这些行 |

**同批而非分期**：四表共享同一缺陷类（无重建来源 + drill 静默灭失）、同一写面模块形态（card 5 具名持锁函数）、同一套接线/迁移/drill/fixture 足迹。分期 = 迁移机制跑两遍、drill 改造两遍、fixture 切换两遍，且分期窗口内 relatedness 的人决策仍在被静默灭——病只按表除了一半。

### 2.2 排除项（两低危表不进）

- `sessions`：30 分钟 TTL 逐出（`session.py:27`），by design 易失。
- `notifications`：30 天 retention GC（`notify.py:141`），易失 feed。

两者现状 drill 也静默丢失，但不构成「权威状态灭失」——drill 报告加一条 note 即可，不扩大搬移面。

### 2.3 备选裁决（oracle 2026-10-31）

- **B 同文件 + 显式 seam** —— 单独否决 / 被 A 吸收：card 5 已建过具名持锁函数这层 seam，L4 遗留的恰恰是 seam 之后的东西——两账本同住一个被当作可丢弃文件管理的文件。B 不动 wipe，drill 判据继续对权威状态说谎。B 唯一有价值的内容（接口化）是 A 的必然子集：两个 conn 本身就是接口。
- **C 恢复重建源（落盘投影）** —— 否决，三层理由：① 违宪：D25 是 10-27 联审用户裁决，spec 宪法修正案 #2 与 §2.1 第 2 条（suggestions/ 禁写守卫）+ `EXCLUDED_INDEX_DIRS` + SuggestionsGate 硬关闭构成三重结构防御，C 等于连拆三道；② 判例「介质跟着读者走」（spec:42）：文件副本唯一读者是 Obsidian，而 Obsidian 已被移出设计目标（grill Q10）；③ 工程更差：投影文件要自己的双写一致性、冲突、GC 故事（D25 删掉它的裁决原文）。另：`mcp_server.py:59-63` 已对外部 LLM 宣称 "proposal records live in the ledger only (no file)"——C 会让产品对外文档变成谎言。
- **D 接受 + 文档化（降级为 accepted loss）** —— 否决：① 与 invariant 11 的目的直接相悖，wipe 掉凭证 = 举证能力归零；② 决策记录 ≠ 遥测，把决策降格为遥测来套用 accepted-loss 措辞是改定义不解决问题；③ 现状下 D 不是「声明的例外」而是「静默的默认」（§一.3 的 footgun）——没有人声明过这个损失。

### 2.4 接受的风险（两项，不构成否决）

1. **per-machine 迁移不可集中验证**：每台机器各自 first-boot 迁移，无中心检查点确认「所有机器迁完」。缓解：迁移函数在「未迁」状态（源表仍在）时每次启动重试并打一条 warning 日志；日志留存（server.log 滚动）本身即审计面。
2. **备份口径变化**：A 落地后「备份 index.db」不再等于「备份系统状态」。本仓库现存没有成文的备份脚本，这是一条**面向运维的落档要求**（§5 step 5），不是代码改动。

---

## 三、设计

### 3.1 文件与锁

- **新文件** `.sync/ledger.db`（WAL，与 index.db 同惯例）。四表 DDL 从 `init_db` 移入新的 `init_ledger_db(path)`（同 schema.py 的 `_ensure_*` 迁移家族惯例，并**为 v2b① provenance 列预留 `_ensure_*` 槽位**——先有槽，后入住）。
- **新锁** `_ledger_lock` = `threading.Lock()`，非重入，与 `_db_lock` 同位。import 纪律照抄 card 5：**包外不可 import，具名函数是接口**（`test_vault_db_locks.py` 族测试照抄，见 §5 step 1 锚点）。
- **为何要锁**：「WAL 下无锁」只对单语句写者成立；card 5 的具名函数**全部是复合临界区**——`proposal_records.create`（INSERT→relpath UPDATE→transitions INSERT，:52-78）、`advance`（条件 UPDATE + transitions INSERT，:98-114）、`revive_rearm`、`gc_sweep`（SELECT+DELETE 一锁）、`relatedness.upsert_edge`/`merge_edge`（读-判-写，:88-95）。跨语句原子性靠进程内锁；WAL 只仲裁**跨进程**单写者（connect 默认 5s busy timeout + 条件 UPDATE 模式——spec §5.2 原话就是按这个设计的）。
- **锁域收窄是收益**：`_db_lock` 只管文件账本，`_ledger_lock` 只管四表；跨域写者从此互不排队（L3 量化的跨域竞争在跨域方向归零）。同域内竞争格局不变（ledger 写者低频：propose 在 bgagent run 内、adopt/reject 是人操作、capture 随工具调用）。
- **并发模型不变**：ledger conn **刻意沿用现有单例惯例**（一处 init、一处注册、一个关闭点），不引入新生命周期习语。L5-prod（每线程连接）仍是独立卡片，到那时**两个 conn 一起迁**——单例对是它的迁移单位。L4 不改 `test_proposals_advance_race.py` 的共享 fixture 形态（它变成 ledger fixture，仍是单条共享 conn）；「不要静音此测试」的 docstring 语义保留，加一行指针说明它现在钉的是 ledger conn 的跨线程暴露（范围更小、更纯）。

### 3.2 接线（conn 对）

`server/vault/db/conn.py` 从 26 行单例（`init`/`get_conn`）扩为对：`init(index_conn, ledger_conn)` / `get_index_conn()` / `get_ledger_conn()`，docstring 写明**这对共享一个生命周期**（L5-prod 迁移单位）。

| 位置 | 改动 |
|---|---|
| `server/vault/db/schema.py` | `init_db` 删 4 段 DDL（proposals :239 / proposal_transitions :267 / snapshots :294 / relatedness 及各自索引）；新增 `init_ledger_db(path)` + `_ledger_lock` |
| `server/vault/db/conn.py` | 单例 → 对（形状与生命周期注释如上） |
| `server/main.py:160-173` | 建两文件 → 两 conn → 一次性迁移（§3.3）→ 注册对 → `create_app(..., ledger_conn=...)` → `build_runtime(..., ledger)` |
| `server/mcp_server.py:178-183` | 同上（MCP stdio 是独立进程，自己的对） |
| `server/routes/__init__.py` | `WebContext` 加 `ledger_conn` 字段 |
| `server/proposals.py` | 写面调用换 ledger conn；读面（record/scan）换 conn 参数语义；**`list_trails`/`trail`（:413-434）= 全代码库唯一的双域查询**（同读 proposal_transitions 与 change_log），拆成两 conn 显式——这是「seam 从并发原语变成接口」的落点 |
| `server/routes/memory.py` | 提案域读写（adopt/reject/revive/trail/rollback/ignore）走 `ctx.ledger_conn`；change_log 脉冲查询（:391-398，executor 活动面）保持 `ctx.db_conn` |
| `server/routes/console.py` | 删除路径的 capture/finalize（:127/:146 域）走 ledger conn |
| agent 工具面（~15 处 `get_conn()`） | **按触表定域**：proposals/snapshots/relatedness 触表 → `get_ledger_conn()`，其余 → `get_index_conn()`（propose/adopt/reject/get/list_proposals、write_doc 等工具的 capture、runner readback/records_by_run） |
| `server/bgagent/executor.py:394` | `run_executor(vault_dir, conn)` 的 conn = **ledger conn**（scan/advance/expire_overdue/capture/finalize/gc/merge_edge 全是 ledger 域；executor 本体不碰 change_log——文件写入走 `ledger.register` 无 conn，watcher 消费走 index conn） |
| `server/bgagent/scheduler.py` | `_executor_loop` 传 ledger conn；`_armed_loop` 维持 index conn（trigger_state/change_log） |
| `tests/conftest.py` | `db_conn` 保持（index 域）；新增 `ledger_conn` fixture；autouse 隔离 fixture 重置对的两个 |
| `tests/functional/seed.py` + `run_sentinel_once.py`/`run_executor_once.py` | 开 ledger 库种提案 / 跑 executor |

**测试影响面**（单 conn 假设的依赖）：真正要动 fixture 的只是**四表域的读写**——`test_proposals*.py`、`test_snapshots*.py`、`test_relatedness.py`、`test_executor*.py`（双域：proposals 状态 + change_log 断言）、`test_memory_api.py`/`test_final_review_tools.py`/`test_invariants_v2.py`（跨域 trail/rollback 路径）、`test_recovery.py`（新判据）、`test_main.py`（启动路径加 ledger）。纯文件账本域的大头（test_db.py 的 60 处 `init_db`、test_ledger.py、test_reconcile.py、test_fts_sync.py）只受「index.db 不再建那 4 张表」的 DDL 断言影响，fixture 形状不动。

### 3.3 存量迁移（first-boot 启动路径内，ATTACH 单连接原子搬移）

`init_db` 与 `init_ledger_db` 都执行后，main.py / mcp_server.py 各调一个具名迁移函数。单条 index conn 上 `ATTACH DATABASE '<sync>/ledger.db' AS ledger`，**同一事务内**逐表搬移（SQLite 允许一条连接跨 attached DB 开单事务 → 搬移原子）：

| 分支 | 条件 | 动作 |
|---|---|---|
| ① 未迁 | 源表在 main 且目标表空 | `INSERT INTO ledger.t SELECT * FROM main.t` + `DROP TABLE main.t` |
| ② crash 窗口 | 源表在 main 且目标表**非空**（上次迁移 crash 在 INSERT 与 DROP 之间） | 两表都留、不丢数据、surface 告警（人工介入） |
| ③ 已迁/全新 | 源表不在 main | no-op |

幂等，与 schema.py 既有 `_ensure_*` 家族同一惯例；分支②每次启动重试并打 warning（§2.4-1 的缓解）。

**否决一次性脚本**：`.sync/` 不参与客户端同步（`client/sync.py:77-85` 扫描跳过；客户端自己的 `.sync/` 只有 `state.json` + logs）——每台机器各自持有自己的 ledger.db，**升级是每机器各一次的首启事件**。脚本要每机器手跑一次，不符合无 auth、LAN、自启的产品形态；启动路径 first-boot 步骤自动覆盖「以后才上线的机器」。客户端视角零影响（从不读 `.sync/`）。

**双进程并发首启**（main + MCP 同时起）：WAL 单写者仲裁（现状 index.db 已是这个模式），不坏数据——step 3 锚点之一。

### 3.4 Recovery drill 改造

1. **备份**：两个文件 + 各自 wal/shm sidecar。
2. **wipe**：只删 index.db 三件套；`_wipe_db` 不再接受/触碰 ledger.db。
3. **重建**：index 侧照旧（`init_db` + `ensure_indexed` + `_rebuild_files_table`）；**ledger 侧零重建——这是判据本身**。
4. **报告新增**：`ledger: {file, preserved: bool, rows_before: {proposals, proposal_transitions, snapshots, relatedness}, rows_after: {...}}`；`ok = files_match and fts_ok and ledger.preserved and rows_before == rows_after`。
5. **notes 措辞**：index.db = 可重建缓存（现有三条 loss 注记保留）；**ledger.db = 权威状态，drill 永不触碰——若 ledger.db 灭失，那是数据灭失事件，不是本程序的范围**。
6. **新测试**（test_recovery.py 增补）：
   - 预置四表行的 vault 跑 drill → 断言行数逐表保留、`ledger.preserved is True`；
   - **「ledger.db 不存在」的降级路径**（pre-A vault 首次 drill）：报告 note 而非 ok 翻转——判据区分「无 ledger 文件 = 无状态可保」与「有 ledger 文件 = 必须逐行保留」。

### 3.5 v2b 交互与排序（**L4 先，v2b 后**）

v2b 九项 × L4 搬移面碰撞：

| v2b 项 | 碰搬移的表？ | L4 先做的后果 |
|---|---|---|
| ① provenance 盖章（词条级进账本记录） | **proposals 加列** | 在 ledger.db 上跑一个 `_ensure_*` 加列，落点干净；若 v2b 先做，该列长在 index.db 里，L4 迁移 + 该列全部测试随迁 |
| ② relatedness → 排序器 + ignored 机械过滤 | **relatedness** | 接线代码落在 ledger 域，读写面不重排 |
| ⑤ 整批回滚（严格逆序） | **snapshots** | 回滚入口建立在稳定索引上 |
| ⑦ 机器区快照默认关配置 | **snapshots** + config | capture 路径按域分流已就位，加开关是局部改动 |
| ③④⑥⑧⑨ | 文件区/工具面/配置/notifications（index 域） | 不碰 |

**序的判断**：L4 是一次性边界搬移（fixture/conn/drill 重排付一次）；v2b 是九项功能，其中四项的写域 = L4 搬移面。L4 先 → v2b 在稳定边界上开发，回归网（ledger 逐行保留判据）从第一行 v2b 代码起就罩着账本。v2b 先 → v2b 的整套测试写在一个即将换 fixture 的形状上，L4 的 diff 要吞掉 v2b 新代码，**双付成本**。

**协调点**：L4 落地时若 v2b ①/⑤/⑦ 任何一项已开工，其分支 rebase 到 L4 之上即可——L4 零端点改动、零 API 形状改动，只换 conn 的来源，无功能级冲突。

---

## 四、实施序（5 步 + 验证锚点）

1. **Step 1 — 域定义 + DDL 分家**：`init_ledger_db` + `_ledger_lock`；`init_db` 删 4 段 DDL；`proposal_records`/`snapshot_records`/`relatedness` 换锁换 import；`proposals.py`/`snapshots.py` 读面换 conn 参数语义。
   *锚点*：`test_vault_db_locks.py` 族 + `test_proposals.py`/`test_snapshots.py`/`test_relatedness.py` 全绿（换 ledger fixture）；grep 断言 `_db_lock` 不再被 ledger 域模块 import、`_ledger_lock` 不出现在包外（照抄 card 5 的 import 纪律测试）。
2. **Step 2 — 接线对**：`conn.py` 对形状；main.py/mcp_server.py 双 init + 注册对；WebContext.ledger_conn；executor/scheduler 传 ledger conn；agent 工具面按触表定域；conftest `ledger_conn` fixture + 隔离 fixture。
   *锚点*：`test_main.py`（启动路径）、`test_runtime.py`、`test_bgagent_*` 全绿；L5 金丝雀测试（test_proposals_advance_race）在 ledger fixture 上行为不变（「不要静音」docstring 加一行指针）。
3. **Step 3 — 存量迁移**：ATTACH 搬移函数 + 幂等三分支 + main/mcp 两处调用。
   *锚点*：新迁移测试——旧 DB（四表有行）首启 → 行逐表等价、源表 DROP、`proposal_transitions.proposal_id` 无孤儿；二次启动 no-op；crash 窗口分支（源在 + 目标非空）→ 不丢数据 + 告警；**双进程并发首启**不坏数据（WAL 单写者）。
4. **Step 4 — drill 改造**：双备份、单 wipe、ledger 逐行保留判据、报告字段 + notes 措辞。
   *锚点*：`test_recovery.py` 新用例（§3.4-6）；CLI smoke（test_recovery_cli_main）报告形状更新；**行为不变量：drill 后 `ok: true` 且四表行数 100% 保留**（= L4 验收标准本身）。
5. **Step 5 — 文档 + 收官**：`server/vault/db/README.md` 并发节改双锁/双 conn 形态；AGENTS.md Gotchas 补 ledger.db 条目（「两个 SQLite 文件：index.db 可重建，ledger.db 权威不可重建——drill 只碰前者」）；backlog L4 卡片落「已裁决关闭」注记（照 L3 格式）；v2.1 spec §二分层表回写一行（账本层 = 两文件：index.db 缓存 + ledger.db 权威）；运维落档：备份口径必须含 ledger.db（§2.4-2）。
   *锚点*：全量 `pytest tests/ -v`（基线 727 passed + 已知 1 预存失败 `test_config_legal_layer::test_real_config_yaml_v2_migration` 不变）；L5 金丝雀按现状（保留可能的失败，不静音）。

---

## 五、验收标准

1. **drill 验收 = L4 验收标准**：预置四表行 → drill 后 `ok: true` 且 `rows_before == rows_after`（逐表）、`ledger.preserved is True`；ledger.db 全程未被触碰。
2. **权威状态静默灭失不再可能**：index.db 删除/损坏后重启，四表状态从 ledger.db 完整恢复（迁移/重建路径不依赖 index.db 幸存）；ledger.db 灭失有明确告警语义（数据灭失事件，不是静默重建）。
3. **import 纪律**：`_db_lock` 不被 ledger 域模块 import；`_ledger_lock` 不出现在 `server/vault/db` 包外（测试断言）。
4. **零 API/端点形状改动**：Web API、MCP 工具面、控制台行为对外不变（只换 conn 来源）。
5. **测试基线**：727 passed + 1 已知预存失败不变；L5 金丝雀不静音。

## 六、非目标

- 并发模型改造：L5-prod（每线程连接）仍是独立卡片，届时两个 conn 一起迁。
- `sessions`/`notifications` 的保留策略：维持现状（易失），drill 报告 note 即可。
- 备份脚本：本仓库现存没有成文备份机制；本 spec 只落档口径要求，不新建脚本。
- 跨机器迁移状态的中心化检查：接受 §2.4-1 的 per-machine 语义（日志为审计面）。
