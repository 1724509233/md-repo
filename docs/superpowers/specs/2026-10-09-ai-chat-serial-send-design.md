# AI Chat 串行发送与会话重置设计

## 背景与动机

`AIChatPanel.js` 当前存在两个问题：

1. **新会话不清空历史**：点击"新会话"按钮后，`state.sessionId` 更新但组件内 `messages` 未清空，UI 显示旧对话历史而实际上下文已切换。
2. **流式推理中连续发送导致并行 fetch**：`sendChat()` 在 `status === 'thinking'` 时仍可被 Enter 键触发，每次调用直接 `abort()` 旧流并发新 fetch。由于 `abort()` 后旧 reader 的 `AbortError` 清理是异步的，多个 fetch 可能并行飞行，后端同时跑多个 `agent_loop`，前端多个 reader 同时写 `messages`。

**目标：**
- 新会话时清空 UI 历史（已完成，见下方"已实施"）
- 保证同一时间只有一个 chat fetch 在飞，后一条严格等前一条清理完才发送

**非目标：**
- 不修改后端 `agent_chat` 的并发处理（后端 per-session lock 是独立议题）
- 不改变发送按钮在 thinking 状态下隐藏的 UI 行为（Enter 键是唯一的中途打断入口）
- 不引入消息队列/批量发送

## 已实施

### 新会话清空历史

`AIChatPanel.js` 新增 `watch(() => props.sessionId, ...)`，sessionId 变化时：
- 中止进行中的流（`abortCtrl.abort()`）
- 清空 `messages`、`currentTurn`、`inputText`
- 状态回到 `ready`

## 方案设计：Promise 链串行发送

### 核心思路

将 `sendChat()` 拆为两层：

- **`sendChat()`**（入口）：校验输入、清空输入框、将 `doSend(text)` 追加到 Promise 链
- **`doSend(text)`**（实际发送）：中止旧流 → 等清理完 → fetch → reader 循环 → 清理

```
sendChain = Promise.resolve()

function sendChat() {
  const text = inputText.value.trim()
  if (!text) return
  inputText.value = ''
  sendChain = sendChain.then(() => doSend(text)).catch(e => {
    console.error('[ai-chat] sendChain error', e)
  })
}
```

### doSend 时序

```
doSend(text):
  1. if abortCtrl.value:          // 有旧流在跑
       oldCtrl.abort()            // 同步：关闭 fetch 连接
       await rAF                  // 等一帧（~16ms）：旧 reader 的 AbortError
                                  //   被 catch、finishTurn 执行完
       finishTurn(oldTurn)        // 兜底：确保旧 turn 收尾
  2. addMsg('user', text)
  3. setStatus('thinking')
  4. abortCtrl.value = new AbortController()
  5. try { fetch + reader 循环 }   // 现有逻辑不变
     catch (AbortError) { ... }
  6. abortCtrl.value = null
     setStatus('ready')
```

### 为什么能保证串行

1. `sendChain` 是 Promise 链，`doSend#N+1` 只在 `doSend#N` 的 async 函数 **resolve** 后才开始
2. `doSend#N` 的 reader 循环要么正常读完（`chunk.done`），要么被 `abort()` 抛 `AbortError` 进 catch——两种路径都走到步骤 6（`abortCtrl.value = null`）
3. 所以 `doSend#N+1` 开始时，`abortCtrl.value` 一定是 `null`，旧 reader 一定已停

### 连续 Enter 行为

```
Enter#1: sendChain → doSend#1 (fetch#1 开始流式输出)
Enter#2: sendChain → doSend#2 (排队等 doSend#1 resolve)
         doSend#1: abort → 16ms → catch → resolve
         doSend#2: abortCtrl=null → fetch#2 开始
Enter#3: 排队等 doSend#2 resolve
```

每次只有一条 fetch 在飞。用户连续按 Enter 的消息会依次发送，不丢失、不并行。

### 延迟分析

| 场景 | 额外延迟 | 说明 |
|------|---------|------|
| 上一次流已自然结束 | ≈ 0ms | `abortCtrl.value` 已是 `null`，跳过 abort 分支 |
| 上一次流正在输出（中途打断） | ≈ 16ms | 一帧 rAF，等旧 reader 清理。人感知不到 |

### stopChat 兼容

`stopChat()`（停止按钮）逻辑不变：`abort()` + `finishTurn` + `setStatus('ready')`。

但需要处理一个边界：如果用户先点停止（`stopChat`），再立即按 Enter 发送，此时 `abortCtrl.value` 已被 `stopChat` 设为 `null`，`doSend` 会跳过 abort 分支直接发新请求——正确。

如果用户在 `doSend` 的 abort 等待（rAF）期间点停止，`stopChat` 会再次 `abort()`（此时 `abortCtrl.value` 还是旧值）并 `finishTurn`。`doSend` 的 rAF 结束后会再调一次 `finishTurn`——需要幂等保护：

```js
function finishTurn(turn) {
  if (turn._finished) return   // 幂等
  turn._finished = true
  turn.streaming = false
  turn.fullText = turn.blocks.filter(b => b.type === 'text').map(b => b.text).join('\n\n')
  turn.rendered = true
}
```

### 改动范围

| 文件 | 改动 |
|------|------|
| `server/templates/js/components/AIChatPanel.js` | 1. 新增 `sendChain` 变量<br>2. `sendChat()` 改为追加到链<br>3. 新增 `doSend(text)` 包含现有 fetch 逻辑<br>4. `finishTurn()` 加幂等保护 |

不改后端、不改 `api.js`、不改模板。

## 验证

1. **正常发送**：输入消息 → Enter → 流式输出正常 → 状态回 ready
2. **中途打断**：流式输出中按 Enter 发新消息 → 旧流停止（已输出部分保留）→ 新消息流式输出
3. **连续快速 Enter**：连续按 3 次 Enter（不同消息）→ 三条消息依次发送，不并行
4. **停止按钮**：流式中点停止 → 状态回 ready → Enter 发送正常
5. **新会话**：点新会话 → 历史清空 → 输入发送 → 使用新 sessionId
6. **全库搜索模式**：切换到全库搜索 → 发送 → RAG 搜索 + agent chat 正常
