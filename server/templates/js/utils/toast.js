/* 单例内联 toast（P1-12）：替换阻塞式 alert()。
 * 同 id 复用——连续报错替换文本并重置计时，绝不堆叠（Review Focus #4）。
 * 内联样式而非 style.css 新区块：隔离 T7 待目检 diff（ledger Task 7 Ruling）。 */
const TIMER_MS = 6000
let timer = null

export function sanitize(msg) {
  // 剥端点段「METHOD /path → NNN」：路径可含空格 → 非锚定 + 惰性；先剥带尾冒号形态再剥裸形态
  // （实现 meta 不进用户文案，AGENTS.md 文案规范；终审 I2：锚定 ^ 永不命中拼好前缀的消息）
  return String(msg)
    .replace(/ ?(?:GET|POST|PUT|DELETE|PATCH) \/\S[^\n]*? → \d{3}:/g, ':')
    .replace(/ ?(?:GET|POST|PUT|DELETE|PATCH) \/\S[^\n]*? → \d{3}/g, '')
}

function hide(el) {
  if (timer) { clearTimeout(timer); timer = null }
  el.style.opacity = '0'
  el.style.transform = 'translateX(1rem)'
}

export function showToast(message, kind = 'error') {
  let el = document.getElementById('app-toast')
  if (!el) {
    el = document.createElement('div')
    el.id = 'app-toast'
    el.setAttribute('role', 'alert')
    el.style.cssText = 'position:fixed;right:1rem;bottom:1rem;z-index:100;max-width:22rem;' +
      'background:var(--surface);color:var(--ink);border:1px solid var(--brass);border-radius:10px;' +
      'padding:10px 14px;font-family:Inter,"PingFang SC",sans-serif;font-size:13px;line-height:1.6;' +
      'box-shadow:0 8px 32px rgba(33,29,24,.18);cursor:pointer;opacity:1;' +
      'transform:translateX(0);transition:transform .25s ease,opacity .25s ease;'
    el.addEventListener('click', () => hide(el))
    document.body.appendChild(el)
  }
  el.textContent = sanitize(message)
  el.style.opacity = '1'
  el.style.transform = 'translateX(0)'
  if (timer) clearTimeout(timer)
  timer = setTimeout(() => hide(el), TIMER_MS)
}
