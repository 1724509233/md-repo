# Spec：Agent 权限策略收拢（policy.py）

> **定位**：纯收拢重构设计。把散落在 4 处的 agent 权限规则收拢到单一权威模块，通道入口只声明、不实现检查。
>
> **分支**：`feat/memory-runtime` · **状态**：待评审 · **日期**：2026-10-22
>
> **前置**：commit `8484773`（suggestions/ 直写校验，27 用例）；2026-10-22 权限审计（3 通道 × 7 层防线梳理）
>
> **评审**：2026-10-22 oracle 设计模式充分性评审——结论「足够，需修正，无需重设计」；6 项修正已并入（子树闭合 / L1 格式定性 / fail-closed + meta-test / 字段注释 / McpExternalGate 归属 / 单入口 claim 精度）
>
> **依据标记**：【代码】= 已读源码核实；【宪法】= memory-runtime 宪法文档（`D:/md-vault/projects/md-repo/docs/memory-runtime.md`）

---

## 1. 背景：规则散落与三个结构问题

权限规则当前分布在 4 处【代码】：

| 位置 | 内容 |
|------|------|
| `server/agent/` 核心 | `spec.py`（能力声明）、`validate.py`（schema + 边界 + scope 检查）、`runtime.py`（确认流程）、`execute.py`（dispatch） |
| `server/agent/tools/` | `tool.py`（路径语法校验 `_check_md_path`、`Tool.permission`）、各工具 args 模型、`write_doc/edit_doc._execute`（suggestions 手写接线） |
| 通道入口 ×3 | `api.py::_build_agent_spec`（主 agent）、`bgagent/runner.py`（bgagent）、`mcp_server.py`（`_gate_external_write` + suggestions 手写接线 + ledger） |
| `server/proposals.py` | suggestions/ 不变量本体（`validate_suggestions_write`）+ 状态机 |

三个结构问题：

- **P1 边界双查漂移**：vault 边界在语法层（`tool.py::_check_md_path`：`..`/绝对路径/`.md`/`.sync`）和语义层（`validate.py`：resolve + `is_relative_to`）各查一遍，两处规则独立维护——`.sync` 只有语法层拦，语义层不认。
- **P2 域名规则"1 本体 + 2 手写接线"**：`validate_suggestions_write` 被 MCP（`mcp_server.py` write_doc/edit_doc）和 agent 工具（`write_doc.py`/`edit_doc.py::_execute`）各手写一遍"读旧内容 + `count==1` 假设替换 + 校验"；move/delete 两个 ask 工具无接线（绕写口）。
- **P3 通道能力无单一来源**："哪个通道有什么 scope/确认策略"写在 3 个构造点，`validate.py` 只认传入参数，加新通道或改语义要三处同改。

## 2. 目标与非目标

**目标**：

1. **检查单一入口**：所有通道的工具调用检查都走 `policy.py::validate_tool_call(name, args, policy)`；通道入口不手写任何检查。（“单入口”在**通道层**——runtime / MCP handler——成立；`execute_tool` 是不做检查的执行层 facade，本重构不改。）
2. **域名规则单接线**：`SuggestionsGate` 一处实现覆盖全部 4 个 ask 工具 × 全部通道；删除两处手写接线。
3. **通道只声明**：`ChannelPolicy` dataclass 是检查的唯一输入；主 agent / bgagent 的能力由 `AgentSpec.policy()` 派生，MCP 显式构造。
4. **行为保持**：现有接受/拒绝决策与错误消息不变（由现存 27 + gate 测试钉住），除第 4 节明确列出的缺口闭合。

**非目标**（各自独立待办，本重构不碰）：

- validate scope 与 ask 流程解耦（vault 待办 #5，语义变更：scope 是否约束 read 工具）
- 主 agent 加 write_scope（权限变更，随 auto_approve 开关一起决策）
- auto_approve 前端开关（vault 待办 #4）
- MCP write_doc 假成功排查（vault 待办 #7）

## 3. 目标架构

### 3.1 新模块 `server/agent/policy.py`

```python
@dataclass(frozen=True)
class ChannelPolicy:
    """通道能力声明 —— validate_tool_call 的唯一输入（检查语义，无流程语义）。"""
    vault_dir: Path
    write_scope: Path | None = None   # 写边界（None = 全 vault；ask-only 政策在 validate_tool_call 内）
    report_dir: Path | None = None    # 保护目录，ask 工具禁写
    gates: tuple[WriteGate, ...] = () # 通道专属规则（MCP：主区只读）

class WriteGate:
    """域名/通道写规则。read 工具一律 no-op。"""
    name: str
    def check(self, tool_name: str, args: dict, vault_dir: Path) -> str | None: ...

VAULT_GATES: tuple[WriteGate, ...] = (SuggestionsGate(),)
# vault 不变量：任何能写的通道都不能伪造状态机，无条件应用，通道不可关闭。
# 通道专属 gate（如 McpExternalGate）定义在各自通道模块（mcp_server.py），经 gates 注入。

def validate_tool_call(name: str, args: dict, policy: ChannelPolicy) -> str | None:
    """1. pydantic schema（保留 validate.py 现有简单格式 `{loc}: {msg}`；
       原值回放 + 类型提示的详细格式在执行层 Tool.validate，同一 model 的冗余
       校验，本重构不动）
       2. 语义边界：resolve 后在 vault 内 + 首段非 .sync + write_scope + report_dir
       3. gates：VAULT_GATES + policy.gates，按序，首个非 None 即拒绝
       返回错误串或 None。"""
```

要点：

- `auto_approve` **不进** ChannelPolicy——它是 runtime 流程输入（跳过 confirm），不是检查，留在 `AgentSpec`。
- 工具白名单**不进** ChannelPolicy——它是 LLM 呈现层（`spec.tools` → `to_openai_tool` 列表），不是检查；MCP 的"只有 5 个工具"由 handler 暴露面保证。
- `validate.py` 的 `validate_tool_call` 移入 policy.py；`parse_since` 留在 validate.py。全部调用点在库内（runtime / mcp_server / 测试），**不做兼容 shim**——签名变更一次改完。

### 3.2 两级 gate：vault 不变量 vs 通道规则

| Gate | 归属 | 内容 | 替换 |
|------|------|------|------|
| `SuggestionsGate` | `VAULT_GATES`（全局，policy.py） | suggestions/ 不变量的单接线：write_doc/edit_doc 调 `proposals.validate_suggestions_write`（含读旧内容、`count==1` 假设替换、文件不存在时让位工具规范错误）；**move_docs / delete_doc 首段为 `suggestions`（任意深度）一律拒绝**（缺口闭合，见 §4；与 MCP gate 既有子树前缀口径一致，堵住子目录绕行） | 两处手写接线（MCP + agent 工具） |
| `McpExternalGate` | 通道规则（MCP 的 `policy.gates`）·**类留在 `mcp_server.py`**（依赖方向 channel → policy，policy.py 不积累通道细节） | 非 suggestions/ 路径 → 返回现行协议指引错误串（文案逐字保留：main area read-only + `type: correction` / `status: awaiting_review` / 文件名规范） | `mcp_server.py::_gate_external_write` |

`McpExternalGate` 用 gate 而非 `write_scope=suggestions` 的原因：scope 检查的错误消息是通用文案，会丢掉对外部 LLM 关键的"如何写提案"协议指引。

**gate 作用域与 fail-closed**（2026-10-22 评审修正）：gate 判定"是否写工具"用 `TOOL_REGISTRY[tool_name].permission == "ask"`（结构化，policy.py 与 tools 同包无循环依赖），tool_name 只用于内容语义分支（write/edit 算内容，move/delete 拒绝）。**默认分支 fail-closed**：已注册的 ask 工具 + 命中 suggestions 子树 + 未命中任何已知分支 → 一律拒绝——未来新写工具（如 rename_doc）必须被 gate 显式覆盖，否则 CI 大声失败，绝不静默放行。同类键名约定一并落文档：`_extract_paths` 只认 `path`/`from_path`/`to_path`/`moves` 参数名，新 ask 工具必须沿用标准路径参数名（否则连 L2 边界/scope 检查都看不见它）。

### 3.3 通道入口改造

| 通道 | 改造 |
|------|------|
| 主 agent（`api.py`） | `_build_agent_spec` 不变；`AgentSpec` 增 `policy()` 方法派生 `ChannelPolicy(vault_dir, write_scope, report_dir)`（gates 空） |
| bgagent（`runner.py`） | 构造不变，同上经 `spec.policy()`；target_dir/report_dir 照旧传入 |
| 外部 MCP（`mcp_server.py`） | `main()` 构造 `MCP_POLICY = ChannelPolicy(vault_dir, gates=(McpExternalGate(),))`；write_doc/edit_doc **删除** suggestions 手写接线与 `_gate_external_write` 调用，统一 `validate_tool_call(name, args, MCP_POLICY)`；ledger 逻辑不动 |

runtime（`runtime.py`）：`validate_tool_call(tc["name"], args, spec.policy())`，删除 write_scope/report_dir 传参。

### 3.4 边界双查收拢（P1）

- 语义层成为**权威边界**：resolve + vault 内 + **新增首段 `.sync` 排除** + write_scope + report_dir。
- 语法层（args 模型的 `_check_md_path`）**保留**：早失败、模型友好的错误（`.md only` 等），但不再是唯一防线。
- 测试钉住：构造"语法层放行、语义层命中 `.sync`"的用例，证明权威检查不依赖语法层。

## 4. 行为盘点：保持 vs 变更

| # | 场景 | 现状 | 重构后 | 类型 |
|---|------|------|--------|------|
| 1 | 三通道 write_doc/edit_doc 到 suggestions/ | 校验（type 白名单 / status 不变量 / 文件名） | 同 | 保持 |
| 2 | 主 agent 写主区 | 免确认校验外无 scope 限制，ask 工具逐个 confirm | 同 | 保持 |
| 3 | bgagent 越出 target_dir / 写 report_dir | scope 错误串拒绝 | 同（错误串不变） | 保持 |
| 4 | MCP 写主区 | 协议指引错误串 | 同（`McpExternalGate` 逐字保留） | 保持 |
| 5 | agent 工具 suggestions 校验的**时机** | confirm 批准后在 `_execute` 内拒绝 | confirm 之前（validate 阶段）拒绝 | 改善：不再让用户批准一个必然被拒的操作 |
| 6 | **move_docs 任一方向路径首段为 `suggestions`（任意深度）** | 无检查（可把提案移出状态机视野 / 改文件名破坏规范 / 移入子目录藏进 scan() 视野外） | 一律拒绝，错误串指向控制台 | **缺口闭合（行为变更）** |
| 7 | **delete_doc 路径首段为 `suggestions`（任意深度）** | 无检查（绕过状态机消灭提案，台账留孤儿行） | 一律拒绝 | **缺口闭合（行为变更）**【宪法】物理删除 = 永远人工通道（Web UI 文件管理走 `/api/docs/manage`，不经 agent 策略，不受影响） |
| 8 | `.sync` 写入 | 仅语法层拦 | 语义层权威拦（+语法层早失败） | 无观测变化，补漂移漏洞 |

#6/#7 的拒绝文案建议：`Error: suggestions/ is state-machine territory — proposals cannot be moved/deleted by direct tool writes (use the memory console)`。

**配置影响核查**【代码】：当前 config 中无 bgagent 的 target_dir 指向 suggestions/（relay 类 agent 指向各自目录），#6/#7 不影响现有运行；未来若配置 target_dir=suggestions/，该 agent 的 move/delete 受限为预期行为。

**扩展注记**（memory-runtime 宪法「模块 5 授权分层表」→ 现有 seam 的映射，留给 memory-runtime 任务，本重构不实施）：三层中两层已落地——"自动执行+留审计" = `AUTO_TYPES` + `write_source` 台账；"全自动" = auto 通道。唯一机制缺口是"永远人工"（须可越过 `auto_approve=True`）：未来需 permission 第三态（如 `always_ask`）+ runtime 确认决策点（现 runtime.py:159 单点）配合，约 10 行；当前零消费者，YAGNI 不预留。注意检查（validate 阶段）不可被 auto_approve 跳过，而确认（流程）可以——suggestions 域的"物理删除永远人工"已经以 vault 不变量（VAULT_GATES）形式落地，任何通道绕不过。

## 5. 改动面（文件级）

| 文件 | 改动 |
|------|------|
| `server/agent/policy.py` | **新增**：ChannelPolicy / WriteGate / SuggestionsGate / VAULT_GATES / validate_tool_call（移入 + 语义 `.sync` + fail-closed 默认分支） |
| `server/agent/spec.py` | +`policy()` 方法 |
| `server/agent/runtime.py` | 改 validate_tool_call 调用为 policy 形式 |
| `server/agent/validate.py` | 删除 validate_tool_call（parse_since 保留） |
| `server/agent/tools/write_doc.py` `edit_doc.py` | 删除 `_execute` 内 suggestions 手写接线 |
| `server/mcp_server.py` | 删除 `_gate_external_write` + 两处手写接线；**新增 `McpExternalGate`（通道专属，留本模块）**；构造 MCP_POLICY；handler 内 validate 调用改 policy 形式 |
| `server/agent/__init__.py` | 更新 re-export（policy 面） |
| `tests/test_suggestions_validation.py` | 5 个工具入口集成用例改写为 `validate_tool_call(name, args, policy)` 形式（测真实通道路径，更强）；新增 move/delete 拒绝用例（含子目录路径） |
| `tests/test_mcp_gate.py` | 适配 policy 构造（协议指引文案断言不动） |
| `tests/test_agent.py` / `test_bgagent_runner.py` | 适配签名 |
| 新增 meta-test（位置随 test_suggestions_validation.py） | 遍历 `TOOL_REGISTRY` 中 `permission == "ask"` 的全部工具，构造指向 `suggestions/` 的 probe 调用，断言 `validate_tool_call` 拒绝——新 ask 工具落地时 gate 未覆盖则 CI 红 |

预估：policy.py 新增 ~150 行；删除 ~40 行；测试净增 ~80 行。

## 6. 测试与验证

1. `pytest tests/ -q` 全量（基线 537+，另 1 个已知 flaky：`test_trigger_tick_runs_when_human_event`，已证明与改动无关）。
2. 重点套件：`test_suggestions_validation` / `test_mcp_gate` / `test_agent` / `test_bgagent_runner` / `test_proposals`。
3. 行为变更 A/B：#6/#7 的拒绝是仅有的新拒绝——用 git stash 对照确认干净树上这些用例是"接受"，重构后是"拒绝"，其余决策一致。
4. L1 格式钉住：`validate_tool_call` 保持简单格式（`{loc}: {msg}`），MCP 错误串逐字不变，test_mcp_gate 现有断言零改动即通过。
5. MCP 独立进程回归：`md-vault-mcp` 的 stdio 入口依赖 validate_tool_call 签名，test_mcp_gate 覆盖；改动后 MCP 进程需重启才生效（AGENTS.md 已知流程）。

## 7. 分阶段执行

| 阶段 | 内容 | 行为 | 独立可验证 |
|------|------|------|-----------|
| 1 | policy.py 骨架（ChannelPolicy / validate 移入 / 语义 `.sync` / AgentSpec.policy / runtime 适配） | 无变化 | 全量测试绿 |
| 2 | SuggestionsGate 单接线 + 删两处手写接线 + 集成测试改写 | 无变化（#5 时机改善） | 全量绿 + A/B |
| 3 | McpExternalGate 替换 `_gate_external_write` | 无变化 | test_mcp_gate 绿 |
| 4 | move/delete 缺口闭合（#6/#7，suggestions 子树任意深度 + fail-closed 默认分支 + meta-test） | **行为变更** | 新用例 + A/B |

每阶段一个 commit，阶段 4 可单独回滚（`git revert` 一commit）而不影响 1-3 的收拢成果。

## 8. 风险

| 风险 | 缓解 |
|------|------|
| validate_tool_call 签名变更波及 MCP 独立进程 | 库内调用点全部同 PR 改完；test_mcp_gate 钉住；无 shim 不留半新半旧 |
| bgagent 无人值守遇到新的 move/delete 拒绝，产生未知错误串进 LLM 上下文 | 拒绝串短且可行动（§4 文案）；当前配置无 agent 写 suggestions/，实际暴露面为零 |
| 确认流程时序变化（#5）影响前端 | confirm_request 事件面不变，只是无效写更早被拒，前端无需改动 |

## 9. 设计决策点（待评审确认）

1. **move/delete 缺口闭合纳入本重构**（推荐 §4 #6/#7 方案：suggestions 子树任意深度一律拒绝，清理走 Web UI 人工通道）——2026-10-22 评审确认子树口径（原"顶层"措辞留子目录绕行缝，已修正）。若要求"纯收拢零行为变更"，可剔除阶段 4 单开任务，但 P2 的"单接线"声明将只对 write/edit 成立。
2. **McpExternalGate 用 gate 而非 write_scope=suggestions**——为保协议指引文案；替代方案是 scope 检查支持自定义拒绝文案（更通用但当前只有一个消费者，YAGNI）。
3. **不做兼容 shim**——validate_tool_call 签名一次改完；替代方案是旧签名转发（多一层维护面，调用点本就不多）。
4. **模块命名 `policy.py`**（ChannelPolicy 语义）；备选 `gate.py`（更贴"检查"本意但声明面也在这，名不副实）。
