# 架构评审 2026-10-28 遗留 backlog（6 卡全部落地后）

来源：`architecture-review-20261028-1711.html` 的 6 张卡已全部落地（cards 1/2/3/4/5/6，commit 见下），card 5 的侦察（explorer 持锁点全图）+ 过门（oracle 评审）+ 对账（验证阶段）把几个**一直存在**的角落照亮了，按「本卡边界外、登记待后续」处理。本清单 = 下一轮评审/债单的现成候选卡片。

落地 commit 索引：card1 `50dc8d8`+`ae09abd` · card2 `0ccd509`+`9a39cf5` · card3 `aa5a2d9`+`dfa2489` · card4 `60c7276` · card5 `64ce93d` · card6 `190521d`+`eb6d2f9`

---

## L1. refs 下划线跨包 import 收编

- **问题**：`server/vault/orchestration/refs.py` 的 `_update_references_batch` / `_remove_dead_links` 是下划线私有，却被包外两处**锁外**裸调：`server/routes/console.py:91`、`server/agent/tools/move_docs.py:63/78`。正是 card 5 痛斥的「私有 interface 被跨包当接口」模式（但无锁违规——DB 读 + FS 写，不持锁）。
- **出处**：explorer 持锁点全图（card 5 侦察）；oracle 过门发现 plan 漏登记，要求标 pre-existing/out-of-scope。
- **为何不在 card 5 做**：改名/收编是接口面变更，与锁的 import 面正交；card 5 边界节明确不动。
- **下轮形态**：公开命名（`update_references_batch`）或收进 vault.db 具名函数；调用方随接口走。小卡。

## L2. `get_changes_sources` / `count_changes_sources` 读包锁不一致

- **问题**：两个**纯读**函数（`server/vault/db/changes.py:74/:122`）的调用惯例不一致——scheduler 把它们包进 `with _db_lock`（现状：`_has_human_activity_since`、card 5 前的 `_armed_check`），而 recall/snapshots 域锁外直读。card 5 后 scheduler 侧已迁走（`trigger_state.arm_change` 锁内调 scan——scan 无锁，故安全），但「读到底要不要包锁」这条惯例仍无书面答案。
- **出处**：explorer 全图观察项 #2（原话「现状本身不一致」）。
- **为何不在 card 5 做**：行为面（改锁读语义可能引入竞争窗口变化），card 5 边界节明确「不顺带改，另开项」。
- **下轮形态**：书面定一条惯例（建议：读不持锁，写临界区内的读随写包锁——即 card 5 现状），写进 vault/db/README 并发段；grep 对齐调用点。文档卡 + 少量调用点。

## L3. `apply_change` 锁内磁盘 I/O（性能卡）

- **问题**：`server/vault/orchestration/file_ops.py` 的 `apply_change` 大临界区内做磁盘 I/O——`read_bytes`（moved 分支读旧 hash 上下文）+ refs 的 rglob 全库扫描（reference 重写）。锁持有期 = DB 事务期 + 磁盘扫描期，reference 重写会阻塞所有写者。
- **出处**：oracle 过门 point F（「既有问题，立独立性能卡，别顺手改」）。
- **为何不在 card 5 做**：拆它需要跨模块事务协调（五域：files/change_log/segments/graph/refs），超出锁具名化范围。
- **下轮形态**：锁内只做 DB 语句；磁盘 I/O 移到锁外（先读后锁），或把 refs 重写拆出为独立临界区。需先量：典型 vault 规模下锁持有时间分布。**性能卡，非正确性卡。**
- **已量化关闭（2026-10-30，用户裁决 A）**：四规模（300/1k/3k/10k）× 四 action 实测锁内磁盘 I/O（可复现脚本 + 数据 = `docs/benchmarks/2026-10-30-apply-change-lock-hold-benchmark.md`）。结论：真实规模（302 文件）锁持有 ≤4ms（最高频 created 路径 ~0.4ms，跨规模不变）；10k 文件 = deleted/moved 中位 14–17ms、max ~24ms（暖缓存；页缓存冷启动观测到一次性 261–440ms 尖峰，未再复现），驱动项 = 每次 move/delete 锁内 2–4 个 rglob 全库扫描 + referrer 读写。当前规模改动收益 ≈ 0、风险（五域复合操作拆分、TOCTOU）≫ 收益。**重开条件：vault > 5k 文件或观测到写者排队**；重开策略 = 先做 B1（锁内 rglob → `files` 表查询，3 处调用点）再评估全文形态。

## L4. 提案状态账本 与 文件账本 的隔离（v2.2 方向）

- **问题**：`proposals`/`proposal_transitions`（提案记录状态账本）与 `change_log`（文件账本）共享同一连接与同一把锁——它们之间的 seam 是一个并发原语，不是接口。card 5 沿锁边界切了写面（`vault/db/proposal_records.py` 持锁、`proposals.py` 读面+状态机），但两账本的**逻辑隔离**没动。
- **出处**：评审 spec card 5 的 amber 注（2026-10-28 评审原文）；关联 v2.2 debt #1（conditional UPDATE，已落地为 `proposal_records.advance` 的 rowcount 判定，invariant 11）。
- **为何不在 card 5 做**：amber 注定位它是「正在朝隔离它们的方向走」的战略方向，不是本卡的验收项。
- **已裁决落地（2026-10-31，oracle 裁决 A 方案，spec 先行）**：主矛盾经 oracle 裁决重定义为**生命周期耦合而非并发**——产品唯一的恢复程序（recovery drill）对权威状态是假的：index.db 丢失/损坏 → 重启 `init_db` 新建空库，四表（proposals / proposal_transitions / snapshots / relatedness——权威、无重建来源）静默清空、无告警，drill 的 ok 判据（files+FTS）检测不到；drill CLI 对 live vault 无守卫。落地 = 四表同批迁 `.sync/ledger.db`（独立 conn + 独立非重入 `_ledger_lock`，WAL 仲裁跨进程），first-boot 启动路径内 ATTACH 单事务原子迁移（幂等三分支），drill 改造（双备份、单 wipe、四表逐行保留 = 验收标准），并发矛盾作为副产品归零。B（同文件+seam）被 A 吸收；C（落盘投影）违宪（D25 三重守卫）；D（accepted loss）灭 ask-gate 审计凭证。v2b 碰撞矩阵：①②⑤⑦ 的写域 = 四表 → **L4 先 v2b 后**。实施与验收：`docs/superpowers/specs/2026-10-31-ledger-file-split-design.md`（5 步实施序 + 锚点全绿；全量 pytest 735 passed + 1 预存失败）。L5 交互：ledger conn 沿用单例惯例，L5-prod 届时两个 conn 一起迁；金丝雀不静音。运维口径：备份必须含 ledger.db（AGENTS.md 已落）。

## L5. 并发测试卫生：`test_proposals_advance_race.py` 偶发线程异常

- **问题**：`tests/test_proposals_advance_race.py::test_concurrent_adopt_exactly_one_wins` 的 worker 线程偶发 `sqlite3.InterfaceError` → `PytestUnhandledThreadExceptionWarning`。根因方向：并发测试让 worker 线程与主线程共享同一 sqlite 连接，而共享连接的跨线程使用本身依赖 `_db_lock` 纪律——测试没走具名持锁函数时就会踩。
- **出处**：card 5 fixer 终跑报告（fixer 称在基线 worktree 5/5 复现）；**本会话 7 次验证未复现**（5 次单跑 + 2 次全量，2026-10-29）——调度依赖的 flake，但测试仍绿。
- **下轮形态**：worker 线程改走 `proposal_records.advance`（具名持锁）而非裸 conn 操作；或每线程独立连接（WAL 下可行）。**测试卫生卡，小。**

---

## 优先级建议

L5（小、钉住正确性）→ L1（小、接口面）→ L2（文档为主）→ L3（先量化再动）→ L4（需架构讨论，可并入下一轮评审的候选卡）。
