/* SettingsPage 组件：设置页（spec 2026-11-22 §6）——config.yaml 页面化编辑。
 * 整页模式照监控页先例：.mwrap/.m-scroll/.mpage 骨架 + mp-enter/mp-out 转场复用（style.css 动效层
 * 整族作用域于 .mwrap，本页零新增动效 CSS）；#settings-wrap id = app.js 互斥/转场选择器作用域
 * （两页同族类 .mwrap 并存，querySelector 类选择器会误选监控页）。
 * 契约（Task 7 交付面）：props 无；inject('state') + inject('api')（api.getSettings/putSettings
 *   非 2xx 不抛，返回 {status,data}，仅网络失败 throw）；
 *   emits ['back', 'open-doc']；挂载点 = app 模板 v-show="state.settingsPage"。
 * 互斥契约：文档区/监控页/设置页三态互斥（app 开页时收起其余两态；「← 返回文档」不自动重开面板）。
 * 本文件 = Task 8 + Task 9：分段导航 + 「主 agent」/「后台 agent」/「杂项」表单 + 共用保存流程。
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
 *    model.api_key（掩码形态）未触碰一律剥除（缺省=保持，同①）。Task 9 起条目可编辑：
 *    编辑面只建模表单可见键，raw 副本中的其余键（escalation 等）保存时原样合并回条目。
 * ③ 空标量省略键（不发 null——加载器 int(null) 即炸→422；省略 = 加载器缺省/服务端保持原值，
 *    两种后端解读下都零崩溃）；显式语义各有专职：tools null=全量、privacy.dirs []=空名单、
 *    ai null=删段、api_key ""=清除。
 * ④ GET ai 段缺位 ⇒ 表单照常空白渲染（页面可新建段）：用户整段零改动 ⇒ payload ai:null（幂等删段）；
 *    改动过任何字段 ⇒ 按填写内容整段提交。
 * 文案纪律（AGENTS Frontend Rule 2）：正文零字段名/端点路径/设计概念名；
 * applied/restart_needed 后端键经 FIELD_CN 翻成用户语言，未知键兜底「相关设置」。
 */
const { ref, computed, watch, nextTick, inject, onBeforeUnmount } = Vue

// 分段导航（桌面左列锚点 / ≤639 分段钮同一数据源）
const SECTIONS = [
  { id: 'ai', label: '主 agent' },
  { id: 'bg', label: '后台 agent' },
  { id: 'misc', label: '杂项' }
]

// 主 agent 数值参数行：字段键 + 完整 label（label 即用户语言，min/step 进原生 input 属性）
const AI_PARAMS = [
  { key: 'max_tokens', label: '最大输出长度', min: 1, step: 1 },
  { key: 'temperature', label: '生成温度', min: 0, max: 2, step: 0.1 },
  { key: 'context_window', label: '上下文窗口', min: 1, step: 1 },
  { key: 'max_rounds', label: '最大轮次', min: 1, step: 1 },
  { key: 'read_doc_max_chars', label: '单文档读取上限（字符）', min: 1, step: 1 },
  { key: 'confirm_timeout', label: '操作确认超时（秒）', min: 1, step: 1 }
]
const LOG_LEVELS = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']

// ─── 后台 agent 段（Task 9）───
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

// applied / restart_needed 后端键 → 用户语言（未知键兜底，绝不在正文回显原始字段名）
const FIELD_CN = {
  ai: '主 agent 配置',
  background_agents: '后台 agent',
  notifications: '站内通知',
  'notifications.daily_cap': '每日通知上限',
  privacy: '隐私目录',
  'privacy.dirs': '隐私目录',
  log: '日志',
  'log.level': '日志级别',
  'log.dir': '日志存放位置',
  'log.retention_days': '日志保留天数'
}
const cnField = k => FIELD_CN[k] || '相关设置'

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
  emits: ['back', 'open-doc'],
  setup(props, { emit }) {
    const state = inject('state')
    const api = inject('api')
    const wrapRef = ref(null)
    // ─── 进入动效（监控页同款溶解节奏）：v-show display:none→block + mp-enter 类每次打开重放；
    //     reduced-motion 降级由 style.css 既有 .mwrap.mp-enter 归零规则覆盖，JS 无需分支 ───
    let enterT = null
    let firstRendered = false
    watch(
      () => state.settingsPage,
      async (open) => {
        if (!open) return
        const el = wrapRef.value
        if (!el) return
        if (firstRendered) await nextTick() // 上一轮 v-show 隐藏态已提交 → 重放入场
        else firstRendered = true
        el.classList.remove('mp-enter')
        void el.offsetWidth // 强制 reflow：连开两次也重放入场
        el.classList.add('mp-enter')
        clearTimeout(enterT)
        enterT = setTimeout(() => el.classList.remove('mp-enter'), 950) // 序列最长 ~560ms，留尾
      }
    )

    function back() { emit('back') }

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
    }

    // 打开页面首拉（深链/常开恢复均经此；此后 revision/409 机制负责新鲜度，重开不重复拉）
    watch(
      () => state.settingsPage,
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

    // ═══ 保存（单一「保存全部」；三态反馈：200 生效报告 / 409 冲突重载 / 422 页顶告警，
    //     外加 500 {saved:true} 的 apply 失败窗口如实报——spec §4.2 步 6）───
    const saving = ref(false)
    const report = ref(null)  // {kind:'ok'|'conflict'|'invalid'|'savedapply'|'error', ...}
    const appliedText = computed(() => {
      const r = report.value
      return r && r.applied && r.applied.length
        ? r.applied.map(cnField).join(' · ')
        : '相关设置'
    })
    const restartText = computed(() => {
      const r = report.value
      return (r && r.restart ? r.restart.map(cnField).join('、') : '') + ' 需重启服务后生效'
    })

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

    async function save() {
      if (saving.value || !form.value) return
      saving.value = true
      report.value = null
      try {
        const res = await api.putSettings(buildPayload())
        if (res.status === 200) {
          const d = res.data || {}
          report.value = { kind: 'ok', applied: d.applied || [], restart: d.restart_needed || [] }
          // 保存成功后重载 GET：按新 revision 回填全表单（与后端真相一致，api_key 掩码同步刷新）
          await load()
        } else if (res.status === 409) {
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
      wrapRef, back,
      SECTIONS, AI_PARAMS, LOG_LEVELS, section,
      loading, loadError, form, meta, load,
      apiKeySet, apiKeyMask, apiKeyTouched, apiKeyInput, apiKeyHint, clearApiKey,
      toolsMode, setToolsMode, toggleTool,
      dirDraft, dirInputRef, addDir, removeDir, onDirKeydown,
      // 后台 agent 段
      BG_TRIG_OPS, BG_TRIG_OP_CN, BG_TRIG_SOURCES, BG_SCHED, BG_TRIG_NUMS, BG_MODEL_NUMS,
      bgEditing, bgForm, bgDelArmed, bgBadge, bgTitle, bgRenamed, bgConflict, bgKeyHint,
      bgSelect, bgAdd, onBgDelete, bgToggleTrigger, bgToggleEvent, bgToggleTool, bgToggleVerb, bgClearKey,
      saving, save, report, appliedText, restartText, reloadFromServer
    }
  },
  template: `
    <div ref="wrapRef" class="mwrap" id="settings-wrap">
      <div class="m-scroll">
        <div class="mpage">
          <div class="mp-head">
            <!-- 单出口：回文档视图（三态互斥；面板不自动重开，重入走 rail【记忆】） -->
            <button class="mp-back" type="button" title="返回文档视图" aria-label="返回文档视图" @click="back">
              <svg aria-hidden="true" viewBox="0 0 24 24"><path d="M15 5l-7 7 7 7"/></svg>返回文档</button>
            <h1 class="mp-title">设置</h1>
            <div class="mp-meta">保存即校验、即落盘</div>
          </div>

          <!-- 反馈条（aria-live=polite：保存结果不抢读屏）：200 生效报告 / 409 冲突 / 422 告警 -->
          <div aria-live="polite">
            <div v-if="report && report.kind === 'ok'" class="st-banner ok">
              已保存 · 已生效：{{ appliedText }}<template v-if="report.restart.length">；{{ restartText }}</template>
            </div>
            <div v-else-if="report && report.kind === 'conflict'" class="st-banner warn">
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
                <!-- ═══ 主 agent ═══ -->
                <div v-show="section === 'ai'" class="mcard">
                  <div class="mc-h"><span class="mc-t">主 agent</span></div>
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
                          <button v-for="t in meta.tools" :key="t" class="st-chip" type="button"
                            :class="{ on: toolsSel.includes(t) }" @click="toggleTool(t)">{{ t }}</button>
                        </div>
                        <div v-else class="zline">工具清单暂不可用</div>
                        <span class="st-hint">点选启用；全不选 = 主 agent 无工具</span>
                      </template>
                      <span v-else class="st-hint">当前开放全部工具</span>
                    </div>
                  </div>
                </div>

                <!-- ═══ 后台 agent ═══ -->
                <div v-show="section === 'bg'">
                  <div class="mcard">
                    <div class="mc-h"><span class="mc-t">后台 agent</span></div>
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
                        <span class="st-badge" :class="bgBadge(e).cls" :title="bgBadge(e).tip">{{ bgBadge(e).label }}</span>
                        <button class="st-toggle" :class="{ on: e.enabled }" type="button" role="switch"
                          :aria-checked="e.enabled" :aria-label="(e.enabled ? '停用 ' : '启用 ') + bgTitle(e)"
                          @click="e.enabled = !e.enabled">{{ e.enabled ? '启用' : '停用' }}</button>
                      </div>
                    </div>
                    <div v-else class="zline">暂无后台 agent，可点「添加后台 agent」新建</div>
                    <div class="st-bg-foot">
                      <button class="st-add" type="button" @click="bgAdd()">添加后台 agent</button>
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
                            placeholder="英文短名，保存后作为该任务的身份" v-model="bgForm.alias">
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
                          <button v-for="t in meta.tools" :key="t" class="st-chip" type="button"
                            :class="{ on: bgForm.tools.includes(t) }"
                            @click="bgToggleTool(bgEditing, t)">{{ t }}</button>
                        </div>
                        <div v-else class="zline">工具清单暂不可用</div>
                        <span v-if="bgConflict(bgForm)" class="st-warn-inline">提案与直写工具不可同时启用，保存会被拒绝</span>
                        <template v-if="bgForm.tools.includes('propose')">
                          <div v-if="meta.verbs.length" class="st-chips">
                            <button v-for="v in meta.verbs" :key="v" class="st-chip" type="button"
                              :class="{ on: bgForm.verbs.includes(v) }"
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
                      <span class="st-hint">执行器条目请手改 yaml</span>
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
                  <div class="mc-h"><span class="mc-t">杂项</span></div>
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

            <!-- 页底静态说明（spec §6）：页面外段的手工维护口径 + 安全承重字段刻意不页面化 -->
            <div class="st-footnote">
              网络监听地址与端口、数据目录、出网开关与写通道凭证等设置不在本页，需手工编辑配置文件并重启服务——
              这些安全承重字段刻意保持手改。
            </div>
          </template>
        </div>
      </div>
    </div>
  `
}
