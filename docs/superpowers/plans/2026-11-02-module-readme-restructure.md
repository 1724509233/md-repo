# 模块 README 结构统一与更新 — 实施计划

> Todo: [docs] 项目各模块readme结构重新梳理并更新
> 日期: 2026-11-02 · 状态: **已完成（2026-11-03）** — 执行中用户指令「自行完成，不提交」覆盖第 4 节第 3 步的单 commit，全部改动留在工作区
> 用户决策（三轮问答确认）: 范围=现有 8 个更新+补写 4 个缺失（client/routes/config/tests）；根 README=重写；AGENTS.md=允许小幅同步；结构=统一七段式骨架；深度=深度摘要+vault 为详情追溯；易漂移数据=不钉死；验讫=自动检查+oracle 评审；提交=单 commit；**执行=串行派、分批次评审**。

---

## 1. 统一骨架（七段式）

所有模块 README 按同一节序组织，括号节可省略：

```
# <path> — <一句话定位>
（首段：职责 + 入口/启动方式）

## 模块结构        文件/目录表，职责一列
## 公共 API        函数/类级，签名从代码抄
## 关键设计 / 链路  图 + 不变量；深度论证只留摘要 + vault spec 链接
## 依赖方向        上游/下游，ASCII 图或表
## 注意事项        gotchas、纪律、惯例
## 使用示例        （可选）
```

规则：
1. **不钉易漂移数据**：测试数写 `pytest tests/ -v`，不写数字；代码引用只用 文件+符号（如 `ledger.register`），不用行号锚。
2. **深度摘要 + vault 追溯**：大段设计论证（锁纪律、ledger split 三层论证等）压成摘要，链接指向 vault spec（`~/md-vault/projects/md-repo/specs/…`）。
3. **vault 链接白名单**：只链接执行时确认存在的 spec 文件（执行第 0 步 `ls` vault specs 目录建白名单）；不存在的只写文字指引，不写链接。
4. **prompts/、templates/、agent/tools/ 不单独建 README**：各自一段说明并入上层 README（prompts→bgagent+agent；templates→根 README；tools→agent/README）。
5. 语言：中文叙述 + 代码标识符保留英文（沿用现有风格与 NOTES.md 惯例）。

---

## 2. 受影响文件（13 个）

### A. 根层

| 文件 | 动作 | 要点 |
|------|------|------|
| `README.md` | **重写** | ① 架构图改双账本：`index.db`（可重建缓存）+ `ledger.db`（权威四表：proposals/proposal_transitions/snapshots/relatedness），现图只画了单 index.db；② API 端点表对 `server/routes/` 六域**逐条重核**（现表 12 行，漏 notifications/recall/memory/proposals/folders 等，执行时从 routes 代码收集全量）；③ AI 工具表保持 15 个并与 agent/README 对齐（auto 9：read_doc/search_docs/list_files/get_related_docs/recent_changes/recall_stats/propose/list_proposals/get_proposal；ask 6：write_doc/edit_doc/delete_doc/move_docs/adopt_proposal/reject_proposal）；④ 功能清单补「提案通道」「后台 agent（五角色）」（执行时从代码确认）；⑤ 项目结构树补 client/ 的 `state.py`+`config.py`、server/ 的 `config/`+`model.py`+`snapshots.py`+`proposals.py`+`recovery.py`+`sync_lock.py`；⑥ MCP 章节：工具清单对 `mcp_server.py` 重核（NOTES.md 提及 `md-vault.propose` 日常路径，现 README 只列 5 个工具，疑似缺提案工具）；⑦ 测试数去钉死；⑧ 快速开始/待办/设计约束/技术栈保留并对代码核实（命令、extras：`[dev]`/`[mcp]`） |
| `AGENTS.md` | 小幅同步 | 仅修明确漂移：`574 tests` → 去数字；检查中发现的其他与代码矛盾处（如 Commands 段命令）逐条列清单修，不做结构性改动 |

### B. server 存量（6 个）

| 文件 | 现状 | 主要修正 |
|------|------|----------|
| `server/README.md` | **严重过时** | ① 顶层模块表重建：现表列 `config.py`（实为 `config/` 包 5 模块）、`write_registry.py`（实为 `ledger.py`）；补 `ledger.py`/`events.py`/`snapshots.py`/`proposals.py`/`recovery.py`/`sync_lock.py`/`model.py`/`routes/`/`prompts/`/`templates/`；② 依赖图与「文件变更链路」换 ledger 模型：writers `register()` 预注册 → watcher consumer `consume()` 记账；web 直写 `record()` 同步落行；文件夹 `record_folder()`；③ 「agent 9 个工具」→15；④ 运行时链路图三张保留重写（变更/对话/bgagent 调度，bgagent 补 cron+arming） |
| `server/agent/README.md` | 过时 | ① 「9 个工具」→15，工具表从 `tools/__init__.py` 注册顺序抄；② 模块结构表补 tools/ 一模块一工具布局（15 文件）；③ `write_registry` 两处引用→`ledger.register()`；④ 关键设计补 policy 三层（L1 schema/L2 语义边界/L3 gates+SuggestionsGate）摘要+链接 `2026-10-22-agent-permission-policy-design`；⑤ 依赖表对码重核（`conn.py` 现 API 是 `init(index_conn, ledger_conn)`/`get_index_conn()`/`get_ledger_conn()`，现 README 写的 `get_conn()` 疑似旧名） |
| `server/bgagent/README.md` | 过时 | ① 模块结构补 `executor.py`（确定性执行器，无 LLM，adopted 提案唯一落地者，宪法铁律 1）；② 调度章节补**事件 arming**（`db/trigger_state.py`：`arm_change`/`arm_recall`/`record_run`，cron+事件双驱动），链接 `2026-10-24-event-driven-scheduler-design`+`2026-10-11-bgagent-design`；③ 五角色清单（碎片整理 consolidator / 矛盾批注 proofreader / 执行器 executor / 召回诊断 sentinel / 历史回顾 narrator[生产禁用]，NOTES.md 2026-11-01 落地）；④ `write_registry`→`ledger.register`；⑤ 配置示例对 `config/bgagent.py` 现字段重核；⑥ 报告格式/通知机制保留核对 |
| `server/vault/README.md` | 较新，局部过时 | ① 「依赖的 server/ 模块」表 `server.write_registry`（已删）→`server.ledger`；② 「数据库表结构」拆 index.db 域 + ledger.db 域，补 `trigger_state`/`recall_log`/`notifications`/`proposals`/`proposal_transitions`/`snapshots`/`relatedness`（现表清单 9 张，缺 ledger 域全部）；③ 关键设计节对骨架重排，FTS/移动检测/refs/线程安全压成摘要+spec 链接 |
| `server/vault/db/README.md` | 基本最新 | ① 对骨架重排（内容保留）；② 「连接与并发」锁纪律全文压成摘要+链接 `2026-10-31-ledger-file-split-design`；③ 去行号锚（现文有 `changes.py:74/:122`、`trigger_state.py:103-121` 式引用）改 文件+符号；④ clarifying：本包同时承载 index 域与 ledger 域两域写面 |
| `server/vault/orchestration/README.md` | 部分过时 | ① 「调用方」节重写为 ledger 模型：`apply_change`/`apply_folder_change` 是 row mutators，**唯一外部 importer 是 `ledger.py`**（现文写 main.py/execute.py 直调，已不符）；`reconcile` 由 watcher 启动+周期兜底调用、`ensure_indexed` 由 main/mcp_server 共用；② 补 `apply_folder_change`（文件夹删除/dotfiles 特性）；③ `apply_change` 持锁描述对现码核实（现文「orchestration 持锁为 recall 先例」与 card-5 规则表述需对齐实际代码）；④ 关键设计摘要+spec 链接（`2026-10-10-startup-reconcile`、`2026-10-07-edges-based-ref-update`、`2026-10-01-file-change-log`） |

### C. 新增（4 个）

| 文件 | 内容 |
|------|------|
| `client/README.md` | 轮询客户端（`python -m client.main config.yaml`）；模块表 `main.py`/`config.py`/`state.py`/`sync.py`；同步链路：`scan_local_files`（SHA-256）→ POST `/api/sync`（`local_files` manifest）→ 服务端 `compare_files` 纯函数（无 IO）→ `to_pull`/`to_push`/`conflicts`（写 `.conflict-{device}.md`，本地版本不动）/`to_delete`（stale copy：recent_moves 改名检测）→ 更新 `state.json`；409 静默等下轮（SyncLock 300s 自愈）；注意事项：删除不同步（by design）、`write_bytes` Windows 换行、config 只读 `client:` 段；术语指向 GLOSSARY |
| `server/routes/README.md` | 6 域（console/notifications/sync/recall/memory/agent）各一模块 + `register(app, ctx)` 约定；`WebContext`（`__init__.py`）字段表：`db_conn`（index 域）/`ledger_conn`（ledger 域）/`bg_targets`/`sync_lock`/…（执行时从 dataclass 抄）；路由表顺序由 `tests/test_routes_order.py` 快照钉住；全量端点映射表（供根 README 引用对齐）；注意事项：`_safe_rel_path` 穿越防护、无认证 LAN 专用 |
| `server/config/README.md` | 单一 `config.yaml` 双 section 约定（server/ 与 client/ 各读各段）；模块表 `core`/`ai`/`log`/`notifications`/`bgagent` 各解析函数（执行时从代码抄）；注意事项：`background_agents` alias 唯一、target_dirs 不重叠、bgagent 独立 model 段必填不复用 `ai:` |
| `tests/README.md` | 运行方式（`pytest tests/ -v`，不钉数）；conftest fixtures 表：`vault_dir`/`db_conn`（index 域）/`ledger_conn`（ledger 域，2026-10-31 split）/`app`/`client`/`ai_config`；域划分按 test 文件分组（执行时 ls tests/ 收集）；注意事项：fixture 用 `write_bytes`（Windows 换行）、`SyncLock.locked_at` 直设注入、路由顺序快照测试、import 纪律源码扫描测试（`test_vault_shim.py`/`test_ledger_import_discipline.py`） |

---

## 3. 执行：串行 5 批，逐批评审

每批流程（批内文件由一个 fixer lane 完成）：

1. **派 fixer**（background subagent），brief 含：七段式骨架、该批文件清单与上节要点、事实核查要求（**只从代码写**：每个符号/签名/端点/字段先读码确认；无法确认的列入 brief 回传「待确认」清单，不许猜）、该批 vault 链接白名单、规则 1-5。
2. **我事实核查**：读产出文件，符号对码、链接解析、骨架齐全。
3. **自动检查**（脚本化 grep）：陈旧标识符扫描（`write_registry`、`service.py`、独立文件 `config.py`、「9 个工具」、旧名 `db.py`/`watcher.py`、钉死测试数）+ 相对链接全部解析 + 骨架节序检查。
4. **oracle 评审该批 diff**（subagent），意见回传 fixer 同 session 修复；修复后复查。
5. 批验收 → 下一批。

批次顺序（依赖方向：根定义模块口径 → 顶层索引 → 深度模块）：

| 批 | 文件 | 说明 |
|----|------|------|
| 1 | `README.md` + `AGENTS.md` | 根层口径先行（双账本图、端点表、15 工具表）；AGENTS.md 漂移清单随本批改 |
| 2 | `server/README.md` + `server/routes/README.md` + `server/config/README.md` | 顶层索引 + 两个新增顶层文档，同属 server 顶层写域 |
| 3 | `server/agent/README.md` + `server/bgagent/README.md` | agent 家族，共享 tools/ledger/arming 事实 |
| 4 | `server/vault/README.md` + `db` + `fs` + `orchestration` | vault 四件套（高度互链，同批保证一致） |
| 5 | `client/README.md` + `tests/README.md` | 独立周边 |

批与批之间：我核对批 1 建立的口径（模块描述、工具表、端点表）在后续批中被遵守；漂移当场修正。

## 4. 收尾

1. **全量交叉一致性 sweep**：根 README ↔ server/README ↔ AGENTS.md ↔ 各模块 README（工具数=15、ledger API 名、sync action 类型、routes 六域清单、双账本命名）。
2. **自动检查全量复跑**（13 文件）。
3. ~~**单 commit**~~（执行中被用户指令「自行完成，不提交」覆盖：全部改动保留在工作区，由用户自行提交）。
4. 交付：逐文件变更清单（每文件 1-3 行改动摘要）+ 自动检查结果 + oracle 各批评审结论 + 「待确认」遗留项（如有）。

## 5. 验讫标准

- [x] 13 文件全部对齐七段式骨架（⑦ 可省；根 README/AGENTS.md 按计划走各自结构，模块 11 文件全部具名 ②-⑥，db/fs 的「文件结构」已于 2026-11-03 统一为「模块结构」）
- [x] 全部提及符号/函数/端点在代码中 grep 存在（每批对码 + 修复轮复查）
- [x] 全部相对链接解析通过；vault 链接 15/15 文件确认存在
- [x] 陈旧标识符扫描零命中（清单：write_registry / service.py / 文件型 config.py / 「9 个工具」/ db.py / watcher.py / 钉死测试数 574、738、750；`service.py` 仅存「已删除」语境）
- [x] 根/server/AGENTS/模块 四向交叉一致（300s sync 锁、锁纪律 card-5 现行引用面、reconcile 双路径、FTS 四档、15 工具、MCP propose-only、双账本 11+4 基准句全绿）
- [x] 5 批 oracle 评审意见全部 dispositioned（①6 ②7 ③9 ④9 ⑤10，共 41 条）
- [x] 交付：逐文件变更清单 + oracle 各批结论 + 待确认遗留项（见第 8 节）；**未 commit**（用户指令）

## 6. 假设与风险

**假设**
- 纯文档改动，不动任何代码；发现代码文档性注释漂移**不顺手改**，记入清单。
- 本计划文件是 `docs/superpowers/plans/` 第 6 份（AGENTS.md 保留清单列了 5 份 2026-11-02 计划，新增一份为本次 todo 的计划，不删不动原有 5 份）。
- vault specs 白名单以执行时 `ls ~/md-vault/projects/md-repo/specs/` 为准（已确认存在：md-sync-design 09-27、agent-permission-policy 10-22、memory-runtime 10-21、ledger-file-split 10-31、event-driven-scheduler 10-24、bgagent 10-11、file-change-log 10-01、snapshot-watcher 10-07、edges-based-ref-update 10-07、startup-reconcile 10-10 等 25+ 份）。

**风险**
1. 新增 4 文档的内容幻觉 → 只从代码写 + 符号逐个验证 + 待确认清单机制。
2. 单 commit 13 文件 diff 大 → 交付逐文件变更清单缓解。
3. 批间口径漂移（工具表措辞、端点口径）→ 批 1 定口径 + 收尾全量 sweep。
4. 个别设计事实需对码才能写准（apply_change 持锁主体、mcp_server 工具清单、bgagent 配置字段、routes 全量端点）→ fixer brief 要求读码确认，不确认不写。

## 7. 执行第 0 步（开工前）

- `ls ~/md-vault/projects/md-repo/specs/` 建 vault 链接白名单
- 对码收集：routes 六域全量端点、mcp_server 工具清单、bgagent 配置字段、`conn.py` 现 API、`apply_change`/`apply_folder_change` 持锁与 importer 现状、client 同步回包字段、tests/ 文件清单
- 输出「事实底稿」给各批 fixer brief 使用

## 8. 收尾记录（2026-11-03）

**范围外辅助改动（2 处，已在交付报告标记，用户可回退）**
1. `tests/functional/config.example.fun.yaml` — consolidator 块补 `tools` 白名单 + `verbs: [summary]`（v2 法律层强制，缺省 = fail-open 禁止；不补则文档化的 `run_sentinel_once` 命令在首个 run 前崩溃），「当前 11 个工具」陈旧注释 → 15 名。按文档化 sed 命令端到端验证：3 agent 全过法律层（sentinel 6 只读 / consolidator 7+summary / executor 豁免）。
2. `server/config/README.md` 相对路径解析条 — 重写为三基准契约（服务端 `vault_dir` 相对 cwd；bgagent `target_dir`/`report_dir` 相对 `vault_dir`、`prompt` 相对 config 文件目录），ora-5 跨批发现的真实交叉冲突。

**待确认（遗留，不阻塞文档交付）**
1. `config.yaml` proofreader 块：注释「低频次纯 cron 巡查」↔ trigger 块（create/update ×3 / 6h）矛盾 — 需用户决策删 trigger 或改注释；README 按 trigger 块行为写。
2. 代码/配置注释漂移 5 处（按规则仅记录未改）：`agent/events.py` docstring 6 vs 实发 8 事件；`bgagent/notify.py` RunReport docstring 4 vs 6 状态；`graph_sql` frontier docstring（批量 vs 现行逐节点）；`client/config.py` L37 注释「mirrors the server-side resolution contract」（服务端实为 cwd 基准）；`run_sentinel_once.py` docstring 称 InAppHook 为生产 sink（生产用 GuardedNotificationHook）。
