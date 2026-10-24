/* API 封装：所有 /api/* 调用集中于此 */

let lastTreeHash = ''

export const api = {
  /** GET /api/docs — 文件 + 目录（ETag 缓存） */
  async getDocs() {
    const headers = lastTreeHash ? { 'If-None-Match': lastTreeHash } : {}
    const res = await fetch('/api/docs', { headers })
    if (res.status === 304) return null // 无变化
    lastTreeHash = res.headers.get('ETag') || ''
    return res.json()  // { files: [...], dirs: [...] }
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

  /** POST /api/ai/search — RAG 搜索（recall: strict/near/relaxed/fuzzy，缺省 strict） */
  async aiSearch(query, topK = 5, signal, recall = 'strict') {
    const res = await fetch('/api/ai/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query, top_k: topK, recall }),
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
  },

  // ── 记忆控制台（spec 2026-10-21 §7：console 只移状态，执行器落文件）──

  /** GET /api/memory/status — 脉搏：by_status 九态 + awaiting 队列 + 触发态 + 召回需求 */
  async getMemoryStatus() {
    const res = await fetch('/api/memory/status')
    if (!res.ok) throw new Error(`memory/status → ${res.status}`)
    return res.json()
  },

  /** GET /api/recall/stats — 召回深钻 L2（docs / failed_queries 各 ≤200 行；监控页按需触发，非轮询） */
  async getRecallStats(days = 30, halfLife = 30) {
    const res = await fetch('/api/recall/stats?days=' + days + '&half_life=' + halfLife)
    if (!res.ok) throw new Error(`recall/stats → ${res.status}`)
    return res.json()
  },

  /** GET /api/memory/proposals — 全部提案（frontmatter 字段；解析失败为 malformed 项，不 500） */
  async getMemoryProposals() {
    const res = await fetch('/api/memory/proposals')
    if (!res.ok) throw new Error(`memory/proposals → ${res.status}`)
    return res.json()
  },

  /** POST /api/memory/proposals/{relpath}/adopt — 采纳（awaiting_review → adopted，actor human） */
  async adoptProposal(relpath) {
    const res = await fetch(_proposalUrl(relpath, 'adopt'), { method: 'POST' })
    return _jsonOrDetail(res)
  },

  /** POST /api/memory/proposals/{relpath}/reject — 驳回（reason 必填；服务端空 reason 亦 400） */
  async rejectProposal(relpath, reason) {
    const res = await fetch(_proposalUrl(relpath, 'reject'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reason })
    })
    return _jsonOrDetail(res)
  },

  /** GET /api/memory/proposals/{relpath}/trail — change_log 轨迹（seq/ts/source/op/path，最新在前） */
  async getProposalTrail(relpath) {
    const res = await fetch(_proposalUrl(relpath, 'trail'))
    return _jsonOrDetail(res)
  }
}

/** 提案 URL：relpath 形如 suggestions/2026-10-21_x.md，逐段编码 */
function _proposalUrl(relpath, suffix) {
  return '/api/memory/proposals/' + relpath.split('/').map(encodeURIComponent).join('/') + '/' + suffix
}

/** 非 2xx 时抛出带服务端 detail 的 Error（FastAPI 的 400/404 文案直接进 UI） */
async function _jsonOrDetail(res) {
  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    try {
      const data = await res.json()
      if (data && data.detail) detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail)
    } catch (e) { /* 非 JSON 响应体 → 保留状态码文案 */ }
    throw new Error(detail)
  }
  return res.json()
}
