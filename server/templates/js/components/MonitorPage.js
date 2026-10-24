/* MonitorPage 组件：后台监控页 · L1（定稿设计 design/console-v4-watch.html 场景 02 · 非对称网格 02E）
 * 六区块 row-major：组 1「状态（健康吗）」①九态 ∣ ②异常清单 / 组 2「系统（在干什么）」
 *   ③后台任务 ∣ ④执行器 / ⑤召回（通栏）/ ⑥最近终态（通栏）。
 * 空间：≥1024px 网格 1200px（列比 1:1.6）· ≤1023px 单列零重排（= 场景 04 窄屏回退）。
 * 零态 = 常驻淡显：六网格单元永远在场，计数 = 0 → 卡内容 = 淡显占位（.zline：无异常 / 无终态 / 无召回流量）。
 * 快照口径：30s 轮询 = 无时序数据，刻度条 / 比例条静态，不画动画填充 / 趋势曲线。
 * 数据：state.memoryStatus / state.memoryProposals 由 app 30s 轮询注入（后端成本为零）；
 *   L2 = 全页唯一按需层：点召回卡按钮触发一次 GET /api/recall/stats（docs / failed_queries 各 ≤200 行）。
 * 互斥契约：与记忆控制台面板互斥（app 开页时收起面板；「← 返回文档」不自动重开面板，重入走 rail【通知】）。
 */
const { ref, computed, watch, nextTick, inject } = Vue
import { api } from '../api.js'

/* 九态格序 = 设计稿批准顺序（产品现有 by_status 口径，3×3）；
 * cls：dim = 零态淡显 · hot = awaiting 铜 · err = failed 红 */
const NINE_TILES = [
  { key: 'processing', label: 'Processing' },
  { key: 'awaiting_review', label: 'Awaiting' },
  { key: 'adopted', label: 'Adopted' },
  { key: 'rejected', label: 'Rejected' },
  { key: 'merged', label: 'Merged' },
  { key: 'expired', label: 'Expired' },
  { key: 'failed', label: 'Failed' },
  { key: 'malformed', label: 'Malformed' },
  { key: 'pending', label: 'Pending' }
]

/* 执行器循环周期 30min：lamp 亮 = last_op_ts 在 30 分钟内（循环在跑），其余 = 待机 */
const EXECUTOR_WINDOW_S = 30 * 60

function pad2(n) { return String(n).padStart(2, '0') }
/* epoch 秒 → MM-DD HH:mm（0/空 → ''，副注不渲染） */
function fmtDT(ts) {
  if (!ts) return ''
  const d = new Date(ts * 1000)
  return pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) + ' ' + pad2(d.getHours()) + ':' + pad2(d.getMinutes())
}
/* 日期串（YYYY-MM-DD[THH:MM] 或 datetime 化的 "YYYY-MM-DD HH:MM:SS"）→ MM-DD（提案 created / merged_at 前缀） */
function fmtDay(iso) {
  if (!iso) return ''
  const m = String(iso).match(/(\d{4})-(\d{2})-(\d{2})/)
  return m ? m[2] + '-' + m[3] : String(iso)
}
/* merged_at（PyYAML datetime 化 → "YYYY-MM-DD HH:MM:SS"）→ MM-DD HH:mm（设计稿口径） */
function fmtMerged(s) {
  if (!s) return ''
  const m = String(s).match(/(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/)
  return m ? m[2] + '-' + m[3] + ' ' + m[4] + ':' + m[5] : String(s)
}
/* 上次运行：epoch 秒 → 「26m 前 / 3h 前 / 昨天 10:00」（同 MemoryConsole.relRun 口径） */
function relRun(ts) {
  if (!ts) return '尚未运行'
  const diff = Date.now() / 1000 - ts
  if (diff < 3600) return Math.max(1, Math.floor(diff / 60)) + 'm 前'
  if (diff < 86400) return Math.floor(diff / 3600) + 'h 前'
  if (diff < 86400 * 2) return '昨天 ' + new Date(ts * 1000).toTimeString().slice(0, 5)
  return new Date(ts * 1000).toISOString().slice(0, 10)
}

export const MonitorPage = {
  name: 'MonitorPage',
  emits: ['back'],
  setup(props, { emit }) {
    const state = inject('state')
    const mwrapRef = ref(null)
    // ─── 进入/首次数据编排（动效层；v-show display:none→block 会重启动画 → 每次进入自然重放）───
    // 首载「一次」口径：首次轮询在页面打开前到达 → 开场就是已填充态（数据在场，不演空态）；
    // 打开时仍无数据 → 首份数据到达的那一次 = 首载，条/刻度/灯做一次填充入场。
    let dataSeen = false
    let enterT = null
    watch(
      () => state.memoryStatus,
      (st) => {
        if (st && (st.agents || st.recall || st.executor || st.proposals)) dataSeen = true
      },
      { immediate: true }
    )
    let firstRendered = false
    watch(
      () => state.monitoring,
      async (open) => {
        if (!open) return
        const el = mwrapRef.value
        if (!el) return
        const enter = () => {
          el.scrollTop = 0 // 此刻已重新可见（v-show 提交后）→ 入场从首屏开始
          el.classList.remove('mp-enter')
          void el.offsetWidth // 强制 reflow：连开两次也重放入场
          el.classList.add('mp-enter')
          clearTimeout(enterT)
          enterT = setTimeout(() => el.classList.remove('mp-enter'), 950) // 序列最长 ~560ms，留尾
        }
        if (firstRendered) {
          await nextTick() // 上一轮 v-show 隐藏态已提交 → 重放入场
          enter()
        } else {
          firstRendered = true
          await nextTick() // .mp-enter 类此刻挂载
          if (dataSeen) enter() // 数据在场 → 开场即入场
          else {
            // 等首份数据到达再入场（仅打开时仍无数据的路径触发）
            let t = 0
            const iv = setInterval(() => {
              t += 20
              if (dataSeen) { clearInterval(iv); enter() }
              else if (t >= 1500) { clearInterval(iv) } // 数据缺席 → 不入场，zline 静态呈现
            }, 20)
          }
        }
      }
    )


    // ─── ① 九态：by_status 九 key 恒定全量（8 状态 + malformed）；格序 = 设计稿 ───
    const byStatus = computed(() =>
      (state.memoryStatus && state.memoryStatus.proposals && state.memoryStatus.proposals.by_status) || {})
    const nine = computed(() => NINE_TILES.map(t => {
      const v = byStatus.value[t.key] || 0
      return { ...t, v,
        cls: v === 0 ? 'dim' : (t.key === 'awaiting_review' ? 'hot' : t.key === 'failed' ? 'err' : '') }
    }))

    // ─── ② 异常清单 = status failed（红边卡只在 failed > 0 时强调）；
    //     行尾 trail 摘要 = 列表项新字段 trail（最新 3 条 {seq,ts,source,op,path}，fixer 车道交付中，
    //     缺省 → 空，不用 mock 文案顶替）；trail_count > 3 → 尾注「共 N 条」─────
    const failedCount = computed(() => byStatus.value.failed || 0)
     const anomalies = computed(() =>
      (state.memoryProposals || [])
        .filter(p => p.status === 'failed')
        .map(p => ({
          ...p,
          dt: fmtDay(p.created),
          trail: Array.isArray(p.trail) ? [...p.trail].sort((a, b) => (a.seq || 0) - (b.seq || 0)) : [],
          trail_count: p.trail_count || 0
        })))

    // ─── 新告警强调（轮询发现 failed 数增长或出现新的 failed 提案 → 红边卡一次环脉冲 + 计数 pill pop；
    //     复用 softPulse 家族的克制节奏，动画结束自清；首次数据到达只建基线不闪，同 L0 fresh 口径）─────
    let alertBase = null
    const alertFresh = ref(false)
    let alertT = null
    watch(anomalies, (list) => {
      const set = new Set(list.map(p => p.relpath))
      if (alertBase === null) { alertBase = set; return }
      const grew = [...set].some(r => !alertBase.has(r))
      if (grew) {
        alertFresh.value = true
        clearTimeout(alertT)
        alertT = setTimeout(() => { alertFresh.value = false }, 1400)
      }
      alertBase = set
    })

    // ─── ③ 后台任务 = 分段刻度条：实心段 = 当前 arm，总段数 = 目标 armed_target。
    //     降级口径（armed_target 端点恒 null）：按当前 arm 画实心段；arm = 0 → 1 段虚线占位
    //     （不放满 10 段假刻度——「总段数 = 目标」的语义在目标未知时不成立）；
    //     目标值标注「取自 config」（端点不可见前端 config，文案照设计稿口径）─────
    const agents = computed(() =>
      ((state.memoryStatus && state.memoryStatus.agents) || []).map(a => {
        const armed = a.armed || 0
        const target = a.armed_target != null ? a.armed_target : null
        return { ...a, armed, target,
          total: target != null ? Math.max(target, 1) : Math.max(armed, 1),
          filled: target != null ? Math.min(armed, target) : armed,
          tg: 'arm ' + armed + (target != null ? '/' + target : '') }
      }))

    // ─── ③ 降级口径触发器：任一 agent 的目标 / 日上限为 null → 页头标注「取自 config」；
    //     非 null 时（bg_targets 已接线）标注真实来源 ───
    const capsDegraded = computed(() => agents.value.some(a => a.target == null || a.runs_cap == null))

    // ─── ④ 执行器 = 大数字（ops_today）+ armed 状态灯（lamp 亮 = last_op_ts 在 30 分钟内，
    //     执行器循环周期 30min）；status.executor 新块 fixer 车道交付中 —— 缺省或全零（{0,0,null}）→ zline ───
    const executor = computed(() => {
      const ex = state.memoryStatus && state.memoryStatus.executor
      if (!ex) return null
      const ops = ex.ops_today || 0
      const lastTs = ex.last_op_ts || 0
      if (!ops && !lastTs && !ex.last_batch) return null
      return {
        ops,
        lamp: lastTs > 0 && (Date.now() / 1000 - lastTs) <= EXECUTOR_WINDOW_S,
        last: fmtDT(lastTs),
        last_batch: ex.last_batch || '',
        adopted: byStatus.value.adopted || 0
      }
    })

    // ─── ⑤ 召回 = top5 横条比例图：宽度 ∝ weighted（按 top5 最大值归一，值标条尾兜底）；
    //     静态比例条（30s 轮询 = 快照，无时序，不画趋势曲线）。
    //     recall_error 出现（表缺失降级）→ 卡内淡显占位「召回遥测暂不可用」，不 500 不隐藏卡 ───
    const recall = computed(() => (state.memoryStatus && state.memoryStatus.recall) || {})
    const recallError = computed(() => !!recall.value.recall_error)
    const recallTop = computed(() => (recall.value.top || []).slice(0, 5))
    const recallMax = computed(() => recallTop.value.reduce((m, t) => Math.max(m, t.weighted || 0), 0))
    const recallSum = computed(() => recallTop.value.reduce((s, t) => s + (t.weighted || 0), 0))
    const wLines = computed(() => recallTop.value.map(t => ({
      ...t,
      pct: recallMax.value > 0 ? (t.weighted || 0) / recallMax.value * 100 : 0,
      w: (t.weighted || 0).toFixed(1),
      lh: t.last_hit ? 'last_hit ' + fmtDT(t.last_hit) : ''
    })))
    const allZeroRecall = computed(() =>
      !recallError.value && !recall.value.failed_7d && !recall.value.docs_tracked && recallTop.value.length === 0)

    // L2 = 全页唯一按需层：按钮触发一次 GET /api/recall/stats（0fr→1fr 展开，aria-expanded 同步）；
    // docs 行附真实 counts.human/on_demand/synthetic 细分列（增强，超出设计稿三列形态）；
    // failed 行「7d 次数」= 窗口内同 query 出现次数（≤200 行内口径）
    const l2Open = ref(false)
    const l2Loading = ref(false)
    const l2Error = ref(null)
    const l2Data = ref(null)   // 原始端点对象 {docs, failed_queries, failed_count, ...}
    async function toggleL2() {
      l2Open.value = !l2Open.value
      if (l2Open.value && !l2Data.value && !l2Loading.value) {
        l2Loading.value = true
        l2Error.value = null
        try {
          l2Data.value = await api.getRecallStats()
        } catch (e) {
          l2Error.value = e.message
        } finally {
          l2Loading.value = false
        }
      }
    }
    const l2FailedRows = computed(() => {
      const rows = (l2Data.value && l2Data.value.failed_queries) || []
      const count = {}
      for (const r of rows) count[r.query] = (count[r.query] || 0) + 1
      return rows.map(r => ({ ...r, dt: fmtDT(r.ts), n: count[r.query] }))
    })

    // ─── ⑥ 最近终态 = merged / rejected 按 merged_at/created 降序取 3（文字行；
    //     终态 = 事件非比例，不图形化；failed 不入此区——已在②异常清单）─────
    const finals = computed(() =>
      (state.memoryProposals || [])
        .filter(p => p.status === 'merged' || p.status === 'rejected')
        .sort((a, b) => String(b.merged_at || b.created || '').localeCompare(String(a.merged_at || a.created || '')))
        .slice(0, 3)
        .map(p => ({ ...p, dt: fmtDay(p.created), mergedDisp: fmtMerged(p.merged_at) })))

    function back() { emit('back') }

    return {
      nine, failedCount, anomalies, agents, capsDegraded, executor,
      recall, recallError, recallSum, recallMax, wLines, allZeroRecall,
      l2Open, l2Loading, l2Error, l2Data, l2FailedRows, toggleL2,
      finals, back, fmtDT, relRun,
      mwrapRef, alertFresh
    }
  },
  template: `
    <div ref="mwrapRef" class="mwrap">
      <div class="m-scroll">
        <div class="mpage grid">
          <div class="mp-head">
            <!-- 单出口：回文档视图；面板不自动重开（重入走 rail【通知】） -->
            <button class="mp-back" type="button" title="返回文档视图（面板不自动重开，重入走 rail【通知】）" @click="back">
              <svg viewBox="0 0 24 24"><path d="M15 5l-7 7 7 7"/></svg>返回文档</button>
            <div class="mp-title">记忆运行时 · 后台监控</div>
            <div class="mp-meta">30s 轮询缓存 · /api/memory/status + /api/memory/proposals · L0/L1 后端成本为零<br><template v-if="capsDegraded">armed_target / runs_cap null → 目标值取自 config</template><template v-else>目标值 = config（trigger.arm_count / budget_runs_per_day）</template></div>
          </div>

          <!-- 组 1「状态」= 页面第一问「健康吗」（事件驱动访问落在首屏内） -->
          <div class="mgroup">状态<span class="q">健康吗</span></div>

          <!-- ① 九态：正交变量锁定，不图形化（计数已是最小图形）；窄列，内容顶部对齐 -->
          <div class="mcard">
            <div class="mc-h"><span class="mc-t">九态</span><span class="mc-poll">by_status · 8 状态 + malformed · 30s 轮询</span></div>
            <div class="s9">
              <div v-for="t in nine" :key="t.key" class="s9c" :class="t.cls">
                <div class="k" :title="t.key">{{ t.label }}</div>
                <div class="v">{{ t.v }}</div>
              </div>
            </div>
            <div class="mc-note">顺序 = 产品现有 by_status 口径 · 零态格淡显 / awaiting 铜 / failed 红 · 区块计数 = 0 → 内容行淡显占位（<i>.zline</i>），卡片常驻</div>
          </div>

          <!-- ② 异常清单 = 红边卡（「需要被看见」的区块的图形处理）；宽列：trail 长路径需宽 -->
          <div class="mcard" :class="{ alert: failedCount > 0, 'mp-fresh-alert': alertFresh }">
            <div class="mc-h"><span class="mc-t">异常清单</span><span class="mc-poll">failed · trail 按需取</span>
              <span v-if="failedCount > 0" class="alert-pill" :class="{ 'mp-pill-pop': alertFresh }">{{ failedCount }} failed</span></div>
            <template v-if="anomalies.length">
              <div v-for="a in anomalies" :key="a.relpath" class="ano-row"
                :style="{ borderColor: 'color-mix(in srgb, var(--st-error) 35%, var(--line))' }">
                <div class="ano-top">
                  <span class="tchip">{{ a.type || 'unknown' }}</span>
                  <span class="spill failed" style="font-size:9.5px;padding:1px 7px">failed</span>
                  <span class="tt" :title="a.relpath">{{ a.title }}</span>
                  <span class="dt" v-if="a.dt">{{ a.dt }}</span>
                </div>
                <div v-if="a.trail.length" class="trail-mini">
                  <div v-for="t in a.trail" :key="t.seq" class="tline">
                    <span class="op">#{{ t.seq }} · {{ t.source }}</span>
                    <span class="tp" :title="t.op + ' · ' + t.path">{{ t.op }} · {{ t.path }}</span>
                  </div>
                  <span v-if="a.trail_count > 3" class="tcount">共 {{ a.trail_count }} 条</span>
                </div>
              </div>
            </template>
            <div v-else class="zline">无异常</div>
            <div class="mc-note">红边 = 区块级强调（failed &gt; 0）· trail = 列表项新字段（最新 3 条，<i>trail / trail_count</i>）· 全零 → 内容行淡显占位「无异常」</div>
          </div>

          <!-- 组 2「系统」= 页面第二问「在干什么」 -->
          <div class="mgroup">系统<span class="q">在干什么</span></div>

          <!-- ③ 后台任务 = 分段刻度条（静态快照：30s 轮询无时序数据，不画动画填充 / 趋势） -->
          <div class="mcard">
            <div class="mc-h"><span class="mc-t">后台任务</span><span class="mc-poll">trigger_state · 目标取自 config</span></div>
            <template v-if="agents.length">
              <div v-for="a in agents" :key="a.alias" class="seg-row">
                <div class="seg-top"><span class="nm">{{ a.alias }}</span><span class="tg">{{ a.tg }}</span></div>
                <div class="segs">
                  <i v-for="i in a.total" :key="i" class="seg" :class="{ f: i <= a.filled, e: i > a.filled }"></i>
                </div>
                <div class="seg-sub">
                  <span>上次 {{ relRun(a.last_run) }}</span><span class="sp"></span>
                  <span>今天 {{ a.runs_today }}<template v-if="a.runs_cap != null">/{{ a.runs_cap }} 次</template><template v-else> 次 · cap 取自 config</template></span>
                </div>
              </div>
            </template>
            <div v-else class="zline">无后台智能体</div>
            <div class="mc-note">实心段 = 当前 arm · 总段数 = 目标 armed_target（null → 按 arm 画，arm = 0 时 1 段虚线占位，目标值取自 config）· 细信息（上次 / 今天次数）图形放不下，仍走 mono 注脚行 · <i>trigger_state.armed / last_run / runs_today</i> 原字段</div>
          </div>

          <!-- ④ 执行器 = 大数字（待落地计数）+ armed 状态灯；网格行内等高，exec-vis 垂直居中 -->
          <div class="mcard">
            <div class="mc-h"><span class="mc-t">执行器</span><span class="mc-poll">30min 循环 · 非 LLM</span></div>
            <div v-if="executor" class="exec-vis">
              <span class="lamp" :class="{ on: executor.lamp }" :title="executor.lamp ? 'armed · 运行中' : 'armed · 待机'"></span>
              <span class="big">{{ executor.ops }}</span>
              <span class="cap-line"><b>executor</b> · 30min 循环 · <template v-if="executor.last">last_op {{ executor.last }}<br></template><template v-else>尚未执行<br></template>{{ executor.adopted }} 条 adopted 待落地 · 计数 = <i>by_status.adopted</i><template v-if="executor.last_batch"><br>last_batch {{ executor.last_batch }}</template></span>
            </div>
            <div v-else class="zline">无执行器活动</div>
            <div class="mc-note">大数字 = 待落地计数（status.executor.ops_today）· 状态灯 = last_op_ts 在 30 分钟内（循环周期 30min，脉冲 = 在跑）· 确定性 30 分钟周期：扫描「adopted 待落地」或「14d 超期提案」落地，保留旧版（可回滚）</div>
          </div>

          <!-- ⑤ 召回 = top5 横条比例图（通栏：L2 表格横向滚动需求下降） -->
          <div class="mcard full">
            <div class="mc-h"><span class="mc-t">召回</span><span class="mc-poll">30d 半衰期 · L2 = 全页唯一按需层</span></div>
            <template v-if="!recallError">
              <div class="rcl-stats">
                <div class="rcl-stat"><div class="k">7d 无命中</div><div class="v">{{ recall.failed_7d || 0 }}</div></div>
                <div class="rcl-stat"><div class="k">在轨文档</div><div class="v">{{ recall.docs_tracked || 0 }}</div></div>
                <div class="rcl-stat"><div class="k">top5 加权</div><div class="v">{{ recallSum.toFixed(1) }}</div></div>
              </div>
              <div v-if="wLines.length" class="wbar">
                <div class="wbar-h">top5 · 宽度 ∝ weighted（最大 {{ recallMax.toFixed(1) }} = 100%）· 静态比例条，无时序</div>
                <div v-for="t in wLines" :key="t.path" class="wline">
                  <span class="p" :title="t.path">{{ t.path }}<span v-if="t.lh" class="lh">{{ t.lh }}</span></span>
                  <span class="bar"><i :style="{ width: t.pct + '%' }"></i></span>
                  <span class="v">{{ t.w }}</span>
                </div>
              </div>
              <div v-else-if="allZeroRecall" class="zline">无召回流量</div>
            </template>
            <div v-else class="zline">召回遥测暂不可用（表缺失降级）</div>
            <button v-if="!recallError" class="rcl-l2btn" type="button"
              :aria-expanded="l2Open" aria-controls="mp-fold-l2"
              title="L2 深钻：/api/recall/stats 按需 GET（docs / failed_queries 各 cap 200 行）"
              @click="toggleL2">
              <span class="car"><svg viewBox="0 0 24 24"><path d="M9 5l7 7-7 7"/></svg></span>L2 深钻 · docs / failed_queries 各 ≤200 行</button>
            <div class="fold" :class="{ open: l2Open }" id="mp-fold-l2">
              <div>
                <div class="l2-pad">
                  <div v-if="l2Error" class="tcount" style="align-self:flex-start">GET /api/recall/stats → {{ l2Error }}</div>
                  <div v-else-if="l2Loading">加载 L2…</div>
                  <template v-else>
                    <div v-if="!l2Data" class="zline">尚未加载（点击 L2 按钮触发一次 GET）</div>
                    <template v-else>
                      <div class="l2-sub">召回文档榜<span class="cap">/api/recall/stats → docs · 生产 ≤200 行 · 细分 = 真实 counts 字段</span></div>
                      <div class="mtable">
                        <table>
                          <thead><tr><th>文档</th><th>human</th><th>on_demand</th><th>synthetic</th><th>加权</th><th>last_hit</th></tr></thead>
                          <tbody>
                            <tr v-if="!l2Data.docs.length"><td colspan="6">窗口内无召回流量</td></tr>
                            <tr v-for="d in l2Data.docs" :key="d.path">
                              <td :title="d.path">{{ d.path }}</td>
                              <td>{{ (d.counts && d.counts.human) || 0 }}</td>
                              <td>{{ (d.counts && d.counts.on_demand) || 0 }}</td>
                              <td>{{ (d.counts && d.counts.synthetic) || 0 }}</td>
                              <td><span class="w">{{ (d.weighted || 0).toFixed(1) }}</span></td>
                              <td>{{ fmtDT(d.last_hit) || '—' }}</td>
                            </tr>
                          </tbody>
                        </table>
                      </div>
                      <div class="l2-sub">失败查询<span class="cap">failed_queries · 生产 ≤200 行（窗口 {{ l2Data.window_days != null ? l2Data.window_days : 30 }}d）· 7d 次数 = 窗口内同 query 计数</span></div>
                      <div class="mtable">
                        <table>
                          <thead><tr><th>query</th><th>最近</th><th>7d 次数</th></tr></thead>
                          <tbody>
                            <tr v-if="!l2FailedRows.length"><td colspan="3">窗口内无失败查询</td></tr>
                            <tr v-for="(q, i) in l2FailedRows" :key="q.id != null ? q.id : i">
                              <td :title="q.query">{{ q.query }}</td>
                              <td>{{ q.dt }}</td>
                              <td>{{ q.n }}</td>
                            </tr>
                          </tbody>
                        </table>
                      </div>
                    </template>
                  </template>
                  <div class="mc-note">比例条 = 静态（30s 轮询 = 快照，无时序，不画趋势曲线）· L2 = 全页唯一按需层：点本按钮触发一次 <i>GET /api/recall/stats</i> · 全零 → 统计行淡显占位「无召回流量」</div>
                </div>
              </div>
            </div>
          </div>

          <!-- ⑥ 最近终态 = 文字行（终态 = 事件非比例，不图形化——沿用 02D 判断） -->
          <div class="mcard full">
            <div class="mc-h"><span class="mc-t">最近终态</span><span class="mc-poll">proposals 终态 · 倒序</span></div>
            <div v-if="finals.length" class="rev-rows">
              <div v-for="p in finals" :key="p.relpath" class="rev-row">
                <div class="rev-top">
                  <span class="tchip">{{ p.type || 'unknown' }}</span>
                  <span class="tt" :title="p.relpath">{{ p.title }}</span>
                  <span class="dt" v-if="p.dt">{{ p.dt }}</span>
                </div>
                <div class="rev-sub">
                  <span class="spill" :class="p.status">{{ p.status }}</span>&nbsp;
                  <template v-if="p.status === 'merged'">merged_at {{ p.mergedDisp || '—' }}</template>
                  <template v-else>理由：{{ p.reject_reason || '—' }}</template>
                </div>
              </div>
            </div>
            <div v-else class="zline">无终态</div>
            <div class="mc-note">取 <i>/proposals</i> merged / rejected 最近 3 条（failed 另列②异常清单）· 单条完整轨迹在终审详情（trail）· 全零 → 内容行淡显占位「无终态」</div>
          </div>

        </div>
      </div>
    </div>
  `
}
