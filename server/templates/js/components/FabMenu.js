/* FabMenu 组件（沉浸式壳 W1 全局入口，spec 2026-11-22-immersive-shell §3 · plan Task 2）：
   左下 48px 圆钮（M 字样非图标——图标单点规则维持现状）+ 聚合黄铜 dot（任一徽标键 >0 才画）。
   hover 展开右上 90° 扇弧（六枚子钮）：80ms 迟滞开 / 指针离开「主钮 + 子钮 + 桥区」三元并集
   250ms 迟滞收——各块进出 = leave→enter 连续事件，靠双计时器互抵实现零掉线；桥区 .fab-bridge
   为 clip-path 四分之一盘透明层，桥接圆心至子钮的指针移动路径（展开态才接指针）。
   扇位 = CSS 自定义属性 --a 逐钮硬编码角度（禁几何 JS），旋转三件套定位：
   rotate(--a) → translateY(-半径) → 反旋保持 upright；半径 0↔92px + scale 走 transform，零 reflow。
   收起态折叠壳家族（visibility 两态 + delay，同 st-modal 纪律；v-show display 切断展开动画——
   计划口径的 v-show 由此家族等价承接，偏离已在 Task 报告备案）。
   键盘：Enter/Space 展开收起（原生 button click 后备路）；ArrowRight/Down（及反向）沿子钮序
   环移焦点（preventScroll 红线）；展开态 Esc 关本层并 stopPropagation（Esc 只关最上层）。
   expose：closeIfOpen() -> boolean（T3 Esc 链消费：true = 本层吞掉了这发 Esc）/ isOpen()。
   z 层序（spec §7）：主钮 20 < 桥区 54 < 扇弧子钮 55（根不建堆叠上下文，子层直接参与
   根层——扇弧须盖过 z-50 下拉族与 T3 抽屉 30；st-modal 浮窗族（81）更高，沿用现值）。 */
import { ICONS } from '../utils/icons.js'

const { ref, computed, onBeforeUnmount } = Vue

/* 扇弧钟点序（预览稿向，2026-11-22 用户拍板）：CSS rotate 正方向为顺时针，--a 0°=正上、
   90°=右缘——自正上起 0°→90° 每 18°（预览稿场景 3 同款半径 92px）；
   badgeKey = 聚合键（仅三枚挂数字徽标）；id = emit('pick', id) 值域 */
const ITEMS = [
  { id: 'file', label: '文件', icon: ICONS.folder, angle: '0deg', badgeKey: null },
  { id: 'search', label: '搜索', icon: ICONS.search, angle: '18deg', badgeKey: null },
  { id: 'trash', label: '回收站', icon: ICONS.trash, angle: '36deg', badgeKey: 'trash' },
  { id: 'monitor', label: '监控', icon: ICONS.chart, angle: '54deg', badgeKey: null },
  { id: 'notifications', label: '通知', icon: ICONS.bell, angle: '72deg', badgeKey: 'notif' },
  { id: 'settings', label: '设置', icon: ICONS.gear, angle: '90deg', badgeKey: 'memory' },
]

export const FabMenu = {
  name: 'FabMenu',
  props: {
    badges: { type: Object, default: () => ({ trash: 0, notif: 0, memory: 0 }) },
  },
  emits: ['pick', 'home'],
  setup(props, { emit, expose }) {
    const open = ref(false)
    const rootEl = ref(null)
    let openT = null
    let closeT = null

    const badgeTotal = computed(() => {
      const b = props.badges || {}
      return (b.trash || 0) + (b.notif || 0) + (b.memory || 0)
    })
    const badgeOf = (it) => (it.badgeKey ? ((props.badges || {})[it.badgeKey] || 0) : 0)
    // title 构成分解：memory 键 = 待审/失败（badges 额外键 memoryAwaiting/memoryFailed，
    // T6 接真值前的越界防御 = 缺键报 0）；其余键 = 计数直陈
    function subTitle(it) {
      const n = badgeOf(it)
      if (!n) return it.label
      const b = props.badges || {}
      if (it.badgeKey === 'memory')
        return it.label + ' · 待审 ' + (b.memoryAwaiting || 0) + ' · 失败 ' + (b.memoryFailed || 0)
      return it.label + ' · ' + n
    }

    function clearTimers() { clearTimeout(openT); clearTimeout(closeT) }
    /* 迟滞对（80/250ms）：块与块之间指针短暂越界只挂定时器不落地；定时器挂 setTimeout，
       指针在三元并集内任意块间移动 = armClose 后立刻 armOpen，定时器互抵 = 不闪关 */
    function armOpen() {
      clearTimeout(closeT)
      if (open.value) return
      clearTimeout(openT)
      openT = setTimeout(() => { open.value = true }, 80)
    }
    function armClose() {
      clearTimeout(openT)
      clearTimeout(closeT)
      closeT = setTimeout(() => { open.value = false }, 250)
    }

    function pick(it) {
      clearTimers()
      open.value = false
      emit('pick', it.id)
    }
    // 点主钮 = 回首页；toggle 兼作键盘/触摸的展开收起后备路（指针路径 armOpen 已先行负责）
    function onMainClick() {
      clearTimers()
      open.value = !open.value
      emit('home')
    }

    function moveFocus(step) {
      const subs = rootEl.value ? Array.from(rootEl.value.querySelectorAll('.fab-sub')) : []
      if (!subs.length) return
      const idx = subs.indexOf(document.activeElement)
      // 焦点在主钮（-1）时起步：正向进首位、反向进末位；行内环回
      const next = idx < 0 ? (step > 0 ? 0 : subs.length - 1)
                           : (idx + step + subs.length) % subs.length
      subs[next].focus({ preventScroll: true })   // preventScroll 红线（键盘与视口节）
    }
    function onKeydown(e) {
      if (e.key === 'Escape') {
        if (!open.value) return
        e.stopPropagation()   // 扇弧展开即最上层：本层吃掉 Esc，不得穿透 app 级分层链
        clearTimers()
        open.value = false
      } else if (open.value && (e.key === 'ArrowRight' || e.key === 'ArrowDown')) {
        e.preventDefault(); moveFocus(1)
      } else if (open.value && (e.key === 'ArrowLeft' || e.key === 'ArrowUp')) {
        e.preventDefault(); moveFocus(-1)
      }
    }

    /* Esc 链根方法（T3 escTarget('fan') 消费）：真关了才 true */
    function closeIfOpen() {
      clearTimers()
      if (!open.value) return false
      open.value = false
      return true
    }
    const isOpen = () => open.value

    onBeforeUnmount(clearTimers)
    expose({ closeIfOpen, isOpen })

    return { items: ITEMS, open, rootEl, badgeTotal, badgeOf, subTitle,
             armOpen, armClose, pick, onMainClick, onKeydown }
  },
  template: `
    <div ref="rootEl" class="fab-root" @keydown="onKeydown">
      <!-- 指针桥：主钮圆心至六子钮的四分之一盘（clip-path 扇形），透明零绘制，展开态才接指针 -->
      <div class="fab-bridge" :class="{ open: open }" aria-hidden="true"
        @pointerenter="armOpen" @pointerleave="armClose"></div>
      <!-- 六子钮：--a 硬编码角度（style.css 旋转三件套定位）；收起 visibility 出 Tab 序 -->
      <div class="fab-fan" :class="{ open: open }" role="menu" aria-label="功能扇弧">
        <button v-for="it in items" :key="it.id" type="button" role="menuitem"
          class="fab-sub" :style="{ '--a': it.angle }"
          :data-notif-toggle="it.id === 'notifications' || null"
          :title="subTitle(it)" :aria-label="it.label" :tabindex="open ? 0 : -1"
          @click="pick(it)" @pointerenter="armOpen" @pointerleave="armClose">
          <span class="fab-ic" v-html="it.icon"></span>
          <span v-if="badgeOf(it) > 0" class="fab-badge">{{ badgeOf(it) > 99 ? '99+' : badgeOf(it) }}</span>
        </button>
      </div>
      <!-- 主钮：M 字样（衬线黄铜）；点 = 回首页 + toggle 后备；聚合 dot 任一徽标 >0 -->
      <button type="button" class="fab-main" :class="{ fan: open }"
        :aria-expanded="open ? 'true' : 'false'" aria-haspopup="true"
        title="打开功能扇弧 · 回到首页" @click="onMainClick"
        @pointerenter="armOpen" @pointerleave="armClose">M<span v-if="badgeTotal > 0" class="fab-dot"></span></button>
    </div>
  `,
}
