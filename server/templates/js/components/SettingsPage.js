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
 *   错误条（409/422/500）固定于浮层头部下方、滚动区之外（必现）；成功不再有条——徽标归零即信号。
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
 */
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
// 建模键全集：raw 中其余键（escalation 等）保存时原样合并回条目（不可编辑不可丢）
const BG_MODELED = new Set(['alias', 'name', 'enabled', 'cron', 'target_dir', 'prompt', 'report_dir',
  'max_rounds', 'timeout_s', 'budget_runs_per_day', 'budget_max_proposals', 'trigger', 'tools', 'verbs', 'model'])
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
    tools: [], verbs: [], trigger: null, model: bgBlankModel()
  }
}
// raw 条目（GET payload 形）→ 编辑镜像；api_key 止步掩码 {set,mask}，touched 每条目独立
function bgEntryView(raw) {
  const r = raw && typeof raw === 'object' ? raw : {}
  const m = r.model && typeof r.model === 'object' ? r.model : {}
  const key = m.api_key && typeof m.api_key === 'object' ? m.api_key : null
  const trig = r.trigger && typeof r.trigger === 'object' ? r.trigger : null
  return {
    raw: JSON.parse(JSON.stringify(r)),
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
    }
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

export const SettingsPage = {
  name: 'SettingsPage',
  emits: ['close'],
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
          const gear = document.getElementById('settings-rail-btn')
          if (gear) gear.focus({ preventScroll: true })
          return
        }
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
      confirmOpen.value = false   // 422/409：确认浮层收起、设置浮层不关、错误条照报（固定头部下方区）
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
      // 服务端真相回填 = 条目编辑态归零（保存后重载同路径：折叠列表、解武装）
      bgEditing.value = -1
      disarmBgDelete()
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
      if (mode === 'custom' && !toolsSel.value.length) toolsSel.value = [...(meta.value.tools || [])]
    }
    function toggleTool(t) {
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

    // ═══ 隐私目录标签（输入 + 回车成标签、× 删；重复即忽略）───
    const dirDraft = ref('')
    const dirInputRef = ref(null)
    function addDir() {
      const v = String(dirDraft.value).trim()
      if (!v) return
      if (!form.value.privacy.dirs.includes(v)) form.value.privacy.dirs.push(v)
      dirDraft.value = ''
      // 键盘聚焦红线（AGENTS Gotchas）：preventScroll 防键盘弹起时视口被程序化平移
      if (dirInputRef.value) dirInputRef.value.focus({ preventScroll: true })
    }
    function removeDir(i) { form.value.privacy.dirs.splice(i, 1) }
    function onDirKeydown(e) {
      if (e.key === 'Enter') { e.preventDefault(); addDir() }
    }

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
      form.value.background_agents.splice(i, 1)
      if (bgEditing.value === i) bgEditing.value = -1
      else if (bgEditing.value > i) bgEditing.value -= 1
    }
    onBeforeUnmount(disarmBgDelete)

    function bgToggleTrigger(i) {
      const e = form.value.background_agents[i]
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
      const k = e.tools.indexOf(t)
      if (k >= 0) e.tools.splice(k, 1)
      else e.tools.push(t)
    }
    function bgToggleVerb(i, v) {
      const e = form.value.background_agents[i]
      if (!e) return
      const k = e.verbs.indexOf(v)
      if (k >= 0) e.verbs.splice(k, 1)
      else e.verbs.push(v)
    }
    function bgClearKey(i) {
      const e = form.value.background_agents[i]
      if (!e) return
      e.model.keyTouched = true
      e.model.keyInput = ''
    }
    const bgKeyHint = e => (String(e.model.keyInput) === '' ? '保存后将清除密钥' : '保存后将写入新密钥')

    // ═══ 保存（单一「保存全部」；成功无条——分段徽标归零即信号；409/422/500 错误条固定头部
    //     下方区必现；500 {saved:true} 的 apply 失败窗口如实报——spec §4.2 步 6）═══
    const saving = ref(false)
    const report = ref(null)  // {kind:'conflict'|'invalid'|'savedapply'|'error', ...}

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

    // 返回 true = 已保存（保存并关闭据此放行）；false = 未保存，错误条已就位
    async function save() {
      if (saving.value || !form.value) return false
      saving.value = true
      report.value = null
      try {
        const res = await api.putSettings(buildPayload())
        if (res.status === 200) {
          // 保存成功后重载 GET：按新 revision 回填全表单（与后端真相一致，api_key 掩码同步刷新，
          // 分段徽标随 hydrate 基线重取归零）
          await load()
          return true
        }
        if (res.status === 409) {
          report.value = { kind: 'conflict' }
        } else if (res.status === 422) {
          report.value = { kind: 'invalid', errors: (res.data && res.data.errors) || [] }
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
      load()
    }

    // 分段导航（单一在场段；≤639 由 CSS 折成顶部分段钮行）
    const section = ref('ai')
    // 打开本段且全站脉搏未到货 → 静默补拉徽标数据（immediate 覆盖直落本段的后续打开路径）
    watch(section, (s) => { if (s === 'bg') ensureBgStatus() }, { immediate: true })

    return {
      state, panelRef, requestClose,
      SECTIONS, AI_PARAMS, LOG_LEVELS, section,
      loading, loadError, form, meta, load,
      apiKeySet, apiKeyMask, apiKeyTouched, apiKeyInput, apiKeyHint, clearApiKey,
      toolsMode, toolsSel, setToolsMode, toggleTool, toolLabel, toolCls, toolTitle,
      // 分段徽标 + 关闭确认浮层
      dirtyAi, dirtyBg, dirtyMisc, bgEntryDirty,
      confirmOpen, confirmRef, cancelBtnRef, cancelConfirm, discardAndClose, saveAndClose, onConfirmKeydown,
      dirDraft, dirInputRef, addDir, removeDir, onDirKeydown,
      // 后台 agent 段
      BG_TRIG_OPS, BG_TRIG_OP_CN, BG_TRIG_SOURCES, BG_SCHED, BG_TRIG_NUMS, BG_MODEL_NUMS,
      bgEditing, bgForm, bgDelArmed, bgBadge, bgTitle, bgRenamed, bgConflict, bgKeyHint,
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
      <!-- 错误条固定区（头部下方、滚动区之外——错误必现）：409 冲突 / 422 校验 / 500 如实报；
           aria-live=polite 保存结果不抢读屏。成功无条 = 分段徽标归零即信号 -->
      <div class="st-alerts" aria-live="polite">
        <div v-if="report && report.kind === 'conflict'" class="st-banner warn">
          配置已被页面外修改，本次保存未生效。
          <button class="mp-back st-banner-btn" type="button" title="重新载入服务端最新配置" @click="reloadFromServer()">重载</button>
          <span class="st-hint st-banner-hint">重载后本页未保存的修改将丢弃</span>
        </div>
        <div v-else-if="report && report.kind === 'invalid'" class="st-banner err">
          未保存——配置校验未通过：
          <ul v-if="report.errors.length" class="st-berrs">
            <li v-for="(e, i) in report.errors" :key="i">{{ e.message }}</li>
          </ul>
          <span v-else class="st-hint">服务端未返回具体原因</span>
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
              </nav>

              <div class="st-body">
                <!-- ═══ 主智能体（卡头右上角 = 分段保存状态徽标，下同）═══ -->
                <div v-show="section === 'ai'" class="mcard">
                  <div class="mc-h">
                    <span class="mc-t">主智能体</span>
                    <span class="st-sv" :class="{ dirty: dirtyAi }" :title="dirtyAi ? '本段有未保存的修改' : '本段已与服务端一致'">{{ dirtyAi ? '未保存' : '已保存' }}</span>
                  </div>
                  <div class="st-fields">
                    <label class="st-field">
                      <span class="st-lbl">服务地址</span>
                      <input class="st-input" type="text" autocomplete="off" placeholder="例如 http://127.0.0.1:11434/v1"
                        v-model="form.ai.base_url">
                    </label>
                    <label class="st-field">
                      <span class="st-lbl">模型名称</span>
                      <input class="st-input" type="text" autocomplete="off" v-model="form.ai.model">
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
                        <input class="st-input" type="number" :min="p.min" :max="p.max" :step="p.step"
                          v-model="form.ai[p.key]">
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
                        <div v-if="meta.tools.length" class="st-chips">
                          <button v-for="t in meta.tools" :key="t" class="st-chip st-toolchip" type="button"
                            :class="toolCls(t, toolsSel.includes(t))" :aria-pressed="toolsSel.includes(t)"
                            :title="toolTitle(t, toolsSel.includes(t))" @click="toggleTool(t)">{{ toolLabel(t) }}<span
                              v-if="toolCls(t, toolsSel.includes(t)).ask" class="st-chip-ask" aria-hidden="true">询问</span></button>
                        </div>
                        <div v-else class="zline">工具清单暂不可用</div>
                        <span class="st-hint">点选启用；全不选 = 主智能体不开放任何工具</span>
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
                          </span>
                        </button>
                        <span class="st-sv" :class="{ dirty: bgEntryDirty(i) }" :title="bgEntryDirty(i) ? '此条目有未保存的修改' : '此条目已与服务端一致'">{{ bgEntryDirty(i) ? '未保存' : '已保存' }}</span>
                        <span class="st-badge" :class="bgBadge(e).cls" :title="bgBadge(e).tip">{{ bgBadge(e).label }}</span>
                        <button class="st-toggle" :class="{ on: e.enabled }" type="button" role="switch"
                          :aria-checked="e.enabled" :aria-label="(e.enabled ? '停用 ' : '启用 ') + bgTitle(e)"
                          @click="e.enabled = !e.enabled">{{ e.enabled ? '启用' : '停用' }}</button>
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
                      <div class="st-group">基本</div>
                      <div class="st-grid">
                        <label class="st-field">
                          <span class="st-lbl">名称</span>
                          <input class="st-input" type="text" autocomplete="off" placeholder="界面显示名"
                            v-model="bgForm.name">
                        </label>
                        <label class="st-field">
                          <span class="st-lbl">标识</span>
                          <input class="st-input st-input-mono" type="text" autocomplete="off"
                            placeholder="英文短名，用于系统识别" v-model="bgForm.alias">
                        </label>
                      </div>
                      <span v-if="bgRenamed(bgForm)" class="st-warn-inline">改名将清空该条已存密钥（需重填），且触发计权从零开始</span>
                      <label class="st-field">
                        <span class="st-lbl">工作目录</span>
                        <input class="st-input st-input-mono" type="text" autocomplete="off"
                          placeholder="知识库内目录，例如 notes" v-model="bgForm.target_dir">
                      </label>
                      <label class="st-field">
                        <span class="st-lbl">提示词文件</span>
                        <input class="st-input st-input-mono" type="text" autocomplete="off"
                          placeholder="相对配置文件路径，例如 server/prompts/x.md" v-model="bgForm.prompt">
                      </label>
                      <label class="st-field">
                        <span class="st-lbl">报告目录</span>
                        <input class="st-input st-input-mono" type="text" autocomplete="off"
                          placeholder="留空 = 工作目录下的默认报告区" v-model="bgForm.report_dir">
                      </label>
                      <div class="st-field">
                        <span class="st-lbl">启用</span>
                        <button class="st-toggle" :class="{ on: bgForm.enabled }" type="button" role="switch"
                          :aria-checked="bgForm.enabled" aria-label="启用此条目"
                          @click="bgForm.enabled = !bgForm.enabled">{{ bgForm.enabled ? '启用' : '停用' }}</button>
                        <span class="st-hint">停用后保存：该任务从调度中退出，进行中的一轮跑完即停</span>
                      </div>

                      <div class="st-group">调度与预算</div>
                      <label class="st-field">
                        <span class="st-lbl">定时计划</span>
                        <input class="st-input st-input-mono" type="text" autocomplete="off"
                          placeholder="例如 0 2 * * *（每日 02:00）" v-model="bgForm.cron">
                      </label>
                      <div class="st-grid">
                        <label v-for="p in BG_SCHED" :key="p.key" class="st-field">
                          <span class="st-lbl">{{ p.label }}</span>
                          <input class="st-input" type="number" :min="p.min" :step="p.step" v-model="bgForm[p.key]">
                        </label>
                      </div>
                      <div class="st-field" role="group" aria-label="事件触发">
                        <div class="st-trg-head">
                          <span class="st-lbl">事件触发</span>
                          <button class="st-toggle" :class="{ on: bgForm.trigger }" type="button" role="switch"
                            :aria-checked="!!bgForm.trigger" aria-label="开启事件触发"
                            @click="bgToggleTrigger(bgEditing)">{{ bgForm.trigger ? '开' : '关' }}</button>
                        </div>
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
                              <input class="st-input" type="number" :min="p.min" :max="p.max" :step="p.step"
                                v-model="bgForm.trigger[p.key]">
                            </label>
                          </div>
                        </template>
                      </div>

                      <div class="st-group">工具</div>
                      <div class="st-field" role="group" aria-label="工具授权">
                        <div v-if="meta.tools.length" class="st-chips">
                          <button v-for="t in meta.tools" :key="t" class="st-chip st-toolchip" type="button"
                            :class="toolCls(t, bgForm.tools.includes(t))" :aria-pressed="bgForm.tools.includes(t)"
                            :title="toolTitle(t, bgForm.tools.includes(t))" @click="bgToggleTool(bgEditing, t)">{{ toolLabel(t) }}<span
                              v-if="toolCls(t, bgForm.tools.includes(t)).ask" class="st-chip-ask" aria-hidden="true">询问</span></button>
                        </div>
                        <div v-else class="zline">工具清单暂不可用</div>
                        <span class="st-hint">允许 = 自动执行 · 询问 = 执行前需你确认 · 拒绝 = 未授权</span>
                        <span v-if="bgConflict(bgForm)" class="st-warn-inline">提案与直写工具不可同时启用，保存会被拒绝</span>
                        <template v-if="bgForm.tools.includes('propose')">
                          <div v-if="meta.verbs.length" class="st-chips">
                            <button v-for="v in meta.verbs" :key="v" class="st-chip" type="button"
                              :class="{ on: bgForm.verbs.includes(v) }" :aria-pressed="bgForm.verbs.includes(v)"
                              @click="bgToggleVerb(bgEditing, v)">{{ v }}</button>
                          </div>
                          <span class="st-hint">选择该任务可提交的提案类型</span>
                        </template>
                        <span v-else class="st-hint">启用提案工具后可在此限定提案类型</span>
                      </div>

                      <div class="st-group">模型</div>
                      <div class="st-grid">
                        <label class="st-field">
                          <span class="st-lbl">接口协议</span>
                          <input class="st-input st-input-mono" type="text" autocomplete="off" placeholder="openai"
                            v-model="bgForm.model.provider">
                        </label>
                        <label class="st-field">
                          <span class="st-lbl">模型名称</span>
                          <input class="st-input" type="text" autocomplete="off" v-model="bgForm.model.model_id">
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
                          <input class="st-input" type="number" :min="p.min" :max="p.max" :step="p.step"
                            v-model="bgForm.model[p.key]">
                        </label>
                      </div>
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
                    <div class="st-grid">
                      <label class="st-field">
                        <span class="st-lbl">每日通知上限</span>
                        <input class="st-input" type="number" min="0" step="1" v-model="form.notifications.daily_cap">
                      </label>
                      <label class="st-field">
                        <span class="st-lbl">日志保留天数</span>
                        <input class="st-input" type="number" min="1" step="1" v-model="form.log.retention_days">
                      </label>
                      <label class="st-field">
                        <span class="st-lbl">日志级别</span>
                        <select class="st-input" v-model="form.log.level">
                          <option v-for="lv in LOG_LEVELS" :key="lv" :value="lv">{{ lv }}</option>
                        </select>
                      </label>
                    </div>
                    <label class="st-field">
                      <span class="st-lbl">隐私目录</span>
                      <div v-if="form.privacy.dirs.length" class="st-tags">
                        <span v-for="(d, i) in form.privacy.dirs" :key="d" class="st-tag">{{ d }}<button
                          class="st-tag-x" type="button" :aria-label="'移除隐私目录 ' + d" @click="removeDir(i)">×</button></span>
                      </div>
                      <input ref="dirInputRef" class="st-input" type="text" autocomplete="off"
                        placeholder="输入目录名，回车添加" v-model="dirDraft" @keydown="onDirKeydown">
                      <span class="st-hint">名单内目录的内容受隐私保护</span>
                    </label>
                  </div>
                </div>

                <!-- 保存全部（单一写面；revision 随行） -->
                <div class="st-foot">
                  <button class="st-save" type="button" :disabled="saving" @click="save">
                    {{ saving ? '保存中…' : '保存全部' }}
                  </button>
                </div>
              </div>
            </div>

            <!-- 页底静态说明（spec §6）：页面外段的手工维护口径（用户语言，Rule 2） -->
            <div class="st-footnote">
              网络与端口、数据目录、出网开关与写通道凭证等设置请手工编辑配置文件（重启后生效）。
            </div>
          </template>
      </div>
    </div>

    <!-- 关闭确认浮层（spec §6 三选，替代原生 confirm）：role=alertdialog；层级 = 设置浮层(81)
         之上、toast(100) 之下（style.css st-confirm 族）；折叠壳家族照 st-modal 纪律（visibility
         出 Tab 序 + 转场仅 opacity/transform + RM 两态同杀）；默认焦点 = 取消钮，Escape = 取消，
         设置浮层 inert 时自然 Tab 困于本层。保存并关闭失败（409/422）：本层收起、设置浮层不关、
         错误条照报（头部下方固定区）；放弃修改 = 重拉服务端真相后关闭 -->
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
