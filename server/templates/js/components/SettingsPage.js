/* SettingsPage 组件：设置页（spec 2026-11-22 §6）——config.yaml 页面化编辑。
 * 整页模式照监控页先例：.mwrap/.m-scroll/.mpage 骨架 + mp-enter/mp-out 转场复用（style.css 动效层
 * 整族作用域于 .mwrap，本页零新增动效 CSS）；#settings-wrap id = app.js 互斥/转场选择器作用域
 * （两页同族类 .mwrap 并存，querySelector 类选择器会误选监控页）。
 * 契约（Task 7 交付面）：props 无；inject('state') + inject('api')（api.getSettings/putSettings
 *   非 2xx 不抛，返回 {status,data}，仅网络失败 throw）；
 *   emits ['back', 'open-doc']；挂载点 = app 模板 v-show="state.settingsPage"。
 * 互斥契约：文档区/监控页/设置页三态互斥（app 开页时收起其余两态；「← 返回文档」不自动重开面板）。
 * 本文件 = Task 8：分段导航 + 「主 agent」+「杂项」表单；「后台 agent」段由 Task 9 在 SECTIONS
 * 的 ai 与 misc 之间插入（保存流程本 Task 建好、Task 9 共用）。
 *
 * payload 构造契约（spec §4.1/§4.2，buildPayload 是本页唯一写面——四条铁律）：
 * ① api_key 三态：未触碰（touched 集合无此键）⇒ payload 省略该键（服务端回填原明文）；
 *    「清除」小钮 ⇒ 发 ""（显式清空）；输入新值 ⇒ 发新值。GET 回的 {set,mask} 掩码形态永不回传。
 * ② background_agents 原样直通——PUT 按 alias 对账、payload 缺席即删条目，本 Task 不编辑此段
 *    也必须回传；条目内 model.api_key（同为掩码形态）一律剥除（缺省=保持，同①）。
 * ③ 空标量省略键（不发 null——加载器 int(null) 即炸→422；省略 = 加载器缺省/服务端保持原值，
 *    两种后端解读下都零崩溃）；显式语义各有专职：tools null=全量、privacy.dirs []=空名单、
 *    ai null=删段、api_key ""=清除。
 * ④ GET ai 段缺位 ⇒ 表单照常空白渲染（页面可新建段）：用户整段零改动 ⇒ payload ai:null（幂等删段）；
 *    改动过任何字段 ⇒ 按填写内容整段提交。
 * 文案纪律（AGENTS Frontend Rule 2）：正文零字段名/端点路径/设计概念名；
 * applied/restart_needed 后端键经 FIELD_CN 翻成用户语言，未知键兜底「相关设置」。
 */
const { ref, computed, watch, nextTick, inject } = Vue

// 分段导航（桌面左列锚点 / ≤639 分段钮同一数据源；Task 9 在 ai 与 misc 之间插 background agents）
const SECTIONS = [
  { id: 'ai', label: '主 agent' },
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
        // 直通段（Task 9 接管编辑）：深拷贝防编辑污染，PUT 原样回传防 alias 对账误删
        background_agents: JSON.parse(JSON.stringify(d.background_agents || [])),
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
      // ── 直通段（铁律②）：条目回传但剥掩码密钥（缺省=保持原明文）──
      p.background_agents = (f.background_agents || []).map(e => {
        const c = JSON.parse(JSON.stringify(e))
        if (c && c.model && typeof c.model === 'object') delete c.model.api_key
        return c
      })
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

    return {
      wrapRef, back,
      SECTIONS, AI_PARAMS, LOG_LEVELS, section,
      loading, loadError, form, meta, load,
      apiKeySet, apiKeyMask, apiKeyTouched, apiKeyInput, apiKeyHint, clearApiKey,
      toolsMode, setToolsMode, toggleTool,
      dirDraft, dirInputRef, addDir, removeDir, onDirKeydown,
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
