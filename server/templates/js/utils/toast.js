/* 单例内联 toast（P1-12）：替换阻塞式 alert()。
 * 同 id 复用——连续报错替换文本并重置计时，绝不堆叠（Review Focus #4）。
 * P2 簇⑤（spec §5.6/§5.7）：action 按钮 API + 隐藏撤命中 + kind 装饰位。
 * 内联样式而非 style.css 新区块：隔离 T7 待目检 diff（ledger Task 7 Ruling）。 */
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
  // spec §5.6（C1）：fade 只降 opacity——含「重试」按钮的隐藏体是视口右下角的隐形命中靶，
  // 不可见按钮永不可点：隐藏即撤 pointer-events（show 时恢复）；
  // I-2：pointer-events 封不死键盘面——Tab 仍能聚焦隐形按钮，inert 连 Tab 一起封
  el.style.pointerEvents = 'none'
  el.inert = true
}

export function showToast(message, kind = 'error', action = null) {
  let el = document.getElementById('app-toast')
  if (!el) {
    el = document.createElement('div')
    el.id = 'app-toast'
    el.setAttribute('role', 'alert')
    el.style.cssText = 'position:fixed;right:1rem;bottom:1rem;z-index:100;max-width:22rem;' +
      'background:var(--surface);color:var(--ink);border:1px solid var(--brass);border-radius:10px;' +
      'padding:10px 14px;font-family:Inter,"PingFang SC",sans-serif;font-size:13px;line-height:1.6;' +
      'box-shadow:0 8px 32px rgba(33,29,24,.18);cursor:pointer;opacity:1;pointer-events:auto;' +
      'transform:translateX(0);transition:transform .25s ease,opacity .25s ease;'
    el.addEventListener('click', () => hide(el))
    document.body.appendChild(el)
  }
  // kind→border（spec §5.7）：2026-11-18 盘上勘察无 --success/--st-success 系 token
  // （style.css :root 只有 brass 单强调色）→ 兜底：success 沿用 brass 边框，
  // kind 在真 token 落地前为装饰性形参（零视觉差异，调用点语义先行）。
  // 主体整体重建（textContent 构建，零 HTML 注入）：单例复用路径由此天然获得
  // 「换/摘 action」能力——连续报错替换文本时按钮随 action 实参换挂或摘除，不留过期按钮。
  el.textContent = ''
  const span = document.createElement('span')
  span.textContent = sanitize(message)
  el.appendChild(span)
  if (action) {
    const btn = document.createElement('button')
    btn.type = 'button'
    btn.textContent = action.label
    btn.style.cssText = 'margin-left:10px;padding:2px 10px;border:1px solid var(--brass);' +
      'border-radius:6px;background:transparent;color:var(--ink);font:inherit;font-size:12px;cursor:pointer;'
    btn.addEventListener('click', (e) => {
      e.stopPropagation() // 点按钮不得冒泡进主体 close（点主体才是关闭语义）
      const r = action.onClick()
      if (r && r.then) { // 在途 disable 防双击重复触发（spec §5.4）
        btn.disabled = true
        r.finally(() => { btn.disabled = false })
      }
    })
    el.appendChild(btn)
  }
  el.style.opacity = '1'
  el.style.transform = 'translateX(0)'
  el.style.pointerEvents = 'auto' // 恢复命中（hide 撤过；见 §5.6 注释）
  el.inert = false                // 解除 inert（I-2；与 pointer-events 同对）
  if (timer) clearTimeout(timer)
  // 带 action 驻留 12s（给用户点「重试」的反应窗口），纯提示仍 6s
  timer = setTimeout(() => hide(el), action ? 12000 : 6000)
}
