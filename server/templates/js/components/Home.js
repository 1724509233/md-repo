/* Home 组件（首页·管家日报，spec 2026-11-22-home-butler-landing）。
   Task 4 = 导航骨架占位：挂载 + 拉数 + G1 时段问候 + 错误态；
   完整渲染（分级文案 homeVoice / G2-G4 / ✨一句话）在 Task 5 整体重写本文件。 */
const { ref, computed, inject, onMounted, watch } = Vue

export const Home = {
  name: 'Home',
  emits: ['open-doc', 'open-memory', 'open-trash'],
  setup(props, { emit }) {
    const state = inject('state')
    const api = inject('api')
    const data = ref(null)
    const error = ref(false)

    // G1 时段问候（占位最小版；Task 5 换 homeVoice.greetingOf + 分级文案）
    const greeting = computed(() => {
      const h = new Date().getHours()
      return h < 12 ? '早上好' : h < 18 ? '午后好' : h < 23 ? '晚上好' : '夜深了'
    })

    function load() {
      // since = max(lastPresence, now−7d)：缺席窗从上次在场起算（§6.2）；
      // 隐私模式读不到 → 缺省 7d 上限窗
      let last = 0
      try { last = Number(localStorage.getItem('mdvault.lastPresence')) || 0 } catch (e) { last = 0 }
      const since = Math.max(last || 0, Date.now() - 7 * 86400e3)
      let seen = 0
      try { seen = Number(localStorage.getItem('mdvault.lastSeenChangeSeq')) || 0 } catch (e) { seen = 0 }
      api.getHome(since, seen)
        .then(d => { data.value = d; error.value = false })
        .catch(() => { error.value = true })
    }

    onMounted(load)
    watch(() => state.homeRefreshSeq, load)   // 监控页退场 → 重取（§6.3）

    return { data, error, greeting }
  },
  template: `
    <div class="hb-shell flex-1 min-w-0 overflow-y-auto">
      <div class="hb-inner">
        <p class="hb-greeting">{{ greeting }}</p>
        <p v-if="error" class="hb-muted">首页数据没取回来，刷新试试。</p>
        <p v-else-if="!data" class="hb-muted">正在翻看这几天的记录…</p>
      </div>
    </div>
  `
}
