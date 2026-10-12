/* API 封装：所有 /api/* 调用集中于此 */

let lastTreeHash = ''

export const api = {
  /** GET /api/docs — 文件列表（ETag 缓存） */
  async getDocs() {
    const headers = lastTreeHash ? { 'If-None-Match': lastTreeHash } : {}
    const res = await fetch('/api/docs', { headers })
    if (res.status === 304) return null // 无变化
    lastTreeHash = res.headers.get('ETag') || ''
    return res.json()
  },

  /** GET /api/docs/{path} — 读取文档内容 */
  async getDoc(path) {
    const res = await fetch('/api/docs/' + encodeURIComponent(path))
    if (!res.ok) throw new Error(`GET /api/docs/${path} → ${res.status}`)
    return res.text()
  },

  /** PUT /api/docs/{path} — 保存文档 */
  async saveDoc(path, content) {
    const res = await fetch('/api/docs/' + encodeURIComponent(path), {
      method: 'PUT',
      headers: { 'Content-Type': 'text/plain' },
      body: content
    })
    if (!res.ok) throw new Error(`PUT /api/docs/${path} → ${res.status}`)
    return res.json()
  },

  /** POST /api/docs/manage — 文件管理（create_file/create_folder/rename/move/delete） */
  async manage(action, payload) {
    const res = await fetch('/api/docs/manage', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action, ...payload })
    })
    if (!res.ok) throw new Error(`manage ${action} → ${res.status}`)
    return res.json()
  },

  /** POST /api/docs/{path} — 上传文档（multipart） */
  async uploadDoc(path, file) {
    const form = new FormData()
    form.append('file', file)
    const res = await fetch('/api/docs/' + encodeURIComponent(path), { method: 'POST', body: form })
    if (!res.ok) throw new Error(`POST /api/docs/${path} → ${res.status}`)
    return res.json()
  },

  /** GET /api/docs/changes?since= — 变更轮询 */
  async getChanges(since) {
    const res = await fetch('/api/docs/changes?since=' + since)
    return res.json()
  },

  /** POST /api/ai/search — RAG 搜索 */
  async aiSearch(query, topK = 5, signal) {
    const res = await fetch('/api/ai/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query, top_k: topK }),
      signal
    })
    const data = await res.json()
    return data.results || []
  },

  /** POST /api/agent/chat — Agent 聊天（SSE 流式，返回 Response） */
  async agentChat(body, signal) {
    return fetch('/api/agent/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal
    })
  },

  /** POST /api/agent/confirm — 工具调用确认 */
  async agentConfirm(id, approved, sessionId) {
    const res = await fetch('/api/agent/confirm', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id, approved, session_id: sessionId })
    })
    return res.json()
  },

  /** POST /api/agent/new-session — 新建会话 */
  async newSession(oldSessionId) {
    const res = await fetch('/api/agent/new-session', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ old_session_id: oldSessionId })
    })
    if (!res.ok) throw new Error(`new-session → ${res.status}`)
    return res.json()
  },

  /** GET /api/graph/backlinks?path= — 反向链接 */
  async graphBacklinks(path) {
    const res = await fetch('/api/graph/backlinks?path=' + encodeURIComponent(path))
    return res.json()
  },

  /** GET /api/graph/related?path=&hops= — 关联文档 */
  async graphRelated(path, hops = 1) {
    const res = await fetch('/api/graph/related?path=' + encodeURIComponent(path) + '&hops=' + hops)
    return res.json()
  },

  /** GET /api/notifications?unread=1 — 未读通知（最新 50） */
  async getNotifications(unread = true) {
    const res = await fetch('/api/notifications?unread=' + (unread ? 1 : 0))
    return res.json()
  },

  /** POST /api/notifications/read — {ids: [...]} 或 {all: true} */
  async markNotificationsRead(body) {
    const res = await fetch('/api/notifications/read', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    return res.json()
  }
}
