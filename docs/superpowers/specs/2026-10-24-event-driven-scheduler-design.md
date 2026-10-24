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

叶子模块：只 import `asyncio`。放 `server/` 顶层而非 `bgagent/`——`bgagent/__init__.py` 导出 `start_background_agents` → 导入 scheduler → executor → `file_ops`，若 `file_ops` 反过来 import `bgagent.events` 会触发循环导入；顶层叶子模块切断该环。模块级单例（`set_bus` + 模块函数 `publish`），与代码库现有 `_db_lock`（schema.py）、`_sync_lock`（api.py）、write_registry 同构。

```python
class Bus:
    """进程内事件总线。持运行中 loop 引用 + 每 topic 的订阅者 asyncio.Queue 列表。"""
    def __init__(self): self._loop = asyncio.get_running_loop(); self._subs = {}
    def subscribe(self, topic: str) -> asyncio.Queue: ...
    def publish(self, topic: str, payload: dict) -> None:
        # 本线程（== loop 线程）→ put_nowait
        # 跨线程 → self._loop.call_soon_threadsafe(...)
        # loop 已停止 / 未订阅 → 丢弃（debug 日志）
        # 契约：publish 永不抛异常
```

模块级：`set_bus(bus | None)`（startup/shutdown 调用）、`get_bus() -> Bus | None`（loop 侧取实例订阅用）、`publish(topic, payload)`（bus 为 None → no-op）、`wake_wait(queue, timeout) -> None`（`asyncio.wait_for(queue.get(), timeout)` 的封装 + 超时不抛，两个循环共用；**loop 侧唯一的"新逻辑"，集中于此单测**）。

**Payload 契约（hint only, not authoritative）**：订阅者不消费 payload 数据，只用来叫醒；醒来后照旧跑游标/文件扫描重新派生全部事实。载荷保持极小：

| topic | 载荷 | 唯一订阅者 |
|---|---|---|
| `change` | `{op, source, path}` | 各 change 源 trigger agent 的 `_armed_loop` |
| `proposal` | `{relpath, status, actor}` | `_executor_loop` |
| `recall` | `{hits, source}` | recall 源 trigger agent 的 `_armed_loop`（sentinel） |

## 4. 发布点（3 处，每处 1–3 行，**ledger commit 之后**发布）

| 发布点 | 事件 | 线程上下文 |
|---|---|---|
| `apply_change`（`server/vault/orchestration/file_ops.py:42`） | `change` | 混合：SnapshotWatcher 线程（跨线程）、API 协程、executor `to_thread` worker、sync client |
| `proposals.advance`（`server/proposals.py:239`） | `proposal` | 混合：console adopt/reject（loop 线程）、executor merge/fail（to_thread）、系统过期（to_thread） |
| `recall.log_query`（`server/vault/db/recall.py:30`） | `recall` | loop 线程（搜索响应路径） |

`advance` 的发布在其内部 `apply_change` 记账成功之后——两个事件都发（change 事件走既有武装扫描口径，proposal 事件给 executor；executor 的 `merged/failed/expired` 自我唤醒 → 扫描 → no-op，无害）。`log_query` 的 zero-hit 也发布（无命中 = 失败信号，正是 sentinel 武装的对象）。

## 5. 循环重接线（`server/bgagent/scheduler.py`）

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
- `start_background_agents(...)` 之前：`events.set_bus(events.Bus())`（bgagent 任务与 uvicorn 同处 `asyncio.run(_serve())` 一个 loop，Bus 捕获之）。
- `server.serve()` 返回后：`events.set_bus(None)`（关停，publish 变 no-op）。

## 6. 性质与失败语义

- **事件丢失**（跨线程投递失败、bus 未就绪窗口）→ 最坏慢一个兜底周期，永不卡死；重启后队列空但游标在 DB，首次兜底扫描自愈（与 memory-runtime §9.2"惰性重种、telemetry 可丢"语义一致）。
- **14 天超期**是时间型条件（无事件可发），由 30min 兜底覆盖——与今天行为完全一致。
- **执行频率不变**：事件只提前"检查"，不提前"执行"；LLM run 仍受三闸门 + per-agent lock 约束，executor 合并幂等。
- **队列增长**：payload 极小 + drain 合并；批同步数百文件的最坏情形 = 几次 no-op 扫描，无需背压。
- **自排除不受影响**：事件只是唤醒，武装计数仍由扫描时按排除集（{alias, executor, agent, *bgagent}）过滤——合成流量依然永不武装。

## 7. 范围边界

| | 内容 |
|---|---|
| **In** | `server/events.py`（新）；scheduler 两个循环重接线；三个发布点；main.py 接线；`tests/test_events.py`（新）+ `tests/test_trigger.py` 追加 |
| **Out** | `_agent_loop` cron 语义（不动）；MemoryConsole adopted 卡 UI（独立前端任务，仍 open）；已知缺口 L1/L2（actor 限制——本重构碰 `advance()` 但不改 actor 策略）；新配置项（零）；client 轮询循环（另一子系统） |

## 8. 测试

**`tests/test_events.py`（总线单测；每测试 monkeypatch 注入独立 Bus，互不串扰）**
1. loop 线程 publish → 订阅队列收到。
2. 跨线程 publish（真 `threading.Thread`，`call_soon_threadsafe` 投递）→ 收到。
3. bus 未 set / 已关停（`set_bus(None)`）→ no-op 不抛。
4. 合并：N 次 publish → 一次 drain 取空。
5. `wake_wait`：事件到达立即返回；静默时超时返回不抛。

**循环集成（`tests/test_trigger.py` 追加；loop 注入短 `interval_s`）**
1. 提前唤醒：5s 兜底 + 已 armed vault + 发 `proposal` 事件 → `executor_fn` ~1s 内被调用（远早于兜底）。
2. 兜底自愈：不发事件 → 兜底 tick 照常执行。
3. spurious 唤醒：发 `status=merged` 事件 → 扫描 no-op，`executor_fn` 计数不增。
4. armed loop 同构：change / recall 事件各提前唤醒一次 `_armed_check`（spy `run_fn`）。
5. 回归：`test_trigger.py`（双源/seq 游标/三闸门/自排除）与 `test_executor.py` 全绿——扫描/判定函数未改，行为必须不变。

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
