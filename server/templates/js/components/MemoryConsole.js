/* MemoryConsole 组件：300px 记忆控制台面板（v4 L0 主态 = 定稿 design/console-v4-watch.html 场景 01）
 * 两个面：L0 主态（.l0bar 纯状态行「N 待审 · N 待落地 · N 失败 · 轮询 30s」+ 行尾「后台 →」唯一入口
 *         + .feed 流水：agent 事件细行 / 待审卡（钻入详情）/ 终态卡淡显痕迹）
 *         终审详情（五段材料按判断顺序 + 粘性动作行 + 拒绝理由 + 确认之后子视图）
 * 宪法：console 只移状态（adopt/reject 走状态机，actor = human）；文件由确定性执行器落地。
 * 数据：state.memoryStatus / state.memoryProposals 由 app 30s 轮询注入，本组件不自建轮询；
 *       L0 与 feed 全部消费这两个 ref（awaiting 5 字段按 relpath join proposals 富化 source_refs / run_id / created）；
 *       内容摘录经 GET /api/docs/{relpath} 按需读取（frontmatter 剥离后取 body）。
 * 入口纪律：「后台 →」= 唯一 L0→L1 入口（window.openMonitorPage()，Phase A 挂载）；
 *       v3 九格脉搏 / 触发行 / 召回块整体移除——九态由监控页①九格承担，面板内彻底消失。
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

    // ─── L0 状态行 + feed 事件行数据（v3 的触发条 / 召回块整体移除 → 监控页「系统」组；
    //     recall 仅保留：详情面「相似记忆」的 source_refs ∩ 召回榜消费）───
    const byStatus = computed(() =>
      (state.memoryStatus && state.memoryStatus.proposals && state.memoryStatus.proposals.by_status) || {})
    // L0 单行行动数（设计场景 01 口径）：awaiting（行动）/ adopted（执行器队列）/ failed（需留意，>0 告警红）
    const l0 = computed(() => ({
      awaiting: byStatus.value.awaiting_review || 0,
      adopted: byStatus.value.adopted || 0,
      failed: byStatus.value.failed || 0
    }))
    const agents = computed(() => (state.memoryStatus && state.memoryStatus.agents) || [])
    const recall = computed(() => (state.memoryStatus && state.memoryStatus.recall) || {})
    const recall_error = computed(() => !!recall.value.recall_error)

    // ─── feed 流水（设计场景 01 定稿：agent 事件细行 / 待审卡 / 终态卡淡显痕迹；
    //     时间轴分组 今天 / 昨天 / MM-DD，无时间数据归「更早」；倒序取前 FEED_CAP 条，
    //     尾注 f-more 计数指向监控页「最近终态 / 异常清单」）───
    const FEED_CAP = 8
    const pad2 = n => String(n).padStart(2, '0')
    const dayKey = d => d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate())
    /* merged_at（PyYAML datetime 化 "YYYY-MM-DD HH:MM:SS" / ISO T）→ MM-DD HH:mm（设计稿口径） */
    function fmtMerged(s) {
      const m = String(s || '').match(/(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/)
      return m ? m[2] + '-' + m[3] + ' ' + m[4] + ':' + m[5] : String(s || '')
    }
    const feedGroups = computed(() => {
      const st = state.memoryStatus
      const awaiting = ((st && st.proposals && st.proposals.awaiting_review) || []).map(it => {
        const full = byRel.value[it.relpath] || {}
        return {
          kind: 'awaiting', key: 'a-' + it.relpath, relpath: it.relpath,
          type: it.type, title: it.title, age: it.age_days, created: full.created || null,
          src: (full.source_refs && full.source_refs[0]) || it.relpath,
          run_id: full.run_id || '', fresh: freshRel.value === it.relpath
        }
      })
      // 事件行 = 最近发生了什么：仅「跑过」的 agent 成行（last_run = 0 尚未运行 → 无事件，见监控页③）
      const events = agents.value
        .filter(a => a.last_run)
        .map(a => {
          const d = new Date(a.last_run * 1000)
          const t = new Date(); const y = new Date(); y.setDate(y.getDate() - 1)
          const today = dayKey(d) === dayKey(t), yesterday = dayKey(d) === dayKey(y)
          return {
            kind: 'agent', key: 'e-' + a.alias, alias: a.alias,
            date: new Date(d.getFullYear(), d.getMonth(), d.getDate()),
            time: today || yesterday ? pad2(d.getHours()) + ':' + pad2(d.getMinutes()) : pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()),
            msg: 'run 完成 · 今天 ' + (a.runs_today || 0) + ' 次'
          }
        })
      const terminals = (state.memoryProposals || [])
        .filter(p => p.status === 'merged' || p.status === 'rejected' || p.status === 'failed')
        .map(p => {
          const trail = Array.isArray(p.trail) ? [...p.trail].sort((a, b) => (a.seq || 0) - (b.seq || 0)) : []
          let landed = null
          for (const r of trail) if (r.path && r.path !== p.relpath) { landed = r.path; break }
          let row2
          if (p.status === 'merged') row2 = (landed ? '落地 ' + landed : p.relpath) + ' · merged_at ' + (p.merged_at ? fmtMerged(p.merged_at) : '—')
          else if (p.status === 'rejected') row2 = '理由：' + (p.reject_reason || '—')
          else row2 = trail.length ? ('最近 op · ' + trail[trail.length - 1].path) + ' · 提案保留待修' : '提案保留待修 · 详情见「后台 →」'
          return {
            kind: 'term', key: 't-' + p.relpath, relpath: p.relpath, type: p.type,
            title: p.title, status: p.status, created: p.created || null, row2, trail: trail.slice(-2)
          }
        })
      /* 日期 → 组 key（t / y / MM-DD / x = 无日期数据「更早」）+ 标签 */
      const now = new Date(), yest = new Date(); yest.setDate(yest.getDate() - 1)
      const keyOf = d => d ? (dayKey(d) === dayKey(now) ? 't' : dayKey(d) === dayKey(yest) ? 'y' : dayKey(d)) : 'x'
      const labelOf = k => k === 't' ? '今天 · ' + pad2(now.getMonth() + 1) + '-' + pad2(now.getDate())
        : k === 'y' ? '昨天 · ' + pad2(yest.getMonth() + 1) + '-' + pad2(yest.getDate())
        : k === 'x' ? '更早' : k.slice(5)
      const parseDay = s => { const m = String(s || '').match(/\d{4}-\d{2}-\d{2}/); return m ? new Date(Number(m[0].slice(0, 4)), Number(m[0].slice(5, 7)) - 1, Number(m[0].slice(8, 10))) : null }
      /* 组内序 = 设计稿示例（agent 细行 → 待审卡 → 终态卡）；awaiting 最旧在前（v3 审查队列口径）、
         终态倒序；组间按日期倒序（今天 > 昨天 > 旧日期 > 更早） */
      const sortVal = x => x.kind === 'agent' ? 0 : x.kind === 'awaiting' ? 1 : 2
      const rank = k => k === 'x' ? 0
        : k === 't' ? Number(dayKey(now).replace(/-/g, ''))
        : k === 'y' ? Number(dayKey(yest).replace(/-/g, ''))
        : Number(k.replace(/-/g, ''))
      const groups = []
      const gmap = new Map()
      const push = (k, item) => {
        if (!gmap.has(k)) { const g = { key: k, label: labelOf(k), items: [] }; gmap.set(k, g); groups.push(g) }
        gmap.get(k).items.push(item)
      }
      for (const e of events) push(keyOf(e.date), e)
      for (const a of awaiting) push(keyOf(parseDay(a.created)), a)
      for (const t of terminals) push(keyOf(parseDay(t.created)), t)
      groups.sort((a, b) => rank(b.key) - rank(a.key))
      for (const g of groups) {
        // 同 kind 内：awaiting 最旧在前（age 降序）；终态倒序（created 降序）
        g.items.sort((a, b) => sortVal(a) - sortVal(b)
          || (a.kind === 'awaiting' ? (b.age || 0) - (a.age || 0)
              : String(b.created || '').localeCompare(String(a.created || ''))))
      }
      // f-time（设计稿 HH:mm 位）：agent 行 = last_run 时刻（上方已建）；卡 = 创建日相对标签（created 无时刻，
      // 无 created 的卡回退 age 口径）——终态卡回退 merged_at 时刻（设计稿「merged_at 10-20 09:41」取 HH:mm 无）→ 用日期标签
      for (const g of groups) for (const it of g.items) {
        if (it.kind === 'agent') continue
        const d = parseDay(it.created)
        it.time = d ? keyOf(d) === 't' ? '今天' : keyOf(d) === 'y' ? '昨天' : pad2(d.getMonth() + 1) + '-' + pad2(d.getDate())
          : (it.kind === 'awaiting' ? (formatAge(it.age) || '—') : '—')
      }
      // 倒序截断 FEED_CAP：超出部分并入 f-more 计数（指向监控页 ⑥最近终态 / ②异常清单）
      const flat = []
      for (const g of groups) for (const it of g.items) {
        if (flat.length >= FEED_CAP) break
        flat.push(it)
      }
      const total = groups.reduce((s, g) => s + g.items.length, 0)
      const shown = new Set(flat.map(x => x.key))
      const out = groups.map(g => ({ ...g, items: g.items.filter(x => shown.has(x.key)) })).filter(g => g.items.length)
      if (out.length) {
        const lastGroup = out[out.length - 1]
        const li = lastGroup.items[lastGroup.items.length - 1]
        if (li) li.lastItem = true   // 时间轴竖线在末行止住（.f-item.last::before 隐藏）
      }
      return { groups: out, more: Math.max(0, total - flat.length) }
    })
    const feedLen = computed(() => feedGroups.value.groups.reduce((s, g) => s + g.items.length, 0))
    /* feed 终态卡 mini-trail 行（与详情面 trailRowsOf 同口径：human = 状态机记账，其余 = 执行器落文件） */
    function trailMsg(t) {
      return t.source === 'human' ? '状态变更 · 状态机 ledger 记账' : '应用变更 · ' + t.path
    }

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
    /* 状态 pill（仅详情面消费）：status key → .spill 色 class（九态色板整体在监控页①，
       面板内保留原 key 短写；awaiting_review 为队列键名 → awaiting 类） */
    const PILL_CLS = {
      awaiting_review: 'awaiting', adopted: 'adopted', rejected: 'rejected',
      expired: 'expired', merged: 'merged', failed: 'failed'
    }
    function pillCls(s) { return PILL_CLS[s] || '' }
    function pillText(s) { return s || '' }

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
    // 设计场景 03：钻取/返回不毁队列面流水 —— 返回时恢复队列面滚动位置
    let queueScroll = 0
    watch(
      () => props.detail,
      async (rel) => {
        if (!rel) {
          await nextTick()
          const qel = document.querySelector('#memory-console-shell [data-face="queue"] .c-scroll')
          if (qel) qel.scrollTop = queueScroll
          return
        }
        const qel = document.querySelector('#memory-console-shell [data-face="queue"] .c-scroll')
        if (qel) queueScroll = qel.scrollTop
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
    // 「后台 →」= 唯一 L0→L1 入口（Phase A 在 app 挂载 window.openMonitorPage：
    // 面板收起 + 监控页接管主区域，互斥契约由 app 承担；顶栏无按钮）
    function openMonitor() {
      if (typeof window.openMonitorPage === 'function') window.openMonitorPage()
    }

    // 切到详情面后聚焦滚动区（键盘可用）；深链高亮：铃铛深链 null→path 时详情头标题一次黄铜下划线
    // sweep（marker-sweep 家族，~.4s 后自清；队内钻取 path→path 同样重放 = 新上下文的提示）
    let deepT = null
    watch(
      () => props.detail,
      async (rel) => {
        if (!rel) return
        await nextTick()
        const el = document.querySelector('#memory-console-shell .c-scroll')
        if (el) el.scrollTop = 0
        const face = document.querySelector('#memory-console-shell [data-face="detail"]')
        if (face) {
          face.classList.add('mp-deep')
          clearTimeout(deepT)
          deepT = setTimeout(() => face.classList.remove('mp-deep'), 900)
        }
      }
    )

    return {
      l0, feedGroups, feedLen, openMonitor, trailMsg,
      pillText, pillCls, formatAge, recall_error,
      detailItem, queueIndex, queueCount, proposalsLoaded, bodyText, bodyParas, bodyTotal, bodyClamped, bodyError, similar,
      canAct, acting, actionError, rsnOpen, rejectReason, doneView, trailRows,
      pillStatus, reasonOk, landedFile, doneMeta, doneLabel,
      adopt, reject, pick, back, close, openSrc, toggleRsn,
      TYPE_ICONS, TYPE_ICON_DEFAULT
    }
  },
  template: `
    <!-- L0 主态：纯状态行 + 流水（:key 变化 → 面入场动画重放：opacity + 8px 位移） -->
    <div v-if="!detail" :key="'queue'" data-face="queue" class="c-face anim">
      <div class="p-head">
        <span class="picon"><svg viewBox="0 0 24 24"><path d="M3 12h4l2.5-6 5 12 2.5-6h4"/></svg></span>
        <span class="ptitle">记忆控制台</span>
        <span class="pacts"><button class="ibtn" title="关闭" @click="close"><svg viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg></button></span>
      </div>
      <!-- L0 行动数 = 纯状态行（整行不可点；「轮询 30s」并入单行）；行尾「后台 →」= 唯一 L0→L1 入口；
           failed > 0 → 失败数告警红 + 入口小红点（0 时不渲染 dot） -->
      <div class="l0bar">
        <span class="l0-status"><span>{{ l0.awaiting }} 待审</span><span class="ld">·</span><span>{{ l0.adopted }} 待落地</span><span class="ld">·</span><span :class="{ err: l0.failed > 0 }">{{ l0.failed }} 失败</span><span class="ld">·</span><span class="dim">轮询 30s</span></span>
        <button class="l0-go" type="button" title="后台监控页 L1（面板收起，监控页接管主区域）" @click="openMonitor"><span v-if="l0.failed > 0" class="dot" aria-hidden="true"></span>后台<svg viewBox="0 0 24 24"><path d="M9 5l7 7-7 7"/></svg></button>
      </div>
      <div class="c-scroll">
        <div class="feed">
          <template v-if="feedLen">
            <template v-for="(grp, gi) in feedGroups.groups" :key="'g' + grp.key">
              <div class="f-day">{{ grp.label }}</div>
              <template v-for="(it, ii) in grp.items" :key="'i' + it.key">
                <div v-if="it.kind === 'agent'" class="f-item f-agent">
                  <span class="f-time">{{ it.time }}</span>
                  <div class="f-agent-txt"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8"/><path d="M12 8v4l2.5 2.5"/></svg><span class="nm">{{ it.alias }}</span><span class="msg">{{ it.msg }}</span></div>
                </div>
                <div v-else-if="it.kind === 'awaiting'" class="f-item" :class="{ hot: it.fresh }">
                  <span class="f-dot"></span>
                  <span class="f-time">{{ it.time }}</span>
                  <button class="f-card" :class="{ fresh: it.fresh }" type="button" :title="it.relpath" @click="pick(it)">
                    <div class="f-row1"><span class="tchip"><span v-html="TYPE_ICONS[it.type] || TYPE_ICON_DEFAULT"></span>{{ it.type || 'unknown' }}</span><span class="f-title">{{ it.title }}</span><span class="f-stamp">awaiting</span></div>
                    <div class="f-row2">{{ it.src }}<template v-if="it.run_id"><span class="sep">·</span>{{ it.run_id }}</template></div>
                  </button>
                </div>
                <div v-else class="f-item f-term" :class="{ last: it.lastItem }">
                  <span class="f-dot"></span>
                  <span class="f-time">{{ it.time }}</span>
                  <button class="f-card f-term" type="button" :title="it.relpath" @click="pick(it)">
                    <div class="f-row1"><span class="tchip">{{ it.type || 'unknown' }}</span><span class="f-title">{{ it.title }}</span><span class="spill" :class="it.status" style="font-size:9.5px;padding:1px 7px">{{ it.status }}</span></div>
                    <div class="f-row2">{{ it.row2 }}</div>
                    <div v-if="it.trail.length" class="f-trail">
                      <div v-for="(t, ti) in it.trail" :key="ti" class="log-line"><span class="seq">#{{ t.seq }}</span><span class="act">{{ t.source }}</span><span class="msg">{{ trailMsg(t) }}</span></div>
                    </div>
                  </button>
                </div>
              </template>
            </template>
            <div v-if="feedGroups.more > 0" class="f-more">{{ feedGroups.more }} 条更老 —— 终态全貌见「后台 →」· 最近终态</div>
          </template>
          <div v-else class="q-empty">
            <div class="ring"><svg viewBox="0 0 24 24"><path d="M4 13l3-8h10l3 8v6H4z"/><path d="M4 13h5l1.5 2.5h3L15 13h5"/></svg></div>
            <div class="t1">无待审提案</div>
            <div class="t2">系统运行正常 · 后台活动见「后台 →」</div>
          </div>
        </div>
      </div>
    </div>

    <!-- 详情面：五段材料按判断顺序 + 粘性动作行 + 确认之后子视图 -->
    <div v-else :key="'detail-' + detail" data-face="detail" class="c-face anim">
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
