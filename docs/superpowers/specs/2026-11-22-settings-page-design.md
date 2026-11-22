# 设置页：config.yaml 页面化编辑（分级热生效）

- 日期：2026-11-22
- 状态：已评审（oracle GO-with-fixes，M1–M6 / S1–S8 全部吸收，2026-11-22）
- 决策记录：方案 A（直写 config.yaml，ruamel 注释保真）胜出；旁路覆盖文件（两处真相）与纯文本编辑器（繁琐未解）否决。热生效取「分级」档：**进行中的后台 agent run 用旧配置跑完，下一拍起用新配置**；「全部立即重建」否决（撕掉进行中的 run 会留半截写入）。UI 形态改版（2026-11-22 用户拍板）：整页接管 → **modal 浮层 + 侧栏底部齿轮入口**（见 §6）。

## 1. 背景与目标

config.yaml 目前唯一编辑方式是手改文件 + 重启。最繁琐的是 `background_agents:` 段（单条目约 20 个字段，含工具白名单 / verbs / trigger / 独立模型配置）与 `ai:` 段。目标：Web 控制台新增「设置」页，页面化编辑高频段；保存即校验、即落盘、分级热生效。**config.yaml 仍是唯一配置真相**——页面改与手改 yaml 两个入口互不打架，页面上能保存的配置重启必然能加载。

## 2. 范围

| 段 | 页面可编辑 | 生效 |
|---|---|---|
| `ai:`（主 agent） | ✅ | 分级热（下一次派发，§5.1） |
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
5. **身份由通道背书**：页面对配置的修改经 web 面（LAN 无鉴权）。注意：这是**完整性新增面**（feature 前 LAN 访客碰不到 config），已被用户拍板接受、演进位已备案——诚实表述见 §7，勿据本条误判「零新增」。actor 语义不涉及 vault 账本，配置修改日志走 logging（不进 change_log——config 不是 vault 内容）。

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
    "restart_fields": ["log.dir"]   // 后端常量硬编码（就一个字段，不成机制），响应照带 restart_needed
  }
}
```

掩码格式：`首段-…末4位`（长度 <8 时只回「已设置」不回片段）。

**路径口径**：加载器把 `target_dir`/`report_dir` 归一成 resolved 绝对路径——GET 必须**反归一为 vault 相对 posix** 再出（report_dir 等于缺省值 `<target_dir>/.reports/<alias>` 时**省略该键**，PUT 缺席即缺省）；`prompt` 本就相对 config 目录，原样出。PUT 方向由加载器重新归一+校验（出格 vault 即 422）。

### 4.2 `PUT /api/settings`

**写回模型 = 字段级写回原 round-trip 文档**（不是整文档重建）：GET 读入的 ruamel 文档节点上，只对页面可见键做 set/delete，未建模键（如 bgagent 条目的 `escalation`）与页面外 section（server/client/mcp）**天然逐字节保留**——消灭「页面保存静默丢手写字段」这类缺陷。页面可见键的删除语义显式：`ai: null` = 删段；bgagent 列表按 alias 对账（payload 缺席 = 删条目）；`privacy.dirs: []` = 空名单。处理序：

1. **revision 守卫**：与服务端当前文件字节哈希不符 → 409（体含当前 revision）。
2. **密钥回填**：`ai.api_key` / `model.api_key` 键**缺省或 `null`** = 从现存文件回填原明文（null ≡ 缺省——钉死，防前端序列化 undefined/null 差异误删 key）；键存在（含 `""`）= 替换。
3. **临时文件校验**：ruamel dump 到 **config 同目录**临时文件 → 在临时路径上跑 §3.1 全量加载器 → 不过则删临时文件、422 返回 `{ "errors": [{"message": "<加载器异常原文>", "section": "..."}] }`（加载器 fail-fast，一次一条，如实回显）。**同目录是承重约束不是风格**：加载器唯一的 config-dir 锚是 bgagent prompt（`Path(config_path).parent`）——临时文件若去 `%TEMP%`，prompt 存在性校验假阴性（评审已核实其余路径均不锚 config 目录）。校验时 `load_background_agents` 的 `vault_dir` 传**运行进程现值**（server 段不可编辑，二者等价，照此写明）。错误回显双钉：**消息中不得出现 api_key 值**（防经 422 泄密）；`background_agents` 类消息须含条目定位前缀（加载器现文案已含 alias——格式契约写明，前端据此定位，改 loader 文案须同步前端）。
4. **备份**：旧 `config.yaml` → `config.yaml.bak-YYYYmmdd-HHMMSS-ffffff`（同目录，**微秒尾**防同秒双保存撞名；保留最近 3 份，多余即删）。
5. **原子落盘**：临时文件 `os.replace` 就位。**Windows 失败语义**：`os.replace` 遇无 FILE_SHARE_DELETE 的目标句柄（杀软/索引器瞬时句柄）抛 PermissionError → 退避重试 1–2 次，仍败 → 500 且如实报「未保存」；此时**备份已产生**是承认的例外（见下条副作用边界）。
6. **热生效**（§5）→ 响应 200：`{ "revision": "...", "applied": [...], "restart_needed": ["log.dir"] }`。**apply 失败窗口**（replace 成功后 apply 抛）：**绝不反滚已提交文件**（盘上是新配置、内存半新半旧——回滚只会制造第三态），响应 500 + `{saved: true, error: "…部分热生效失败，建议重启服务"}`；测试钉此响应形状且钉文件未被反滚。

失败路径零副作用的准确边界：409/422 时不写任何文件（备份也不产生）；备份在 replace 前产生、replace 失败时备份在场但不算配置变更。

**串行化**：PUT 的「revision 守卫 → 校验 → 落盘 → 热生效」全程持一把路由级 `asyncio.Lock`——revision 检查与 replace 之间不得插入另一次保存（关闭 TOCTOU；两次保存严格一前一后，后者撞 409）。

### 4.3 路由未接线

`create_app(settings=None)` 可选参数（`SettingsDeps` dataclass：`config_path: Path` + 可变 `runtime` 句柄槽）。`settings=None` 或 `runtime` 未挂时 `GET/PUT` 返回 503「设置管理未接线」（mirror ai=None→503 先例）。conftest 的 `app` fixture 不接线即天然覆盖 503 路径；接线面在 `tests/test_settings_routes.py` 自建。

**接线时序**：main() 先 create_app 后 build_runtime——`SettingsDeps.runtime` 是可变槽，build_runtime 完成后由 main 填充（`bg_supervisor` / `notif_hook` / `root_logger` / `file_handler`）。测试 seam：deps 可全 mock。

## 5. 分级热生效

### 5.1 `ai:` → 换 `ctx.ai_config` + 缓存 Agent 代际失效

事实钉（2026-11-22 盘点）：Agent 实例按 session 缓存在 `routes/agent.py` 的 `agent_cache`，`ctx.ai_config` 只在 **Agent 构造时刻**快照进 AgentSpec（`_build_agent_spec`）——换 ctx 只影响 cache miss 的新会话。热生效语义对齐 bgagent 档：

- 保存后替换 `ctx.ai_config`（新会话即刻新配置）+ `agent_cache` 条目打**代际戳**（settings generation ++）：派发时 `_get_agent` 比对代际，落后即重建（复用 TTL 驱逐的既有守卫——持会话锁/有 pending_confirm 的不重建，**进行中的 run 用旧配置跑完，下一次派发重建**）；
- **承重红线（评审 M1）**：失效只许在 `_get_agent` 读侧发生，**永不从外部清空/摘除 agent_cache**——RunHub 的 persist 回调在持久化时刻经 `_get_agent(sid)` 取引用，外部清缓存会让正在运行的会话「从 DB 重建（缺本轮消息）后写回」→ 丢对话。回归钉：run 进行中保存配置，该会话消息零丢失。
- 例外通道 `/api/ai/chat` 本就每请求直读 ctx，天然热生效；`/api/egress` 状态读 `ctx.ai_config`，随热切自然跟随；
- 测试钉三态：新会话读新值 / 空闲旧会话下一派发读新值 / 运行中会话跑完前不读新值。

### 5.2 `notifications.daily_cap` / `log`

`GuardedNotificationHook` 增 `set_daily_cap(n)`。`log_level`：`root_logger.setLevel()`；`log_retention_days`：`file_handler.setBackupCount()`（stdlib 自带）；`log_dir` 不做热切（重建 handler 会断当日文件滚动，标重启）。

### 5.3 `privacy.dirs`

`PrivacyConfig` 本就是按 config.yaml mtime 的活视图（spec 2026-11-18 §2.7），原子写自然 bump mtime → 下次查询即算。零新代码，测试只钉「写盘后 privacy.dirs 反映新值」。

### 5.4 `background_agents` → supervisor + slot

现状：`start_background_agents()` 把 `cfg` 闭包进 per-alias 的 `_agent_loop` / `_armed_loop` 任务。改造：

- **`AgentSlot`**（scheduler.py）：`cfg` + `retired: bool` + `reload: asyncio.Event`（仅 cron 环消费，见 S1）。`_agent_loop` / `_armed_loop` 改收 slot：
  - **圈首时序钉死（评审 M2，防忙环）**：`reload.clear()` → 读 `cfg = slot.cfg` / 查 `slot.retired` / 取活别名集 → 进 `wait_for(slot.reload.wait(), delay)`。clear 与重新进 wait 之间**不得有 await 点**（此段与同步 `apply()` 同循环同线程 ⇒ 无交错——「apply 同步 + 同事件循环」是热重载的承重单线程契约，写进模块头注）。clear 置后不置先：先 clear 再读 cfg，保证「clear 前 set 的更新」必被本圈读到；
  - cron 睡眠 = `asyncio.wait_for(slot.reload.wait(), delay)`（TimeoutError = 正常到点；reload 置位 = 立即回圈顶按新 cron 重锚）；**armed 环不接 reload**（评审 S1 砍点）：本就 30min backstop + 事件唤醒，圈首读 `slot.cfg` 足够，trigger 参数变更最晚下一拍生效——不引入 wake_wait/Event 双路合并的复杂度；
  - `slot.retired` → 回圈顶即 return 退出（进行中的 tick 已结束后才走到圈顶）。
- **`BgSupervisor`**（scheduler.py）：持有 `vault_dir` / `db_conn` / `ledger_conn` / `hooks` / `privacy` / `alias→(slot, [tasks], lock)` 注册表 + **活别名集**（`aliases: set`，apply 原地增删）；per-alias `asyncio.Lock` **归 supervisor 持有、跨 slot 重建复用**（评审 M3：若重建发新锁，慢 run 未退时新旧任务同别名无互斥 → 双 run 同写 target_dir）。方法 `apply(agents)`（同步、循环内调用）分支表——**registry 收录 config 中全部别名（含 enabled:false，其 slot 出生即 retired、无任务）**：
  - 别名存续（slot 在场且未 retired）且 cfg 有变 → `slot.cfg = new; slot.reload.set()`；
  - **别名复活（slot.retired → 重新出现且 enabled）** → 原 slot 复位（retired=False, cfg=new, reload.set()）+ 用**原 Lock** 重建任务（评审 M3：缺此分支则「禁用→启用」静默失效到重启）；
  - 别名删除或 `enabled: false` → `slot.retired = True; slot.reload.set()`（跑完本轮自然退出）；**alias 改名 = 删 + 增**（trigger_state 从零计权；**api_key 回填按 alias 匹配、新别名无处回填**——UI 文案必须提示重填 key，评审 S3）；
  - `is_executor` 出现/消失 → executor 任务的增/缓退（executor 环每圈自尽式检查 retired；确定性合并秒级）；
  - **两侧 self-exclusion 同步重算（评审 M4，scheduler.py 头注铁律「单源不得 drift」的落实）**：发布侧 `events.set_filter("change", …)` 从活别名集重算；**扫描侧 `_handle_tick` / `_armed_check` 的 `all_aliases` 不再吃 spawn 闭包，每圈从 supervisor 活别名集现取**；
  - **任务生命周期（评审 M5）**：`tasks()` 恒返回 live 全集——retired 任务**留在注册表直到自然完成**（done-callback 负责摘除），stop 时一律 cancel/await；`Runtime.stop()` 置 supervisor 死标志，死后再调 `apply()` 直接 raise（防 stop 后路由 create_task 复活）。
- **归因分层**：supervisor 不知道 WebContext。`ctx.bg_targets` 重注入（新 `_agent_caps` 结果，监控页预算显示随之更新）由 settings 路由在 `apply()` 成功后执行——路由手里本来就有校验期加载的 `BackgroundAgentConfig` 列表。
- **兼容与主路径（评审 S8）**：`start_background_agents()` 保留为 supervisor 薄封装、**仅供既有测试**；**main 生产路径直接构造 supervisor**——生产跑的是被测路径，不测 slot 机制的封装不算实现。`Runtime` 持 supervisor 引用，stop 语义经 supervisor（先 cancel 全部 live 任务，再置死标志）。

**并发守卫**：§4.2 的 PUT 路由锁已把所有保存（含 apply）严格串行化——revision 检查与落盘之间不插入第二次保存，supervisor 内部不再设第二把锁。

### 5.5 出域台账（红线 7）

热生效成功后若模型端点集合（ai.base_url/model + 各 bgagent model）与保存前不同 → 重跑 `log_model_endpoints()` 打全套当前端点行（幂等、廉价；云端/本地分类日志即「出域事实变化」的可见事件）。

## 6. UI 结构（实施时 @designer 主导细节，本节是结构与交互契约）

- 新组件 `server/templates/js/components/SettingsPage.js`。**交互形态 = modal 浮层（2026-11-22 用户拍板改版；原「整页接管」方案否决——设置不占主页面是行业规范，仓内监控页先例不适用于设置类）**：
  - **入口**：左侧竖栏**底部**齿轮图标钮（icon-only，`aria-label="设置"`，常驻可见）；监控页页头入口删除（单一入口纪律）；深链 `#settings` 保留 = 打开浮层（F5 可恢复，关闭清 hash）。
  - **桌面**：居中对话框（backdrop 压暗；宽度约 880px 上限、高约 86vh 上限，头部固定 + 体部内滚；主页面零 reflow）。
  - **移动端（≤639px）**：全屏浮层（头部带关闭 + 保存钮；壳高走既有布局视口链，不用 100vh；键盘弹出红线照旧）。
  - **转场**：backdrop opacity + 面板 transform（scale/translate，≤200ms，仅 GPU 属性），reduced-motion 直切；折叠壳家族照 `MobileSheet` 先例（visibility:hidden + RM 下 `.open` 同杀——审计钉家族）。
  - **关闭**：右上 X / Escape（层级最高，先于监控页退出）/ 点 backdrop；**有未保存改动时任一关闭途径需二次确认**（原生 `confirm` 先例被容忍），干净则直关；关闭后焦点归位齿轮钮。z-index 为既有层级族最高。
  - **内部结构不变**：桌面左列分段导航（浮层内双列）、移动端顶部分段钮；表单/保存流程/反馈条/页底说明全部沿用原契约。
- 页内分段导航（桌面左列锚点、移动端堆叠/分段控件，照监控页先例）：
  1. **主 agent**：base_url / model / api_key（password 型，显示「已设置（留空保持不变）」态——**UI 留空 = payload 中省略该键**（§4.2 步 2 的缺省保持语义），显式清空走「清除」小按钮 = payload 发 `""`）/ 数值参数 / tools 白名单多选（选项来自 `_meta.tools`）；
  2. **后台 agent**：卡片列表（名称 / alias / 启用开关 / cron / 运行状态徽标——复用 `/api/memory/status` 的 agents 数据），点开条目表单，内部分组：基本（name/alias/enabled/target_dir/prompt/report_dir）、调度与预算（cron/max_rounds/timeout_s/budget_*/trigger）、工具（tools checkbox、verbs checkbox、propose+write_doc 同持即时红字提示——服务端仍是权威）、模型（provider/model_id/base_url/api_key/参数）。「添加」= 空表单；「删除」= 确认框（文案含「若正在运行，本轮跑完即停」）；**alias 改名 = 删 + 增**：表单需提示「改名将清空已存 api_key（需重填）且 trigger 计权从零开始」（§5.4/S3）。
  3. **杂项**：daily_cap / log_level（下拉）/ log_retention_days / privacy.dirs（标签式增删）。
- 单一「保存全部」按钮 + revision 随行；保存后生效报告条（applied / restart_needed / 409 冲突提示重载）。
- 非法保存（422）：错误消息展示在页顶告警条 + 按 section 定位高亮（错误 message 是加载器原文，含段名可字符串定位）。
- 页面底部静态说明：server / client / mcp 段需手改 yaml（`lan_exposure` 等安全承重字段刻意不页面化）。
- 遵守 AGENTS.md Frontend 规则（transform 动画、focus preventScroll、移动端壳高链等既有红线）。

## 7. 红线对照（诚实版，评审 S4 定稿）

- **保密性同水位**：api_key 明文永不出 GET / 日志 / 422 回显；egress 状态读 `ctx.ai_config` 随热切自然跟随；无新增鉴权、无新增网络暴露面、localhost 默认不变。
- **完整性是新增面（备案，非辩护）**：feature 前 LAN 访客碰不到 config（agent 写面被 safe_rel_path 限死 vault 内）；feature 后**任意 LAN 设备可换模型端点、关后台 agent、改工具白名单**。「不新增」不成立——**用户拍板接受**（与「web 面可写全库」同一信任决策的延伸，2026-11-22）。**演进位**：设置写面 token 化（仿 `mcp.write_token` 形态：配置项 + 空=关闭），需要时按红线 1/2 的整体水位迁移路径走，不单点造锁。
- 校验失败不落盘 → 运行中服务永远不可能持有一份重启加载不了的配置；
- 快照兜底哲学（红线 8）不适用本面：config 备份（3 份滚动）就是本面的回滚面。

## 8. 测试面

- `tests/test_routes_order.py`：快照更新。
- `tests/test_settings_routes.py`：GET 形状与掩码；PUT 成功（落盘 + 备份 + 新 revision）；422 零落盘零备份；409 revision；密钥缺省=保持、显式 ""=清除；round-trip 注释保真（写入含注释样本，改一个值后注释仍在）；503 未接线。
- `tests/test_bgagent_scheduler.py` 扩展：slot 换 cfg → 下一拍新配置；reload 打断 idle sleep 重锚；retired 在 tick 结束后退出；apply() 增删别名与 executor 增删；发布侧过滤器重算；**评审补钉**——连开两次保存无忙环且两次都生效（M2）、禁用→启用即时生效（M3）、run 中禁用→启用无双跑（锁复用，M3）、加别名后存量 agent 扫描排除集立即含新别名（M4）、stop 能 cancel/await retire 中的任务且死后 apply() raise（M5）。
- 热生效钉：保存后 `ctx.ai_config.model` 新值；**agent_cache 代际三态**（新会话读新值 / 空闲旧会话下一派发读新值 / 运行中会话跑完前不读新值）+ **run 进行中保存零丢消息**（M1）；`hook.set_daily_cap`；`log level` 现场变更；privacy 写盘即反映；字段级写回保真钉（escalation/未知键保存后原样在场）。
- 写路径钉：PUT×2 并发第二家 409；`api_key: null` ≡ 保持（S2）；同秒双保存备份不撞名（S6）；apply 失败 → 500 `{saved:true}` 且文件未被反滚（M6.3）；422 回显不含 key 值（S5）。
- 出域台账钉：改 base_url → 日志行数/内容变化（caplog）。
- 前端静态审计家族：新增 SettingsPage.js 进 `test_web_ui_audit` 相关钉（壳高 / preventScroll 家族按需）。

## 9. 文件清单（预估）

| 动作 | 文件 |
|---|---|
| 新增 | `server/routes/settings.py`、`tests/test_settings_routes.py`、`server/templates/js/components/SettingsPage.js` |
| 修改 | `server/routes/__init__.py`（ROUTE_MODULES + WebContext/SettingsDeps）、`server/api.py`（create_app 参数）、`server/routes/agent.py`（agent_cache 代际戳）、`server/main.py`（接线 + build_runtime 返 supervisor/notif_hook 引用）、`server/bgagent/scheduler.py`（slot/supervisor）、`server/bgagent/notify.py`（set_daily_cap）、`server/templates/js/components/MonitorPage.js` + `app.js` + `index.html`（入口与整页模式）、`pyproject.toml`（ruamel.yaml）、`tests/test_routes_order.py` |
| 文档 | `server/routes/README.md`、`server/bgagent/README`（若有）同步；AGENTS.md Gotchas 视实施结果决定是否补一条热生效铁律 |
