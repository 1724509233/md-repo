# 设置页（config.yaml 页面化编辑）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Web 控制台新增「设置」页，页面化编辑 config.yaml 的 ai / background_agents / notifications / privacy / log 五段，保存即校验、即落盘（ruamel 注释保真）、分级热生效。

**Architecture:** 新增 `/api/settings` 路由域（GET 掩码回显 + PUT 字段级写回，revision 乐观并发 + 路由锁串行化）；bgagent 调度器改造为 supervisor+slot 结构（圈首读 cfg、run 用旧配置跑完）；ai 段经 agent_cache 代际戳分级生效；前端照监控页整页模式新增 SettingsPage.js。

**Tech Stack:** FastAPI + ruamel.yaml（新增依赖）+ asyncio；Vue3 CDN 无构建 SPA。

**Spec:** `docs/superpowers/specs/2026-11-22-settings-page-design.md`（本计划全部决策的法源，冲突时 spec 赢）

## Global Constraints

- 解释器：`D:\anaconda3\python.exe`（anaconda base，已装全部依赖，`ruamel.yaml 0.18.17` 在场）；测试命令一律 `D:\anaconda3\python.exe -m pytest <路径> -v`。
- 依赖唯一口径 `pyproject.toml`（无 requirements 文件）；ruamel.yaml 加进 `dependencies` 数组。
- 路由表序由 `tests/test_routes_order.py` 快照钉死；`settings` 域追加在 `ROUTE_MODULES` **末尾**（trash 之后），`EXPECTED` 同步尾部追加。
- 前端红线（AGENTS.md）：聚焦必须 `focus({preventScroll:true})`；禁 `h-screen/100vh` 壳高；禁 `alert(`；内联 svg 一律 `aria-hidden="true"`；纯图标钮必须 `aria-label`；异步区 `aria-live`；动效只用 transform/opacity（reduced-motion 降级）。
- Windows：测试 fixture 写文件用 `write_bytes`；`os.replace` 目标可能被瞬时句柄挡住 → 退避重试语义（spec §4.2.5）。
- 提交信息风格照仓库中文 conventional commits；每个 Task 末尾提交一次。
- 密钥铁律：api_key 明文永不出 GET / 422 / 日志。

## Review Focus

（每条都要落到标注任务的测试里）

1. **run 进行中保存配置**（agent 会话与 bgagent 皆然）→ 不丢消息、不双跑、旧配置跑完（Task 6、3）。
2. **页面开着时手改 yaml** → 保存撞 409，手改内容一个字不被覆盖（Task 5）。
3. **`api_key` 三态**：缺省=保持、`null`=保持、`""`=清除；前端未触碰字段绝不进 payload（Task 5、9）。
4. **未知/未建模键（escalation、页面外 section）与注释、CRLF** 保存后逐字节在场（Task 5）。
5. **禁用→启用、改名、stop 竞态**：无静默失效、无复活任务、锁跨重建复用（Task 2、3）。
6. **忙环**：连开两次保存后 cron 环不得空转（M2 clear 时序钉）（Task 1）。

---

### Task 1: scheduler — AgentSlot 与圈首时序（cron 环 slot 化）

**Files:**
- Modify: `server/bgagent/scheduler.py`
- Test: `tests/test_bgagent_scheduler.py`（扩展）

**Interfaces:**
- Produces: `class AgentSlot: cfg: BackgroundAgentConfig; retired: bool; reload: asyncio.Event`；`_agent_loop(slot, sup, vault, lock, hooks, run_fn, conn)`——`sup` 本 Task 允许是 duck-typed 最小对象（Task 2 换成真 supervisor），别名集一律圈首 `sup.current_aliases()` 现取（Task 1 里先用最小实现，Task 2 接管）；圈首时序 = `reload.clear()` → 读 cfg/retired/别名集 → `await asyncio.wait_for(slot.reload.wait(), delay)`（TimeoutError=到点）。`_handle_tick` 签名不变（调用时刻持 cfg）。模块头注补「apply 同步 + 同事件循环」承重契约。
- Consumes: 既有 `_next_fire` / `run_once`。

- [ ] **Step 1**: 新测试 `test_agent_loop_reads_slot_cfg_each_cycle`：slot.cfg 换成 cron 不同的第二个 cfg，`reload.set()` 后环在亚秒回圈顶并按新 cron 重锚（fake clock 或直接断 `_next_fire` 入参变化——照现有测试的 monkeypatch 手法）。
- [ ] **Step 2**: `test_reload_event_cleared_no_busy_loop`：`reload.set()` 生效后（环消费一次），后续圈 sleep 恢复为满 delay（用「一次 set 只打断一拍」的计数断言），无忙环。
- [ ] **Step 3**: `test_run_in_flight_keeps_old_cfg`：环进入 `_handle_tick`（mock 长 run）期间换 `slot.cfg`，run 收到的是旧 cfg 对象；结束后下一拍读新 cfg。
- [ ] **Step 4**: 实现 `AgentSlot` + `_agent_loop` slot 化（M2 时序：clear → 读 → 进 wait 之间零 await）。`_armed_loop` 同步改收 slot 但**只圈首读 cfg，不接 reload**（S1）。
- [ ] **Step 5**: `test_bgagent_scheduler.py` 全绿（既有用例经 `start_background_agents` 过渡期可先内部构造 slot）。
- [ ] **Step 6**: 提交 `feat(bgagent): AgentSlot 圈首时序——reload clear→读 cfg→wait 零 await，进行中 run 持旧配置`。

### Task 2: scheduler — BgSupervisor.apply 全分支

**Files:**
- Modify: `server/bgagent/scheduler.py`
- Test: `tests/test_bgagent_scheduler.py`

**Interfaces:**
- Produces: `class BgSupervisor(vault_dir: Path, *, hooks, run_fn, db_conn=None, ledger_conn=None, executor_fn=None, privacy=None)`；方法 `apply(agents: list[BackgroundAgentConfig]) -> None`（同步；`Runtime` 置死后 raise `RuntimeError`）、`tasks() -> list[asyncio.Task]`（live 全集，retired 未退者在场直到 done，done-callback 摘除）、`shutdown() -> None`（cancel 全部 + 死标志）、`current_aliases() -> set[str]`（活别名集，循环每圈现取）。apply 内部：per-alias `asyncio.Lock` 由 supervisor 持有跨重建复用；registry 收录全部别名（enabled:false 出生即 retired）；分支表 = 存续换 cfg+set / **retired 复活复位重建** / 删除或禁用 retire / 改名=删+增 / executor 增缓退 / **两侧 self-exclusion 同步**（`events.set_filter("change", …)` 从活别名集重算；`_handle_tick`/`_armed_check` 的 `all_aliases` 改从 `sup.current_aliases()` 现取，废 spawn 闭包）。
- Consumes: Task 1 的 AgentSlot/环。

- [ ] **Step 1**: 失败测试组（M3/M4）：`test_apply_reenable_resumes`（禁用→启用即时生效）、`test_apply_no_double_run_when_reenable_mid_run`（run 中禁用→启用：锁复用，无双跑）、`test_scan_side_sees_new_alias_immediately`（加别名后存量 agent 的 self_exclusion 立即含新别名——断 `_armed_check` 收到的 all_aliases 或 `events` 过滤器状态）、`test_executor_add_remove`。
- [ ] **Step 2**: 实现 BgSupervisor 与 apply 分支表；`start_background_agents(...)` 改薄封装（建 supervisor→apply→返回 `tasks()`，签名不动，docstring 标「测试兼容面；生产路径直接构造 supervisor」）。
- [ ] **Step 3**: 全文件测试绿 + `test_events.py`/`test_trigger.py` 等既有调度相关测试绿。
- [ ] **Step 4**: 提交 `feat(bgagent): BgSupervisor.apply——re-enable/锁归属/扫描侧活别名集/双过滤器同步`。

### Task 3: scheduler 生命周期 + notify cap 热调

**Files:**
- Modify: `server/bgagent/scheduler.py`、`server/bgagent/notify.py`、`server/main.py`（仅 build_runtime 的 supervisor 替换与 Runtime 字段）
- Test: `tests/test_bgagent_scheduler.py`、`tests/test_runtime.py`

**Interfaces:**
- Produces: `Runtime` 增字段 `supervisor: BgSupervisor`、`notif_hook: GuardedNotificationHook`（stop() 改为 `supervisor.shutdown()`）；`GuardedNotificationHook.set_daily_cap(n: int)`；`build_runtime` 内 `events.set_filter("change", …)` 初始行**删除**（apply 是过滤器唯一写入点，单源）。
- Consumes: Task 2。

- [ ] **Step 1**: 失败测试：`test_shutdown_covers_retiring_task`（retired 未完任务被 stop cancel/await；死后 `apply()` raise）、`test_set_daily_cap_hot`（notify 测试内直接调 setter 后 cap 行为变化）。
- [ ] **Step 2**: 实现 setter；main.py：`build_runtime` 建 supervisor（初始 `apply(bg_agents)`），`Runtime.bg_tasks` 保留但由 `supervisor.tasks()` 提供语义；stop 走 shutdown。
- [ ] **Step 3**: `test_runtime.py` + 全量 bgagent 相关测试绿。
- [ ] **Step 4**: 提交 `feat(bgagent): supervisor 生命周期与通知 cap 热调——stop 全覆盖，死后 apply 拒绝`。

### Task 4: config/doc_io — 文档读写底座

**Files:**
- Create: `server/config/doc_io.py`
- Test: `tests/test_settings_doc_io.py`

**Interfaces:**
- Produces（全在 doc_io，leaf：只 import 数据类与 stdlib+ruamel，不碰 routes/main）：
  - `revision_of(path: Path) -> str` → `"sha256:<hex>"`（文件字节哈希）
  - `open_roundtrip(path) -> tuple[YAML, Any]`（`YAML()`；`typ="rt"`、`preserve_quotes=True`；读写 newline 保真：读 `path.read_bytes` → `load`，dump 进 bytes 缓冲）
  - `save_doc(yaml_obj, doc, target: Path) -> str`：dump 到 **target 同目录**临时文件（`tempfile.mkstemp(dir=target.parent, prefix=".config-", suffix=".yaml.tmp")`）→ `os.replace`，PermissionError 退避重试 ≤2 → 成功返回新 `revision_of(target)`；全败 raise `SettingsWriteError(tmp_path)`（**tmp 必须清掉**）
  - `backup_file(path: Path) -> Path`（`config.yaml.bak-YYYYmmdd-HHMMSS-ffffff`，滚动留 3）
  - `mask_api_key(key: str | None) -> dict` → `{"set": bool, "mask": str | None}`；`set`=key 非空；`mask`= `f"{key[:4]}-…{key[-4:]}"`（`len(key) < 8` → mask=None）
  - `bgagent_to_payload(entry: dict, vault: Path) -> dict`：raw 条目直通 + `target_dir` 反归一 vault 相对 posix；`report_dir` 等于缺省 `<target_dir>/.reports/<alias>` 时删键；`model.api_key` 换 `mask_api_key` 形态
  - `payload_to_bgagent_entry(payload: dict, old_entry: dict | None) -> dict`：写回用；`model.api_key` 缺省/`null` → 取 `old_entry` 原明文（无 old → 不写键）；`""` → 写空串
- Consumes: 无。

- [ ] **Step 1**: 失败测试：注释+CRLF 文件 `open_roundtrip`→改一个标量→`save_doc` → 注释与 CRLF 在场、目标值已变、返回 revision 与文件哈希一致；`backup_file` 双调用不同名（微秒尾）且第 4 份时最旧被删；`mask_api_key` 三例（长 key/短 key/None）；`bgagent_to_payload` 反归一与 report_dir 缺省省略；`payload_to_bgagent_entry` key 三态。
- [ ] **Step 2**: `save_doc` 的 PermissionError 重试测试（monkeypatch `os.replace` 前两次抛 PermissionError 第三次成 → 成功；三次全抛 → `SettingsWriteError` 且无 tmp 残留、目标未动）。
- [ ] **Step 3**: 实现 doc_io；测试绿。
- [ ] **Step 4**: `pyproject.toml` `dependencies` 追加 `"ruamel.yaml>=0.17"`。
- [ ] **Step 5**: 提交 `feat(config): doc_io——round-trip 读写/备份/掩码/路径反归一底座`。

### Task 5: /api/settings 路由（GET + PUT 全语义）

**Files:**
- Create: `server/routes/settings.py`
- Modify: `server/routes/__init__.py`（WebContext 增 `settings: SettingsDeps | None = None`、`settings_gen: int = 0`；定义 `@dataclass SettingsDeps: config_path: Path; runtime: object | None = None`——runtime 槽由 main 填，duck-typed 属性：`supervisor` / `notif_hook` / `root_logger` / `file_handler` / `privacy`；`ROUTE_MODULES` 末尾 + `settings`）
- Modify: `server/api.py`（`create_app(..., settings=None)` 传入 WebContext）
- Test: `tests/test_settings_routes.py`（新建；`create_app` 直调 + 真临时 config 文件，runtime 槽全 mock）

**Interfaces:**
- Produces: `GET /api/settings`、`PUT /api/settings`（形状=spec §4.1/§4.2 逐字段）；模块内 `_put_lock: asyncio.Lock`（register 时建）。GET：doc_io 读 raw doc → ai 段直通（api_key→mask，段缺位→null）+ background_agents 逐条 `bgagent_to_payload` + notifications/privacy/log 直通（缺省补默认值显示）+ `_meta`（tools=`TOOL_REGISTRY` 键排序、verbs=`VALID_TYPES` 排序、restart_fields=`["log.dir"]` 常量）。PUT：①锁内 revision 守卫（409 `{current_revision}`）②密钥回填（`payload_to_bgagent_entry`；`ai: null`=删段）③字段级写回 raw doc 节点④tmp=同目录 + 全量加载器校验（`load_server_config`/`load_notifications`/`load_background_agents(tmp, ctx.vault_dir)`/`validate_tool_whitelists`；422 `{errors:[{message, section}]}`，消息含 alias 定位前缀=契约）⑤`backup_file`⑥`save_doc`（500 未保存语义）⑦apply 编排（§5 顺序：`ctx.ai_config` 替换→`supervisor.apply(loaded)`→`ctx.bg_targets=_caps(loaded)`→**`ctx.settings_gen += 1`**→`notif_hook.set_daily_cap`→`root_logger.setLevel` + `file_handler.backupCount=`→egress 端点集变化则重跑 `log_model_endpoints`）→ 200 `{revision, applied, restart_needed}`；apply 段抛错 → 500 `{saved: true, error}` 且**不反滚文件**。`ctx.settings is None` 或 `runtime is None` → 503。
- Consumes: Task 2/3 supervisor、Task 4 doc_io。

- [ ] **Step 1**: GET 测试（形状/掩码/null 段/`_meta`）+ 503 两态 + 路由序快照更新（`test_routes_order.py` EXPECTED 尾部 + `GET/PUT /api/settings` 两行）。
- [ ] **Step 2**: PUT 成功路径测试：落盘内容字段级正确、注释/escalation/未知键/server 段逐字节在场（含 CRLF 样本）、备份生成、revision 更新、`applied`/`restart_needed`（改 log.dir → `["log.dir"]`）。
- [ ] **Step 3**: PUT 失败路径测试（Review Focus 1/2/3/4 落钉）：422 零落盘零备份 + 消息不含 key 值；409（手改场景：GET 后直接 `write_bytes` 改文件）；`api_key` 三态；`ai: null` 删段。
- [ ] **Step 4**: 热生效编排测试：`ctx.ai_config` 换新、`supervisor.apply` 收到加载后的 cfg、`ctx.bg_targets` 更新、`ctx.settings_gen` ++、hook/logger/handler 现场变更（真 `logging.getLogger("t-settings")` + 真 TimedRotatingFileHandler 进 tmp 文件）、egress caplog（改 base_url → `md-vault.egress` 新行）、**privacy.dirs 写盘即反映**（PrivacyConfig 指向 tmp config，保存后 `.dirs` 立即新值）。apply 失败 → 500 `{saved:true}` 且文件是新内容未反滚（supervisor mock 抛）。
- [ ] **Step 5**: `test_routes_order.py` 全绿（顺序冻结检查）+ 新文件全绿。
- [ ] **Step 6**: 提交 `feat(routes): /api/settings——GET 掩码回显 + PUT 校验即启动/字段级写回/备份/编排热生效`。

### Task 6: agent_cache 代际 + main 接线

**Files:**
- Modify: `server/routes/agent.py`（agent_cache 值改 `(agent, gen)`；`_get_agent` 代际比对重建）
- Modify: `server/main.py`（`create_app(..., settings=deps)`；`SettingsDeps(config_path, runtime=None)`；`_run` 里 `build_runtime` 返回后填 `deps.runtime = SimpleNamespace(supervisor=rt.supervisor, notif_hook=rt.notif_hook, root_logger=root_logger, file_handler=file_handler, privacy=privacy)`）
- Test: `tests/test_settings_hotapply.py`（新建）、`tests/test_agent_sessions.py`（防回归即可）

**Interfaces:**
- Consumes: Task 5 的 `ctx.settings_gen` / `SettingsDeps`；Task 3 的 `rt.supervisor` / `rt.notif_hook`。
- Produces: `_get_agent` 语义——缓存条目 gen 落后且**不忙**（复用 `_evict_stale_agents` 同款守卫：不持锁、无 pending_confirm）→ 原位重建（同 sid、messages 迁移照 `_evict` 重建路径）；忙 → 原样返回。**任何路径不清空缓存、不从外部 pop**（spec M1 红线）。

- [ ] **Step 1**: 失败测试（三态 + 丢消息钉）：`test_new_session_sees_new_ai_config`、`test_idle_session_rebuilt_on_next_dispatch`、`test_busy_session_keeps_old_spec_and_zero_message_loss`（run 中翻 `settings_gen`，断本轮完成消息集完整 + spec 未换、下轮新 spec）。
- [ ] **Step 2**: 实现 `_get_agent` 代际比对（`ctx.settings_gen` 读自 WebContext；cache dict 值升 tuple 波及点全改）。
- [ ] **Step 3**: main.py 接线 + 冒烟：起 app 级测试断 `ctx.settings.runtime.supervisor` 在场（build_runtime 直调路径）。
- [ ] **Step 4**: 相关测试 + 全量回归绿。
- [ ] **Step 5**: 提交 `feat(settings): agent 缓存代际失效 + main 接线 SettingsDeps`。

### Task 7: 前端底座——api.js + 整页模式

**Files:**
- Modify: `server/templates/js/api.js`（`getSettings()`；`putSettings(body)`——**非 2xx 不抛**，返回 `{status, data}`，网络失败才 throw）
- Modify: `server/templates/js/app.js`（`state.settingsPage`；`openSettingsPage()`/`exitSettingsPage()` 互斥三态（收起 monitoring 与 L0 各态，照 `openMonitorPage` 函数体先例）；Escape 链插入 `else if (state.settingsPage) exitSettingsPage()`（位置与 monitoring 同级）；`applyInitialHash` 支持 `#settings` → openSettingsPage，`exitSettingsPage` 时 `history.replaceState(null,'',location.pathname)` 清 hash；onMounted 末 `window.openSettingsPage = openSettingsPage`；`app.component('SettingsPage', SettingsPage)` + import；模板挂 `<SettingsPage v-show="state.settingsPage" @back="exitSettingsPage()" @open-doc="openDoc" />`，文档区 v-show 条件加 `&& !state.settingsPage`）
- Modify: `server/templates/js/components/MonitorPage.js`（`mp-head` 加「设置」钮，`emits` 增 `settings`，`@click="$emit('settings')"`；app.js 模板给 MonitorPage 加 `@settings="openSettingsPage()"`）
- Test: `tests/test_web_ui_audit.py` 跑绿即可（无新钉）

**Interfaces:**
- Produces: `SettingsPage` 组件契约——props 无；`inject('state')/inject('api')`；emits `['back', 'open-doc']`；挂载点与 `state.settingsPage` 布尔。

- [ ] **Step 1**: 实现 api.js 两函数 + app.js 页模式 + MonitorPage 入口。
- [ ] **Step 2**: 空壳 SettingsPage.js（`.mwrap/.m-scroll/.mpage` 骨架复用 + `st-` 前缀类族 + mp-enter/mp-out 同款转场，页头返回钮照 `mp-back`）。
- [ ] **Step 3**: `test_web_ui_audit.py` 绿 + 手工冒烟（起服务点开关互斥、Escape、`#settings` F5 恢复）。
- [ ] **Step 4**: 提交 `feat(web): 设置页整页模式与入口——state.settingsPage 互斥/深链/转场`。

### Task 8: SettingsPage — 主 agent + 杂项段

**Files:**
- Modify: `server/templates/js/components/SettingsPage.js`
- Test: `tests/test_web_ui_audit.py` 全绿 + 点击验证（Task 10）

**Interfaces:**
- Produces: 页面内 `form`（GET payload 的深拷贝）+ dirty 追踪（api_key 字段「未触碰 ⇒ payload 省略该键」的实现：`touched` 集合）。保存流程（本 Task 建好、Task 9 共用）：PUT `{form + revision}` → 200 更新 revision + 生效报告条 / 409 冲突条（「重载」按钮重新 GET）/ 422 页顶告警条（errors[].message 原文）。

- [ ] **Step 1**: 分段导航（桌面左列锚 / ≤639 分段钮照 `mode-btn` 先例）+「主 agent」表单（api_key 密码型 + 「已设置（留空保持不变）」placeholder + 「清除」小钮发 `""`）+「杂项」表单（daily_cap/retention 数字、log_level 下拉、privacy.dirs 标签增删）。
- [ ] **Step 2**: 保存全部 + 三态反馈条（applied/restart_needed 展示遵守 AGENTS 文案纪律：只放用户判断需要的信息）+ 页底静态说明（server/client/mcp 段手改 yaml + `lan_exposure` 刻意不页面化）+ `focus({preventScroll:true})`（任何聚焦调用）。
- [ ] **Step 3**: 审计测试绿；提交 `feat(web): 设置页主 agent 与杂项段`。

### Task 9: SettingsPage — 后台 agent 段

**Files:**
- Modify: `server/templates/js/components/SettingsPage.js`
- Test: 同上

- [ ] **Step 1**: 卡片列表（名称/alias/启用开关/cron + `/api/memory/status` 状态徽标复用 `getMemoryStatus`）+ 条目表单四分组（基本/调度与预算/工具与 verbs checkbox 组（`sp-chip` 开片样式先例）/模型）。
- [ ] **Step 2**: 增删改交互：「添加」空表单；「删除」两步武装钮（TrashPanel `emptyArmed` 先例）文案含「若正在运行，本轮跑完即停」；改名时提示「api_key 需重填、trigger 计权从零」（S3）；`propose+write_doc` 同持即时红字（前端提示，服务端权威）。
- [ ] **Step 3**: 审计测试绿；提交 `feat(web): 设置页后台 agent 管理段`。

### Task 10: 全量回归 + 文档同步 + 浏览器点击验证

**Files:**
- Modify: `server/routes/README.md`（新域行 + 端点计数）、`server/config/README.md`（doc_io 行）、`server/bgagent/README.md`（若无此文件则跳过）、`AGENTS.md`（Gotchas 补一条：设置页写面热生效铁律——圈首 clear/锁归属/永不外清缓存，指本 spec）
- 本 Task 由 orchestrator 执行而非 fixer（验证性）。

- [ ] **Step 1**: `D:\anaconda3\python.exe -m pytest tests/ -v` 全绿（记录用例数与耗时）。
- [ ] **Step 2**: 起真服务（真 config 副本，port 试 9889），agent-browser 点击全链路：入口→改 ai model→保存→GET 复核 + `GET /api/memory/status` 复核→加/禁用/删 bgagent→手改副本文件→保存撞 409→重载；移动端视口过一遍（`browser.resize mobile`）；截图存 tmp 供验收包。
- [ ] **Step 3**: 文档同步 + 提交 `docs(settings): 路由/config/bgagent README 与 AGENTS 铁律同步`。

---

## 并行图（orchestrator 派单用）

- 波 1：Task 1 →（顺序）Task 2 → Task 3 ｜ Task 4（独立，并行）
- 波 2：Task 5（依赖 2/3/4）｜ Task 7（前端，仅依赖 API 契约——可并行开跑）
- 波 3：Task 6（依赖 3/5）｜ Task 8 → Task 9（同文件，顺序；依赖 Task 7）
- 波 4：Task 10（唯一收口人：orchestrator）
