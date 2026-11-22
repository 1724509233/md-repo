/* BubblePeek 组件（沉浸式壳 T7，spec §4A / 预览稿场景 2·8）：右缘暗金半隐握把 + 管家气泡栈——桌面专属。
   握把 = 30px 暗金圆钮一半探出右缘（right:-15px，CSS .grip），纸色线性领结（ICONS.bowtie 单点）居可见半区；
   pill = 铃铛未读 + 管家回复合计（纸底黄铜字 .gpill）。hover 握把/气泡浮出气泡栈
   （buildPeek 纯函数：最近 3 条摘要 + 「还有 N 条」）；点气泡 emit('pick', item)、点握把 emit('expand')。
   挂载条件 v-if="isDesktop" 在 app 模板（≤1023px 零触达红线）；visible = !aiOpen（浮窗开着握把让位）。
   docMissing 为契约保留位（V1 无行为分支；演进位 = 当前文档态参与摘要渲染时消费）。 */
const { ref, computed, inject } = Vue
import { ICONS } from '../utils/icons.js'
import { fmtRel, fmtMD } from '../utils/format.js'
import { buildPeek } from '../utils/peekFeed.js'

export const BubblePeek = {
  name: 'BubblePeek',
  props: {
    unread: { type: Number, default: 0 },      // 铃铛未读（state.unreadCount 下传）
    replies: { type: Array, default: () => [] }, // state.peekReplies（浮窗关时完成的管家回话）
    docMissing: { type: Boolean, default: false },
    visible: { type: Boolean, default: true }
  },
  emits: ['expand', 'pick'],
  setup(props) {
    // 通知源 = app 全局态（栈只渲摘要；数量分解/明细/台账全归铃铛下拉——摘要归摘要、详情归详情）
    const state = inject('state', null)
    const hover = ref(false)
    let closeT = null
    // 迟滞收起：握把⇄气泡栈间隙 150ms 内到达不闪收（指针桥的极简形态——两簇相邻，无扇区几何）
    function arm() { clearTimeout(closeT); hover.value = true }
    function disarm() { clearTimeout(closeT); closeT = setTimeout(() => { hover.value = false }, 150) }
    const total = computed(() => (props.unread || 0) + (props.replies || []).length)
    const stack = computed(() => buildPeek(state ? state.notifications : [], props.replies, 3))
    function bt(ts) {
      // fmtRel null（≥2 天前）→ 日期兜底（format.js 契约：调用方自组，禁本地拷贝）
      const r = fmtRel(ts)
      return r == null ? fmtMD(ts) : r
    }
    return { hover, arm, disarm, total, stack, bt, ICONS }
  },
  template: `
    <div v-show="visible">
      <button type="button" class="grip"
        :title="total > 0 ? '管家在书房——留言 ' + total + ' 条' : '管家在书房——点开聊天浮窗'"
        aria-label="管家消息" @pointerenter="arm" @pointerleave="disarm" @click="$emit('expand')">
        <span class="grip-ic" v-html="ICONS.bowtie"></span>
        <span v-if="total > 0" class="gpill">{{ total > 99 ? '99+' : total }}</span>
      </button>
      <div class="bubbles" v-show="hover" @pointerenter="arm" @pointerleave="disarm">
        <button v-for="it in stack.items" :key="it.id" type="button" class="bubble"
          :title="it.text" :aria-label="'打开：' + it.text" @click="$emit('pick', it)">
          <p>{{ it.text }}</p>
          <span class="bt">{{ bt(it.ts) }}</span>
        </button>
        <span v-if="stack.more > 0" class="bmore">还有 {{ stack.more }} 条</span>
      </div>
    </div>
  `
}
