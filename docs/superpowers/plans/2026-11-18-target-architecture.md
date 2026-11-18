# 目标目录结构与依赖规则（2026-11-18，已定稿进入执行）

状态：**冻结**（2026-11-18：用户批准目标树+6 规则+命名 `services/`/`core/`；Gate 0 对抗评审 A1–A8 逐条裁决并入 §7）。冻结后改动需复审再冻。执行中：Phase 0（卡 1a/1b/2/3）。

## 0. 基线核实（本文所有数字的出处）

以下均由本次评审在 HEAD 上实测，与任务书基线一致（个别数字差异为计数口径）：

- `server/` 根 16 个 `.py`：15 个实体模块 + `__init__.py`。基础设施（events/logrotate/sync_lock/snapshot_watcher）、领域服务（ledger/proposals/snapshots/recovery/sync/privacy/egress）、实体类型（model.py 的 `ModelConfig`，16 行）混放。
- `vault/db/` 之外裸 `.execute(` 实测 **69** 处（本节初稿曾写 68，逐模块清单漏了 `bgagent/executor.py:257`）：indexer 13、`agent/tools/recall_stats` 11–12、proposals 9–10、routes 合计 18（memory 6 / notifications 3 / egress 3 / agent 3 / sync 2 / recall 1）、snapshots 5、`ai/search` 4、recovery 3、file_ops 2、snapshot_watcher 1、mcp_server 1、`bgagent/executor` 1（后两组间 11/12 与 9/10 为口径差，棘轮基线以**逐处枚举**为准，模块计数仅参考）。R1 全口径（`+executescript/sqlite3.connect`）为 72 处——多出的 3 处 connect 是 recovery.py:100/:251、ai/search.py:89。A1 已裁：全口径入基线；两处 recovery connect 为长期基线（冷重建/drill 自持连接的合法例外，注释注明）；PRAGMA 随卡 8 下沉为 db 具名开连函数。（`db/schema.py` 自身 52 处、`migrate_ledger` 11 处在 db 包内，属合法驻地。）
- `file_ops.py:19` 顶层 `from ..db.schema import _ACTION_TO_OP, _db_lock`（跨包私有符号）；`file_ops.py:194-195` 函数体内懒取 `db.conn.get_ledger_conn()` 跨库写 relatedness —— 确认属实。
- `db.conn.get_index_conn/get_ledger_conn` 懒取遍布 24 个文件（agent/tools 全家、bgagent、reminders/runtime、file_ops、mcp_server、main）。口径 = 25 个命中文件减去 `db/conn.py` 定义处，基线对账按此口径。白名单今天已含未来 services 候选 `snapshots.py`（:400/:548/:564）与 orchestration 的 `file_ops.py`（:194-197）——A3 已裁：冻结基线名单只缩不涨。
- **新发现 1（包级环）**：`vault/orchestration/trash.py:37` `from ... import ledger`，而 `services(ledger) → orchestration.file_ops` —— orchestration 与未来的 services 之间存在包级双向依赖，今天靠导入时机才没炸。
- **新发现 2**：`snapshot_watcher.py` 依赖 `vault.orchestration.move_detect` + `vault.file_entry`，不能归入零上行依赖的共享内核包。
- **新发现 3**：`ledger.py`、`sync.py`、`egress.py` 本身已是零 SQL（egress 已下沉到 `db/egress_sql.py`）—— 这三个是「纯移动」最干净的样本，写面哲学完好，保留。
- 钉测现状确认：`test_vault_shim.py`（38 名 shim 面）、`test_routes_order.py`（快照的是 app.router 的 **(methods, path) 表**，不是模块路径）、`test_ledger_import_discipline.py` / `test_vault_db_locks.py` / `test_refs_discipline.py`（**pytest 源码扫描钉纪律已是本仓库的成熟惯例**）。
- `client/` 不 import 任何 `server.*`；`pyproject.toml` 钉 `md-vault-mcp = "server.mcp_server:main"`。

## 1. 目标目录树（包粒度）

```
server/
├── main.py                  # 组合根：config→双库→事件总线→Runtime→app 的唯一装配点（零业务）
├── api.py                   # create_app 工厂（零业务）；HTTP 通道的装配半边。位置保留（钉测/习惯）
├── mcp_server.py            # MCP stdio 通道入口：token 鉴权→身份注入→工具分发。位置保留（pyproject script 锚点）
│
├── routes/                  # 【接口层】HTTP 通道。8 个域模块 + WebContext + ROUTE_MODULES 冻结序。
│                            #   目标：零 SQL、零私有符号 import；身份与参数校验在此完成
├── agent/                   # 【接口层+应用层】交互 agent 通道：runtime/session/validate/execute=通道循环，
│   └── tools/               #   15 个工具=用例（调 services，不碰 SQL）
├── bgagent/                 # 【应用层】后台 agent 通道：scheduler/runner/executor/notify/readback
│
├── services/                # 【应用层·新建】根目录领域服务迁入：
│   ├── ledger.py            #   change_log+files 唯一写面（哲学原样保留；已是零 SQL）
│   ├── proposals.py         #   提案状态机与授权层（SQL 下沉后成为纯编排）
│   ├── snapshots.py         #   pre-image 快照与 GC
│   ├── trash.py             #   回收站用例（从 vault/orchestration 迁入，斩断包级环，见 §3）
│   ├── recovery.py          #   冷重建播种
│   ├── sync.py              #   三向分类（纯函数，全仓最纯的领域逻辑）
│   └── egress.py            #   出域台账写面（SQL 已在 db/egress_sql）
│
├── core/                    # 【基础设施·共享内核·新建】任何包都可依赖、自己不依赖 server 内任何东西：
│   ├── events.py            #   事件总线（asyncio loop-bound）
│   ├── privacy.py           #   PrivacyConfig 活视图 + is_privacy（读边界规则的载体，被 20 个 server 模块 + 7 个测试跨层消费，必须住地板）
│   ├── sync_lock.py         #   跨进程文件锁（O_CREAT|O_EXCL）
│   ├── logrotate.py         #   日志清扫协程
│   └── types.py             #   FileEntry + ModelConfig：跨层值对象（实体统一住所）
│
├── vault/                   # 索引子系统（数据面），内部三层保持现状骨架：
│   ├── __init__.py          #   38 名 Deprecation shim——仅迁移期存续，随 test_vault_shim 一并退役，非目标结构成员
│   ├── watcher.py           #   snapshot_watcher 迁入（它 import move_detect+FileEntry，放 core 会破「内核零上行」）
│   ├── db/                  #   【基础设施】repository 层：目标态 = 全仓唯一 SQL 驻地；双库连接注册(conn.py)与两把锁的家
│   ├── fs/                  #   文本解析(segmenter/link_parser=领域) + vault_ops(fs 操作+路径护栏=基础设施)
│   └── orchestration/       #   【应用层】索引编排：indexer/file_ops/refs/move_detect（SQL 下沉后只编排不写 SQL）
│
├── ai/                      # 【适配层·叶子】LLM 适配器：chat/rerank/search（search 内 4 处 SQL 下沉到 db）
│                            #   目标态只依赖 db/fs/core，故与 orchestration/services 同层。现状违例两处：
│                            #   search.py:37 懒取 indexer._normalize_tag、:198 懒取 rebuild_segments_all
│                            #   ——A7 已裁：卡 8 了结（_normalize_tag 移 fs/ 成公开函数）；ai 上行禁边补入 R5（卡 5 随全片上线）
├── config/                  # 【基础设施】配置加载（现状良好，不动）
└── templates/               # 前端（Vue CDN，不动）
```

**四层映射的诚实说法**：四层是**依赖规则**，不是四个目录。包与层的对应是：接口层 = `routes/ + api.py + mcp_server.py + agent/ 的 runtime 半边`；应用层 = `services/ + vault/orchestration/ + agent/tools/ + bgagent/`；领域层 = `core/types + vault/fs 解析器 + services/sync 等纯逻辑`（刻意**不**为纯逻辑单立 domain 包——纯规则今天与编排同居各模块，强拆是搬家不是收益）；基础设施 = `vault/db + core + config + vault/fs/vault_ops + vault/watcher`。`ai/` 角色上是基础设施（LLM 适配器），层位却按**纯叶子**放中层（≈services/orchestration）——因为它只依赖 db/fs/core，这样 recovery→ai.rag_search 是同层合法边（§3 对账注）。

**三个归属问题的答案**：
- **双库连接**：基础设施层，物理住址保持 `vault/db/conn.py`（挪址要动 24 个文件、零行为收益；L5-prod 每线程连接落地时再论搬家）。但**取用面收缩**：目标态 `get_index_conn/get_ledger_conn` 只允许通道入口适配器调用（agent 工具运行时、mcp_server、bgagent runner 装配点），services/orchestration 一律显式参数传 conn——这是 file_ops 隐式跨库写的根治方向。
- **事件总线**：基础设施层，`core/events.py`。发布方（file_ops/recall/proposals/bgagent）向下依赖 core，合法。
- **锁**：`sync_lock` 是基础设施（core）；`_db_lock`/`_ledger_lock` 留在 `db/schema.py` 包内（已有钉测），跨包消费面以「持锁的具名函数」为接口——这条纪律已存在，目标结构只是把它推广到所有私有符号（§3）。

命名已定（2026-11-18 评审）：`services/`（弃 application/——应用层不止此包，该名误导；弃 domain/——trash/recovery 是用例非纯领域）、`core/`（弃 platform/——与 stdlib 同名视觉歧义且更长）。注意 `agent/events.py`（SSE 事件适配）与 `core/events.py`（总线）是两回事，迁移卡在 docstring 里互指一下即可。

## 2. 现状 → 目标完整映射表

工作量三档：**P** = 纯移动（移动 + grep 更新 import，pytest 全绿即完成）；**S** = 需下沉 SQL（行为保持，逐函数搬进 db 具名函数）；**I** = 需注入改造（行为等价但签名/时序变化，需要针对性回归）。

### 2.1 server/ 根 16 模块

| 现状 | 去向 | 档 | 消费面（import 更新量） | 备注 |
|---|---|---|---|---|
| `api.py` | 原地不动 | — | 0 | 钉测与 conftest 都指它 |
| `main.py` | 原地不动 | — | 0 | 组合根 |
| `mcp_server.py` | 原地不动 | I(后期) | 0 | pyproject script 锚点；1 处 SQL 下沉、懒 conn 收敛在后期卡 |
| `events.py` | `core/events.py` | P | 6+4 | **sed 红线**：agent 包内 `from .events import Event`（agent/\_\_init\_\_.py:8、runtime.py:23、agent.py:12）指 `agent/events.py`，不许碰；更新用白名单枚举式，禁盲 sed |
| `logrotate.py` | `core/logrotate.py` | P | 1 | |
| `sync_lock.py` | `core/sync_lock.py` | P | 2 | |
| `privacy.py` | `core/privacy.py` | P | 20+7 | 被 db 层 import（changes:7/graph_sql:13/segments_sql:8），故必须住共享内核；移动本身零风险 |
| `model.py` | `core/types.py` | P | 2 | 16 行；config/bgagent + agent/spec |
| `snapshot_watcher.py` | `vault/watcher.py` | P | 2 | 依赖 move_detect/FileEntry，不能进 core |
| `ledger.py` | `services/ledger.py` | P | ~11 | 已是零 SQL；写面哲学原样带走 |
| `proposals.py` | `services/proposals.py` | P→S | ~8 | 移动先行；10 处 SQL 下沉到 `db/proposal_records.py` |
| `snapshots.py` | `services/snapshots.py` | P→S | ~6 | 5 处 SQL 下沉到 `db/snapshot_records.py` |
| `recovery.py` | `services/recovery.py` | P→S | 1(main) | 3 处 SQL 下沉；另持 `ai.rag_search` + `snapshot_watcher` 两条边，层序调整后均合法（§3 对账注） |
| `sync.py` | `services/sync.py` | P | 1 | 纯函数，最干净的卡 |
| `egress.py` | `services/egress.py` | P | ~5 | SQL 已在 `db/egress_sql.py`，纯移动 |
| `__init__.py` | 原地 | — | 0 | |

### 2.2 server/vault/

| 现状 | 去向 | 档 | 备注 |
|---|---|---|---|
| `file_entry.py` | `core/types.py`（FileEntry）；**原址留一行 re-export 存根** | P | 存根期间 shim 的 `from .file_entry import FileEntry` 与一切旧 import 照常工作——这是与 shim 的唯一冲突点，见 §5 |
| `__init__.py`（shim） | 不迁移；测试逐个改 import 真模块后整体退役（独立决策卡） | — | 38 名面在退役前不许变 |
| `db/`（18 模块） | 原地不动，**作为唯一 SQL 驻地接受全仓下沉** | 接收方 | 新增/扩展：runs 统计查询（来自 routes/memory）、recall 统计聚合（来自 recall_stats 工具）、indexer 事务化函数 |
| `db/schema.py` | 原地 | I(伴随) | `_ACTION_TO_OP/_OP_TO_ACTION` 公开化为 `ACTION_TO_OP/OP_TO_ACTION`（op 归一化词表本就是公共契约）；`_db_lock` 保持私有，file_ops 需要的临界区改为 db 层具名事务函数 |
| `fs/`、`orchestration/{indexer,refs,move_detect}.py` | 原地 | indexer=S(13)、其余 P/— | indexer 编排留下，SQL 下去 |
| `orchestration/file_ops.py` | 原地 | S+I | 2 处 SQL + 2 处 `with _db_lock:` 下沉为 db 事务函数；跨库 relatedness 改写**上移到 ledger**（双 SQLite 文件本非原子，上移是行为等价的边界显式化；ledger 全部参数收 conn、自身零懒取——A4 已裁：`record()/record_folder()` 增可选参 `ledger_conn=None` 由通道侧传入，卡 10 改动面含 ~7 个调用方机械加参）；此后 file_ops 零 SQL、零私有 import |
| `orchestration/trash.py` | `services/trash.py` | P | 斩包级环：trash 是用例（restore/purge/empty），调 ledger 合法；斩后 orchestration⊄services 成为可钉规则 |

### 2.3 接口层与通道的 SQL/conn 收敛

| 现状 | 去向 | 档 | 备注 |
|---|---|---|---|
| `routes/memory.py` | 原地 | S(6) | 后台 runs 统计 → db 新查询函数。只改函数体，register() 结构不动 → 路由表钉测全绿 |
| `routes/{notifications,egress,agent,sync,recall}.py` | 原地 | S(3+3+3+2+1) | 同上，逐模块一张卡 |
| `agent/tools/recall_stats.py` | 原地 | S(11) | 聚合查询 → `db/recall.py`；工具只格式化 |
| `agent/tools/*`（懒 conn 者） | 原地 | I(后期) | conn 注入 ToolContext；`get_*_conn` 白名单棘轮收缩 |
| `ai/search.py` | 原地 | S(4) | degraded rag fallback SQL → `db/fts_query.py` |
| `bgagent/*`、`config/*`、`agent/` 其余 | 原地 | — | 结构已良；仅 import 路径随移动更新 |

### 2.4 仓库其余

`client/`（不依赖 server，零改动）、`tests/`（迁移卡的机械 import 更新 + 新增 §4 钉测）、`templates/`、`docs/`、`issues/` 不动。

## 3. 依赖规则（允许方向 + 禁止边）

层序（自底向上）：`config ≈ core < vault/db ≈ vault/fs < ai ≈ vault/orchestration ≈ services < agent/bgagent < routes/api/mcp/main`。

**允许**：任何包 → config/core（共享内核随便踩）；services → vault 全部（其中 SQL 只经 `db/` 具名函数）；orchestration → db/fs/core；agent/bgagent → services/vault/core；routes/api/mcp → services/vault 公共面/core；routes/api/mcp → ai、services → ai（A7 已裁）。

**禁止边**（每条 = §4 一条棘轮规则）：

1. **SQL 单一驻地**：`.execute(`/`.executescript(`/`sqlite3.connect` 只许出现在 `server/vault/db/**`。今天 68 处违例全部列成棘轮基线，只许缩不许涨。
2. **接口层零 SQL**：`routes/**`、`api.py`、`mcp_server.py` 不得出现 `.execute(`——规则 1 的子集，单列是因为这三处的钉要**立刻**归零（下沉完成后基线里不许留名字）。
3. **私有符号禁跨包**：任何 `from 别的包 import _x`（下划线开头）即违例。包粒度 = **任意 `__init__` 目录**（A2 已裁：私有不出目录的本意），存量 **20 条**入棘轮基线只缩不涨（如 `_invalidate_wikilink_filename`、`_iter_vault_md`、`_find_referrers`）；file_ops→`db.schema._ACTION_TO_OP/_db_lock` 属必修项。已有 `_ledger_lock` 钉测是此规则的特例，保留。
4. **懒 conn 白名单**：`get_index_conn/get_ledger_conn` 调用者 = **冻结基线名单**（通道入口 + db 内部 + main 组合根；A3 已裁：snapshots.py/file_ops.py 等存量成员在名单内），名单外新调用者 = CI 红；基线只缩不涨，收缩计划：file_ops 随卡 10、snapshots/agent tools 随卡 11。
5. **斩包级环**：`vault/orchestration/** ⊄ services`、`vault/orchestration ⊄ routes`；`services ⊄ agent、bgagent、routes`（实测当前 services 六模块零上行 import，此规则今天就能钉死）；`vault/db/** ⊄ orchestration、services、agent、bgagent、routes`（`db/notifications.py:19` 的 docstring 已有「不 import bgagent 形状」的口头纪律，此规则把它钉死）；`ai ⊄ services、agent、bgagent、routes`（A7 已裁；`ai→orchestration` 两处懒取 search.py:37/:198 入暂存基线，卡 8 了结）；`core ⊄ vault、services、routes、agent、bgagent、ai`（trash 移入 services 后全部成立）。**对账注（2026-11-18）**：services 六模块对 agent/bgagent/routes 实测零上行（亲验）；recovery 的 `from server.ai import rag_search`（recovery.py:37）与 `snapshot_watcher`（目标址 `vault/watcher.py`，services→vault 允许）在层序调整后均合法，规则 5 上线当天即可零违例钉死。
6. **身份不从参数来（红线 3 的结构化）**：`ToolContext.write_source/run_id/session_id` 只许通道入口装配点赋值——钉法：`agent/tools/**` 源码禁止对 `ctx.write_source|ctx.run_id|ctx.session_id` 赋值（现在没有，钉住防未来）。

## 4. 强制机制选型

| | pytest AST/源码扫描测试 | import-linter |
|---|---|---|
| 表达力 | 规则 1-6 全能表达（SQL 调用、私有符号、conn 调用者、赋值禁止都是源码级，非纯 import 方向） | 只能表达包级 import 方向（规则 5 勉强，其余表达不了） |
| 棘轮（基线只缩不涨） | 测试文件里放基线列表，天然支持 | 要靠每合约 `allow_failed_imports` 手改，别扭 |
| 新依赖 | 0 | +1 dev 依赖 |
| 仓库惯例 | **已是惯例**：`test_ledger_import_discipline.py`/`test_vault_db_locks.py`/`test_refs_discipline.py` 三个前例 | 无 |
| 反馈速度 | pytest 全量里跑，秒级 | 略快，但本仓全量本就不是瓶颈 |

**推荐：纯 pytest。** 新增单文件 `tests/test_architecture.py`：一次 `ast.walk` 全 `server/**/*.py`，按规则表出违例集，与外置基线 `tests/architecture_baseline.json`（A6 已裁：外置，conftest 注册 `pytest_addoption` 提供 `--update-arch-baseline`，写回 JSON 后 git diff 逐行可读）比对，要求「子集 + 报告缩了多少」。import-linter 拒绝：规则半数不是 import 方向问题，且棘轮是刚需。**起步口径（定稿）**：规则 6 = 零违例起步即钉；规则 2 = 以现状 19 处（routes 18 + mcp_server PRAGMA）入基线，卡 7/8 逐模块归零，归零后该模块名永久禁止再入基线；规则 3 = 存量私有符号入基线（口径待 §7-A2）只缩不涨；规则 1/4 = 现状入基线只缩不涨；规则 5 = 分两步——`core ⊄ 任何东西` 随卡 1 上线，其余（含 orchestration⊄services，需 trash 先迁）随卡 5 上线。R1 检测形态锁 AST（Call: execute/executemany/executescript + sqlite3.connect；tests/** 豁免；docstring 天然不中——这正是弃文本扫描的理由；db 外 executemany 实测为 0，随首片零违例钉死）；R6 锁 AST Assign/AugAssign 且 target 含 Attribute(write_source|run_id|session_id)，dataclass 字段默认值 AnnAssign（tool.py:35/40/46）不算违例也不入基线，`dataclasses.replace(ctx, write_source=…)` 为文档化盲区不追加检测；test_architecture 用 AST 故基线允许字面量（豁免仓库文本扫描自命中惯例）。

## 5. 迁移顺序草案（每卡独立提交、全量 pytest 绿；先零风险后行为改动）

**卡前总则（钉死）**：① 开工卡前 `git status --porcelain` 必须为空（当前工作区有前端未提交改动，开工前处理），每卡起点 = 上一卡 HEAD；② AGENTS.md 的地址类 import 指引（events/privacy/sync_lock/ledger/trash 等）随移动卡**同 commit** 更新——该文件明教「Import directly from the real modules」，不随更 = 教错后续会话；③ 卡内意外红两级处置（§7-A8 确认）：机械修复（import 路径/字符串字面量/docstring 互指）当卡继续并写卡尾注记，凡触及行为、签名、conftest fixture、钉测断言的调整**停卡上报**；④ commit 模板：`refactor(arch): card <N> <slug> — <move|sink|pin> (R<x> baseline −<k>)`，基线无变化写 `R− none`，卡 7 子卡编号 7a–7f。

**Phase 0 — 纯移动，钉测零接触**（卡 1 按 A5 裁决拆两卡）
1a. `core/` 包 + events/logrotate/sync_lock 三件迁入 + **`tests/test_architecture.py` 首片上线（`core ⊄ 任何东西` 零违例钉死，基线走外置 JSON + `--update-arch-baseline`，conftest 注册 addoption）**。冲突：无——不在 38 名 shim 面里，routes 包原封未动。sed 红线：agent 包内 `from .events import Event`（agent/\_\_init\_\_.py:8、runtime.py:23、agent.py:12）指 `agent/events.py`，不许碰。
1b. privacy（20+7 消费面）+ model（`ModelConfig` → `core/types.py`）迁入。同卡更新 AGENTS.md 的 sync_lock/privacy 旧址指引（1a 已更 sync_lock 行）。
2. FileEntry → `core/types.py`，`vault/file_entry.py` 留 re-export 存根。**与 shim 的唯一冲突点**：shim `from .file_entry import FileEntry` 经存根照常解析，`test_vault_shim` 38 名不破。
3. `snapshot_watcher.py` → `vault/watcher.py`。冲突：无。

**Phase 1 — services/ 包**
4. `ledger/sync/egress` 三纯件迁入（零 SQL，最干净）。冲突：无（shim 不含它们；根 import 改 `from ..services import ledger` 等，机械 sed）。
5. `trash.py` orchestration→services + 钉规则 5 上线（`orchestration⊄services` 当卡即可零违例钉死）。
6. `proposals`、`snapshots`、`recovery` 迁入（先 P 后 S 两提交）。**决策点：根转发 stub（`server/ledger.py = from .services.ledger import *`）刻意不用**——双重 import 面会让写面纪律与 §4 扫描变模糊，硬切测试 import 是机械劳动。

**Phase 2 — SQL 下沉（接口层先归零）**
7. routes 六模块逐个下沉（每卡一模块，`register()` 结构不动 → `test_routes_order` 全程绿；规则 2 在该模块卡内基线归零）。
8. `recall_stats` 工具(11)、`ai/search`(4)、`mcp_server`(1)。
9. `indexer`(13)。
10. `file_ops`：SQL 下沉 + `ACTION_TO_OP` 公开化 + relatedness 改写从 file_ops 上移 ledger。**本阶段唯一行为改动卡**：双库本非原子、上移等价，但需 `test_ledger*/test_move_detect/test_relatedness/test_reconcile` 回归；`test_vault_db_locks` 与锁纪律钉测保持绿是验收线。

**Phase 3 — conn 显式化（可无限期后置，按需要卡）**
11. ToolContext 携带双 conn，工具停用懒取；规则 4 白名单逐卡收缩（24 文件 → 目标 ~6）。
12. shim 退役评估：tests 全部改真模块 import 后，`server/vault/__init__.py` + `test_vault_shim.py` 一起删，`file_entry.py` 存根随之删。独立决策，本方案不强制。

**与钉测的总账**：`test_routes_order` 钉的是路由表快照——本方案对 routes 包**零移动、只改函数体**，它全程无感；`test_vault_shim` 只在卡 2 被存根化解、卡 12 主动退役；若未来有人要迁 `routes/` 址，该钉测其实仍会绿（快照不认模块路径），但也完全没必要——原地就是目标态。

## 6. 风险与不做清单

**风险**：
- 移动卡的测试 import 更新量大（privacy ~17、ledger ~11 处消费面）：机械但噪声大；一模块一提交，`grep -rl` 驱动，别混进行为卡。
- 卡 10（file_ops）是全案唯一行为改动，出错面在 ledger 消费路径：该卡单独评审、单独可 revert。
- 棘轮基线会腐烂（新违例被 grandfather）：纪律 = 每张下沉卡必须同卡使基线严格缩小，review 检查项写进 commit message 模板。
- FileEntry 双名（types.py + 存根）与新旧 import 并存期：止于卡 12，可接受。
- `core/` 一词诱惑未来倾倒杂物：规则 5 的 `core ⊄ 任何东西` 从卡 1 当天钉死，防它长胖。

**刻意不做**：
- 不引入 DI 框架/FastAPI Depends；WebContext 显式传参模式保留。
- 不动双库单例 conn 对（L5-prod 每线程连接是独立工程，conn.py 自己说它是迁移单元，随它）。
- 不建 auth/HTTPS、不建敏感分区 0→1、不修评审定稿列出的现状缺口（auto 轨免鉴权、MCP 写状态回查、watchdog 归因退化、长驻 MCP 自写不可见）——它们是安全/功能 backlog，不是结构问题，本方案只让「通道→身份注入→policy」住址变显式（routes/mcp_server/agent-runtime = 三个注入点）。
- 不为 proposals/snapshots 拆 DDD 聚合层——SQL 下沉 + 具名函数足够。
- 不把 routes 强制改造成「只准碰 service 门面」：短中期允许 routes→vault/db **公共**读函数（console 今天就是薄读），只禁 SQL 与私有符号——过度门面是下一轮的事。
- 不拆 `server/vault` 独立成包/仓库；不重写 client；不碰前端。
- 不修 move→edit→delete 同步盲区、外部删除复活窗口（既有文档化残差）。
- 不引入 import-linter。

## 7. 裁决记录（A1–A8，Gate 0 对抗评审产出；2026-11-18 逐条裁决完毕，全文与代价分析见 `.slim/deepwork/arch-layering/spec-review-1.md`）

| # | 开放项 | **裁决（终值）** |
|---|---|---|
| A1 | R1 口径：`connect`/PRAGMA 算不算违例？recovery 自持 conn 怎么办？ | ✅ 全口径 72 处入基线；recovery.py:100/:251 为长期基线（冷重建/drill 自持连接的合法例外，注释注明）；PRAGMA 随卡 8 下沉为 db 具名开连函数 |
| A2 | R3「包」粒度 | ✅ 任意 `__init__` 目录口径，20 条存量入基线；例子换成 `_find_referrers`（refs.py:22、trash.py:38），`_update_references` 例子作废（同包非违例） |
| A3 | R4 白名单字面矛盾 | ✅ 冻结基线名单只缩不涨，名单外新调用者即红；收缩计划：file_ops 随卡 10、snapshots/tools 随卡 11 |
| A4 | 卡 10 relatedness 上移后 ledger.db conn 来源 | ✅ `record()/record_folder()` 增可选参 `ledger_conn=None` 由通道侧传入；卡 10 改动面含 ~7 个调用方机械加参。**补裁终值（2026-11-19，gate2-addendum.md）**：扩至 `consume()` 同款 kw-only 参；`record_folder` 不加参（作废）；必传 4 点=move_docs/console:110/main.py:85/reconcile 接线；重写留 file_ops moved 分支只换 conn 来源 |
| A5 | 卡 1 拆合 | ⚖️ **两卡**：卡 1a = core 包 + events/logrotate/sync_lock + test_architecture 首片；卡 1b = privacy + model → core。首片钉测在 1a 上线（core 当日成立即钉） |
| A6 | 棘轮基线存放 | ✅ 外置 `tests/architecture_baseline.json` + `--update-arch-baseline`（conftest 注册 addoption） |
| A7 | ai 叶子性无规则钉且现已被违 | ✅ R5 增 `ai ⊄ {services, agent, bgagent, routes}`；`ai→orchestration`（search.py:37/:198）入暂存基线，卡 8 了结（`_normalize_tag` 移 `fs/` 成公开函数）；§3 允许方向补 `routes/api/mcp → ai`、`services → ai`。**补裁终值（2026-11-19，gate2-addendum.md）**：:198 采案 A——删 ai 懒重建，空 segments 显式降级 legacy（案 B 移 db 驳回）；随卡清 `_STAGED_EDGES` 并元测试断言恒空 |
| A8 | 意外红处置协议 + commit 模板 | ✅ 两级协议与模板按推荐值写入 §5 总则；边界判例归 spec-review-1.md 续记 |
