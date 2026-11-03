# server/config/ — YAML 配置解析包

md-sync 服务端读取 `config.yaml` 的解析包：拆成五个模块，各自解析一段并产出强类型 dataclass，**所有加载都是 fail-fast**（非法配置在启动期报错，不运行到中途才炸）。由 `server/main.py`（及 `mcp_server.py`）在入口调用。

## 模块结构

| 文件 | 职责 |
|------|------|
| `core.py` | `ServerConfig` + `load_server_config()`；`_parse_ai_tools()` 校验 `ai.tools` 白名单 |
| `ai.py` | `AIConfig` 数据类（主 agent 的 LLM 连接配置） |
| `log.py` | `LogConfig` 数据类 + `from_raw()`（扁平 `server.log_*` 键，fail-fast 校验） |
| `notifications.py` | `NotificationsConfig` + `load_notifications()`（通知量护栏，cap 是法） |
| `bgagent.py` | `BackgroundAgentConfig` + `load_background_agents()`；`TRIGGER_OPS` / `TRIGGER_DEFAULTS`；trigger / tools / verbs / escalation 解析器 |

`__init__.py` 重导出公共面：`ServerConfig`、`load_server_config`、`AIConfig`、`LogConfig`、`NotificationsConfig`、`load_notifications`、`ModelConfig`、`BackgroundAgentConfig`、`load_background_agents`、`TRIGGER_OPS`、`TRIGGER_DEFAULTS`。

## 公共 API（函数级）

| 符号 | 签名 | 说明 |
|------|------|------|
| `load_server_config` | `(config_path: str) -> ServerConfig` | 读 `server:` 段（+ 顶层 `ai:`）；文件缺失 → `FileNotFoundError` |
| `load_notifications` | `(config_path: str) -> NotificationsConfig` | 读顶层 `notifications:` 段（缺失 → 默认） |
| `load_background_agents` | `(config_path: str, vault_dir: Path) -> list[BackgroundAgentConfig]` | 读 `background_agents:` 段（缺失 → `[]`） |
| `LogConfig.from_raw` | `(raw: dict, config_path: str) -> LogConfig` | 解析扁平 `server.log_*` 键（classmethod） |

### 数据类字段表

**ServerConfig**

| 字段 | 类型 | 说明 |
|------|------|------|
| `host` | str | 监听地址 |
| `port` | int | 端口（默认 9888） |
| `vault_dir` | Path | 解析后的绝对 vault 路径（`~` 展开 + resolve） |
| `ai` | `AIConfig \| None` | 未配置 AI 段 → None |
| `log` | `LogConfig` | default factory |

**AIConfig**

| 字段 | 默认 | 说明 |
|------|------|------|
| `base_url` | （必填） | OpenAI 兼容端点 |
| `api_key` | `""` | Bearer key |
| `model` | `"qwen2.5:14b"` | 模型 id |
| `max_tokens` | `4096` | 单次输出上限 |
| `temperature` | `0.7` | 采样温度 |
| `context_window` | `16384` | 上下文窗口 |
| `max_rounds` | `15` | agent 流式循环轮数上限 |
| `read_doc_max_chars` | `4000` | `read_doc` 截断阈值 |
| `tools` | `None` | 主 agent 工具白名单；None = 未配置 = 全量（未知名启动报错，见下） |

**LogConfig**（YAML 扁平键 `server.log_level` / `log_dir` / `log_retention_days`）

| 字段 | 默认 | 说明 |
|------|------|------|
| `level` | `"INFO"` | 必须为 stdlib 合法级别名 |
| `dir` | `".sync/logs"` | 相对路径按 `vault_dir` 解析 |
| `retention_days` | `30` | 必须 ≥ 1（禁用每日清理） |

**NotificationsConfig**

| 字段 | 默认 | 说明 |
|------|------|------|
| `daily_cap` | `20` | 每日正常通知行上限（cap 是法，可配非硬编码；≥ 1） |

**BackgroundAgentConfig**

| 字段 | 默认 | 说明 |
|------|------|------|
| `name` | alias | 显示名 |
| `alias` | （必填） | 唯一标识（必须唯一） |
| `enabled` | `True` | 是否启用 |
| `cron` | （必填） | 5 段标准 cron（server 时区） |
| `target_dir` | （必填） | 写 scope，解析后的绝对路径 |
| `prompt` | （必填） | system-prompt 文件，相对 **config 文件所在目录** |
| `report_dir` | `<target_dir>/.reports/<alias>` | 报告输出，解析后的绝对路径 |
| `max_rounds` | `30` | 单次 run 轮数上限 |
| `timeout_s` | `1800` | 单次 run 超时 |
| `model` | （必填） | `ModelConfig`（独立 provider 配置，**不复用 `ai:` 段**） |
| `budget_runs_per_day` | `3` | 当日 run 预算 cap |
| `budget_max_proposals` | `5` | 当日提案预算 cap |
| `trigger` | `None` | arming 配置（见下） |
| `is_executor` | `False` | 确定性执行器角色（至多一个，target_dir 不限，免重叠检查） |
| `tools` | `None` | LLM 角色**必填**工具白名单（缺失 = 加载报错，fail-closed）；executor 豁免 |
| `verbs` | `None` | `tools` 含 `propose` 时**必填**的 propose 动词白名单 |
| `escalation` | `None` | 升级元数据（condition → action 映射） |

`TRIGGER_OPS = {create, update, delete, move}`；`TRIGGER_DEFAULTS = {arm_count: 3, min_interval_h: 1.0}`。

## 关键设计

### 双 section 约定

单一 `config.yaml` 含 `server:` / `client:` 双 section，**各读各段**：client 由 `client/config.py` 读 `client:`；server 侧 `load_server_config` 读 `server:` 段，另从顶层读 `ai:` / `background_agents:` / `notifications:` 段。两边不互相读取对方 section。

### ai.tools fail-fast

`_parse_ai_tools()`：list 中每个名字必须存在于 agent 工具注册表（`TOOL_REGISTRY`）。未配置 / None → None（全量，向后兼容——默认一直是全量）；**空 list 或未知名 → 加载期 `ValueError`**（fail-fast，mirror bgagent `tools` 先例——一个空白名单不得静默授予全部工具）。

### bgagent 合法层（加载期 fail-fast）

`load_background_agents` 在构造前逐项校验，违规直接报错：

- **alias 唯一**：duplicate → 报错。
- **target_dir 不得重叠**（嵌套即重叠）：**executor 豁免**——其 target_dir 可为 vault 根（天然包含所有 LLM scope，且无 LLM 写面）。共享写者面豁免：`suggestions/`（提案区，零文件落）与 `index/`（机器区：执行器 + 报告通道）双 scope 均在其中 → 豁免互叠。
- **report_dir 不得重叠**（共享目录会互覆盖报告、互挤剪枝额度；executor 豁免）。
- **LLM 角色 `tools` 白名单 fail-closed**：未声明（None）= 加载报错，**不会 fail-open 到全部工具**；executor 豁免（无 LLM tool 面）。
- **`propose` + `write_doc` 不可同角色**（ADR-0004 边界线：提案角色无直接写，终审门不可被绕过）。
- **`tools` 含 `propose` 时 `verbs` 必填**（未声明 verbs = 无限制 propose，禁止）。
- **至多一个 `is_executor`**。

### arming 配置语义（`trigger`）

配置侧只声明**门限**，运行时三重门判定（`armed ≥ arm_count` / `min_interval` 冷却 / 当日预算）见 [../README.md](../README.md) 的 bgagent 调度链路。

| `source` | 必填键 | 说明 |
|----------|--------|------|
| `change`（默认） | `events`（非空 list，`TRIGGER_OPS` 子集） | 计匹配的 change_log 事件 |
| `recall` | `metric: "failed_or_low"` | 计 failed / low-score 召回命中（`events` 被忽略 + warning） |

共用的门限键（两 source）：`arm_count`（默认 3，≥ 1）、`min_interval_h`（默认 1.0）、`low_score`（默认 0.2，须在 `(0,1)`——是命中判分阈值，非独立 run 门）。

spec `~/md-vault/projects/md-repo/specs/2026-10-11-bgagent-design.md` 与 `~/md-vault/projects/md-repo/specs/2026-10-24-event-driven-scheduler-design.md`。

## 依赖方向

```
config/
  core.py ──►  ai.AIConfig / log.LogConfig / agent.tools.TOOL_REGISTRY（校验白名单）
  bgagent.py ─►  ../model.ModelConfig / ../proposals（PROPOSALS_DIR 顶层 import；
                                VALID_TYPES 在 _parse_verbs() 内惰性 import）
               ─►  agent.tools.TOOL_REGISTRY（_parse_tools() 内惰性 import，避免 runner 引入的环）
  __init__.py ── 重导出公共面
  （leaf：只 import 数据类 + 注册表常量，不依赖 main / api / routes / vault）
```

`model.py`（`server/model.py`）是 `ModelConfig` 的**单一**定义（base-layer leaf），被 `config` 与 `agent` 共同 import——bgagent.py 从 `..model` 引入并重导出。

## 注意事项

- **相对路径解析基准不同（三处）**：服务端 `vault_dir` 相对**进程 cwd** 解析（`Path(...).resolve()`，无 config 目录参与）；bgagent 的 `target_dir` / `report_dir` 相对 `vault_dir` 解析（且须落在 vault 内），`prompt` 相对 **config 文件所在目录**解析（`load_background_agents` 用 `path.parent`）；`server.log_dir` 的相对路径按 `vault_dir` 解析（main.py 做 `vault_dir / logs_dir`）。改路径语义时区分这些基准（客户端 `vault_dir` 相对 config 文件目录，见 client/README）。
- **ModelConfig 单一化**：`ModelConfig` 只在 `server/model.py` 定义一次；新增字段须改这一处，别在 config / agent 各建一份。
- **AI 段是可选的**：顶层无 `ai:` 段（或缺 `base_url`）→ `ServerConfig.ai = None`，对应路由端点返回 503；但 bgagent 的 `model` 是**必填**的（每个 bgagent 独立 provider 配置，不沿用 `ai:` 段）。
- **cap 是法**：`daily_cap` / `budget_runs_per_day` / `budget_max_proposals` 都从 YAML 读，是配置法律而非框架硬编码——框架只 enforcement。

## 使用示例

```python
from server.config import load_server_config, load_background_agents, load_notifications

cfg = load_server_config("config.yaml")
vault = cfg.vault_dir
agents = load_background_agents("config.yaml", vault)   # [] 若未配置
notif = load_notifications("config.yaml")               # daily_cap 默认 20
```
