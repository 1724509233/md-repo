# 首页管家日报 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 打开网站第一眼是「管家日报」首页：主智能体第一人称汇报缺席窗内系统活动、用户近三天脉络、待裁决行动清单，按需一句 LLM 点评。

**Architecture:** 后端新增一个路由域 `server/routes/home.py`（一个只读聚合 GET + 一个受模型门控的按需 POST summary，egress 行级台账）；前端新增 `Home.js` 组件只在「主区无当前文档且非监控页」时占据主区，缺席分级/文案由 `utils/homeVoice.js` 纯函数合成，事实数字全部结构化渲染不经 LLM。

**Tech Stack:** FastAPI + SQLite（index.db / ledger.db）、openai SDK `AsyncOpenAI`（非流式单发）、Vue 3 CDN 无构建、pytest + TestClient、web_ui_audit 字符串钉家族。

**Spec:** `docs/superpowers/specs/2026-11-22-home-butler-landing-design.md`（本计划逐节对照它；文案语料、阈值、形状以 spec 为准）

## Global Constraints

- 分支 `feat/home-butler`（基于 main）。**本 worktree 测试一律用 `python -m pytest`**（module 模式让 cwd 的 `server/` 压过指向主 checkout 的可编辑安装）；**不要**重跑 `pip install -e`（会把共享环境里的包指向本 worktree，干扰并行会话）。
- 文案：spec §3 分级表与 §4.4 语料**逐字使用**；S0/S1 不出现时长数字。语气：沉稳、简洁、克制；不卖萌、不堆 emoji、不过度拟人。
- 阈值/窗口：缺席分级 15min / 6h / 2d；系统活动窗钳制 `[now−7d, now]`；我的活动窗固定 3d；summary 窗固定 7d；失败退避 300s；summary 一句话 ≤40 字量级。
- 前端 token 只能用 style.css 现有变量：`--paper #F4EFE6 / --ink #211D18 / --ink-soft #6E655A / --line #D8CFBF / --surface #FBF8F2 / --brass #9A6B32`（黄铜唯一强调色）。
- G2/G3/G4 每行与所有数字前端结构化渲染；LLM 文本只进 G1 一句话且必须 Vue 插值（`{{ }}`）渲染，**Home.js 内禁止 `v-html`**。
- 测试 fixture 写文件一律 `write_bytes`（Windows 换行铁律）。不新增依赖、不新增鉴权面、网络监听面不变。
- 路由表顺序被 `tests/test_routes_order.py` 快照钉死：新路由必须同步 `EXPECTED` 与计数注释。
- **UI 目检门（项目规则 3）**：Task 6 通过用户浏览器目检前，前端渲染代码不得先于目检提交合入 main（分支内提交允许）。
- 合并预告：`feat/settings-page` 也改 app.js hash watch 区（`#settings` 支路在本分支不存在）；两分支后合入者负责在合并时同时保留本计划的 null-清除支路与对方的 `#settings` 支路。

## Review Focus

（spec 隐含、任务测试不全覆盖、最容易咬人的输入/场景；实施时对照自查）

1. **logo 回首页 → F5/重开标签**：必须落首页（hash 已清）。Task 4 钉 + Task 6 目检脚本第 2 步。
2. **首页上按浏览器前进/后退**：hash 空 → `parseHash` 返回 null → hashchange handler no-op，首页保持、不弹旧文档。目检脚本第 3 步（代码已有 `if (!h) return` 天然覆盖，无需新码）。
3. **隐私模式 / localStorage 禁用**：home 相关全部 `setItem/getItem` 走 try/catch 助手；读不到 → 泛化 G1（时段问候、无数字、无首次句），页面不崩。Task 4 钉 token + 目检脚本第 5 步。
4. **未配置 bgagent（`ctx.bg_targets` 为 None）**：GET /api/home 必须 200，组只剩「我自己动手的/外部」，unseen 只数 `agent`。Task 2 钉。
5. **空窗出摘要**：窗内零活动也要调模型（digest 含「这段时间没有动静」行），G1 数字与 G4 不受该文本影响。Task 3 钉。
6. **云端端点 vs 回环**：summary 出域必须有 egress 台账行；回环（本地模型）零行是正确行为。Task 3 双钉。

---

### Task 1: `GET /api/home` — 窗口钳制、actor 分桶、my_days

**Files:**
- Create: `server/routes/home.py`
- Modify: `server/routes/__init__.py`（import 行 + `ROUTE_MODULES` 元组，在 `agent,` 后加 `home,`——home 无路径冲突，置于 trash 前即可）
- Modify: `tests/test_routes_order.py`（`EXPECTED` 插两行 + 计数注释 48→50）
- Test: `tests/test_home_routes.py`（新建）

**Interfaces:**
- Consumes: `changes.get_changes_sources(conn, since=0.0, ops=("create","update","delete","move"), exclude_sources=None, limit=500, since_seq=0) -> list[dict]`（行键 `seq,ts,source,op,action,path,from_path`）；`changes.get_changes(conn, since=0.0, path=None, source=None, limit=50, privacy_dirs=())`；`ctx.db_conn / ctx.ledger_conn / ctx.vault_dir / ctx.bg_targets`（键即别名集）。
- Produces（Task 2/3 在同一文件续写；Task 4/5 消费响应形状）:
  ```python
  # server/routes/home.py
  SYSTEM_WINDOW_DAYS = 7
  MY_WINDOW_DAYS = 3

  def _effective_since(since: float | None, now: float) -> float   # 钳制到 [now-7d, now]，None→now-7d
  def _bucket(source: str, aliases: frozenset[str]) -> tuple[str, str | None]
      # "agent"→("agent",None); source in aliases→("bgagent",source); 其余→("external",None)
  def build_home_payload(db_conn, *, vault_dir, aliases: frozenset[str],
                         since: float | None, last_seen_seq: int,
                         ledger_conn=None, now: float | None = None) -> dict
      # 返回 {"window": {"since": f, "until": f},
      #       "system_groups": [{"actor","alias","last_ts","ops":[{"seq","ts","action","path","from_path"}]}],  # last_ts 降序；组内 ts 降序
      #       "my_days": [{"date","counts":{...},"ops":[...]}],   # 固定 now-3d 窗，按服务器本地日期，倒序
      #       "actions": {...}}                                   # Task 2 填；Task 1 先返回 {"pending_proposals":0,"unseen_agent_changes":0,"conflicts":0,"trash_batches":0}
  def register(app, ctx) -> None   # @app.get("/api/home") async，query: since: float | None = None, last_seen_seq: int = 0
  ```
  `ops[].seq` 是客户端写 `lastSeenChangeSeq` 游标所需，勿省。

- [x] **Step 1: 写失败测试** `tests/test_home_routes.py`（fixture 组合 `client, db_conn, vault_dir` + 自建带 `bg_targets` 的 app：复制 `tests/test_agent_stream_routes.py:124-128` 的 create_app+TestClient 自建范式，传 `bg_targets={"守夜人": {"armed_target": None, "runs_cap": None}}`）。三个测试：
  ```python
  # 造数惯用法：from server.vault.db.changes import log_change
  # log_change(db_conn, path, "update", source=..., ts=...) 逐行写，自带 commit
  def test_buckets_and_ordering(client, db_conn): ...
      # 种 agent / 守夜人 / watchdog / reconcile / unknown 各一行（now 附近）
      # 断言：恰好 3 组；组头 actor 集 == {"agent","bgagent","external"}；external 组 ops 含 watchdog/reconcile/unknown 三行；
      # 「守夜人」组 alias=="守夜人"；组序按 last_ts 降序；ops 按 ts 降序；op 项含 seq 键
  def test_window_clamp(client, db_conn): ...
      # now+100 的 since → window.since == 钳制后值且 ≤ window.until；8 天前行（since=0 请求）不出现在任何组
  def test_my_days_fixed_3d(client, db_conn): ...
      # source="web" 两行：2 天前、5 天前 → my_days 只含 2 天前的日期行，且 counts 正确；即使请求 since 拉到 7 天前也不含 5 天前
  ```
- [x] **Step 2: 跑测试确认失败**：`python -m pytest tests/test_home_routes.py -v` → FAIL（404 / ImportError）。
- [x] **Step 3: 实现** `home.py`（按上面签名；system 行取 `get_changes_sources(db_conn, since=eff, exclude_sources={"web"}, limit=2000)`，my 行取 `get_changes(db_conn, since=now-3d, source="web", limit=500)`；别名集 `frozenset((ctx.bg_targets or {}).keys())` 在路由层传入）；注册进 `ROUTE_MODULES` 与路由表。
- [x] **Step 4: 更新路由钉**：`tests/test_routes_order.py` `EXPECTED` 按注册实际位置插 `(("GET",), "/api/home")`，计数注释同步。
- [x] **Step 5: 全绿**：`python -m pytest tests/test_home_routes.py tests/test_routes_order.py -v` → PASS。
- [x] **Step 6: Commit**：`git add -A && git commit -m "feat(home): GET /api/home 聚合端点——窗口钳制/别名集分桶/3d 足迹窗"`

### Task 2: `actions` 四计数（含 seq 已读游标）

**Files:**
- Modify: `server/routes/home.py`（填实 `build_home_payload` 的 actions 段）
- Modify: `tests/test_home_routes.py`

**Interfaces:**
- Consumes: Task 1 的 `build_home_payload`；`services/proposals.status_summary(conn) -> {"by_status": {...}, ...}`（传 `ctx.ledger_conn`，`by_status["awaiting_review"]` 即待审计数）；`proposal_records.create(conn, ...)`（ledger 域造 awaiting_review 行——参数以 `server/vault/db/proposal_records.py:39` 现签名为准，测试里参考既有调用点）；`file_index.get_all_files(conn)`；`services/trash.list_trash(vault_dir)`。
- Produces: `actions = {"pending_proposals": int, "unseen_agent_changes": int, "conflicts": int, "trash_batches": int}`。unseen 语义 = `seq > last_seen_seq` 且 source ∈ {"agent"} ∪ 别名集（**watchdog/unknown/reconcile 恒不计**）；conflicts = files 表路径含 `.conflict-` 的行数；实现取 `get_changes_sources(db_conn, since_seq=last_seen_seq, exclude_sources={"web"}, limit=5000)` 后 Python 侧 allow-filter（集合是唯一判据，SQL 谓词只是省量）。

- [x] **Step 1: 写失败测试**
  ```python
  def test_actions_counts(client, db_conn, ledger_conn, vault_dir, home_client): ...
      # proposals: proposal_records.create(ledger_conn, ...) 两条默认状态行
      # conflicts: from server.vault.db.file_index import upsert_file——path 用 "notes/a.conflict-pc.md"（write_bytes 落盘后 upsert）
      # trash: (vault_dir/".trash"/"b1"/"x.md").parent.mkdir(...) + write_bytes 手工一批（形状对齐 list_trash 文档约定）
      # unseen: log_change agent×2 / 守夜人×1 / watchdog×5；last_seen_seq=0 请求 → 3；
      #         last_seen_seq=最大 agent seq → watchdog 再多也不涨（假账防线）
  def test_actions_no_bg_targets(...):  # create_app 不传 bg_targets → 200；unseen 只数 agent 行
  ```
  （`home_client` = Task 1 里自建的带 bg_targets app 的 fixture 化提取。）
- [x] **Step 2: 跑测试确认失败**：`python -m pytest tests/test_home_routes.py -v` → actions 断言 FAIL。
- [x] **Step 3: 实现**（proposal 计数用 `status_summary(ctx.ledger_conn)["by_status"]["awaiting_review"]`；trash 用 `len(list_trash(vault_dir))`；conflicts 用 get_all_files Python 计数）。
- [x] **Step 4: 全绿**：`python -m pytest tests/test_home_routes.py -v` → PASS。
- [x] **Step 5: Commit**：`git commit -am "feat(home): actions 四计数——待审提案/冲突/回收站/seq 游标式代改计数"`

### Task 3: `POST /api/home/summary` — 单飞/缓存/退避 + egress 行级接线

**Files:**
- Modify: `server/routes/home.py`（新增 SummaryCtl + `assemble_home_digest` + 路由；`from openai import AsyncOpenAI` **直接 import 进本模块命名空间**（测试 monkeypatch 点 = `server.routes.home.AsyncOpenAI`））
- Modify: `server/agent/prompts.py`（文件尾追加 `HOME_SUMMARY_SYSTEM` 常量：§4.3 语气 + 一句话 ≤40 字 + 只可基于 digest、不得提及清单外动作）
- Modify: `tests/test_home_routes.py`

**Interfaces:**
- Consumes: Task 1/2 的 `build_home_payload`（summary 用自己的 7d 全窗，`last_seen_seq=0`）；`services/egress` 的 `EgressTarget(channel, actor, run_id, endpoint_host, model_id)`、`should_record`、`flush_touches(conn, privacy_dirs, target, touched: dict[str,str])`（fail-open）、`parse_endpoint`；`ctx.ai_config`（字段 `base_url, api_key, model, temperature`）。
- Produces:
  ```python
  SUMMARY_COOLDOWN_S = 300
  class SummaryCtl:                       # 挂 app.state.home_summary（测试 seam）
      cache: dict[str, str]               # 服务端本地日期 "%Y-%m-%d" → text
      _inflight: asyncio.Task | None      # 单飞
      _last_fail: float                   # 退避锚
  class SummaryError(Exception): status: int; code: str
  async def generate_summary(ctx, ctl: SummaryCtl) -> str   # 不建 HTTP；HTTP 语义在路由层
  def assemble_home_digest(payload: dict, *, privacy_dirs: tuple[str, ...],
                           personal_profile: dict | None = None) -> tuple[str, dict[str, str]]
      # → (打码文本（privacy 命中路径 → 〈私密〉），touched = {真实非隐私路径: "digest"})
      # 零活动 digest 必须含「这段时间没有动静」行（Review Focus 5）；personal_profile 参数本次恒 None（演进位钩子）
  # 路由：400 {"error":"model_not_configured"}；冷却 429 {"error":"cooldown"}；失败 502 {"error": str(e)}；成功 200 {"text": str}
  # LLM：非流式 create(model=ai_config.model, messages=[system=HOME_SUMMARY_SYSTEM, user=digest],
  #      temperature=ai_config.temperature, max_tokens=200)；content strip 后为空视同失败
  # egress：target = EgressTarget("home", "agent", "", parse_endpoint(ai_config.base_url), ai_config.model)；
  #      should_record 真 → flush_touches(ctx.ledger_conn, tuple(ctx.privacy.dirs) if ctx.privacy else (), target, touched)
  #      ——在模型调用完成后、无论成败都执行（失败也先记账，红线 7）
  ```

- [x] **Step 1: 写失败测试**（stub 抄 `tests/test_egress_runtime.py:67-91` 的 `_Completions/_Client` monkeypatch 范式，patch 点 `server.routes.home.AsyncOpenAI`；自建 app 传 `ai_config`（`base_url="http://model.test/v1"` 云形状）+ `ledger_conn` + privacy（PrivacyConfig 构造照抄既有测试用法，grep `PrivacyConfig(` in tests））：
  ```python
  def test_summary_requires_model(client): ...                 # 无 ai_config 的 app fixture → 400 model_not_configured
  def test_summary_cache_singleflight_backoff(...):            # 首次 200 且 stub create 计 1；当日第二次 POST → create 仍 1（{"text"} 相同）；
                                                                # 直接 await asyncio 双发 generate_summary 对 → create 仍 1（单飞）；
                                                                # stub 改抛异常 → 502；紧随第二次 POST → 429
  def test_summary_egress_and_masking(...):                    # digest 无 privacy 路径原文、含 〈私密〉；ledger 的 egress 表行含真实非隐私路径、无隐私路径、channel=="home"；
                                                                # 断言失败路径（stub raise）后台账行仍在
  def test_summary_loopback_zero_rows(...):                    # ai_config fixture 默认 localhost → 台账零行但 200
  def test_summary_empty_window_digest(...):                   # 零活动窗 → 仍调用 stub；捕获的 user 消息含「这段时间没有动静」
  ```
- [x] **Step 2: 确认失败**：`python -m pytest tests/test_home_routes.py -v` → 新测试 404。
- [x] **Step 3: 实现**（prompts 常量 → digest 组装 → ctl → 路由）。
- [x] **Step 4: 全绿**：`python -m pytest tests/test_home_routes.py tests/test_egress_runtime.py -v`。
- [x] **Step 5: Commit**：`git commit -am "feat(home): POST /api/home/summary——单飞/当日缓存/失败退避 + egress 行级台账 + digest 组装"`

### Task 4: 前端导航骨架（goHome / hash 修复 / 心跳 / Home 挂载）

**Files:**
- Modify: `server/templates/js/app.js`
- Modify: `server/templates/js/components/TopBar.js`
- Modify: `server/templates/js/api.js`
- Create: `server/templates/js/components/Home.js`（本任务只做最小占位：inject、拉数、G1 一行 + 错误态；完整渲染在 Task 5）
- Test: `tests/test_web_ui_audit.py`（追加钉）

**Interfaces:**
- Consumes: app 既有 `provide('state'/'api'/'manage'/...)`、`openDoc`、`onMemoryRailClick`（app.js:579）、`activePanel`（app.js:64，二级边栏 ref）、`state.files`、hash watch（app.js:751-755 的 `if (!p) return`）、`exitMonitorPage`（:95）、`onMounted`（:788）。
- Produces:
  - app.js：`state.homeRefreshSeq`（进 setup return）；`goHome()`（`state.currentPath` 非空→置 null；空→no-op）；hash watch null 支路 `if (!p) { if (location.hash) history.replaceState(null, '', location.pathname); return }`；`lastPresence` 心跳三写点 + 5min interval（`const LS_LAST='mdvault.lastPresence'`，try/catch 包裹）；`exitMonitorPage` 内 `state.homeRefreshSeq++`；模板主区（:1069 层内）插 `<Home v-if="!state.currentPath" @open-doc="openDoc" @open-memory="onMemoryRailClick()" @open-trash="openTrashFace()" />`，doc-scroll 容器加 `v-show="!!state.currentPath"`；新函数 `function openTrashFace() { activePanel.value = 'trash' }`（进 return）；TopBar 标签加 `@go-home="goHome()"`。
  - TopBar.js：`emits` 追加 `'go-home'`；模板 `.topbar-logo` 容器（:93）加 `@click="goHome()"` 与 `cursor:pointer` 处理（复用 `.topbar-logo` 类，样式若需 hover 给 style.css `.topbar-logo{cursor:pointer}` 一行）；setup return 加 `goHome`。
  - api.js：`async getHome(sinceMs, lastSeenSeq)` → `GET /api/home?since=..&last_seen_seq=..`（since 传秒：服务端语义是 epoch 秒；`Math.floor(sinceMs/1000)`）；`async genHomeSummary()` → POST。
  - Home.js（占位版即可编译运行）：`props: {}`，`emits: ['open-doc','open-memory','open-trash']`；onMounted + `watch(() => state.homeRefreshSeq)` 触发 `api.getHome(lastPresenceMs ?? Date.now()-7d, lastSeenSeq)`；G1 至少渲染时段问候字符串。
- [x] **Step 1: 先写钉（红）**：在 `tests/test_web_ui_audit.py` 追加（惯例 = 稳定字符串 token）：
  ```python
  def test_home_landing_wired(): app.js 源含："'Home'"（import/component 注册两处之一）、
      "history.replaceState(null, '', location.pathname)"、"v-if=\"!state.currentPath\""、
      "state.homeRefreshSeq"、"mdvault.lastPresence"、"pagehide"、"visibilitychange"、"@go-home"
  def test_home_component_registered(): "app.component('Home', Home)" in app.js
  ```
- [x] **Step 2: 确认失败** → 实现 app.js / TopBar.js / api.js / Home.js 占位（按上面 Interfaces 逐条，行号为当前 main 坐标，编辑以内容锚定）。
- [x] **Step 3: 全绿 + 回归**：`python -m pytest tests/test_web_ui_audit.py -v` 与 `python -m pytest tests/ -x -q`（全量防误伤既有钉）。
- [x] **Step 4: 手测导航闭环**：`python -m server.main config.yaml`（若无 config 复制 config.example.yaml）→ 打开首页占位 → 点文档 → 点 logo 回首页 → **F5 仍是首页** → 后退键不动。
- [x] **Step 5: Commit**：`git commit -am "feat(home): 落地导航骨架——logo 回首页清 hash、lastPresence 心跳、Home 挂载与监控退页重取"`

### Task 5: Home 完整渲染 + homeVoice 文案层 + 样式

**Files:**
- Create: `server/templates/js/utils/homeVoice.js`
- Create: `server/templates/js/components/Home.js`（Task 4 占位重写为完整版）
- Modify: `server/templates/style.css`（尾部追加「首页 · 管家日报」分节——视觉与 `docs/superpowers/specs/2026-11-22-home-butler-landing-preview.html` 一致，类名统一 `hb-` 前缀，token 只用 :root 现有变量；中文字形按 spec §9.7 **不加** CJK 衬线回退）
- Test: `tests/test_web_ui_audit.py`（追加钉）

**Interfaces:**
- Consumes: Task 4 的 api 形状与 emits；`state.files`（找第一条 `.conflict-` 路径给「帮我看看」→ `emit('open-doc', p)`）。
- Produces（homeVoice.js 纯函数契约）:
  ```js
  export function tierOf({ bootedOnce, lastPresence, now })
    // lastPresence 不可读→'nostorage'；无值→'first'；bootedOnce→'S0'；
    // <15*60 →'S1'；<6*3600→'S2'；<2*86400→'S3'；else 'S4'（单位秒差值）
  export function absentPhrase(absentSec)   // <900→''；<2d→'N 个小时'（≥24h 用「一天」级中文量词）；≥2d→'N 天又 M 小时'
  export function greetingOf(now)           // <12 早上好 / <18 午后好 / <23 晚上好 / else 夜深了
  export const HOME_COPY = { ... }          // spec §3 分级表 + §4.4 语料逐字搬入（含 S0/S1 变体、G2/G3/G4 段题与空态、组头模板、页脚）
  ```
- Home.js 行为要点：G1 按 tierOf 出话（S0/S1 无数字；G4 非空切变体文案——量词中文「N 件/件事」按 spec 语料）；✨按钮 → `genHomeSummary()`（生成中禁用；`model_not_configured` → 按钮旁「管家还没接上模型」；429/502 → 保持分级文案）；G2 `tier ∈ {S0,S1}` → 单句收束不渲段题卡；组头用 groupHead（Task 1 actor/alias 字段）；G3 最近一天默认展开、S0/S1 段题变体；G4 盒子三行动作（记忆控制台 emit / unseen → `document.querySelector('#hb-g2').scrollIntoView` / trash emit）；渲染非空 system_groups 后 `try { localStorage.setItem('mdvault.lastSeenChangeSeq', String(maxSeq)) } catch {}`；模块级 `let bootedOnce = false`（首次 mount 置真）；所有文本 Vue 插值渲染。
- [x] **Step 1: 先写钉（红）**：homeVoice 源含 `'S0'..'S4'` 分支 token 与 `'mdvault.lastSeenChangeSeq'`；Home.js 源 **不含** `"v-html"`；Home.js 含 `'您刚离开，还没有新的事'`、`'我做不了主的事'`（语料在位）；style.css 含 `.hb-` 分节。
- [x] **Step 2: 确认失败** → 实现 homeVoice.js → style.css 分节 → Home.js 完整版。
- [x] **Step 3: 全绿 + 全量回归**：`python -m pytest tests/test_web_ui_audit.py -v && python -m pytest tests/ -x -q`。
- [x] **Step 4: Commit**：`git commit -am "feat(home): 管家日报完整渲染——分级文案 homeVoice、G2 信笺分组、G4 行动盒子、已读游标落笔"`

### Task 6: 浏览器目检门（用户验收，UI 提交前置）

**Files:** 无新文件（问题修回各文件）。

- [x] **Step 1:** `python -m server.main config.yaml`，按清单逐项过（桌面 + DevTools 移动模拟 + 隐私模式窗口）：
  1. 首屏即首页、无 hash；G1 文案与缺席时长正确（首次 → 首次句）；
  2. 开文档 → 点 logo 回首页 → **F5 仍首页**、URL 无 `#doc/`；后退/前进不弹文档；
  3. 造 activity：网页编辑几篇 + `log_change` 种 watchdog/agent/别名行 → G2 三桶正确、无「我让 reconcile 办的」；G3 只近 3 天；G4 三条动作全可跳（记忆控制台/锚滚 G2/回收站面）；
  4. ✨ 按钮：未配模型 → 提示；配本地模型 → 一句话出现且数字区纹丝不动；再点 → 文案秒回（缓存）；拔网线（模型不可达）→ 502 后按钮冷却、分级文案保留；
  5. 隐私模式窗口打开首页：不崩、泛化 G1；
  6. 移动端 375px：单列无横滚、G4 盒子不溢出；
  7. 监控页进出：退出后首页数据刷新。
- [x] **Step 2:** 用户明确认可 → 若有修改回 Task 4/5 文件并复跑钉 → 提交收尾：`git commit -am "fix(home): 目检回修"`（如有）。
- [x] **Step 3:** 汇报可合并；`docs/superpowers/plans/` 本文件勾选状态同步为已完成。

---

## Self-Review（写完后已核）

1. **Spec 覆盖**：§3 G1-G4→T1/T2/T4/T5；§4 文案/映射→T1 桶/T5 homeVoice；§5.1→T1/T2；§5.2→T3；§6.1 渲染+hash→T4；§6.2 请求→T4/T5；§6.3 游标+心跳→T4/T5；§7 红线→T3（egress/打码）+全局约束；§8 钉→各任务 Step1。无缺口。
2. **类型一致性**：`build_home_payload`/`generate_summary`/`assemble_home_digest`/tierOf 参数签名跨任务一致；API 键名（system_groups/my_days/actions/unseen_agent_changes）全文统一。
3. **步粒面**：造数/fixture 惯用法已指到既有文件行号（log_change、_Client stub、create_app 自建），无 TBD。
