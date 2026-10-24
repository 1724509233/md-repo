# Spec：事件驱动 Scheduler 重构（trigger 武装 + executor 唤醒）

分支：`feat/memory-runtime` · 2026-10-24 · 依据 `2026-10-21-memory-runtime-design.md`（memory-runtime 基线）与 `2026-10-11-bgagent-design.md`

## 1. 背景与动机

memory-runtime 的两个 30 分钟轮询循环被定性为"伪事件驱动"：

- **`_executor_loop`**（`server/bgagent/scheduler.py:317`）：`await asyncio.sleep(1800)` 死循环。用户在记忆控制台采纳提案（`awaiting_review → adopted`）后，最坏要等满 30 分钟执行器才落地——人工动作之后没有快速反馈链路。
- **`_armed_loop`**（`scheduler.py:287`）：trigger agent 的武装检查每 30 分钟扫一次 DB 游标。spec 2026-10-21 §8.1 对 sentinel 本就有"低分召回提前唤醒"的意图标注，但实现上同样是定时轮询。

目标：事件到达 → 循环立即被唤醒 → 照常跑权威扫描；30 分钟定时器降级为兜底。人工采纳 → 执行器落地从"最坏 30min"变为"秒级"。

## 2. 设计决策（与用户逐项确认）

| 决策点 | 选择 | 理由 |
|---|---|---|
| 范围 | 只改两个轮询循环；`_agent_loop`（cron）不动 | cron 循环是计时器不是轮询，语义本就正确 |
| 事件语义 | **DB 游标为准，事件只负责唤醒** | 事件丢失 = 变慢，永不卡死；自排除/seq 单调性/at-least-once/重启重武装全部由现有扫描逻辑保证，一行不改 |
| 基础设施 | 共享进程内事件总线（新 `server/events.py`） | 发布点不感知订阅拓扑；未来订阅者（如 UI 实时推送）可直接接入 |
| 配置面 | **零新增配置项** | 兜底保持 1800s 常量；执行频率仍由三闸门（arm_count / min_interval_h / 日预算）约束，事件不改变执行节奏 |

## 3. 事件总线（新文件 `server/events.py`）

叶子模块：只 import 标准库（`asyncio` / `threading` / `logging`）。放 `server/` 顶层而非 `bgagent/`——`bgagent/__init__.py` 导出 `start_background_agents` → 导入 scheduler → executor → `file_ops`，若 `file_ops` 反过来 import `bgagent.events` 会触发循环导入；顶层叶子模块切断该环。模块级单例（`set_bus` + 模块函数 `publish`），与代码库现有 `_db_lock`（schema.py）、`_sync_lock`（api.py）、write_registry 同构。

```python
class Bus:
    """进程内事件总线。持运行中 loop 引用 + 每 topic 的订阅者 asyncio.Queue 列表。"""
    def __init__(self):
        self._loop = asyncio.get_running_loop()
        self._t = threading.current_thread()   # 线程判定钉死：publish 内 threading.current_thread() is self._t
        self._subs = {}                        # topic -> [asyncio.Queue, ...]（同 topic 允许多订阅者，1→N fan-out）
        self._filters = {}                     # topic -> predicate(payload) | 无（发布端早弃，§4a；fan-out 前执行）
    def subscribe(self, topic: str) -> asyncio.Queue: ...
    def publish(self, topic: str, payload: dict) -> None:
        try:                                   # 宽捕获：call_soon_threadsafe 对已关闭 loop 抛 RuntimeError
            if threading.current_thread() is self._t:
                for q in self._subs.get(topic, ()): q.put_nowait(payload)
            else:
                self._loop.call_soon_threadsafe(self._deliver, topic, payload)
        except Exception:
            logger.debug("event dropped: %s", topic, exc_info=True)   # 契约：publish 永不抛
```

**发布点契约**：三个发布点**不加** try/except 包 publish，调用方无需也不应 catch——永不抛由 Bus 内部宽捕获保证。此约束有真实后果面：watcher 回调本有 `_safe_call` 隔离（snapshot_watcher.py:240-248），但 API 协程（adopt/reject 端点）与 executor handlers 无隔离，publish 一旦外抛 = adopt 端点 500 / 合并 handler 记 error。

模块级：`set_bus(bus | None)`（startup/shutdown 调用）、`get_bus() -> Bus | None`（loop 侧取实例订阅用）、`publish(topic, payload)`（bus 为 None → no-op）、`set_filter(topic, predicate | None)`（发布端早弃，§4a）、`wake_wait(queue, timeout) -> None`（`asyncio.wait_for(queue.get(), timeout)` 封装；**超时不抛，但 `CancelledError` 必须透传**——外层 cancel 时 task 要能 promptly 退出，不得吞）。两个循环共用，**loop 侧唯一的"新逻辑"，集中于此单测**。

**Payload 契约（hint only, not authoritative）**：订阅者不消费 payload 数据，只用来叫醒；醒来后照旧跑游标/文件扫描重新派生全部事实。载荷保持极小：

| topic | 载荷 | 订阅者 |
|---|---|---|
| `change` | `{op, source, path}` | 每个 change 源 trigger agent 各一个（1→N fan-out，config 允许多个 change 源 agent 并存） |
| `proposal` | `{relpath, status, actor}` | `_executor_loop` |
| `recall` | `{hits, source, traffic}` | recall 源 trigger agent 的 `_armed_loop`（sentinel） |

## 4. 发布点（3 处，**ledger commit 之后**发布，发布端早弃见 §4a）

| 发布点 | 事件与条件 | 线程上下文 |
|---|---|---|
| `apply_change`（`server/vault/orchestration/file_ops.py:42`） | `change`；早弃：source ∈ {executor, agent, *bgagents} 时跳过 | 混合：SnapshotWatcher 线程（跨线程）、API 协程、executor `to_thread` worker、sync client |
| `proposals.advance`（`server/proposals.py:239`） | `proposal`；**仅 `new_status == "adopted"` 时发布** | 生产上仅 console adopt（loop 线程）；auto 管线复活时 `processing→adopted` 亦被覆盖 |
| `recall.log_query`（`server/vault/db/recall.py:30`） | `recall`；早弃：`synthetic 且 hits > 0` 时跳过；zero-hit 恒发 | loop 线程（搜索响应路径） |

**发布时序**：`advance` 的 proposal 事件在其内部 `apply_change` 记账成功之后发布。`log_query` 的 zero-hit 恒发（无命中 = 失败信号，正是 sentinel 武装的对象）。

### 4a. 发布端早弃（优化，等价于扫描端过滤）

动机：发布端不设防时，agent 自己的写、executor 产物、agent 自己的搜索都会唤醒全部订阅者再被扫描丢弃——事件量与唤醒次数和有效信号不成比例；`advance` 全迁移发布还会让 executor 自我唤醒（merged/failed/expired 事件 → 扫描 no-op）。

三个过滤器（全部为**纯丢弃优化**：权威判定仍在扫描端，一行不改；过滤丢弃的事件最坏只使唤醒推迟到兜底周期）：

| topic | 过滤条件（丢弃） | 等价性论证 |
|---|---|---|
| `change` | source ∈ {"executor", "agent"} ∪ 全部 bgagent alias | 任一 trigger agent 的排除集 = {自身alias, executor, agent, *bgagents} ⊇ 该集合（自身 alias ∈ bgagents）→ 被丢弃 source 的行在**所有** agent 的 `get_changes_sources` 计数中恒为 0 |
| `recall` | traffic == "synthetic" 且 hits > 0 | 此时 recall_queries 行非 zero-hit（不计数）、recall_log 行 synthetic（低分计数排除）→ 对 `scan_recall_arming` 贡献为 0；zero-hit 恒发、非合成恒发（低分命中可能 < 0.2） |
| `proposal` | `new_status != "adopted"`（发布条件，非过滤器） | executor 权威扫描（`_executor_armed`）仅对 adopted 未合并 / 14d 超期武装；非 adopted 迁移不产生新工作。executor 自己产生的迁移（merged/failed/expired）天然被条件排除 → **自激零残留**；`failed→pending` 重试边与 auto 管线复活走 `→adopted` 时仍会发布 |

接线与语义注：
- `events.set_filter(topic, predicate | None)`：predicate 在 `publish` 内 fan-out 前执行，纯函数；未注册 = 不过滤。`change` / `recall` 的 predicate 由 main.py 启动时注册（别名集合来自 config，与 `start_background_agents` 的 `all_aliases` 同源），**进程生命周期内静态**（config 无热加载）。
- `recall` 载荷增加 `traffic` 字段（由 `log_query` 内 `_traffic(source)` 同源计算），predicate 不反推。
- `reconcile` / human 系 source 不在 change 过滤集合 → 照常发布（唤醒武装检查 by design，见 §6）。
- 扫描端过滤**保留为权威**：早弃只是减事件，不是替代——若过滤集合与扫描排除集未来漂移，正确性仍由扫描保证，代价仅是多/少几次唤醒。

## 5. 循环重接线（`server/bgagent/scheduler.py`）

**统一 loop 骨架**（try/except 落点在此钉死，两个循环同构）：

```python
bus = events.get_bus()
queue = None if bus is None else bus.subscribe(TOPIC)   # 订阅一次，循环外；队列贯穿循环生命周期
while True:
    if queue is None:                                  # bus 缺席降级 = 今天行为（debug 日志一次）
        await asyncio.sleep(interval_s)
    else:
        await wake_wait(queue, interval_s)             # 事件或超时，合并同一路径
        while not queue.empty():                       # 无条件 drain（N 事件 → 1 次扫描）
            queue.get_nowait()                         # 无 await 在中间；跨线程 put 只经 call_soon_threadsafe 回调；
                                                      # 代码注释钉死：勿改成 try/except QueueEmpty（引入 yield 点）
    try:
        ... 原有逻辑不动（扫描 / 判定 / 三闸门 / per-agent lock）...
    except asyncio.CancelledError:
        raise                                          # 透传，task promptly 退出
    except Exception:
        logger.exception(...)                          # 只记日志，不杀 task（与今天同构的存活骨架）
```

**`_executor_loop`（:317）**
1. 进循环前 `queue = bus.subscribe("proposal")`（队列贯穿循环生命周期）。
2. `await asyncio.sleep(EXECUTOR_INTERVAL_S)` → `await wake_wait(queue, EXECUTOR_INTERVAL_S)`，随后 drain：`while not queue.empty(): queue.get_nowait()`（N 个事件合并为 1 次扫描）。
3. 其余原样：`_executor_armed(vault)` glob 扫描（权威判定：adopted 未合并或 14d 超期）→ `to_thread(executor_fn, vault, conn)`。

**`_armed_loop`（:287）**
1. 按 `cfg.trigger["source"]` 订阅：`recall` → `"recall"`，否则 → `"change"`。
2. 同样的 `wake_wait` + drain 模式（超时 = ARMED_CHECK_INTERVAL_S 兜底）。
3. `_armed_check` 原样执行：双源游标（change: `last_armed_seq` 单调 seq；recall: 双 id 游标）、自排除集、三闸门、per-agent lock。

**可测试接缝**：两个 loop 增加 `interval_s` 参数（默认分别为 `EXECUTOR_INTERVAL_S` / `ARMED_CHECK_INTERVAL_S`），与现有 `executor_fn` / `run_fn` 注入风格一致。

**bus 缺席降级**：loop 启动时 `get_bus()` 为 None（生产路径不会发生——main.py 先 set 后起任务；仅测试/异常场景）→ 该 loop 退回纯 `asyncio.sleep(interval)` 轮询（= 今天行为），debug 日志一次。事件唤醒是加速，不是正确性依赖。

**启动接线（`server/main.py` `_run()`，:117 附近）**
- `start_background_agents(...)` 之前：`events.set_bus(events.Bus())`（bgagent 任务与 uvicorn 同处 `asyncio.run(_serve())` 一个 loop，Bus 捕获之）；同处注册 `set_filter("change"/"recall")`（§4a，别名集合与 `all_aliases` 同源）。
- `server.serve()` 返回后：`events.set_bus(None)`（关停，publish 变 no-op）。

## 6. 性质与失败语义

- **事件丢失**（跨线程投递失败、bus 未就绪窗口）→ 最坏慢一个兜底周期，永不卡死；重启后队列空但游标在 DB，首次兜底扫描自愈（与 memory-runtime §9.2"惰性重种、telemetry 可丢"语义一致）。
- **14 天超期**是时间型条件（无事件可发），由 30min 兜底覆盖——与今天行为完全一致。
- **执行频率不变**：事件只提前"检查"，不提前"执行"；LLM run 仍受三闸门 + per-agent lock 约束，executor 合并幂等。
- **队列增长**：payload 极小 + drain 合并；批同步数百文件的最坏情形 = 几次 no-op 扫描，无需背压。
- **自排除不受影响**：事件只是唤醒，武装计数仍由扫描时按排除集（{alias, executor, agent, *bgagent}）过滤——合成流量依然永不武装。
- **lost-wake 免疫（keystone）**：超时路径与事件路径合并进同一轮 drain + 扫描。`wait_for` 超时取消 `queue.get()` 的 CPython 竞态下（item 已出队但返回值被丢弃），item 虽已离队，同轮无条件扫描已覆盖数据（publish 在 commit 之后，数据必在库）；scan / drain 期间新到的事件由下一轮 `wake_wait` 立即拾取——两种情形都只变慢，不漏。
- **LIMIT 500 cap 语义不变**（`get_changes_sources`，changes.py:79）：>500 行的 burst 游标取返回行 max seq，旧 head 被跳过——既有行为，非回归；事件驱动让"一次大 burst 一次扫描"更常见，cap-skip 语义保持、无需背压。
- **reconcile 源**：`reconcile`（file_ops.py:140）不在自排除集，周期性 reconcile（30s，worker 线程）事件会唤醒武装检查——by design（与 watchdog 源同级），视为 human 侧流量。

## 7. 范围边界

| | 内容 |
|---|---|
| **In** | `server/events.py`（新）；scheduler 两个循环重接线；三个发布点；main.py 接线；`tests/test_events.py`（新）+ `tests/test_trigger.py` 追加 |
| **Out** | `_agent_loop` cron 语义（不动）；MemoryConsole adopted 卡 UI（独立前端任务，仍 open）；已知缺口 L1/L2（actor 限制——本重构碰 `advance()` 但不改 actor 策略）；新配置项（零）；client 轮询循环（另一子系统） |
| **Docs** | 重写 scheduler.py 过时 docstring（模块 docstring、常量注释、`_executor_loop` docstring）为"事件唤醒 + 30min 兜底"；2026-10-21 基线为历史快照不回改——其 §8.1 角色矩阵"触发方式"列由本 spec 取代（30min = 兜底间隔，事件 = 唤醒） |

## 8. 测试

**`tests/test_events.py`（总线单测；每测试 monkeypatch 注入独立 Bus，互不串扰）**
1. loop 线程 publish → 订阅队列收到。
2. 跨线程 publish（真 `threading.Thread`，`call_soon_threadsafe` 投递）→ 收到。
3. bus 未 set / 已关停（`set_bus(None)`）→ no-op 不抛。
4. 合并：N 次 publish → 一次 drain 取空。
5. `wake_wait`：事件到达立即返回；静默时超时返回不抛；**`CancelledError` 透传**（task cancel → promptly 退出，不得吞）。
6. **stale Bus + 已关闭 loop**：`asyncio.run` 内建 Bus，run 结束后从测试线程 publish → 不抛（§3 宽捕获契约的回归）。
7. **多订阅者 fan-out + topic 隔离**：同 topic 两订阅者各收一份；`change` 事件不进 `proposal` 队列（真实路径：多个 change 源 agent 并存）。
8. **change 早弃**：注册 predicate 后 source ∈ {executor, agent} ∪ alias → 丢弃；web / reconcile / system → 通过。**对拍**：对 fixture 中每个 source 断言（predicate 丢弃）⟺（真实 `_self_exclusion` 对每个 agent 均含该 source）——与扫描端排除函数差分对拍。
9. **recall 早弃**：`synthetic 且 hits>0` → 丢弃；`synthetic 且 hits=0` → 通过（zero-hit 恒发）；非合成 → 通过。**对拍**：丢弃条件命中的行对真实 `scan_recall_arming` 计数贡献为 0。
10. **adopted-only**：monkeypatch `publish`，驱动 `TRANSITIONS` 全部合法迁移 → 仅 `new_status == "adopted"` 时调用（executor 自我 `advance(merged/failed/expired)` 均不发）。

**循环集成（`tests/test_trigger.py` 追加；loop 注入短 `interval_s`）**
1. 提前唤醒：5s 兜底 + 已 armed vault + 发 `proposal` 事件 → `executor_fn` 被调用，且调用时刻 ∈ [0, ~2s)，远早于 5s 兜底（断言调用计数 + 真实时间，不断言返回值）。
2. 兜底自愈：不发事件 → 兜底 tick 照常执行。
3. spurious 唤醒（防御）：手工向队列注入 `status=merged` 事件（adopted-only 后 advance 不再发此事件，但 loop 必须容忍 stale/外来事件）→ 扫描 no-op，`executor_fn` 计数不增。
4. **bus 缺席降级**：`get_bus()` 为 None → 纯 sleep tick 照常（§5 安全网回归）。
5. **超时竞态容忍**：`wake_wait` 超时返回后再 publish 一个事件 → 同轮扫描已覆盖，或下一次 `wake_wait` 在 100ms 内返回，不等满兜底（§6 keystone 回归）。
6. armed loop 同构：change / recall 事件各提前唤醒一次 `_armed_check`（spy `run_fn`）；**recall 含 zero-hit 用例**（zero-hit 是 sentinel 武装的主要对象而非边缘——`log_query` zero-hit 也发布）。
7. 回归：`test_trigger.py`（双源/seq 游标/三闸门/自排除）与 `test_executor.py` 全绿——扫描/判定函数未改，行为必须不变。

**端到端（提交前，用户原始场景）**
起服务器 → 造 awaiting_review 提案 → 控制台采纳 → 日志 `[executor] armed — running deterministic merge` 秒级出现（今天最坏 30min）→ change_log 出现 `source='executor'` 行 → UI L0 "N 待落地" 随 30s 轮询翻 0。`pytest tests/ -v` 全绿。

## 附录：关键代码位置

| 位置 | 角色 |
|---|---|
| `server/bgagent/scheduler.py:287` / `:317` | `_armed_loop` / `_executor_loop`（重接线对象） |
| `server/bgagent/scheduler.py:201` | `_armed_check`（权威扫描，不动） |
| `server/bgagent/scheduler.py:303` | `_executor_armed`（权威判定，不动） |
| `server/vault/orchestration/file_ops.py:42` | `apply_change`（发布点 1） |
| `server/proposals.py:239` | `advance`（发布点 2；状态机唯一入口） |
| `server/vault/db/recall.py:30` | `log_query`（发布点 3） |
| `server/main.py:117` | `start_background_agents` 调用点（bus 接线处） |
| `tests/test_trigger.py` / `tests/test_executor.py` | 行为钉住回归（400 行 / 357 行） |
