/* Home 组件（首页·管家日报，spec 2026-11-22-home-butler-landing §6）：
   G1 分级一句话（可被 ✨一句话整句替换）/ G2 缺席窗信笺分组 / G3 固定 3d 足迹 /
   G4 规则行动清单。铁律：G2/G3/G4 全结构化渲染、LLM 无渲染权（G1 的 LLM 文本
   也只走 Vue 插值转义，全程不碰内 HTML）；数字不经模型。文案单点 = utils/homeVoice.js。 */
const { ref, computed, inject, onMounted, watch } = Vue
import {
  LS_KEYS, HOME_COPY, tierOf, greetingOf, absentPhrase, cnCount,
  groupHead, OP_VERB, OP_SUM, OP_ORDER,
} from '../utils/homeVoice.js'

/* S0 判据：运行时标志（spec §6.1）——本会话首次 mount 后置真；「离开」的定义
   收紧为两次 boot 之间的空窗。模块级：跨 Home 挂载/卸载存续，随刷新归零。 */
let bootedOnce = false

export const Home = {
  name: 'Home',
  emits: ['open-doc', 'open-memory', 'open-trash'],
  setup(props, { emit }) {
    const state = inject('state')
    const api = inject('api')

    const data = ref(null)
    const loadFailed = ref(false)
    const expanded = ref(new Set())
    const summary = ref('')
    const summing = ref(false)
    const sumNote = ref('')
    /* 游标 ts 化（2026-11-23）：renderSeenTs = 本次渲染的未看对比基准（请求携带的
       旧 seen_ts，epoch 秒；null=本次全不算未看）。localStorage 已先推进，未看标记
       用这个旧值判——同次渲染内保持，下次自然消失。baseSinceMs = 缺席窗起点（ms），
       回看「收起」要回它；lookback = 回看窗开着。 */
    const renderSeenTs = ref(null)
    const baseSinceMs = ref(0)
    const lookback = ref(false)

    /* ── 分级（进首页算一次；spec §6.3）──
       lastPresence: undefined=存储不可读 / null=键不存在（真首次）/ number=上次在场 ms */
    const boot = (() => {
      const now = Date.now()
      let presence
      try {
        const raw = localStorage.getItem(LS_KEYS.lastPresence)
        presence = raw === null ? null : (Number(raw) || null)
      } catch (e) { presence = undefined }
      const tier = tierOf({ bootedOnce, lastPresence: presence, now })
      return { tier, presence, now }
    })()
    bootedOnce = true   // 首次 mount 即置真（S0 从此成立）

    const shortStay = computed(() => boot.tier === 'S0' || boot.tier === 'S1')
    const actions = computed(() => (data.value && data.value.actions) || null)
    const groups = computed(() => (data.value && data.value.system_groups) || [])
    const myDays = computed(() => (data.value && data.value.my_days) || [])
    const over7d = computed(() => !!data.value &&
      (data.value.window.until - data.value.window.since) >= 7 * 86400)

    /* ── G1 组装（hi = 段落数组，a=true 段落黄铜；LLM 摘要整句替换）── */
    const g1 = computed(() => {
      if (summary.value) return { hi: [{ t: summary.value }], said: [] }
      const g = greetingOf(boot.now)
      const C = HOME_COPY.g1
      const n = actions.value ? actions.value.unseen_agent_docs
                             + actions.value.pending_proposals
                             + actions.value.conflicts
                             + actions.value.trash_batches : 0
      switch (boot.tier) {
        case 'S0': return { hi: [{ t: n ? C.s0Has : C.s0Quiet }], said: [] }
        case 'S1': return { hi: [{ t: `${g}。${C.s1}` }], said: [] }
        case 'nostorage': return { hi: [{ t: `${g}。` }], said: [C.nostorageSaid] }
        case 'first': return { hi: [{ t: `${g}。` }], said: [C.firstSaid] }
      }
      // S2–S4：缺席叙事（数字只在 ≥15min 出现）
      const absentSec = boot.presence ? (boot.now - boot.presence) / 1000 : 0
      const phrase = absentPhrase(absentSec)
      if (boot.tier === 'S2' && phrase) {
        if (!n) return { hi: [{ t: `${g}。您离开的` }, { t: phrase, a: true }, { t: C.s2QuietTail }], said: [] }
        const h = Math.max(1, Math.floor(absentSec / 3600))
        return { hi: [{ t: `${g}。${C.s2HasHead}` }, { t: `${h} 个小时`, a: true },
                      { t: `${C.s2HasTail}${n}${C.s2HasTail2}` }], said: [] }
      }
      if (phrase) return { hi: [{ t: `${g}。${C.longHead}` }, { t: phrase, a: true }, { t: C.longTail }],
        said: n ? [`${C.longSaid}${C.longSaidHasHead}${cnCount(n)}${C.longSaidHasTail}`] : [C.longSaid] }
      return { hi: [{ t: `${g}。` }], said: [C.nostorageSaid] }   // 理论不可达兜底
    })

    /* ── G2/G3/G4 展示层 ── */
    const g2Title = computed(() => over7d.value ? HOME_COPY.g2.title7d : HOME_COPY.g2.title)
    const g3Title = computed(() => shortStay.value ? HOME_COPY.g3.titleShort : HOME_COPY.g3.title)

    /* 回看开关渲染条件：缺席窗比 3 天窄才出现。「现在」用 payload.window.until
       （服务端时钟），缺席窗 since 用 baseSinceMs——扩窗重取后数据里的 since 已是
       扩窗值，条件仍按缺席窗判，钮才恒可达「收起」态。 */
    const canLookback = computed(() => !!data.value &&
      baseSinceMs.value / 1000 > data.value.window.until - 3 * 86400)

    /* ── G2 渲染结构：actor 桶 → 日段 → 文件行（2026-11-23 重构波）──
       日段 = 服务器本地日期（单机 LAN 单时区，前端 Date 与服务端 localtime 同区）
       降序；段内按 path 归并（ops 已 ts 降序 → 日段与行天然新在前）。 */
    function localDay(tsSec) {
      const d = new Date(tsSec * 1000)
      const p = (n) => String(n).padStart(2, '0')
      return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate())
    }
    const CHIP_ORDER = ['新建', '编辑', '移动', '删除']
    const isDel = (a) => a === 'deleted' || a === 'delete'
    const g2Groups = computed(() => {
      if (!data.value) return []
      const until = data.value.window ? data.value.window.until : Date.now() / 1000
      const seen = renderSeenTs.value   // 旧游标：未看点用推进前基准，别用推进后的值
      const today = localDay(until)
      const yest = localDay(until - 86400)
      return groups.value.map((g) => {
        const byDay = new Map()
        for (const op of g.ops) {
          const date = localDay(op.ts)
          let day = byDay.get(date)
          if (!day) { day = { date, rows: new Map() }; byDay.set(date, day) }
          let row = day.rows.get(op.path)
          if (!row) {
            const i = op.path.lastIndexOf('/')
            row = { path: op.path, name: i < 0 ? op.path : op.path.slice(i + 1),
                    dir: i < 0 ? '' : op.path.slice(0, i), ops: [] }
            day.rows.set(op.path, row)
          }
          row.ops.push(op)
        }
        const days = [...byDay.values()].map((day) => ({
          date: day.date,
          title: day.date === today ? '今天' : day.date === yest ? '昨天' : md(day.date),
          rows: [...day.rows.values()].map((r) => {
            const cnt = {}
            for (const op of r.ops) {
              const label = OP_SUM[op.action] || op.action
              cnt[label] = (cnt[label] || 0) + 1
            }
            return {
              path: r.path, name: r.name, dir: r.dir,
              chips: CHIP_ORDER.filter((c) => cnt[c]).map((c) => `${c}×${cnt[c]}`),
              lastTs: r.ops[0].ts,   // ops ts 降序 → 首元素即末次
              unseen: seen !== null && r.ops.some((op) => op.ts > seen),
              dead: r.ops.every((op) => isDel(op.action)),   // 全删行点了只撞 404 幽灵态
            }
          }).sort((a, b) => (b.unseen ? 1 : 0) - (a.unseen ? 1 : 0)),   // 未看优先置顶（稳定序，段内仍新在前）——折叠封顶下未读点绝不藏进「还有 N 篇」
        }))
        return { actor: g.actor, alias: g.alias, days }
      })
    })
    /* 日段行数封顶折叠（2026-11-23 噪声治理，同 my_days「紧凑+展开」语法）：
       超 G2_DAY_CAP 显前 8（未看已置顶）+「还有 N 篇……展开›」。只折展示，
       计数/未看/G4 谓词全不动；展开态仅存内存，重取（load/回看）回默认。 */
    const G2_DAY_CAP = 8
    const expandedG2 = ref(new Set())
    function g2DayKey(g, d) { return g.actor + '/' + (g.alias || '') + '@' + d.date }
    function g2Base(d) { return d.rows.length <= G2_DAY_CAP ? d.rows : d.rows.slice(0, G2_DAY_CAP) }
    function g2Fold(d) { return d.rows.slice(G2_DAY_CAP) }   // 折叠区内容（恒在 DOM，靠 0fr 收高度）
    function toggleG2Day(key) {
      const s = new Set(expandedG2.value)
      if (s.has(key)) s.delete(key); else s.add(key)
      expandedG2.value = s
    }
    function openRow(r) {
      if (r.dead) return   // openPath 守卫精神：组内全 delete 不可点
      emit('open-doc', r.path)
    }

    function hm(ts) {
      const d = new Date(ts * 1000)
      return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0')
    }
    function md(dateStr) { return String(dateStr).slice(5) }   // YYYY-MM-DD → MM-DD
    function verb(a) { return OP_VERB[a] || a }
    function daySummary(d) {
      const parts = OP_ORDER.filter((op) => d.counts[op])
        .map((op) => `${OP_SUM[op]} ${d.counts[op]}`)
      return parts.join(' · ')
    }
    function toggleDay(date) {
      const s = new Set(expanded.value)
      s.has(date) ? s.delete(date) : s.add(date)
      expanded.value = s
    }
    function openPath(op) {
      if (op.action === 'delete') return   // 已删路径点了只会撞 404 幽灵态
      emit('open-doc', op.path)
    }

    const g4Rows = computed(() => {
      const a = actions.value
      if (!a) return []
      const C = HOME_COPY.g4
      const rows = []
      if (a.pending_proposals > 0) rows.push({ k: 'p', text: C.proposals(a.pending_proposals), link: C.proposalsLink, act: () => emit('open-memory') })
      if (a.unseen_agent_docs > 0) rows.push({ k: 'u', text: C.unseen(a.unseen_agent_docs), link: C.unseenLink, act: scrollToUnseen })
      if (a.conflicts > 0) {
        // 优先服务端 conflict_path（计数口径=全 files 表，含机器区）；可见面兜底仅防旧 payload
        const cp = a.conflict_path || (state.files || []).find((f) => f.path && f.path.includes('.conflict-'))
        rows.push({ k: 'c', text: C.conflicts(a.conflicts), link: C.conflictsLink, act: () => { const p = cp && (cp.path || cp); if (p) emit('open-doc', p) } })
      }
      if (a.trash_batches > 0) rows.push({ k: 't', text: C.trash(a.trash_batches), link: C.trashLink, act: () => emit('open-trash') })
      return rows
    })

    function scrollToG2() {
      // 显式容器滚动（2026-11-23 修复落不了位）：消 scrollIntoView 多滚动祖先歧义
      const el = document.querySelector('#hb-g2')
      const shell = el && el.closest('.hb-shell')
      if (!el || !shell) return
      const rm = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches
      const top = shell.scrollTop + el.getBoundingClientRect().top - shell.getBoundingClientRect().top
      shell.scrollTo({ top: Math.max(0, top), behavior: rm ? 'auto' : 'smooth' })
    }

    function scrollToUnseen() {
      // 「看我改了什么」= 首条未看文件行（同族显式容器滚动，禁裸 scrollIntoView）；
      // 找不到目标退回段头滚动。落位后黄铜 8% 底 flash ~1.2s 淡出；
      // prefers-reduced-motion 下无动画、类都不加（等于直接清）。
      const dot = document.querySelector('#hb-g2 .hb-unseen-dot')
      const row = dot ? dot.closest('.hb-file-row') : null
      const g2 = document.querySelector('#hb-g2')
      const shell = g2 && g2.closest('.hb-shell')
      if (!row || !shell) { scrollToG2(); return }
      const rm = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches
      const top = shell.scrollTop + row.getBoundingClientRect().top - shell.getBoundingClientRect().top
      shell.scrollTo({ top: Math.max(0, top), behavior: rm ? 'auto' : 'smooth' })
      if (rm) return
      row.classList.add('hb-flash')
      window.setTimeout(() => row.classList.remove('hb-flash'), 1300)
    }

    /* ── 拉数（每次进入首页；监控退场经 homeRefreshSeq；无轮询，spec §6.2）── */
    function fetchHome(sinceMs, seenTs) {
      renderSeenTs.value = seenTs   // 本次渲染未看基准 = 请求携带的旧游标
      return api.getHome(sinceMs, seenTs).then((d) => {
        data.value = d
        loadFailed.value = false
        expanded.value = d.my_days.length ? new Set([d.my_days[0].date]) : new Set()
        expandedG2.value = new Set()   // 重取（含回看切换）→ 日段折叠回默认
        // 已读游标（§6.3，2026-11-23 换尺 ts）：G2 渲出非空内容才落笔——「看过」即
        // 看过，免疫窗钳制。推进值取 payload.window.until（服务端时钟，绝不用本地钟）。
        // S0/S1 收束态列表根本没露脸，「看过」不成立，绝不推进（终审 M2）。
        if (d.system_groups.length && !shortStay.value) {
          try { localStorage.setItem(LS_KEYS.seenTs, String(d.window.until)) } catch (e) { /* 无存储：下次重报，设计内 */ }
        }
      })
    }
    function load() {
      let presence = boot.presence
      try {
        const raw = localStorage.getItem(LS_KEYS.lastPresence)
        if (raw !== null) presence = Number(raw) || null
      } catch (e) { /* 保持 boot 读数 */ }
      const since = Math.max(presence || 0, Date.now() - 7 * 86400e3)
      baseSinceMs.value = since
      lookback.value = false
      let seenTs = null
      try {
        const raw = localStorage.getItem(LS_KEYS.seenTs)
        const v = raw === null ? NaN : Number(raw)
        seenTs = Number.isFinite(v) ? v : null   // 读不到 → 不带 seen_ts，服务端视为 null
      } catch (e) { seenTs = null }
      fetchHome(since, seenTs).catch(() => { loadFailed.value = true })
    }

    /* 回看开关：点开把窗扩到「缺席窗与前三天取早者」，seen_ts 沿用本次请求旧值
       （G4 计数不受扩窗影响：扩窗行天然 ts ≤ seenTs，服务端谓词自然排除）；
       收起回缺席窗 since。游标推进规则不变（fetchHome 内同一守卫）。 */
    function toggleLookback() {
      if (!data.value || !data.value.window) return
      const target = lookback.value
        ? baseSinceMs.value
        : Math.min(baseSinceMs.value, (data.value.window.until - 3 * 86400) * 1000)
      lookback.value = !lookback.value
      fetchHome(target, renderSeenTs.value).catch(() => { loadFailed.value = true })
    }

    /* ── ✨按需一句话（成功后整句替换；失败保分级文案）── */
    function genSummary() {
      if (summing.value) return
      summing.value = true
      sumNote.value = ''
      api.genHomeSummary().then((r) => {
        if (r.ok) { summary.value = r.text }
        else if (r.error === 'model_not_configured') sumNote.value = HOME_COPY.g1.noteNoModel
        else if (r.error === 'cooldown') sumNote.value = HOME_COPY.g1.noteCooldown
        else sumNote.value = HOME_COPY.g1.noteFailed
      }).catch(() => { sumNote.value = HOME_COPY.g1.noteFailed })
        .finally(() => { summing.value = false })
    }

    onMounted(load)
    watch(() => state.homeRefreshSeq, load)   // 监控页退场 → 重取（§6.3）

    return {
      state, HOME_COPY, groupHead, data, loadFailed, groups, g2Groups, myDays, g4Rows, expanded,
      shortStay, g1, g2Title, g3Title, summary, summing, sumNote,
      canLookback, lookback, toggleLookback,
      G2_DAY_CAP, expandedG2, g2Base, g2Fold, g2DayKey, toggleG2Day,
      hm, md, verb, daySummary, toggleDay, openPath, openRow, genSummary, load,
      scrollToG2, scrollToUnseen,
    }
  },
  template: `
    <div class="hb-shell">
      <div class="hb-home">

        <!-- G1 管家一句话 -->
        <section class="hb-g1">
          <h1 class="hb-g1-hi"><span v-for="(seg, i) in g1.hi" :key="i" :class="{ 'hb-absent': seg.a }">{{ seg.t }}</span></h1>
          <p v-for="(s, i) in g1.said" :key="i" class="hb-g1-said">{{ s }}</p>
          <div class="hb-g1-act">
            <button class="hb-g1-btn" :disabled="summing" @click="genSummary">
              <span class="hb-star">✦</span>{{ HOME_COPY.g1.btn }}
            </button>
            <span v-if="sumNote" class="hb-g1-note">{{ sumNote }}</span>
          </div>
        </section>

        <!-- 取数失败：G1 照常（纯前端合成），G2–G4 收进降级行（§6.2） -->
        <p v-if="loadFailed" class="hb-quiet">{{ HOME_COPY.loadFail }}
          <a class="hb-g4-link" href="#" @click.prevent="load">{{ HOME_COPY.retry }} ›</a></p>

        <template v-else-if="data">
          <!-- G2 您不在时，我做了什么（S0/S1 短窗收束：不渲段题与卡片，§G2；
               收束态句尾同挂回看钮——回看不分离开时长；点开即展开完整结构。
               非收束态：actor 桶 → 日段（今天/昨天/MM-DD 降序）→ 文件行（path 归并） -->
          <section class="hb-sec" id="hb-g2">
            <p v-if="shortStay && !lookback" class="hb-quiet">{{ HOME_COPY.g2.short }}<button
               v-if="canLookback" class="hb-lookback" @click="toggleLookback">{{ HOME_COPY.g2.lookback }}</button></p>
            <!-- 换窗入场（2026-11-23）：回看展开/收起整段 key 变化重建，段题+列表一体淡入上浮；
                 同窗周期刷新 key 不变 → 不重播 -->
            <div v-else class="hb-win" :key="lookback ? 'lb' : 'base'">
              <div class="hb-sec-head">
                <h2 class="hb-sec-title">{{ lookback ? HOME_COPY.g2.lookbackTitle : g2Title }}</h2>
                <button v-if="canLookback" class="hb-lookback" @click="toggleLookback">
                  {{ lookback ? HOME_COPY.g2.lookbackOff : HOME_COPY.g2.lookback }}
                </button>
              </div>
              <p v-if="!g2Groups.length" class="hb-quiet">{{ HOME_COPY.g2.quiet }}</p>
              <div v-else class="hb-grp" v-for="g in g2Groups" :key="g.actor + '/' + (g.alias || '')">
                <p class="hb-grp-head">{{ groupHead(g) }}</p>
                <div class="hb-g2-day" v-for="d in g.days" :key="d.date">
                  <p class="hb-g2-day-head">{{ d.title }}</p>
                  <div class="hb-file-row" :class="{ 'is-dead': r.dead }" v-for="r in g2Base(d)" :key="r.path"
                       :title="r.path" @click="openRow(r)">
                    <span v-if="r.unseen" class="hb-unseen-dot"></span>
                    <span class="fname">{{ r.name }}</span>
                    <span v-if="r.dir" class="fdir">{{ r.dir }}/</span>
                    <span class="hb-chip" v-for="c in r.chips" :key="c">{{ c }}</span>
                    <span class="t">{{ hm(r.lastTs) }}</span>
                  </div>
                  <!-- 折叠区（2026-11-23 动画）：grid 0fr⇄1fr 纯 CSS 高度过渡，零 JS 测高；
                       行淡入+3px 上浮；reduced-motion 全关。折叠行恒在 DOM，0fr 高度即 0 命中区 -->
                  <div v-if="d.rows.length > G2_DAY_CAP" class="hb-g2-fold"
                       :class="{ 'is-open': expandedG2.has(g2DayKey(g, d)) }">
                    <div class="hb-g2-fold-inner">
                      <div class="hb-file-row" :class="{ 'is-dead': r.dead }" v-for="r in g2Fold(d)" :key="r.path"
                           :title="r.path" @click="openRow(r)">
                        <span v-if="r.unseen" class="hb-unseen-dot"></span>
                        <span class="fname">{{ r.name }}</span>
                        <span v-if="r.dir" class="fdir">{{ r.dir }}/</span>
                        <span class="hb-chip" v-for="c in r.chips" :key="c">{{ c }}</span>
                        <span class="t">{{ hm(r.lastTs) }}</span>
                      </div>
                    </div>
                  </div>
                  <p v-if="d.rows.length > G2_DAY_CAP" class="hb-g2-more">
                    <a href="#" @click.prevent="toggleG2Day(g2DayKey(g, d))">{{
                      expandedG2.has(g2DayKey(g, d)) ? HOME_COPY.g2.collapseMore
                        : HOME_COPY.g2.more(d.rows.length - G2_DAY_CAP) }}</a>
                  </p>
                </div>
              </div>
            </div>
          </section>

          <!-- G3 您上次离开前做到哪（固定 3d 窗，与缺席窗解耦；只展开最近一天） -->
          <section class="hb-sec">
            <h2 class="hb-sec-title">{{ g3Title }}</h2>
            <p v-if="!myDays.length" class="hb-quiet">{{ HOME_COPY.g3.quiet }}</p>
            <div v-else class="hb-day" v-for="d in myDays" :key="d.date">
              <span class="d">{{ md(d.date) }}</span>
              <span class="sum">{{ daySummary(d) }}</span>
              <a v-if="d.ops.length" class="expand" href="#" @click.prevent="toggleDay(d.date)">
                {{ expanded.has(d.date) ? HOME_COPY.g3.collapse : HOME_COPY.g3.expand }}
              </a>
              <!-- G3 同语法折叠（2026-11-23）：grid 0fr⇄1fr，行淡入；默认展开最近一天（类恒挂不重播动画） -->
              <div v-if="d.ops.length" class="hb-day-fold" :class="{ 'is-open': expanded.has(d.date) }">
                <div class="hb-day-fold-inner">
                  <div class="hb-day-ops">
                    <div class="hb-grp-row" v-for="op in d.ops" :key="op.seq">
                      <span class="t">{{ hm(op.ts) }}</span><span class="act">{{ verb(op.action) }}</span><span class="mid">·</span>
                      <a class="path" href="#" @click.prevent="openPath(op)" :title="op.path">{{ op.path }}</a>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </section>

          <!-- G4 我做不了主的事（整页唯一盒子区；数字全结构化，不经 LLM） -->
          <section class="hb-sec">
            <h2 class="hb-sec-title">{{ HOME_COPY.g4.title }}</h2>
            <p v-if="!g4Rows.length" class="hb-quiet">{{ HOME_COPY.g4.quiet }}</p>
            <div v-else class="hb-g4">
              <div class="hb-g4-row" v-for="r in g4Rows" :key="r.k">
                <span class="hb-g4-txt">{{ r.text }}</span>
                <!-- 回收站行经 openTrashFace 开抽屉（恒开）：入口级豁免标记登记进 app.js
                     抽屉外点守卫链，同点外点链不得吞开（data-notif-toggle 先例）。 -->
                <a class="hb-g4-link" href="#" :data-drawer-entry="r.k === 't' ? '1' : null"
                   @click.prevent="r.act()">{{ r.link }}</a>
              </div>
            </div>
          </section>
        </template>

        <p class="hb-foot">{{ HOME_COPY.foot }}</p>
      </div>
    </div>
  `,
}
