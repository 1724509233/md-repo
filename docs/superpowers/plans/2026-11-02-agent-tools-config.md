# Agent tools 配置（待办 b4620a26）

> Goal: 主 agent（Web 聊天通道）支持按 `ai.tools` 配置裁剪工具面，未知工具名启动时 fail-fast；缺省 = 全量工具（现状不变）。
> 待办原文：「agent缺少tools配置，_get_agent默认全部工具注册上下文」

## Architecture

- **配置面**：`ai:` 段新增可选 `tools:` 列表（工具名）。解析在 `server/config/core.py:load_server_config`（现 L40-52 构造 `AIConfig`），数据类 `server/config/ai.py:AIConfig` 加 `tools: list[str] | None = None`（None = 未配置 = 全量）。
- **校验（fail-fast）**：配置加载时对照 `TOOL_REGISTRY.keys()`（server/agent/tools/__init__.py）逐一核对，未知名抛 `ValueError`（列出未知名 + 可用名）。镜像 bgagent 法律层先例（server/config/bgagent.py：tools 必须显式声明、非法即启动报错，无 fail-open 默认）。主 agent 的差别：未配置 = 全量（向后兼容，现状即全量）；bgagent 的未配置 = 报错。
- **消费面**：`server/routes/agent.py:_build_agent_spec`（L27-44，现 L38 硬编码 `tools=list(TOOL_REGISTRY.values())`）→ 按 `ctx.ai_config.tools` 过滤构造。`_get_agent`（L46-52）不变——它只是按 session 缓存 `Agent(_build_agent_spec(), ...)`，"默认全部工具"的根源在 `_build_agent_spec`。
- **不动**：bgagent（已有 tools 配置）、MCP 通道（mcp_server.py 固定 6 工具面，不在本待办范围）、确认流 / 策略层。

## Tech Stack

Python 3.12、FastAPI、pydantic（args 模型）、pytest。无新依赖。

## Spec / 依据

- 待办 b4620a26
- bgagent 法律层先例：`docs/superpowers/specs/2026-10-22-agent-permission-policy-design.md` §通道入口（bgagent tools 显式声明 + fail-fast）
- AGENTS.md 架构节（routes 按 URL 前缀分模块，agent 域 = server/routes/agent.py）

## Global Constraints

- `tests/test_routes_order.py` 钉住路由表顺序——本 plan 不改路由注册。
- 不引入认证/HTTPS（LAN-only 设计）。
- 缺省行为必须与现状逐字节一致（全量工具），现有 agent 会话/测试不受影响。

## Review Focus

- 过滤后工具面是否同步反映到：runtime 发给 LLM 的 tools 列表（LoopRuntime 按 spec.tools 构建）+ 确认流（spec.tools 之外的工具名不会再出现在 tool_call 里，无需额外处理）。
- 配置示例（config.example.yaml）措辞：说明缺省=全量、显式列表=白名单、拼错=启动失败。

## Tasks

### Task 1：AIConfig 增加 tools 字段 + 加载期 fail-fast（TDD）

- [ ] 1.1 红：`tests/test_config.py`（已存在）新增（风格参照 `tests/test_config_legal_layer.py` 的 fail-fast 用例先例）：
  - `test_ai_tools_subset_accepted`：yaml `ai.tools: ["read_doc", "search_docs"]` → `load_server_config` 成功，`cfg.ai.tools == ["read_doc", "search_docs"]`。
  - `test_ai_tools_unknown_name_fails_fast`：`ai.tools: ["read_doc", "nope_tool"]` → `pytest.raises(ValueError)`，消息含 `nope_tool` 与至少一个可用名。
  - `test_ai_tools_absent_defaults_none`：无 `tools` 键 → `cfg.ai.tools is None`。
  - fixture 用 `write_bytes` 写 yaml（AGENTS.md Windows 换行约定；yaml 为纯文本，文本写入亦可，但与测试套件既有 fixture 风格保持一致）。
- [ ] 1.2 绿：`server/config/ai.py` `AIConfig` 加 `tools: list[str] | None = None`；`server/config/core.py` 解析 `ai_raw.get("tools")`（list[str] 或 None；非 list 抛 ValueError），构造后校验每个名字 ∈ `TOOL_REGISTRY`（`from ..agent.tools import TOOL_REGISTRY`），未知名抛 ValueError（含未知名列表 + `sorted(TOOL_REGISTRY)` 可用名）。
- [ ] 1.3 跑 `pytest tests/test_config.py -v`（或新建测试所在文件）全绿；跑 `pytest tests/test_agent.py -q` 确认无回归。

### Task 2：_build_agent_spec 按配置过滤（TDD）

- [ ] 2.1 红：`tests/test_agent.py` 新增
  - `test_build_agent_spec_filters_tools`：构造最小 ctx stub（`ai_config` 带 `tools=["read_doc"]`、`vault_dir=tmp_path`），调用 `_build_agent_spec`（需要把它从 register 闭包内提出为模块级函数 `_build_agent_spec(ctx)` 以便测试——**重构点**：签名改 `_build_agent_spec(ctx)`，`register` 内 L38 改调用；不改变任何路由行为）→ `spec.tools == [TOOL_REGISTRY["read_doc"]]`。
  - `test_build_agent_spec_default_all_tools`：`ai_config.tools is None` → `set(spec.tools) == set(TOOL_REGISTRY.values())`。
- [ ] 2.2 绿：`server/routes/agent.py` 按上述重构；过滤实现：`[TOOL_REGISTRY[n] for n in ctx.ai_config.tools] if ctx.ai_config.tools else list(TOOL_REGISTRY.values())`（加载期已保证名字合法，此处不再二次校验）。
- [ ] 2.3 验证：`pytest tests/test_agent.py -q` + `pytest tests/test_api.py -q` 全绿；`python -m server.main config.yaml` 冒烟（缺省配置下 agent 正常创建，日志无报错）。

### Task 3：配置示例 + 收尾

- [ ] 3.1 `config.example.yaml` `ai:` 段加注释与示例（`# tools: [read_doc, search_docs, propose]  # 可选：工具白名单；缺省 = 全部工具；未知名启动报错`），示例值保持注释态。
- [ ] 3.2 全量 `pytest tests/ -q` 通过。
- [ ] 3.3 关单：待办 b4620a26 标记完成（在用户确认本 plan 执行完毕之后）。

## 执行方法

单 lane @fixer（小改动，3 文件 + 测试），lane brief = 本文件 + `server/config/bgagent.py` 的 fail-fast 措辞先例。
