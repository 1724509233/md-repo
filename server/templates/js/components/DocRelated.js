/* DocRelated 组件：关联呈现内容（spec §6.4 候选①，2026-11-14：预览区侧栏 + 移动 bottom sheet）
 * 三块纵向堆叠，全部可折叠（2026-11-14 用户拍板：tag 与 wikilink 分块 + 各块折叠 + 机器建议改名 AI 建议）：
 *   块1「Wikilink」= 显式用户链接（wikilink + related；related 罕见，同归显式层），块标题带链环图标，默认展开
 *   块2「AI 建议」 = 机器层（黄铜区分：节头 .cnt.hot + 行左缘 2px + 4% 黄铜底；块标题带 relatedness 图标黄铜；
 *         依据摘要 1 行 ellipsis + title 全值 + ghost 忽略按钮「忽略」行内右对齐；
 *         无建议/全部排干 → 整块不渲染，无常驻空位；最后一条被忽略 → 整块淡出退场），默认展开
 *   块3「Tag」 = 隐式共享 tag（doc→tag→doc 中介，最弱），块标题带标签图标，默认收起
 *   图标 = 关联类型，放块标题（.c-sec-h .b-ic）；文档名行恢复全宽（移除上轮每行图标，不影响名展示宽度）
 *   空态：无数据的子块 → 不渲染该块（无常驻空位，与机器层同口径）；wikilink 与 tag 皆空 → 单行「本文档没有出链」
 * 折叠机制 = 项目既有 .fold（grid 0fr→1fr，GPU 友好无 reflow，同 MonitorPage L2 深钻）；chevron .car 旋转 90°
 * 数据：GET /api/memory/doc/{path} 按需读取（{path, links, suggestions}，links 带 type∈{wikilink,related,tag}；
 *       suggestions 带 edge_id/basis；D18 死端点 + D20 排干谓词在服务端）；本组件不自建轮询。
 * 忽略：POST /api/memory/relatedness/{edge_id}/ignore（单边否决无确认弹窗；
 *       行 0.2s 淡出移除 → 计数更新 → 最后一条消失后整块淡出；再次进入该建议不再出现 = 服务端 ignored 过滤）
 * 文档跟随：props.path（= 当前文档）变化 → 内容原地换（重新读取），折叠偏好保留（同一次打开内）；
 *       组件随侧栏开合重挂载（app.js v-if）→ 每次打开回到默认折叠态（wikilink/AI 建议展开，tag 收起）。
 * 容器（桌面 280px 侧栏 .rel-side / 移动 ≤860px bottom sheet .rel-sheet）由 app.js 承载，本组件只渲染内容 + 动作。
 * 内容件 = style.css 文档面既有类（.doc-row/.mach/.doc-basis/.doc-ignore/.c-sec-h/.cnt.hot/.doc-path-line/.doc-none/.fold），
 *       候选① 与控制台方案零分叉（设计稿 2026-11-14 取舍说明「内容件零分叉」）。
 */
const { ref, computed, watch, nextTick, onMounted, onBeforeUnmount } = Vue
import { api } from '../api.js'

/* 块标题图标（单色线条，stroke: currentColor；.c-sec-h .b-ic 槽 13px）
 * 图标 = 关联类型，放块标题（2026-11-14 用户拍板：分块 + 图标进标题，文档名行恢复全宽）。
 * 黄铜仍为机器层（AI 建议）专属强调色（.b-ic-hot）；用户层（wikilink/tag）走中性墨。
 *   wikilink → 链环（正文 [[wikilink]] 显式链接）
 *   tag      → 标签（共享 tag 的 doc→tag→doc 隐式中介，最弱）
 *   AI 建议  → relatedness（机器发现，黄铜） */
const IC_LINK = '<svg aria-hidden="true" viewBox="0 0 24 24"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>'
const IC_TAG = '<svg aria-hidden="true" viewBox="0 0 24 24"><path d="M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z"/><line x1="7" y1="7" x2="7.01" y2="7"/></svg>'
const IC_REL = '<svg aria-hidden="true" viewBox="0 0 24 24"><circle cx="5" cy="12" r="2.5"/><circle cx="19" cy="12" r="2.5"/><path d="M7.5 12h9"/></svg>'

export const DocRelated = {
  name: 'DocRelated',
  props: {
    path: { type: String, default: null }   // 当前文档路径（null = 未选文档，容器不渲染本组件）
  },
  emits: ['open-doc', 'close'],
  setup(props, { emit }) {
    const data = ref(null)        // null = 加载中；{path, links, suggestions}
    const error = ref(null)       // 404 = 文档已删 → 死文档态（服务端文案直显）
    const actionError = ref(null) // 忽略失败（404 = 已被他人忽略等）
    const machLeaving = ref(false)  // AI 建议块整块退场淡出（最后一条被忽略后 .2s 移除）
    const faceEl = ref(null)        // 面容器（.rel-face；入场动画 cFaceIn 重放目标）
    // 三块折叠态（2026-11-14 用户拍板：wikilink / AI 建议默认展开，tag 默认收起）。
    // 组件随侧栏开合重挂载（app.js v-if）→ 每次打开回默认态；切文档（props.path watch）不重置
    // = 同一次打开内保留用户折叠偏好。
    const fold = ref({ wikilink: true, ai: true, tag: false })
    let loadToken = 0
    let leavingT = null
    let firstPath = true            // 首次 path 观察 = 挂载（不重放入场，挂载动画已覆盖）

    const loading = computed(() =>
      props.path !== null && data.value === null && !error.value)

    // 分块（2026-11-14 用户拍板：tag 与 wikilink 分块）：
    //   wikilink 块 = 显式用户链接（wikilink + related；related 罕见，同归显式层，非 tag 即入此块）
    //   tag 块 = 隐式共享 tag（最弱，默认收起）
    const wikiLinks = computed(() => data.value ? data.value.links.filter(l => l.type !== 'tag') : [])
    const tagLinks = computed(() => data.value ? data.value.links.filter(l => l.type === 'tag') : [])
    // 用户层空态：wikilink 与 tag 皆空 → 单行「本文档没有出链」（AI 建议块不受此影响）
    const noOutLinks = computed(() => data.value && wikiLinks.value.length === 0 && tagLinks.value.length === 0)

    function toggleFold(key) { fold.value[key] = !fold.value[key] }

    // 面入场 cFaceIn（opacity + 8px 位移 .2s）：桌面侧栏 = 开启时（挂载）+ 切文档时原地重放；
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

    // 文档跟随：切文档 → 内容原地换（token 守卫丢弃过期响应）+ 桌面侧栏面入场重放（mockup 同口径）；
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
      fold, toggleFold, wikiLinks, tagLinks, noOutLinks,
      ignoreSuggestion, IC_LINK, IC_TAG, IC_REL
    }
  },
  template: `
    <div class="rel-face" ref="faceEl">
      <div class="p-head">
        <span class="picon"><svg aria-hidden="true" viewBox="0 0 24 24"><circle cx="5" cy="12" r="2.5"/><circle cx="19" cy="12" r="2.5"/><path d="M7.5 12h9"/></svg></span>
        <span class="ptitle">关联</span>
        <span class="pacts"><button class="ibtn" title="关闭" aria-label="关闭" @click="$emit('close')"><svg aria-hidden="true" viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg></button></span>
      </div>
      <div class="doc-path-line" :title="path">{{ path }}</div>
      <div class="rel-scroll">
        <div v-if="loading" class="q-empty" style="padding-top: 40px">
          <div class="t1">读取中…</div>
        </div>
        <div v-else-if="error" class="q-empty" style="padding-top: 40px">
          <div class="t1">该文档已不存在</div>
          <div class="t2">可能已被删除或重命名</div>
        </div>
        <template v-else-if="data">
          <!-- 用户层空态：wikilink 与 tag 皆空 → 单行中性小字（位置 = 用户层，AI 建议之上） -->
          <div v-if="noOutLinks" class="c-sec"><div class="doc-none">本文档没有出链</div></div>
          <!-- 块1 Wikilink（显式用户链接 wikilink+related；空 → 不渲染；默认展开） -->
          <div v-if="wikiLinks.length" class="c-sec">
            <button class="c-sec-h c-sec-fold" type="button" :aria-expanded="fold.wikilink" aria-controls="fold-wiki" @click="toggleFold('wikilink')">
              <span class="b-ic"><span v-html="IC_LINK"></span></span>
              <span>Wikilink</span>
              <span class="cnt">{{ wikiLinks.length }}</span>
              <span class="car"><svg aria-hidden="true" viewBox="0 0 24 24"><path d="M9 5l7 7-7 7"/></svg></span>
            </button>
            <div class="fold" :class="{ open: fold.wikilink }" id="fold-wiki"><div>
              <button v-for="l in wikiLinks" :key="l.path" type="button" class="doc-row"
                :title="l.path" @click="$emit('open-doc', l.path)">
                <div class="doc-row-line"><span class="doc-row-path">{{ l.path }}</span></div>
              </button>
            </div></div>
          </div>
          <!-- 块2 AI 建议（机器层，黄铜；无建议/全部排干 → 不渲染；最后一条忽略 → 整块淡出；默认展开） -->
          <div v-if="data.suggestions.length || machLeaving" class="c-sec" :class="{ leaving: machLeaving }">
            <button class="c-sec-h c-sec-fold" type="button" :aria-expanded="fold.ai" aria-controls="fold-ai" @click="toggleFold('ai')">
              <span class="b-ic b-ic-hot"><span v-html="IC_REL"></span></span>
              <span>AI 建议</span>
              <span class="cnt hot">{{ data.suggestions.length }}</span>
              <span class="car"><svg aria-hidden="true" viewBox="0 0 24 24"><path d="M9 5l7 7-7 7"/></svg></span>
            </button>
            <div class="fold" :class="{ open: fold.ai }" id="fold-ai"><div>
              <div v-for="s in data.suggestions" :key="s.edge_id" class="doc-row mach"
                :class="{ fading: s.fading }" :title="s.path">
                <div class="doc-row-line">
                  <span class="doc-row-path">{{ s.path }}</span>
                  <button class="doc-ignore" type="button" :disabled="s.ignoring" @click="ignoreSuggestion(s)">{{ s.ignoring ? '…' : '忽略' }}</button>
                </div>
                <div v-if="s.basis" class="doc-basis" :title="s.basis">依据：{{ s.basis }}</div>
              </div>
            </div></div>
          </div>
          <!-- 块3 Tag（隐式共享 tag，最弱；空 → 不渲染；默认收起） -->
          <div v-if="tagLinks.length" class="c-sec">
            <button class="c-sec-h c-sec-fold" type="button" :aria-expanded="fold.tag" aria-controls="fold-tag" @click="toggleFold('tag')">
              <span class="b-ic"><span v-html="IC_TAG"></span></span>
              <span>Tag</span>
              <span class="cnt">{{ tagLinks.length }}</span>
              <span class="car"><svg aria-hidden="true" viewBox="0 0 24 24"><path d="M9 5l7 7-7 7"/></svg></span>
            </button>
            <div class="fold" :class="{ open: fold.tag }" id="fold-tag"><div>
              <button v-for="l in tagLinks" :key="l.path" type="button" class="doc-row"
                :title="l.path" @click="$emit('open-doc', l.path)">
                <div class="doc-row-line"><span class="doc-row-path">{{ l.path }}</span></div>
              </button>
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
