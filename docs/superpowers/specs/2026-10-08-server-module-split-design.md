# Server 模块拆分设计：db.py / agent.py → 包结构

日期：2026-10-08
状态：已确认骨架，待实现

## 目标

`server/db.py`（1122 行）和 `server/agent.py`（604 行）职责清晰但单文件过大，且有持续累加倾向。本次拆分为包结构，使每个模块 <200 行、边界按领域划分，为后续演进（新智能体、新检索/图谱特性）提供落点。

**硬约束：纯移动代码，零行为变更。** 所有函数体、签名、默认值原样搬迁；不重构逻辑、不改接口。

## 目录骨架

```
server/
├── db/
│   ├── __init__.py      # re-export 兼容层（见下）
│   ├── schema.py        # 连接与锁：init_db / init_segments / init_graph、_db_lock、
│   │                    #   _ACTION_TO_OP / _OP_TO_ACTION 映射、旧表迁移逻辑
│   ├── file_index.py    # files 表：upsert_file / get_all_files / delete_file
│   ├── sessions.py      # sessions 表：save_session / load_session / get_recent_sessions
│   ├── changes.py       # change_log 表：log_change / get_changes / clear_changes
│   ├── segments.py      # RAG 分块：segment_markdown / _tokenize_for_index / _split_long_text /
│   │                    #   index_segments / delete_segments / rebuild_segments_all /
│   │                    #   segments_search / _iter_vault_md
│   ├── graph.py         # wikilink 图谱：graph_upsert_entity / graph_add_edge /
│   │                    #   graph_extract_file / graph_delete_file / graph_get_backlinks /
│   │                    #   graph_get_related / graph_rebuild_all / _resolve_wikilink /
│   │                    #   _expand_wikilinks / _extract_title / _find_referrers
│   └── file_ops.py      # 文件系统写路径（非 db）：apply_change / edit_file /
│                        #   _is_filename_unique / _update_references / _update_references_batch /
│                        #   _remove_dead_links
├── agent/
│   ├── __init__.py      # re-export 兼容层（见下）
│   ├── prompts.py       # SYSTEM_PROMPT（读 prompts/system.md）、_get_period / _build_time_block、
│   │                    #   sse() 格式化
│   ├── tools.py         # 工具定义：10 个 Args(BaseModel)、TOOL_ARGS、_tool()、
│   │                    #   TOOLS / READ_TOOLS / WRITE_TOOLS 注册表
│   ├── validate.py      # validate_tool_call / parse_since、_check_md_path / _check_optional_path
│   ├── execute.py       # 工具执行：execute_tool（name → 动作分发，含 confirm 机制）
│   └── loop.py          # agent_loop（LLM 调用 + 工具循环编排）
```

### 命名说明

- `file_index`（索引表里有什么）vs `file_ops`（往磁盘写什么），避免两个 "files" 混淆。
- agent 侧 `tools.py`（定义，给 LLM 看的 JSON Schema）+ `execute.py`（实现，分发执行）成对出现。

## 连接管理（保持现状）

- **创建**：`main.py` 启动时 `init_db(db_path)`，进程内单连接（WAL）。
- **传递**：`conn` 显式传参（`db_conn`），不引入连接池/全局单例 — 测试可各自开临时 db。
- **并发保护**：模块级 `_db_lock` 随 `init_db` 一起归入 `schema.py`，成为"连接与锁"的唯一归属地。
- 函数签名不变：所有 db 模块函数仍接收 `conn` 参数。

## 兼容层规则（re-export）

两个 `__init__.py` 把原模块的全部公开符号 re-export，使现有 ~100 处 import（`from server.db import X` / `from .db import X` / `from .agent import X`，含测试）**一行不改**：

- `server/db/__init__.py`：re-export 7 个子模块的全部公开函数 + `_db_lock`、`_ACTION_TO_OP`、`_OP_TO_ACTION`（测试直接引用了 `_update_references`、`_is_filename_unique`、`_resolve_wikilink`、`_expand_wikilinks`、`_find_referrers` 等下划线符号，一并导出）。
- `server/agent/__init__.py`：re-export `SYSTEM_PROMPT`、`_build_time_block`、`sse`、`TOOLS`、`READ_TOOLS`、`WRITE_TOOLS`、`validate_tool_call`、`parse_since`、`execute_tool`、`agent_loop` 及 10 个 Args 类。

规则：
1. 本次所有代码移动后，**不修改任何调用方 import**。
2. 后续新代码应直接 import 子模块（如 `from server.db.segments import segments_search`），旧路径逐步淘汰。
3. 删除原 `server/db.py`、`server/agent.py`（由包目录取代）。

## 模块间依赖（拆分后）

- `db/file_ops.py` → `db/changes.py`（log_change）、`db/graph.py`（图谱更新）：原 db.py 内部调用变为跨模块 import。
- `db/segments.py`、`db/graph.py` → `db/schema.py` 无依赖（只接收 conn）。
- `agent/execute.py` → `db/file_ops.py`、`db/graph.py`、`db/changes.py`（原 `from .db import ...` 局部 import 改为指向子模块）。
- `agent/loop.py` → `agent/tools.py`（无，tools 由调用方传入）、`ai.py`。
- `agent/tools.py` 不依赖 execute/loop（纯声明）。

## 迁移步骤

1. 建 `server/db/` 包：按上表把 db.py 的函数体原样搬入各子模块，处理模块内互调（改为跨模块 import），写 `__init__.py` re-export。删除 `server/db.py`。
2. 建 `server/agent/` 包：同上。注意 `prompts/system.md` 的相对路径（`Path(__file__).parent / "prompts"` → 需指向 `server/prompts/`，保持 prompt 文件位置不变）。删除 `server/agent.py`。
3. 全量测试：`pytest tests/ -v` 必须全绿（现有 ~100 处 import 不动，作为兼容层的验证）。
4. 冒烟：`python -m server.main config.yaml` 启动，health/docs/agent 路由可用。

## 验证标准

- `pytest tests/ -v` 全绿（拆分前后同一套测试，零修改）。
- 每个新文件 <250 行。
- `grep -rn "from server.db import\|from .db import"` 结果不变（调用方零改动）。
- 服务可启动，`/api/health`、`/api/docs`、agent chat SSE 正常。

## 不做的事

- 不改任何函数行为、签名、默认值。
- 不引入连接池 / Database 类封装（后续演进项）。
- 不动 client/、templates/、prompts/ 内容。
- 不拆 api.py（431 行，本次范围外）。
