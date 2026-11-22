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

  /** GET /api/docs 原始全量（绕过 ETag 三态）——给需要「每次都要数据」的非树消费者。
   *  getDocs 的 304→null 契约会把「无变化」当空数据喂给它们（2026-11-22 设置页目录候选实证）。 */
  async getDocsRaw() {
    const res = await fetch('/api/docs', { cache: 'no-store' })
    if (!res.ok) throw new Error(`GET /api/docs (raw) → ${res.status}`)
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

  /** GET /api/docs/changes?since= — 变更轮询 */
  async getChanges(since) {
    const res = await fetch('/api/docs/changes?since=' + since)
    return res.json()
  },

  /** GET /api/trash — 回收站列表（batch 按 deleted_at 降序；files = [{path, size}]） */
  async trashList() {
    const res = await fetch('/api/trash')
    if (!res.ok) throw new Error(`trash list → ${res.status}`)
    return res.json()
  },

  /** POST /api/trash — {action: restore|purge|empty, batch?, path?}；非 ok 抛错带 status（409 = 恢复冲突） */
  async trashAct(action, payload = {}) {
    const res = await fetch('/api/trash', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action, ...payload })
    })
    if (!res.ok) throw new Error(`trash ${action} → ${res.status}`)
    return res.json()
  },

  /** POST /api/ai/search — RAG 搜索（recall: strict/near/relaxed/fuzzy，缺省 strict；
   *  tags = 结构化 tag 条件（spec 2026-11-13，非空数组才发送）；weights = {tag: number}） */
  async aiSearch(query, topK = 5, signal, recall = 'strict', tags, weights) {
    const body = { query, top_k: topK, recall }
    if (Array.isArray(tags) && tags.length) body.tags = tags
    if (weights && typeof weights.tag === 'number') body.weights = { tag: weights.tag }
    const res = await fetch('/api/ai/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal
    })
    const data = await res.json()
    // 返回完整响应体：results（命中）+ recall（产出结果的档位——服务端零命中
    // 回退时 relaxed→fuzzy，响应记终点档，meta 行据此显示，spec §3.3）
    return data
  },

  /** POST /api/recall/click — 点击落账（spec 2026-11-13 §4.4）：fire-and-forget，
    *  服务端恒 204；错误静默吞（点击响应不依赖落账成功，UI 无感） */
  recallClick(payload) {
    fetch('/api/recall/click', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    }).catch(() => { /* fail-open：静默 */ })
  },

  /** GET /api/tags — 全量 tag + 文档数（tag 选择器；doc_count 降序、name 升序；legacy 无 graph 表 → []） */
  async getTags() {
    const res = await fetch('/api/tags')
    if (!res.ok) throw new Error(`GET /api/tags → ${res.status}`)
    const data = await res.json()
    return data.tags || []
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

  /** GET /api/agent/stream — 会话事件订阅 SSE（返回 Response，同 agentChat 风格；
   *  snapshot=1 恢复 / run_id+cursor 增量重连两形态，spec 2026-11-20 §6；opts.signal 摘订阅用） */
  async agentStream(sessionId, opts = {}) {
    const params = new URLSearchParams({ session_id: sessionId })
    if (opts.runId) params.set('run_id', opts.runId)
    if (opts.cursor) params.set('cursor', String(opts.cursor))
    if (opts.snapshot) params.set('snapshot', '1')
    return fetch('/api/agent/stream?' + params.toString(), { signal: opts.signal })
  },

  /** POST /api/agent/stop — 显式停止（cancel + 清队列 + 清 pending_confirms → 终帧 stopped）；
   *  幂等 {ok, stopped}（无活跃 Run → stopped:false） */
  async agentStop(sessionId) {
    const res = await fetch('/api/agent/stop', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId })
    })
    if (!res.ok) throw new Error(`agentStop → ${res.status}`)
    return res.json()
  },

  /** POST /api/agent/queue/remove — 删除未出队项（已出队静默 200）；面板列表只认 queue_update 事件 */
  async agentQueueRemove(sessionId, qid) {
    const res = await fetch('/api/agent/queue/remove', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, qid })
    })
    if (!res.ok) throw new Error(`queue/remove → ${res.status}`)
    return res.json()
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

  /** POST /api/agent/feedback — 回答反馈（spec 2026-11-13 §5.3，D21/D22）：
   *  verdict = useful/not_useful → upsert（键 session_id+message_id）；null → 撤回（删行，幂等）。
   *  客户端不传 citations——服务端读持久化消息快照（D21：无东西可伪造） */
  async agentFeedback(sessionId, messageId, verdict) {
    const res = await fetch('/api/agent/feedback', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, message_id: messageId, verdict })
    })
    if (!res.ok) throw new Error(`feedback → ${res.status}`)
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

  /** GET /api/agent/settings?session_id=X — 会话级自动批准开关 */
  async getAgentSettings(sessionId) {
    const res = await fetch('/api/agent/settings?session_id=' + encodeURIComponent(sessionId))
    if (!res.ok) throw new Error(`GET /api/agent/settings → ${res.status}`)
    return res.json()
  },

  /** POST /api/agent/settings {session_id, auto_approve} — 设置会话级自动批准开关 */
  async setAgentSettings(sessionId, autoApprove) {
    const res = await fetch('/api/agent/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, auto_approve: autoApprove })
    })
    if (!res.ok) throw new Error(`POST /api/agent/settings → ${res.status}`)
    return res.json()
  },

  /** GET /api/agent/sessions — 会话摘要列表（last_active 降序；
   *  {sessions:[{id,title,message_count,tool_count,last_active}]}，spec 2026-11-19 §1.2） */
  async listSessions() {
    const res = await fetch('/api/agent/sessions')
    if (!res.ok) throw new Error(`GET /api/agent/sessions → ${res.status}`)
    return res.json()
  },

  /** GET /api/agent/sessions/{sid} — 全保真恢复载荷（{session_id,messages,token_count,last_active}）；
   *  非 2xx 抛错带 status（404 = 他端已删，spec §3「详情 404 → 刷新列表，现场不动」） */
  async getSession(sid) {
    const res = await fetch('/api/agent/sessions/' + encodeURIComponent(sid))
    if (!res.ok) throw new Error(`GET /api/agent/sessions/${sid} → ${res.status}`)
    return res.json()
  },

  /** DELETE /api/agent/sessions/{sid} — 删除会话；非 2xx 抛错带 status
   *  （409 = 流中持锁，404 = 无行，503 = 无 db） */
  async deleteSession(sid) {
    const res = await fetch('/api/agent/sessions/' + encodeURIComponent(sid), { method: 'DELETE' })
    if (!res.ok) throw new Error(`DELETE /api/agent/sessions/${sid} → ${res.status}`)
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
  },

  /** GET /api/memory/proposals/{relpath} — 记录读取（详情面摘录 body = justification；D25 无投影文件） */
  async getProposal(relpath) {
    const res = await fetch(_proposalUrl(relpath))
    return _jsonOrDetail(res)
  },

  /** GET /api/memory/reports — 报告渠道列表（index/reports/<alias>/*.md，mtime 降序；{relpath, alias, date, title, size, mtime}） */
  async getMemoryReports() {
    const res = await fetch('/api/memory/reports')
    if (!res.ok) throw new Error(`memory/reports → ${res.status}`)
    return res.json()
  },

  /** GET /api/memory/reports/{relpath} — 单份报告读取（{relpath, content}；越界 400 文案透传） */
  async getMemoryReport(relpath) {
    const res = await fetch('/api/memory/reports/' + relpath.split('/').map(encodeURIComponent).join('/'))
    return _jsonOrDetail(res)
  },

  /** GET /api/memory/doc/{path} — 文档面两层（spec §6.4）：links = 用户 wikilink 层；
   *  suggestions = 机器层（含 edge_id 供忽略按钮 + basis 供依据摘要）；
   *  D18 死端点过滤 + D20 排干谓词在服务端（graph_get_related_with_machine 同谓词） */
  async getDocRelated(path) {
    const res = await fetch('/api/memory/doc/' + path.split('/').map(encodeURIComponent).join('/'))
    return _jsonOrDetail(res)
  },

  /** POST /api/memory/relatedness/{edge_id}/ignore — 一键忽略（ignored 机械过滤的人面入口，D12/D15；
   *  行保留为遥测，读面过滤；重复忽略 404 文案透传） */
  async ignoreRelatedness(edgeId) {
    const res = await fetch('/api/memory/relatedness/' + edgeId + '/ignore', { method: 'POST' })
    return _jsonOrDetail(res)
  },

  // ── 回滚（spec 2026-11-14：人独占，调用粒度 = call_id；监控页⑦块消费）──

  /** GET /api/memory/rollback-calls?limit= — 最近调用组（最新在前；
   * {call_id, ts, actor, run_id, op, files: [path...]}） */
  async getRollbackCalls(limit = 20) {
    const res = await fetch('/api/memory/rollback-calls?limit=' + limit)
    if (!res.ok) throw new Error(`rollback-calls → ${res.status}`)
    return res.json()
  },

  /** POST /api/memory/rollback {call_id} — 回滚一次调用（verify-all-then-write-all，服务端不变量）。
   * 不复用 _jsonOrDetail：其对 409 抛错且丢弃 body，而 409 body = {ok, conflicts}（无 detail 字段，
   * 只剩 "HTTP 409"）→ 409 时解析 body 返回可区分结果（半态 body 含 restored = 已恢复部分）；
   * 其余非 2xx 抛错 */
  async rollbackCall(callId) {
    const res = await fetch('/api/memory/rollback', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ call_id: callId })
    })
    if (res.status === 409) {
      let data = {}
      try { data = await res.json() } catch (e) { /* 非 JSON body → 空 conflicts */ }
      return { ok: false, conflicts: Array.isArray(data.conflicts) ? data.conflicts : [], restored: data.restored, status: 409 }
    }
    if (!res.ok) throw new Error(`rollback → ${res.status}`)
    return res.json()
  },

  // ── 出域审计（spec 2026-11-18 §3.5：监控页⑧块消费）──

  /** GET /api/egress/status — 出域端点快照（{cloud_endpoints:[host...], last_ts}） */
  async getEgressStatus() {
    const res = await fetch('/api/egress/status')
    if (!res.ok) throw new Error(`egress status → ${res.status}`)
    return res.json()
  },

  /** GET /api/egress — 出域账本行（ts 倒序，≤limit 行；{rows:[{ts,channel,actor,run_id,endpoint_host,model,path,via}]}） */
  async getEgress(limit = 50) {
    const res = await fetch('/api/egress?limit=' + limit)
    if (!res.ok) throw new Error(`egress → ${res.status}`)
    return res.json()
  },

  // ── 设置页（spec 2026-11-22 §4：GET 掩码回显 + PUT 字段级写回）──
  // 契约：非 2xx **不抛**——409（revision 冲突）/ 422（校验失败）/ 503（未接线）的 body
  // 都是页面流程数据（重载按钮 / 页顶告警条文案），返回 {status, data}；仅网络失败 throw。

  /** GET /api/settings — 五段可编辑配置 + _meta（api_key 恒掩码） */
  async getSettings() {
    return _settingsFetch('/api/settings', { method: 'GET' })
  },

  /** PUT /api/settings — {form 字段 + revision}；200 {revision, applied, restart_needed} */
  async putSettings(body) {
    return _settingsFetch('/api/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    })
  }
}

/** 设置端点共用 fetch：解析 status + JSON body（非 JSON → null），非 2xx 不抛 */
async function _settingsFetch(url, init) {
  const res = await fetch(url, init)
  let data = null
  try { data = await res.json() } catch (e) { /* 非 JSON 响应体 → null */ }
  return { status: res.status, data }
}

/** 提案 URL：relpath 形如 suggestions/2026-10-21_x.md，逐段编码；suffix 省略 = 记录读取端点 */
function _proposalUrl(relpath, suffix) {
  const base = '/api/memory/proposals/' + relpath.split('/').map(encodeURIComponent).join('/')
  return suffix ? base + '/' + suffix : base
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
