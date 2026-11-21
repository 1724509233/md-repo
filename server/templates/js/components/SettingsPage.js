/* SettingsPage 组件：设置页（spec 2026-11-22 §6）——config.yaml 页面化编辑。
 * 整页模式照监控页先例：.mwrap/.m-scroll/.mpage 骨架 + mp-enter/mp-out 转场复用（style.css 动效层
 * 整族作用域于 .mwrap，本页零新增 CSS）；#settings-wrap id = app.js 互斥/转场选择器作用域
 * （两页同族类 .mwrap 并存，querySelector 类选择器会误选监控页）。
 * 契约（Task 7 交付面）：props 无；inject('state')（Task 8 起加 inject('api') 取数保存）；
 *   emits ['back', 'open-doc']；挂载点 = app 模板 v-show="state.settingsPage"。
 * 互斥契约：文档区/监控页/设置页三态互斥（app 开页时收起其余两态；「← 返回文档」不自动重开面板）。
 * 本文件当前为骨架：分段导航与表单（主 agent / 后台 agent / 杂项）由 Task 8/9 填充，st- 前缀类族届时新增。
 */
const { ref, watch, nextTick, inject } = Vue

export const SettingsPage = {
  name: 'SettingsPage',
  emits: ['back', 'open-doc'],
  setup(props, { emit }) {
    const state = inject('state')
    const wrapRef = ref(null)
    // ─── 进入动效（监控页同款溶解节奏）：v-show display:none→block + mp-enter 类每次打开重放；
    //     reduced-motion 降级由 style.css 既有 .mwrap.mp-enter 归零规则覆盖，JS 无需分支 ───
    let enterT = null
    let firstRendered = false
    watch(
      () => state.settingsPage,
      async (open) => {
        if (!open) return
        const el = wrapRef.value
        if (!el) return
        if (firstRendered) await nextTick() // 上一轮 v-show 隐藏态已提交 → 重放入场
        else firstRendered = true
        el.classList.remove('mp-enter')
        void el.offsetWidth // 强制 reflow：连开两次也重放入场
        el.classList.add('mp-enter')
        clearTimeout(enterT)
        enterT = setTimeout(() => el.classList.remove('mp-enter'), 950) // 序列最长 ~560ms，留尾
      }
    )

    function back() { emit('back') }

    return { wrapRef, back }
  },
  template: `
    <div ref="wrapRef" class="mwrap" id="settings-wrap">
      <div class="m-scroll">
        <div class="mpage">
          <div class="mp-head">
            <!-- 单出口：回文档视图（三态互斥；面板不自动重开，重入走 rail【记忆】） -->
            <button class="mp-back" type="button" title="返回文档视图" aria-label="返回文档视图" @click="back">
              <svg aria-hidden="true" viewBox="0 0 24 24"><path d="M15 5l-7 7 7 7"/></svg>返回文档</button>
            <h1 class="mp-title">设置</h1>
            <div class="mp-meta">保存即校验、即落盘</div>
          </div>

          <!-- 骨架占位卡：分段表单（主 agent / 后台 agent / 杂项）由 Task 8/9 填充 -->
          <div class="mcard">
            <div class="mc-h"><span class="mc-t">配置</span></div>
            <div class="zline">表单即将上线</div>
          </div>

        </div>
      </div>
    </div>
  `
}
