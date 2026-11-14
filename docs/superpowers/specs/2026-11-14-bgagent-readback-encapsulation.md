# bgagent 回读封装（reminder + gate）+ 驳回段 + sentinel 工具面

状态：draft v2（2026-11-14，用户逐项拍板；oracle 审查 approve-with-changes，M1-M3/S1-S4 已并入）。决策编号 R* 为本 spec 局部；引用的 D9/D14/D23 属 recall-data-chain spec（2026-11-13）与 reversible-automation v2。

## 1. 背景

- 回读注入（reversible-automation v2 §5.3：提案积压 + 失败回读；D9：relatedness 忽略遥测；D14：点击/反馈消费信号）目前实现为 `server/bgagent/runner.py::_readback_block(alias)` 私有函数（~120 行），字符串拼接进 `system_prompt`——与 runner 耦合、无封装、无 gate 语义。**本 spec 扩展 v2 §5.3 枚举：新增 rejected 行**（人驳回 + 人写理由）；契约意图（run N 的失败必在 run N+1 上下文）对提案角色完整保持。
- 2026-11-14 用户拍板三条：
  1. **驳回段**：`rejected` 行进回读。现状不对称：`failed`（系统否决）进回读，`rejected`（人否决）不进——「理由必填」这个信任升级数据源 richest 的偏好信号哪儿都没去。
  2. **gate**：回读 = 提案角色反馈环。段 3（消费信号）现状全局无 alias 过滤，泄漏进非提案角色（sentinel/narrator/notes-organizer）——它们无 propose 工具，收到无法行动的信号。用户选 A：整块 gate 在 propose 工具。
  3. **封装**：「升级为 reminder 和 gate 的封装，不要耦合在 runner 中」——回读 = `SystemReminder` 子类 + gate 一等公民，runner 只声明。
- 已核实事实：
  - 段 1/2 是 alias 过滤 + 空则静默 → 非提案角色**结构性**永远看不到。真正的泄漏只有段 3。
  - `recall_feedback_stats` 工具（Lane D，D23 拆分）已暴露 ungrounded（top 5 + 次数）；**唯一真实缺口 = config.yaml sentinel 白名单缺该工具**（sentinel.md 输入节 L13 与 config.example.yaml 示例段 L152 均已就位）。
  - co-click 对只在回读段 3（两个 stats 工具都没有）；数据面 `recall.click_stats()["co_click"]` 就绪。
  - `reject_reason` 列 failed/rejected 共用；两条驳回渠道（UI 端点 reason 必填 / 对话 `reject_proposal` reason 必填非空）都保证理由，回读格式仍保留空理由回退（防御）。`advance()` 条件 UPDATE 不动 `actor` → `actor == alias` 过滤对 rejected 行成立。
  - `BackgroundAgentConfig.tools`：LLM 角色 load-time 必填（None = 加载错误，config/bgagent.py fail-fast）；**但** run_once 自身把 `cfg.tools is None` 当作「全部工具」（L463-472），测试夹具绕过 loader 直接构造 → **gate 用有效工具面，不用 raw cfg.tools**（R3）。
  - `SystemReminder` 现有子类仅 `TimeSystemReminder`（render 恒非空）；生产 SystemReminder 消费者仅 Web 聊天路径（routes/agent.py，TimeSystemReminder）；`UserReminder` 契约已是 `render() -> str | None`。
  - 段头 `## 你的提案回读（未决积压与失败）` 是 pinned 测试锚点（test_bgagent_runner.py L651）——**段头变更 = 测试锚点变更**（M3）。
  - sentinel.md L39-40 / narrator.md L27-28 的回读节在 gate 后成死文本（S1/S2）；narrator 的「回读 = 唯一提案数据源」措辞改造前已部分失真（其段 1/2 结构性空，实际只收段 3 全局泄漏）。

## 2. 决策

| # | 决策 |
|---|---|
| R1 | **块级 gate = has_propose**：角色**有效工具面**无 `propose` → 整块不注入（render 返回 None）。效果：sentinel/narrator/notes-organizer 整块不见；consolidator/proofreader 见全块。 |
| R2 | **gate 一等公民**：`Gate = Callable[[RoleCtx], bool]`；每段 = `_Section(gate, build)`（build 返回 None = 无话可说）。当前三段同 has_propose；结构支持将来按段分化不动框架（结构性扩展，成本低；最小替代 = 块级单 gate + 三 builder，未采纳）。 |
| R3 | **封装 = `ReadbackSystemReminder`**：新模块 `server/bgagent/readback.py`，子类 `server.agent.reminders.SystemReminder`。**构造接收 run_once 的有效 tools 列表（filter_tools + delete_doc 硬摘之后），不用 raw cfg.tools**——gate 反映「角色实际能做什么」，与白名单检查真正同源；None 问题整体消失（run_once 已把 None 分支解析为全量注册表）。runner.py 删 `_readback_block` + `READBACK_TOP_N`（~120 行），无条件声明 `system_reminders=[ReadbackSystemReminder(cfg.alias, tools)]`——runner 零判断，gate 全在 reminder 内。 |
| R4 | **框架契约**：`SystemReminder.render() -> str \| None`（**None 或空 = 跳过注入**；对齐既有 UserReminder 契约）。`agent.py` 注入点：`body = r.render(); if body: append`（truthiness：None 与空串都跳过——现状空串会注入空 `<system-reminder></system-reminder>`，此为改进）。TimeSystemReminder 不受影响（恒非空）。 |
| R5 | **驳回段**（并入段 1「## 你的提案回读」）：alias 过滤、newest-first、top 5 + 截断标记，格式 `- 驳回 #id [type] relpath: 人写理由`（与 failed 段同形；空理由回退 `(no reason recorded)`）。段头文案更新为「未决积压 / 失败 / 驳回」（**= 测试锚点变更**，§5 同步）。 |
| R6 | **sentinel 工具面**：config.yaml sentinel `tools` 白名单加 `recall_feedback_stats`（唯一真实缺口；sentinel.md 输入节与 config.example.yaml 示例段已就位，核实过）。 |
| R7 | **co-click 对留在回读段 3**（D14 原用途 = 提案角色的 propose 证据）；不加进任何工具（YAGNI——sentinel 的比率级信号 recall_stats + recall_feedback_stats 已覆盖）。 |
| R8 | **已知代价**：proofreader（errata）也见段 3 消费信号（全局无 alias 可滤）——有界 ≤10 行，可接受；逃生阀 = per-role readback 配置 flag，现在不做。 |
| R9 | **时间块拼接保持现状**（run_once 手动字符串拼接）——out of scope，不动行为。 |

## 3. 设计

### 3.1 模块形状（server/bgagent/readback.py，新）

```python
@dataclass(frozen=True)
class RoleCtx:
    alias: str
    tools: frozenset[str]

Gate = Callable[[RoleCtx], bool]

@dataclass(frozen=True)
class _Section:
    gate: Gate
    build: Callable[[RoleCtx], list[str] | None]   # None = 无话可说

class ReadbackSystemReminder(SystemReminder):
    SECTIONS: tuple[_Section, ...] = (
        _Section(gate=has_propose, build=_build_proposal_section),    # 积压 + failed + rejected
        _Section(gate=has_propose, build=_build_relatedness_section),  # live 边 + ignored 配对
        _Section(gate=has_propose, build=_build_telemetry_section),    # co-click + ungrounded
    )

    def __init__(self, alias: str, tools: list):
        # tools = run_once 的有效工具列表（filter_tools + delete_doc 硬摘后），非 raw cfg.tools
        self._ctx = RoleCtx(alias, frozenset(t.name for t in tools))

    def render(self) -> str | None:
        lines: list[str] = []
        for s in self.SECTIONS:
            if s.gate(self._ctx):
                body = s.build(self._ctx)
                if body:
                    lines.extend(body)
        return "\n".join(lines) if lines else None
```

- `READBACK_TOP_N = 5` 移入本模块（top N + 截断标记，同现状）。
- 三段 builder = 现有 `_readback_block` 逻辑平移；段 1 增加 rejected 收集（`status == "rejected"`，同 `reject_reason` 列）。
- **降级语义（相对现状的变化，显式声明，S4）**：段 1/2 依赖 ledger conn、段 3 依赖 index conn，**互不牵连**——现状 ledger conn None / scan 异常 → 整块静默（含段 3，尽管段 3 用独立 index conn，属过度耦合副作用）；新语义 ledger 挂 → 仅段 1/2 静默，段 3 照常渲染（index 可用时）。段级异常 → warning log + 该段 None（同现状）。

### 3.2 段 1 格式（改后）

```
## 你的提案回读（未决积压 / 失败 / 驳回）      ← 段头 = 测试锚点，变更时同步断言
- 未决 N 条（states；类型：types）
- 失败 #id [type] relpath: reason            ← top 5 + 「…另有 N 条失败未列出」
- 驳回 #id [type] relpath: 人写理由           ← 新，top 5 + 「…另有 N 条驳回未列出」
```

### 3.3 框架契约（server/agent/reminders.py + agent.py）

- `SystemReminder.render() -> str | None`（ABC 签名 + docstring：**None 或空 = 跳过注入**）。
- `agent.py` 注入点（现 L28）：`body = r.render(); if body: messages.append({"role": "system", "content": f"<system-reminder>{body}</system-reminder>"})`。
- UserReminder 契约不动。
- 消息结构：bgagent 路径从 1 条 system 消息变为 2 条（prompt+时间块 / 回读）——与 Web 路径现状（system_prompt + TimeSystemReminder）同构；reminder 仅在 `if not messages:` 时渲染（agent.py L26-29），bgagent 每 run 新建 Agent → 每 run 注入，同现状；Web 会话恢复路径不重渲染，同现状 TimeSystemReminder，无回归。

### 3.4 runner.py 解耦

- 删 `_readback_block` / `READBACK_TOP_N`。
- `run_once`：`AgentSpec(system_prompt=cfg.prompt.read_text() + 时间块, system_reminders=[ReadbackSystemReminder(cfg.alias, tools)], ...)`——`tools` = 现有 L463-480 之后的有效列表（filter_tools + delete_doc 硬摘）。
- gate 谓词用有效工具面（与白名单检查同源，R3）。

### 3.5 sentinel / narrator prompt 对齐（死文本清理）

- **sentinel.md**：回读节（L39-40）改写——哨兵不持 propose，框架不注入回读（删节或改一句说明）；输入节 recall_feedback_stats 行已就位（L13），不补。
- **narrator.md**：回读节（L27-28）改写——narrator 不持 propose，回读不注入；「待你处理」数据源措辞对齐现实（提案数据实际来自 `recent_changes` 账本，见其 L13/L24）。
- config.yaml sentinel：`tools: [read_doc, search_docs, list_files, recent_changes, get_related_docs, recall_stats, recall_feedback_stats, write_doc]`。

## 4. 文件

| 文件 | 改动 |
|---|---|
| `server/bgagent/readback.py` | 新：RoleCtx / Gate / _Section / ReadbackSystemReminder / 三段 builder / READBACK_TOP_N |
| `server/agent/reminders.py` | SystemReminder.render 契约 → `str \| None`（None 或空 = 跳过） |
| `server/agent/agent.py` | 注入点跳过 None/空 |
| `server/bgagent/runner.py` | 删 _readback_block + READBACK_TOP_N；声明 reminder（有效 tools） |
| `config.yaml` | sentinel tools 加 recall_feedback_stats |
| `server/prompts/sentinel.md` | 回读节改写（死文本清理） |
| `server/prompts/narrator.md` | 回读节改写（死文本清理 +「待你处理」数据源对齐） |
| `tests/test_readback.py` | 新：单测 |
| `tests/test_bgagent_runner.py` | 重写 4 个回读测试（断言对象 = reminder 消息 + 消息结构锚点；`_cfg` 夹具按 R3 传 tools） |
| `tests/test_relatedness.py` | `test_readback_relatedness_section` 迁新模块 API（构造 ReadbackSystemReminder 调 render()） |
| `tests/test_relatedness_rank.py` | `test_ignored_pair_repropose_failed_both_directions` 同上 |
| `tests/test_agent.py` | None/空 跳过用例 |

## 5. 测试

**新 tests/test_readback.py**（单测，直接构造 ReadbackSystemReminder）：
- gate：有效面无 propose → render() 是 None；有 propose → 全块
- 驳回段：alias 过滤（只收自己 actor 的 rejected 行）/ newest-first / top 5 + 标记 / 理由格式 / 空理由回退
- 段 2 回归：live 边 + ignored 配对（alias 过滤）
- 段 3 回归：co-click + ungrounded（全局）
- 静默：全无数据 → None
- **降级（S4）**：ledger conn None + index 有数据 → 仅段 3 渲染

**tests/test_bgagent_runner.py**（随注入点 + API 重写，非「保持绿」）：
- 4 个回读测试：断言对象从 `spec.system_prompt` 改为 reminder 消息（`agent.messages` 中 `<system-reminder>` 包裹的回读）；顺序锚点改为消息列表结构（system_prompt 含时间块 → reminder 消息含回读）
- `_cfg` 夹具：按 R3 传有效 tools（否则 gate 崩 + `frozenset(None)` TypeError，波及该文件全部 run_once 测试）
- 段头文案断言同步 §3.2 新锚点
- **新增集成**：非提案角色（sentinel 视角 tools 无 propose）run_once → messages 无回读内容；rejected 行端到端（含人写理由可见）

**tests/test_relatedness.py / tests/test_relatedness_rank.py**：`_readback_block` 直接调用迁新模块 API（构造 `ReadbackSystemReminder` 调 `render()`），断言语义不变。

**tests/test_agent.py**：reminder render() → None 或空 → 无 `<system-reminder>` 消息；TimeSystemReminder 照常注入。

**基线**：pytest 1018 passed / 5 failed（5 个基线失败不修不计）。

## 6. 验收

1. `grep _readback_block server/bgagent/runner.py` → 无输出（逻辑整体移出）。
2. 非提案角色（sentinel）run_once → messages 无回读内容；提案角色（consolidator）→ 三段全在。
3. rejected 行（含人写理由）在回读可见，格式符合 §3.2。
4. pytest：基线 + 新测试全绿。
5. sentinel 可调 recall_feedback_stats（config.yaml 白名单就位）。
6. sentinel.md / narrator.md 无回读死文本（gate 后引用不存在的行为已清理）。

## 7. Out of scope

- 时间块 → reminder 迁移（R9）。
- per-role readback 配置 flag（R8 逃生阀，YAGNI）。
- co-click 进 recall_stats（R7）。
- consolidator/proofreader prompt 补「驳回 = 人否决」说明（段头自描述已够，可选）。
- schema / 状态机 / executor / 其他 prompt。
