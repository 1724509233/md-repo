# Agent 会话管理 A 底座（历史列表 / 全保真续聊 / 删除 / 刷新恢复）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** AI 面板新增历史会话列表（视图切换形态），点击全保真恢复续聊，支持删除与刷新恢复，后端补三个会话路由。

**Architecture:** 三层契约「视图 ≠ 会话 ≠ 运行」：历史列表是 AIChatPanel 纯视图层开关（不触碰 messages/sessionId/abortCtrl）；恢复走「先取数后原子换」单一管线（手动切换与刷新启动同路）；恢复映射独立成无依赖 ESM 模块 `utils/chatRestore.js`（node 可直测）。后端零渲染知识：只加列表/详情/删除三路由 + `SessionManager.remove`。

**Tech Stack:** FastAPI + SQLite（sessions 表无 schema 变更）、Vue 3 CDN（无构建）、node 行为测试（`test_web_ui_audit.py` 的 `import(as_uri)` 模式）。

**Spec:** `docs/superpowers/specs/2026-11-19-agent-session-history.md`（随本计划一并执行）

## Global Constraints

- 测试命令：`.venv/bin/python -m pytest <target> -v`（全量基线 1358 passed，不得退化）。
- **UI 布局类改动未经用户浏览器目检不得 commit**（AGENTS.md Frontend 规则 3）——Task 4/5 的 commit 步骤带目检闸。
- a11y 红线：列表行主体必须是 `<button>`（搜索卡教训 e96cf4ed）；装饰 svg `aria-hidden="true"`；聚焦调用一律 `focus({preventScroll:true})`。
- 提醒前缀字面量 `[当前打开:` 与 `DocPathUserReminder._PREFIX`（reminders.py:65）同源，后端标题过滤与前端恢复过滤都必须引用常量/钉一致。
- 路由表顺序由 `tests/test_routes_order.py` 快照钉死，新增路由只进 agent 域块并同步快照。
- 不新增 Python/前端依赖；localStorage 访问照 `mdvault.searchWeights` 先例 try/catch（search.js:86、SearchPanel.js:141）。

## Review Focus

（spec 隐含但任务测试不直接 exercised 的易咬人面，逐条已把钉加进归属任务）

1. **流式中打开历史列表**——必须不打断进行中的 SSE（视图开关若误连 sessionId watch 会 abort，回归肉眼难见）。→ Task 4 静态钉 + 目检①。
2. **删除会话后立刻在同一会话继续发消息**——`agent_cache` 里的 Agent 还持旧 `_messages`，不 pop 会把删掉的会话整段复活。→ Task 2 复活测试。
3. **恢复的消息流显示内部提醒**——`[当前打开: …]` 持久化后是普通 user 消息，不过滤则历史里冒出系统噪音。→ Task 3 映射测试。
4. **刷新掐断流的残缺尾部**（孤儿 tool_call）——恢复渲染必须落 error 块不白屏；下一轮 API 合法性由既有 `runtime.py:105 _ensure_tool_results` 兜底，不得新写平行机制。→ Task 3 映射测试（渲染侧）；服务端既有钉已覆盖运行侧。
5. **隐私模式 localStorage 不可用**——启动路径 try/catch，不得炸整个面板。→ Task 5 静态钉。

---

### Task 1: DB 层会话摘要与删除

**Files:**
- Modify: `server/vault/db/sessions.py`
- Modify: `server/vault/db/README.md:148`（函数表行）
- Modify: `tests/test_db.py:347`（`test_get_recent_sessions` 替换）

**Interfaces:**
- Consumes: 无
- Produces: `list_session_summaries(conn, limit=50) -> list[dict]`，元素 `{id: str, title: str, message_count: int, last_active: float}`；`delete_session(conn, session_id: str) -> bool`（rowcount>0）。`get_recent_sessions` 删除（唯一调用方是其自身测试）。

- [ ] **Step 1: 写失败测试**（tests/test_db.py，替换 test_get_recent_sessions）

```python
def test_list_session_summaries(db_conn):
    from server.vault.db.sessions import save_session, list_session_summaries
    from server.agent.reminders import DocPathUserReminder
    # 空行（auto_approve 预建形态）不进列表
    save_session(db_conn, "empty", [])
    save_session(db_conn, "s1", [{"role": "user", "content": DocPathUserReminder._PREFIX + " a.md]"},
                                 {"role": "user", "content": "重构提案通道" * 30},
                                 {"role": "assistant", "content": "好"}])
    save_session(db_conn, "s2", [{"role": "assistant", "content": "x"}])  # 无 user
    out = list_session_summaries(db_conn)
    assert [s["id"] for s in out] == ["s2", "s1"]          # last_active DESC
    t = next(s for s in out if s["id"] == "s1")["title"]
    assert not t.startswith(DocPathUserReminder._PREFIX)    # 跳提醒行
    assert t.startswith("重构提案通道") and len(t) <= 41    # 40 字符 + 可选省略号
    assert next(s for s in out if s["id"] == "s1")["message_count"] == 3
    t2 = next(s for s in out if s["id"] == "s2")["title"]
    assert t2 == "工具会话 · 1 条"                          # 兜底链
    assert "[当前打开:" == DocPathUserReminder._PREFIX      # 三处同源钉（spec §1.1）

def test_delete_session(db_conn):
    from server.vault.db.sessions import save_session, delete_session
    save_session(db_conn, "s1", [{"role": "user", "content": "hi"}])
    assert delete_session(db_conn, "s1") is True
    assert delete_session(db_conn, "s1") is False
```

- [ ] **Step 2: 跑测试确认失败**（`pytest tests/test_db.py -k "session_summaries or delete_session" -v`，预期 ImportError）
- [ ] **Step 3: 实现**——`list_session_summaries`：SQL 按 `last_active DESC LIMIT ?` 取 `id, messages, last_active`，Python 侧 `json.loads` 后投影；过滤 `not msgs`；title 取**首条** `role=='user'` 且不以 `_PREFIX` 开头（从 `..agent.reminders` import 常量？——**不要**跨层 import，agent→vault 反向依赖；用模块级 `_REMINDER_PREFIX = "[当前打开:"` + 注释指回 reminders.py:65，一致性由 Step 1 的同源钉守住）；`len<=40` 不截，否则 `[:40] + "…"`（截 40 加省略号后 len==41，测试如此）。`delete_session`：`DELETE WHERE id=?` 返回 `cursor.rowcount > 0`。删除 `get_recent_sessions`；README 表行改列两个新函数；同步 `test_get_recent_sessions` 已删。
- [ ] **Step 4: 全绿**（`pytest tests/test_db.py -v`）
- [ ] **Step 5: Commit** `feat(db): sessions 摘要列表+删除原语（会话管理 T1）`

### Task 2: 会话路由三件套 + SessionManager.remove

**Files:**
- Modify: `server/vault/db/sessions.py`（不动，消费 T1）
- Modify: `server/agent/session.py`（加 `remove`）
- Modify: `server/routes/agent.py`（register() 内 feedback 路由后新增三路由）
- Modify: `tests/test_routes_order.py:62-72`（agent 域 7→10 条，42→45，trash 注释重编号）
- Create: `tests/test_agent_sessions.py`

**Interfaces:**
- Consumes: T1 `list_session_summaries` / `delete_session`
- Produces: `GET /api/agent/sessions` → `{"sessions":[{id,title,message_count,last_active}]}`（db None → `{"sessions":[]}`）；`GET /api/agent/sessions/{sid}` → `{"session_id","messages","token_count","last_active"}`，无行 404；`DELETE /api/agent/sessions/{sid}` → `{"ok":true}` / 404 / 409（锁持有）；`SessionManager.remove(sid) -> None`（pop `_messages`/`_last_active`/`_locks` 三表）

- [ ] **Step 1: 写失败测试**（tests/test_agent_sessions.py；`_mock_openai`/`_spy_agent_cache` 小工具从 `tests/test_privacy_docpath_injection.py:48-91` 原样复制进本文件，测试文件间不互 import）

```python
def test_sessions_list_route(client, db_conn): ...       # save_session 造两行 → GET 形状/排序/无空行
def test_session_detail_route(client, db_conn): ...      # 200 形状四键；GET /api/agent/sessions/nope → 404
def test_delete_route_pops_cache_and_no_resurrect(client, db_conn, monkeypatch):
    # _spy_agent_cache 捕获 agent_cache；_mock_openai 下 POST /api/agent/chat 建会话；
    # DELETE → 200；load_session None；captured cache 无该 sid；
    # 再 POST chat（同 sid）→ load_session messages 仅 1 user 消息（无复活）
def test_delete_route_locked_409(client, monkeypatch):
    # spy 捕获 locks 表；locks["s1"]._locked = True（内存 seam，仿 test_sync_lock 缝上拒绝不落盘）→ DELETE 409
def test_sessions_routes_without_db(tmp_path, ai_config):
    # create_app(db_conn=None)：GET /api/agent/sessions → {"sessions":[]}；
    # GET /api/agent/sessions/x → 503；DELETE → 503（spec §3 表）
def test_session_manager_remove(): ...                   # 三表 pop；无 sid 静默
```

（测试体按 Interfaces 的形状断言写全，非伪码。）
- [ ] **Step 2: 跑测试确认失败**（404/AttributeError）
- [ ] **Step 3: 实现**——routes/agent.py：三路由照 Interfaces 形状；DELETE 顺序 = 锁检查（409）→ db `delete_session`（False→404）→ `session_mgr.remove(sid)` + `agent_cache.pop(sid, None)` + `_agent_last_active.pop(sid, None)`；`ctx.db_conn is None` → GET 列表 `[]`、GET 详情/DELETE 503。session.py `remove`：三 dict 各 `pop(sid, None)`。
- [ ] **Step 4: 更新路由快照**：test_routes_order.py EXPECTED agent 块后追加三行（注释 `# 43 list` `# 44 detail` `# 45 delete`），trash 两行注释号 41/42→46/47，头注 42→45。
- [ ] **Step 5: 全绿**（`pytest tests/test_agent_sessions.py tests/test_routes_order.py -v`）
- [ ] **Step 6: Commit** `feat(agent): 会话列表/详情/删除路由 + SessionManager.remove（会话管理 T2）`

### Task 3: 恢复映射模块 chatRestore.js（node 可测）

**Files:**
- Create: `server/templates/js/utils/chatRestore.js`
- Modify: `tests/test_web_ui_audit.py`（文件尾追加 node 测试）

**Interfaces:**
- Consumes: 服务端持久化 messages（LLM 形态：role user/assistant/tool，assistant 或含 `tool_calls:[{id,function:{name,arguments}}]`，终帧 assistant 或含 `id`/`citations`；tool 消息含 `tool_call_id`/`content`）
- Produces: `mapHistory(raw) -> [{ role:'user'|'assistant', blocks:[Block], messageId?: string, citations?: array }]`，Block = `{type:'text',text}` 或 `{type:'tool',id,name,args,result,status,expanded:false}`；status ∈ `'done'|'error'|'rejected'`

- [ ] **Step 1: 写失败 node 行为测试**（test_web_ui_audit.py 追加，照 :483-497 的 `import(as_uri)` + `process.exit(n)` 模式；exit code 逐个区分用例）

断言集（用例数据在测试内构造原始 messages）：
1. 提醒行丢弃：`[{"role":"user","content":"[当前打开: a.md]"},{...}]` → 无对应 user 块；
2. 普通 user → `{role:'user',blocks:[{type:'text',...}]}`；
3. 多轮归并：assistant(content 空, tool_calls×1) → tool 结果 → assistant(content, tool_calls×1) → tool 结果(`Error: boom`) → assistant 终帧(`content:'完成', id:'abc', citations:[…]`) → **一条** assistant 描述符：blocks = [text?, tool(done), tool(text), tool(error), text(完成)]，`messageId==='abc'`，citations 透传；
4. `result === 'User rejected this operation.'` → status `'rejected'`；
5. 孤儿 tool_call（无 tool 消息）→ status `'error'`；
6. assistant.content 为 null/空 → 不产 text 块；
7. 非末轮 assistant 无 id/citations → messageId/citations 不出现。

- [ ] **Step 2: 跑测试确认失败**（`pytest tests/test_web_ui_audit.py -k chat_restore -v`，import 失败）
- [ ] **Step 3: 实现** `mapHistory`：单遍扫描；`toolCallIndex = {}`（tool_call_id → {block, msg}）；连续 assistant/tool 段并入当前 assistant 描述符，遇非提醒 user 或扫描结束即封段；tool 消息到达回填 result 并定 status（`Error` 前缀→error、逐字 `User rejected this operation.`→rejected、否则 done）；段封盘时未被回填的 tool 块 → error。零 import 零依赖（Vue 不可 import——node 环境没有）。
- [ ] **Step 4: 全绿**（node 缺失则 skip，与 format.js 测试同政策）
- [ ] **Step 5: Commit** `feat(ui): chatRestore 历史映射模块（会话管理 T3）`

### Task 4: AIChatPanel 历史视图 + 恢复分支 + 删除/切换交互

**Files:**
- Modify: `server/templates/js/components/AIChatPanel.js`
- Modify: `server/templates/js/api.js`（+3 方法）
- Modify: `tests/test_ui.py`（`test_api_js_exports_all_endpoints` 纳入 `/api/agent/sessions`）
- Modify: `tests/test_web_ui_audit.py`（静态钉）

**Interfaces:**
- Consumes: T2 三路由；T3 `mapHistory`
- Produces: props 新增 `restorePayload: { type: Object, default: null }`（`{sid, data}`，app 层 Task 5 喂）；api 方法 `listSessions()` / `getSession(sid)`（非 2xx throw）/ `deleteSession(sid)`（非 2xx throw）；组件内 `view: 'chat'|'history'`

- [ ] **Step 1: 写静态钉（先失败）** test_web_ui_audit.py：
  - `test_session_history_view_pins`：历史钮 `aria-label="历史会话"` + `aria-pressed`；`view === 'history'` 模板分支存在；历史行主体 `<button`（行 wrapper 内删除钮是兄弟非后代）；`toggleView` 函数体正则提取后**零** `abort`（Review Focus 1）；`watch(() => props.sessionId` 体内含 `view.value = 'chat'` 与 `restorePayload`；Esc 处理与 `history` 守卫同体。
  - test_ui.py 端点钉补 `/api/agent/sessions`。
- [ ] **Step 2: 跑钉确认失败**
- [ ] **Step 3: 实现（AIChatPanel.js）**：
  - setup：`view = ref('chat')`、`historyList = ref([])`、`deletingId = ref(null)`（两步删除态）、`confirmSwitchId = ref(null)`（流式切换确认态）、`toggleView`（history 展开时 `loadHistory()`：`api.listSessions()`，失败 toast——toast 复用 app 层 inject 现用件；组件无 toast 通道则 console + 列表显「加载失败，重试」行内钮）、`fmtRel` 走 `../utils/format.js`。`emits` 数组（:31）补 `'switchSession'`。
  - 头部（ai-acts:534）：历史钮插新会话钮左侧；`view==='history'` 时 ai-id 区换「← 返回」（`@click="toggleView"`）+「历史会话」。
  - 消息区（:555）按 `view` 切模板：history 列表 = `div` 行 `[button.row(标题/fmtRel/当前高亮 is-current) + button.del(×)]`；`status==='thinking' && row.id===props.sessionId` 隐藏 del；deletingId 行态 = 「删除？ 确认/取消」两钮；confirmSwitchId 行态 = 「停止并切换？ 是/否」两钮。行点：非当前会话且 `status==='thinking'` → 置 confirmSwitchId；否则 `switchTo(row.id)`。
  - `switchTo(sid)`：`await api.getSession(sid)`（catch：toast/inject 通道 → `loadHistory()` 刷新，现场不动）→ `emit('switchSession', { sid, data })`（app 层做 sessionId+restorePayload 原子换，Task 5）。**组件不直接换 sessionId**。
  - 删除：确认 → `api.deleteSession(sid)` → 行移除；`sid === props.sessionId` → `emit('newSession')`（自动新会话）。
  - Esc：textarea `onKeydown` 不动；history 视图 keydown 监听加在面板根（`@keydown.esc` 容器级，仅 `view==='history'` 生效 → `toggleView()`）。
  - 恢复分支（watch(sessionId:472)）：首行前判断 `props.restorePayload?.sid === newSid` → `restore(props.restorePayload.data.messages)` 替代 `messages.value = []`；`restore` = `mapHistory()` 描述符 → `shallowReactive({ id: ++msgSeq, role, blocks, streaming:false, rendered: role==='assistant', blocksLen: blocks.length, uiTick: 0, messageId, citations })` 逐个 push 后 `scrollToBottom(true)`。**rendered:true 是恢复行出 markdown 的前提**。
  - 新会话钮在 history 视图保留（现状钮事件直通 app 层 startNewSession；回到 chat 视图由 watch(sessionId) 的 `view.value='chat'` 达成）。
  - api.js：三方法照 agentConfirm 风格（getSession/deleteSession 非 2xx `throw new Error(...)`）。
- [ ] **Step 4: 钉全绿**（`pytest tests/test_web_ui_audit.py tests/test_ui.py -k "history or sessions or chat_restore" -v` + 全量 `pytest tests/ -q` 不退化）
- [ ] **Step 5: 目检闸（AGENTS.md 规则 3）**：请用户浏览器目检 spec §5 目检项 ①②③④⑤⑥（刷新⑦留待 T5）——流中开列表流不断 / 切换确认 / 全保真观感 / 删除两步 / Esc / 键盘可达。**未批不 commit**。
- [ ] **Step 6: Commit** `feat(ui): AIChatPanel 历史视图+全保真恢复分支（会话管理 T4）`

### Task 5: app.js 接线——存储、启动恢复、切换落地

**Files:**
- Modify: `server/templates/js/app.js`（state:31、startNewSession:461、AIChatPanel 挂载点:1121、新 selectSession）
- Modify: `tests/test_web_ui_audit.py`（静态钉）

**Interfaces:**
- Consumes: T4 `emit('switchSession', {sid, data})`、api 三方法
- Produces: `state.sessionId` 生命周期（新会话/切换/启动恢复）；`state.restorePayload`；localStorage 键 `mdvault.aiSession`

- [ ] **Step 1: 写静态钉（先失败）** `test_session_persistence_pins`：app.js 含 `mdvault.aiSession`；`localStorage.setItem` 恰 2 个调用点（startNewSession 成功处 + selectSession 成功处，各带 try/catch——grep 计 `setItem` 出现 2 次）；`getItem` 处 try/catch（Review Focus 5）；启动恢复路径 `api.getSession` 成功后**同一函数内**赋 `state.sessionId` 与 `state.restorePayload`（原子换，读序断言：restorePayload 赋值先于 sessionId）；模板挂载点含 `:restore-payload`。
- [ ] **Step 2: 跑钉确认失败**
- [ ] **Step 3: 实现**：
  - state 增 `restorePayload: null`；`sessionId` 初值保持现状 `'web-' + Date.now()`（启动恢复在其上原子换，空初值路径会炸 doSend 兜底，别留空窗）。
  - 模块级 `persistSessionId(sid)`（try/catch）。
  - `startNewSession()`（:461）：`state.sessionId = data.session_id` 后加 `persistSessionId(data.session_id)` + `state.restorePayload = null`。
  - 新 `async function bootRestoreSession()`：try 读 getItem → 无值直接 return；`const data = await api.getSession(sid)` → `state.restorePayload = { sid, data }` → `state.sessionId = sid`；catch（404/网络/存储炸）→ 静默保当前新会话。挂载流程中 `loadDocs()` 旁 await 调用（非阻塞渲染：catch 内不发 toast，静默是新用户路径）。
  - 模板（:1121）：AIChatPanel 加 `:restore-payload="state.restorePayload"` `@switchSession="onSwitchSession"`。
  - `onSwitchSession({ sid, data })`：`state.restorePayload = { sid, data }` → `state.sessionId = sid` → `persistSessionId(sid)`（顺序：payload 先于 sessionId，panel watch 消费时 payload 必在场）。
- [ ] **Step 4: 全绿**（新钉 + `pytest tests/ -q` 全量不退化）
- [ ] **Step 5: 目检闸**：spec §5 目检项 ⑦——会话中 F5 刷新 → 同会话消息原位恢复可续聊；隐私模式开页不炸。未批不 commit。
- [ ] **Step 6: Commit** `feat(ui): 会话 id 持久化+启动恢复（会话管理 T5）`

---

## 收尾（无独立任务）

- 全量 `pytest tests/ -q` 绿 + spec §5 目检 ①–⑦ 全过 → 用户在会话内确认 → 按 finishing-a-development-branch 决议合入。
- 待办 `3daabcb2` 标完成时机 = 合入后。
