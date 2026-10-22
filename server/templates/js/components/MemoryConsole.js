/* MemoryConsole 组件：300px 记忆控制台面板（设计稿 design/memory-console-design.html，spec 2026-10-21 §7）
 * 两个面：审查队列（队列卡片 + 脉搏：九态 / 触发 X/Y / 召回需求）
 *         终审详情（五段材料按判断顺序 + 粘性动作行 + 拒绝理由 + 确认之后子视图）
 * 宪法：console 只移状态（adopt/reject 走状态机，actor = human）；文件由确定性执行器落地。
 * 数据：state.memoryStatus / state.memoryProposals 由 app 30s 轮询注入，本组件不自建轮询；
 *       内容摘录经 GET /api/docs/{relpath} 按需读取（frontmatter 剥离后取 body）。
 */
const { ref, computed, watch, inject, nextTick } = Vue
import { api } from '../api.js'

/* 类型 chip 图标（单色线条，stroke: currentColor；未知类型回退时钟） */
const TYPE_ICONS = {
  entity_index: '<svg viewBox="0 0 24 24"><path d="M4 6h16M4 12h16M4 18h10"/></svg>',
  link: '<svg viewBox="0 0 24 24"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>',
  summary: '<svg viewBox="0 0 24 24"><path d="M8 5h11M8 12h11M8 19h11"/><path d="M3.5 5l1 1 2-2M3.5 12l1 1 2-2M3.5 19l1 1 2-2"/></svg>',
  correction: '<svg viewBox="0 0 24 24"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>',
  reminder: '<svg viewBox="0 0 24 24"><path d="M6 9a6 6 0 1 1 12 0c0 5 2 6 2 6H4s2-1 2-6"/><path d="M10 19a2 2 0 0 0 4 0"/></svg>',
  archive: '<svg viewBox="0 0 24 24"><rect x="4" y="4" width="16" height="6" rx="1"/><path d="M6 10v9h12v-9M10 14h4"/></svg>'
}
const TYPE_ICON_DEFAULT = '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>'

/* 九态脉搏格（标签用批准的短写；hot = 待审、err = 失败） */
const STATUS_TILES = [
  { key: 'pending', label: 'Pending' },
  { key: 'processing', label: 'Process' },
  { key: 'awaiting_review', label: 'Awaiting' },
  { key: 'adopted', label: 'Adopted' },
  { key: 'rejected', label: 'Rejected' },
  { key: 'expired', label: 'Expired' },
  { key: 'merged', label: 'Merged' },
  { key: 'failed', label: 'Failed' },
  { key: 'malformed', label: 'Malform' }
]

const BODY_CLAMP = 360   // 内容摘录字符上限（超出显示「… 共 N 字」）
const PARA_CLAMP = 220   // 单段摘录字符上限
const MAX_PARAS = 3

function clip(s, n) {
  s = s.replace(/\s+/g, ' ').trim()
  return s.length > n ? s.slice(0, n) + '…' : s
}

export const MemoryConsole = {
  name: 'MemoryConsole',
  props: {
    open: { type: Boolean, default: false },      // 面板开合（app 控制；关闭态 width 0）
    detail: { type: String, default: null }       // 详情面的提案 relpath（null = 队列面）
  },
  emits: ['pick', 'back', 'close', 'actioned', 'open-doc'],
  setup(props, { emit }) {
    const state = inject('state')

    // ─── 数据（app 30s 轮询写入；byRel 供详情面 / 队列富化查表）───
    const byRel = computed(() => {
      const m = {}
      for (const p of (state.memoryProposals || [])) m[p.relpath] = p
      return m
    })

    // ─── 审查队列（status.awaiting_review 最旧在前，富化 source_refs[0] + run_id）───
    const queue = computed(() => {
      const st = state.memoryStatus
      if (!st || !st.proposals) return []
      const awaiting = st.proposals.awaiting_review || []
      return awaiting.map(it => {
        const full = byRel.value[it.relpath] || {}
        return {
          ...it,
          run_id: full.run_id || '',
          src: (full.source_refs && full.source_refs[0]) || ''
        }
      })
    })
    const queueCount = computed(() => queue.value.length)

    // 脉冲只给「最新到达」的卡：上一次轮询里不存在、本次出现的 relpath；
    // 脉冲常驻该卡直到它离开队列（采纳/驳回/过期）；首次轮询只建基线，不闪。
    let firstStatusSeen = false
    let prevQueueSet = null
    const freshSet = ref(new Set())
    watch(
      () => state.memoryStatus,
      (st) => {
        const cur = (st && st.proposals && st.proposals.awaiting_review) || []
        const curSet = new Set(cur.map(x => x.relpath))
        if (!firstStatusSeen) {
          firstStatusSeen = true
          prevQueueSet = curSet
          return
        }
        if (prevQueueSet) {
          for (const x of cur) if (!prevQueueSet.has(x.relpath)) freshSet.value.add(x.relpath)
          for (const r of [...freshSet.value]) if (!curSet.has(r)) freshSet.value.delete(r)
        }
        prevQueueSet = curSet
      }
    )
    const freshRel = computed(() => {
      const q = queue.value
      if (!q.length) return null
      for (let i = q.length - 1; i >= 0; i--) {
        if (freshSet.value.has(q[i].relpath)) return q[i].relpath
      }
      return null
    })

    // ─── 脉搏：九态 / 触发 X/Y + 今日 run 预算 / 召回需求 ───
    const byStatus = computed(() =>
      (state.memoryStatus && state.memoryStatus.proposals && state.memoryStatus.proposals.by_status) || {})
    const agents = computed(() => (state.memoryStatus && state.memoryStatus.agents) || [])
    const recall = computed(() => (state.memoryStatus && state.memoryStatus.recall) || {})
    const recallTopWeight = computed(() =>
      (recall.value.top || []).reduce((s, t) => s + (t.weighted || 0), 0))
    const recall_error = computed(() => !!recall.value.recall_error)
    // 「高召回」标签旁的指标定义 tip：点击展开/收起（非 hover）；展开行插在标签行下方，
    // 0fr→1fr 过渡，收起态零高度不影响列表对齐
    const tipOpen = ref(false)

    function relRun(ts) {
      if (!ts) return '尚未运行'
      const diff = Date.now() / 1000 - ts
      if (diff < 3600) return Math.max(1, Math.floor(diff / 60)) + ' 分钟前'
      if (diff < 86400) return Math.floor(diff / 3600) + ' 小时前'
      if (diff < 86400 * 2) return '昨天 ' + new Date(ts * 1000).toTimeString().slice(0, 5)
      return new Date(ts * 1000).toISOString().slice(0, 10)
    }
    function triggerPct(a) {
      const armed = a.armed || 0
      if (a.armed_target) return Math.min(100, Math.round((armed / a.armed_target) * 100))
      return armed > 0 ? 100 : 0
    }
    // 状态 pill：服务端 status key → 批准短写 + CSS class（awaiting_review → Awaiting/awaiting）
    const PILL = {
      pending: ['Pending', ''], processing: ['Process', ''], awaiting_review: ['Awaiting', 'awaiting'],
      adopted: ['Adopted', 'adopted'], rejected: ['Rejected', 'rejected'], expired: ['Expired', 'expired'],
      merged: ['Merged', 'merged'], failed: ['Failed', 'failed'], malformed: ['Malform', '']
    }
    function pillText(s) { return (PILL[s] && PILL[s][0]) || s || '' }
    function pillCls(s) { return (PILL[s] && PILL[s][1]) || '' }
    // 空队列历史行：终审过的累计数（adopted + rejected + expired 为终审终态）
    const reviewedTotal = computed(() =>
      (byStatus.value.adopted || 0) + (byStatus.value.rejected || 0) + (byStatus.value.expired || 0))

    // ─── 详情面 ───
    const detailItem = computed(() =>
      props.detail ? (byRel.value[props.detail] || null) : null)

    // 队列卡年龄（mockup 文案：今天 / 1d / 4d）
    function formatAge(days) {
      if (days == null || isNaN(days)) return ''
      if (days < 1) return '今天'
      if (days === 1) return '1d'
      return Math.floor(days) + 'd'
    }
    // 详情头计数：当前提案在审查队列中的位次（深链指向队外提案 → 0，隐藏）
    const queueIndex = computed(() => {
      if (!props.detail) return 0
      const i = queue.value.findIndex(p => p.relpath === props.detail)
      return i < 0 ? 0 : i + 1
    })
    // 提案列表是否已到达（null = 尚未轮询；[] 轮询过但无提案 → 详情区分「加载 / 未找到」）
    const proposalsLoaded = computed(() => state.memoryProposals !== null)

    // 内容摘录（proposal 文件 body；frontmatter 剥离）
    const bodyText = ref(null)      // null = 加载中 / 无
    const bodyError = ref(null)
    let bodyLoadToken = 0
    async function loadBody(relpath) {
      const token = ++bodyLoadToken
      bodyText.value = null
      bodyError.value = null
      try {
        const raw = await api.getDoc(relpath)
        if (token !== bodyLoadToken) return
        const m = raw.match(/^---\r?\n[\s\S]*?\r?\n---\r?\n?/)
        bodyText.value = (m ? raw.slice(m[0].length) : raw).trim()
      } catch (e) {
        if (token === bodyLoadToken) bodyError.value = e.message
      }
    }
    const bodyParas = computed(() => {
      if (!bodyText.value) return []
      const paras = bodyText.value.split(/\n{2,}/).map(p => p.trim()).filter(Boolean)
      return paras.slice(0, MAX_PARAS).map(p => clip(p, PARA_CLAMP))
    })
    const bodyTotal = computed(() => bodyText.value ? bodyText.value.replace(/\s+/g, '').length : 0)
    const bodyClamped = computed(() => bodyTotal.value > BODY_CLAMP)

    // 相似记忆比对：source_refs ∩ 召回 top（30d 加权榜）
    const similar = computed(() => {
      const item = detailItem.value
      if (!item || !Array.isArray(item.source_refs)) return []
      const top = recall.value.top || []
      const max = top.reduce((m, t) => Math.max(m, t.weighted || 0), 0)
      const byPath = {}
      for (const t of top) byPath[t.path] = t
      return item.source_refs
        .filter(r => byPath[r])
        .slice(0, 3)
        .map(r => ({
          path: r,
          score: (max > 0 ? 0.5 + 0.45 * (byPath[r].weighted / max) : 0.5).toFixed(2),
          sub: '与提案共享来源 · 30d 召回榜在列'
        }))
    })

    // 动作行可见性：仅 awaiting_review 且未进入确认之后
    const canAct = computed(() =>
      !!detailItem.value && detailItem.value.status === 'awaiting_review' && !doneView.value)

    // ─── 动作（adopt / reject：只移状态；成功后取轨迹 + 切确认之后子视图，app 重轮询）───
    const acting = ref(false)
    const actionError = ref(null)
    const rsnOpen = ref(false)
    const rejectReason = ref('')
    const doneView = ref(false)
    const trail = ref([])
    const pillStatus = ref('')
    const lastAction = ref('')

    const reasonOk = computed(() => rejectReason.value.trim().length > 0)

    async function loadTrail(relpath) {
      try {
        const data = await api.getProposalTrail(relpath)
        trail.value = ((data && data.trail) || []).slice().reverse()  // 端点最新在前 → 时间正序
      } catch (e) {
        trail.value = []
      }
    }

    // 轨迹行文案由 trailRowsOf（文件尾）生成；此处组装渲染行 + adopted 未落地时的虚线预告行
    const showFutureRow = computed(() =>
      doneView.value && detailItem.value &&
      (pillStatus.value || detailItem.value.status) === 'adopted')
    const trailRows = computed(() => {
      const rows = trailRowsOf(trail.value, lastAction.value)
      if (showFutureRow.value) {
        rows.push({ seq: 'next', act: 'executor', msg: '落地下一循环（执行器待跑）', future: true })
      }
      return rows
    })
    const doneLabel = computed(() => {
      const s = pillStatus.value || (detailItem.value && detailItem.value.status) || ''
      if (s === 'merged') return '执行器已落地'
      if (s === 'adopted') return '等待执行器落地'
      if (s === 'rejected') return '已驳回 · ledger 记账'
      if (s === 'expired') return '已过期'
      return '状态 ' + pillText(s)
    })
    function toggleRsn() {
      rsnOpen.value = !rsnOpen.value
      actionError.value = null
    }

    async function adopt() {
      const item = detailItem.value
      if (!item || acting.value) return
      acting.value = true
      actionError.value = null
      rsnOpen.value = false
      try {
        await api.adoptProposal(item.relpath)
        lastAction.value = 'adopt'
        pillStatus.value = 'adopted'
        doneView.value = true
        await loadTrail(item.relpath)
        emit('actioned')   // app 立即重轮询（列表 / 徽标 / 脉搏同步翻转）
      } catch (e) {
        actionError.value = e.message
        emit('actioned')   // 400（状态已被他人改动等）→ 提示 + 重轮询
      } finally {
        acting.value = false
      }
    }

    async function reject() {
      const item = detailItem.value
      if (!item || acting.value || !reasonOk.value) return
      acting.value = true
      actionError.value = null
      try {
        await api.rejectProposal(item.relpath, rejectReason.value.trim())
        lastAction.value = 'reject'
        pillStatus.value = 'rejected'
        doneView.value = true
        rsnOpen.value = false
        await loadTrail(item.relpath)
        emit('actioned')
      } catch (e) {
        actionError.value = e.message
        emit('actioned')
      } finally {
        acting.value = false
      }
    }

    // 确认之后子视图：落地文件 = 轨迹中 executor 写入的目标文档（path ≠ 提案自身）
    const landedFile = computed(() => {
      const item = detailItem.value
      if (!item) return null
      const row = trail.value.find(r => r.path && r.path !== item.relpath)
      return row ? row.path : null
    })
    const doneMeta = computed(() => {
      const item = detailItem.value
      if (!item) return ''
      const s = pillStatus.value || item.status
      if (s === 'merged') return 'merged_at ' + (item.merged_at || '—') + ' · 状态 awaiting_review → adopted → merged'
      if (s === 'adopted') return 'merged_at 待定 · 状态 awaiting_review → adopted，执行器下一循环落地'
      if (s === 'rejected') return 'reject_reason 已写入 · ' + (item.reject_reason || '—')
      return '状态 ' + s
    })

    // 详情面复位 + 按需取摘录；深链指向非待审提案（铃铛深链）→ 直接给确认之后子视图
    watch(
      () => props.detail,
      async (rel) => {
        if (!rel) return
        pillStatus.value = ''
        doneView.value = false
        rsnOpen.value = false
        rejectReason.value = ''
        actionError.value = null
        trail.value = []
        lastAction.value = ''
        await loadBody(rel)
        const item = detailItem.value
        if (item && item.status !== 'awaiting_review') {
          doneView.value = true
          pillStatus.value = item.status
          await loadTrail(rel)
        }
      }
    )
    // 轮询发现状态已离开 awaiting_review（他人操作）→ 动作行收起，切确认之后
    watch(
      () => detailItem.value && detailItem.value.status,
      (s) => {
        if (!s || s === 'awaiting_review' || doneView.value) return
        doneView.value = true
        if (!pillStatus.value) pillStatus.value = s
        if (detailItem.value) loadTrail(detailItem.value.relpath)
      }
    )

    function pick(item) { emit('pick', item.relpath) }
    function back() { emit('back') }
    function close() { emit('close') }
    function openSrc(ref) { emit('open-doc', ref) }

    // 切到详情面后聚焦滚动区（键盘可用）
    watch(
      () => props.detail,
      async (rel) => {
        if (!rel) return
        await nextTick()
        const el = document.querySelector('#memory-console-shell .c-scroll')
        if (el) el.scrollTop = 0
      }
    )

    return {
      queue, queueCount, freshRel, byStatus, agents, recall, recallTopWeight, recall_error, tipOpen,
      relRun, triggerPct, pillText, pillCls, formatAge, reviewedTotal, STATUS_TILES,
      detailItem, queueIndex, proposalsLoaded, bodyText, bodyParas, bodyTotal, bodyClamped, bodyError, similar,
      canAct, acting, actionError, rsnOpen, rejectReason, doneView, trailRows,
      pillStatus, reasonOk, landedFile, doneMeta, doneLabel,
      adopt, reject, pick, back, close, openSrc, toggleRsn,
      TYPE_ICONS, TYPE_ICON_DEFAULT
    }
  },
  template: `
    <!-- 队列面（:key 变化 → 面入场动画重放：opacity + 8px 位移） -->
    <div v-if="!detail" :key="'queue'" class="c-face anim">
      <div class="p-head">
        <span class="picon"><svg viewBox="0 0 24 24"><path d="M8 5h11M8 12h11M8 19h11"/><path d="M3.5 5l1 1 2-2M3.5 12l1 1 2-2M3.5 19l1 1 2-2"/></svg></span>
        <span class="ptitle">记忆控制台</span>
        <span class="pcount">{{ queueCount }} 待审</span>
        <span class="pacts"><button class="ibtn" title="关闭" @click="close"><svg viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg></button></span>
      </div>
      <div class="c-scroll">
        <div class="c-sec">
          <div class="c-sec-h"><span>审查队列</span><span class="cnt" :class="{ hot: queueCount > 0 }">{{ queueCount }}</span><span class="poll">轮询 30s</span></div>
          <div v-if="queue.length" class="q-list">
            <button v-for="item in queue" :key="item.relpath" type="button"
              class="q-card" :class="{ fresh: freshRel === item.relpath }"
              :title="item.relpath" @click="pick(item)">
              <div class="q-row1">
                <span class="tchip"><span v-html="TYPE_ICONS[item.type] || TYPE_ICON_DEFAULT"></span>{{ item.type || 'unknown' }}</span>
                <span class="q-title">{{ item.title }}</span>
                <span class="q-age">{{ formatAge(item.age_days) }}</span>
              </div>
              <div class="q-row2" :title="item.run_id || item.src">{{ item.src || item.relpath }}<template v-if="item.run_id"> · {{ item.run_id }}</template></div>
            </button>
          </div>
          <div v-else class="q-empty">
            <div class="ring"><svg viewBox="0 0 24 24"><path d="M4 13l3-8h10l3 8v6H4z"/><path d="M4 13h5l1.5 2.5h3L15 13h5"/></svg></div>
            <div class="t1">无待审提案</div>
            <div class="t2">系统运行正常，脉搏见下</div>
          </div>
          <div v-if="queueCount === 0" class="q-hist">累计终审 {{ reviewedTotal }} · 脉搏仍常驻</div>
        </div>

        <div class="c-sec">
          <div class="c-sec-h"><span>脉搏</span></div>
          <div class="p-tiles">
            <div v-for="t in STATUS_TILES" :key="t.key" class="p-tile"
              :class="{ hot: t.key === 'awaiting_review' && (byStatus[t.key] || 0) > 0, err: t.key === 'failed' && (byStatus[t.key] || 0) > 0 }">
              <div class="k" :title="t.key">{{ t.label }}</div>
              <div class="v" :class="{ zero: !(byStatus[t.key] || 0) }">{{ byStatus[t.key] || 0 }}</div>
            </div>
          </div>
          <div class="tr-rows">
            <div v-if="!agents.length" class="tr-sub" style="padding: 2px">暂无后台智能体（trigger_state 为空）</div>
            <div v-for="a in agents" :key="a.alias" class="tr-row">
              <div class="tr-top"><span class="tr-name">{{ a.alias }}</span><span class="tr-target" v-if="a.armed_target != null">触发 {{ a.armed }}/{{ a.armed_target }}</span><span class="tr-target" v-else>触发 {{ a.armed }}</span></div>
              <div class="tr-bar"><i :style="{ width: triggerPct(a) + '%' }"></i></div>
              <div class="tr-sub"><span>上次 {{ relRun(a.last_run) }}</span><span class="sp"></span><span v-if="a.runs_cap">今天 {{ a.runs_today }}/{{ a.runs_cap }} 次</span><span v-else-if="a.runs_today > 0">今天 {{ a.runs_today }} 次</span></div>
            </div>
          </div>
          <div class="rcl">
            <div class="rcl-stats">
              <div class="rcl-stat"><div class="k">7d 无命中</div><div class="v">{{ recall.failed_7d || 0 }}</div></div>
              <div class="rcl-stat"><div class="k">在轨文档</div><div class="v">{{ recall.docs_tracked || 0 }}</div></div>
              <div class="rcl-stat"><div class="k">top5 加权</div><div class="v">{{ recallTopWeight.toFixed(1) }}</div></div>
            </div>
            <div v-if="recall.top && recall.top.length" class="rcl-top">
              <div class="k">高召回 · 30d 加权<button type="button" class="rcl-tip" :class="{ open: tipOpen }" :aria-expanded="tipOpen" title="指标定义" @click="tipOpen = !tipOpen"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 8h.01"/><path d="M11 12h1v4h1"/></svg></button></div>
              <div class="rcl-tipbox" :class="{ open: tipOpen }"><div class="rcl-tipbox-in"><p>近 30 天 human（人）+ on_demand（agent）召回命中，按 30 天半衰期指数衰减加权；不含后台 agent 合成流量。</p></div></div>
              <div v-for="t in recall.top" :key="t.path" class="rcl-path" :title="t.path">{{ t.path }}<span class="w">w.{{ (t.weighted || 0).toFixed(2) }}</span></div>
            </div>
            <div v-else class="rcl-top"><div class="k">高召回 · 30d 加权</div><div class="rcl-path" style="color: var(--ink-soft)">召回表暂无数据</div></div>
          </div>
        </div>
      </div>
    </div>

    <!-- 详情面：五段材料按判断顺序 + 粘性动作行 + 确认之后子视图 -->
    <div v-else :key="'detail-' + detail" class="c-face anim">
      <div class="p-head">
        <button class="ibtn" title="返回队列" @click="back"><svg viewBox="0 0 24 24"><path d="M15 5l-7 7 7 7"/></svg></button>
        <span class="ptitle">终审</span>
        <span class="pcount" v-if="queueIndex > 0">{{ queueIndex }}/{{ queueCount }}</span>
        <span class="pacts"><button class="ibtn" title="关闭" @click="close"><svg viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg></button></span>
      </div>
      <div class="c-scroll">
        <div v-if="!detailItem" class="q-empty" style="padding-top: 40px">
          <template v-if="!proposalsLoaded">
            <div class="t1">加载提案数据…</div>
            <div class="t2">首次轮询到达后自动显示</div>
          </template>
          <template v-else>
            <div class="t1">未找到该提案</div>
            <div class="t2">文件可能已被删除或重命名</div>
          </template>
        </div>
        <template v-else>
          <!-- (1) 类型 + 状态 + 标题 + 年龄 -->
          <div class="d-head">
            <div class="d-chips">
              <span class="tchip"><span v-html="TYPE_ICONS[detailItem.type] || TYPE_ICON_DEFAULT"></span>{{ detailItem.type || 'unknown' }}</span>
              <span class="spill" :class="pillCls(pillStatus || detailItem.status)">{{ pillText(pillStatus || detailItem.status) }}</span>
              <span class="sp"><span class="q-age">{{ formatAge(detailItem.age_days) }}</span></span>
            </div>
            <div class="d-title">{{ detailItem.title }}</div>
            <div class="d-path" :title="detailItem.relpath">{{ detailItem.relpath }}</div>
          </div>

          <!-- (2) 内容摘录 -->
          <div class="d-sec">
            <div class="d-label"><span>内容</span><span class="ln">body 摘录 · 全文见来源</span></div>
            <div class="d-md" v-if="bodyError"><span style="color: #b91c1c">{{ bodyError }}</span></div>
            <div class="d-md" v-else-if="!bodyParas.length"><span style="color: var(--ink-soft)">{{ bodyText === null ? '读取中…' : '（无正文）' }}</span></div>
            <div class="d-md" v-else>
              <p v-for="(p, i) in bodyParas" :key="i">{{ p }}</p>
              <div v-if="bodyClamped" class="ellip">… 共 {{ bodyTotal }} 字</div>
            </div>
          </div>

          <!-- (3) 来源（可跳转原文） -->
          <div class="d-sec" v-if="detailItem.source_refs && detailItem.source_refs.length">
            <div class="d-label"><span>来源</span><span class="ln">source_refs · 可跳转</span></div>
            <div class="src-list">
              <a v-for="r in detailItem.source_refs" :key="r" class="src-link" :title="r" href="javascript:void(0)" @click="openSrc(r)">
                <svg viewBox="0 0 24 24"><path d="M14 5h5v5M19 5l-8 8"/><path d="M19 13v5a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h5"/></svg>{{ r }}
              </a>
            </div>
          </div>

          <!-- (4) 批次 · 可逆性 -->
          <div class="d-sec">
            <div class="d-label"><span>批次 · 可逆性</span></div>
            <div class="run-box">
              <div class="run-line"><span class="rk">批次</span><span class="rv" :title="detailItem.run_id">{{ detailItem.run_id || '—' }}</span></div>
              <div class="run-line"><span class="rk">产出者</span><span class="rv">{{ detailItem.actor || '—' }}</span></div>
              <div class="run-line"><span class="rk">可逆性</span><span class="rv"><span class="spill" :class="detailItem.reversible ? 'adopted' : ''">{{ detailItem.reversible ? '可回滚' : '不可回滚' }}</span></span></div>
              <div class="run-line"><span class="rk">创建</span><span class="rv">{{ detailItem.created || '—' }}</span></div>
            </div>
          </div>

          <!-- (5) 相似记忆比对 -->
          <div class="d-sec">
            <div class="d-label"><span>相似记忆</span><span class="ln">source_refs ∩ 召回榜 · 查重复</span></div>
            <div v-if="similar.length" class="sim-rows">
              <div v-for="s in similar" :key="s.path" class="sim-row">
                <div class="sp1"><span class="pp" :title="s.path">{{ s.path }}</span><span class="sc">{{ s.score }}</span></div>
                <div class="sp2">{{ s.sub }}</div>
              </div>
            </div>
            <div v-else class="tr-sub" style="padding: 2px 2px 4px">与 30d 召回榜无重叠（{{ recall_error ? '召回数据暂不可用' : '无共享来源' }}）</div>
          </div>

          <!-- 操作失败提示（400 等；服务端文案直接展示） -->
          <div v-if="actionError" class="m-err">
            <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 8v4"/><path d="M12 16h.01"/></svg>
            <span>{{ actionError }}</span>
          </div>

          <!-- 确认之后：状态翻转 + 执行器落地 + change_log 轨迹 -->
          <div v-if="doneView" class="done">
            <div class="done-k"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M8 12.5l2.5 2.5L16 9.5"/></svg>{{ doneLabel }}</div>
            <div v-if="landedFile && (pillStatus || detailItem.status) === 'merged'" class="done-file" :title="landedFile">{{ landedFile }}</div>
            <div class="done-meta">{{ doneMeta }}</div>
            <div class="log-trail">
              <div v-for="(row, i) in trailRows" :key="row.seq + '-' + i" class="log-line" :class="{ future: row.future }">
                <span class="seq">{{ row.seq }}</span><span class="act">{{ row.act }}</span><span class="msg">{{ row.msg }}</span>
              </div>
            </div>
          </div>

          <!-- 拒绝理由（必填；输入非空前提交禁用） -->
          <div v-if="rsnOpen && !doneView" class="rsn">
            <div class="rsn-hint"><span class="req">必填</span>拒绝理由将写入 reject_reason，是信任升级的数据源</div>
            <textarea v-model="rejectReason" class="rsn-ta" placeholder="例：两篇文档近期已人工合并过，此提案过期"></textarea>
            <button class="rsn-sub" :class="{ ok: reasonOk }" :disabled="!reasonOk || acting" @click="reject">确认驳回</button>
          </div>

          <!-- 动作行（sticky 底） -->
          <div v-if="canAct" class="act-row">
            <button class="btn btn-adopt" :disabled="acting" @click="adopt">
              <svg viewBox="0 0 24 24"><path d="M5 12l5 5 9-10"/></svg>{{ acting ? '提交中…' : '审查通过 · 采纳' }}
            </button>
            <button class="btn btn-rej" :disabled="acting" @click="toggleRsn">驳回</button>
          </div>
        </template>
      </div>
    </div>
  `
}

/* 轨迹渲染行：human = 状态机决策（最近一条用刚执行的动作文案）；非 human = 执行器落文件 */
function trailRowsOf(trail, lastAction) {
  const rows = []
  let humanIdx = -1
  trail.forEach((r, i) => { if (r.source === 'human') humanIdx = i })
  trail.forEach((r, i) => {
    const msg = r.source === 'human'
      ? (i === humanIdx && lastAction === 'adopt' ? '采纳提案 · 状态机 ledger 记账'
        : i === humanIdx && lastAction === 'reject' ? '驳回提案 · 状态机 ledger 记账'
          : '状态变更 · 状态机 ledger 记账')
      : '应用变更 · ' + r.path
    rows.push({ seq: '#' + r.seq, act: r.source, msg })
  })
  return rows
}
