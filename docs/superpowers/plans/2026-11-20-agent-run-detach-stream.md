# Agent 运行脱离连接（关 SSE 续跑 / 边界回放 / 服务端队列）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** LLM 运行从 HTTP 连接中独立出来（服务端 asyncio 任务 + 事件环形缓冲），重连做「快照前缀 + 边界回放 + live 尾巴」，回答中消息进服务端队列，停止显式化。

**Architecture:** 新 `RunHub`（每会话一个 asyncio 驱动任务：持锁、消费队列、事件入 buffer 广播、轮末 save+记边界）；`POST /api/agent/chat` 变纯投递，`GET /api/agent/stream` 统一恢复+订阅；前端事件分发器抽成 node 可测的 `chatEvents.js`，面板改常驻订阅 + 事件驱动渲染 + 队列面板。Spec：`docs/superpowers/specs/2026-11-20-agent-run-detach-stream.md`（rev2，oracle 评审后定稿——施工前通读，本计划不复述其理由）。

**Tech Stack:** FastAPI + asyncio（无新依赖）、Vue 3 CDN（无构建）、node 行为测试（`test_web_ui_audit.py` 的 `import(as_uri)` 模式）、pytest。

**Spec:** `docs/superpowers/specs/2026-11-20-agent-run-detach-stream.md`

## Global Constraints

- 测试命令 `.venv/bin/python -m pytest <target> -v`；全量基线 **1369 passed**，任何任务收尾零退化。
- **UI 布局类改动未经用户浏览器目检不得 commit**（AGENTS 规则 3）——T5/T6 的 commit 步骤带目检闸。
- SSE 载荷保持平铺 `{"type", ...}`（`prompts.sse()` 的既有格式），新事件仅新增顶层键 `run_id`、`seq`。
- 常量逐字取 spec 值：`BUFFER_MAX_EVENTS=20000`、`QUEUE_MAX=5`、`RUN_RETAIN=600.0s`、`confirm_timeout` 默认 `300.0`、历史列表轮询 4s、重连退避 1s 起封顶 30s。
- a11y：新增交互控件一律真 `<button>`；装饰 svg `aria-hidden="true"`。
- 路由表由 `tests/test_routes_order.py` 快照钉死：agent 域 10→13，`EXPECTED` 45→48 行（三条新路由注册在 feedback 路由之后、sessions 三路由之前；trash 注释号 +3 顺延）。
- 新配置面 `confirm_timeout` 同步 `config.example.yaml`。

## Review Focus

（spec 隐含、单任务测试不直接 exercise 的易咬人面；每条已把钉写进归属任务）

1. **断连 ≠ 停止**——SSE 订阅关闭后驱动任务必须继续跑且事件全量可回放（历史回归面：现状就是断连即夭折，改造不彻底会残留 finally 半吊子）。→ T1 断连免疫测试。
2. **刷新不吞前缀（B1 本体）**——有活跃 Run 的会话刷新，snapshot 帧的历史前缀必须完整、回放只从边界起（重复与缺帧双向）。→ T1 边界测试 + T3 路由形状测试 + 目检①。
3. **stop/超时后 pending_confirms 泄漏**——僵尸条目会永久豁免 TTL 且迟到 confirm 假 200；超时与确认同帧不可双裁决。→ T2 三条测试。
4. **保留期内删会话**——DELETE 不清 RunHub buffer 则删掉的会话从回放复活。→ T3 回归钉。
5. **出队重验**——排队期间文档删除/入隐私区，出队注入前必须重跑三连校验（隐私红线 5：路径进模型上下文即出域）。→ T3 测试。
6. **save 先于终帧**——客户端见终帧即可能重连走快照路径，边界必须先于终帧入 buffer。→ T1 顺序钉。

---

### Task 1: RunHub 核心（纯内存运行体，无 HTTP）

**Files:**
- Create: `server/agent/runhub.py`
- Create: `tests/test_runhub.py`

**Interfaces:**
- Consumes: `Agent.chat(message, **doc_kwargs) -> AsyncIterator[Event]`（`events.py`，`Event(type, data)`）；`asyncio.Lock`（调用方注入的 `get_lock(sid)`）。
- Produces（T3 依赖的精确面）:
  ```python
  class QueueFull(Exception): ...
  class RunHub:
      def __init__(self, get_agent, get_lock, persist, load_messages, prepare_doc,
                   *, retain=600.0, buffer_max=20000, queue_max=5): ...
          # get_agent(sid)->Agent; get_lock(sid)->asyncio.Lock;
          # persist(sid)->None(轮末落库); load_messages(sid)->list[dict](DB 快照读);
          # prepare_doc(raw_doc_path:str|None)->dict(agent.chat 的 doc kwargs，出队侧重验在此)
      def submit(self, sid: str, message: str, doc_path: str | None) -> str  # -> run_id；QueueFull 超限；无活跃 Run 时 create_task(_drive)
      async def subscribe(self, sid: str, *, run_id: str | None = None,
                          cursor: int = 0, snapshot: bool = False) -> AsyncIterator[dict]
          # 帧=dict，含 type/run_id/seq。snapshot=True：先 {"type":"snapshot","messages":[...],
          #   "persisted_through_seq":n,"run_id":rid}(不入 buffer) 再 run_status 起回放
          #   seq>=persisted_through_seq 之后的 buffer 尾；无 Run：snapshot 帧(load_messages)+
          #   {"type":"run_status","running":False,"state":None,"run_id":None,"seq":0} 即终。
          # snapshot=False：Run 缺失或 run_id 不匹配 → {"type":"resync","seq":0} 即终；
          #   否则回放 seq>cursor（cursor 早于缓冲头 → 夹到缓冲头）。终帧 run_status{running:False}。
      def stop(self, sid: str) -> bool            # cancel + 清队列；对 get_agent(sid).pending_confirms 逐项 entry["event"].set()（approved 保持 None → 按拒绝落定）再 cancel；广播 queue_update[]；无 Run → False
      def queue_remove(self, sid: str, qid: str) -> None   # 幂等；成功清才广播
      def is_running(self, sid: str) -> bool      # 仅活跃 Run（终止保留期不算）
      def discard(self, sid: str) -> None         # 清 Run 条目（含终止 buffer）
  ```
- buffer 事件形态：`{"type": ev.type, "run_id": rid, "seq": n, **ev.data}`；`_drive` 在轮 done 且队列非空时发下一轮（顺序：`user_message{qid,text}` → 该轮事件）；每轮（含 stopped/error 收尾轮）结束 `persist(sid)` → 记 `persisted_through_seq=buffer 尾 seq` → **然后才**广播终帧（Review Focus 6 顺序钉）；`except asyncio.CancelledError` 与 `except Exception` 分支分开，均走 save→记边界→终帧（stop 终帧 state="stopped"，崩溃 state="error" 且先补一条 error 事件）→ finally 放锁。惰性逐出：`submit/subscribe/stop` 入口调 `_evict_expired()`。

- [ ] **Step 1: 写失败测试**（`tests/test_runhub.py`，纯 asyncio，`FakeAgent` 按脚本 yield Event 并记录 chat 入参）

测试集（名称+断言核心，用 `pytest.mark.asyncio`，时钟注入 `retain=0.05` 类小值）：
  - `test_submit_starts_run_buffers_events`：submit → 订阅回放含 run_status(running)、user_message、token 序、done、run_status(false) 终帧；seq 单调连续；seq 与 run_id 每帧在场。
  - `test_queue_serial_drain`：运行中 submit×2 → queue_update 帧含两 qid → 每轮 done 后先 user_message(qid) 再下一轮事件；FakeAgent 收到的第二参 doc kwargs 来自 `prepare_doc` 的**出队时刻**返回（断言 prepare_doc 调用次数=消息数，参数含排队原文）。
  - `test_queue_max`：第 6 条 raise QueueFull。
  - `test_queue_remove_idempotent`：删除后 queue_update 少一条；删不存在的 qid 不广播不炸。
  - `test_stop_order_and_state`：运行中 stop → buffer 终段顺序 = persist 调用记录**先于** run_status{stopped} 帧（persist 侧记调用序，buffer 侧找 seq）；stop 返回 True；再 stop False。
  - `test_disconnect_immune`：订阅消费 3 帧后 break → 等驱动自然跑完 → snapshot=1 重订阅 → 全量事件在场且终帧 running:False。
  - `test_snapshot_boundary_no_dup_no_gap`：跑完轮 1、轮 2 进行至半时 snapshot=1：snapshot.messages=轮 1 后持久化全量（persist 侧记每次落库的 messages 快照引用），回放首帧 seq == persisted_through_seq+1，不含轮 1 事件。
  - `test_cursor_incremental_and_runid_mismatch`：run_id 匹配 → 只回 seq>cursor；不匹配/缺 run_id → 单帧 resync 即终。
  - `test_trim_clamp`：buffer_max=5，长 token 流后 snapshot 回放从现存头起、无空洞声明帧错序。
  - `test_lazy_eviction`：终止后 `await asyncio.sleep(retain+)` 再任意入口触碰 → is_running False 且 buffer 清空（下次 submit 新 run_id、seq 从 1）。
  - `test_discard_clears_terminal_buffer`：终止保留期内 discard → subscribe(snapshot=True) 走无 Run 空态路径。
  - `test_is_running_false_after_done_until_evict`：done 后终止态 is_running False。

- [ ] **Step 2: 跑测试确认失败**（`pytest tests/test_runhub.py -v`，ImportError）
- [ ] **Step 3: 实现 `runhub.py`**——`Run` 用 dataclass（run_id=uuid4().hex[:12]）；waiters 广播 = 同步 `queue.put_nowait(frame)`；订阅端 `while` 取 waiter 帧 + buffer 增量双源（先进入「回放→实时」无缝拼接：实现口径=订阅先把当前 buffer 快照入本地指针，然后注册 waiter，从指针起按 seq 去重续发——单事件循环内 append/注册同步步，无丢失窗口）。`_drive` 出队消息经 `prepare_doc(doc_path_raw)` 转 kwargs 后 `agent.chat(message, **kwargs)`。
- [ ] **Step 4: 全绿**（`pytest tests/test_runhub.py -v`）
- [ ] **Step 5: Commit** `feat(agent): RunHub 运行体（任务驱动/环形缓冲/边界回放/队列/停止）（运行脱离连接 T1）`

### Task 2: 确认超时 + pending_confirms 清理（runtime 侧）

**Files:**
- Modify: `server/agent/runtime.py:386-395`
- Modify: `server/agent/spec.py`（AgentSpec 加 `confirm_timeout: float = 300.0`）
- Modify: `server/config/ai.py`（AIConfig 同名字段，dataclass 默认 300.0）
- Modify: `server/routes/agent.py:85-109`（`_build_agent_spec` 传入 `confirm_timeout=ctx.ai_config.confirm_timeout`）
- Modify: `config.example.yaml`（ai 节注释行 `confirm_timeout: 300`）
- Test: `tests/test_agent.py`（追加，仿 :605 起 confirm 家族的 mock 模式）

**Interfaces:**
- Consumes: 既有 `agent.pending_confirms[tc_id] = {"event": asyncio.Event(), "approved": None}` 协议（`agent.confirm()` 不变）。
- Produces: 超时后该 tc 落 `tool_result(rejected)`；`pending_confirms` 任何路径不残留；`AgentSpec.confirm_timeout` 供 T1/T3 环境使用。

- [ ] **Step 1: 写失败测试**（test_agent.py 追加）
  - `test_confirm_timeout_auto_rejects`：`spec.confirm_timeout=0.05` → confirm_request 后无人应答 → 收到 `tool_result` 含 rejected 且文本为 `User rejected this operation.`，随后 done；`agent.pending_confirms == {}`。
  - `test_confirm_no_zombie_after_timeout`：上例之后 `agent.confirm("call_x", True) is False`（无僵尸条目，迟到假 200 面关闭）。
  - `test_confirm_and_timeout_single_verdict`：`confirm_timeout=0.1`，在 ~0.05s 时 confirm(True) → 断言该 tc 的 tool_result **恰好一条**（双裁决防重复落 tool 消息：超时分支判前 `entry["approved"] is not None` 即弃权重用确认值）。
- [ ] **Step 2: 确认失败**（当前无超时，测试挂起——给测试包 `asyncio.wait_for` 断言失败，或先加 timeout 参数 ImportError 即红）
- [ ] **Step 3: 实现**——runtime.py 确认块：`entry = {...}` 入表 → `yield confirm_request` → `try: await asyncio.wait_for(entry["event"].wait(), timeout=spec.confirm_timeout)` `except asyncio.TimeoutError:` → `if entry["approved"] is None:` 按 rejected 走既有拒绝分支，else 用已落定的 approved；`finally: agent.pending_confirms.pop(tc["id"], None)`（原 `del` 删除）。三处字段（AgentSpec/AIConfig/_build_agent_spec）加 `confirm_timeout`。
- [ ] **Step 4: 全绿**（`pytest tests/test_agent.py -v` 全文件）
- [ ] **Step 5: Commit** `feat(agent): ask 确认超时自动拒绝 + pending_confirms finally 清理（运行脱离连接 T2）`

### Task 3: 路由面接线（投递/订阅/停止/队列/徽标/删除）

**Files:**
- Modify: `server/routes/agent.py`（chat 改造 + 三新路由 + sessions running + DELETE discard）
- Modify: `tests/test_routes_order.py`（EXPECTED 45→48，docstring 计数）
- Modify: `tests/test_agent_sessions.py`（running 字段 + DELETE discard 断言）
- Modify: `tests/test_privacy_docpath_injection.py` 与 `tests/test_api.py` 中所有打 `/api/agent/chat` 读 SSE 的旧助手 → 迁移到两拍新助手（下附）
- Create/Modify: `tests/test_agent_stream_routes.py`（新路由专项）

**Interfaces:**
- Consumes: T1 `RunHub` 全签名；T2 spec 字段。
- Produces: 路由面 = spec §6 表逐字（202 `{ok, run_id}` / stream 查询参 `session_id,run_id,cursor,snapshot` / stop `{ok,stopped}` / queue/remove `{ok}` / sessions 元素 +`running` / DELETE 成功路径 `runhub.discard(sid)`）。register() 内构造 `runhub = RunHub(get_agent=_get_agent, get_lock=session_mgr.get_lock, persist=lambda sid: session_mgr.save(sid, _get_agent(sid).messages), load_messages=..., prepare_doc=...)`——`load_messages`：db None → `[]`，否则 `load_session(...)`["messages"] 或 `[]`；`prepare_doc`：出队侧重验三连（`_safe_rel_path` + `is_file` + `_privacy_hit`→privacy_doc），非法/缺失降级 None kwargs（不发 409，投递时前置检查仍在）。

- [ ] **Step 1: 写两拍测试助手 + 失败测试**（新文件 test_agent_stream_routes.py；`_mock_openai`/`_spy_agent_cache` 从 test_privacy_docpath_injection.py:48-91 复制）

迁移助手（旧 SSE 单拍全部换它）：
```python
def _run_chat(client, message, session_id="s1", doc_path=""):
    r = client.post("/api/agent/chat", json={"message": message, "session_id": session_id,
                                             "doc_path": doc_path})
    assert r.status_code == 202, r.text
    lines = []
    with client.stream("GET", "/api/agent/stream",
                       params={"session_id": session_id, "snapshot": "1"}) as resp:
        for raw in resp.iter_lines():
            if raw.startswith("data: "):
                lines.append(raw)
    return lines
```
新专项测试：
  - `test_chat_post_returns_json_not_sse`：200/202，`{"ok": true, "run_id": <str>}`，无 `queued` 键。
  - `test_stream_snapshot_shape`：无运行会话 → `snapshot` 帧（messages/persisted_through_seq/run_id）→ `run_status{running:false,state:null}`；有历史会话 → messages 为持久化全量。
  - `test_stream_cursor_and_resync`：run_id 不匹配 → 单 `resync` 帧。
  - `test_stream_live_tail_after_submit`：mock 慢流下 submit 后 cursor 订阅收到 live 帧直至终帧（TestClient 并发用 `anyio.from_thread`/后台线程或改直接驱动 runhub——实现者选可确定性写法，允许直调 `app.state.runhub`——**路由注册时挂 `app.state.runhub = runhub` 供测试 seam**）。
  - `test_stop_route`：运行中 `{ok:true,stopped:true}` + 会话终帧；空会话 `{ok:false,stopped:false}`；stop 后该会话 `agent.pending_confirms` 为空（Review Focus 3 路由侧钉：stop 路径清 pending——runhub.stop 内对 `get_agent(sid).pending_confirms` 逐项 `entry["event"].set()`（approved 保持 None 即按拒绝落定）再 cancel）。
  - `test_queue_remove_route`：`{ok:true}` 幂等。
  - `test_dequeue_revalidates_privacy`（Review Focus 5）：入队排队期间把 doc 移入隐私区（或磁盘删除）→ 该轮 `agent.chat` 收到 `doc_path=None`（隐私时 `privacy_doc=True`），用 spy Agent 捕获 kwargs。
  - `test_sessions_list_running_flag` + `test_delete_clears_runhub_buffer`（Review Focus 4）：运行完（终止保留期内）DELETE → GET stream snapshot → 走空态（不再回放）。
  - 旧文件迁移：`grep -rn "api/agent/chat" tests/` 逐处换 `_run_chat`，事件断言字符串保持（帧格式兼容证明）。
- [ ] **Step 2: 跑测试确认失败**（405/404/KeyError）
- [ ] **Step 3: 实现 routes/agent.py**——chat：前置校验保留 → `runhub.submit`（QueueFull→409）→ `return JSONResponse({"ok": True, "run_id": rid}, status_code=202)`（spec §6 逐字；handler 签名去 `-> dict`）；stream：`StreamingResponse`，`async for frame in runhub.subscribe(...): yield sse(frame.pop("type") 之平铺, frame)`（复用 `sse()`，帧 dict 已平铺）；stop/queue-remove 按表；sessions 列表 dict-comprehension 注 `running: runhub.is_running(s["id"])`；DELETE 在 `session_mgr.remove` 后加 `runhub.discard(sid)`。TTL/驱逐交互：`_evict_stale_agents` 追加参数 `runhub.is_running(sid)` 即跳过（活跃 Run 持锁已被既有 lock 检查覆盖——加注释点破，不加代码）。
- [ ] **Step 4: 路由快照**：EXPECTED agent 块 +3 行（stream/stop/queue.remove，注释号顺延），头注 45→48。
- [ ] **Step 5: 全绿**（`pytest tests/test_agent_stream_routes.py tests/test_agent_sessions.py tests/test_routes_order.py tests/test_privacy_docpath_injection.py tests/test_api.py tests/test_agent.py -v` + 全量 `-q` 零退化）
- [ ] **Step 6: Commit** `feat(agent): 投递/订阅/停止/队列路由 + 列表 running + 删除清 buffer（运行脱离连接 T3）`

### Task 4: `chatEvents.js` 纯分发器（node 可测）

**Files:**
- Create: `server/templates/js/utils/chatEvents.js`
- Modify: `tests/test_web_ui_audit.py`（尾部追加 node 行为测试）

**Interfaces:**
- Consumes: 服务端平铺事件帧（spec §6 表）。
- Produces（T5 依赖）: `export function applyEvent(ctx, evt) -> string[]`——`ctx = { messages, currentTurn, queue, busy, tokenUsage, runId, cursor, terminal }`（全部 plain 对象/数组，**零 Vue 依赖**）；就地更新 ctx，返回 DOM 指令数组 `[]|['stream','thinking']`（token/reasoning 需直写 DOM 的标记；T5 面板据此写 streamTextEl/thinkingTextEl）。行为规约：
  - `snapshot`：`ctx.snapshot = {messages, persisted_through_seq}` 挂 ctx（面板负责渲染），runId 记录；
  - `run_status`：busy=running；终帧（running:false）→ `terminal=true` + 扫 messages 把 `status==='confirm'` 工具块置 `rejected`（spec §6 收尾钉）；
  - `user_message`：push `{role:'user', text}` 进 messages（气泡唯一来源）；
  - `queue_update`：`ctx.queue = queued`；
  - `resync`：`ctx.resync = true`（面板重开 snapshot 订阅）；
  - token/reasoning/tool_call/tool_result/confirm_request/tool_usage/error/done：与现面板 doSend switch 分支语义逐一对齐（token 无 currentTurn 则建 turn，done 收尾 turn 但 **busy 不变**——忙态只归 run_status）；
  - **run_id 守卫**：`evt.run_id` 存在且 ≠ ctx.runId（ctx.runId 非 null 时）→ 忽略并返回 `[]`；`evt.seq` 记 `ctx.cursor`。

- [ ] **Step 1: 写失败 node 行为测试**（test_web_ui_audit.py 尾部，照 :483 `import(path.as_uri())`+`process.exit(n)` 模式；exit code 分用例）：新五分支各一断言 + run_id 异源丢帧 + confirm 终帧收尾 + done 不改 busy + seq 游标推进 + 静态钉（文件零 `import` 语句、零 `Vue`）。
- [ ] **Step 2: 确认失败** → **Step 3: 实现** → **Step 4: 全绿**（`pytest tests/test_web_ui_audit.py -k chat_events -v`）
- [ ] **Step 5: Commit** `feat(ui): chatEvents 事件分发器纯函数（运行脱离连接 T4）`

### Task 5: AIChatPanel 重构（常驻订阅/队列面板/停止/徽标）+ api.js

**Files:**
- Modify: `server/templates/js/components/AIChatPanel.js`
- Modify: `server/templates/js/api.js`（+`agentStream(sid,{runId,cursor,snapshot})`（fetch 返回 Response，同 agentChat 风格）、`agentStop(sid)`、`agentQueueRemove(sid,qid)`）
- Modify: `tests/test_ui.py`（端点钉 +`/api/agent/stream`、`/api/agent/stop`、`/api/agent/queue/remove`）
- Modify: `tests/test_web_ui_audit.py`（钉翻转+新增组）

**Interfaces:** Consumes T1-T4 全部后端面 + `applyEvent`。**Produces:** 面板内部 `sub`（订阅控制器：open/close/reconnect 退避）、`queuePanel` 数据源 `ctx.queue`、`status` 由 run_status 驱动。`emit('switchSession', sid)` 载荷**从 {sid,data} 改裸 sid**（T6 消费）。

- [ ] **Step 1: 静态钉先红**（test_web_ui_audit.py 改写）：删除切换确认组（`confirmSwitchId` 相关③b/⑨ 断言连同 `_fn_body` 提取体）；翻转组：send 钮无 `hidden: status === 'thinking'`、`watch(() => props.sessionId` 体内零 `abort`（换 `closeSub()`）、`onUnmounted` 零 `abortCtrl`、`stopChat` 体含 `agentStop`；新组：`agentStream(` 在场、退避常量（`30000`）、队列面板容器 `id="ai-queue-panel"` 行内 `<button` 删除钮、history 行 spinner（`history-running` 类 + `aria-hidden`）、`setInterval` 4000 与 history 视图守卫、`applyEvent` import。
- [ ] **Step 2: 跑钉确认失败**
- [ ] **Step 3: 实现面板**——
  - `sub` 控制器：`openSub(snapshot=true)` 起 `api.agentStream` reader，行解析复用现 doSend 的 buffer/split 管线 → 每帧 `applyEvent(ctx, evt)`；指令数组含 'stream'/'thinking' → 现 DOM 直写（streamDisplayText 包裹不变）；断线（非终帧、非卸载）→ 指数退避带 run_id+cursor 重连；`resync` → `openSub(snapshot=true)`；`ctx.snapshot` 在场 → 既有 `restoreMessages(messages)` 渲前缀（chatRestore 管线复用）。
  - `sendChat` → `submit()`：POST，无 reader，失败 toast/console（输入已清空则不回填——投递失败才回填，队列成功即清）。`doSend`/`sendChain`/`abortCtrl`/`stopChat 的 abort` 拆除；`stopChat` = `api.agentStop(props.sessionId)`（订阅留着看终帧）。
  - `watch(sessionId)`：`closeSub()` → 重置（现重置面除 messages 外保留）→ `view='chat'` → `openSub()`。挂载 openSub 一次。
  - 队列面板（输入框上方）：`v-for q in ctx.queue` 行 = 截断文本 + `×`（`@click.stop` + `queueRemove`）；空隐藏。忙态输入框不锁、send 钮恒显（运行中点击=入队）；stop 钮显隐改 `status==='thinking'`（run_status 驱动 status）。
  - history 行：`row.running` → spinner svg（`aria-hidden` + `title="运行中"`，× 位左侧）；`loadHistory` 定时器：`watch(view)` history → `setInterval(loadHistory, 4000)`，离开/卸载清；`switchTo` 不再 fetch getSession——行点击直接 `emit('switchSession', row.id)`（流式中确认框与 404 兜底面随之前移的切换语义一并消失）。
  - `restorePayload` prop 与 app 层喂入路径**废弃删除**（恢复归订阅）。
- [ ] **Step 4: 钉全绿**（`pytest tests/test_web_ui_audit.py tests/test_ui.py -v` + 全量零退化 + `node --input-type=module --check < AIChatPanel.js`）
- [ ] **Step 5: 目检闸**：spec §10 目检 ①–⑧ 中不依赖 T6 的部分（①需刷新——T6 后完整版，此处先验开面板即订阅+切走切回不打断 ②③④⑤⑥⑦⑧）。**未批不 commit**。
- [ ] **Step 6: Commit** `feat(ui): AIChatPanel 常驻订阅+队列面板+真停止+运行徽标（运行脱离连接 T5）`

### Task 6: app.js 接线简化（boot 恢复去 fetch 化 / 切换载荷拆除）

**Files:**
- Modify: `server/templates/js/app.js`（startNewSession:461 区、bootRestoreSession、onSwitchSession、模板挂载点）
- Modify: `tests/test_web_ui_audit.py`（`test_session_persistence_pins` 改写）

**Interfaces:** Consumes T5 `emit('switchSession', sid)`。Produces: `state.sessionId` 生命周期不变（新会话/切换/启动恢复三写点 + persistSessionId 恰 2 调用点不变）；`state.restorePayload` **整体退役**。

- [ ] **Step 1: 钉改写先红**：`test_session_persistence_pins`——删「读序 payload 先于 sessionId」「模板 :restore-payload」断言；新断言：bootRestore 体内**零** `api.getSession` 调用（恢复改由面板订阅承担）+ 仍有 `mdvault.aiSession` 读写、try/catch、setItem 恰 2；历史/模板钉：`@switchSession="onSwitchSession"` 在场、`restorePayload` 在 app.js 与 AIChatPanel.js 双零出现（合审跨文件钉放本任务）。
- [ ] **Step 2: 确认失败** → **Step 3: 实现**：`bootRestoreSession()` 只 getItem→`state.sessionId = sid`（catch 静默）；`onSwitchSession(sid)` = sessionId 赋值 + persistSessionId；模板删 `:restore-payload`；startNewSession 不变。
- [ ] **Step 4: 全绿**（`pytest tests/test_web_ui_audit.py -k "persistence or restore" -v` + 全量 `-q` 零退化）
- [ ] **Step 5: 目检闸（终版）**：spec §10 目检清单 1–8 全量走一遍（重点 ①刷新前缀+半截轮、④停止回填输入框、⑦临时把 confirm_timeout 调 5s 验自动拒绝）。**未批不 commit**。
- [ ] **Step 6: Commit** `feat(ui): boot 恢复去 fetch 化 + switchSession 载荷简化（运行脱离连接 T6）`

---

## 收尾（无独立任务）

- 全量绿 + §10 目检 8 项过 → 用户确认 → finishing-a-development-branch 决议合入；合入后销待办 `aa6a2096`。
- 合入前建议跑一次真实端点手测（真实模型流式下断网/刷新），mock 测不到真 cancel 时序。
