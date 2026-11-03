# tests — pytest 测试套件

md-sync 的测试套件：覆盖服务端 / 客户端各域（api / vault / ledger / proposals / agent / bgagent / sync / recall / ui / mcp 等）的单元与集成测试，外加一组**扫 `server/` 源码树来"钉住纪律"的元测试**（import 纪律、路由表顺序、vault shim 面）。入口：`pytest tests/ -v`；单测 `pytest tests/test_api.py::test_health -v`。

套件持续增长，**不钉测试总数**。

## 模块结构

| 单元 | 职责 |
|------|------|
| `conftest.py` | 套件级 fixtures + helper（本文件③的主角）：`vault_dir` / `db_conn` / `ledger_conn` / `app` / `client` / `ai_config` + `simulate_consumer` + autouse 句柄隔离 |
| `test_*.py` | 各域测试（按文件名分域，如 `test_api*.py` / `test_vault_*.py` / `test_ledger*.py` / `test_proposals*.py` / `test_bgagent_*.py` / `test_agent.py` / `test_sync.py` / `test_reconcile.py` / `test_recall_*.py` / `test_ui.py` / `test_mcp_*.py` …） |
| `functional/` | bgagent 链路**本地 mock 演练** harness（mock_llm / seed / run_*_once / config.example.fun.yaml，见③） |
| `__init__.py` | 包标记 |

## 公共 API（`conftest.py`）

### helper

```python
def simulate_consumer(conn, vault_dir, path: str, action: str = "modified", from_path: str = "")
```

驱动**生产 watcher-consumer 路径**（`server.ledger.consume` —— 与 `server/main.py` 四个 `_on_*` 回调同一条）对 `path` 走一次，返回被弹出的 `(source, batch_id, skip)` 归因。

> 断言 ledger 行的单测**必须显式驱动这条 consumer 路径**，不要自己写行（AGENTS.md 变更归因纪律：写前 `ledger.register()`，consumer 落账；`action` 必须是要触发的那个 watcher 事件 —— 首写 `"created"`、改写 `"modified"`、`"deleted"`、`"moved"` + `from_path`）。

### fixtures

```python
@pytest.fixture(autouse=True)
def _db_handle_isolation()
# 每个测试前后调用 server.vault.db.conn.init(None, None) 重置模块级 DB 句柄对。
# 工具经 db_handle.get_index_conn() / get_ledger_conn() 懒解析连接（2026-10-31 双账本拆分，L4）；
# 这防止一个测试注册的连接泄漏进下一个。

@pytest.fixture
def vault_dir(tmp_path: Path) -> Path
# 临时 vault：tmp_path/vault，含 .sync/ 子目录 + 样本 notes/test.md。
# 样本一律 write_bytes(SAMPLE_CONTENT) 二进制写入（见⑥ Windows 换行 gotcha）。

@pytest.fixture
def db_conn(vault_dir: Path)
# index.db 域：vault_dir/.sync/index.db，预置样本文件（upsert_file）；
# db_handle.init(conn, None) 注册到模块句柄；teardown conn.close()。

@pytest.fixture
def ledger_conn(vault_dir: Path, db_conn)
# ledger.db 域：vault_dir/.sync/ledger.db —— 四张权威状态表
# （proposals / proposal_transitions / snapshots / relatedness；2026-10-31 拆分，L4）。
# 依赖 db_conn，使同时请求两者的测试拿到完整注册对：db_handle.init(db_conn, conn)；teardown conn.close()。

@pytest.fixture
def app(vault_dir: Path, db_conn, ledger_conn)
# create_app(vault_dir=..., db_conn=..., ledger_conn=...) 装配的 FastAPI app。

@pytest.fixture
def client(app)      # TestClient(app)

@pytest.fixture
def ai_config()      # mock server.config.AIConfig（base_url / api_key / model），供 agent 端点测试
```

### functional/ 演练 harness

bgagent 链路的本地 mock 环境演练（Tier-1 回归资产 + Tier-2 冒烟），**不依赖真实 LLM**：

| 文件 | 用法 | 职责 |
|------|------|------|
| `mock_llm.py` | `python -m tests.functional.mock_llm --port 11435`（起服务）；`--once`（不打服务，打印两 playbook 的 request→response 序列自检） | OpenAI 兼容的**脚本化** mock LLM（stdlib http.server，无 fastapi 依赖）。按 system prompt 选 playbook：含 `sentinel` → sentinel 剧本（turn0 `recall_stats` → turn1 只读治理报告，v2 不产提案）；否则 consolidator 剧本（turn0 `recent_changes` → turn1 `propose` summary 提案 → turn2 中文总结）。SSE 线格式与 `server/agent/runtime.py` 流式客户端逐字节对齐 |
| `seed.py` | `python -m tests.functional.seed --vault <path> {proposals\|recall\|changes} ...` | 向 scratch vault 播种数据。proposals 走 `server.proposals.create` + 合法 `advance` 转态（落 ledger.db，逐步记 `proposal_transitions`）；recall 落 index.db（mirror `recall.log_query`）；changes 走 `log_change`（index.db） |
| `run_executor_once.py` | `python -m tests.functional.run_executor_once --vault <path>` | 一次确定性 executor 循环：调 `server.bgagent.executor.run_executor(vault, conn)`（生产 30 分钟循环体），打印 JSON summary（merged / expired / errors） |
| `run_sentinel_once.py` | `python -m tests.functional.run_sentinel_once --config config.fun.yaml`（前置：`mock_llm --port 11435` 已在运行） | 一次**真实入口** sentinel 运行：走 `server.bgagent.runner.run_once`（生产入口）+ `load_background_agents`，mock LLM 在 11435。退出码 0 iff 运行 ok 且有可观测输出（≥1 条本 run 提案记录，或非空 summary） |
| `config.example.fun.yaml` | `sed "s#{{VAULT_DIR}}#<scratch-vault 绝对路径>#g" tests/functional/config.example.fun.yaml > config.fun.yaml`（**在仓库根**执行，prompt 路径相对 config 目录解析） | 功能测试配置模板：sentinel（只读 recall 触发）/ consolidator（create 事件触发，唯一写动作 `propose`）/ executor（框架角色 `is_executor`，非 LLM）三个 agent，全部指向 mock LLM `127.0.0.1:11435`，server 端口 9889（避开生产 9888） |

## 关键设计 / 链路

**fixture 依赖链**（双账本拆分的直接体现）：

```
tmp_path
  └─ vault_dir            临时 vault + 样本 notes/test.md（write_bytes 二进制写入）
       ├─ db_conn         index.db，预置样本；db_handle.init(conn, None)
       │    └─ ledger_conn ledger.db，依赖 db_conn → db_handle.init(db_conn, conn)
       │         └─ app   create_app 装配
       │              └─ client   TestClient(app)
       └─ ai_config       mock AIConfig（独立，不依赖 vault）
_autouse_ _db_handle_isolation   每个测试前后重置句柄对（init(None, None)）
```

`db_conn`（index 域）与 `ledger_conn`（ledger 域）是**两个独立的 fixture**，对应 2026-10-31 拆到两个 SQLite 文件的两账本：`index.db`（可重建缓存）/ `ledger.db`（四张权威状态表）。拆分出处见 `~/md-vault/projects/md-repo/specs/2026-10-31-ledger-file-split-design.md`。

**钉住纪律的元测试**（扫 `server/` 源码树断言不变量 —— "surface does not regress" 无法 grep，测试即 pin）：

- `test_ledger_import_discipline.py`：双锁 import 纪律。断言 (i) **ledger 域三模块**（`proposal_records.py` / `snapshot_records.py` / `relatedness.py`）不再引用 `_db_lock`（它们现持 `_ledger_lock`）；(ii) `_ledger_lock` 在 `server/vault/db` 包外**零出现**。测试文件本身在包外，故用 `_`.join 拼出标识符，使扫描不命中自身（card-5 模式）。
- `test_routes_order.py`：路由表注册顺序快照。Starlette 按**注册顺序**匹配第一个完全命中、静默遮蔽后续重复，故 order IS behavior（console 内 manage ⪯ upload、changes ⪯ get_doc；memory 内 `{relpath}/subpath` ⪯ detail catchall）。快照钉住整张 31 路由表 + `/static` mount + 无重复不变量。
- `test_vault_shim.py`：`server.vault` 根 shim 的 **38 个 re-export 名**钉住（12 public + 26 test-only），`from server.vault import *` 恰好 12 public。`service.py` facade 已删（card-5），shim emit `DeprecationWarning`，tests 仍走 shim —— **不要破坏**。

## 依赖方向

- `conftest.py` → `server.api`（`create_app`）、`server.ledger`（`consume`）、`server.vault`（`init_db` / `upsert_file`）、`server.vault.db`（`conn` 句柄）、`server.vault.db.schema`（`init_ledger_db`）。
- 各 `test_*.py` → conftest fixtures + 被测 `server.*` 模块。
- `functional/` → `server.*` 生产模块（`proposals` / `bgagent.executor` / `bgagent.runner` / `bgagent.notify` / `vault.db.schema` / `vault.db.conn` / `vault.db.migrate_ledger` / `config`）；其中 `mock_llm.py` **零 server 依赖**（纯 stdlib http.server），`seed.py` 依赖 `server.proposals` + `server.vault.db.changes` / `recall` / `schema`，`run_sentinel_once.py` import `vault.db.conn` 注册句柄对（mock 下 recall/proposal 工具可用的前提）。
- 元测试（import 纪律 / 路由顺序 / shim）→ 读 `server/` 源码树做静态断言，不 import 被测对象本身。

## 注意事项

- **Windows 换行**：测试 fixture 写样本文件**一律 `write_bytes`**（二进制），否则 SHA-256 在 Windows 上因换行转译（CRLF）不匹配。新增文本 fixture 必须沿用二进制写入。
- **sync 锁是内存的**：测试模拟锁定直设 `app.state.ctx.sync_lock.locked_at`（seam 在 `WebContext.sync_lock`，`app.state.ctx` 暴露；`server/sync_lock.py` 明确"通过该 seam 驱动，而非戳模块全局"），**不碰模块全局**。sync 锁 stale 300s。
- **双域 fixture 不可混用**：`db_conn` 与 `ledger_conn` 分属两个 SQLite 文件，分别覆盖 index 域与 ledger 域；断言权威状态表的测试要用 `ledger_conn`，断言可重建缓存的用 `db_conn`。
- **不钉测试总数**：套件持续增长，README 不写"共 N 个测试"，只给运行方式（`pytest tests/ -v`）。
- **functional harness 的配置不入库**：`config.fun.yaml` 由 `config.example.fun.yaml` sed 生成（含本地 scratch-vault 绝对路径），不提交。
