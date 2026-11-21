# 设置页：config.yaml 页面化编辑（分级热生效）

- 日期：2026-11-22
- 状态：待评审（oracle）
- 决策记录：方案 A（直写 config.yaml，ruamel 注释保真）胜出；旁路覆盖文件（两处真相）与纯文本编辑器（繁琐未解）否决。热生效取「分级」档：**进行中的后台 agent run 用旧配置跑完，下一拍起用新配置**；「全部立即重建」否决（撕掉进行中的 run 会留半截写入）。

## 1. 背景与目标

config.yaml 目前唯一编辑方式是手改文件 + 重启。最繁琐的是 `background_agents:` 段（单条目约 20 个字段，含工具白名单 / verbs / trigger / 独立模型配置）与 `ai:` 段。目标：Web 控制台新增「设置」页，页面化编辑高频段；保存即校验、即落盘、分级热生效。**config.yaml 仍是唯一配置真相**——页面改与手改 yaml 两个入口互不打架，页面上能保存的配置重启必然能加载。

## 2. 范围

| 段 | 页面可编辑 | 生效 |
|---|---|---|
| `ai:`（主 agent） | ✅ | 热（下一个 run） |
| `background_agents:` | ✅ 增删改 | 分级热（§5.4） |
| `notifications.daily_cap` | ✅ | 热 |
| `privacy.dirs` | ✅ | 热（既有 mtime 机制，免费） |
| `log_level` / `log_retention_days` | ✅ | 热 |
| `log_dir` | ✅ | **重启**（页面标注） |
| `server:`（host/port/lan_exposure/vault_dir） | ❌ 不进页面 | 页面放静态说明：手改 yaml + 重启 |
| `client:` | ❌ | 另一台机器的配置，服务端页面不碰 |
| `mcp.write_token` | ❌ | 写通道凭证，保持手改 |

## 3. 核心不变式

1. **校验即启动校验**：PUT 落盘前在临时副本上跑真加载器（`load_server_config` + `load_notifications` + `load_background_agents` + `validate_tool_whitelists`）。加载不过 → 不落盘。页面永远不会保存一份「启动会炸」的配置。
2. **注释保真**：写回用 `ruamel.yaml` round-trip（唯一新增依赖），用户手写的注释与条目格式不丢。
3. **不覆盖手改**：PUT 携带 GET 时的文件 revision（sha256），不符 → 409，前端提示重载。
4. **密钥不出明文**：GET 只回「已设置 + 掩码」；PUT 中 key 缺省 = 保持不变。
5. **身份仍由通道背书**：页面对配置的修改经 web 面（LAN 无鉴权，与既有「web 可写 vault」同水位，本设计不新增也不削弱鉴权面）；actor 语义不涉及 vault 账本，配置修改日志走 logging（不进 change_log——config 不是 vault 内容）。

## 4. 后端 API

新模块 `server/routes/settings.py`，前缀 `/api/settings`，注册进 `ROUTE_MODULES`（同步更新 `tests/test_routes_order.py` 快照）。

### 4.1 `GET /api/settings`

返回五段可编辑配置 + 元数据：

```jsonc
{
  "revision": "sha256:<hex>",          // config.yaml 字节哈希，乐观并发用
  "ai": {                               // 缺省段 = null
    "base_url": "...", "model": "...", "max_tokens": 4096, "temperature": 0.7,
    "context_window": 131072, "max_rounds": 15, "read_doc_max_chars": 4000,
    "confirm_timeout": 300, "tools": null,
    "api_key": { "set": true, "mask": "sk-…abcd" }   // 永不明文
  },
  "background_agents": [ /* 与 yaml 条目同形；model.api_key 同掩码规则 */ ],
  "notifications": { "daily_cap": 20 },
  "privacy": { "dirs": ["日记"] },
  "log": { "level": "INFO", "dir": ".sync/logs", "retention_days": 30 },
  "_meta": {
    "tools": [ /* TOOL_REGISTRY 键全集 */ ],
    "verbs": [ /* core.constants.VALID_TYPES 全集 */ ],
    "restart_fields": ["log.dir"]
  }
}
```

掩码格式：`首段-…末4位`（长度 <8 时只回「已设置」不回片段）。

### 4.2 `PUT /api/settings`

整文档替换语义（一次提交全量五段，与「一份 yaml」模型一致）。处理序：

1. **revision 守卫**：与服务端当前文件字节哈希不符 → 409（体含当前 revision）。
2. **密钥回填**：`ai.api_key` / `model.api_key` 键**缺省** = 从现存文件回填原明文；键存在（含 `""`）= 替换。
3. **临时文件校验**：ruamel dump 到同目录临时文件 → 在临时路径上跑 §3.1 全量加载器 → 不过则删临时文件、422 返回 `{ "errors": [{"message": "<加载器异常原文>", "section": "..."}] }`（加载器 fail-fast，一次一条，如实回显）。
4. **备份**：旧 `config.yaml` → `config.yaml.bak-YYYYmmdd-HHMMSS`（同目录，保留最近 3 份，多余即删）。
5. **原子落盘**：临时文件 `os.replace` 就位。
6. **热生效**（§5）→ 响应 200：`{ "revision": "...", "applied": [...], "restart_needed": ["log.dir"] }`。

失败路径零副作用：409/422 时不写任何文件（备份也不产生）。

**串行化**：PUT 的「revision 守卫 → 校验 → 落盘 → 热生效」全程持一把路由级 `asyncio.Lock`——revision 检查与 replace 之间不得插入另一次保存（关闭 TOCTOU；两次保存严格一前一后，后者撞 409）。

### 4.3 路由未接线

`create_app(settings=None)` 可选参数（`SettingsDeps` dataclass：`config_path: Path` + 可变 `runtime` 句柄槽）。`settings=None` 或 `runtime` 未挂时 `GET/PUT` 返回 503「设置管理未接线」（mirror ai=None→503 先例）。conftest 的 `app` fixture 不接线即天然覆盖 503 路径；接线面在 `tests/test_settings_routes.py` 自建。

**接线时序**：main() 先 create_app 后 build_runtime——`SettingsDeps.runtime` 是可变槽，build_runtime 完成后由 main 填充（`bg_supervisor` / `notif_hook` / `root_logger` / `file_handler`）。测试 seam：deps 可全 mock。

## 5. 分级热生效

### 5.1 `ai:` → 换 `ctx.ai_config`

聊天投递（runhub）每次 dispatch 读 `ctx.ai_config`。保存成功后整对象替换 → 下一个 run 即新配置；进行中的 run 持有自己的 Agent 引用，不受影响。`ai` 段被整段删除 → `ctx.ai_config = None`（行为同未配置：agent 端点 503）。

### 5.2 `notifications.daily_cap` / `log`

`GuardedNotificationHook` 增 `set_daily_cap(n)`。`log_level`：`root_logger.setLevel()`；`log_retention_days`：`file_handler.setBackupCount()`（stdlib 自带）；`log_dir` 不做热切（重建 handler 会断当日文件滚动，标重启）。

### 5.3 `privacy.dirs`

`PrivacyConfig` 本就是按 config.yaml mtime 的活视图（spec 2026-11-18 §2.7），原子写自然 bump mtime → 下次查询即算。零新代码，测试只钉「写盘后 privacy.dirs 反映新值」。

### 5.4 `background_agents` → supervisor + slot

现状：`start_background_agents()` 把 `cfg` 闭包进 per-alias 的 `_agent_loop` / `_armed_loop` 任务。改造：

- **`AgentSlot`**（scheduler.py）：`cfg` + `retired: bool` + `reload: asyncio.Event`。`_agent_loop` / `_armed_loop` 改收 slot：
  - 每圈开头 `cfg = slot.cfg`（`_handle_tick` / `_armed_check` 在调用时刻拿 cfg → **进行中的 run 天然持有旧配置对象跑完**）；
  - cron 睡眠改为 `asyncio.wait_for(slot.reload.wait(), delay)`（TimeoutError = 正常到点；reload 置位 = 立即重锚，按新 cron 重算下一拍）；armed 环在 `wake_wait` 基础上同理由 reload 事件打断；
  - 每圈检查 `slot.retired` → return 退出（当前 tick 已结束后才走到圈顶）。
- **`BgSupervisor`**（scheduler.py）：持有 `vault_dir` / `db_conn` / `ledger_conn` / `hooks` / `privacy` / `alias→(slot, [tasks])` 注册表；方法 `apply(agents: list[BackgroundAgentConfig]) -> None`（同步函数，内部 create_task/cancel，供路由调用，要求运行环内执行）：
  - 别名存续且 cfg 有变化 → `slot.cfg = new; slot.reload.set()`；
  - 别名删除或 `enabled: false` → `slot.retired = True; slot.reload.set()`（跑完本轮自然退出）；
  - 别名新增 → 按现有 spawn 逻辑起 cron/armed 任务；**alias 改名 = 删 + 增**（trigger_state 新别名从零计权，页面对话框文案须提示这一点）；
  - `is_executor` 出现/消失 → executor 任务的增/缓退（executor 环每圈自尽式检查 retired；确定性合并秒级，撕它无意义）；
  - **事件总线发布侧过滤器重算**：`events.set_filter("change", …)` 的 `_excluded_sources` 来自别名全集——别名增删后必须重算（否则 self-exclusion 漂移，arming 计数被自身写入污染）；
- **归因分层**：supervisor 不知道 WebContext。`ctx.bg_targets` 重注入（新 `_agent_caps` 结果，监控页预算显示随之更新）由 settings 路由在 `apply()` 成功后执行——路由手里本来就有校验期加载的 `BackgroundAgentConfig` 列表。
- **兼容**：`start_background_agents()` 保留为 supervisor 的薄封装（现有测试与 `Runtime.stop()` 的 task-cancel 语义不变：supervisor 暴露 `tasks()` 全集，Runtime 持 supervisor 引用，stop 时先 cancel 全部再置死）。

**并发守卫**：§4.2 的 PUT 路由锁已把所有保存（含 apply）严格串行化——revision 检查与落盘之间不插入第二次保存，supervisor 内部不再设第二把锁。

### 5.5 出域台账（红线 7）

热生效成功后若模型端点集合（ai.base_url/model + 各 bgagent model）与保存前不同 → 重跑 `log_model_endpoints()` 打全套当前端点行（幂等、廉价；云端/本地分类日志即「出域事实变化」的可见事件）。

## 6. UI 结构（实施时 @designer 主导细节，本节是结构与交互契约）

- 新组件 `server/templates/js/components/SettingsPage.js`。页面机制照监控页先例：整页模式（`state.settings`，与监控页/文档区互斥，进入/退出转场复用监控页的溶解节奏），深链 `#settings`（可分享、F5 可恢复）。入口默认放**监控页页头「设置」按钮**（设置属运维区；bgagent 状态展示本就在监控页）——具体摆位 @designer 可改，但必须满足：单一入口、监控页/设置页/文档区三者互斥不叠加。
- 页内分段导航（桌面左列锚点、移动端堆叠/分段控件，照监控页先例）：
  1. **主 agent**：base_url / model / api_key（password 型，显示「已设置（留空保持不变）」态——**UI 留空 = payload 中省略该键**（§4.2 步 2 的缺省保持语义），显式清空走「清除」小按钮 = payload 发 `""`）/ 数值参数 / tools 白名单多选（选项来自 `_meta.tools`）；
  2. **后台 agent**：卡片列表（名称 / alias / 启用开关 / cron / 运行状态徽标——复用 `/api/memory/status` 的 agents 数据），点开条目表单，内部分组：基本（name/alias/enabled/target_dir/prompt/report_dir）、调度与预算（cron/max_rounds/timeout_s/budget_*/trigger）、工具（tools checkbox、verbs checkbox、propose+write_doc 同持即时红字提示——服务端仍是权威）、模型（provider/model_id/base_url/api_key/参数）。「添加」= 空表单；「删除」= 确认框（文案含「若正在运行，本轮跑完即停」）；
  3. **杂项**：daily_cap / log_level（下拉）/ log_retention_days / privacy.dirs（标签式增删）。
- 单一「保存全部」按钮 + revision 随行；保存后生效报告条（applied / restart_needed / 409 冲突提示重载）。
- 非法保存（422）：错误消息展示在页顶告警条 + 按 section 定位高亮（错误 message 是加载器原文，含段名可字符串定位）。
- 页面底部静态说明：server / client / mcp 段需手改 yaml（`lan_exposure` 等安全承重字段刻意不页面化）。
- 遵守 AGENTS.md Frontend 规则（transform 动画、focus preventScroll、移动端壳高链等既有红线）。

## 7. 红线对照

- 无新增鉴权、无新增暴露面、无 localhost 假设变化（web 面既有 LAN 无鉴权水位，见 §3 第 5 条）；
- api_key 明文永不出 GET / 日志（`log_model_endpoints` 本就只打端点不打 key）；
- 校验失败不落盘 → 运行中服务永远不可能持有一份重启加载不了的配置；
- 快照兜底哲学（红线 8）不适用本面：config 备份（3 份滚动）就是本面的回滚面。

## 8. 测试面

- `tests/test_routes_order.py`：快照更新。
- `tests/test_settings_routes.py`：GET 形状与掩码；PUT 成功（落盘 + 备份 + 新 revision）；422 零落盘零备份；409 revision；密钥缺省=保持、显式 ""=清除；round-trip 注释保真（写入含注释样本，改一个值后注释仍在）；503 未接线。
- `tests/test_bgagent_scheduler.py` 扩展：slot 换 cfg → 下一拍新配置；reload 打断 idle sleep 重锚；retired 在 tick 结束后退出；apply() 增删别名与 executor 增删；过滤器重算。
- 热生效钉：保存后 `ctx.ai_config.model` 新值（下一个 run 生效钉）；`hook.set_daily_cap`；`log level` 现场变更；privacy 写盘即反映。
- 出域台账钉：改 base_url → 日志行数/内容变化（caplog）。
- 前端静态审计家族：新增 SettingsPage.js 进 `test_web_ui_audit` 相关钉（壳高 / preventScroll 家族按需）。

## 9. 文件清单（预估）

| 动作 | 文件 |
|---|---|
| 新增 | `server/routes/settings.py`、`tests/test_settings_routes.py`、`server/templates/js/components/SettingsPage.js` |
| 修改 | `server/routes/__init__.py`（ROUTE_MODULES + WebContext/SettingsDeps）、`server/api.py`（create_app 参数）、`server/main.py`（接线 + build_runtime 返 supervisor/notif_hook 引用）、`server/bgagent/scheduler.py`（slot/supervisor）、`server/bgagent/notify.py`（set_daily_cap）、`server/templates/js/components/MonitorPage.js` + `app.js` + `index.html`（入口与整页模式）、`pyproject.toml`（ruamel.yaml）、`tests/test_routes_order.py` |
| 文档 | `server/routes/README.md`、`server/bgagent/README`（若有）同步；AGENTS.md Gotchas 视实施结果决定是否补一条热生效铁律 |
