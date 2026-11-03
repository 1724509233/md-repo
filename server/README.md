# server/ — 服务端

md-sync 的 FastAPI 服务端：LAN 多设备 Markdown 同步、本地 AI 助手（RAG + LLM agent）、后台定时 agent（bgagent）与内存运行时（提案 / 快照 / 召回）。入口 `python -m server.main config.yaml`（默认端口 9888）；MCP stdio 走独立入口 `md-vault-mcp`（不经 main.py，见 `mcp_server.py`）。

## 模块结构

### 顶层文件（14）

| 文件 | 职责 |
|------|------|
| `main.py` | 入口：config → 双账本（index.db + ledger.db）→ `create_app` → `build_runtime()`（事件总线 + publisher 过滤、四个 ledger 回调、SnapshotWatcher、bgagent 任务；返回带 `stop()` 的 `Runtime`）→ uvicorn |
| `api.py` | `create_app()` 工厂：构造 `WebContext`、挂载 `/static`（templates 目录）、`routes.register_all`（wiring only，路由在 `routes/`） |
| `ledger.py` | 变更账本：`change_log` + `files` 的**唯一写入面**。四函数 `register` / `record` / `record_folder` / `consume` |
| `events.py` | 进程内事件总线：**wake signal 不是数据通道**——订阅者不得依赖收到每个事件或 payload 内容做决策，权威状态由 consumer 自己的扫描重导出 |
| `sync.py` | `compare_files()` 纯函数无 IO：返回 `to_pull` / `to_push` / `to_delete` / `conflicts`（`recent_moves` 检测服务端改名 → stale copy 移入 `to_delete`） |
| `snapshot_watcher.py` | `SnapshotWatcher`：watchdog 事件（doorbell）→ 线程去抖 → 快照对比；脏集合 + doorbell；启动调 `file_ops.reconcile`（惰性 import，一次）；周期兜底为 watcher 自身 `_reconcile` 扫描级标 dirty（事件经 `ledger.consume` 落库） |
| `snapshots.py` | pre-image 快照（可逆层）：LLM 写入（agent 直写工具、executor merge）与交互式删除（Web 文档管理）写前捕获全文 pre-image；回滚入口（verify-all-then-write-all） |
| `proposals.py` | 提案状态机：8 状态 `TRANSITIONS`、`advance`；DB 行是唯一状态源（v2a 零文件落）；`VALID_TYPES` 5 类动词；14 天 TTL |
| `recovery.py` | 恢复演练：证明 index.db 是可重建缓存（只动 index.db，可重复可测） |
| `sync_lock.py` | 内存同步锁：一进程同时一个 sync；超 300s 视为 stale 可重取（崩溃自愈） |
| `logrotate.py` | 服务端日志定期清理：`.sync/logs` 下 `server.log.*` 轮转文件，每日 3 点后清扫超 retention 天数的 |
| `model.py` | `ModelConfig` 单一数据类（base-layer leaf，被 `server.config` 与 `server.agent` 共同 import） |
| `ai.py` | RAG 搜索 `rag_search()`（转交 raw query，委派 `segments_sql` 做 jieba 分词 + FTS 四档召回）+ LLM 流式 `chat_stream()` |
| `mcp_server.py` | MCP stdio 独立入口（`md-vault-mcp`，不经 main.py）：6 工具，有效写入面仅 `propose`（`write_doc`/`edit_doc` 被双重 gate 拒绝），自管 config 发现与 DB 初始化 |

### 子目录（7）

| 目录 | 说明 | 详细文档 |
|------|------|----------|
| `agent/` | LLM agent 系统：Agent 状态机、LoopRuntime、15 工具（一模块一工具）、会话持久化 | [agent/README.md](agent/README.md) |
| `bgagent/` | 后台定时 agent：cron 调度 + 事件 arming、独立模型配置、自动报告；执行器是框架角色（非 LLM） | [bgagent/README.md](bgagent/README.md) |
| `vault/` | 索引子系统：db（SQLite）/ fs（文件系统）/ orchestration（协调层） | [vault/README.md](vault/README.md)（待更新） |
| `routes/` | Web 路由：6 个 URL 前缀域，每域一模块 | [routes/README.md](routes/README.md) |
| `config/` | YAML 配置解析包（core / ai / log / notifications / bgagent） | [config/README.md](config/README.md) |
| `prompts/` | 提示词：bgagent 各角色 prompt（md）+ agent system prompt（`system.md`） | — |
| `templates/` | Web UI 静态资源：`index.html` 壳 + `js/` + `style.css`（`/static` 挂载源） | — |

## 公共 API（函数级）

| 符号 | 签名 | 说明 |
|------|------|------|
| `create_app` | `(vault_dir, db_conn, ledger_conn=None, ai_config=None, bg_targets=None) -> FastAPI` | 应用工厂（`api.py`） |
| `main` / `build_runtime` | `main(argv=None)` / `build_runtime(vault_dir, conn, ledger_conn, bg_agents, notifications_cfg) -> Runtime` | 入口与运行时装配（`main.py`） |
| `ledger.register` | `(rel_path, source, batch_id="")` | 写盘前登记（watcher 归因） |
| `ledger.record` | `(conn, vault_dir, rel_path, action, source, *, from_path="", new_hash="", batch_id="", **passthrough)` | 同步落行（web 直写） |
| `ledger.record_folder` | `(conn, vault_dir, relpath, action, source="web", from_path="")` | 文件夹行（不触 registry） |
| `ledger.consume` | `(conn, vault_dir, rel_path, action, from_path="") -> tuple[str, str, bool]` | watcher 消费侧记账 |
| `compare_files` | `(local, server, recent_moves=None) -> dict` | 同步比对（`sync.py`） |
| `rag_search` | `(vault_dir, query, top_k=5, expand=True, recall="strict", include_code=False) -> list[dict]` | FTS 四档召回（`ai.py`） |
| `chat_stream` | `(base_url, api_key, model, messages, max_tokens=4096, temperature=0.7)`（async generator） | OpenAI 兼容 SSE 流（`ai.py`） |
| `SnapshotWatcher` | `(vault_dir, db_conn=None, on_file_created=None, on_file_modified=None, on_file_moved=None, on_file_deleted=None, debounce=1.5, reconcile_interval=30.0)` | 快照对比 watcher |
| `proposals.create` / `advance` | `create(conn, vault_dir, type, actor, run_id, source_refs, justification, payload=None, now=None)` / `advance(conn, vault_dir, record_id, new_status, actor, reject_reason="")` | 提案创建 / 状态迁移 |
| `snapshots.capture` / `finalize` / `rollback` | `capture(conn, vault_dir, path, op, actor, *, run_id="", call_id="", new_path="", now=None)` / `finalize(conn, snapshot, post_hash)` / `rollback(conn, vault_dir, call_id) -> dict` | pre-image 捕获 / 落定 / 回滚 |
| `run_recovery_drill` | `(vault_dir) -> dict` | 恢复演练（`recovery.py`，CLI `python -m server.recovery drill <vault>`） |
| `SyncLock.try_acquire` / `.release` | `-> bool` / `-> None` | 内存同步锁（`sync_lock.py`） |

## 关键设计

### 变更链路（watcher → ledger → 一次落库）

```
用户 / 外部编辑器 / watcher 触发文件变化
  → Watchdog 事件（doorbell，不是数据源）
  → SnapshotWatcher 线程去抖 + 快照对比（emit 语义事件）
  → ledger.consume() 归因（registry 命中 = 注册者 source；未注册兜底 "watchdog"）
  → apply_change()（vault/orchestration/file_ops.py）一次完成 文件索引 + change_log + FTS + 图谱
```

写者侧纪律：in-process 通道（agent tools、executor、bgagent runner、web 异步路径）写盘**前**调 `ledger.register()`，consumer 据此归因记账；web 控制台直写（文档管理）调 `ledger.record()` 同步落行，consumer 对该 path booking nothing。`apply_change` / `apply_folder_change` 的唯一外部 importer 是 `ledger.py`。未注册写入（MCP stdio、直接磁盘编辑）兜底 `source="watchdog"`。

- invariant：registry TTL 10s；skip mark 共享该 TTL——过期后到达的 FS 事件重新以 `watchdog` 记账（同步 record 设计的既有窗口，保留而非修复）。

### Ledger 模型（四函数分工）

| 函数 | 语义 |
|------|------|
| `register` | 写盘前登记，watcher consumer 归因记账（in-process 通道） |
| `record` | 同步落行 + skip（web 直写，console feed 须即时响应） |
| `record_folder` | 文件夹行，不触 registry（watcher 无 folder 回调，无双行风险） |
| `consume` | watcher 消费侧：pop 注册 → `apply_change`；skip 路径 booking nothing |

深度论证见 spec `~/md-vault/projects/md-repo/specs/2026-10-31-ledger-file-split-design.md` 与 `~/md-vault/projects/md-repo/specs/2026-10-01-file-change-log-design.md`。

### 双账本两连接（ledger-file split）

`.sync/index.db`（**可重建缓存**：files / segments / edges / change_log / trigger_state / recall / …）+ `.sync/ledger.db`（**权威状态**：proposals / proposal_transitions / snapshots / relatedness 四张无重建来源表）。`vault/db/conn.py` 注册这对连接（`init(index_conn, ledger_conn)` / `get_index_conn()` / `get_ledger_conn()`），**共享一个生命周期**（一处 init、一处注册、一处关闭）。首启 `migrate_ledger` 把四表从 index.db 迁出（幂等，per-machine）；恢复演练（`recovery.py`）**只动 index.db**。两把锁、同一纪律，详见 [vault/db/README.md](vault/db/README.md)。

spec `~/md-vault/projects/md-repo/specs/2026-10-31-ledger-file-split-design.md`。

### Agent 对话链路（SSE + 确认 + execute 门面）

```
POST /api/agent/chat {session_id, message}
  → SessionManager.get_or_create(session_id)
  → Agent.chat(message)  （LoopRuntime 流式循环，最多 max_rounds 轮）
    → OpenAI stream (SSE) → Event(token | tool_call | confirm_request | tool_result | done)
    → 工具调用: validate_tool_call（L1 schema / L2 语义边界 / L3 gates）
                → execute_tool（薄门面，解析名字）
                → permission="ask" 需用户经 confirm 端点批准
  → 会话持久化（finally，任何退出路径都 save）
```

- 15 工具一模块一工具（`agent/tools/`）；`execute_tool()` 只解析名字，DB 访问由各工具经 `conn.get_index_conn()` / `get_ledger_conn()` 懒加载。
- 路径安全单入口 `policy.validate_tool_call`；`VAULT_GATES` 承载 vault 不变量（`SuggestionsGate`）。

spec `~/md-vault/projects/md-repo/specs/2026-10-22-agent-permission-policy-design.md`。

### bgagent 调度链路（cron 底线 + 事件 arming）

```
config.yaml → load_background_agents()
  → start_background_agents():
      每个 enabled 非执行器 agent → 一个 cron task
      每个 trigger agent         → 一个 30 分钟 arming task
      执行器                      → 一个共享 30 分钟 task（框架角色）
```

- **cron 是 deadline floor**：change-source tick 先做 idle 检查（无非自排除 create 事件 → 记 idle RunReport，不调 LLM）；recall-source tick 无条件跑（复盘不靠自觉）。
- **事件 arming 三重门**：arming scan（source=change 计匹配的 change 事件；source=recall 计 failed / low-score 命中，`low_score` 是命中判分阈值）累加 armed 计数；**释放一次 run 须过三门**——`armed ≥ arm_count`、`min_interval_h` 冷却已过、当日预算（`budget_runs_per_day`）有余。
- **30 分钟 backstop**（`ARMED_CHECK_INTERVAL_S`）与事件 wake 汇聚到**同一个权威 DB scan**（lost-wake 免疫）；无 bus 退化为纯 sleep loop。
- **执行器**（框架角色，非 LLM）：proposal 事件 wake 或 30 分钟 backstop → 权威 scan（proposals 表有 adopted 未 merge 或 14 天超期 awaiting_review）→ worker 线程确定性 merge；无工作 → 静默 no-op。

spec `~/md-vault/projects/md-repo/specs/2026-10-11-bgagent-design.md` 与 `~/md-vault/projects/md-repo/specs/2026-10-24-event-driven-scheduler-design.md`。

### 提案状态机（8 状态 / 5 动词）

```
pending → processing → adopted | awaiting_review | failed
adopted → merged | failed          （merge 失败 → failed，只读归档）
awaiting_review → adopted | rejected | expired
expired → awaiting_review          （revive，human-only）
```

DB 行（`proposals` 表）是唯一状态源：v2a 零文件落（无 `suggestions/` 投影、无 change_log 行），`proposal_transitions` 是全量状态账。`events.publish("proposal")` 只在 `adopted` 触发（执行器的唯一可行动状态）。5 类动词分两条授权通道：auto（entity_index / relatedness → adopted 后执行器 merge）/ review（summary / errata / reminder → awaiting_review 待人审）。

spec `~/md-vault/projects/md-repo/specs/2026-10-21-memory-runtime-design.md`。

## 依赖方向

```
                                main.py（入口：装配 + uvicorn）
           ┌─────────────────────┬─────────────────────┬───────────────┐
            ▼                     ▼                     ▼               ▼
         api.py           snapshot_watcher          bgagent/        ledger.py
      (create_app)        (SnapshotWatcher)       (cron+arming)     (变更账本)
                                                   ─写前 register 归因─►
            ▼
         routes/  (6 域)
            │
            ▼
      ┌───────────────────┐
      │       vault/      │
      │     db/fs/orch    │
      └───────────────────┘

      写前 register 归因：bgagent/ 与 agent/（15 工具）均调 ledger.register
      ledger 唯一 importer：file_ops（apply_change / apply_folder_change）

    独立入口（不经 main）：
      mcp_server.py ──► config/ vault/ agent/ ai.py snapshots/

    base 层 leaf（被多模块读取，自身不依赖上层）：
      events.py（事件总线，stdlib-only）▲ 单一上行边，importer：
                  main / proposals / file_ops / recall / bgagent/scheduler
      config/  ·  model.py（ModelConfig 单一化，config 与 agent 共同 import）
```

方向纪律：
- `ledger.py` 居中——`apply_change` / `apply_folder_change` 只被它 import；上层（routes / agent / snapshots / proposals）写账一律经 `ledger` 四函数。
- `events.py` 是 stdlib-only leaf——vault file_ops / proposals / db recall / bgagent scheduler 都可 import 而不成环。
- `config/` 与 `model.py` 是 base 层 leaf，不依赖 main / api / routes / vault。
- `mcp_server.py` 是独立入口，与 main.py 平行，各自装配 config + 双账本 + 共享 `ensure_indexed`。

## 注意事项

- **`server.vault` 根 shim 已废弃**：`service.py` facade 已删除（card 5）；新代码直接从 `server.vault.db` / `.fs` / `.orchestration` 导入。`__init__.py` 是兼容 shim（re-export 38 个旧名，emit DeprecationWarning），面由 `tests/test_vault_shim.py` 钉住；tests 仍走 shim——不要破坏它。
- **sync 锁是注入的**：`SyncLock` 实例由 `create_app` 构造并注入 `WebContext`（`app.state.ctx.sync_lock`）；测试经该 seam 直设 `lock.locked_at` 模拟锁定，不碰模块全局；进程重启后重置。超 300s 的锁视为 stale，可重取（崩溃自愈）。
- **`_db_lock` / `_ledger_lock` 纪律**：两把锁（非重入 `threading.Lock`）均只由 `vault.db` 的具名持锁 mutation 函数**在函数内部持有**（复合临界区锁内原子）。`tests/test_ledger_import_discipline.py` 扫源码钉住两条：(i) ledger 域三模块（`proposal_records` / `snapshot_records` / `relatedness`）持 `_ledger_lock`，**不得再引用 `_db_lock`**；(ii) **`_ledger_lock` 在 `server/vault/db` 包外零出现**（具名函数即接口）。没有测试钉「`_db_lock` 包外不可 import」——其现行引用面：包内为 `recall` / `notifications` / `trigger_state` 的具名持锁函数（函数内持有），包外仅 `file_ops` 的 apply 路径——`file_ops.py` 在 orchestration（vault/db 包外）import `_db_lock` 供 `apply_change` / `apply_folder_change` 复合临界区，是 card-5 先例例外。`init_db`/DDL、`recovery._rebuild_files_table`、`migrate_ledger` 的 ATTACH 均为单进程/启动语境，不持锁，不在该纪律范围。
- **`_db_lock` 死锁陷阱**：单把非重入锁，若读者在锁内持有它再调用同模块读 helper 会同线程死锁——故**纯读 helper 无锁**，由 mutation 具名函数负责加锁，锁内调用读 helper 安全。读函数调用方不要再包 `with _db_lock`。
- **MCP server 是独立进程**：不经 main.py 启动，有自己的 config 自动发现（CWD → 项目根）与 DB 初始化逻辑；stdio 是独立进程，无法向 server 的写 registry 注册，其写入经 watcher 兜底归因（`source="watchdog"`）。
- **事件总线是 wake signal，不是数据通道**：丢事件只让 wake 变迟（由 consumer 的 backstop 兜底），绝不漏；订阅者决策一律以自己的权威扫描为准。
- **可逆层永不阻塞写入**：snapshot 捕获失败（读错 / 磁盘满 / DB 错）**不阻塞也不回滚写入**——写入照常，快照跳过并记日志；降级计数暴露在 `/api/memory/status`。

## 使用示例

```bash
python -m server.main config.yaml          # 启动服务（默认端口 9888）
python -m server.recovery drill <vault>    # 运行恢复演练（index.db 可重建性证明）
md-vault-mcp                               # MCP stdio 独立入口
```
