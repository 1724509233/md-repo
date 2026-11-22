# 首页 · 管家日报设计（home-butler-landing）

- 日期：2026-11-22
- 状态：设计定稿 + oracle 送审修订已并入（§9.8–9.9），待实施计划
- 范围定性：架构级新增视图面（新落地页 + 新路由域），不改既有子系统行为

## 0. TL;DR

打开网站第一眼不再是一块空编辑区，而是「回到书房」：主智能体以第一人称「我」（管家）汇报您不在时系统与智能体团队做了什么、您上次离开前做到哪、以及几件做不了主等您拿主意的事。后端一个聚合端点 + 一个按需 LLM 端点；前端一个 Home 组件，只在「无当前文档」时占据主区。

## 1. 背景与目标

**现状**：无 hash 打开网站 → 空编辑区 + 侧栏文件树。「系统做了什么」散落在 L0 控制台 feed、监控页、通知下拉；「我做了什么」只有 change_log 里 source=web 的行可查；「接下来干什么」没有任何入口聚合。「懂自己的管家」叙事缺第一出口。

**目标**：把首页变成用户回来时省去摩擦心智的那张「桌上便签」——缺席多久、谁干了什么、有什么等裁决，一屏讲完。

**沉浸方向定位**：用户已定沉浸化为大方向（侧栏收起、固定页进浮层、菜单展开、快捷键操作文档）。本次**只做首页**，但首页必须为那个方向留好位：
- Home 组件对壳零依赖——契约只有「占据主区」一条；后续壳怎么变，Home 不动。
- 视觉语言按低 chrome「信笺」调做（不做大仪表盘卡片墙），与沉浸终态同向。

## 2. 范围

**做**：`/api/home` 聚合 GET、`/api/home/summary` 按需 LLM 一句话、前端 Home 组件与落地/返回行为、lastVisit 缺席锚点、行动清单规则引擎、规范文案与口吻常量、后端测试 + 前端钉。

**不做**（全部进 §10 演进位）：沉浸式壳改造、个人记忆读取与画像、工具态智能体深度模式、`#home` 深链、`继续上次文档`快捷入口（首页不含「回到文档」的通道——文档入口本来就在侧栏与搜索结果里）。

## 3. 信息架构（自上而下四段）

### G1 管家一句话
- 时段问候 + 缺席叙事（叙事栈大字，缺席时长黄铜色；V1 中文按现状落无衬线，见 §9.7）。**叙事按缺席分级（§6.3 判据），S0/S1 一律不报数字**——「离开了 3 分钟」既有监控感又暴露心跳粒度，数字的戏剧性只在 ≥小时级才成立：

| 级 | 判据（进入首页时算一次） | G1 主文案（括注 = 状态条件变体） |
|---|---|---|
| **S0** 站内回首页 | 运行时标志：本会话已 boot，logo 返回（无刷新） | 「午后好。桌子还在这儿。」（G4 非空 →「您回来啦。桌上有几件事，等您过目。」；G4 空 →「没什么变动，不赶时间。」） |
| **S1** 瞬时离开 | boot 且 `now − lastPresence < 15min` | 「下午好。您才离开片刻，书房还是原样。」（极简兜底「我在。随时可以继续。」） |
| **S2** 当日离开 | 15min – 6h | 「午后好。您离开的这两个小时，书房一切如常。」（有待办 →「您出去了 4 个小时。有 3 件事等您拿主意。」） |
| **S3** 隔夜 | 6h – 2d | 现稿（「您不在的这几天，我把书房打理好了……」按实际时长改写量词） |
| **S4** 多日 | > 2d | 现稿 |

- 数字粒度：<15min 无数字；15min–2d 报小时；≥2d 报「N 天又 M 小时」；**>7d 时 G2 段题挂「过去 7 天」限定语**——窗钳制是服务端边界、叙事是客户端感知，两层不许互相撒谎。
- 存储不可用（`setItem` 抛）→ 泛化文案：只报时段问候，无缺席数字、无首次专属句（与真首次分流，§6.3）。
- ✨按钮「让我说说这几天的事」：按需调 `POST /api/home/summary`，成功后**整句替换**分级文案；生成中按钮禁用；失败保留分级文案，退避冷却后可重试（§5.2）。

### G2 您不在时，我做了什么
- 数据窗内（§5.1 since）source ≠ web 的全部 change_log 行，按 actor 成组（§4.2 映射表）。
- 组 = 报告卡：组头是口吻模板渲染的「我自己动手的 / 我让〈别名〉办的 / 我从您设备上接的 / 我眼皮子底下的外部改动」，组内每行 `时刻 · 动作 · 路径（可点跳转）`。
- 窗内为空 → 整段收成一句：「这段时间安安静静，我只看了看房子的四周。」（不显示空卡列表）
- **S0/S1 短窗降级**：不渲染 G2 段题与卡片，只留一句「您刚离开，还没有新的事」——「安安静静」句仅真窗（S2+）使用，否则管家在 S0 场景里演戏。

### G3 您上次离开前做到哪
- source = web 的行按日分组倒序，每日一行紧凑摘要（编辑 N · 新建 N · 移动 N），默认只展开最近一天，点「展开 ›」出当日路径明细。
- **窗口与缺席窗解耦：固定回看 3 天**（G3 目的是接上下文不是报账——S0/S1 短离开也必须有内容；长缺席则恰好呈上离开前的脉络）。
- 段题分级变体：S0/S1 用「您手头的脉络」；S2+ 用「您上次离开前做到哪」。
- 目的：回来接上下文，不是审计表——所以不渲染删除细节计数以外的冗余维度。

### G4 我做不了主的事
规则行动清单，每条一行 + 直达动作：
| 条目 | 数据源 | 跳转 |
|---|---|---|
| N 条记忆提案待裁决 | proposals 待审计数 | 记忆控制台 |
| N 处是智能体代您改的，您还没看过 | `seq > lastSeenChangeSeq` 且 source ∈ {agent} ∪ config 别名集（**watchdog 除外**——含您自己在 Obsidian 的编辑与 MCP 直写，计入即假账） | G2 段（锚点滚动） |
| N 个同步冲突 | files 表 `.conflict-` 文件名计数 | 冲突入口（现有 ConflictBanner 通道） |
| 回收站批次数（仅 >0 显示） | trash 批次查询 | 回收站面板 |
- 空态：「都打理好了，不急。」
- 全部为结构化渲染，数字不经 LLM（§4.1）。

## 4. 文案铁律（第一人称叙事）

### 4.1 事实与语气分离
G2/G3/G4 的每一行与所有数字由前端从 change_log 结构化渲染，**LLM 永不触碰事实与计数**；LLM 只产出 G1 的那句「闲聊」。管家报的是真账，不是创作。（提示词中的「不得编造」是质量约束，不是防线——防线是 LLM 拿不到渲染权。）

### 4.2 actor → 第一人称组头映射
change_log 的 source 列是自由词表（bgagent 归因 = 别名原文直落，还有 `reconcile`、`unknown` 等系统值）。**分桶唯一判据 = config `background_agents` 别名集合**，绝不按「非保留词即别名」推断（否则渲染出「我让 reconcile 办的」笑话）：

| source 值 | 组头模板 | 说明 |
|---|---|---|
| `agent` | 我自己动手的 | 主智能体 |
| ∈ config 别名集合 | 我让〈别名〉办的 | bgagent |
| 其余一切值（`watchdog`/`unknown`/`reconcile`/未来新值） | 我眼皮子底下的外部改动 | watchdog 含用户自己的 Obsidian 编辑与 MCP 直写（MCP 归因退化为 watchdog 兜底，AGENTS.md 备案） |
| `web` | （不进 G2，进 G3） | 用户本人 |

注：`mcp` 今天是 change_log 死词（MCP 写归 watchdog），不进表；若未来归因正式化再立专属行。

### 4.3 语气规则（单点存放）
沉稳、简洁、克制的暖意；不卖萌、不堆 emoji、不过度拟人（不说系统「活着」「开心」）。
- 前端静态文案：`server/templates/js/utils/homeVoice.js` 常量模块。
- 服务端 summary 提示词 persona 段：`server/agent/prompts.py` 的 `HOME_SUMMARY_*` 常量。
两处口吻必须一致（首页的我和聊天面板里的是同一个管家）；改动时同 PR 一起改。

### 4.4 规范文案示例（以 v2 mockup 为准的定稿语料）
- G2 组行：`22:04 归档整理 · review/…-trash-actor.md`、`— 收了 3 篇，送上 6 篇；有 1 处对不上`（同步组用归并句不逐行，见 §5.1）
- G4：「3 条记忆提案，等您拿主意」「5 处是我代您改的，您还没看过」「1 处同步冲突，两版我都留着没删 → 帮我看看」
- 页脚：「打开网站我就在这张桌前 · 点左上角字样随时回来」

## 5. 后端

### 5.1 `GET /api/home?since=<epoch_float>&last_seen_seq=<int>`
- `since` 缺省 = 当前时间 − 7 天；服务端钳制 `now − 7d ≤ since ≤ now`（下界：缺席超 7 天时更老事件按历史处理，payload 有界；上界：防未来窗空数据配长缺席叙事）。
- `last_seen_seq` 缺省 0：客户端已读游标（§6.3），只喂 `actions.unseen_agent_changes` 计数。
- 响应形状：

```json
{
  "window": {"since": 0.0, "until": 0.0},
  "system_groups": [
    {"actor": "agent", "alias": null, "last_ts": 0.0,
     "ops": [{"ts": 0.0, "action": "created", "path": "a/b.md", "from_path": null}]}
  ],
  "my_days": [
    {"date": "2026-11-21", "counts": {"edited": 3, "created": 1, "moved": 1},
     "ops": [{"ts": 0.0, "action": "edited", "path": "c.md", "from_path": null}]}
  ],
  "actions": {"pending_proposals": 3, "unseen_agent_changes": 5,
              "conflicts": 1, "trash_batches": 2}
}
```

- `system_groups`：非 web 行按 §4.2 actor 成组，组按 `last_ts` 降序；同步类不单独设 source——设备上传经上传/保存通道归因 web 的既有事实**不改**，「我从您设备上接的」组仅在存在可归因的同步行时渲染（实施期核实归因现状，若现状无法区分则本组暂缺省，见 §11 实施注记）。
- `my_days`：web 行按服务器本地日期分组（单机 LAN，单一时区，够用）；**窗口固定取近 3 天，与 `since` 无关**（§3-G3）。
- `actions.unseen_agent_changes` = **`seq > last_seen_seq` 且 source ∈ {`agent`} ∪ config 别名集** 的行数（已读游标语义，§6.3）——不是窗计数：天然免疫 7d 钳制与重复进首页；watchdog 行被 source 集排除，不会把用户自己的 Obsidian 编辑报成「代您改的」（oracle 送审 B2：违者即违 §4.1 真账铁律）。
- `actions.conflicts` = files 表路径含 `.conflict-` 的行数；`trash_batches`、`pending_proposals` 复用现有 trash/proposals 查询。
- 实现：新 `server/routes/home.py`（`register(app, ctx)` 域模式），复用 `get_changes` / proposals / trash 查询；注册进 `create_app` 路由表并同步 `test_routes_order.py` 钉。
- 备案边界：change_log 在 index.db（可重建缓存），恢复演练后 G2/G3 合法归零——G1 分级文案此时可能说「安安静静」而实际是刚重建，属可接受降级，不做特殊态。

### 5.2 `POST /api/home/summary`
- 流程：服务端按 7 天窗重新聚合 → `assemble_home_digest(groups, *, personal_profile=None)` → 单次非流式 LLM 调用（**主 agent 模型 config**）→ `{"text": "…"}`。
- 输入 digest **只含组头、动作、计数与路径，不含任何正文**；`privacy_dirs` 命中路径替换为 `〈私密〉`（文件名本身可能泄漏含义）。
- persona 提示词：§4.3 语气 + 限一句话（≤40 字量级）+ 只可基于 digest。
- 缓存：进程内 `{date_str: text}` 当日缓存——当日只花一次调用，多标签共享；重启重生成可接受。假设 uvicorn **单进程**（main.py 现行默认，写明此假设）；设置页热换模型后当日缓存不失效（备案，可接受）。
- **单飞 + 失败退避（成本面承重）**：并发 POST 共用一个 in-flight 调用（缓存只覆盖「成功后」，同时点击必须去重）；失败后最小调用间隔（建议 5 分钟，实现期定值）内直接 429/拒绝，前端按钮进禁用冷却态——重试连击不得变成无上限云调用与出域（oracle 送审 M4）。
- 错误：模型未配置 → `400 {"error": "model_not_configured"}`；调用失败 → `502 {"error": "…"}`。
- 出域台账（行级语义，与现机制逐文档行一致，勿造合成行）：`EgressTarget(channel="home", actor="agent", run_id="", endpoint_host/model 取服务端 config——身份由通道注入，红线 3)`；`touched` = **打码前真实非隐私路径集**（`flush_touches` 双保险靠真实路径判 privacy，`〈私密〉` 占位符既泄漏不了什么也查不了），`via='digest'`；**LLM 调用失败也先 flush**（payload 已出域，红线 7 要的是事实不是体面）。回环端点（本地模型）→ `should_record` False → 零行是正确行为（egress 回环门既有）。MonitorPage 对 channel 原样渲染，新增 `home` 值无需改 UI，目检一眼即可。
- `personal_profile` 参数本次恒 None——个人记忆注入点（§10）。

## 6. 前端

### 6.1 渲染与导航
- 唯一规则：**主区无当前文档且非监控页 → 渲染 Home**。`#doc/…`、`#settings`、`#monitor` 深链与 boot 行为零改动。
- 左上 logo 点击 → `currentPath = null` 回首页（文档状态不销毁；返回不丢上次文档，再点侧栏即回），**且必须同时 `history.replaceState(null, '', location.pathname)` 清 hash**（与设置浮层关合清 `#settings` 手法同形，`app.js:627` 先例）。理由：`app.js:770` hash watch 是 `if (!p) return`，currentPath 置空不清 hash → 回首页后一刷新 `applyInitialHash` 重开旧文档，**首页对「回首页→刷新」永不可达**（oracle 送审 B1）。这与 §2「不做 #home 深链」不冲突：清 hash ≠ 造深链。实施确认 rail 顶部字样有 click 绑定，别做成装饰文字。
- 「文档状态不销毁」意味着 dirty 未保存守卫在首页期间继续拦刷新/关标签（logo 回首页不经 openDoc 的 dirty 守卫）——**有意为之**，显式备案。
- S0 判定（G1 分级）：运行时标志，本会话首次 boot 成功后置位，logo 回首页不改变——「离开」的定义收紧为两次 boot 之间的空窗（§6.3）。
- 转场复用 `.doc-enter` 溶解；`prefers-reduced-motion` 降级沿既有规范。
- 移动端：Home 天然单列自适应；侧栏入口维持现有 mobile sheet 体系。

### 6.2 组件与请求
- 新 `server/templates/js/components/Home.js`；`api.js` 增 `getHome(since)` / `genHomeSummary()`。
- 拉取时机：每次进入首页（boot 无 hash / logo 点击各拉一次）；**退出监控页回主区时同样重取一次**（monitoring 是 v-show 状态切换不是导航，缺它拿的是 boot 时陈数据）；**不做周期轮询**（首屏页，避免空载）。
- 失败降级：G1 照常渲染（纯前端合成）；G2–G4 区显示「没打听到消息」+ 重试按钮。

### 6.3 缺席锚点（lastPresence）与已读游标
- **`mdvault.lastPresence`**：只在 `document.visibilityState === 'visible'` 时写——写点共四：boot 补锚（回调即判 visible，兜 mobile pagehide 不可靠）、心跳（回调内判 visible，5 分钟）、`visibilitychange → hidden`、`pagehide`。全部受 visible 门控：杜绝后台标签页整夜续锚导致次日缺席归零（throttle 是引擎恩赐不是契约，oracle 送审 M1；boot 笔的门控是终审 M1 追补，boot 在后台恢复标签下也不得续锚）。
- **分级判据在进入首页/boot 时算一次**（S0 用运行时标志，S1–S4 用 `now − lastPresence`），阈值 15min / 6h / 2d（可调量级，拍板记录 §9.8）。
- **首次 vs 存储不可用分流**：`setItem` 抛 → 泛化文案（时段问候，无数字无首次句）；`getItem` 返回 null 且写入可用 → 真首次（首次文案 + 窗默认 7 天）。
- **已读游标 `mdvault.lastSeenChangeSeq`**：G2 渲染非空时写入窗内最大 seq；`unseen_agent_changes` 由服务端按 `last_seen_seq` 参数过滤（§5.1）——「看过」即看过，免疫窗钳制与重复进首页。
- 多设备各记各的；G1 时长是纯客户端减法，服务端窗收客户端 epoch，双设备时钟偏移平移窗——单机 LAN 可接受，备案。

## 7. 安全红线对照（2026-11-18 定稿八条）

- 无新暴露面（同源 LAN 读接口 + 一个受模型配置门控的 POST）；无新鉴权面（整体水位不迁移，红线 2）。
- trusted stamp 不变：home 只读；summary 的身份由服务端 config 决定，请求参数不含身份。
- 出域最小化：digest 无正文、私密路径打码、落 egress 台账行（红线 7）。
- prompt 不是安全机制（红线 4）：事实正确性由「LLM 无渲染权」的结构保证，非提示词保证。
- 读边界维持现状全库（Web 面是用户自己的数据视图，红线 5 现状条款）。

## 8. 测试

- `tests/test_home_routes.py`：batch/actor 归组（**config 别名集为唯一判据**：`reconcile`/`unknown`/未知 source 一律归「外部」组，不得渲染成别名组头）、since 双向钳制（含 `since ≥ now` 未来窗）、`actions` 四计数（含 `.conflict-` 文件名计数）、**unseen 排除 watchdog + `last_seen_seq` 游标过滤（二次进首页不重报）**、summary 当日缓存复用（stub 模型调用计数 ==1）、**单飞并发（并发双发只起一次真实调用）+ 失败退避拒绝**、egress 台账行存在 + **回环端点零行**、**失败也先 flush 台账**、模型未配置 400、privacy 路径打码（digest 打码 / touched 用真实路径）。
- `tests/test_routes_order.py`：新路由入表钉。
- web_ui_audit 钉家族：Home 组件注册、渲染条件（`currentPath` 守卫）、logo→home 绑定、**logo 回首页的 `replaceState` 清 hash（断 pathname 回写）**、**G1 的 LLM 文本走 Vue 插值转义渲染（禁 v-html 钉）**、**S0/S1 短窗 G2 收束不渲染段题**。
- UI 按项目规则 3：**浏览器真机目检 + 用户明确认可后才提交**，「测试绿」≠「UI 对」。
- 平台注意：新增 fixture 写文件一律 `write_bytes`（Windows 换行铁律）。

## 9. 决策记录（用户 2026-11-22 拍板）

1. 形态：整页接管首屏（侧栏保留），为沉浸方向留位；本次不做壳改造。
2. 建议机制：规则行动清单为底 + LLM 一句话按需生成（✨点击才调模型）。
3. LLM 生成方式：单次调用（主 agent 模型 config），非工具态智能体。
4. 叙事：主智能体管家第一人称；bgagent = 团队/助手；vault = 书房。
5. 个人记忆：本次只留 reminder 与注入点（现状 vault 根目录文件条目 → 后续 DB 实体，每实体一条目）。
6. 架构：方案 A——单聚合端点 + 单前端组件。
7. 叙事字形：V1 中文维持现状（Source Serif 4 仅拉丁面，CJK 落雅黑/苹方无衬线），预览稿里的中文衬线视为愿景效果；CJK 衬线进 §10 演进位。预览稿：`2026-11-22-home-butler-landing-preview.html`（同目录）。
8. 短期离开分级：S0–S4 全套采纳（阈值 15min/6h/2d 可调），S0/S1 不报数字；`lastPresence` 仅 visible 写（写点共四，全受 visible 门控，含 boot 补锚）；「还没看过」改 seq 已读游标；G3 固定 3d 窗。备选「下次 boot 才结算」方案弃用。
9. oracle 送审修订全套并入：logo 回首页清 hash（B1）、「代改」计数只数 agent+别名排除 watchdog（B2）、config 别名集为分桶唯一判据（M3）、summary 单飞+失败退避（M4）、egress 行级接线+失败先 flush（M5）、监控退出重取（M6）、其余 minor 全落正文。

## 10. 演进位（不做，但承重）

- **个人记忆**（用户明示的 reminder）：现状为 vault 根目录下的文件条目，后续可能转为 db 存储实体（每实体一条目）。钩子 = `assemble_home_digest(..., personal_profile=...)`；实体化后可向 G1（懂你的措辞）与 G4（偏好相关待办）扩展。实施时勿让 home 对根目录记忆文件的形态产生任何硬编码假设。
- **CJK 叙事衬线**：预览稿的中文衬线是愿景效果。若未来要落地：自托管思源宋体 SC 子集（G1 一句话/别名等动态文本必须全覆盖常用字 → 首屏字体加载为百 KB～MB 级决策，需单独评审），不做系统宋体回退（Windows SimSun 大字观感差，三端不一致）。
- **沉浸式壳**：固定页进浮层、菜单展开态、文档操作快捷键——壳变了 Home 零改动。
- **管家深度模式**：带只读工具的智能体读记忆/近期会话后给建议（复用 bgagent 无头运行先例）；触发形态（按需按钮 or 周期）届时再定。

## 11. 实施注记（留给计划阶段的已知不确定点）

- 设备同步在 change_log 的归因现状：上传通道今天记 source=web。若实施期核实「无法区分设备同步与本人编辑」，则 G2 的「我从您设备上接的」组在 V1 缺省不渲染（G4 冲突条目不受影响），不为此改归因（会牵动 sync lane 回归）。核实结论写入实施计划。
