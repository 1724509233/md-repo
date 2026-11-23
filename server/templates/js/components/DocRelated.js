/* DocRelated 组件：关联呈现内容（spec 2026-11-23-rel-face-redesign 重设计定稿；原 §6.4 候选① + 2026-11-14 分块版）
 * 两段结构（R1/R6：用户层三块折叠退役合并为单列表；机器层保留独立折叠段）：
 *   用户层 = 图谱出链（wikilink + related）与 tag 中介召回混排单列表——无块题无折叠，
 *     后端 score 降序即终序（R7：显式链接在前、tag 中介 0.5 分沉底，排序已给秩序，chip 负责归因）；
 *     条目 = 两行式（§2）：名行 = 文件名（去 .md，R2）+ tag 条目名行尾挂黄铜弱描边 #tag chip（R5 自明来源），
 *     次行 = 所在目录（mono 弱灰；根目录显示「根目录」三字，R3 行高恒定；
 *     候选列表内 basename 撞名各行次行切「尾部优先」截断——保尾段、前缀 …/，R4 非撞名行不动）。
 *     整行 title = 全路径（tag 条目追加「 · 共享标签：…」全量 tag 名，§2.1）。
 *     头徽标 .pcount = 用户层候选总数（为 0 不渲染，§3；AI 段自带计数，头徽标再挂 0 是噪音）。
 *   「AI 建议」段 = 机器层（黄铜区分：节头 .cnt.hot + 行左缘 2px + 4% 黄铜底；R6 名行同步文件名口径，
 *     但不加目录次行——依据行优先于目录行，它是裁决输入；目录进 title；
 *     依据摘要 1 行 ellipsis + title 全值 + ghost 忽略按钮「忽略」行内右对齐；
 *     无建议/全部排干 → 整段不渲染，无常驻空位；最后一条被忽略 → 整段淡出退场），默认展开
 *   空态：用户层空 → 双行空态（「暂无关联文档」+ 次行小字「用 [[链接]] 或 tag 建立关联，这里会自动出现」，§4）。
 *   旧两块块题（显示名字样）与各自折叠态随合并退役（R1：折叠税 + 弱关联永久隐形；块题即实现 meta）；
 *   fold 初值只剩 ai。
 * 折叠机制 = 项目既有 .fold（grid 0fr→1fr，GPU 友好无 reflow，同 MonitorPage L2 深钻）；chevron .car 旋转 90°
 * 行口径单源 = utils/docRelFormat.js 纯函数（fileName/dupBasenames/dirText/tagChip/rowTitle；
 *   chip 双字段兼容读取——字段缺席期不渲染 chip = 合法，无报错无占位）。
 * 数据：GET /api/memory/doc/{path} 按需读取（{path, links, suggestions}，links 带 type∈{wikilink,related,tag}，
 *       tag 条目携共享标签 tags[]（或单值 tag，后端补字段并行中，缺席期 chip 不渲染）；
 *       suggestions 带 edge_id/basis；D18 死端点 + D20 排干谓词在服务端）；本组件不自建轮询。
 * 忽略：POST /api/memory/relatedness/{edge_id}/ignore（单边否决无确认弹窗；
 *       行 0.2s 淡出移除 → 计数更新 → 最后一条消失后整块淡出；再次进入该建议不再出现 = 服务端 ignored 过滤）
 * 文档跟随：props.path（= 当前文档）变化 → 内容原地换（重新读取），折叠偏好保留（同一次打开内）；
 *       组件随容器开合重挂载（app.js v-if）→ 每次打开回默认展开态。
 * 容器（桌面 ≥1024 fixed 浮层 .rel-side / 平板 861–1023 in-flow 侧栏 / 移动 ≤860 bottom sheet）由 app.js 承载，
 *       本组件只渲染内容 + 动作；行规格三态完全同构（§6，差异全在容器层）。
 * 内容件 = style.css 文档面类（.doc-row/.doc-row-name/.doc-row-dir/.doc-tag/.pcount/.mach/.doc-basis/.doc-ignore/
 *       .c-sec-h/.cnt.hot/.doc-none/.doc-none-sub/.fold），候选① 与控制台方案零分叉。
 */
const { ref, computed, watch, nextTick, onMounted, onBeforeUnmount, getCurrentInstance } = Vue
import { api } from '../api.js'
import { fileName, dupBasenames, dirText, tagChip, rowTitle } from '../utils/docRelFormat.js'

/* 块标题图标（单色线条，stroke: currentColor；.c-sec-h .b-ic 槽 13px）
 * 仅存「AI 建议」块题 = relatedness 图标（.b-ic-hot 黄铜 = 机器层专属强调色）；
 * 用户层块题随合并退役，旧链环/标签两图标随块题一同退场（来源归因由名行 chip 承担）。 */
const IC_REL = '<svg aria-hidden="true" viewBox="0 0 24 24"><circle cx="5" cy="12" r="2.5"/><circle cx="19" cy="12" r="2.5"/><path d="M7.5 12h9"/></svg>'

export const DocRelated = {
  name: 'DocRelated',
  props: {
    path: { type: String, default: null }   // 当前文档路径（null = 未选文档，容器不渲染本组件）
  },
  emits: ['open-doc', 'close'],
  setup(props, { emit }) {
    // 折叠块 id 去重（P2 §1.3）：vendored Vue 3.4.21 无 useId → getCurrentInstance().uid 拼 id，
    // 桌面浮层与移动 sheet 双实例（app.js 各自 v-if）不再撞 fold-* DOM id；aria-controls 同源引用
    const uid = getCurrentInstance().uid
    const foldId = k => `fold-${k}-${uid}`
    const data = ref(null)        // null = 加载中；{path, links, suggestions}
    const error = ref(null)       // 404 = 文档已删 → 死文档态（服务端文案直显）
    const actionError = ref(null) // 忽略失败（404 = 已被他人忽略等）
    const machLeaving = ref(false)  // AI 建议块整块退场淡出（最后一条被忽略后 .2s 移除）
    const faceEl = ref(null)        // 面容器（.rel-face；入场动画 cFaceIn 重放目标）
    // 折叠态只剩 ai（R1：用户层合并为单列表无折叠；2026-11-14 版 wikilink/tag 两块折叠随合并退役）。
    // 组件随容器开合重挂载（app.js v-if）→ 每次打开回默认态；切文档（props.path watch）不重置
    // = 同一次打开内保留用户折叠偏好。
    const fold = ref({ ai: true })
    let loadToken = 0
    let leavingT = null
    let firstPath = true            // 首次 path 观察 = 挂载（不重放入场，挂载动画已覆盖）

    const loading = computed(() =>
      props.path !== null && data.value === null && !error.value)

    // 用户层 = data.links 本体（wikilink + related + tag 中介混排，R1 不分块；R7 后端 score 降序即终序，
    // 前端不二次排序）。userCount = 头徽标 .pcount 计数（为 0 徽标不渲染）。
    const userCount = computed(() => (data.value ? data.value.links.length : 0))
    // R4 同名消歧：候选列表内 basename 撞名集合（随数据一次 computed，行侧 dirFor 查集）。
    // 次行三式（普通目录 / 根目录占位 / 撞名尾部截断）与 chip、title 口径全在 utils/docRelFormat.js。
    const dupNames = computed(() =>
      dupBasenames(data.value ? data.value.links.map(l => l.path) : []))
    function dirFor(p) { return dirText(p, dupNames.value) }

    function toggleFold(key) { fold.value[key] = !fold.value[key] }

    // 面入场 cFaceIn（opacity + 8px 位移 .2s）：桌面浮层 = 开启时（挂载）+ 切文档时原地重放；
    // 移动 sheet = 开启由抽屉滑入承担（内容不重放，mockup 同口径——replayAnim 只作用于桌面 relFace）
    function replayFaceAnim() {
      if (window.innerWidth <= 860 || !faceEl.value) return
      faceEl.value.classList.remove('anim')
      void faceEl.value.offsetWidth
      faceEl.value.classList.add('anim')
    }
    onMounted(() => {
      if (window.innerWidth > 860 && faceEl.value) faceEl.value.classList.add('anim')
    })

    async function load(path) {
      const token = ++loadToken
      data.value = null
      error.value = null
      actionError.value = null
      machLeaving.value = false
      try {
        const d = await api.getDocRelated(path)
        if (token !== loadToken) return
        data.value = d
      } catch (e) {
        if (token === loadToken) error.value = e.message
      }
    }

    // 文档跟随：切文档 → 内容原地换（token 守卫丢弃过期响应）+ 桌面浮层面入场重放（mockup 同口径）；
    // 开合状态在容器（app），折叠偏好在本组件 fold（同一次打开内保留，不随切文档重置）
    watch(
      () => props.path,
      (p) => {
        if (!p) { data.value = null; error.value = null; return }
        const isSwitch = !firstPath
        firstPath = false
        load(p)
        if (isSwitch) nextTick(replayFaceAnim)
      },
      { immediate: true }
    )

    // 忽略 = 单边否决（可逆：agent 再提 → 人再裁；低风险无确认弹窗）→ 行淡出 .2s 移除；
    // 计数随数组收缩更新；最后一条消失 → AI 建议块整块淡出退场（machLeaving 保持块渲染至淡出完成）
    async function ignoreSuggestion(s) {
      if (s.ignoring) return
      s.ignoring = true
      actionError.value = null
      try {
        await api.ignoreRelatedness(s.edge_id)
        s.fading = true
        setTimeout(() => {
          const arr = data.value && data.value.suggestions
          if (!arr) return
          const i = arr.findIndex(x => x.edge_id === s.edge_id)
          if (i < 0) return
          arr.splice(i, 1)
          if (arr.length === 0) {
            machLeaving.value = true
            clearTimeout(leavingT)
            leavingT = setTimeout(() => { machLeaving.value = false }, 200)
          }
        }, 200)
      } catch (e) {
        s.ignoring = false
        actionError.value = e.message
      }
    }

    onBeforeUnmount(() => clearTimeout(leavingT))

    return {
      data, error, actionError, loading, machLeaving, faceEl,
      fold, toggleFold, userCount, dirFor,
      fileName, tagChip, rowTitle,
      ignoreSuggestion, IC_REL, foldId
    }
  },
  template: `
    <div class="rel-face" ref="faceEl">
      <div class="p-head">
        <span class="picon"><svg aria-hidden="true" viewBox="0 0 24 24"><circle cx="5" cy="12" r="2.5"/><circle cx="19" cy="12" r="2.5"/><path d="M7.5 12h9"/></svg></span>
        <span class="ptitle">关联</span>
        <span v-if="userCount" class="pcount">{{ userCount }}</span>
        <span class="pacts"><button class="ibtn" title="关闭" aria-label="关闭" @click="$emit('close')"><svg aria-hidden="true" viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg></button></span>
      </div>
      <div class="rel-scroll">
        <div v-if="loading" class="q-empty" style="padding-top: 40px">
          <div class="t1">读取中…</div>
        </div>
        <div v-else-if="error" class="q-empty" style="padding-top: 40px">
          <div class="t1">该文档已不存在</div>
          <div class="t2">可能已被删除或重命名</div>
        </div>
        <template v-else-if="data">
          <!-- 用户层空态（§4 双行文案：主行中性小字 + 次行行动指引；位置 = 用户层，AI 建议之上） -->
          <div v-if="!userCount" class="c-sec">
            <div class="doc-none">暂无关联文档</div>
            <div class="doc-none-sub">用 [[链接]] 或 tag 建立关联，这里会自动出现</div>
          </div>
          <!-- 用户层单列表（R1 合并：图谱出链 + tag 中介混排，无块题无折叠；R7 后端序即终序。
               行 = 名行（文件名 + 条件 tag chip，R2/R5）+ 次行目录（R3 根目录恒显；R4 撞名切尾部截断）。
               chip 双字段兼容（tags?.[0] ?? tag 在 docRelFormat.tagChip）：字段缺席期不渲染 chip，行仍合法 -->
          <div v-else class="c-sec">
            <button v-for="l in data.links" :key="l.path" type="button" class="doc-row"
              :title="rowTitle(l)" @click="$emit('open-doc', l.path)">
              <div class="doc-row-line">
                <span class="doc-row-name">{{ fileName(l.path) }}</span>
                <span v-if="tagChip(l)" class="doc-tag">#{{ tagChip(l) }}</span>
              </div>
              <div class="doc-row-dir">{{ dirFor(l.path) }}</div>
            </button>
          </div>
          <!-- AI 建议段（机器层，黄铜；无建议/全部排干 → 不渲染；最后一条忽略 → 整块淡出；默认展开。
               R6 仅名行改文件名口径（fileName 同源，不加目录次行——依据行优先），其余零改动 -->
          <div v-if="data.suggestions.length || machLeaving" class="c-sec" :class="{ leaving: machLeaving }">
            <button class="c-sec-h c-sec-fold" type="button" :aria-expanded="fold.ai" :aria-controls="foldId('ai')" @click="toggleFold('ai')">
              <span class="b-ic b-ic-hot"><span v-html="IC_REL"></span></span>
              <span>AI 建议</span>
              <span class="cnt hot">{{ data.suggestions.length }}</span>
              <span class="car"><svg aria-hidden="true" viewBox="0 0 24 24"><path d="M9 5l7 7-7 7"/></svg></span>
            </button>
            <div class="fold" :class="{ open: fold.ai }" :id="foldId('ai')"><div>
              <div v-for="s in data.suggestions" :key="s.edge_id" class="doc-row mach"
                :class="{ fading: s.fading }" :title="s.path">
                <div class="doc-row-line">
                  <span class="doc-row-path">{{ fileName(s.path) }}</span>
                  <button class="doc-ignore" type="button" :disabled="s.ignoring" @click="ignoreSuggestion(s)">{{ s.ignoring ? '…' : '忽略' }}</button>
                </div>
                <div v-if="s.basis" class="doc-basis" :title="s.basis">依据：{{ s.basis }}</div>
              </div>
            </div></div>
          </div>
          <!-- 忽略失败提示（404 = 已被他人忽略等；服务端文案直接展示） -->
          <div v-if="actionError" class="m-err">
            <svg aria-hidden="true" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 8v4"/><path d="M12 16h.01"/></svg>
            <span>{{ actionError }}</span>
          </div>
        </template>
      </div>
    </div>
  `
}
