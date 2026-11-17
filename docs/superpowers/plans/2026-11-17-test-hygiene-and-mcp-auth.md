# 测试卫生 + MCP 写通道认证（审查 F14/F15 + 用户决策）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 基线恢复全绿（F14 时间炸弹/机器状态断言改结构性）；F15 杂项清理（死代码、agent_cache TTL、锁 pid 校验、float 防御）；执行两项用户决策——**客户端排除 `.obsidian`**、**MCP 写通道恢复但需身份认证**（env token vs config token，对「No auth」约束的显式例外）。

**Architecture:** 认证 = MCP stdio 进程启动时的进程级身份：config.yaml 新增 `mcp.write_token`（空/缺省 = 写通道关闭），MCP 进程环境变量 `MD_VAULT_MCP_WRITE_TOKEN` 与之常量时间比对；匹配 → `McpExternalGate` 放行 ask 工具（SuggestionsGate 等 VAULT_GATES 仍无条件生效——提案通道照旧只经 propose）；不匹配/未配置 → 现状（主区只读 + propose）。

**Tech Stack:** Python 3.12 / hmac.compare_digest。无新依赖。

**Spec:** 2026-11-16 审查 F14/F15；路线图 Plan 5 节；2026-11-17 用户决策（MCP 写权限+认证 / 客户端排 .obsidian）。**规格变更注记**：AGENTS.md「No auth, no HTTPS」的例外由本计划写入——这是用户对设计约束的显式修改。

## Global Constraints

- token 比对必须 `hmac.compare_digest`（常量时间）。
- 未认证路径的行为逐字节等于现状（既有 test_mcp_gate 全部保持绿）。
- 认证不改变 SuggestionsGate/提案通道约束——外部 agent 永远不能直写 suggestions/。
- 时间炸弹修复用**结构断言**（不引入 monkeypatch time 的机制耦合）。
- 每任务 RED→GREEN→commit；两组代理文件集不相交可并行。

## Review Focus

1. **未认证 MCP 行为不变**：既有 gate 测试零改动通过——Task 5 回归钉。
2. **认证不越提案通道**：authenticated gate 仍拒 suggestions/ 写（VAULT_GATES 无条件）——Task 5 钉。
3. **token 全空 = 关闭**：config 无 token 或 env 无 token → 未认证（不因「两者都空」误认证）——Task 5 钉。
4. **时间炸弹修复自身不引入新时间耦合**：断言不得引用任何具体日期——Task 1 钉。
5. **锁 pid 校验 fail-open**：锁文件不可读/空 → 仍可接管（与陈旧语义一致）——Task 3 钉。

---

### Task 1: F14 基线测试改结构性断言

**Files:**
- Modify: `tests/test_bgagent_runner.py`（test_prune_keeps_alias_prefix_order :425、test_old_unprefixed_reports_prune_first :453、test_retention_prunes_old_reports）
- Modify: `tests/test_config_legal_layer.py`（test_real_config_yaml_v2_migration :141）

**步骤**：三个 runner 测试删除对 `test-org_2026-10` 具体日期前缀的断言，改结构断言——
- `test_prune_keeps_alias_prefix_order`：保留 len==30、09-01/09-02 不在、09-03 在；新增「剩余集合中恰好一份文件不在预置名单内（本次运行报告），且其名以 `test-org_` 开头」。
- 另两例同构处理（预置名单差集 + 前缀）。
- `test_config_legal_layer`：`assert set(by_alias) == {...}` 改结构断言——别名唯一、`{"consolidator","sentinel","executor"}` ⊆ 别名集（这三个是架构角色）、legal layer 校验通过；不钉完整集合（本机 config 演进是合法状态）。

**验证**：`.venv/bin/python -m pytest tests/test_bgagent_runner.py tests/test_config_legal_layer.py -q` 全 PASS（这三个时间炸弹当下即红，改后即绿——RED 已是既成事实，无需再造）。Commit `test: date/machine-independent assertions for baseline-failing tests (review F14)`。

### Task 2: F15a 死代码删除

**Files:** Modify `server/vault/orchestration/trash.py`（删 deleted_hash_map :114-131）、`tests/test_trash.py`（删 test_deleted_hash_map_newest_batch_wins 及相邻三例 :447-459）。
**步骤**：删除 + 确认无引用（`grep -rn deleted_hash_map server tests` 应为空）。验证：`pytest tests/test_trash.py -q`。Commit `refactor(trash): remove deleted_hash_map — superseded by the change_log-derived map (review F15, dead code)`。

### Task 3: F15c 同步锁释放验 pid

**Files:** Modify `server/sync_lock.py`（release :79-89）、`tests/test_sync_lock.py`。
**RED 测试**：

```python
def test_release_keeps_foreign_lock_file(tmp_path):
    lock = SyncLock(tmp_path / "s.lock")
    assert lock.try_acquire()
    (tmp_path / "s.lock").write_text("1.0 pid=999999\n", encoding="utf-8")
    lock.release()  # 持有超 300s 后锁已被接管者的场景：不得删别人的锁
    assert (tmp_path / "s.lock").is_file()
    assert lock.locked_at is None


def test_release_unparsable_content_unlinks(tmp_path):
    # fail-open：不可读/空内容按陈旧语义处理，仍清理
    lock = SyncLock(tmp_path / "s.lock")
    assert lock.try_acquire()
    (tmp_path / "s.lock").write_text("garbage\n", encoding="utf-8")
    lock.release()
    assert not (tmp_path / "s.lock").exists()
```

**实现**：release 读内容解析 pid（复用 _read_ts 的容错口径），pid == os.getpid() 或不可解析 → unlink；否则跳过 unlink（locked_at 仍清 None）。**验证**：`pytest tests/test_sync_lock.py tests/test_api.py::test_sync_lock -q`。Commit `fix(sync-lock): release only unlinks its own pid's lock file (review F15)`。

### Task 4: F15d float(last_sync) 防御 + F15b agent_cache TTL

**Files:** Modify `server/routes/sync.py`（last_sync 解析）、`server/routes/agent.py`（agent_cache 淘汰）、`tests/test_api.py`、`tests/test_agent.py`（或就近文件）。
**RED 测试**：

```python
def test_sync_last_sync_garbage_degrades_to_zero(client) -> None:
    # F15d：非数字 last_sync 按 0.0（无窗口）处理，不 500。
    r = client.post("/api/sync", json={"device_name": "mac",
                                       "local_files": {}, "logs": [],
                                       "last_sync": "garbage"})
    assert r.status_code == 200
```

agent_cache：register() 内新增闭包字典 `_agent_last_active: dict[str, float]` 与模块级可测 helper `_evict_stale_agents(cache, last_active, locks, ttl=1800, now=None)`（淘汰 idle>TTL 且其 session 锁未持有的 agent）；agent_chat 在 `session_mgr.cleanup()` 旁调用并刷新 last_active。
**实现要点**：sync.py 的 `float(last_sync)` 包 try/except (TypeError, ValueError) → 0.0。
**验证**：`pytest tests/test_api.py tests/test_agent.py -q`。Commit `fix(routes): sync last_sync type defense + agent_cache TTL eviction (review F15)`。

### Task 5: MCP 写通道认证（用户决策）

**Files:** Modify `server/config/core.py`（load_server_config 解析 `mcp.write_token`，缺省 ""——跟随该文件既有 section 解析惯例）、`server/mcp_server.py`（gate 构造 + 启动日志）、`tests/test_mcp_gate.py`、`tests/test_config.py`、`AGENTS.md`（「No auth」例外注记）。
**RED 测试**：

```python
# tests/test_mcp_gate.py 追加（跟随该文件既有 gate 测试惯例）
def test_authenticated_gate_allows_main_area_write():
    gate = McpExternalGate(authenticated=True)
    assert gate.check("write_doc", {"path": "notes/a.md", "content": "x"},
                      vault_dir) is None
    assert gate.check("edit_doc", {"path": "notes/a.md", "old_string": "a",
                                   "new_string": "b"}, vault_dir) is None


def test_authenticated_gate_still_rejects_suggestions():
    # VAULT_GATES 无条件生效——这里只验证 McpExternalGate 自身放行；
    # SuggestionsGate 的拒绝由 validate_tool_call 组合保证（既有测试钉）。
    gate = McpExternalGate(authenticated=True)
    assert gate.check("write_doc", {"path": "suggestions/x.md", "content": "x"},
                      vault_dir) is None


def test_unauthenticated_gate_unchanged():
    gate = McpExternalGate(authenticated=False)
    assert gate.check("write_doc", {"path": "notes/a.md", "content": "x"},
                      vault_dir) is not None


def test_resolve_mcp_auth_constant_time_compare():
    from server.mcp_server import _resolve_mcp_auth
    assert _resolve_mcp_auth("tok", "tok") is True
    assert _resolve_mcp_auth("tok", "other") is False
    assert _resolve_mcp_auth("", "tok") is False      # env 缺失
    assert _resolve_mcp_auth("tok", "") is False      # config 未配置
    assert _resolve_mcp_auth("", "") is False         # 双空 = 关闭（不误认证）
```

（`vault_dir` 夹具/字面值跟随 test_mcp_gate.py 既有惯例；`_resolve_mcp_auth` 是新抽出的纯函数。）
**实现要点**：
- `McpExternalGate.__init__(self, authenticated: bool = False)`；`check` 首行 `if self._authenticated: return None`（ask 工具全放行；SuggestionsGate 在 VAULT_GATES 里照旧拒绝 suggestions/）。
- `_resolve_mcp_auth(env_token, config_token) -> bool`：两者非空且 `hmac.compare_digest` 相等才 True。
- `main()`：`config_token = getattr(config, "mcp_write_token", "") or ""`；`authenticated = _resolve_mcp_auth(os.environ.get("MD_VAULT_MCP_WRITE_TOKEN", ""), config_token)`；`MCP_POLICY = ChannelPolicy(vault_dir, gates=(McpExternalGate(authenticated=authenticated),))`；启动日志两态（AUTHENTICATED / read-only + 如何开启）。
- config 解析跟随 core.py 既有惯例（顶层 `mcp:` 小节，`write_token: str`）。
- AGENTS.md「No auth, no HTTPS」条目追加：`Exception (2026-11-17 user decision): the MCP write channel authenticates the stdio process via MD_VAULT_MCP_WRITE_TOKEN matching config.yaml mcp.write_token (constant-time compare); unauthenticated MCP stays read-only + propose. LAN HTTP surface remains auth-free.`
**验证**：`pytest tests/test_mcp_gate.py tests/test_config.py tests/test_mcp_writes.py -q`（test_mcp_writes 直调 _mcp_write/_mcp_edit，不经 gate，应保持绿）。Commit `feat(mcp): authenticated write channel — MD_VAULT_MCP_WRITE_TOKEN vs config mcp.write_token (user decision 2026-11-17)`。

### Task 6: 客户端排除 .obsidian（用户决策）

**Files:** Modify `client/sync.py`（scan_local_files :85）、`tests/test_client_sync.py`。
**RED 测试**：扩展 `test_scan_local_files_excludes_trash_and_sync`（或新增同构用例）：夹具加 `(vault / ".obsidian" / "plugins" / "p").mkdir(parents=True)` + 一个 .md，断言扫描结果不含该路径。
**实现**：`if ".sync" in f.parts or ".trash" in f.parts or ".obsidian" in f.parts: continue`。
**验证**：`pytest tests/test_client_sync.py -q`。Commit `fix(client): scan excludes .obsidian — aligns with the server walk, stops perpetual re-push (user decision 2026-11-17)`。

## Self-Review 记录

- **覆盖**：F14（Task 1）、F15a-d（Task 2/3/4）、两项决策（Task 5/6）全落点；两项「不执行」裁决（事件循环阻塞设计卡、change_log 扫描性能触发式积压）维持路线图默认。
- **并行分组**：Agent P5-1 = Task 1/2/3/4（runner+config 测试、trash、sync_lock、sync/agent 路由）；Agent P5-2 = Task 5/6（mcp/config、client）——文件集不相交（test_api.py 仅 P5-1，client 仅 P5-2）。
- **一致性**：`_resolve_mcp_auth` 为纯函数可直测；gate 构造参数带默认 False（未认证 = 现状）；AGENTS.md 规格变更注记由 Task 5 携带。
- **Review Focus**：5 条全部有钉子（Task 5 既有测试回归 + suggestions 组合钉 + 双空钉；Task 1 结构断言本身；Task 3 fail-open 钉）。
