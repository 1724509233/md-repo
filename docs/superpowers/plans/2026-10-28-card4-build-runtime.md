# Card 4 — build_runtime：runtime 组装有名字 + exclusion 政策一个家

Spec: `architecture-review-20261028-1711.html` card 4（/var/folders/…/T/opencode/）· branch `feat/memory-runtime`
Cards 1/2/3/6 已落地（ledger / routes 拆包 / proposals 读面下沉 / ModelConfig 单一化）。基线：708 passed, 1 skipped。

## Problem（证据，逐行核实过）

1. 组装埋在嵌套闭包：`server/main.py:93-137`（`main → _serve → _run`）——
   `events.set_bus` :100 · `set_filter("change")` :102-103 · `set_filter("recall")` :104-105 ·
   4× `_on_*` → `ledger.consume` :107-117 · `SnapshotWatcher` :119-127 ·
   `start_background_agents` :130-136。leverage 极高、locality 为零（无名字、无测试）。
2. 「合成流量不得 arm 自己的 scheduler」同一集合手工构建两遍：
   - `main.py:101`　`_excluded_sources = {"executor", "agent"} | {a.alias for a in bg_agents}`
   - `scheduler.py:116-122`　`_self_exclusion(cfg, all_aliases)` → `{cfg.alias, "executor", "agent", *all_aliases}`
   - 两者恒等（`cfg.alias ∈ all_aliases`，同一 `bg_agents` 列表传两边）——两处维护，可以漂移。
   - `_self_exclusion` 的调用方只有两处：`scheduler.py:144`（`_has_human_activity_since`）、`scheduler.py:236`（`_armed_check`）。
3. `tests/test_main.py` 只测 reconcile（SnapshotWatcher.start 路径），组装零覆盖。
4. `server/events.py` 是 clean leaf（card 4 clean strip 已确认）——**不动**。

## Solution

### 1. exclusion 政策一个家（scheduler.py）

- `_self_exclusion` 改公开 `self_exclusion(aliases: set[str]) -> set[str]`，body = `{"executor", "agent"} | aliases`。
- 去掉 `cfg` 参数（冗余：`cfg.alias ∈ all_aliases`）；docstring 承载政策陈述：框架角色 + 所有 bgagent alias 永不 arm 自己；人类通道（web/watchdog/sync）保持可见。
- 两个调用方改为 `self_exclusion(all_aliases)`。
- `main.py` 的 bus filter 改为 `from .bgagent.scheduler import self_exclusion`（注意 import 方向：scheduler 不 import main，无环），谓词 = `lambda p: p.get("source") not in self_exclusion({a.alias for a in bg_agents})`。

### 2. build_runtime（main.py 顶层 async 函数）

```python
async def build_runtime(vault_dir, conn, bg_agents, notifications_cfg) -> Runtime
```

- 内容 = 现在 `_run` 的 wiring 部分，逐条搬：`events.set_bus(events.Bus())`（须在 loop 内构造）→ 两个 `set_filter` → 4× ledger 回调 → `SnapshotWatcher(...)` + `start()` + `await asyncio.sleep(0)` → bg_agents 非空时 `cleanup_old_notifications(conn)` + `start_background_agents(..., hooks=[GuardedNotificationHook(InAppHook(conn), conn, daily_cap=...)], db_conn=conn)` + `await asyncio.sleep(0)`。
- 返回 `Runtime`（dataclass 或普通类）：`watcher`、四个回调（on_created/on_modified/on_moved/on_deleted，测试直接调用它们验证 wiring）、`bg_tasks: list[asyncio.Task]`；`stop()` = `watcher.stop()` + 逐个 cancel bg_tasks。
- `start_logrotate` 留在 `main()` 启动序列（启动卫生，不属于 wiring graph）；`events.set_bus(None)` 的关停留在 serve 之后。
- `main()` 收缩为：parse config → logging 装配 → db + register + ensure_indexed → create_app → `rt = await build_runtime(...)` → uvicorn serve → `rt.stop(); events.set_bus(None)`。

### 3. 测试（新增 tests/test_runtime.py；repo 惯例 = sync 测试内 `asyncio.run()`，见 tests/test_events.py:3）

- `self_exclusion({"a","b"}) == {"executor", "agent", "a", "b"}`；并断言 scheduler 的 arming scan 与 main 的 bus filter 用的是同一个函数（`scheduler.self_exclusion is self_exclusion` 级别的单源断言）。
- bus filter：build_runtime 后 `events.get_bus()` 非 None；publish change `source="executor"|"agent"|alias` → 丢弃；`source="web"` → 送达（subscribe 一个 queue 断言）。
- recall filter：`traffic="synthetic", hits>0` → 丢弃；真实流量 → 送达。
- 四个回调：`ledger.register(path, "web")` + 落盘 + 调回调 → change_log 恰一行，op/path/from_path/source 正确（moved 带 from_path）。
- bg：传一个 enabled 的 fake `BackgroundAgentConfig` → `bg_tasks` 非空；空列表 → 空。
- 全量 pytest 保持绿（708 基线不回归）。

## 验收（card 4 原文 bullets）

- [ ] 组装变成单元测试
- [ ] exclusion 政策一个家（locality）
- [ ] wiring 有名字（build_runtime）
- [ ] leverage 保留，locality 到手
- [ ] events.Bus 不动（已是 clean leaf）

## 边界（不做）

- 不动 events.py / SnapshotWatcher / ledger / bgagent 内部逻辑（纯搬移 + 单源化）。
- 不动 config 加载、logging 装配的细节。
- 文档：AGENTS.md「Architecture」段如提到 main.py 组装细节则同步一行（`build_runtime` 有名字）；GLOSSARY 不动。

## 验证

- `.venv/bin/python -m pytest tests/ -q` 全绿（owner: fixer 自跑，我复核）。
- 冒烟：`config.yaml` 存在时 `timeout 8 .venv/bin/python -m server.main config.yaml` 能起（看日志含 bg started / watcher），无 import 环。
