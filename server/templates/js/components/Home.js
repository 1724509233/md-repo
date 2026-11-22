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
      const n = actions.value ? actions.value.unseen_agent_changes
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
      if (a.unseen_agent_changes > 0) rows.push({ k: 'u', text: C.unseen(a.unseen_agent_changes), link: C.unseenLink, act: scrollToG2 })
      if (a.conflicts > 0) {
        const cp = (state.files || []).find((f) => f.path && f.path.includes('.conflict-'))
        rows.push({ k: 'c', text: C.conflicts(a.conflicts), link: C.conflictsLink, act: () => { if (cp) emit('open-doc', cp.path) } })
      }
      if (a.trash_batches > 0) rows.push({ k: 't', text: C.trash(a.trash_batches), link: C.trashLink, act: () => emit('open-trash') })
      return rows
    })

    function scrollToG2() {
      const el = document.querySelector('#hb-g2')
      if (!el) return
      const rm = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches
      el.scrollIntoView({ behavior: rm ? 'auto' : 'smooth', block: 'start' })
    }

    /* ── 拉数（每次进入首页；监控退场经 homeRefreshSeq；无轮询，spec §6.2）── */
    function load() {
      let presence = boot.presence
      try {
        const raw = localStorage.getItem(LS_KEYS.lastPresence)
        if (raw !== null) presence = Number(raw) || null
      } catch (e) { /* 保持 boot 读数 */ }
      const since = Math.max(presence || 0, Date.now() - 7 * 86400e3)
      let seen = 0
      try { seen = Number(localStorage.getItem(LS_KEYS.seenSeq)) || 0 } catch (e) { seen = 0 }
      api.getHome(since, seen).then((d) => {
        data.value = d
        loadFailed.value = false
        expanded.value = d.my_days.length ? new Set([d.my_days[0].date]) : new Set()
        // 已读游标（§6.3）：G2 渲出非空内容才落笔——「看过」即看过，免疫窗钳制。
        // S0/S1 收束态列表根本没露脸，「看过」不成立，绝不推进游标（终审 M2）。
        if (d.system_groups.length && !shortStay.value) {
          const maxSeq = Math.max(...d.system_groups.flatMap((g) => g.ops.map((op) => op.seq)))
          try { localStorage.setItem(LS_KEYS.seenSeq, String(maxSeq)) } catch (e) { /* 无存储：下次重报，设计内 */ }
        }
      }).catch(() => { loadFailed.value = true })
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
      state, HOME_COPY, groupHead, data, loadFailed, groups, myDays, g4Rows, expanded,
      shortStay, g1, g2Title, g3Title, summary, summing, sumNote,
      hm, md, verb, daySummary, toggleDay, openPath, genSummary, load,
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
          <!-- G2 您不在时，我做了什么（S0/S1 短窗收束：不渲段题与卡片，§G2） -->
          <section class="hb-sec" id="hb-g2">
            <p v-if="shortStay" class="hb-quiet">{{ HOME_COPY.g2.short }}</p>
            <template v-else>
              <h2 class="hb-sec-title">{{ g2Title }}</h2>
              <p v-if="!groups.length" class="hb-quiet">{{ HOME_COPY.g2.quiet }}</p>
              <div v-else class="hb-grp" v-for="g in groups" :key="g.actor + '/' + (g.alias || '')">
                <p class="hb-grp-head">{{ groupHead(g) }}</p>
                <div class="hb-grp-row" v-for="op in g.ops" :key="op.seq">
                  <span class="t">{{ hm(op.ts) }}</span><span class="act">{{ verb(op.action) }}</span><span class="mid">·</span>
                  <a class="path" href="#" @click.prevent="openPath(op)" :title="op.path">{{ op.path }}</a>
                </div>
              </div>
            </template>
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
              <div v-if="expanded.has(d.date)" class="hb-day-ops">
                <div class="hb-grp-row" v-for="op in d.ops" :key="op.seq">
                  <span class="t">{{ hm(op.ts) }}</span><span class="act">{{ verb(op.action) }}</span><span class="mid">·</span>
                  <a class="path" href="#" @click.prevent="openPath(op)" :title="op.path">{{ op.path }}</a>
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
                <a class="hb-g4-link" href="#" @click.prevent="r.act()">{{ r.link }}</a>
              </div>
            </div>
          </section>
        </template>

        <p class="hb-foot">{{ HOME_COPY.foot }}</p>
      </div>
    </div>
  `,
}
