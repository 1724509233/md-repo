# Agent 会话管理 A 底座设计（历史列表 / 全保真续聊 / 删除）

- 日期：2026-11-19 · 分支建议：`feat/agent-session-history`（自 main）
- 来源：待办 `3daabcb2`（会话管理，历史查看，切换）；2026-11-19 brainstorming 定稿
- 上游决策（用户拍板）：策略选 **A 手动多会话**（对比「滚动窗口+摘要」，后者作为会话内压缩功能另行设计）；恢复保真度=全保真；布局=面板内视图切换（B 形态）；流式中点他项会话=确认+停止；本期删除落地，**归档走单独 pipeline**（待办 `7fa77d13`，本 spec 只留扩展点）。
- 架构契约（写给后续）：**视图 ≠ 会话 ≠ 运行** 三层分离——历史列表是纯视图层开关；运行与连接解耦留给 SSE 续跑（待办 `aa6a2096`），届时只替换「停止并切换」一处，列表零改动。

## 0. 范围裁决

- **本期做**：会话列表（只读）、点开全保真续聊、会话删除（两步内联确认）、流式切换确认停流、**刷新恢复**（页面刷新回到刷新前会话并原位恢复消息流）。
- **递延/不做**：重命名（title 字段 + 编辑 UI，待办既有递延）；自动清理（并入归档 pipeline 议题）；feedback verdict 回填（恢复后反馈按钮默认无选中态，表按 session+message 键可后置回填）；token 用量回填（`sessions.token_count` 列现无任何写入方——`session_mgr.save` 不传 token_count——恢复后用量条归零、下次对话自然回填；**不**做假回填）；列表虚拟化（并入 UI-P2 递延 `0425c912`）；分页（limit 50 固定）。

## 1. 后端（`server/vault/db/sessions.py` + `server/routes/agent.py`）

### 1.1 DB 层（无 schema 变更）

- 新增 `list_session_summaries(conn, limit=50) -> list[dict]`：`SELECT id, messages, last_active FROM sessions ORDER BY last_active DESC LIMIT ?`，Python 侧投影为 `{id, title, message_count, tool_count, last_active}`（2026-11-19 目检轮口径修正：`message_count` = 非提醒 user 行数 + 连续 assistant/tool 段数，与恢复渲染 mapHistory 语义一一对照——段 = maximal 连续 assistant+tool 行块，遇非提醒 user 或结束封段，提醒行不封段不计行；`tool_count` = 所有 assistant 行 `tool_calls` 条目总数，孤儿与已回填都算，与恢复后工具块数一致）：
  - 排除 `messages == []` 行（`set_auto_approve` 会预建空行，列表全是噪音）。
  - **title = 首条非提醒 user 消息摘要**（跳过 `[当前打开:` 前缀——`DocPathUserReminder` 持久化后同 role 同形，不跳过标题就全是路径）。取首条不取末条：末条随对话漂移且常是上下文碎片；首条=话题锚（ChatGPT 同款，沉睡会话续聊置顶配首条标题是标准可辨行为）。截 40 字符 + `…`。**兜底链**：无 user 消息（仅工具会话）→ `工具会话 · N 次工具`（N=`tool_count`；无 user 行时可见消息数常为段数，标题不依赖它）；全无 → `（无标题）`。前缀字面量与 `DocPathUserReminder._PREFIX`（reminders.py:65）三处同源（后端标题过滤/前端恢复过滤/reminder 本体），钉：pytest 断言字面量一致。
  - blob 全读在个人量级（数十会话 × 数百 KB）可接受；日后过大再改 SQL `json_extract`，YAGNI 不预支。
- 新增 `delete_session(conn, session_id) -> bool`（rowcount>0）。
- 现有 `get_recent_sessions`（sessions.py:90）无调用方且返回全量 blob——**删除**，被 1.1 取代（连带 test_db.py:347 用例改写、db/README.md 行更新；`test_vault_root_shim_retired.py:22` 是墓碑名单——断言的正是这些名字不得复活为 `server.vault` 属性，函数删除后该断言依然真，无需改动）。

### 1.2 路由（agent 域，注意 `test_routes_order` 钉）

- `GET /api/agent/sessions` → `{sessions: [...]}`（1.1 投影；`ctx.db_conn` None → `[]`）。
- `GET /api/agent/sessions/{sid}` → `{session_id, messages, token_count, last_active}`；无行 404。sid 仅做 DB 查找，无路径语义，不过 `_safe_rel_path`。messages 为 LLM 形态原样（含 `[当前打开:` 提醒行、assistant.tool_calls、tool 结果行、assistant 终帧的 `id/ts/citations` 持久化字段——**引用摘要/反馈按钮恢复的依据**，runtime.py:305-314）。
- `DELETE /api/agent/sessions/{sid}` → 锁被持有（`session_mgr.get_lock(sid).locked()`）→ 409；否则删行 + `session_mgr.remove(sid)` + `agent_cache.pop(sid)`（`_agent_last_active` 同步 pop）。
- `SessionManager.remove(sid)`（session.py）：pop `_messages/_last_active/_locks` 三表；无锁在场（409 已前置）故不涉 held-lock 竞态。

## 2. 前端（`AIChatPanel.js` 为主，`app.js`/`api.js` 薄接线）

### 2.1 视图切换（B 形态）

- `ai-acts` 加历史图标钮（30px `ai-ibtn` 家族规格，`aria-label="历史会话"`，`aria-pressed` 随 `view`）；`view = ref('chat' | 'history')`。
- `view==='history'`：头部左展示层替换为「← 返回 + 历史会话」，**新会话钮保留**；消息区整块换成列表；输入区隐藏。返回列表 = 返回钮或 **Esc**。
- **回归红线**：`view` 开关路径**不得**触碰 `messages`/`sessionId`/`abortCtrl`（开列表绝不打断流）。对照现状：`sessionId` watch（AIChatPanel.js:472）会 abort——列表视图不得经由它。钉：静态断言 toggle 代码块内无 `abort` 调用 + 目检「流式中开列表流不断」。

### 2.2 列表条目

- 行 = `<div>` 包 `[button 行主体][button ×]`（**行主体必须 `<button>`**——搜索卡键盘不可达教训 e96cf4ed；删除钮为兄弟不是后代，无嵌套 button）。
- 行内容：title / 相对时间（`utils/format.js fmtRel`，Intl 固定 locale）+ 计数副行 `N 条`（message_count）· `N 次工具`（tool_count，0 不显）；当前会话行 `is-current` 高亮。
- 删除两步：× → 行内「删除？ 确认/取消」→ `api.deleteSession` → 行移除；失败红显。当前会话行在 `status==='thinking'` 时隐藏 ×（后端 409 兜底）。**删除当前会话成功 → 自动 `newSession`**（面板不留死会话）。
- 点非当前行：`status==='thinking'` → 行内联「停止并切换？ 是/否」→ 是 = `stopChat()` + 切换；非流式直接切换。

### 2.3 全保真恢复

- 切换是**先取数后原子换**：`selectSession(sid)` → `await api.getSession(sid)` 成功 → `state.restorePayload = {sid, data}` → `state.sessionId = sid`；失败（404=已删等）→ 现有 toast 基建提示 + 只刷新列表，现场不动。
- panel `sessionId` watch 增加分支：`restorePayload?.sid === newSid` → `messages.value = restoreMessages(data.messages)`（替代现状 `[]`）并消费 payload；`tokenUsage` 归零（见 §0）；其余重置逻辑不变。
- 映射函数**独立模块** `server/templates/js/utils/chatRestore.js`（可被 node 行为测试直接 import）：
  - `role==='user'`：`[当前打开:` 前缀行**丢弃**（内部提醒，live 从不显示，恢复也不显示）；其余 → user 文本气泡。
  - `assistant.tool_calls` → **连续 assistant+tool 链归并进同一个 AI turn 消息**（对齐 live 的 currentTurn 跨轮累积结构）：text 块（content 非空）+ 每个 tool_call → tool 块 `{id, name, args(parsed JSON), expanded:false, status, result}`；`status`：tool 结果 `Error` 前缀 → `'error'`，`User rejected` → `'rejected'`，否则 `'done'`（result 摘要走既有 `toolResultSummary`，无 meta → 原文首行——已知降级，a24e72ac 明文）。
  - assistant 终帧 → text 块 + **`messageId`/`citations` 透传**（恢复行与 live 行必须同字段形状——反馈钮与引用摘要直接复用；目检断言）。
  - `role==='tool'` → 按 `tool_call_id` 回填对应 tool 块 result。

### 2.4 api.js

`listSessions() / getSession(sid) / deleteSession(sid)`；`test_api_js_exports_all_endpoints` 家族同步。

### 2.5 刷新恢复

- 现状：`app.js:31` 每次页面加载生成全新 `sessionId: 'web-' + Date.now()`——刷新即弃会话。
- 持久化当前会话 id：`localStorage['mdvault.aiSession']`（键族/try-catch 隐私模式先例照 `searchWeights`，search.js:86 + SearchPanel.js:141）。写点 = newSession 成功、selectSession 成功、**sendChat 投递成功**三处（2026-11-21 F5 断订阅流修复：前两处不覆盖「页面新开的会话里直接发消息」这条主路径——存储不落则 F5 后 bootRestore 恢复回旧 sid/空态、面板空白；面板 sendChat 202 成功 → `emit('sessionActive', sid)` → app `onSessionActive` 只 persist 不赋值）。前两处与 `state.sessionId` 赋值同点，无第四写点。
- 启动恢复：初始化读到存储 sid → `getSession(sid)`：200 → `state.sessionId = sid` + `restorePayload`（复用 §2.3 全保真路径，启动与手动切换**同一条恢复管线**）；404/失败 → 走现状新会话（存储自然被首条新会话覆盖）。恢复动作不阻塞面板挂载（异步，期间显示空面板 → 消息到达回填；addMsg 的贴底逻辑天然把首屏滚到底）。
- **刷新中断的残缺尾部**（刷新掐断 SSE → run 夭折，finally 已持久化的 messages 可能以 assistant.tool_calls 结尾、无 tool 结果）：恢复渲染遇孤儿 tool_call → tool 块 `status='error'`；下一轮对话服务端 `_ensure_tool_results`（runtime.py:105）会补合成 tool 结果，API 合法性由既有机制守住，前端不重演该逻辑。
- 多标签页：键全局单值，后启动/后切换者赢（文档级竞态接受，个人工具不做 per-tab 存储；备案）。

## 3. 错误处理汇总

| 面 | 处置 |
|---|---|
| 列表/详情 fetch 失败 | toast + 列表重载；不触现场 |
| 删除 409（锁） | 多标签页在他会话流式中可触达（本面板 thinking 守卫只盖当前会话）；toast + 行保留 |
| 删除成功（当前会话） | 自动 newSession |
| 详情 404（他端已删） | toast + 刷新列表 |
| `ctx.db_conn` None | 列表返回 `[]`；删除 503 |

## 4. 扩展点（只留路，不实现）

- 归档 pipeline（`7fa77d13`）：`POST /api/agent/sessions/{id}/archive` 预留语义；delete 语义保持「不可逆删」不漂移。
- SSE 续跑（`aa6a2096`）：§2.2「停止并切换」确认可升级为「后台续跑不停 + 重连接上」；`view` 层零改动；「进行中」徽标届时进列表行。

## 5. 测试

- 后端 pytest：`list_session_summaries`（排除空行/标题跳提醒/排序/limit）、`delete_session`、三路由（形状/404/409/删除后 `_get_agent` 重建拉回 DB 会话继续可聊）、`SessionManager.remove` 三表清、`test_routes_order` 过。
- node 行为测试（chatRestore.js，直调映射）：提醒行丢弃 / 多轮工具链归并单 turn / tool_call_id 配对 / Error·rejected 状态 / citations+messageId 透传 / 无 tool_calls 纯文本。
- 静态钉（`test_web_ui_audit.py` 家族）：历史钮存在且 `aria-pressed`；行主体 `button`；toggle 视图路径零 `abort`；`utils/format.js` 复用（无本地日期拷贝）；`mdvault.aiSession` 存储键存在且写点=3（newSession/selectSession 与 sessionId 赋值同点 + sendChat 投递成功 persist；2026-11-21 口径）。
- 浏览器目检（**不批不提交**，规则 3）：① 流式中开列表流不断 ② 点他项→内联确认→停流切换 ③ 恢复观感全保真（工具折叠可展开、引用摘要、反馈可点）④ 删除两步 + 删当前自动新会话 ⑤ Esc 返回 ⑥ 键盘 Tab 全程可达 ⑦ **会话中刷新 → 同会话消息原位恢复可续聊**（含刷新前刚发未回完的残缺尾部显 error 不白屏）。
