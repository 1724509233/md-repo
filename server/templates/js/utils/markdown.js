/* Markdown 渲染封装：marked + DOMPurify + hljs + mermaid */

let rendererConfigured = false

export function setupMarkdown() {
  if (rendererConfigured || !window.marked) return
  rendererConfigured = true

  const renderer = new marked.Renderer()

  // 代码块：带语言标签 + 复制按钮
  renderer.code = function (code, lang) {
    const langLabel = lang || 'text'
    const cls = lang ? ` class="language-${lang}"` : ''
    return `<div class="code-block"><div class="code-block-header"><span>${langLabel}</span><button class="code-copy-btn" type="button">复制</button></div><pre><code${cls}>${code}</code></pre></div>`
  }

  // 表格：包裹 overflow-x-auto
  renderer.table = function (header, body) {
    return `<div class="overflow-x-auto"><table><thead><tr>${header}</tr></thead><tbody>${body}</tbody></table></div>`
  }

  marked.setOptions({ renderer })
}

/**
 * 渲染 markdown 文本为安全 HTML
 * @param {string} text - markdown 原文
 * @returns {string} 安全 HTML
 */
export function renderMarkdown(text) {
  setupMarkdown()
  const raw = marked.parse(text || '')
  const clean = window.DOMPurify ? DOMPurify.sanitize(raw) : raw
  return clean
}

/**
 * 对已插入 DOM 的预览元素执行后处理（hljs 高亮 + mermaid 渲染）
 * @param {HTMLElement} container - 预览容器
 */
export function postProcessPreview(container) {
  // 代码高亮
  container.querySelectorAll('pre code').forEach(el => {
    if (window.hljs) hljs.highlightElement(el)
  })

  // Mermaid 图表
  const mermaidBlocks = container.querySelectorAll('pre code.language-mermaid')
  if (mermaidBlocks.length && window.mermaid) {
    mermaidBlocks.forEach(block => {
      const div = document.createElement('div')
      div.className = 'mermaid'
      div.textContent = block.textContent
      block.parentElement.replaceWith(div)
    })
    mermaid.run({ nodes: container.querySelectorAll('.mermaid') })
  }
}

/**
 * HTML 转义
 */
export function escapeHtml(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')
}
