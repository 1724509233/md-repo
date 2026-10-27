# md-sync

LAN 多设备 Markdown 文档同步系统的领域语言（FastAPI 服务端 + Python 轮询客户端 + Web UI / MCP / bgagent）。术语必须带主语（如 **client sync cycle** 而非「sync cycle」）；一个词若没有主语，先问「谁的」。端点直接用 URL 称呼，不另造词。

## Language

### 同步核心

**client sync cycle**：
客户端 `sync_once` 的一次完整往返：POST manifest → compare → pull / push / conflict / delete 四步。
_Avoid_: sync、sync round、同步（裸词）

**compare**：
服务端纯 manifest 对比（`compare_files`，无 IO）：路径集合差集 → `to_pull` / `to_push`，同路径不同哈希 → conflict。
_Avoid_: diff、sync（服务端）

**manifest**：
路径→SHA-256 的文件清单，中性数据结构，使用时必须带主语：`client manifest`（客户端的，线格式名 `local_files`）或 `server manifest`（服务端的，从 vault 实际状态导出）；裸词禁用。compare 就是拿这两份 manifest 做差集。
_Avoid_: local_files（线格式名，不是术语）、文件清单（裸词）

**stale copy**：
服务端 rename 后客户端变旧的路径副本（内容哈希与移动后的新路径一致）；compare 将其从 `to_push` 移入 `to_delete`，客户端应删除本地副本——否则 push 回去会在服务端复活旧路径。是同步协议 actionType 的一个值。
_Avoid_: to_delete（字段名，不是术语）、delete、旧文件

**delete**：
vault 内一次真实文件删除，记入 change_log（op 枚举之一：created / modified / deleted / moved），经 `apply_change` 唯一变更入口落账。与 stale copy 分属两层：op enum（文件 changelog）vs actionType（同步协议）。
_Avoid_: stale copy、to_delete、删除文件（裸词）

**sync lock**：
服务端模块级内存锁（`_sync_lock`，server/api.py）：300s 超时自愈；持锁期间 `/api/sync` 竞争返回 409；进程重启即重置。409 → 客户端静默等下一个 sync cycle 重试。
_Avoid_: lock（裸词）、mutex

### 文件观察

**snapshot watcher**：
服务端快照对比式文件监听（`SnapshotWatcher` 类）：watchdog library 仅作 doorbell（只标记、不带数据），worker 线程 debounce 1.5s → 原子换出 dirty set → 读盘对比内存快照 → 发语义事件（create / modify / move / delete）；另有 30s reconcile 全量扫描兜底补漏。
_Avoid_: watcher（裸词）、watchdog（那是依赖库）、file watcher

**dirty set**：
单一全局 `set[str]`（vault 相对路径）：doorbell 事件只做 `add(rel)`，同 path 天然去重；worker 原子换出后批量处理。事件不带任何 payload，语义全部来自读盘对比。
_Avoid_: per-path 队列（规格文档口径，与实现不符）、dirty queue、事件队列

**source="watchdog"**：
change_log.source 的取值：非项目内修改的统一兜底来源——服务端本机直接落盘的写入（如 Obsidian 桌面端），write registry 查不到登记时兜底。指写入方，不是 watchdog library。
_Avoid_: 「watchdog 库写的」、local write（裸词）

**write registry**：
服务端进程内的写归因注册表（模块级 dict，TTL 10s）：项目写通道落盘前 `register_write(path, source[, batch_id, consumer_skip])` 登记（batch_id = 执行批次标识，executor / run 透传）；snapshot watcher 观察到变更后 `pop_source(path)` 取走 `(source, batch_id, consumer_skip)` 元组记入 change_log；`consumer_skip=True` 专用于 web 直写路径（端点同步落账，watcher 不重复记账，防双行）；查不到 = 非项目内修改 → 兜底 `source="watchdog"`。
_Avoid_: write_registry（模块名，不是术语）、归因表

**reconcile**：
一个词、两个机制（主语恒为 snapshot watcher，裸词禁用）：① 运行时 reconcile——worker 空闲 30s 全量扫描对比内存 snapshot，差异只标脏、走正常 flush 管线（归因正常，Obsidian 直写记 `source="watchdog"`）② 启动 reconcile——`start()` 内首次全量扫描后、doorbell 启动前，对比 DB（files 表），直接 `apply_change`、归因 `source="reconcile"`，含 pair_moves 移动检测与重复检测。
_Avoid_: 对账（裸词）、resync

### 索引/搜索

**segment**：
markdown 的文本块：`segment_markdown` 的产物（frontmatter→meta、``` 代码块→code、其余按 400 字/空行切分，带 `heading_path` 面包屑）；搜索索引与引用的单位。其持久化为 `segments` 表（FTS 命中粒度，每行一个 BM25 分数）。
_Avoid_: chunk、block、分块（裸词）

**segments_fts_docsize**：
FTS5 external-content 虚表的阴影计数表：对虚表 `count(*)` 会扫内容表、数值不可信，此表行数才是判断 FTS 虚表与内容表是否失同步的唯一可靠信号；`ensure_indexed` 两级检查的第二级比这两个数，不等 → 仅 FTS rebuild（不碰盘）。
_Avoid_: FTS 计数器、docsize 阴影表（裸词）

**recall level**：
分级搜索的档位（strict → near → relaxed → fuzzy，由紧到松），搜索请求 recall 参数的取值域。
_Avoid_: recall tier、recall（裸词）

**recall log**：
搜索行为账本，两张表：`recall_queries`（每次搜索一行，含命中数；零命中行 = failed-recall 信号）与 `recall_log`（每次命中一行，含 score）；唯一写入方 server/vault/db/recall.py；消费方 = sentinel 与 recall arming。
_Avoid_: recall telemetry（代码对用途的自称，不是术语）、recall（裸词）

### 记忆运行时

**sentinel**（记忆哨兵）：
bgagent，无落地动词角色（报告通道，ADR-0004）：读 recall log、对文档分桶、产只读报告（机器区文件 + 控制台 API）；archive 提案已退役（v2.1 D5，实际搬动 = 人 / 交互 agent）；无 propose、不写用户区；每周心跳 + recall arming 提前唤醒。
_Avoid_: 哨兵（裸词，中文须带「记忆」）、sentinel agent

**recall arming**：
sentinel 专属的武装信号源：`scan_recall_arming` 用双游标扫 recall log（零命中查询数 + 低分命中数）累计，达 arm_count 且过冷却/日预算后触发一次运行。
_Avoid_: recall trigger、recall source（事件名）

### 记忆运行时 · v2 提案与写通道

**propose 工具**：
bgagent 提案通道唯一写入口（v2.1 D25）：`propose(type, payload, source_refs, justification)`——调用时三段校验（信封 + per-verb payload schema + 越权拒绝），报错零落盘（错误串返回 agent，无任何文件/账本写入）；成功 = 提案记录进账本；runner 盖章 actor/run_id/verbs（模型不可自报身份）；零文件落，`suggestions/` 由禁写守卫双保险。
_Avoid_: 提案（裸动词）、写提案

**提案记录**：
v2a 提案本体 = 账本库 `proposals` 表一行（payload JSON / source_refs / justification / reject_reason + `proposal_transitions` 变迁历史表）；状态以账本记录为唯一状态源（v1 文件 frontmatter 状态源退役）；`relpath` = 合成寻址标识（确定性命名，背后无文件）；存量 4 条 v1 投影 = 惰性遗留物，不导入、不重投影、不删除。
_Avoid_: 提案文件、suggestion 文件（v1 残留词）

**decide_record**（代码名 `advance`）：
提案状态迁移单路径（`proposals.advance`）：全 server 唯一一处 `UPDATE proposals SET status`；调用方 = 控制台 API / 协同终审四工具 / 执行器 / expire_overdue；读侧前置态校验 + （v2.2 债单 #1：`WHERE status=?` 条件 UPDATE 待实现，非法前置态 → 错误返回，不变量 11/12 测试锚点）。
_Avoid_: 决策、状态迁移（裸词）

**越权拒绝**：
propose 工具第三段校验：提案 `type` ∉ 角色 `verbs:` 声明 → 拒绝并列出允许动词（操作化 reject_reason 返回 agent 供自我纠正）；bgagent `verbs:` 必须显式声明，未声明 → 启动 fail-fast 报错（v2.2 债单 #7，Q7/Q9）。
_Avoid_: verb check、privilege check（裸词）

**身份带外**：
合法性判定原则：读工具面/进程面绑定（进程入口 → 允许目标路径集合；runner 盖章 actor/run_id），不读任何 agent 自报的身份字段；提案通道（propose 盖章）与人执笔直写通道（user-ask / rebase 落地）同规（v2.1 §7.5.8）。
_Avoid_: actor 盖章（裸词）、身份校验（裸词）

**人执笔写路径**（v2.1 §1.3）：
机器执行写调用、但每次写入由人逐次、事前、显式确认的写路径；目前仅两种：user-ask（交互 agent 逐次请示）/ rebase（人发起的 fact 改写，执行器持人确认凭据落地）。成立要件四条，缺一即违宪自动化写：逐次确认、事前确认、独立快照、身份带外。法律拟制：机器为笔、人为作者——宪法上归类为人的写入，不是自动化的第三类写者。
_Avoid_: 人确认写、交互写（裸词）

**报告通道**（v2.2，ADR-0004）：
bgagent 无落地动词角色（sentinel / 叙事者）专属写通道：读工具 + `write_doc`（写边界 = 机器区 `index/`），无 propose、用户区零写；**分界线：提案角色（固化者）= propose 唯一写入口、无直写**（防终审闸门被绕过）；人面 = 控制台报告 API + 推送，主 agent 用既有 `read_doc` 读（index/ 可读、不进搜索图谱，不新增读工具）。
_Avoid_: 报告路径、bgagent 直写（裸词，与用户区直写歧义）

**回读注入**：
runner 每次 run 在上下文头部注入本角色的：未决 N 条 / 失败 M 条（含 reject_reason）/ 被忽略的 relatedness 配对（遥测）；有界 top-5；框架级反馈环（硬约束，非提示词自律——run N 的失败必然在 run N+1 的上下文里）。
_Avoid_: readback、反馈注入（裸词）

### 记忆运行时 · v2 机器区产出

**relatedness**：
机器建议层的文档级关系边：账本库 `relatedness` 表一行（源, 目标, 依据, run_id, ignored）；零文件落、不进图谱、不进搜索、不进用户文件；两层呈现（用户 wikilink 层与机器建议层分列，机器边 = 可一键忽略的建议，忽略 = DB 标记 + 回读遥测）；排序接线 + ignored 配对机械过滤归 v2b。与 related edge（用户 frontmatter 图谱层）是两物。
_Avoid_: related edge（用户层图谱边，另一物）、机器边（裸词）

**errata**（v2.1，替代 correction）：
加法式勘误卡：`index/errata/<stem>.md` 单槽（v2a；唯一文件名 `<stem>-p<record-id>.md` 归 v2b）；合规形态 = 只许 surface 矛盾 + 候选值（标注出处、人可改写）+ 单闸门（物化 = 确权，物化后系统只读、所有权归人）；覆盖保护 = 目标槽已有已物化卡时执行器拒收 + surface（D23/#C1，v2a 债单 #2）；消费 = 召回时原文 + 勘误并列呈现（v2b 接线），永不静默替换。
_Avoid_: correction（退役 v1 动词）、勘误卡（裸词，用 errata）

**记忆化**：
人把报告 / summary 复制进主区的动作 = 机器区派生转用户区记忆（派生 → 用户区的唯一合法路径）；人的书写动作（交互 agent 可代劳，经人执笔写路径）。
_Avoid_: 转正、复制到主区（描述性，不是术语）

**单活快照**：
快照库保留语义：每文件至多一个活 pre-image（新调用覆盖旧）；GC 只允许删「被更新快照取代」的旧 pre-image，永不删活的；in-flight 行（已捕捉、写未确认的崩溃残留）GC 永不回收；删除类单列保留 30 天。回滚 = 回到上一次调用前；同文件多次调用仅支持逆序回滚序列（乱序 = 撞车守卫预期拒绝，是预期行为非故障）。
_Avoid_: 最新快照（描述性）、live pre-image（英文裸词）

**degraded 模式**：
快照捕捉失败（I/O 失败）后的状态：写照常、快照跳过、记日志——fail-open（主路径可用性 > 审计完整性）；LLM 独一产出暂失 pre-image；v2a 失败计数必须进控制台待确认面板（降级可见、不静默；v2a 债单 #5）。
_Avoid_: degraded（裸词）、快照失败（裸词）

### 图谱

**related edge**：
图谱边的一种类型，唯一从 frontmatter 的 `related:` 字段解析（每项经 wikilink 解析后写入；解析不出的静默丢弃）——另两类边 wikilink/tag 来自 body 原文解析。端点 `/api/graph/related` 与工具 `get_related_docs` 名中的 "related" 是泛义「相关文档」（1 跳/N 跳邻域，混合三种边），不等于 related edge。v2.1 spec 的 `relatedness` 是本词条的另一物（机器建议层 DB 行，见 relatedness 词条），勿混用。
_Avoid_: 用 related edge 指代端点/工具的邻域结果（那是 N-hop neighborhood）、relatedness（v2 机器层，另一物）

**reference maintenance**：
文档移动/删除后的引用改写，写方向流程（`refs.py`，拿 referrer lookup 的结果落盘）：move 多移动单遍处理——引用方集合取各 move 的并集、排除目的地自身、每个文件读一次/内存替换/写一次，全路径与唯一短名 wikilink 都替换；delete 移除指向已删文档的死链（全路径 / 去 `.md` / 唯一短名三类模式）。
_Avoid_: reference update（模块名 refs.py 不是术语）、ref resolution

**referrer lookup**（`_find_referrers`）：
引用方集合查询：按候选名（全路径 / 去 `.md` / 唯一短名 rglob）查 edges 表，得所有指向目标的 wikilink 来源；conn 为 None 或 edges 表为空（图谱未构建）时回退全库扫描——唯一文档化的 db→fs 依赖。唯一调用方是 reference maintenance；edges 表另两个读方流程 `graph_get_related`（相关文档查询，三个调用点）与 `graph_get_backlinks`（单跳 backlinks）只返回结果、不改写文件。
_Avoid_: backlinks（那是只读单跳查询）、referrer scan

### Agent

**channel policy**（`ChannelPolicy`）：
通道能力声明，`validate_tool_call` 的唯一输入（检查语义，无流程语义）：vault_dir + write_scope（写边界，None=全 vault，只约束 ask 工具）+ report_dir（保护目录，ask 工具禁写）+ gates。三层检查：L1 pydantic schema / L2 语义边界（resolve + vault 内 + 首段非 `.sync`，权威层）/ write gate；首个非 None 即拒。
_Avoid_: permission（工具属性，不是策略）、policy（裸词）

**permission**：
工具类声明的属性（`Tool.permission`）：`allow` = 自动执行，`ask` = 受写边界约束且走确认提示（除非 auto_approve）；gate 层据此属性结构化判定「是否写工具」（fail-closed）。第三态 `always_ask`（「永远人工」）被 10-22 spec 识别为唯一机制缺口，未实现（零消费者，YAGNI）。
_Avoid_: ask/allow（取值词）、权限（裸词）

**auto_approve**：
`AgentSpec` 的运行时流程输入（bool）：跳过确认提示，不跳过检查（10-22 spec 明示其不进 channel policy）；主 agent 默认 False（ask 工具逐个 confirm），bgagent 恒 True（无人值守，拒绝分支保留兼容）；前端开关是显式 defer 的待办。
_Avoid_: 与 permission 混用、自动批准（裸词）

**write gate**（`WriteGate`）：
channel policy 的第三层：域名/通道写规则，read 工具一律 no-op；`VAULT_GATES` + 通道专属 gates 按序执行，首个非 None 即拒。`VAULT_GATES` = vault 不变量，任何通道不可关闭（现为 `SuggestionsGate`：v2.1 D25 禁写守卫——对 `suggestions/` 子树（任意深度）的一切 ask 工具调用（write/edit/move/delete 任意 op）结构性拒绝，fail-closed；v1 文件级校验钩子 `validate_suggestions_write` 已退役，测试钉住其缺席）；通道专属 gate 定义在通道模块、经 `ChannelPolicy.gates` 注入（如 MCP 的 `McpExternalGate`：主区只读，返回协议指引文案）。
_Avoid_: gate（裸词）、check、门禁（裸词）

**write_source**：
`AgentSpec` 携带的整实例来源归因通道：主 agent 默认 `"agent"`，bgagent 用配置 alias（change_log 与 recall log 均记 alias，而非笼统 "agent"）；4 个 ask 工具经 `register_write` 走 write registry（→ change_log.source），search_docs 记 recall log（→ recall_queries.source）。是 agent 实例进入 source 值域的入口。
_Avoid_: source（裸词）、agent source、写来源（裸词）

**agent session**：
主 agent 的一次对话实例，三个职责分属三个主语：`AgentSpec` 声明 agent 是什么（model/tools/limits + `policy()` 派生）；`Agent` 是运行体（spec + runtime + 内部状态，持有 messages / pending confirms / read tracking，外部经 `chat()`/`confirm()` 驱动）；`SessionManager` 管消息状态与并发（内存缓存、miss 从 `sessions` 表恢复——跨重启存活、TTL 30min、持锁会话不驱逐）。runtime（LoopRuntime）决定怎么执行，是确认流程的单点。
_Avoid_: session（裸词）、chat、conversation
