# 安全红线落地（LAN 闸门 / 隐私读边界 / egress 审计）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 兑现 AGENTS.md 安全红线 §5/§6/§7：LAN 硬闸门、`privacy.dirs` 模型面读边界、文档行级 egress 审计。

**Architecture:** 零派生状态——隐私名单是 config 里的目录集，Python 面 `is_privacy()` 谓词、SQL 面 `privacy_clause()` 构造器在查询时现算；`PrivacyConfig`（mtime 缓存）让主服务器与 MCP stdio 两进程共享同一语义。egress 采集复用 runtime 既有的 `last_meta["paths"]` 管道，run 结束按文档逐行落 ledger.db。

**Tech Stack:** Python 3 / FastAPI / SQLite（index.db 可重建缓存 + ledger.db 权威）/ 无构建步骤的 Vue3 CDN 前端。

**Spec:** `docs/superpowers/specs/2026-11-18-security-redlines-impl-design.md`（本计划全部决策的依据，冲突时以 spec 为准）

## Global Constraints

- 测试命令一律 `.venv/bin/pytest`（系统 python 无 pytest）。
- 新代码零第三方依赖（stdlib only）。
- `privacy.dirs: []` 且端点全回环时，全部行为与改动前**逐字节等价**（每任务不得破坏既有测试；test_routes_order 是唯一允许随路由注册同步更新的钉）。
- 路径比较沿用 `_first_segment` 反斜杠规范化语义；大小写语义统一 ASCII 不敏感（spec §2.2）。
- 模型面拒绝文案统一用 `INVISIBLE_MSG` 常量（spec §2.2）。
- UI 文案规范：界面只放判断需要的信息，不出现字段名/端点 URL 段（api_key、path 段永不显示）。
- 每个任务结束时单独 commit，消息前缀 feat/fix/test/docs。

## Review Focus

（spec 未言明但最可能咬人的五类，各任务已含对应钉）

1. 存量 `host: 0.0.0.0` 配置的 **MCP stdio 启动不得被 LAN 闸门误杀**（闸门在 main.py 不在 loader）→ Task 1。
2. FTS 损坏走 `_legacy_rag_search` 降级回退时**隐私目录照常不可见**→ Task 4。
3. `is_privacy`（Python）与 `privacy_clause`（LIKE）在大小写混写上必须同判 → Task 2+4。
4. 深层同名目录不误伤：名单 `diary` 不得命中 `notes/diary/x.md`（首段语义）→ Task 2。
5. 隐私路径经由 `entity_index` 的 source_refs 渲染进 `index/entities.md`（auto 轨无人工闸门）必须被剔除 → Task 5。

---

### Task 1: config 字段 + LAN 硬闸门

**Files:**
- Modify: `server/config/core.py`（ServerConfig 字段 + 解析）
- Modify: `server/main.py`（uvicorn 启动前调闸门）
- Modify: `config.example.yaml`（host 默认 `127.0.0.1` + 注释）
- Test: `tests/test_lan_gate.py`、`tests/test_config.py`（追加）

**Interfaces:**
- Produces: `ServerConfig.lan_exposure: bool`（缺省 False）；`lan_gate_check(host: str, lan_exposure: bool) -> None`（raise `ValueError`，文案含「局域网内任何人可读写 vault、批准提案」与「lan_exposure: true」开法，放 `server/config/core.py`）；`is_loopback(host: str) -> bool`（同文件：`localhost`/`::1`/`127.` 前缀为回环）。

- [ ] **Step 1: 写失败测试**（`tests/test_lan_gate.py`）：`lan_gate_check("0.0.0.0", False)` raises ValueError 且消息含 `lan_exposure`；`lan_gate_check("0.0.0.0", True)` 通过；`is_loopback` 矩阵 `{"localhost","127.0.0.1","127.2.3.4","::1"} == True`、`{"0.0.0.0","192.168.1.5",""} == False`。`tests/test_config.py` 追加：config 无 `lan_exposure` → `cfg.lan_exposure is False`；`lan_exposure: true` → True；**`host: 0.0.0.0` 无 flag 的 config 经 `load_server_config` 不 raise**（loader 无判定 = MCP 不误杀钉）。
- [ ] **Step 2**: `.venv/bin/pytest tests/test_lan_gate.py -q` → FAIL。
- [ ] **Step 3**: 实现字段（`lan_exposure: bool = False`，解析 `server.get("lan_exposure", False)` 真值化）、两函数、`main.py` 在 uvicorn 启动调用处之前 `lan_gate_check(config.host, config.lan_exposure)`（异常原样向上，非零退出）；example 默认改 `127.0.0.1`。
- [ ] **Step 4**: 全绿且 `.venv/bin/pytest -q` 无回归 → commit `feat(security): LAN hard gate — non-loopback host requires explicit lan_exposure (redline 6)`。

### Task 2: `server/privacy.py` 公共设施

**Files:**
- Create: `server/privacy.py`
- Modify: `server/config/core.py`（解析 `privacy: {dirs: [...]}`，校验）
- Test: `tests/test_privacy.py`、`tests/test_config.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 config 模式惯例。
- Produces: `ServerConfig.privacy_dirs: tuple[str, ...]`（加载时校验：拒绝绝对路径/`..`/含 `/`/空串/首段 ∈ {.sync,.trash,suggestions}（含反斜杠形），去重保序，raise ValueError）；`is_privacy(dirs: tuple[str, ...], path: str) -> bool`；`privacy_clause(dirs: tuple[str, ...], col: str) -> tuple[str, list]`（空名单 → `("", [])`）；`INVISIBLE_MSG: str`；`PrivacyConfig(config_path: Path)`：`.dirs` 属性 mtime 失效重读（yaml.safe_load，缺 privacy 节 → `()`）；`detect_privacy_refs(content: str, dirs: tuple[str, ...]) -> list[str]`（`[[目录/...]]` wikilink 与裸路径串命中，按出现序去重）。

- [ ] **Step 1: 写失败测试**：`is_privacy(("diary",), "日记/x")` True（ASCII 大小写：`"DIARY/x"`、`"Diary/x"` 对名单 `diary` True）；反斜杠 `"diary\\x"` True；深层同名 `"notes/diary/x.md"` False；`"diaryx/a.md"` False（前缀不误伤）；`privacy_clause(("日记",), "path")` 生成子句 `LIKE '日记/%' ESCAPE '\'`，参数含转义后的 `%_`（构造 `a%b/c` 名单项断言参数 `a\\%b/%` 形），空名单 → `("", [])`；SQLite 实测钉：内存库表 `t(path)` 插 `diary/a`、`DIARY/b`、`notes/diary/c`，`WHERE <clause>` 只剩 c（ASCII 大小写与首段语义双钉）；`PrivacyConfig` mtime 变更后 `.dirs` 反映新值；`detect_privacy_refs('see [[日记/2026]] and 日记/x.md plus 工作/日记/note.md', ("日记",))` → 前两者、不含第三者；config 校验拒绝清单（每个非法形态一个 parametrize）。
- [ ] **Step 2**: FAIL。
- [ ] **Step 3**: 实现（`_first_segment` 语义在 privacy 模块内自含实现或复用 `server.agent.policy._first_segment`——**复制语义而非跨层 import**，避免 vault/config 层依赖 agent 层）。
- [ ] **Step 4**: 全绿 → commit `feat(privacy): privacy.dirs config, is_privacy/privacy_clause/PrivacyConfig shared primitives`。

### Task 3: gate 体系接入（Policy 感知签名 + PrivacyGate + 读拒绝）

**Files:**
- Modify: `server/agent/policy.py`（WriteGate.check 签名、ChannelPolicy.privacy 字段、PrivacyGate、VAULT_GATES、validate_tool_call 读侧拒绝）
- Modify: `server/agent/tools/tool.py`（ToolContext.privacy 字段）
- Modify: `server/mcp_server.py`、`server/agent/spec.py` 与 `server/bgagent/runner.py`（构造点注入 privacy 引用）
- Test: `tests/test_privacy_gates.py` + 既有用例适配（test_suggestions_validation / test_mcp_gate / test_proposals*）

**Interfaces:**
- Consumes: Task 2 的 `is_privacy`、`INVISIBLE_MSG`、`PrivacyConfig`。
- Produces: `WriteGate.check(self, tool_name: str, args: dict, policy: "ChannelPolicy") -> str | None`（**签名变更**：第三参 vault_dir → policy；SuggestionsGate/McpExternalGate 取 `policy.vault_dir`）；`ChannelPolicy.privacy: PrivacyConfig | None = None`；`PrivacyGate`（name=`privacy`，进 `VAULT_GATES`）：ask 工具 `from_path`/`moves[].from_path` 命中隐私而对应 to 不命中 → 拒绝（措辞含「human move」指引）；`validate_tool_call` 在既有 L2 循环内追加：`not is_ask and is_privacy(dirs, p)` → `f"Error: {INVISIBLE_MSG}"`（read_doc/get_related_docs/list_files/recent_changes 的路径参数即读拒绝面；`is_ask` 路径**不**按隐私拒绝）；`ToolContext.privacy: PrivacyConfig | None = None`。

- [ ] **Step 1: 写失败测试**：以 `ChannelPolicy(vault_dir, privacy=PrivacyConfig(tmp_cfg))`（dirs=日记）驱动 `validate_tool_call`：`read_doc{"path":"日记/a.md"}` → 错误含 INVISIBLE_MSG 子串；`write_doc` 同路径 → None（写进隐私区允许）；`edit_doc` 同路径 → None；`move_docs{"moves":[{"from_path":"日记/a.md","to_path":"notes/a.md"}]}` → 拒绝（移出）；`from_path":"notes/a.md","to_path":"日记/a.md"` → None（移入）；`get_related_docs` 路径命中 → 拒绝；privacy=None 时以上全部 None（等价钉）。签名迁移适配钉：三 gate 的既有用例仍绿。
- [ ] **Step 2**: FAIL。
- [ ] **Step 3**: 实现（`_extract_paths` 已有 from/to 提取，move 配对规则：ask 工具内逐 args 键对 from→to，`from_path`+`to_path` 成对，`moves[i]` 内成对）。构造点注入：`spec.py` 的 policy 工厂与 ToolContext 构造各带 privacy；mcp_server `main()` 里 `privacy = PrivacyConfig(config_path)`（`_find_config()` 返回值），传 `ChannelPolicy(..., privacy=privacy)` 与 ToolContext。
- [ ] **Step 4**: `.venv/bin/pytest -q` 全绿 → commit `feat(privacy): PrivacyGate + read-side privacy refusal in validate_tool_call (policy-aware gate signature)`。

### Task 4: 检索/关系/列表/变更四收口面 + 降级回退

**Files:**
- Modify: `server/vault/db/segments_sql.py`（segments_search 加 `privacy_dirs` 参）
- Modify: `server/ai/search.py`（rag_search/_legacy_rag_search 加参、tag 轴过滤）
- Modify: `server/vault/db/graph_sql.py`（graph_get_related/graph_get_related_with_machine 加参、BFS 前沿跳过 + 输出双层过滤）
- Modify: `server/agent/tools/{search_docs,get_related_docs,list_files,recent_changes}.py`（传参/过滤，recent_changes 顺手补 `last_meta["paths"]`）
- Modify: `server/mcp_server.py`（`_mcp_search` 与 `get_related_docs` 传 privacy；**get_related_docs 补 `validate_tool_call` 挂点**）
- Test: `tests/test_privacy_surfaces.py`

**Interfaces:**
- Consumes: Task 2 `privacy_clause/is_privacy`；Task 3 `ctx.privacy`。
- Produces: `rag_search(..., privacy_dirs: tuple[str, ...] = ())`；`segments_search(..., privacy_dirs=())`（SQL WHERE 注入）；`_legacy_rag_search(vault_dir, query, top_k, privacy_dirs=())`；`graph_get_related(conn, path, hops=1, privacy_dirs=())`（隐私邻居不记录、不进前沿）；`graph_get_related_with_machine(..., privacy_dirs=())`（user+machine 双层过滤）；`recent_changes` 成功后 `ctx.last_meta = {"paths": [...]}`（path+from_path 值，工具层过滤隐私行）。Web 侧调用点（routes/recall.py、/api/graph/related、/api/memory）一律**不传**（缺省空 = 零变化）。

- [ ] **Step 1: 写失败测试**（建两文件 vault：`日记/a.md`（含独特词 zebrafish）与 `notes/b.md`）：rag_search 传 `privacy_dirs=("日记",)` 零命中、不传命中（既有索引 fixture 上跑）；**降级钉**：把 index.db 改名逼进 `_legacy_rag_search`，带名单仍零命中；大小写钉（名单 `DIARY` vs 路径 `diary/a.md`，SQL 与 legacy 两路同判）；graph 两函数带名单时 user/machine 两层均无隐私路径（预置 relatedness 边一端入隐私）；list_files、recent_changes 工具结果不含隐私路径；recent_changes 成功时 last_meta.paths 已置；**MCP get_related_docs 过 validate**（privacy 名单下传隐私路径返回 INVISIBLE_MSG——直接测注册前的 handler 函数体或走 MCP_POLICY）；write_doc/edit_doc 工具层：非隐私目标 + content 含 `[[日记/x]]` → 结果串含 advisory，隐私区内目标 → 无 advisory，且两者保存均成功。
- [ ] **Step 2**: FAIL。
- [ ] **Step 3**: 实现。tag 轴：`graph_docs_for_tags` 结果 dict 在融合前按键过滤。mcp `_mcp_search` 加 privacy_dirs 透传。另（spec §2.6）：`write_doc`/`edit_doc` 工具结果串尾部追加 advisory——args 的 path 或 content 命中 `detect_privacy_refs(content, dirs)` 时附 `\nwarning: content references private paths: ...`（目标在隐私区内的合法写入不警告；advisory 不改变保存语义）。
- [ ] **Step 4**: 全绿（Web 搜索/graph 路由既有用例零改动即守恒钉）→ commit `feat(privacy): model-surface filters for search/related/list/recent + degraded RAG fallback`。

### Task 5: executor 两合并收口

**Files:**
- Modify: `server/bgagent/executor.py`
- Test: `tests/test_executor_privacy.py`（并入既有 executor fixture）

**Interfaces:**
- Consumes: Task 2 `is_privacy`。
- Produces: `run_executor(vault_dir, conn, privacy_dirs: tuple[str, ...] = ())`（默认空 → 逐字节等价；scheduler 调用点从 config 传入）；`_merge_entity_index`：source_refs 隐私项整行剔除，全剔 → `_MergeFailed("source_refs all privacy-scoped")`；`_merge_relatedness`：source/target 任一命中 → `_MergeFailed`。

- [ ] **Step 1: 写失败测试**：adopted 的 entity_index 提案 source_refs=[日记/a.md, notes/b.md] → entities.md 只含 `- source: notes/b.md`；全隐私 → 记录 failed 且 reason 含 privacy；relatedness 边端点入隐私 → failed；privacy_dirs=() 时以上行为与旧完全一致。
- [ ] **Step 2**: FAIL。
- [ ] **Step 3**: 实现 + `server/bgagent/scheduler.py` 调用点接线（scheduler 持有 config 来源则透传；若无则经 start_background_agents 参数链传入）。
- [ ] **Step 4**: 全绿 → commit `feat(privacy): executor merge guards for entity_index source refs and relatedness endpoints`。

### Task 6: egress 表与写面

**Files:**
- Modify: `server/vault/db/schema.py`（`_LEDGER_DDL` 追加 egress_log 表 + 3 索引，spec §3.1 原文）
- Create: `server/vault/db/egress_sql.py`（写面，`_ledger_lock` 内部持锁，card-5 模式）
- Create: `server/egress.py`（policy/shaping 层）
- Test: `tests/test_egress.py`

**Interfaces:**
- Produces: `egress_sql.insert_touch(conn, ts, channel, actor, run_id, endpoint_host, model, path, via)`（锁内 INSERT+commit）；`egress_sql.prune(conn, cutoff_ts) -> int`；`egress.parse_endpoint(base_url: str | None) -> str`（host[:port]；None/空 → `"(provider default)"`；永不抛）；`egress.EgressTarget`（dataclass：channel/actor/run_id/endpoint_host/model_id）；`egress.should_record(target) -> bool`（**仅纯回环 host 才 False**；`"(provider default)"` 视作云 True）；`egress.flush_touches(conn, privacy_dirs, target, touched: dict[str, str]) -> None`（fail-open try/except 全吞 + logger.exception；逐路径 `is_privacy` 跳（双保险））。

- [ ] **Step 1: 写失败测试**：fresh ledger fixture 上建表成功（init_ledger_db 后 `SELECT count(*) FROM egress_log` 可执行）；insert 两行 + prune(cutoff) 只删旧行；`parse_endpoint("https://api.deepseek.com/v1") == "api.deepseek.com"`、含端口、None → `"(provider default)"`；`should_record`（回环 False / 云 True / default True）；`flush_touches` 对隐私路径跳过、异常 conn（closed）不抛出且日志留痕、成功后行数=len(非隐私 touched)。
- [ ] **Step 2**: FAIL。
- [ ] **Step 3**: 实现（DDL 字符串照 spec §3.1，`%s` 前缀槽位惯例照 _LEDGER_DDL 其他条目）。
- [ ] **Step 4**: 全绿 → commit `feat(egress): egress_log ledger table + write face + endpoint helpers`。

### Task 7: runtime flush 接线 + 两通道 target 构造 + 启动播报

**Files:**
- Modify: `server/agent/runtime.py`（LoopRuntime.run 的 for 循环包 try/finally 收口 flush；循环内 `touched: dict[str,str]` 从 `meta.get("paths")` + 工具名映射 via 采集）
- Modify: `server/agent/spec.py`（AgentSpec 加 `egress: EgressTarget | None = None`）
- Modify: `server/bgagent/runner.py`（spec 构造：`EgressTarget("bgagent", cfg.alias, run_id, parse_endpoint(cfg.model.base_url), cfg.model.model_id)`）
- Modify: `server/routes/agent.py`（交互 spec 构造同款 channel="agent"、actor="agent"、run_id=""）
- Modify: `server/main.py`（启动日志播报：`log_model_endpoints(config) -> list[str]`——主 AI + 各 bgagent 逐端点「云端/本地」分类行，返回行供测试）
- Test: `tests/test_egress_runtime.py`

**Interfaces:**
- Consumes: Task 6 全部；runtime 既有 `meta["paths"]`（recent_changes 已由 Task 4 补齐）。
- Produces: via 映射 `{"read_doc":"read","search_docs":"search","get_related_docs":"related","recent_changes":"recent"}`；循环**全部出口**（final/error×2/round_limit）经 finally flush 恰好一次；`log_model_endpoints(config) -> list[str]`（main.py 或 server/egress.py 实现，main 调用）。

- [ ] **Step 1: 写失败测试**：用既有 LoopRuntime 测试替身（MagicMock 流）驱动一轮含 search_docs+read_doc 成功结果（meta.paths 各带），spec.egress 指向 fake conn：断言 flush 行集 = 触达文档、via 各正确、错误结果（无 meta）零行；同一路径两工具触达 → 一行（首触 via）；异常退出路径（error 事件）仍 flush；`egress=None` 零副作用；`log_model_endpoints`（云端点 config → 行含「云端」与 host，Ollama config → 「本地」，key 不出现）。
- [ ] **Step 2**: FAIL。
- [ ] **Step 3**: 实现（touched 采集放 `meta is not None` 分支、error 结果天然无 meta；bgagent/agent 两通道 target 仅 `should_record` 为 True 才挂 spec，本地端点恒 None = 表恒空）。
- [ ] **Step 4**: 全绿 → commit `feat(egress): run-scoped egress flush in LoopRuntime + bgagent/agent targets + startup endpoint broadcast`。

### Task 8: `/api/egress` 路由 + status + executor 周期清理

**Files:**
- Create: `server/routes/egress.py`
- Modify: `server/routes/__init__.py`（注册，recall 之后）
- Modify: `tests/test_routes_order.py`（钉同步更新）
- Modify: `server/bgagent/executor.py`（gc 之后 `egress_sql.prune(conn, now - 90*86400)`，best-effort try/except）
- Test: `tests/test_egress_route.py`

**Interfaces:**
- Consumes: `egress_sql` 表；`Task 1 is_loopback`（status 端点分类）。
- Produces: `GET /api/egress?path=&host=&run_id=&limit=50` → `{"rows":[{ts,channel,actor,run_id,endpoint_host,model,path,via}]}`（ts 倒序，limit 上限 500）；`GET /api/egress/status` → `{"cloud_endpoints":[host...], "last_ts": float|None}`（cloud 集 = config ai+bgagents 非回环端点）。

- [ ] **Step 1: 写失败测试**（TestClient 既有 fixture）：插三行（不同 path/host/ts）→ 无参倒序 50、`path=` 精确、`run_id=` join、limit 生效；status 对一份云+本地混合 config 返回仅云 host；90 天清理钉：run_executor 一轮后旧行消失、新行在、prune 抛异常不影响 merged 计数。
- [ ] **Step 2**: FAIL。
- [ ] **Step 3**: 实现 + 路由顺序测试期望更新（仅此文件允许改既有钉）。
- [ ] **Step 4**: 全绿 → commit `feat(egress): /api/egress read surface + status + executor-cycle retention sweep`。

### Task 9: Web 面标注回传 + 保存引用警告

**Files:**
- Modify: `server/routes/console.py`（`/api/docs` 负载加 `privacy_dirs` 数组；`PUT /api/docs/{path}` 成功响应加 `privacy_refs: [str]`）
- Test: `tests/test_privacy_web_surface.py`

**Interfaces:**
- Consumes: Task 2 `detect_privacy_refs`、ctx 持有的 `PrivacyConfig`（WebContext 构造点：`create_app` 加 `privacy` 成员，main.py 注入）。
- Produces: `/api/docs` 响应新增顶层 `"privacy_dirs": [...]`（**仅回显，不改变 files/dirs 集**）；`PUT /api/docs/{path}` 响应 `{"ok": true, "privacy_refs": [...]}`（目标文件本身在隐私区 → 恒空；非隐私文件引用隐私目录 → 列表）。

- [ ] **Step 1: 写失败测试**：名单非空时 `/api/docs` 的 files/dirs 集与名单为空时**完全一致**且多出 privacy_dirs 数组（Web 面守恒钉，防误伤）；PUT 保存含 `[[日记/x]]` 的非隐私文件 → privacy_refs 含该 wikilink；保存进隐私区 → 恒空；名单空 → 数组/键行为不变。
- [ ] **Step 2**: FAIL。
- [ ] **Step 3**: 实现（`create_app(..., privacy=PrivacyConfig(...))` → `ctx.privacy`；main.py 注入）。
- [ ] **Step 4**: 全绿（含同步 `/api/sync` 既有用例零变化 = 同步面不碰钉）→ commit `feat(privacy): web annotation payload + save-time privacy reference warnings (display-only)`。

### Task 10: 前端三件套 + 警告/监控 UI（designer 车道）

**Files:**
- Modify: `server/templates/js/`（文件树组件、编辑器头、搜索结果行、保存提示、监控页 egress 区——具体文件由 @designer 定位）
- Test: 无自动化钉（目视确认）；既有 `test_web_ui_audit.py` 若因文案更新则同步。

- [ ] **Step 1**: 派 @designer：文件树隐私目录小锁+muted 弱色调（子文件继承、折叠可见）；编辑器头「锁 模型不可见」徽标；Web 搜索结果隐私命中挂锁；PUT 响应 `privacy_refs` 非空时 toast（文案 spec §2.6 原文）；监控页「当前出域端点：N 个 · 更新于 HH:mm」+ 详情表（/api/egress，列：时间/通道/actor/model/文档）。数据源：`/api/docs` 的 privacy_dirs 客户端首段匹配。
- [ ] **Step 2**: 本地起服务（`.venv/bin/python -m server.main config.yaml`，配含隐私名单与云端点样例），浏览器目视三件套 + toast + egress 表；对照 Web Interface Guidelines 自查。
- [ ] **Step 3**: `.venv/bin/pytest -q` 全绿 → commit `feat(ui): privacy markers (tree/editor/search), save-reference toast, egress console panel`（**用户目视确认前不得 commit**——既有前端规则）。

---

## 并行性与依赖

T1、T2 独立可并行；T3 依赖 T2；T4/T5/T9 依赖 T3（T5 仅需 T2 的谓词，可提前并行）；T6 独立可与 T1-T3 并行；T7 依赖 T6+T4（recent_changes paths）；T8 依赖 T6；T10 依赖 T9/T8 的 API。**推荐两泳道**：道 A＝T1→T3→T4→T5→T9→T10；道 B＝T2→T6→T7→T8（T2 完成后交道 A）。写域无重叠（文件级检查：T3 与 T7 同触 runtime/spec——串行，T7 在 T3 后）。
