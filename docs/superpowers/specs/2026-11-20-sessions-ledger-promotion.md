# spec：sessions 表升格 ledger.db（会话成为权威数据）

日期 2026-11-20 ｜ 状态：草案 r2（oracle 评审已并入，待用户终审）｜ 缘起：通道二 spec 评审讨论——「会话原始记录是未来记忆系统的兜底底座，躺在可重建的 index.db 是坑」｜ 评审法源：oracle 独立评审（C1/H1/H2/M1-M3/L1 全并入）

## 1. 背景与决策

sessions 表（id, messages blob, token_count, last_active, created_at, auto_approve + ALTER 列）今天住 index.db——「可重建缓存」语义，恢复演练随便清。通道一后 messages 已是对话真相的一部分；通道二消费要 join 会话行；未来记忆系统若以会话原始记录为兜底底座，index.db 定位是方向性错误。

**决策（2026-11-20 用户定）**：sessions 整表迁入 ledger.db；**迁移先于通道二实施**。

不做非目标：不改 messages 结构、不做行级化、不引入会话 FTS。

## 2. 迁移机制

### 2.0 前置：index DDL 摘除（评审 C1，违者必炸）

`ensure_index_tables`（`schema.py:412-424`，经 `init_db` 每次启动每个入口都跑）现**无条件** `CREATE TABLE IF NOT EXISTS sessions` + `_ensure_session_cols`。**不摘除则迁移后每次启动在 index.db 重建空 zombie 表**：幂等前提恒假 → 行数校验恒不等 → 永久启动失败环。

- 从 `ensure_index_tables` 移除 sessions CREATE 与 `_ensure_session_cols` 调用；DDL（含列定义）整体移入 `_LEDGER_DDL`。
- promote 函数**必须容忍 zombie**：升级机器上 `init_db` 恰在 migrate 之前刚可能建过空 sessions 表——空 zombie 直接 RENAME 让名，非空 zombie 走 §2.2 非空分支。
- 测试钉：迁移后重启 N 轮，index.db 永不再现 `sessions` 名（DDL 摘除根钉）。

### 2.1 搬迁步骤（并入 migrate_ledger 先例）

入口并入 `server/vault/db/migrate_ledger.py`（`promote_sessions`），站位同 `migrate_ledger`（启动路径、双 conn 后、服务前；MCP 进程同样调用 `migrate_ledger`——**MCP 不读 sessions 但参与迁移**，`BEGIN IMMEDIATE` 排队天然覆盖并发面）：

1. ledger 侧建表：`_LEDGER_DDL` 含 sessions + 索引；`_ensure_session_cols` 改跑在 ledger 表上（ALTER 补齐）。
2. **显式列名拷贝**（评审 M2：旧表 = 5 列 CREATE + ALTER 追加 auto_approve，`SELECT *` 位置耦合禁止）：`INSERT INTO sessions (id, messages, token_count, last_active, created_at, auto_approve) SELECT <同列> FROM index.sessions`。
3. 行数校验 + 裁决，见 §2.2。
4. 源表 `RENAME TO sessions_pre_ledger_migration`（藏表不删，留人工清理；回滚 = 改回名）。

### 2.2 非空分支裁决（评审 H1，与先例 kept-both 对齐）

源空/目标空（常规首迁）：搬迁 + 行数相等校验，不等 → 整体回滚 + 启动失败可见（拒绝带疑迁移）。
**目标非空**（半态残留/回滚后再升级，同 id 行内容可能已分叉）：**禁止 `INSERT OR IGNORE` 静默吞冲突**——照 kept-both 先例：**警告 + 跳过 sessions 迁移 + 继续启动**，日志明示两侧行数与路径，人工裁决哪份为准（人工清目标表即重触发搬迁）。测试必含「同 PK 异内容」用例。

### 2.3 幂等

常规完成态重跑 = 各步空操作（源名已让出、zombie 不存在或为空直接改名）。

## 3. 接线面全集（评审 H2 补全；`routes/agent.py` 实测）

真正的总开关是 **`:127` `SessionManager(ctx.db_conn, ...)` 构造点**；其余 `ctx.db_conn` 触点全集：

| 位置 | 用途 |
|---|---|
| `:127` | SessionManager 构造（换 conn 主开关） |
| `:149` | `get_auto_approve`（agent 重建回填） |
| `:160-162` | `_load_messages`——**snapshot 帧前缀读**（RunHub 铁律②数据源） |
| `:182-194` | persist / persist_user 闭包 |
| `:359` / `:375` | auto_approve GET/SET 路由 |
| `:415-424` | feedback 读侧 `load_session` |
| `:462` | 会话列表路由（含 running 注入） |
| `:475` | GET 单会话路由 |
| `:499` | DELETE → discard |

`_evict_stale_agents`（runhub.py:28-67）实测不碰 DB，无需改。机制上 grep 审计兜底，但按此清单点名施工。`create_app` 的 `ledger_conn=None → 回退 db_conn`（`api.py:67`）使静态钉只能钉「session 函数调用点用 `ctx.ledger_conn`」，钉不住「无 db_conn 参数」——接受此局限，靠根钉 3 兜底。兼容性：`init_ledger_db` 为 `isolation_level=None`+WAL+`sqlite3.Row`，sessions.py 下标访问+`commit()` no-op 全部兼容。

## 4. 恢复演练面同步（评审 M3）

- `recovery.py:92` `LEDGER_TABLES` 保全行数核查 **+sessions**（通道二落地后再 +chat_abort_log——「权威不可重建」新表族必须在演练核查内）。
- `recovery.py:309-318`「sessions empty is expected (acceptable cache loss)」文案改假为真：删 index.db 后 sessions 必须存活且有行数核查钉。
- 测试清单含此两钉。

## 5. 测试清单（TDD）

1. 迁移：有数据搬迁行数一致；幂等重跑空操作；「同 PK 异内容」→ kept-both 拒绝 + 警告不炸；`_ensure_session_cols` 新列在 ledger 表生效；**zombie 容忍**（迁移前 index 里现空/非空 zombie 两态）。
2. DDL 摘除根钉：迁移后多轮重启，index.db 永不再现 sessions 名。
3. 端到端不回归：chat→persist→snapshot 恢复→list→单会话→auto_approve 读写→delete（跑真 ledger_conn）。
4. 恢复演练根钉：模拟删 index.db 重建 → 会话存活 + `LEDGER_TABLES` 核查含 sessions。
5. 审计静态钉：§3 清单各点用 `ctx.ledger_conn`；无 index conn 残留。
6. 通道一/feedback/runhub 既有测试全绿。

## 6. 风险与回滚

- **回滚窗口（评审 M1）**：`sessions_pre_ledger_migration` 住 index.db，而恢复演练设计就是清 index.db——「两侧都留完整副本」仅在**首次演练前 + ledger 侧无新写**窗口内成立；**演练即宣告回滚面关闭**。§2/§5 措辞以此为准。
- 迁移失败最坏态：启动失败 + 日志明示，人工介入；迁移单事务无半态。
- MCP 并发：与先例同站位，`BEGIN IMMEDIATE` 排队覆盖。

## 7. 实施顺手项（评审 L1）

- `main.py:193` `init_db(db_path)` 不带 busy_timeout（MCP 侧带 5000ms）——主进程迁移撞 MCP 持锁秒败的既有不对称，顺手补齐。
- AGENTS.md 两库描述同步：权威文件族 +sessions（恢复演练口径）。

## 8. 依赖关系

本卡落地 → 通道二 spec（`2026-11-20-stop-signals-reasoning-capture.md`）实施；通道二消费 join 与半截行读取全程单库（ledger.db）。
