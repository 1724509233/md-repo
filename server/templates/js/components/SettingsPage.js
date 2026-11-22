/* SettingsPage 组件：设置浮层（spec 2026-11-22 §6，2026-11-22 modal 改版）——config.yaml 页面化编辑。
 * 壳 = modal 浮层（用户拍板改版；原「整页接管」否决）：backdrop 点击关闭 + 居中对话框
 *   （role=dialog aria-modal），桌面 ≤880px×≤86vh 头部固定体部内滚，≤639px 全屏（壳高走 fixed
 *   布局视口链，禁 100vh）；折叠壳家族照 MobileSheet/panel-shell 纪律——常驻 DOM，折叠态
 *   visibility:hidden 出 Tab 序，转场仅 opacity+transform（style.css st-modal 族，RM 两态同杀）。
 * 入口 = app.js rail 底部齿轮钮 / 深链 #settings；与主页面零互斥（浮层不占位）。
 * 契约：props 无；inject('state') + inject('api')（api.getSettings/putSettings
 *   非 2xx 不抛，返回 {status,data}，仅网络失败 throw）；
 *   emits ['close']；开合源 = state.settingsOpen（app 置真），关闭（X/backdrop/Escape 任一路径）
 *   统一经 requestClose——dirty 时弹页内确认浮层（role=alertdialog 三选：保存并关闭 / 放弃修改 /
 *   取消，spec §6 用户改版，原生 confirm 已废），取消即不关；app 只接 @close 落地。
 *   state.settingsCloseSeq++（app Escape 链最高优先支）= 关闭请求信号，本组件 watch 后代跑守卫。
 * 焦点：开层入面板首个可见可交互元素（preventScroll 红线），关层归位 rail 齿轮钮（#settings-rail-btn）；
 *   确认浮层弹出时设置浮层 inert（焦点自然困于确认浮层），默认焦点 = 取消钮，Escape = 取消。
 * 保存状态徽标（取代旧成功反馈条，spec §6）：三张设置卡右上角 + 后台列表逐条目右上角，两态
 *   「已保存（中性）/ 未保存（高亮）」；dirty = 分段指纹（ai 段含密钥 touched/toolsMode/toolsSel、
 *   bg 段逐条目、misc 段=通知+日志+隐私）与 hydrate 基线比对，保存成功/409 重载重取基线即归零。
 *   顶部告警区（2026-11-22 用户改版：非字段校验族专席）只留 409 并发冲突与热生效/其它保存失败；
 *   成功不再有条——徽标归零即信号。
 * 表单项级校验（2026-11-22 用户改版，替代页顶 422 告警条）：422 与保存预检错误全部下沉字段级——
 *   出错字段红框 + 字段下红小字（--st-error 配色族）；预检 = 点保存时统一跑（无 blur 逐字段校验），
 *   有错不发 PUT，跳出错字段所在分段 + 滚动定位 + focus({preventScroll:true})（键盘红线）；
 *   字段被修改即清该字段错误，下次保存全量重算。预检规则只实现服务端已有规则（加载器 raise 点
 *   为法源，服务端仍是权威；文案本地化中文，422 回显 = 加载器原文剥实现 meta 前缀）。
 *   三级兜底（错误不可见是红线）：message 定位不到字段 → 所属卡片顶一行；section 也定不了 → toast；
 *   bg 条目字段错误在折叠态不可见 → 条目行涉错红标 + 自动展开首个涉错条目。
 *   工具不准空（用户拍板）：主智能体与 bg 条目 tools 均不得空选（加载器拒空列表 / v2 法层必填）。
 * 工具权限三态（meta.tool_perms = 注册表单一真相，前端零镜像常量）：勾选+allow=允许（实心）、
 *   勾选+ask=询问（描边+后缀）、未勾选=拒绝（虚线弱样式）；aria-pressed 恒为「是否授权」，
 *   三态语义进 title（英文原名开头，对账 config 用）。
 * 表单内部逻辑（分段导航/三态密钥/保存流程/payload 铁律）与整页模式时代零改动（Task 7-9 产物）。
 * 后台 agent 段（spec §6.2 / §5.4）：卡片列表 = 名称 / 标识 / 启用开关（即表单字段）/ 运行徽标；
 *   徽标数据源 = 全站 30s 脉搏 state.memoryStatus.agents，打开本段时若尚未到货则静默补拉一次，
 *   失败不报错不阻塞表单。条目表单四分组（基本 / 调度与预算 / 工具 / 模型）；
 *   未建模键（escalation 等）经条目 raw 副本直通：hydrate 存 raw → 保存时非建模键合并回条目，
 *   不可编辑、不可丢；is_executor 条目只读展示（raw 原样直通），不进编辑。
 *   api_key 每条目独立 touched（三态同 ai 段）；改名实时提示按 spec S3（api_key 清空 + 计权归零）。
 *   复用主智能体模型（spec §6.2）：条目展示位 model_matches_ai → hydrate 落 fromAi 初值（该展示位
 *   只进编辑镜像、绝不入 raw 直通面——新别名条目会把它原样落进 config）；勾选态隐藏模型字段组
 *   （表单值原地保留，取消勾选即回显），payload 条目 = 其余键照常 + model_from_ai:true 且省略 model
 *   （服务端以写回后的主模型段物化，瞬态键不落盘）；勾选计入条目 dirty 指纹。执行器条目 raw 直通不受扰。
 *
 * payload 构造契约（spec §4.1/§4.2，buildPayload 是本页唯一写面——四条铁律）：
 * ① api_key 三态：未触碰（touched 集合无此键）⇒ payload 省略该键（服务端回填原明文）；
 *    「清除」小钮 ⇒ 发 ""（显式清空）；输入新值 ⇒ 发新值。GET 回的 {set,mask} 掩码形态永不回传。
 * ② background_agents 条目必须全量回传——PUT 按 alias 对账、payload 缺席即删条目；条目内
 *    model.api_key（掩码形态）未触碰一律剥除（缺省=保持，同①）。条目可编辑：
 *    编辑面只建模表单可见键，raw 副本中的其余键（escalation 等）保存时原样合并回条目。
 * ③ 空标量省略键（不发 null——加载器 int(null) 即炸→422；省略 = 加载器缺省/服务端保持原值，
 *    两种后端解读下都零崩溃）；显式语义各有专职：tools null=全量、privacy.dirs []=空名单、
 *    ai null=删段、api_key ""=清除。
 * ④ GET ai 段缺位 ⇒ 表单照常空白渲染（页面可新建段）：用户整段零改动 ⇒ payload ai:null（幂等删段）；
 *    改动过任何字段 ⇒ 按填写内容整段提交。
 * 文案纪律（AGENTS Frontend Rule 2）：正文零字段名/端点路径/设计概念名；
 *   命名口径统一「主智能体 / 后台智能体 / 杂项」（2026-11-22 用户拍板，与监控页「智能体」措辞对齐）。
 * 记忆段（沉浸式壳 T6，spec 2026-11-22-immersive-shell §6）：st-nav 尾「记忆」段整体迁入
 *   MemoryConsole（队列/详情/报告三面，宽度随 pane）；数据零自建轮询——state.memoryStatus/
 *   Proposals/Reports 由 app 30s pollMemory 单一轮询注入，本段只经组件既有 props 下传消费。
 *   关闭守卫边界：记忆段不进 dirty 指纹（dirty = 三表单段 hydrate 基线比对，记忆 pane 无表单
 *   字段不可入指纹）；settingsCloseSeq → requestClose 链零改动（X/backdrop/Escape 仍先过 dirty
 *   确认浮层）。徽标 memoryBadge（badgeParts.total = 待审+失败）>0 才画，title 口径同 FAB 设置子钮。
 */
import { showToast } from '../utils/toast.js'
import { MemoryConsole } from './MemoryConsole.js'
import { ICONS } from '../utils/icons.js'

const { ref, computed, watch, nextTick, inject, onBeforeUnmount } = Vue

// 分段导航（桌面左列锚点 / ≤639 分段钮同一数据源；命名口径 2026-11-22 用户拍板，与监控页对齐）
const SECTIONS = [
  { id: 'ai', label: '主智能体' },
  { id: 'bg', label: '后台智能体' },
  { id: 'misc', label: '杂项' }
]

// 主智能体段数值参数行：字段键 + 完整 label（label 即用户语言，min/step 进原生 input 属性）
const AI_PARAMS = [
  { key: 'max_tokens', label: '最大输出长度', min: 1, step: 1 },
  { key: 'temperature', label: '生成温度', min: 0, max: 2, step: 0.1 },
  { key: 'context_window', label: '上下文窗口', min: 1, step: 1 },
  { key: 'max_rounds', label: '最大轮次', min: 1, step: 1 },
  { key: 'read_doc_max_chars', label: '单文档读取上限（字符）', min: 1, step: 1 },
  { key: 'confirm_timeout', label: '操作确认超时（秒）', min: 1, step: 1 }
]
const LOG_LEVELS = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']

// ─── 后台智能体段（Task 9；UI 文案统一「后台智能体」，代码键名 background_agents 不动）───
// 触发事件全集与后端 change_log 操作集对齐（服务端仍是权威：保存走加载器校验，422 原文回显）。
// 契约缺口备案：/api/settings 的 _meta 只带 tools/verbs/restart_fields，trigger 事件集未随
// payload 下发，此处前端镜像该闭合集（后端加事件须同步此行，漂移后果 = 422 提示而非静默错写）。
const BG_TRIG_OPS = ['create', 'update', 'delete', 'move']
const BG_TRIG_OP_CN = { create: '新建', update: '修改', delete: '删除', move: '移动' }
const BG_TRIG_SOURCES = [{ v: 'change', label: '文档变更' }, { v: 'recall', label: '召回缺口' }]
// 调度与预算数值行 / trigger 数值行 / 模型数值行（label 即用户语言，min/step 进 input 属性）
const BG_SCHED = [
  { key: 'max_rounds', label: '最大轮次', min: 1, step: 1 },
  { key: 'timeout_s', label: '超时上限（秒）', min: 1, step: 1 },
  { key: 'budget_runs_per_day', label: '每日运行上限', min: 0, step: 1 },
  { key: 'budget_max_proposals', label: '每日提案上限', min: 0, step: 1 }
]
const BG_TRIG_NUMS = [
  { key: 'arm_count', label: '触发计数', min: 1, step: 1 },
  { key: 'min_interval_h', label: '最小间隔（小时）', min: 0, step: 0.1 },
  { key: 'low_score', label: '低分阈值', min: 0, max: 1, step: 0.05 }
]
const BG_MODEL_NUMS = [
  { key: 'context_window', label: '上下文窗口', min: 1, step: 1 },
  { key: 'max_tokens', label: '最大输出长度', min: 1, step: 1 },
  { key: 'temperature', label: '生成温度', min: 0, max: 2, step: 0.1 }
]
// 建模键全集：raw 中其余键（escalation 等）保存时原样合并回条目（不可编辑不可丢）。
// model_matches_ai 是 GET 展示位（非 config 键），显式建模防其经非建模键直通面漏进 PUT。
const BG_MODELED = new Set(['alias', 'name', 'enabled', 'cron', 'target_dir', 'prompt', 'report_dir',
  'max_rounds', 'timeout_s', 'budget_runs_per_day', 'budget_max_proposals', 'trigger', 'tools', 'verbs', 'model',
  'model_matches_ai'])
const BG_TRIG_KEYS = new Set(['source', 'events', 'metric', 'arm_count', 'min_interval_h', 'low_score'])
const BG_MODEL_KEYS = new Set(['provider', 'model_id', 'base_url', 'api_key', 'context_window', 'max_tokens', 'temperature'])

function bgBlankModel() {
  return {
    provider: '', model_id: '', base_url: '', context_window: '', max_tokens: '', temperature: '',
    keySet: false, keyMask: '', keyTouched: false, keyInput: ''
  }
}
function bgBlankTrigger() {
  return { source: 'change', events: [], arm_count: '', min_interval_h: '', low_score: '' }
}
function bgBlankEntry() {
  return {
    raw: {}, readOnly: false, name: '', alias: '', enabled: true,
    target_dir: '', prompt: '', report_dir: '', cron: '',
    max_rounds: '', timeout_s: '', budget_runs_per_day: '', budget_max_proposals: '',
    tools: [], verbs: [], trigger: null, model: bgBlankModel(), fromAi: false
  }
}
// raw 条目（GET payload 形）→ 编辑镜像；api_key 止步掩码 {set,mask}，touched 每条目独立；
// 展示位 model_matches_ai 落 fromAi 初值后从 raw 副本摘除（spec §6.2：瞬态键，绝不回传）
function bgEntryView(raw) {
  const r = raw && typeof raw === 'object' ? raw : {}
  const m = r.model && typeof r.model === 'object' ? r.model : {}
  const key = m.api_key && typeof m.api_key === 'object' ? m.api_key : null
  const trig = r.trigger && typeof r.trigger === 'object' ? r.trigger : null
  const rawCopy = JSON.parse(JSON.stringify(r))
  delete rawCopy.model_matches_ai
  return {
    raw: rawCopy,
    readOnly: !!r.is_executor,
    name: strOrEmpty(r.name), alias: strOrEmpty(r.alias),
    enabled: r.enabled !== false, // 缺省 = 启用（加载器同语义）
    target_dir: strOrEmpty(r.target_dir), prompt: strOrEmpty(r.prompt),
    report_dir: strOrEmpty(r.report_dir), cron: strOrEmpty(r.cron),
    max_rounds: numOrEmpty(r.max_rounds), timeout_s: numOrEmpty(r.timeout_s),
    budget_runs_per_day: numOrEmpty(r.budget_runs_per_day),
    budget_max_proposals: numOrEmpty(r.budget_max_proposals),
    tools: Array.isArray(r.tools) ? [...r.tools] : [],
    verbs: Array.isArray(r.verbs) ? [...r.verbs] : [],
    trigger: trig ? {
      source: strOrEmpty(trig.source) || 'change',
      events: Array.isArray(trig.events) ? [...trig.events] : [],
      arm_count: numOrEmpty(trig.arm_count),
      min_interval_h: numOrEmpty(trig.min_interval_h),
      low_score: numOrEmpty(trig.low_score)
    } : null,
    model: {
      provider: strOrEmpty(m.provider), model_id: strOrEmpty(m.model_id), base_url: strOrEmpty(m.base_url),
      context_window: numOrEmpty(m.context_window), max_tokens: numOrEmpty(m.max_tokens),
      temperature: numOrEmpty(m.temperature),
      keySet: !!(key && key.set), keyMask: strOrEmpty(key && key.mask),
      keyTouched: false, keyInput: ''
    },
    // 复用勾选初值 = 服务端展示位（保存成功/重载后随 hydrate 归真）
    fromAi: r.model_matches_ai === true
  }
}
const bgTitle = e => String(e.name || '').trim() || String(e.alias || '').trim() || '（未命名条目）'
// alias 与原值相异即「改名 = 删 + 增」（spec §5.4/S3）——空原值（新条目）不算改名
const bgRenamed = e => {
  const orig = String((e.raw && e.raw.alias) || '')
  return !!orig && String(e.alias || '').trim() !== orig
}
// 分界线即时提示（前端提示、服务端权威）：提案角色与直写工具不得同持
const bgConflict = e => e.tools.includes('propose') && e.tools.includes('write_doc')

// 编辑镜像 → PUT 条目：建模键按表单写值 + raw 非建模键原样直通 + 空标量省略键（铁律③）
function buildBgEntry(vm) {
  const raw = vm.raw && typeof vm.raw === 'object' ? vm.raw : {}
  // 执行器条目不进编辑：raw 原样直通（剥掩码密钥，缺省=保持原明文）
  if (vm.readOnly) {
    const r = JSON.parse(JSON.stringify(raw))
    if (r.model && typeof r.model === 'object') delete r.model.api_key
    return r
  }
  const setStr = (o, k, v) => { const s = String(v == null ? '' : v).trim(); if (s) o[k] = s }
  const e = {}
  for (const k of Object.keys(raw)) {
    if (!BG_MODELED.has(k)) e[k] = JSON.parse(JSON.stringify(raw[k])) // 未知键直通（escalation 等）
  }
  setStr(e, 'alias', vm.alias); setStr(e, 'name', vm.name)
  e.enabled = !!vm.enabled // 两态都显式（区别于省略=保持，这里保持语义由表单承载）
  setStr(e, 'target_dir', vm.target_dir); setStr(e, 'prompt', vm.prompt)
  setStr(e, 'report_dir', vm.report_dir) // 留空 = 缺省（GET 侧缺省键本已省略）
  setStr(e, 'cron', vm.cron)
  for (const p of BG_SCHED) { const v = toNum(vm[p.key]); if (v !== null) e[p.key] = v }
  e.tools = [...vm.tools] // 空数组 = 交给加载器判（422 定位到条目，服务端权威）
  if (vm.tools.includes('propose')) e.verbs = [...vm.verbs]
  if (vm.trigger) {
    const rawTrig = raw.trigger && typeof raw.trigger === 'object' ? raw.trigger : {}
    const t = {}
    for (const k of Object.keys(rawTrig)) {
      if (!BG_TRIG_KEYS.has(k)) t[k] = JSON.parse(JSON.stringify(rawTrig[k]))
    }
    t.source = vm.trigger.source
    if (vm.trigger.source === 'change') t.events = [...vm.trigger.events]
    else t.metric = 'failed_or_low' // recall 源固定指标
    const ac = toNum(vm.trigger.arm_count); if (ac !== null) t.arm_count = ac
    const mi = toNum(vm.trigger.min_interval_h); if (mi !== null) t.min_interval_h = mi
    const ls = toNum(vm.trigger.low_score); if (ls !== null) t.low_score = ls
    e.trigger = t
  }
  // 复用主智能体模型（spec §6.2）：勾选态 = 瞬态键 model_from_ai + 省略 model 键
  // （服务端以写回后的主模型段物化并剥除瞬态键）；表单已填的模型值原地保留——取消勾选即回显
  if (vm.fromAi) {
    e.model_from_ai = true
    return e
  }
  const rawModel = raw.model && typeof raw.model === 'object' ? raw.model : {}
  const model = {}
  for (const k of Object.keys(rawModel)) {
    if (!BG_MODEL_KEYS.has(k)) model[k] = JSON.parse(JSON.stringify(rawModel[k]))
  }
  setStr(model, 'provider', vm.model.provider)
  setStr(model, 'model_id', vm.model.model_id)
  setStr(model, 'base_url', vm.model.base_url)
  for (const p of BG_MODEL_NUMS) { const v = toNum(vm.model[p.key]); if (v !== null) model[p.key] = v }
  if (vm.model.keyTouched) model.api_key = String(vm.model.keyInput) // touched 三态（铁律①）
  e.model = model
  return e
}

// 工具 chip 中文短名（2026-11-22 用户拍板）：未知键兜底显英文原名；悬停 title 恒留英文原名对账 config
const TOOL_CN = {
  read_doc: '读文档', search_docs: '搜索文档', list_files: '文件列表', get_related_docs: '关联文档',
  recent_changes: '近期变更', recall_stats: '召回统计', recall_feedback_stats: '召回反馈统计',
  propose: '提交提案', list_proposals: '提案列表', get_proposal: '查看提案',
  write_doc: '写文档', edit_doc: '编辑文档', delete_doc: '删除文档', move_docs: '移动文档',
  adopt_proposal: '采纳提案', reject_proposal: '驳回提案'
}
const toolLabel = t => TOOL_CN[t] || t
// 三态语义进 title（toolState 家族在 setup 内——要吃 meta.tool_perms）
const TOOL_TRI_TIP = { allow: '允许（自动执行）', ask: '询问（执行前需你确认）', deny: '拒绝（未授权）' }

/* ''/null/undefined/NaN → null（payload 组键时省略），其余 Number */
function toNum(v) {
  if (v === '' || v == null) return null
  const n = Number(v)
  return Number.isFinite(n) ? n : null
}
const numOrEmpty = v => (v == null ? '' : v)
const strOrEmpty = v => (v == null ? '' : String(v))

// ─── 表单项级校验基建（2026-11-22 改版；法源 = 加载器各 raise 点，服务端仍是权威）───
const MSG_INT = '需为整数'
const MSG_NUM = '需为数字'
const MSG_TOOLS = '至少选择一个工具'
const isIntVal = v => v !== '' && v != null && Number.isInteger(Number(v))
const isNumVal = v => v !== '' && v != null && Number.isFinite(Number(v))
// cron 5 段形 + 数值域粗检（最终合法性归服务端 croniter：字母形态——月/周日名、L/W/# 等——放行）
const CRON_RANGE = [[0, 59], [0, 23], [1, 31], [1, 12], [0, 7]]
function checkCron(cron) {
  const fields = String(cron).trim().split(/\s+/)
  if (fields.length !== 5) return '定时计划需为 5 段（分 时 日 月 周）'
  for (let f = 0; f < 5; f++) {
    for (let part of fields[f].split(',')) {
      if (part.includes('/')) {
        const cut = part.indexOf('/')
        const step = part.slice(cut + 1)
        part = part.slice(0, cut)
        if (!/^\d+$/.test(step) || Number(step) < 1) return '定时计划格式有误'
      }
      if (part === '*') continue
      if (part === '') return '定时计划格式有误'
      const m = /^(\d+)(?:-(\d+))?$/.exec(part)
      if (!m) continue // 字母/生僻形态交服务端 croniter 终判（不硬拦 = 不误杀合法值）
      const a = Number(m[1])
      const b = m[2] == null ? a : Number(m[2])
      if (a < CRON_RANGE[f][0] || b > CRON_RANGE[f][1] || a > b) return '定时计划格式有误'
    }
  }
  return ''
}
// 回车成标签即时校验（法源 = core.privacy.parse_privacy_dirs）：非空/非绝对/无 .. /无斜杠/非系统目录
function checkPrivacyDir(v) {
  const norm = String(v).replace(/\\/g, '/')
  if (/^[A-Za-z]:/.test(norm) || norm.startsWith('/')) return '需为库内相对目录名，不能是绝对路径'
  if (norm.split('/').includes('..')) return '不能包含 ..'
  if (norm.includes('/')) return '只填顶层目录名，不要带斜杠'
  if (['.sync', '.trash', 'suggestions'].includes(norm.toLowerCase())) return '系统目录不能设为隐私目录'
  return ''
}
// 422 message → 字段键启发式（已知键词表；定位不了走卡片顶兜底，不硬猜。词表序 = 具体优先，
// 如 verbs 先于 tools——「verbs is required when tools contains 'propose'」归 verbs）
const AI_FIELD_MAP = [
  [/tools/, 'tools'],
  ...AI_PARAMS.map(p => [new RegExp(p.key), p.key])
]
const BG_FIELD_MAP = [
  [/invalid cron/i, 'cron'],
  [/target_dir/, 'target_dir'],
  [/report_dir/, 'report_dir'],
  [/prompt/, 'prompt'],
  [/arm_count/, 'trigger.arm_count'],
  [/low_score/, 'trigger.low_score'],
  [/min_interval_h/, 'trigger.min_interval_h'],
  [/trigger\./, 'trigger'],
  [/verbs/, 'verbs'],
  [/model_id/, 'model.model_id'],
  [/'propose' and 'write_doc'/, 'tools'],
  [/\btools\b/, 'tools'],
  [/\balias\b/, 'alias']
]
const MISC_FIELD_MAP = [
  [/daily_cap/, 'daily_cap'],
  [/log_retention_days/, 'log.retention_days'],
  [/log_level/, 'log.level']
]

export const SettingsPage = {
  name: 'SettingsPage',
  components: { MemoryConsole },
  props: {
    // T6 开层初始段：值域 = SECTIONS 段 id + 'memory'（plan 文本默认值 'model' 系早期草稿段名，
    // 实装默认段 id = 'ai'；未知值回默认段）。齿轮/无参入口恒 'ai'（openSettingsPage 兜底）。
    initialTab: { type: String, default: 'ai' },
    // 记忆徽标 = badgeParts.total（待审 + failed，口径同原 rail 记忆钮 / FAB 设置子钮）；>0 才画
    memoryBadge: { type: Number, default: 0 }
  },
  // 'actioned' = 记忆段采纳/驳回回执（app 立即 pollMemory 同步徽标）；
  // 'open-doc' = 记忆详情「来源」跳原文（app 开文档，设置浮层不自动关——dirty 语义保持）
  emits: ['close', 'actioned', 'open-doc'],
  setup(props, { emit }) {
    const state = inject('state')
    const api = inject('api')
    const panelRef = ref(null)
    // ─── 开层/关层焦点（壳由 CSS 折叠态常驻 DOM，开合只切 class——无 JS 转场参与）：
    //     开层焦点入面板首个「可见」可交互元素（桌面首件 = 头部 X；保存钮 display:none 时
    //     不可聚焦，按 offsetParent 跳过——display:none 元素 focus() 静默失败家族）；
    //     关层焦点归位 rail 齿轮钮（#settings-rail-btn，app 模板常驻 id，查询比 props 传 ref 稳）。
    //     preventScroll 红线：focus 绝不允许程序化平移 visualViewport ───
    watch(
      () => state.settingsOpen,
      async (open) => {
        if (!open) {
          confirmOpen.value = false   // 兜底：设置浮层关死则确认浮层同收（正常路径已各自落定）
          dirOpen.value = false       // 候选下拉随手收起（重开不残留展开态）
          dirActive.value = -1
          const gear = document.getElementById('settings-rail-btn')
          if (gear) gear.focus({ preventScroll: true })
          return
        }
        // T6 initialTab 开层定位（openSettingsPage(tab) / #memory 深链）：未知值回默认段；
        // 齿轮/无参入口 app 侧兜底 settingsTab='ai' → 恒进默认段。
        // 段切换零表单写入——不产生 dirty（见文件头关闭守卫边界），settingsCloseSeq 链零改动。
        const tab = props.initialTab
        section.value = (tab === 'memory' || SECTIONS.some(s => s.id === tab)) ? tab : 'ai'
        // section 停留在杂项的后续打开路径不经 watch(section)——开层时补位拉一次目录候选
        if (section.value === 'misc') ensureVaultDirs()
        // 记忆段补位：直落记忆段且全站脉搏未到货 → 静默补拉一次（同 bg 段口径）
        if (section.value === 'memory') ensureBgStatus()
        await nextTick()
        if (!panelRef.value) return
        const first = Array.from(panelRef.value.querySelectorAll('button, input, select, textarea'))
          .find(el => el.offsetParent !== null)
        if (first) first.focus({ preventScroll: true })
      }
    )

    // ═══ 取数与表单（打开页面时惰性首拉；revision = 乐观并发随行令牌，409 是唯一的过期信号）───
    const loading = ref(false)
    const loadError = ref(null)          // 仅在表单缺席时成卡展示
    const form = ref(null)               // 可编辑镜像 {ai, background_agents, notifications, log, privacy}
    const revision = ref('')
    const meta = ref({ tools: [], verbs: [], restart_fields: [] })
    const aiWasNull = ref(false)         // GET ai 段缺位（新建段路径）
    const apiKeySet = ref(false)         // 当前已设置密钥（掩码回显态）
    const apiKeyMask = ref('')
    const apiKeyTouched = ref(false)     // touched 集合中唯一成员：ai.api_key（§4.2 密钥三态）
    const apiKeyInput = ref('')
    const toolsMode = ref('all')         // 'all'=全量（payload tools:null）| 'custom'=白名单
    const toolsSel = ref([])
    let loadedOnce = false

    // ═══ 表单项级错误态（2026-11-22 改版）：fieldErrors 一个响应式对象（键 = 'ai.<字段>' |
    //     'bg.<条目下标>.<字段路径>' | 'misc.<字段>'，值 = 红小字文案），不给每字段加独立 ref；
    //     cardErrors = message 定位不到字段时的卡片顶兜底（键 = 分段 id）；dirErr = 隐私目录回车错。
    //     预检错误 = 本地化中文（给用户行动用）；422 错误 = 加载器原文（剥「Config file …: 」与
    //     「background_agents[i] (alias)」实现 meta 前缀后落位）───
    const fieldErrors = ref({})
    const cardErrors = ref({})
    const dirErr = ref('')
    const err = key => fieldErrors.value[key] || ''
    const cardErrs = sec => cardErrors.value[sec] || []
    function clearErr(key) {
      if (!fieldErrors.value[key]) return
      const m = { ...fieldErrors.value }
      delete m[key]
      fieldErrors.value = m
    }
    function clearAllErrors() {
      fieldErrors.value = {}
      cardErrors.value = {}
      dirErr.value = ''
    }

    // ═══ 分段保存状态徽标 + 关闭守卫（spec §6：X / backdrop / Escape 任一路径先过 requestClose）═══
    // dirty 分段切指纹（徽标逐卡/逐条目点亮）：ai 段 = ai 表单 + 密钥 touched/input + toolsMode/toolsSel；
    // bg 段 = 逐条目指纹（条目含每条目 keyTouched/keyInput，raw 不随用户改动），任一条目脏则段卡脏，
    // 条目增删以长度失配兜底；misc 段 = 通知 + 日志 + 隐私。基线在每次 hydrate（首拉/保存成功后
    // 重载/409 重载）末尾重取——保存成功与重载自然清零，无需逐字段维护 touched 集。
    const baseAi = ref('')
    const baseBg = ref([])
    const baseMisc = ref('')
    const aiFp = () => JSON.stringify({
      f: form.value.ai,
      ak: (apiKeyTouched.value ? '1' : '0') + '|' + String(apiKeyInput.value), // touched 且 input 空的「清除」态与干净态串不同，不漏判
      tm: toolsMode.value,
      ts: toolsSel.value
    })
    const miscFp = () => JSON.stringify({ n: form.value.notifications, l: form.value.log, p: form.value.privacy })
    const bgFps = () => (form.value.background_agents || []).map(e => JSON.stringify(e))
    const dirtyAi = computed(() => !!form.value && aiFp() !== baseAi.value)
    const dirtyMisc = computed(() => !!form.value && miscFp() !== baseMisc.value)
    const dirtyBg = computed(() => {
      if (!form.value) return false
      const fps = bgFps()
      return fps.length !== baseBg.value.length || fps.some((f, i) => f !== baseBg.value[i])
    })
    const bgEntryDirty = i => bgFps()[i] !== baseBg.value[i]
    const settingsDirty = computed(() => dirtyAi.value || dirtyBg.value || dirtyMisc.value)
    function resetBaselines() {
      baseAi.value = aiFp()
      baseBg.value = bgFps()
      baseMisc.value = miscFp()
    }

    // ─── 关闭确认浮层（spec §6 三选，替代原生 confirm）：保存并关闭（成功才关）/ 放弃修改（重拉
    //     服务端真相后关）/ 取消（留在页面，默认焦点钮）。确认浮层弹出时设置浮层 inert → Tab 自然
    //     困于浮层内；Escape 由浮层 keydown 截停（stopPropagation 先于 app document 级 Escape 链）═══
    const confirmOpen = ref(false)
    const confirmRef = ref(null)
    const cancelBtnRef = ref(null)
    let confirmTrig = null   // 触发关闭的元素（取消后焦点归位）
    watch(confirmOpen, async (open) => {
      if (open) { await nextTick(); if (cancelBtnRef.value) cancelBtnRef.value.focus({ preventScroll: true }) }
    })
    function cancelConfirm() {
      confirmOpen.value = false
      if (confirmTrig && confirmTrig.isConnected) confirmTrig.focus({ preventScroll: true })
      confirmTrig = null
    }
    function requestClose() {
      if (!state.settingsOpen) return
      if (confirmOpen.value) { cancelConfirm(); return }   // 确认浮层已弹时，任何关闭请求都按取消收
      if (settingsDirty.value) {
        confirmTrig = document.activeElement
        confirmOpen.value = true
        return
      }
      emit('close')
    }
    function onConfirmKeydown(e) {
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); cancelConfirm(); return }
      if (e.key !== 'Tab' || !confirmRef.value) return
      const list = Array.from(confirmRef.value.querySelectorAll('button')).filter(el => !el.disabled)
      if (!list.length) return
      const first = list[0], last = list[list.length - 1]
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus({ preventScroll: true }) }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus({ preventScroll: true }) }
    }
    async function discardAndClose() {
      confirmOpen.value = false
      reloadFromServer()   // 改错可直接离开：丢弃本页编辑，回服务端真相
      emit('close')
    }
    async function saveAndClose() {
      const ok = await save()
      confirmOpen.value = false   // 预检/422/409：确认浮层收起、设置浮层不关、错误照报（字段级/卡顶/toast）
      if (ok) emit('close')
    }
    // app Escape 链最高优先支 /（其余关闭路径模板直连）→ settingsCloseSeq 递增 = 关闭请求
    watch(() => state.settingsCloseSeq, () => { if (state.settingsOpen) requestClose() })

    async function load() {
      if (loading.value) return
      loading.value = true
      try {
        const res = await api.getSettings()
        if (res.status === 200 && res.data) {
          hydrate(res.data)
          loadError.value = null
        } else if (res.status === 503) {
          loadError.value = '设置管理未接线'
        } else {
          loadError.value = '设置加载失败，可稍后重试'
        }
      } catch (e) {
        loadError.value = '设置加载失败，可稍后重试'
      } finally {
        loading.value = false
      }
    }

    function hydrate(d) {
      revision.value = d.revision || ''
      meta.value = d._meta || { tools: [], verbs: [], restart_fields: [] }
      const ai = d.ai && typeof d.ai === 'object' ? d.ai : null
      aiWasNull.value = !ai
      form.value = {
        ai: {
          base_url: strOrEmpty(ai && ai.base_url),
          model: strOrEmpty(ai && ai.model),
          max_tokens: numOrEmpty(ai && ai.max_tokens),
          temperature: numOrEmpty(ai && ai.temperature),
          context_window: numOrEmpty(ai && ai.context_window),
          max_rounds: numOrEmpty(ai && ai.max_rounds),
          read_doc_max_chars: numOrEmpty(ai && ai.read_doc_max_chars),
          confirm_timeout: numOrEmpty(ai && ai.confirm_timeout)
        },
        // 条目镜像（Task 9 接管编辑）：raw 副本存底本，未知键保存时直通回条目
        background_agents: (d.background_agents || []).map(bgEntryView),
        notifications: { daily_cap: numOrEmpty(d.notifications && d.notifications.daily_cap) },
        log: {
          level: strOrEmpty(d.log && d.log.level) || 'INFO',
          dir: strOrEmpty(d.log && d.log.dir),          // 页外字段直通回传（重启生效段）
          retention_days: numOrEmpty(d.log && d.log.retention_days)
        },
        privacy: { dirs: [...((d.privacy && d.privacy.dirs) || [])] }
      }
      // api_key：掩码形态止步于此，编辑面只有「是否触碰 + 新值」两态
      apiKeySet.value = !!(ai && ai.api_key && ai.api_key.set)
      apiKeyMask.value = strOrEmpty(ai && ai.api_key && ai.api_key.mask)
      apiKeyTouched.value = false
      apiKeyInput.value = ''
      // tools 两态：null=全量；数组=自定义（切回自定义时以全量起底，开关瞬间不隐式改集合）
      if (ai && Array.isArray(ai.tools)) {
        toolsMode.value = 'custom'
        toolsSel.value = [...ai.tools]
      } else {
        toolsMode.value = 'all'
        toolsSel.value = [...(meta.value.tools || [])]
      }
      // 服务端真相回填 = 条目编辑态归零（保存后重载同路径：折叠列表、解武装、清校验错误态）
      bgEditing.value = -1
      disarmBgDelete()
      clearAllErrors()
      // 分段徽标基线：hydrate 末尾重取各段指纹（保存成功/409 重载同路径 → dirty 自动清零）
      resetBaselines()
    }

    // 打开浮层首拉（深链/常开恢复均经此；此后 revision/409 机制负责新鲜度，重开不重复拉）
    watch(
      () => state.settingsOpen,
      (open) => {
        if (open && !loadedOnce) { loadedOnce = true; load() }
      },
      { immediate: true }
    )

    // ═══ ai 段「用户改动过」判定（仅 aiWasNull 时参与 ai:null 决策）：
    //     以有效值而非控件触碰计——自定义白名单切回全量再清空其余 = 零改动（幂等删段）───
    const aiEdited = computed(() => {
      if (apiKeyTouched.value) return true
      const a = form.value && form.value.ai
      if (!a) return false
      if (String(a.base_url || '').trim() || String(a.model || '').trim()) return true
      if (AI_PARAMS.some(p => toNum(a[p.key]) !== null)) return true
      if (toolsMode.value === 'custom') return true
      return false
    })

    const apiKeyHint = computed(() => {
      if (!apiKeyTouched.value) return ''
      return String(apiKeyInput.value) === '' ? '保存后将清除密钥' : '保存后将写入新密钥'
    })

    function clearApiKey() {
      apiKeyTouched.value = true
      apiKeyInput.value = ''
    }
    function setToolsMode(mode) {
      if (toolsMode.value === mode) return
      toolsMode.value = mode
      clearErr('ai.tools') // 切回全量后白名单不再进 payload，残留红字失义
      if (mode === 'custom' && !toolsSel.value.length) toolsSel.value = [...(meta.value.tools || [])]
    }
    function toggleTool(t) {
      clearErr('ai.tools') // 字段修改即清该字段错误（下次保存全量重算）
      const i = toolsSel.value.indexOf(t)
      if (i >= 0) toolsSel.value.splice(i, 1)
      else toolsSel.value.push(t)
    }
    // 工具三态展示（不可配置、必须展示，spec §6）：勾选态内按 meta.tool_perms 分允许/询问，
    // 未勾选 = 拒绝（弱样式）；aria-pressed 语义恒为「是否授权」，三态只进 class/title
    function toolState(t, granted) {
      if (!granted) return 'deny'
      const p = (meta.value && meta.value.tool_perms) || {}
      return p[t] === 'ask' ? 'ask' : 'allow'
    }
    const toolCls = (t, granted) => {
      const s = toolState(t, granted)
      return { on: granted, allow: s === 'allow', ask: s === 'ask' }
    }
    const toolTitle = (t, granted) => t + ' · ' + TOOL_TRI_TIP[toolState(t, granted)]

    // ═══ 隐私目录标签（可选择 + 可输入组合控件 combobox：自由输入回车走既有校验成标签；
    //     聚焦/输入即下拉 vault 一级目录候选，鼠标点选或 ↑↓+Enter 直接成标签；
    //     chip 仅 × 可删——字段容器是 div 非 label（label 转发激活 = tag 误删根因，
    //     AGENTS「锚点激活行为」泛化红线），chip 本体不响应点击；重复即忽略）───
    const dirDraft = ref('')
    const dirInputRef = ref(null)
    const dirListRef = ref(null)
    const vaultDirs = ref([])        // vault 一级目录名集合（进杂项段首拉一次缓存复用；空 = 退化纯输入）
    let vaultDirsFetched = false
    const dirOpen = ref(false)
    const dirActive = ref(-1)        // 键盘活动项（aria-activedescendant 指向，输入框全程保焦点）
    // 候选 = 一级目录 − 已有标签 − 保留词表，按输入子串过滤；数据源 /api/docs dirs 来自 os.walk，
    // 含无文档的空目录；后端已剪隐藏目录与系统目录，前端保留词表兜底二次过滤（与 checkPrivacyDir 同表）
    const dirCandidates = computed(() => {
      const taken = new Set(((form.value && form.value.privacy.dirs) || []).map(d => String(d).toLowerCase()))
      const q = String(dirDraft.value).trim().toLowerCase()
      return vaultDirs.value.filter(d => !taken.has(d.toLowerCase()) && (!q || d.toLowerCase().includes(q)))
    })
    function ensureVaultDirs() {
      if (vaultDirsFetched) return
      vaultDirsFetched = true        // 失败也只拉一次：静默退化纯输入，不反复打端点
      // getDocsRaw 而非 getDocs：ETag 304→null 契约会把「无变化」喂成空名单（2026-11-22 实证）
      api.getDocsRaw()
        .then(d => {
          const first = new Set()
          for (const p of ((d && d.dirs) || [])) {
            const seg = String(p).split('/')[0]
            if (seg) first.add(seg)
          }
          vaultDirs.value = [...first].sort((a, b) => a.localeCompare(b))
        })
        .catch(() => {})
    }
    function commitDir(v) {
      if (!form.value.privacy.dirs.includes(v)) form.value.privacy.dirs.push(v)
      dirDraft.value = ''
      dirErr.value = ''
      dirActive.value = -1
    }
    // 回车成标签即时校验（自由输入路径）：出错输入框就地红 + 小字，不入库不生成标签（法源 = parse_privacy_dirs）
    function addDir() {
      const v = String(dirDraft.value).trim()
      if (!v) return
      const bad = checkPrivacyDir(v)
      if (bad) { dirErr.value = bad; return }
      commitDir(v)
      // 键盘聚焦红线（AGENTS Gotchas）：preventScroll 防键盘弹起时视口被程序化平移
      if (dirInputRef.value) dirInputRef.value.focus({ preventScroll: true })
    }
    // 候选本身就是一级目录名，天然过 checkPrivacyDir 契约；选后保焦点连选（已添项被过滤自动出列，名单空则面板收起）
    function pickDir(v) {
      commitDir(v)
      if (dirInputRef.value) dirInputRef.value.focus({ preventScroll: true })
    }
    function removeDir(i) { form.value.privacy.dirs.splice(i, 1) }
    function clearDirErr() { if (dirErr.value) dirErr.value = '' }
    function onDirInput() { clearDirErr(); dirOpen.value = true; dirActive.value = -1 }
    function onDirOpenList() { dirOpen.value = true; dirActive.value = -1 }
    function onDirBlurClose(e) {
      // 候选钮 mousedown.prevent 拒收焦点（焦点从不进列表）→ 输入框 blur 且焦点未落在本字段内 = 真离开，关列表
      const el = e.relatedTarget
      if (el && el.closest && el.closest('.st-dir-field')) return
      dirOpen.value = false
      dirActive.value = -1
    }
    function onDirKeydown(e) {
      if (e.key === 'Escape') {
        // 下拉开着时 Esc 只关下拉，不外溢关设置浮层（层级归位）
        if (dirOpen.value) { e.stopPropagation(); dirOpen.value = false; dirActive.value = -1 }
        return
      }
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault()           // 拒默认滚动（默认滚动 = 程序化平移滚动面，preventScroll 红线）
        const n = dirCandidates.value.length
        if (!n) return
        dirOpen.value = true
        const d = e.key === 'ArrowDown' ? 1 : -1
        dirActive.value = dirActive.value < 0 ? (d > 0 ? 0 : n - 1) : (dirActive.value + d + n) % n
        return
      }
      if (e.key === 'Tab') { dirOpen.value = false; return }
      if (e.key === 'Enter') {
        e.preventDefault()
        if (dirOpen.value && dirActive.value >= 0 && dirCandidates.value[dirActive.value]) { pickDir(dirCandidates.value[dirActive.value]); return }
        addDir()
      }
    }
    // 键盘活动项入视口：block:'nearest' 已可见即零操作，需要动也只滚候选面板自身容器（不外溢平移文档视口）
    watch(dirActive, async () => {
      if (dirActive.value < 0 || !dirListRef.value) return
      await nextTick()
      const el = dirListRef.value.children[dirActive.value]
      if (el && el.scrollIntoView) el.scrollIntoView({ block: 'nearest' })
    })

    // ═══ 后台 agent 段（Task 9）：列表/条目编辑态 + 徽标 + 增删改交互 ═══
    const bgEditing = ref(-1)      // 展开编辑的条目下标（-1 = 仅列表）
    const bgDelArmed = ref(-1)     // 两步删除的武装下标（TrashPanel emptyArmed 先例，3s 复位）
    let bgDelTimer = null
    const bgStatusPending = ref(false)

    const bgForm = computed(() => {
      const i = bgEditing.value
      const f = form.value && form.value.background_agents
      return i >= 0 && f && f[i] ? f[i] : null
    })

    // 徽标只显运行/待命/停用：数据源 = 全站 30s 脉搏的 agents（在册 = 有运行/触发登记）；
    // 数据缺席（静默失败）→ 启用条目落「待命」，表单照常可用
    const bgLive = computed(() => {
      const s = new Set()
      for (const a of ((state.memoryStatus && state.memoryStatus.agents) || [])) s.add(a.alias)
      return s
    })
    function bgBadge(e) {
      if (!e.enabled) return { cls: 'off', label: '停用', tip: '已停用；保存后该任务从调度中退出' }
      if (bgLive.value.has(String(e.alias || '').trim())) return { cls: 'run', label: '运行', tip: '服务端在册（有运行或触发登记）' }
      return { cls: 'idle', label: '待命', tip: '已启用；服务端尚未登记（新建/改名后待下一拍）' }
    }
    // 条目行涉错标记（错误可见性兜底：字段错误挂在折叠表单里不可见，列表行恒可见）
    function bgHasErr(i) {
      const pre = 'bg.' + i + '.'
      return Object.keys(fieldErrors.value).some(k => k.startsWith(pre))
    }
    function bgErrTip(i) {
      const pre = 'bg.' + i + '.'
      const k = Object.keys(fieldErrors.value).find(x => x.startsWith(pre))
      return k ? String(fieldErrors.value[k]) : ''
    }
    // 本段首次打开且脉搏尚未到货 → 静默补拉一次（失败吞掉，不阻塞表单加载）
    function ensureBgStatus() {
      if (state.memoryStatus || bgStatusPending.value) return
      bgStatusPending.value = true
      api.getMemoryStatus()
        .then(st => { if (st) state.memoryStatus = st })
        .catch(() => {})
        .finally(() => { bgStatusPending.value = false })
    }

    function bgSelect(i) {
      if (bgEditing.value === i) { bgEditing.value = -1; return }
      disarmBgDelete()
      bgEditing.value = i
    }
    function bgAdd() {
      clearAllErrors() // 条目下标即错误键前缀，列表增删后旧键全部失配——结构变化即清，下次保存重算
      form.value.background_agents.push(bgBlankEntry())
      disarmBgDelete()
      bgEditing.value = form.value.background_agents.length - 1
    }
    function disarmBgDelete() {
      clearTimeout(bgDelTimer)
      bgDelArmed.value = -1
    }
    function onBgDelete(i) {
      if (bgDelArmed.value !== i) { // 第一步：只武装，3s 自动复位
        disarmBgDelete()
        bgDelArmed.value = i
        bgDelTimer = setTimeout(disarmBgDelete, 3000)
        return
      }
      disarmBgDelete()
      clearAllErrors() // 同 bgAdd：下标位移使旧错误键失配
      form.value.background_agents.splice(i, 1)
      if (bgEditing.value === i) bgEditing.value = -1
      else if (bgEditing.value > i) bgEditing.value -= 1
    }
    onBeforeUnmount(disarmBgDelete)

    function bgToggleTrigger(i) {
      const e = form.value.background_agents[i]
      clearErr('bg.' + i + '.trigger') // 触发组开关即清本组校验错误
      clearErr('bg.' + i + '.trigger.arm_count')
      clearErr('bg.' + i + '.trigger.low_score')
      clearErr('bg.' + i + '.trigger.min_interval_h')
      if (e) e.trigger = e.trigger ? null : bgBlankTrigger()
    }
    function bgToggleEvent(i, op) {
      const e = form.value.background_agents[i]
      if (!e || !e.trigger) return
      const k = e.trigger.events.indexOf(op)
      if (k >= 0) e.trigger.events.splice(k, 1)
      else e.trigger.events.push(op)
    }
    function bgToggleTool(i, t) {
      const e = form.value.background_agents[i]
      if (!e) return
      clearErr('bg.' + i + '.tools') // 字段修改即清该字段错误（下次保存全量重算）
      const k = e.tools.indexOf(t)
      if (k >= 0) e.tools.splice(k, 1)
      else e.tools.push(t)
    }
    function bgToggleVerb(i, v) {
      const e = form.value.background_agents[i]
      if (!e) return
      clearErr('bg.' + i + '.verbs')
      const k = e.verbs.indexOf(v)
      if (k >= 0) e.verbs.splice(k, 1)
      else e.verbs.push(v)
    }
    // 复用主智能体模型开关（勾选态整组模型字段隐藏，model_id 必填预检只查非勾选态 → 切换即清残留红字）
    function bgToggleFromAi() {
      const i = bgEditing.value
      const e = form.value.background_agents[i]
      if (!e) return
      clearErr('bg.' + i + '.model.model_id')
      e.fromAi = !e.fromAi
    }
    function bgClearKey(i) {
      const e = form.value.background_agents[i]
      if (!e) return
      e.model.keyTouched = true
      e.model.keyInput = ''
    }
    const bgKeyHint = e => (String(e.model.keyInput) === '' ? '保存后将清除密钥' : '保存后将写入新密钥')

    // ═══ 保存（单一「保存全部」；成功无条——分段徽标归零即信号；顶部告警区只留 409/热生效族，
    //     422 校验错误全部下沉表单项；500 {saved:true} 的 apply 失败窗口如实报——spec §4.2 步 6）═══
    const saving = ref(false)
    const report = ref(null)  // {kind:'conflict'|'savedapply'|'error', ...}——非字段校验族专属

    // ─── 保存预检（点保存时统一跑，无 blur 逐字段校验；规则全集只实现服务端已有规则，
    //     法源 = 加载器各 raise 点；返回 [{key, sec, msg}]，有错 = 不发 PUT）───
    function precheck() {
      const out = []
      const add = (key, sec, msg) => out.push({ key, sec, msg })
      if (!form.value) return out
      // 主智能体：数值键可空、非空类型判（int 族 / 数字族——服务端无范围检查，此处同不加）
      const a = form.value.ai
      for (const p of AI_PARAMS) {
        const v = a[p.key]
        if (v === '' || v == null) continue
        if (p.key === 'temperature' || p.key === 'confirm_timeout') {
          if (!isNumVal(v)) add('ai.' + p.key, 'ai', MSG_NUM)
        } else if (!isIntVal(v)) add('ai.' + p.key, 'ai', MSG_INT)
      }
      // 工具不准空（用户拍板）：自定义白名单空选 = 非法（加载器拒 ai.tools 空列表）
      if (toolsMode.value === 'custom' && !toolsSel.value.length) add('ai.tools', 'ai', MSG_TOOLS)
      // 后台条目（执行器只读条目不进编辑面，跳过）
      const seenAlias = {}
      for (let i = 0; i < form.value.background_agents.length; i++) {
        const e = form.value.background_agents[i]
        if (e.readOnly) continue
        const pfx = 'bg.' + i + '.'
        const al = String(e.alias || '').trim()
        if (!al) add(pfx + 'alias', 'bg', '标识不能为空')
        else if (al === 'agent') add(pfx + 'alias', 'bg', '「agent」为保留标识，请换一个')
        if (al && Object.prototype.hasOwnProperty.call(seenAlias, al)) {
          add('bg.' + seenAlias[al] + '.alias', 'bg', '标识与其他条目重复') // 跨条目判重：涉事条目同时标红
          add(pfx + 'alias', 'bg', '标识与其他条目重复')
        } else if (al) seenAlias[al] = i
        const cron = String(e.cron || '').trim()
        if (!cron) add(pfx + 'cron', 'bg', '定时计划不能为空')
        else { const cm = checkCron(cron); if (cm) add(pfx + 'cron', 'bg', cm) }
        if (!String(e.target_dir || '').trim()) add(pfx + 'target_dir', 'bg', '工作目录不能为空')
        if (!String(e.prompt || '').trim()) add(pfx + 'prompt', 'bg', '提示词文件不能为空')
        // 预算/调度数值键：仅拦非整数，不加范围（服务端无范围检查）
        for (const p of BG_SCHED) if (e[p.key] !== '' && e[p.key] != null && !isIntVal(e[p.key])) add(pfx + p.key, 'bg', MSG_INT)
        if (e.trigger) {
          const ac = e.trigger.arm_count
          if (ac !== '' && ac != null && (!isIntVal(ac) || Number(ac) < 1)) add(pfx + 'trigger.arm_count', 'bg', '需为不小于 1 的整数')
          const ls = e.trigger.low_score
          if (ls !== '' && ls != null && (!isNumVal(ls) || !(Number(ls) > 0 && Number(ls) < 1))) add(pfx + 'trigger.low_score', 'bg', '需为 0 到 1 之间的数字')
          const mi = e.trigger.min_interval_h
          if (mi !== '' && mi != null && !isNumVal(mi)) add(pfx + 'trigger.min_interval_h', 'bg', MSG_NUM)
        }
        // 工具不准空（v2 法层必填非空）；取消复用后 model_id 必填；模型数值键同 ai 类型预检
        if (!e.tools.length) add(pfx + 'tools', 'bg', MSG_TOOLS)
        if (!e.fromAi) {
          if (!String(e.model.model_id || '').trim()) add(pfx + 'model.model_id', 'bg', '模型名称不能为空')
          for (const p of BG_MODEL_NUMS) {
            const v = e.model[p.key]
            if (v === '' || v == null) continue
            if (p.key === 'temperature' ? !isNumVal(v) : !isIntVal(v)) add(pfx + 'model.' + p.key, 'bg', p.key === 'temperature' ? MSG_NUM : MSG_INT)
          }
        }
      }
      // 杂项：daily_cap / log_retention_days 整数 ≥1（可空 = 省略键回缺省）
      const cap = form.value.notifications.daily_cap
      if (cap !== '' && cap != null && (!isIntVal(cap) || Number(cap) < 1)) add('misc.daily_cap', 'misc', '需为不小于 1 的整数')
      const ret = form.value.log.retention_days
      if (ret !== '' && ret != null && (!isIntVal(ret) || Number(ret) < 1)) add('misc.log.retention_days', 'misc', '需为不小于 1 的整数')
      return out
    }
    function groupFields(list) {
      const fe = {}
      for (const p of list) fe[p.key] = fe[p.key] ? fe[p.key] + '；' + p.msg : p.msg
      return fe
    }
    // 预检失败定位：跳所在分段 → 涉错 bg 条目自动展开 → 滚动定位 → 键盘红线焦点
    async function locateFirst(list) {
      const first = list[0]
      section.value = first.sec
      const m = /^bg\.(\d+)\./.exec(first.key)
      if (m) bgEditing.value = Number(m[1])
      await nextTick()
      if (!panelRef.value) return
      const el = panelRef.value.querySelector('[data-fk="' + first.key + '"]')
      if (!el) return
      el.scrollIntoView({ block: 'center', behavior: 'smooth' })
      const target = el.matches('input,select,textarea,button') ? el : el.querySelector('input,select,textarea,button')
      if (target) target.focus({ preventScroll: true })
    }

    // ─── 422 回显（section 定卡片 + message 键词/alias 定位字段；三级兜底 = 字段红字 →
    //     卡片顶一行 → toast，错误不可见是红线）───
    function clean422(msg) {
      return String(msg)
        .replace(/^Config file .*?: /, '')            // 临时校验文件路径前缀 = 实现 meta，剥
        .replace(/^background_agents\[\d+(?: \([^)]*\))?\]\.?/, '') // 条目定位前缀已用于落位，剥
        .trim()
    }
    function entryIndexes(message) {
      // 主定位 = payload 下标（PUT 条目序 = 表单序，天然可靠）；跨条目消息（重叠/判重）无下标形，
      // 按涉事 alias 匹配（涉事条目同时标红）
      const list = form.value.background_agents || []
      const m = /background_agents\[(\d+)\]/.exec(message)
      if (m) { const i = Number(m[1]); return i < list.length ? [i] : [] }
      const hits = []
      list.forEach((e, i) => { const a = String(e.alias || '').trim(); if (a && message.includes(a)) hits.push(i) })
      return hits
    }
    function bgFieldOf(message) {
      for (const [re, k] of BG_FIELD_MAP) if (re.test(message)) return k
      return null
    }
    function apply422(errors) {
      const fe = {}
      const ce = {}
      const card = (sec, msg) => { (ce[sec] || (ce[sec] = [])).push(msg) }
      let expand = -1
      for (const e of errors) {
        const msg = String((e && e.message) || '')
        const sec = e && e.section
        const clean = clean422(msg) || '配置校验未通过，请检查后重试'
        if (msg.includes('主智能体模型未配置')) { fe['ai.model'] = clean; continue } // 复用引用缺失 → 挂主模型字段
        if (sec === 'ai') {
          const hit = AI_FIELD_MAP.find(([re]) => re.test(msg))
          if (hit) fe['ai.' + hit[1]] = clean
          else card('ai', clean)
          continue
        }
        if (sec === 'background_agents') {
          const idxs = entryIndexes(msg)
          const fk = idxs.length ? bgFieldOf(msg) : null
          if (fk) {
            for (const i of idxs) {
              const key = 'bg.' + i + '.' + fk
              fe[key] = fe[key] ? fe[key] + '；' + clean : clean
            }
            if (expand < 0) expand = idxs[0]
          } else card('bg', clean) // 条目定不到或键词表映射不了 → bg 卡顶兜底
          continue
        }
        if (sec === 'notifications' || sec === 'log' || sec === 'privacy') {
          const hit = MISC_FIELD_MAP.find(([re]) => re.test(msg))
          if (hit) fe['misc.' + hit[1]] = clean
          else card('misc', clean)
          continue
        }
        showToast(clean, 'error') // section 都定不了 → toast 兜底
      }
      fieldErrors.value = fe
      cardErrors.value = ce
      // 折叠条目字段错误不可见是红线：自动展开首个涉错条目（列表行另有涉错红标兜底）
      if (expand >= 0 && bgEditing.value !== expand) bgEditing.value = expand
    }

    // 返回 true = 已保存（保存并关闭据此放行）；false = 未保存，错误已按字段/卡顶/toast 就位
    async function save() {
      if (saving.value || !form.value) return false
      report.value = null
      clearAllErrors()
      const problems = precheck() // 预检有错 → 不发 PUT（服务端仍是权威，下次保存全量重算）
      if (problems.length) {
        fieldErrors.value = groupFields(problems)
        await locateFirst(problems)
        return false
      }
      saving.value = true
      try {
        const res = await api.putSettings(buildPayload())
        if (res.status === 200) {
          // 保存成功后重载 GET：按新 revision 回填全表单（与后端真相一致，api_key 掩码同步刷新，
          // 分段徽标随 hydrate 基线重取归零）
          await load()
          // 配置改动即真相变更（privacy.dirs 影响树/搜索/顶栏的隐私显示）→ 通知 app 刷文件树
          // （settingsCloseSeq 同款信号面；config 改动不产 change_log 行，watcher 轮询等不来）
          state.settingsSavedSeq = (state.settingsSavedSeq || 0) + 1
          return true
        }
        if (res.status === 409) {
          report.value = { kind: 'conflict' }
        } else if (res.status === 422) {
          apply422((res.data && res.data.errors) || [])
        } else if (res.status === 500 && res.data && res.data.saved) {
          report.value = { kind: 'savedapply', message: '配置已保存，但部分即时生效失败，建议重启服务' }
        } else if (res.status === 503) {
          report.value = { kind: 'error', message: '设置管理未接线' }
        } else {
          report.value = { kind: 'error', message: '保存失败，可稍后重试' }
        }
      } catch (e) {
        report.value = { kind: 'error', message: '保存失败，可稍后重试' }
      } finally {
        saving.value = false
      }
      return false
    }

    // 409「重载」：丢弃本页未保存编辑，回到服务端真相（与保存成功后的重载同一语义）
    function reloadFromServer() {
      report.value = null
      clearAllErrors()
      load()
    }

    function buildPayload() {
      const f = form.value
      const p = { revision: revision.value }
      // ── ai 段（空标量省略键=不发 null，铁律③；api_key 走 touched 三态，铁律①）──
      const a = f.ai
      const ai = {}
      const bu = String(a.base_url || '').trim()
      if (bu) ai.base_url = bu
      const md = String(a.model || '').trim()
      if (md) ai.model = md
      for (const prm of AI_PARAMS) {
        const v = toNum(a[prm.key])
        if (v !== null) ai[prm.key] = v
      }
      if (apiKeyTouched.value) ai.api_key = String(apiKeyInput.value)
      if (toolsMode.value === 'custom') ai.tools = [...toolsSel.value]
      else ai.tools = null // 全量的显式形态（区别于省略——文档里旧白名单须被清掉）
      p.ai = aiWasNull.value && !aiEdited.value ? null : ai
      // ── 后台 agent 段（铁律②+④）：条目走 buildBgEntry 重建（raw 未知键直通 + 密钥 touched 三态）──
      p.background_agents = (f.background_agents || []).map(buildBgEntry)
      // ── 杂项段（notifications/log 对象恒在场=段级零删除；段内空标量省略键）──
      const notif = {}
      const cap = toNum(f.notifications.daily_cap)
      if (cap !== null) notif.daily_cap = cap
      p.notifications = notif
      const log = { level: f.log.level, dir: f.log.dir }
      const ret = toNum(f.log.retention_days)
      if (ret !== null) log.retention_days = ret
      p.log = log
      p.privacy = { dirs: [...f.privacy.dirs] } // [] 合法 = 显式空名单
      return p
    }

    // 分段导航（单一在场段；≤639 由 CSS 折成顶部分段钮行）
    const section = ref('ai')
    // 打开本段且全站脉搏未到货 → 静默补拉徽标数据（immediate 覆盖直落本段的后续打开路径）；
    // memory 段同口径补拉（MemoryConsole 队列消费 state.memoryStatus，app 30s 轮询兜大部队）
    watch(section, (s) => { if (s === 'bg' || s === 'memory') ensureBgStatus(); if (s === 'misc') ensureVaultDirs() }, { immediate: true })

    // 记忆段事件上抛（T6）：采纳/驳回回执 → app 重轮询；「来源」跳原文 → app openDoc
    function onMemActioned() { emit('actioned') }
    function onMemOpenDoc(path) { emit('open-doc', path) }

    return {
      state, panelRef, requestClose,
      SECTIONS, AI_PARAMS, LOG_LEVELS, section, ICONS,
      memoryBadge, onMemActioned, onMemOpenDoc,
      loading, loadError, form, meta, load,
      apiKeySet, apiKeyMask, apiKeyTouched, apiKeyInput, apiKeyHint, clearApiKey,
      toolsMode, toolsSel, setToolsMode, toggleTool, toolLabel, toolCls, toolTitle,
      // 表单项级校验呈现（红框判定 + 红小字 + 修改即清 + 卡顶兜底）
      err, clearErr, cardErrs, dirErr, clearDirErr,
      // 分段徽标 + 关闭确认浮层
      dirtyAi, dirtyBg, dirtyMisc, bgEntryDirty,
      confirmOpen, confirmRef, cancelBtnRef, cancelConfirm, discardAndClose, saveAndClose, onConfirmKeydown,
      dirDraft, dirInputRef, dirListRef, dirOpen, dirActive, dirCandidates,
      removeDir, onDirKeydown, onDirInput, onDirOpenList, onDirBlurClose, pickDir,
      // 后台 agent 段
      BG_TRIG_OPS, BG_TRIG_OP_CN, BG_TRIG_SOURCES, BG_SCHED, BG_TRIG_NUMS, BG_MODEL_NUMS,
      bgEditing, bgForm, bgDelArmed, bgBadge, bgTitle, bgRenamed, bgConflict, bgKeyHint,
      bgHasErr, bgErrTip, bgToggleFromAi,
      bgSelect, bgAdd, onBgDelete, bgToggleTrigger, bgToggleEvent, bgToggleTool, bgToggleVerb, bgClearKey,
      saving, save, report, reloadFromServer
    }
  },
  template: `
    <!-- backdrop：压暗主页面，点击关闭（与 X/Escape 同经 requestClose——dirty 守卫唯一入口） -->
    <div class="st-backdrop" :class="{ show: state.settingsOpen }" aria-hidden="true" @click="requestClose"></div>

    <!-- 居中对话框（桌面 ≤880px×≤86vh：头部固定 + 体部内滚，主页面零 reflow；≤639px 全屏，
         壳高走 fixed 布局视口链；折叠态 visibility 出 Tab 序 + opacity/transform ≤.2s 转场，
         折叠壳家族与 RM 两态同杀见 style.css st-modal 族）。确认浮层弹出时 inert：焦点/点击
         与自然 Tab 均困于确认浮层（alertdialog 层级在其之上） -->
    <div ref="panelRef" class="st-modal" :class="{ open: state.settingsOpen }" :inert="confirmOpen"
      role="dialog" aria-modal="true" aria-label="设置">
      <div class="st-modal-head">
        <h1 class="st-modal-title">设置</h1>
        <!-- 移动头部保存（≤639px 专用，页内保存行 CSS 隐藏归一）；X 桌面移动恒在 -->
        <button class="st-save st-m-save" type="button" :disabled="saving" @click="save">{{ saving ? '保存中…' : '保存' }}</button>
        <button class="st-x" type="button" title="关闭设置" aria-label="关闭设置" @click="requestClose">
          <svg aria-hidden="true" viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg>
        </button>
      </div>
      <!-- 告警固定区（头部下方、滚动区之外——非字段校验族必现）：409 冲突 / 500 热生效与保存失败；
           422 校验错误已下沉表单项内联（本区不再承接字段校验）。aria-live=polite 保存结果不抢读屏。
           成功无条 = 分段徽标归零即信号 -->
      <div class="st-alerts" aria-live="polite">
        <div v-if="report && report.kind === 'conflict'" class="st-banner warn">
          配置已被页面外修改，本次保存未生效。
          <button class="mp-back st-banner-btn" type="button" title="重新载入服务端最新配置" @click="reloadFromServer()">重载</button>
          <span class="st-hint st-banner-hint">重载后本页未保存的修改将丢弃</span>
        </div>
        <div v-else-if="report && report.kind === 'savedapply'" class="st-banner warn">{{ report.message }}</div>
        <div v-else-if="report && report.kind === 'error'" class="st-banner err">{{ report.message }}</div>
      </div>
      <div class="st-modal-body">

          <!-- 加载 / 错误态（503 = 设置管理未接线；表单缺席才成卡） -->
          <div v-if="loading && !form" class="mcard"><div class="zline">加载配置…</div></div>
          <div v-else-if="loadError && !form" class="mcard">
            <div class="zline">{{ loadError }}</div>
            <div class="st-retry"><button class="mp-back" type="button" @click="load()">重试</button></div>
          </div>

          <template v-else-if="form">
            <div class="st-cols">
              <!-- 分段导航：桌面左列锚点 / ≤639 顶部分段钮（mode-btn 激活态先例 + aria-pressed） -->
              <nav class="st-nav" aria-label="设置分段">
                <button v-for="s in SECTIONS" :key="s.id" class="st-nav-btn" type="button"
                  :class="{ active: section === s.id }" :aria-pressed="section === s.id"
                  @click="section = s.id">{{ s.label }}</button>
                <!-- 记忆段（T6）：图标用 ICONS 现图（link = 关联网络意象，沿「图标单点」不加新条目）；
                     徽标 = memoryBadge（badgeParts.total = 待审+失败，口径同原 rail 记忆钮）>0 才画；
                     构成分解 title 留在 FAB 设置子钮（badges 双键），本段不重复 -->
                <button class="st-nav-btn" type="button"
                  :class="{ active: section === 'memory' }" :aria-pressed="section === 'memory'"
                  title="记忆" @click="section = 'memory'"><span class="st-nav-ic" v-html="ICONS.link"></span>记忆<span
                    v-if="memoryBadge > 0" class="st-nav-badge">{{ memoryBadge > 99 ? '99+' : memoryBadge }}</span></button>
              </nav>

              <div class="st-body">
                <!-- ═══ 主智能体（卡头右上角 = 分段保存状态徽标，下同）═══ -->
                <div v-show="section === 'ai'" class="mcard">
                  <div class="mc-h">
                    <span class="mc-t">主智能体</span>
                    <span class="st-sv" :class="{ dirty: dirtyAi }" :title="dirtyAi ? '本段有未保存的修改' : '本段已与服务端一致'">{{ dirtyAi ? '未保存' : '已保存' }}</span>
                  </div>
                  <div class="st-fields">
                    <!-- 卡片顶兜底行（422 message 定位不到具体字段时；字段级红字为主呈现） -->
                    <div v-if="cardErrs('ai').length" class="st-card-err">
                      <span v-for="(m, i) in cardErrs('ai')" :key="i">{{ m }}</span>
                    </div>
                    <label class="st-field">
                      <span class="st-lbl">服务地址</span>
                      <input class="st-input" type="text" autocomplete="off" placeholder="例如 http://127.0.0.1:11434/v1"
                        v-model="form.ai.base_url">
                    </label>
                    <label class="st-field">
                      <span class="st-lbl">模型名称</span>
                      <input class="st-input" data-fk="ai.model" :class="{ 'is-err': err('ai.model') }"
                        type="text" autocomplete="off" placeholder="例如 qwen2.5:14b"
                        v-model="form.ai.model" @input="clearErr('ai.model')">
                      <span v-if="err('ai.model')" class="st-field-err">{{ err('ai.model') }}</span>
                    </label>
                    <div class="st-field">
                      <label class="st-lbl" for="st-api-key">密钥</label>
                      <div class="st-keyrow">
                        <input id="st-api-key" class="st-input" type="password" autocomplete="new-password"
                          :placeholder="apiKeySet ? '已设置（留空保持不变）' : '未设置'"
                          :title="apiKeyMask ? '当前密钥：' + apiKeyMask : ''" v-model="apiKeyInput">
                        <button v-if="apiKeySet && !apiKeyTouched" class="mp-back" type="button"
                          title="清除已保存的密钥（保存后生效）" @click="clearApiKey">清除</button>
                      </div>
                      <span v-if="apiKeyHint" class="st-hint">{{ apiKeyHint }}</span>
                    </div>
                    <div class="st-grid">
                      <label v-for="p in AI_PARAMS" :key="p.key" class="st-field">
                        <span class="st-lbl">{{ p.label }}</span>
                        <input class="st-input" :data-fk="'ai.' + p.key" :class="{ 'is-err': err('ai.' + p.key) }"
                          type="number" :min="p.min" :max="p.max" :step="p.step"
                          v-model="form.ai[p.key]" @input="clearErr('ai.' + p.key)">
                        <span v-if="err('ai.' + p.key)" class="st-field-err">{{ err('ai.' + p.key) }}</span>
                      </label>
                    </div>
                    <div class="st-field" role="group" aria-labelledby="st-tools-lbl">
                      <span class="st-lbl" id="st-tools-lbl">工具白名单</span>
                      <div class="st-seg" role="group" aria-label="工具白名单模式">
                        <button class="st-seg-btn" type="button" :class="{ on: toolsMode === 'all' }"
                          :aria-pressed="toolsMode === 'all'" @click="setToolsMode('all')">全部工具</button>
                        <button class="st-seg-btn" type="button" :class="{ on: toolsMode === 'custom' }"
                          :aria-pressed="toolsMode === 'custom'" @click="setToolsMode('custom')">自定义</button>
                      </div>
                      <template v-if="toolsMode === 'custom'">
                        <div v-if="meta.tools.length" class="st-chips" data-fk="ai.tools">
                          <button v-for="t in meta.tools" :key="t" class="st-chip st-toolchip" type="button"
                            :class="toolCls(t, toolsSel.includes(t))" :aria-pressed="toolsSel.includes(t)"
                            :title="toolTitle(t, toolsSel.includes(t))" @click="toggleTool(t)">{{ toolLabel(t) }}<span
                              v-if="toolCls(t, toolsSel.includes(t)).ask" class="st-chip-ask" aria-hidden="true">询问</span></button>
                        </div>
                        <div v-else class="zline">工具清单暂不可用</div>
                        <span v-if="err('ai.tools')" class="st-field-err">{{ err('ai.tools') }}</span>
                        <span class="st-hint">点选启用工具，至少保留一项</span>
                        <span class="st-hint">允许 = 自动执行 · 询问 = 执行前需你确认 · 拒绝 = 未授权</span>
                      </template>
                      <span v-else class="st-hint">当前开放全部工具</span>
                    </div>
                  </div>
                </div>

                <!-- ═══ 后台智能体 ═══ -->
                <div v-show="section === 'bg'">
                  <div class="mcard">
                    <div class="mc-h">
                      <span class="mc-t">后台智能体</span>
                      <span class="st-sv" :class="{ dirty: dirtyBg }" :title="dirtyBg ? '本段有未保存的修改' : '本段已与服务端一致'">{{ dirtyBg ? '未保存' : '已保存' }}</span>
                    </div>
                    <div v-if="form.background_agents.length" class="st-bg-list">
                      <div v-for="(e, i) in form.background_agents" :key="i" class="st-bg-row"
                        :class="{ sel: bgEditing === i }">
                        <button type="button" class="st-bg-main" :aria-expanded="bgEditing === i"
                          @click="bgSelect(i)">
                          <span class="st-bg-name">{{ bgTitle(e) }}</span>
                          <span class="st-bg-meta">
                            <span class="st-bg-alias">{{ e.alias || '未填标识' }}</span>
                            <span v-if="e.readOnly" class="st-bg-tag">执行器</span>
                            <!-- 涉错红标：折叠条目的字段错误不可见是红线，列表行兜底可见（title=首条错误） -->
                            <span v-if="bgHasErr(i)" class="st-bg-err" :title="bgErrTip(i)">需修正</span>
                          </span>
                        </button>
                        <span class="st-sv" :class="{ dirty: bgEntryDirty(i) }" :title="bgEntryDirty(i) ? '此条目有未保存的修改' : '此条目已与服务端一致'">{{ bgEntryDirty(i) ? '未保存' : '已保存' }}</span>
                        <span class="st-badge" :class="bgBadge(e).cls" :title="bgBadge(e).tip">{{ bgBadge(e).label }}</span>
                        <button class="st-toggle" :class="{ on: e.enabled }" type="button" role="switch"
                          :aria-checked="e.enabled" :aria-label="(e.enabled ? '停用 ' : '启用 ') + bgTitle(e)"
                          @click="e.enabled = !e.enabled"></button>
                      </div>
                    </div>
                    <div v-else class="zline">暂无后台智能体，可点「添加后台智能体」新建</div>
                    <div class="st-bg-foot">
                      <button class="st-add" type="button" @click="bgAdd()">添加后台智能体</button>
                    </div>
                  </div>

                  <div v-if="bgForm" class="mcard st-bg-form">
                    <div class="mc-h"><span class="mc-t">{{ bgTitle(bgForm) }}</span></div>
                    <div v-if="!bgForm.readOnly" class="st-fields">
                      <!-- 卡片顶兜底行（422 message 定不到条目/字段时） -->
                      <div v-if="cardErrs('bg').length" class="st-card-err">
                        <span v-for="(m, i) in cardErrs('bg')" :key="i">{{ m }}</span>
                      </div>
                      <div class="st-group">基本</div>
                      <div class="st-grid">
                        <label class="st-field">
                          <span class="st-lbl">名称</span>
                          <input class="st-input" type="text" autocomplete="off" placeholder="例如 日记每日整理"
                            v-model="bgForm.name">
                        </label>
                        <label class="st-field">
                          <span class="st-lbl">标识</span>
                          <input class="st-input st-input-mono" :data-fk="'bg.' + bgEditing + '.alias'"
                            :class="{ 'is-err': err('bg.' + bgEditing + '.alias') }" type="text" autocomplete="off"
                            placeholder="例如 organizer" v-model="bgForm.alias" @input="clearErr('bg.' + bgEditing + '.alias')">
                          <span v-if="err('bg.' + bgEditing + '.alias')" class="st-field-err">{{ err('bg.' + bgEditing + '.alias') }}</span>
                        </label>
                      </div>
                      <span v-if="bgRenamed(bgForm)" class="st-warn-inline">改名将清空该条已存密钥（需重填），且触发计权从零开始</span>
                      <label class="st-field">
                        <span class="st-lbl">工作目录</span>
                        <input class="st-input st-input-mono" :data-fk="'bg.' + bgEditing + '.target_dir'"
                          :class="{ 'is-err': err('bg.' + bgEditing + '.target_dir') }" type="text" autocomplete="off"
                          placeholder="例如 notes" v-model="bgForm.target_dir" @input="clearErr('bg.' + bgEditing + '.target_dir')">
                        <span v-if="err('bg.' + bgEditing + '.target_dir')" class="st-field-err">{{ err('bg.' + bgEditing + '.target_dir') }}</span>
                      </label>
                      <label class="st-field">
                        <span class="st-lbl">提示词文件</span>
                        <input class="st-input st-input-mono" :data-fk="'bg.' + bgEditing + '.prompt'"
                          :class="{ 'is-err': err('bg.' + bgEditing + '.prompt') }" type="text" autocomplete="off"
                          placeholder="例如 server/prompts/x.md" v-model="bgForm.prompt" @input="clearErr('bg.' + bgEditing + '.prompt')">
                        <span v-if="err('bg.' + bgEditing + '.prompt')" class="st-field-err">{{ err('bg.' + bgEditing + '.prompt') }}</span>
                        <span class="st-hint">相对配置文件的路径</span>
                      </label>
                      <label class="st-field">
                        <span class="st-lbl">报告目录</span>
                        <input class="st-input st-input-mono" :data-fk="'bg.' + bgEditing + '.report_dir'"
                          :class="{ 'is-err': err('bg.' + bgEditing + '.report_dir') }" type="text" autocomplete="off"
                          placeholder="例如 notes/reports" v-model="bgForm.report_dir" @input="clearErr('bg.' + bgEditing + '.report_dir')">
                        <span v-if="err('bg.' + bgEditing + '.report_dir')" class="st-field-err">{{ err('bg.' + bgEditing + '.report_dir') }}</span>
                        <span class="st-hint">留空 = 工作目录下的默认报告区</span>
                      </label>
                      <div class="st-field">
                        <span class="st-lbl">启用</span>
                        <button class="st-toggle" :class="{ on: bgForm.enabled }" type="button" role="switch"
                          :aria-checked="bgForm.enabled" aria-label="启用此条目"
                          @click="bgForm.enabled = !bgForm.enabled"></button>
                        <span class="st-hint">停用后保存：该任务从调度中退出，进行中的一轮跑完即停</span>
                      </div>

                      <div class="st-group">调度与预算</div>
                      <label class="st-field">
                        <span class="st-lbl">定时计划</span>
                        <input class="st-input st-input-mono" :data-fk="'bg.' + bgEditing + '.cron'"
                          :class="{ 'is-err': err('bg.' + bgEditing + '.cron') }" type="text" autocomplete="off"
                          placeholder="例如 0 2 * * *（每日 02:00）" v-model="bgForm.cron" @input="clearErr('bg.' + bgEditing + '.cron')">
                        <span v-if="err('bg.' + bgEditing + '.cron')" class="st-field-err">{{ err('bg.' + bgEditing + '.cron') }}</span>
                      </label>
                      <div class="st-grid">
                        <label v-for="p in BG_SCHED" :key="p.key" class="st-field">
                          <span class="st-lbl">{{ p.label }}</span>
                          <input class="st-input" :data-fk="'bg.' + bgEditing + '.' + p.key"
                            :class="{ 'is-err': err('bg.' + bgEditing + '.' + p.key) }" type="number" :min="p.min" :step="p.step"
                            v-model="bgForm[p.key]" @input="clearErr('bg.' + bgEditing + '.' + p.key)">
                          <span v-if="err('bg.' + bgEditing + '.' + p.key)" class="st-field-err">{{ err('bg.' + bgEditing + '.' + p.key) }}</span>
                        </label>
                      </div>
                      <div class="st-field" role="group" aria-label="事件触发" :data-fk="'bg.' + bgEditing + '.trigger'">
                        <div class="st-trg-head">
                          <span class="st-lbl">事件触发</span>
                          <button class="st-toggle" :class="{ on: bgForm.trigger }" type="button" role="switch"
                            :aria-checked="!!bgForm.trigger" aria-label="开启事件触发"
                            @click="bgToggleTrigger(bgEditing)"></button>
                        </div>
                        <span v-if="err('bg.' + bgEditing + '.trigger')" class="st-field-err">{{ err('bg.' + bgEditing + '.trigger') }}</span>
                        <span class="st-hint">关闭 = 仅按计划定时运行</span>
                        <template v-if="bgForm.trigger">
                          <select class="st-input" aria-label="触发来源" v-model="bgForm.trigger.source">
                            <option v-for="s in BG_TRIG_SOURCES" :key="s.v" :value="s.v">{{ s.label }}</option>
                          </select>
                          <div v-if="bgForm.trigger.source === 'change'" class="st-chips">
                            <button v-for="op in BG_TRIG_OPS" :key="op" class="st-chip" type="button"
                              :class="{ on: bgForm.trigger.events.includes(op) }"
                              @click="bgToggleEvent(bgEditing, op)">{{ BG_TRIG_OP_CN[op] }}</button>
                          </div>
                          <span v-else class="st-hint">按无命中或低分的召回查询触发</span>
                          <div class="st-grid">
                            <label v-for="p in BG_TRIG_NUMS" :key="p.key" class="st-field">
                              <span class="st-lbl">{{ p.label }}</span>
                              <input class="st-input" :data-fk="'bg.' + bgEditing + '.trigger.' + p.key"
                                :class="{ 'is-err': err('bg.' + bgEditing + '.trigger.' + p.key) }" type="number" :min="p.min" :max="p.max" :step="p.step"
                                v-model="bgForm.trigger[p.key]" @input="clearErr('bg.' + bgEditing + '.trigger.' + p.key)">
                              <span v-if="err('bg.' + bgEditing + '.trigger.' + p.key)" class="st-field-err">{{ err('bg.' + bgEditing + '.trigger.' + p.key) }}</span>
                            </label>
                          </div>
                        </template>
                      </div>

                      <div class="st-group">工具</div>
                      <div class="st-field" role="group" aria-label="工具授权" :data-fk="'bg.' + bgEditing + '.tools'">
                        <div v-if="meta.tools.length" class="st-chips">
                          <button v-for="t in meta.tools" :key="t" class="st-chip st-toolchip" type="button"
                            :class="toolCls(t, bgForm.tools.includes(t))" :aria-pressed="bgForm.tools.includes(t)"
                            :title="toolTitle(t, bgForm.tools.includes(t))" @click="bgToggleTool(bgEditing, t)">{{ toolLabel(t) }}<span
                              v-if="toolCls(t, bgForm.tools.includes(t)).ask" class="st-chip-ask" aria-hidden="true">询问</span></button>
                        </div>
                        <div v-else class="zline">工具清单暂不可用</div>
                        <span v-if="err('bg.' + bgEditing + '.tools')" class="st-field-err">{{ err('bg.' + bgEditing + '.tools') }}</span>
                        <span class="st-hint">点选启用工具，至少保留一项 · 允许 = 自动执行 · 询问 = 执行前需你确认 · 拒绝 = 未授权</span>
                        <span v-if="bgConflict(bgForm)" class="st-warn-inline">提案与直写工具不可同时启用，保存会被拒绝</span>
                        <template v-if="bgForm.tools.includes('propose')">
                          <div v-if="meta.verbs.length" class="st-chips" :data-fk="'bg.' + bgEditing + '.verbs'">
                            <button v-for="v in meta.verbs" :key="v" class="st-chip" type="button"
                              :class="{ on: bgForm.verbs.includes(v) }" :aria-pressed="bgForm.verbs.includes(v)"
                              @click="bgToggleVerb(bgEditing, v)">{{ v }}</button>
                          </div>
                          <span v-if="err('bg.' + bgEditing + '.verbs')" class="st-field-err">{{ err('bg.' + bgEditing + '.verbs') }}</span>
                          <span class="st-hint">选择该任务可提交的提案类型</span>
                        </template>
                        <span v-else class="st-hint">启用提案工具后可在此限定提案类型</span>
                      </div>

                      <div class="st-group">模型</div>
                      <!-- 复用主智能体模型（spec §6.2）：勾选态隐藏整组字段（表单值原地保留，
                           取消勾选即回显），保存条目只带 model_from_ai 瞬态键；文案 = 一次性复制的诚实语义 -->
                      <div class="st-field">
                        <div class="st-trg-head">
                          <span class="st-lbl">复用主智能体模型</span>
                          <button class="st-toggle" :class="{ on: bgForm.fromAi }" type="button" role="switch"
                            :aria-checked="bgForm.fromAi" aria-label="复用主智能体模型"
                            @click="bgToggleFromAi()"></button>
                        </div>
                        <span v-if="bgForm.fromAi" class="st-hint">保存时把主智能体的服务地址、模型名、参数与密钥同步进本条目；之后主模型改动不会自动跟随——重新勾选并保存即再次同步。</span>
                      </div>
                      <template v-if="!bgForm.fromAi">
                      <div class="st-grid">
                        <label class="st-field">
                          <span class="st-lbl">接口协议</span>
                          <input class="st-input st-input-mono" type="text" autocomplete="off" placeholder="openai"
                            v-model="bgForm.model.provider">
                        </label>
                        <label class="st-field">
                          <span class="st-lbl">模型名称</span>
                          <input class="st-input" :data-fk="'bg.' + bgEditing + '.model.model_id'"
                            :class="{ 'is-err': err('bg.' + bgEditing + '.model.model_id') }" type="text" autocomplete="off"
                            placeholder="例如 qwen2.5:14b" v-model="bgForm.model.model_id" @input="clearErr('bg.' + bgEditing + '.model.model_id')">
                          <span v-if="err('bg.' + bgEditing + '.model.model_id')" class="st-field-err">{{ err('bg.' + bgEditing + '.model.model_id') }}</span>
                        </label>
                      </div>
                      <label class="st-field">
                        <span class="st-lbl">服务地址</span>
                        <input class="st-input" type="text" autocomplete="off"
                          placeholder="例如 http://127.0.0.1:11434/v1" v-model="bgForm.model.base_url">
                      </label>
                      <div class="st-field">
                        <label class="st-lbl" :for="'st-bg-key-' + bgEditing">密钥</label>
                        <div class="st-keyrow">
                          <input :id="'st-bg-key-' + bgEditing" class="st-input" type="password"
                            autocomplete="new-password"
                            :placeholder="bgForm.model.keySet ? '已设置（留空保持不变）' : '未设置'"
                            :title="bgForm.model.keyMask ? '当前密钥：' + bgForm.model.keyMask : ''"
                            v-model="bgForm.model.keyInput">
                          <button v-if="bgForm.model.keySet && !bgForm.model.keyTouched" class="mp-back" type="button"
                            title="清除已保存的密钥（保存后生效）" @click="bgClearKey(bgEditing)">清除</button>
                        </div>
                        <span v-if="bgForm.model.keyTouched" class="st-hint">{{ bgKeyHint(bgForm) }}</span>
                      </div>
                      <div class="st-grid">
                        <label v-for="p in BG_MODEL_NUMS" :key="p.key" class="st-field">
                          <span class="st-lbl">{{ p.label }}</span>
                          <input class="st-input" :data-fk="'bg.' + bgEditing + '.model.' + p.key"
                            :class="{ 'is-err': err('bg.' + bgEditing + '.model.' + p.key) }" type="number" :min="p.min" :max="p.max" :step="p.step"
                            v-model="bgForm.model[p.key]" @input="clearErr('bg.' + bgEditing + '.model.' + p.key)">
                          <span v-if="err('bg.' + bgEditing + '.model.' + p.key)" class="st-field-err">{{ err('bg.' + bgEditing + '.model.' + p.key) }}</span>
                        </label>
                      </div>
                      </template>
                    </div>
                    <div v-else class="st-fields">
                      <div class="st-grid">
                        <div class="st-field"><span class="st-lbl">名称</span><span class="st-val">{{ bgForm.name || '—' }}</span></div>
                        <div class="st-field"><span class="st-lbl">标识</span><span class="st-val st-val-mono">{{ bgForm.alias || '—' }}</span></div>
                        <div class="st-field"><span class="st-lbl">定时计划</span><span class="st-val st-val-mono">{{ bgForm.cron || '—' }}</span></div>
                        <div class="st-field"><span class="st-lbl">工作目录</span><span class="st-val st-val-mono">{{ bgForm.target_dir || '—' }}</span></div>
                      </div>
                      <span class="st-hint">此条目不可在本页编辑</span>
                    </div>
                    <div class="st-bg-foot">
                      <button class="st-danger" :class="{ armed: bgDelArmed === bgEditing }" type="button"
                        @click="onBgDelete(bgEditing)">{{ bgDelArmed === bgEditing ? '确认删除？' : '删除此条目' }}</button>
                      <span class="st-hint">若正在运行，本轮跑完即停</span>
                    </div>
                  </div>
                </div>

                <!-- ═══ 杂项 ═══ -->
                <div v-show="section === 'misc'" class="mcard">
                  <div class="mc-h">
                    <span class="mc-t">杂项</span>
                    <span class="st-sv" :class="{ dirty: dirtyMisc }" :title="dirtyMisc ? '本段有未保存的修改' : '本段已与服务端一致'">{{ dirtyMisc ? '未保存' : '已保存' }}</span>
                  </div>
                  <div class="st-fields">
                    <!-- 卡片顶兜底行（422 message 定位不到具体字段时） -->
                    <div v-if="cardErrs('misc').length" class="st-card-err">
                      <span v-for="(m, i) in cardErrs('misc')" :key="i">{{ m }}</span>
                    </div>
                    <div class="st-grid">
                      <label class="st-field">
                        <span class="st-lbl">每日通知上限</span>
                        <input class="st-input" data-fk="misc.daily_cap" :class="{ 'is-err': err('misc.daily_cap') }"
                          type="number" min="1" step="1" v-model="form.notifications.daily_cap" @input="clearErr('misc.daily_cap')">
                        <span v-if="err('misc.daily_cap')" class="st-field-err">{{ err('misc.daily_cap') }}</span>
                      </label>
                      <label class="st-field">
                        <span class="st-lbl">日志保留天数</span>
                        <input class="st-input" data-fk="misc.log.retention_days" :class="{ 'is-err': err('misc.log.retention_days') }"
                          type="number" min="1" step="1" v-model="form.log.retention_days" @input="clearErr('misc.log.retention_days')">
                        <span v-if="err('misc.log.retention_days')" class="st-field-err">{{ err('misc.log.retention_days') }}</span>
                      </label>
                      <label class="st-field">
                        <span class="st-lbl">日志级别</span>
                        <select class="st-input" v-model="form.log.level">
                          <option v-for="lv in LOG_LEVELS" :key="lv" :value="lv">{{ lv }}</option>
                        </select>
                      </label>
                    </div>
                    <!-- 容器必须 div 不能 label：label 内空白/文字点击被浏览器转发激活第一个 labelable
                         后代（button 家族下 = 第一个 tag 的 ×）→ 点标题/提示即误删标签（2026-11-22 根因
                         复盘，AGENTS「锚点激活行为」泛化红线）；标签语义由 aria-labelledby 补位 -->
                    <div class="st-field st-dir-field">
                      <span class="st-lbl" id="st-privacy-lbl">隐私目录</span>
                      <div v-if="form.privacy.dirs.length" class="st-tags">
                        <span v-for="(d, i) in form.privacy.dirs" :key="d" class="st-tag">{{ d }}<button
                          class="st-tag-x" type="button" :aria-label="'移除隐私目录 ' + d" @click="removeDir(i)">×</button></span>
                      </div>
                      <input ref="dirInputRef" class="st-input" :class="{ 'is-err': dirErr }" type="text" autocomplete="off"
                        placeholder="输入目录名，回车添加" v-model="dirDraft"
                        aria-labelledby="st-privacy-lbl" role="combobox" :aria-expanded="dirOpen"
                        aria-controls="st-dir-list" aria-autocomplete="list"
                        :aria-activedescendant="dirOpen && dirActive >= 0 ? 'st-dir-opt-' + dirActive : null"
                        @keydown="onDirKeydown" @input="onDirInput" @focus="onDirOpenList" @blur="onDirBlurClose">
                      <!-- 候选面板走流内展开而非 absolute 浮层：设置体是内滚容器（st-modal-body overflow-y），
                           本字段近段尾，浮层会被底缘裁切；视觉仍走卡族 surface+line+影 -->
                      <div v-if="dirOpen && dirCandidates.length" id="st-dir-list" ref="dirListRef" class="st-dir-list"
                        role="listbox" aria-label="一级目录候选">
                        <button v-for="(d, i) in dirCandidates" :key="d" class="st-dir-opt" type="button" role="option"
                          tabindex="-1" :id="'st-dir-opt-' + i" :aria-selected="dirActive === i"
                          :class="{ active: dirActive === i }" @mousedown.prevent @click="pickDir(d)">{{ d }}</button>
                      </div>
                      <span v-if="dirErr" class="st-field-err">{{ dirErr }}</span>
                      <span class="st-hint">名单内目录的内容受隐私保护 · ↑↓ 可选库内一级目录</span>
                    </div>
                  </div>
                </div>

                <!-- ═══ 记忆（T6，spec 2026-11-22-immersive-shell §6）：MemoryConsole 三面整体迁入，
                     宽度随 pane、滚动归 st-modal-body（.st-pane-memory 样式见 style.css 记忆控制台分节）；
                     关闭守卫边界——记忆段不进 dirty 指纹（本段零表单字段），X/backdrop/Escape 关闭
                     仍统一走 requestClose（settingsCloseSeq 链零改动），故记忆头「关闭」钮直连 requestClose。
                     数据零自建轮询：memoryStatus/Proposals/Reports 由 app 30s pollMemory 注入 state，
                     props 下传（与移动 console-shell 实例同口径——组件 scoped ref 已隔离双实例）。 ═══ -->
                <div v-if="section === 'memory'" class="st-pane-memory">
                  <memory-console :open="true" :detail="state.memoryDetail" :report="state.memoryReport"
                    @pick="state.memoryDetail = $event"
                    @back="state.memoryDetail = null"
                    @report-pick="state.memoryReport = $event"
                    @report-back="state.memoryReport = null"
                    @close="requestClose"
                    @actioned="onMemActioned"
                    @open-doc="onMemOpenDoc($event)" />
                </div>

                <!-- 保存全部（单一写面；revision 随行）——记忆段无表单，保存行/页底说明收起 -->
                <div v-show="section !== 'memory'" class="st-foot">
                  <button class="st-save" type="button" :disabled="saving" @click="save">
                    {{ saving ? '保存中…' : '保存全部' }}
                  </button>
                </div>
              </div>
            </div>

            <!-- 页底静态说明（spec §6）：页面外段的手工维护口径（用户语言，Rule 2） -->
            <div v-show="section !== 'memory'" class="st-footnote">
              网络与端口、数据目录、出网开关与写通道凭证等设置请手工编辑配置文件（重启后生效）。
            </div>
          </template>
      </div>
    </div>

    <!-- 关闭确认浮层（spec §6 三选，替代原生 confirm）：role=alertdialog；层级 = 设置浮层(81)
         之上、toast(100) 之下（style.css st-confirm 族）；折叠壳家族照 st-modal 纪律（visibility
         出 Tab 序 + 转场仅 opacity/transform + RM 两态同杀）；默认焦点 = 取消钮，Escape = 取消，
         设置浮层 inert 时自然 Tab 困于本层。保存并关闭失败（预检/409/422）：本层收起、设置浮层不关、
         错误照报（字段级红字/卡片顶兜底/toast）；放弃修改 = 重拉服务端真相后关闭 -->
    <div class="st-confirm-backdrop" :class="{ show: confirmOpen }" aria-hidden="true" @click="cancelConfirm"></div>
    <div ref="confirmRef" class="st-confirm" :class="{ open: confirmOpen }" :inert="!confirmOpen"
      role="alertdialog" aria-modal="true" aria-labelledby="st-confirm-t" aria-describedby="st-confirm-d"
      @keydown="onConfirmKeydown">
      <h2 id="st-confirm-t" class="st-confirm-t">有未保存的修改</h2>
      <p id="st-confirm-d" class="st-confirm-d">关闭设置后，这些改动会丢失。</p>
      <div class="st-confirm-acts">
        <button class="st-confirm-btn primary" type="button" :disabled="saving" @click="saveAndClose">{{ saving ? '保存中…' : '保存并关闭' }}</button>
        <button class="st-confirm-btn" type="button" @click="discardAndClose">放弃修改</button>
        <button ref="cancelBtnRef" class="st-confirm-btn" type="button" @click="cancelConfirm">取消</button>
      </div>
    </div>
  `
}
