/* MemoryConsole 组件：300px 记忆控制台面板（定稿 design/console-v5-prune.html）
 * 三个面：L0 主态（.l0bar 纯状态行「N 待审 · N 待落地 · N 失败[· N 快照降级]」+ 行尾「后台 →」唯一入口
 *         + .feed 纯审查队列：待审卡 / adopted 在途卡 / 终态卡淡显痕迹；
 *         卡片四要素 = 类型 tchip + 状态 .spill pill + 时间 f-time + 短标题
 *         （src 路径 / run_id / 落地·理由拼接 / mini-trail 全部移出卡片 → 详情对应节）
 *         + 报告区：只读 advisory 通道单行（仅有报告时渲染：alias + 标题 + 日期，无常驻空位）
 *         终审详情（五段材料按判断顺序 + 粘性动作行 + 拒绝理由 + 确认之后子视图；
 *         正文 = justification 全文 markdown（默认展开可折叠），其余各节 = 单行摘要 + 计数 + 点击展开（默认收起））
 *         报告详情（第三面：只读 markdown 全文 + 文件路径行；深链 state.memoryReport 直达）
 * 宪法：console 只移状态（adopt/reject 走状态机，actor = human）；文件由确定性执行器落地。
 * 数据：state.memoryStatus / state.memoryProposals / state.memoryReports 由 app 30s 轮询注入，本组件不自建轮询；
 *       L0 与 feed 全部消费这些 ref（awaiting 按 relpath join proposals 富化 created / trail 时刻，时间轴分组与 f-time）；
 *       正文经 GET /api/memory/proposals/{relpath} 按需读取（记录 body = justification；D25 无投影文件）；
 *       报告全文经 GET /api/memory/reports/{relpath} 按需读取（renderMarkdown = 既有 marked+DOMPurify 助手，无新依赖）；
 *       变更轨迹懒加载：首次展开轨迹节时经 GET /api/memory/proposals/{relpath}/trail 读取（awaiting 项不预取）。
 * 入口纪律：「后台 →」= 唯一 L0→L1 入口（window.openMonitorPage()，Phase A 挂载）；
 *       v3 九格脉搏 / 触发行 / 召回块整体移除——九态由监控页①九格承担，面板内彻底消失。
 */
const { ref, computed, watch, inject, nextTick } = Vue
import { api } from '../api.js'
import { renderMarkdown, postProcessPreview } from '../utils/markdown.js'

/* 类型 chip 图标（单色线条，stroke: currentColor；未知/存量退役类型回退时钟） */
export const TYPE_ICONS = {
  entity_index: '<svg viewBox="0 0 24 24"><path d="M4 6h16M4 12h16M4 18h10"/></svg>',
  relatedness: '<svg viewBox="0 0 24 24"><circle cx="5" cy="12" r="2.5"/><circle cx="19" cy="12" r="2.5"/><path d="M7.5 12h9"/></svg>',
  summary: '<svg viewBox="0 0 24 24"><path d="M8 5h11M8 12h11M8 19h11"/><path d="M3.5 5l1 1 2-2M3.5 12l1 1 2-2M3.5 19l1 1 2-2"/></svg>',
  errata: '<svg viewBox="0 0 24 24"><path d="M17 3a2.8 2.8 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"/></svg>',
  reminder: '<svg viewBox="0 0 24 24"><path d="M6 9a6 6 0 1 1 12 0c0 5 2 6 2 6H4s2-1 2-6"/><path d="M10 19a2 2 0 0 0 4 0"/></svg>'
}
export const TYPE_ICON_DEFAULT = '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>'

/* 折叠节 chevron（设计稿 .d-sec summary 口径：展开旋 90°） */
const CAR = '<svg viewBox="0 0 24 24"><path d="M9 5l7 7-7 7"/></svg>'

export const MemoryConsole = {
  name: 'MemoryConsole',
  props: {
    open: { type: Boolean, default: false },      // 面板开合（app 控制；关闭态 width 0）
    detail: { type: String, default: null },      // 详情面的提案 relpath（null = 队列面）
    report: { type: String, default: null }       // 报告详情面的 relpath（index/reports/**；null = 非报告面；深链直达）
  },
  emits: ['pick', 'back', 'close', 'actioned', 'open-doc', 'report-pick', 'report-back'],
  setup(props, { emit }) {
    const state = inject('state')

    // ─── 数据（app 30s 轮询写入；byRel 供详情面 / 队列富化查表）───
    const byRel = computed(() => {
      const m = {}
      for (const p of (state.memoryProposals || [])) m[p.relpath] = p
      return m
    })

    // ─── 审查队列（status.awaiting_review 最旧在前；位次 / 计数 / fresh 基线查表）───
    const queue = computed(() => {
      const st = state.memoryStatus
      if (!st || !st.proposals) return []
      return st.proposals.awaiting_review || []
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

    // ─── 报告渠道（只读 advisory：index/reports/<alias>/YYYY-MM-DD.md；app 轮询注入 state.memoryReports）───
    // 脉冲口径同 freshSet：首次轮询只建基线不闪；新报告出现 / mtime 跨轮询增长 → 脉冲，
    // 脉冲常驻该卡直到它离开列表（文件被删；slot 覆写 = 同 relpath mtime 增长 → 重新入脉冲集）
    let firstReportsSeen = false
    let prevReportMtimes = null
    const reportFreshSet = ref(new Set())
    watch(
      () => state.memoryReports,
      (list) => {
        if (!Array.isArray(list)) return
        const curMap = new Map(list.map(r => [r.relpath, r.mtime]))
        if (!firstReportsSeen) {
          firstReportsSeen = true
          prevReportMtimes = curMap
          return
        }
        if (prevReportMtimes) {
          for (const [rel, mt] of curMap) {
            const prev = prevReportMtimes.get(rel)
            if (prev === undefined || mt > prev) reportFreshSet.value.add(rel)
          }
          for (const rel of [...reportFreshSet.value]) if (!curMap.has(rel)) reportFreshSet.value.delete(rel)
        }
        prevReportMtimes = curMap
      }
    )
    const reports = computed(() => state.memoryReports || [])
    const reportsLen = computed(() => reports.value.length)
    // 报告单行（仅当有报告时渲染）：alias chip + title（无 # 标题回退 alias）+ 日期相对标签（今天/昨天/MM-DD）
    const reportCards = computed(() => {
      const now = new Date(), yest = new Date(); yest.setDate(yest.getDate() - 1)
      const shortOf = k => k === dayKey(now) ? '今天' : k === dayKey(yest) ? '昨天' : k.slice(5)
      return reports.value.map(r => {
        const m = String(r.date || '').match(/(\d{4})-(\d{2})-(\d{2})/)
        const d = m ? new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])) : null
        return { ...r, dateShort: d ? shortOf(dayKey(d)) : (r.date || '—'), fresh: reportFreshSet.value.has(r.relpath) }
      })
    })

    // ─── L0 状态行（v5：实现注释字样「轮询 30s」不进 UI）；agent 事件行删除（监控页③后台任务承担）───
    const byStatus = computed(() =>
      (state.memoryStatus && state.memoryStatus.proposals && state.memoryStatus.proposals.by_status) || {})
    // L0 单行行动数（设计场景 01 口径）：awaiting（行动）/ adopted（执行器队列）/ failed（需留意，>0 告警红）
    const l0 = computed(() => ({
      awaiting: byStatus.value.awaiting_review || 0,
      adopted: byStatus.value.adopted || 0,
      failed: byStatus.value.failed || 0
    }))
    // 快照降级计数（D19/#C2：v2a 运行期内降级不可静默）：进程内计数，重启归零；
    // >0 时 L0 行内显「N 快照降级」红边 chip，= 0 不占位。脉冲 = 仅计数增长时一次 envPulse（~1.2s 自清，
    // 克制节奏同「新告警强调」alertFresh 家族），不常驻闪烁；首次观察 = 基线（首载不闪，freshSet 同口径）——
    // 观察状态对象而非 derived 值，保证页内 0→1 增长（首载 0、后续轮询到 1）仍能触发
    const degraded = computed(() =>
      ((state.memoryStatus && state.memoryStatus.snapshot && state.memoryStatus.snapshot.degraded) || 0))
    let prevDeg = null
    const degradedFresh = ref(false)
    let degT = null
    watch(
      () => state.memoryStatus,
      (st) => {
        if (!st) return
        const n = ((st.snapshot && st.snapshot.degraded) || 0)
        if (prevDeg === null) { prevDeg = n; return }   // 首次观察 = 基线，不闪
        if (n > prevDeg) {
          degradedFresh.value = true
          clearTimeout(degT)
          degT = setTimeout(() => { degradedFresh.value = false }, 1300)
        }
        prevDeg = n
      }
    )
    const recall = computed(() => (state.memoryStatus && state.memoryStatus.recall) || {})
    const recall_error = computed(() => !!recall.value.recall_error)

    // ─── feed 流水（v5 剪枝：纯审查队列——待审卡 / adopted 在途卡 / 终态卡淡显痕迹；
    //     时间轴分组 今天 / 昨天 / MM-DD，无时间数据归「更早」；倒序取前 FEED_CAP 条，
    //     尾注 f-more 计数指向监控页「采纳提案 / 异常清单」）───
    const FEED_CAP = 8
    const pad2 = n => String(n).padStart(2, '0')
    const dayKey = d => d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate())
    /* 卡时间 = 具体时刻（HH:mm 位）：created 字段只有日期，
       具体 ts 取自 trail 行（path 须为提案文件本身，滤掉共享行）或 merged_at；统一返回毫秒 */
    const trailTs = (p, rel, pick) => {
      const ts = ((p && p.trail) || []).filter(r => r.path === rel && r.ts).map(r => r.ts * 1000)
      if (!ts.length) return null
      return pick === 'last' ? Math.max(...ts) : Math.min(...ts)
    }
    const fmtHM = ts => { const d = new Date(ts); return pad2(d.getHours()) + ':' + pad2(d.getMinutes()) }
    const feedGroups = computed(() => {
      const st = state.memoryStatus
      const awaiting = ((st && st.proposals && st.proposals.awaiting_review) || []).map(it => {
        const p = byRel.value[it.relpath] || {}
        return {
          kind: 'awaiting', key: 'a-' + it.relpath, relpath: it.relpath,
          type: it.type, title: it.title, age: it.age_days, created: p.created || null,
          ts: trailTs(p, it.relpath, 'first'),   // 提交点（trail 最早行）
          fresh: freshRel.value === it.relpath
        }
      })
      const terminals = (state.memoryProposals || [])
        .filter(p => p.status === 'merged' || p.status === 'rejected' || p.status === 'failed')
        .map(p => {
          // merged 取 merged_at 全时刻；rejected / failed 取轨迹最新点（终态转换）；皆无则 null
          const m = /:/.test(p.merged_at || '') ? Date.parse(String(p.merged_at).replace(' ', 'T')) : NaN
          const ts = !isNaN(m) ? m : (trailTs(p, p.relpath, 'last') || trailTs(p, p.relpath, 'first'))
          return {
            kind: 'term', key: 't-' + p.relpath, relpath: p.relpath, type: p.type,
            title: p.title, status: p.status, created: p.created || null, ts: ts || null
          }
        })
      // adopted 在途卡（执行器队列）：与终态卡同壳、不淡显
      const adopted = (state.memoryProposals || [])
        .filter(p => p.status === 'adopted')
        .map(p => ({
          kind: 'adopted', key: 'd-' + p.relpath, relpath: p.relpath, type: p.type,
          title: p.title, status: 'adopted', created: p.created || null, ts: trailTs(p, p.relpath, 'first')
        }))
      /* 日期 → 组 key（t / y / MM-DD / x = 无日期数据「更早」）+ 标签 */
      const now = new Date(), yest = new Date(); yest.setDate(yest.getDate() - 1)
      const keyOf = d => d ? (dayKey(d) === dayKey(now) ? 't' : dayKey(d) === dayKey(yest) ? 'y' : dayKey(d)) : 'x'
      const labelOf = k => k === 't' ? '今天 · ' + pad2(now.getMonth() + 1) + '-' + pad2(now.getDate())
        : k === 'y' ? '昨天 · ' + pad2(yest.getMonth() + 1) + '-' + pad2(yest.getDate())
        : k === 'x' ? '更早' : k.slice(5)
      const parseDay = s => { const m = String(s || '').match(/\d{4}-\d{2}-\d{2}/); return m ? new Date(Number(m[0].slice(0, 4)), Number(m[0].slice(5, 7)) - 1, Number(m[0].slice(8, 10))) : null }
      const dOf = it => it.ts ? new Date(it.ts) : parseDay(it.created)   // 分组键：具体时刻的日期，无时刻回退 created 日期
      /* 组内序 = 设计稿示例（待审卡 → 在途卡 → 终态卡）；awaiting 最旧在前（v3 审查队列口径）、
         终态倒序；组间按日期倒序（今天 > 昨天 > 旧日期 > 更早） */
      const sortVal = x => x.kind === 'awaiting' ? 0 : x.kind === 'adopted' ? 1 : 2
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
      for (const a of awaiting) push(keyOf(dOf(a)), a)
      for (const d of adopted) push(keyOf(dOf(d)), d)
      for (const t of terminals) push(keyOf(dOf(t)), t)
      groups.sort((a, b) => rank(b.key) - rank(a.key))
      for (const g of groups) {
        // 同 kind 内：awaiting 最旧在前（age 降序）；终态倒序（created 降序）
        g.items.sort((a, b) => sortVal(a) - sortVal(b)
          || (a.kind === 'awaiting' ? (b.age || 0) - (a.age || 0)
              : ((b.ts || 0) - (a.ts || 0) || String(b.created || '').localeCompare(String(a.created || '')))))
      }
      // f-time（设计稿 HH:mm 位）：具体时刻（提交点 / 终态转换点 / merged_at）；无时刻回退创建日相对标签
      for (const g of groups) for (const it of g.items) {
        if (it.ts) { it.time = fmtHM(it.ts); continue }
        const d = parseDay(it.created)
        it.time = d ? keyOf(d) === 't' ? '今天' : keyOf(d) === 'y' ? '昨天' : pad2(d.getMonth() + 1) + '-' + pad2(d.getDate())
          : (it.kind === 'awaiting' ? (formatAge(it.age) || '—') : '—')
      }
      // 倒序截断 FEED_CAP：超出部分并入 f-more 计数（指向监控页 ⑥采纳提案 / ②异常清单）
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

    // 正文（记录 body = justification 全文；D25 无投影文件，读记录端点；markdown 渲染，无截断）
    const bodyText = ref(null)      // null = 加载中 / 无
    const bodyError = ref(null)
    let bodyLoadToken = 0
    async function loadBody(relpath) {
      const token = ++bodyLoadToken
      bodyText.value = null
      bodyError.value = null
      try {
        const rec = await api.getProposal(relpath)
        if (token !== bodyLoadToken) return
        bodyText.value = (rec.justification || "").trim()
      } catch (e) {
        if (token === bodyLoadToken) bodyError.value = e.message
      }
    }
    const bodyTotal = computed(() => bodyText.value ? bodyText.value.replace(/\s+/g, '').length : 0)

    // 相似记忆比对：source_refs ∩ 召回 top（30d 加权榜）；只留事实（共享来源 / 召回榜在列），不合成相似度分数
    const similar = computed(() => {
      const item = detailItem.value
      if (!item || !Array.isArray(item.source_refs)) return []
      const top = recall.value.top || []
      const byPath = {}
      for (const t of top) byPath[t.path] = t
      return item.source_refs
        .filter(r => byPath[r])
        .slice(0, 3)
        .map(r => ({
          path: r,
          sub: '与提案共享来源 · 30d 召回榜在列'
        }))
    })

    // 批次摘要短写（summary 行）：run-20261103-0142 → run-…-0142（长 id 进摘要行放不下，全值在展开区）
    function shortRun(id) {
      const m = String(id || '').match(/^(run-)\d+(-\d+)$/)
      return m ? m[1] + '…' + m[2] : (id || '—')
    }

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
    /* 轨迹节（详情面懒加载）：首次展开时取 trail；计数 ln = 行数（加载前「—」）；
       确认之后子视图自带轨迹区 → 轨迹节在 doneView 时隐藏（避免双显） */
    const trailPrimed = ref(false)
    const trailLoading = ref(false)
    const trailCount = computed(() => trail.value.length)
    async function onTrailToggle(e) {
      if (trailPrimed.value || !detailItem.value) return
      trailLoading.value = true
      await loadTrail(detailItem.value.relpath)
      trailLoading.value = false
      if (e.target && e.target.open) trailPrimed.value = true   // 收起即未取 → 不标记（下次展开再取）
    }

    /* 状态 pill（详情头 + 队列卡共用）：status key → .spill 色 class（awaiting_review 为队列键名 → awaiting 类） */
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

    // 详情面复位 + 按需取正文；深链指向非待审提案（铃铛深链）→ 直接给确认之后子视图
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
        trailPrimed.value = false
        trailLoading.value = false
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

    // ─── 报告详情面（第三面：只读；内容 = markdown 全文，renderMarkdown 助手渲染，无新依赖）───
    const reportItem = computed(() =>
      props.report ? (reports.value.find(r => r.relpath === props.report) || null) : null)
    const reportContent = ref(null)      // null = 加载中 / 无
    const reportError = ref(null)
    let reportLoadToken = 0
    async function loadReport(relpath) {
      const token = ++reportLoadToken
      reportContent.value = null
      reportError.value = null
      try {
        const rec = await api.getMemoryReport(relpath)
        if (token !== reportLoadToken) return
        reportContent.value = (rec && typeof rec.content === 'string') ? rec.content : ''
        await nextTick()
        if (reportBodyEl.value) postProcessPreview(reportBodyEl.value)   // hljs 高亮 + mermaid（助手既有后处理）
      } catch (e) {
        if (token === reportLoadToken) reportError.value = e.message    // 404 = 深链指向已删报告 → 服务端文案直显
      }
    }
    watch(
      () => props.report,
      async (rel) => {
        if (!rel) { reportContent.value = null; reportError.value = null; return }
        await loadReport(rel)
      }
    )
    const reportBodyEl = ref(null)
    // 深链接力：bell 深链 null→relpath 时标题一次黄铜下划线 sweep（与详情面同口径）；面内切换同重放
    let rptDeepT = null
    watch(
      () => props.report,
      async (rel) => {
        if (!rel) return
        await nextTick()
        const el = document.querySelector('#memory-console-shell .c-scroll')
        if (el) el.scrollTop = 0
        const face = document.querySelector('#memory-console-shell [data-face="report"]')
        if (face) {
          face.classList.add('mp-deep')
          clearTimeout(rptDeepT)
          rptDeepT = setTimeout(() => face.classList.remove('mp-deep'), 900)
        }
      }
    )
    // 代码块复制按钮（事件委托，EditorPane 同口径：剪贴板 → execCommand 回退）
    function onReportCopyClick(e) {
      const btn = e.target.closest('.code-copy-btn')
      if (!btn) return
      const block = btn.closest('.code-block')
      const pre = block && block.querySelector('pre code')
      if (!pre) return
      const text = pre.textContent
      const done = () => { btn.textContent = '✓'; setTimeout(() => { btn.textContent = '复制' }, 1500) }
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done).catch(() => fallbackCopy(text, done))
      } else {
        fallbackCopy(text, done)
      }
    }
    function fallbackCopy(text, done) {
      const ta = document.createElement('textarea')
      ta.value = text
      ta.style.position = 'fixed'
      ta.style.opacity = '0'
      document.body.appendChild(ta)
      ta.select()
      try { document.execCommand('copy') } catch (e) { }
      document.body.removeChild(ta)
      done()
    }

    function pickReport(r) { emit('report-pick', r.relpath) }
    function backReport() { emit('report-back') }

    return {
      l0, degraded, degradedFresh, feedGroups, feedLen, openMonitor,
      reportCards, reportsLen,
      pillText, pillCls, formatAge, recall_error, shortRun,
      detailItem, queueIndex, queueCount, proposalsLoaded, bodyText, bodyTotal, bodyError, similar,
      canAct, acting, actionError, rsnOpen, rejectReason, doneView, trailRows,
      trailLoading, trailCount, onTrailToggle,
      pillStatus, reasonOk, landedFile, doneMeta, doneLabel,
      reportItem, reportContent, reportError, reportBodyEl, onReportCopyClick,
      adopt, reject, pick, back, close, openSrc, toggleRsn, pickReport, backReport,
      renderMarkdown, CAR,
      TYPE_ICONS, TYPE_ICON_DEFAULT
    }
  },
  template: `
    <!-- L0 主态：纯状态行 + 流水（:key 变化 → 面入场动画重放：opacity + 8px 位移） -->
    <div v-if="!detail && !report" :key="'queue'" data-face="queue" class="c-face anim">
      <div class="p-head">
        <span class="picon"><svg viewBox="0 0 24 24"><circle cx="6" cy="12" r="2.4"/><circle cx="17" cy="6" r="2.4"/><circle cx="17" cy="18" r="2.4"/><path d="M8.2 11 14.8 7.2M8.2 13l6.6 3.8"/></svg></span>
        <span class="ptitle">记忆</span>
        <span class="pcount" v-if="l0.awaiting + l0.adopted + l0.failed > 0">{{ l0.awaiting + l0.adopted + l0.failed }}</span>
        <span class="pacts"><button class="ibtn" title="关闭" @click="close"><svg viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg></button></span>
      </div>
      <!-- L0 行动数 = 纯状态行（整行不可点）；行尾「后台 →」= 唯一 L0→L1 入口；
           failed > 0 → 失败数告警红 + 入口小红点（0 时不渲染 dot） -->
      <div class="l0bar">
        <span class="l0-status"><span>{{ l0.awaiting }} 待审</span><span class="ld">·</span><span>{{ l0.adopted }} 待落地</span><span class="ld">·</span><span :class="{ err: l0.failed > 0 }">{{ l0.failed }} 失败</span><span v-if="degraded > 0" class="l0-deg" :class="{ fresh: degradedFresh }" title="运行期内快照捕捉降级计数（进程内，重启归零）">{{ degraded }} 快照降级</span></span>
        <button class="l0-go" type="button" title="后台监控页 L1（面板收起，监控页接管主区域）" @click="openMonitor"><span v-if="l0.failed > 0" class="dot" aria-hidden="true"></span>后台<svg viewBox="0 0 24 24"><path d="M9 5l7 7-7 7"/></svg></button>
      </div>
      <div class="c-scroll">
        <div class="feed">
          <template v-if="feedLen">
            <template v-for="(grp, gi) in feedGroups.groups" :key="'g' + grp.key">
              <div class="f-day">{{ grp.label }}</div>
              <template v-for="(it, ii) in grp.items" :key="'i' + it.key">
                <div v-if="it.kind === 'awaiting'" class="f-item" :class="{ hot: it.fresh }">
                  <span class="f-dot"></span>
                  <span class="f-time">{{ it.time }}</span>
                  <button class="f-card" :class="{ fresh: it.fresh }" type="button" :title="it.relpath" @click="pick(it)">
                    <div class="f-meta"><span class="tchip"><span v-html="TYPE_ICONS[it.type] || TYPE_ICON_DEFAULT"></span>{{ it.type || 'unknown' }}</span><span class="spill awaiting">awaiting</span></div>
                    <div class="f-title">{{ it.title }}</div>
                  </button>
                </div>
                <div v-else-if="it.kind === 'adopted'" class="f-item" :class="{ last: it.lastItem }">
                  <span class="f-dot"></span>
                  <span class="f-time">{{ it.time }}</span>
                  <button class="f-card" type="button" :title="it.relpath" @click="pick(it)">
                    <div class="f-meta"><span class="tchip"><span v-html="TYPE_ICONS[it.type] || TYPE_ICON_DEFAULT"></span>{{ it.type || 'unknown' }}</span><span class="spill adopted" style="font-size:9.5px;padding:1px 7px">adopted</span></div>
                    <div class="f-title">{{ it.title }}</div>
                  </button>
                </div>
                <div v-else class="f-item f-term" :class="{ last: it.lastItem }">
                  <span class="f-dot"></span>
                  <span class="f-time">{{ it.time }}</span>
                  <button class="f-card f-term" type="button" :title="it.relpath" @click="pick(it)">
                    <div class="f-meta"><span class="tchip"><span v-html="TYPE_ICONS[it.type] || TYPE_ICON_DEFAULT"></span>{{ it.type || 'unknown' }}</span><span class="spill" :class="it.status" style="font-size:9.5px;padding:1px 7px">{{ it.status }}</span></div>
                    <div class="f-title">{{ it.title }}</div>
                  </button>
                </div>
              </template>
            </template>
            <div v-if="feedGroups.more > 0" class="f-more">{{ feedGroups.more }} 条更老 —— 见「后台 →」· 采纳提案</div>
          </template>
          <div v-else class="q-empty">
            <div class="ring"><svg viewBox="0 0 24 24"><path d="M4 13l3-8h10l3 8v6H4z"/><path d="M4 13h5l1.5 2.5h3L15 13h5"/></svg></div>
            <div class="t1">无待审提案</div>
            <div class="t2">系统运行正常 · 后台活动见「后台 →」</div>
          </div>
        </div>
        <!-- 报告区：只读 advisory 通道（index/reports/<alias>/，机器区命名 L2 钉死）；
             仅当有报告时渲染（无常驻空位）；单行 = alias chip + title + 日期（relpath 全值在 hover title）；
             点行 → 报告详情面（第三面） -->
        <div v-if="reportsLen" class="rpt-sec">
          <div class="c-sec-h rpt-h"><span>报告</span><span class="cnt" :class="{ hot: reportsLen > 0 }">{{ reportsLen }}</span><span class="rpt-ln">只读 · 建议不执行</span></div>
          <button v-for="r in reportCards" :key="r.relpath" class="f-card rpt-card" :class="{ fresh: r.fresh }" type="button" :title="r.relpath" @click="pickReport(r)">
            <div class="f-row1"><span class="tchip"><svg viewBox="0 0 24 24"><path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4"/><path d="M9 12h6M9 16h6"/></svg>{{ r.alias }}</span><span class="f-title">{{ r.title || r.alias }}</span><span class="f-date">{{ r.dateShort }}</span></div>
          </button>
        </div>
      </div>
    </div>

    <!-- 报告详情面（第三面）：只读 advisory 全文（markdown 经 renderMarkdown 助手；无动作行——报告无状态机可移，
         回退出口 = 返回队列；深链 state.memoryReport 直达，mp-deep 黄铜下划线 sweep 同详情面口径） -->
    <div v-else-if="report" :key="'report-' + report" data-face="report" class="c-face anim">
      <div class="p-head">
        <button class="ibtn" title="返回队列" @click="backReport"><svg viewBox="0 0 24 24"><path d="M15 5l-7 7 7 7"/></svg></button>
        <span class="ptitle">报告</span>
        <span class="pacts"><button class="ibtn" title="关闭" @click="close"><svg viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg></button></span>
      </div>
      <div class="c-scroll" @click="onReportCopyClick">
        <div v-if="reportContent === null && !reportError" class="q-empty" style="padding-top: 40px">
          <div class="t1">读取中…</div>
          <div class="t2">首次读取后显示全文</div>
        </div>
        <template v-else>
          <div class="d-head">
            <div class="d-chips">
              <span class="tchip"><svg viewBox="0 0 24 24"><path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4"/><path d="M9 12h6M9 16h6"/></svg>{{ (reportItem && reportItem.alias) || '—' }}</span>
              <span class="spill"><span style="font-size:9.5px">只读</span></span>
            </div>
            <div class="d-title">{{ (reportItem && (reportItem.title || reportItem.alias)) || '报告' }}</div>
            <div class="d-path" :title="report">{{ report }}</div>
          </div>
          <div class="d-sec">
            <div class="d-label"><span>正文</span><span class="ln">markdown 全文 · 只读</span></div>
            <div v-if="reportError" class="d-md"><span style="color: #b91c1c">{{ reportError }}</span></div>
            <div v-else-if="reportContent === ''" class="d-md"><span style="color: var(--ink-soft)">（无正文）</span></div>
            <div v-else class="d-md rpt-md" ref="reportBodyEl" v-html="renderMarkdown(reportContent)"></div>
          </div>
        </template>
      </div>
    </div>

    <!-- 详情面：五段材料按判断顺序 + 粘性动作行 + 确认之后子视图 -->
    <div v-else-if="!report" :key="'detail-' + detail" data-face="detail" class="c-face anim">
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
          <!-- (1) 类型 + 状态 + 标题 + 年龄（头部常驻：chip + pill + 标题 + 全路径 + 相对时间） -->
          <div class="d-head">
            <div class="d-chips">
              <span class="tchip"><span v-html="TYPE_ICONS[detailItem.type] || TYPE_ICON_DEFAULT"></span>{{ detailItem.type || 'unknown' }}</span>
              <span class="spill" :class="pillCls(pillStatus || detailItem.status)">{{ pillText(pillStatus || detailItem.status) }}</span>
              <span class="sp"><span class="q-age">{{ formatAge(detailItem.age_days) }}</span></span>
            </div>
            <div class="d-title">{{ detailItem.title }}</div>
            <div class="d-path" :title="detailItem.relpath">{{ detailItem.relpath }}</div>
          </div>

          <!-- (2) 正文：justification 全文 markdown（无截断）；默认展开、可折叠 -->
          <details class="d-sec" open>
            <summary><span class="car" v-html="CAR"></span>正文<span class="ln" v-if="bodyTotal">{{ bodyTotal }} 字</span></summary>
            <div class="body">
              <div class="d-md" v-if="bodyError"><span style="color: #b91c1c">{{ bodyError }}</span></div>
              <div class="d-md" v-else-if="bodyText === null"><span style="color: var(--ink-soft)">读取中…</span></div>
              <div class="d-md" v-else-if="!bodyText"><span style="color: var(--ink-soft)">（无正文）</span></div>
              <div class="d-md" v-else v-html="renderMarkdown(bodyText)"></div>
            </div>
          </details>

          <!-- (3) 来源（可跳转原文）：单行摘要 + 计数，默认收起 -->
          <details v-if="detailItem.source_refs && detailItem.source_refs.length" class="d-sec">
            <summary><span class="car" v-html="CAR"></span>来源<span class="ln">{{ detailItem.source_refs.length }}</span></summary>
            <div class="body">
              <div class="src-list">
                <a v-for="r in detailItem.source_refs" :key="r" class="src-link" :title="r" href="javascript:void(0)" @click="openSrc(r)">
                  <svg viewBox="0 0 24 24"><path d="M14 5h5v5M19 5l-8 8"/><path d="M19 13v5a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h5"/></svg>{{ r }}
                </a>
              </div>
            </div>
          </details>

          <!-- (4) 批次 · 可逆性：summary ln = 批次短写（run-…-0142） -->
          <details class="d-sec">
            <summary><span class="car" v-html="CAR"></span>批次 · 可逆性<span class="ln" :title="detailItem.run_id">{{ shortRun(detailItem.run_id) }}</span></summary>
            <div class="body">
              <div class="run-box">
                <div class="run-line"><span class="rk">批次</span><span class="rv" :title="detailItem.run_id">{{ detailItem.run_id || '—' }}</span></div>
                <div class="run-line"><span class="rk">产出者</span><span class="rv">{{ detailItem.actor || '—' }}</span></div>
                <div class="run-line"><span class="rk">可逆性</span><span class="rv"><span class="spill" :class="detailItem.reversible ? 'adopted' : ''">{{ detailItem.reversible ? '可回滚' : '不可回滚' }}</span></span></div>
                <div class="run-line"><span class="rk">创建</span><span class="rv">{{ detailItem.created || '—' }}</span></div>
              </div>
            </div>
          </details>

          <!-- (5) 相似记忆比对：只留事实（共享来源 / 召回榜在列），不合成相似度分数 -->
          <details class="d-sec">
            <summary><span class="car" v-html="CAR"></span>相似记忆<span class="ln">{{ similar.length ? '共享 ' + similar.length + ' 来源' : '无重叠' }}</span></summary>
            <div class="body">
              <div v-if="similar.length" class="sim-rows">
                <div v-for="s in similar" :key="s.path" class="sim-row">
                  <div class="pp" :title="s.path">{{ s.path }}</div>
                  <div class="sp2">{{ s.sub }}</div>
                </div>
              </div>
              <div v-else class="tr-sub" style="padding: 2px 2px 4px">与 30d 召回榜无重叠（{{ recall_error ? '召回数据暂不可用' : '无共享来源' }}）</div>
            </div>
          </details>

          <!-- (6) 变更轨迹：懒加载（首次展开取 trail）；doneView 时隐藏（确认之后子视图自带轨迹区） -->
          <details v-if="!doneView" class="d-sec" @toggle="onTrailToggle">
            <summary><span class="car" v-html="CAR"></span>变更轨迹<span class="ln">{{ trailLoading ? '…' : (trailCount ? trailCount : '—') }}</span></summary>
            <div class="body">
              <div v-if="trailCount" class="log-trail">
                <div v-for="(row, i) in trailRows" :key="row.seq + '-' + i" class="log-line">
                  <span class="seq">{{ row.seq }}</span><span class="act">{{ row.act }}</span><span class="msg">{{ row.msg }}</span>
                </div>
              </div>
              <div v-else class="tr-sub" style="padding: 2px 2px 4px">{{ trailLoading ? '读取轨迹…' : '无轨迹记录' }}</div>
            </div>
          </details>

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
