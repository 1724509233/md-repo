# 路径守卫盘符洞 + 降级检索扫描面（审查 F5/F13）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 关闭审查仅剩的 P1——Windows 盘符绝对路径（`C:/…`）绕过 `safe_rel_path` 与 `_check_md_path`，使 web 路由在 Windows 服务端可读写 vault 之外任意路径；顺带补齐两个扫描面缺失的 `.trash`/`.obsidian` 排除（F13：降级检索可搜出已删文档；list_files 漏 `.obsidian`）。

**Architecture:** 三处守卫逐层补齐——`safe_rel_path`（web 路由唯一闸）在反斜杠归一后用锚定正则拒绝 `^[A-Za-z]:($|/)`（POSIX/Windows 通用，纯字符串判定）；agent 工具的 `_check_md_path`/`_check_optional_path` 加同一守卫（L2 policy 已挡盘符，此处是报错语义更友好的双层防线）；`_legacy_rag_search` 与 `list_files` 的 rglob 补 `.trash`/`.obsidian` parts 检查（AGENTS.md 扫描面规则）。

**Tech Stack:** Python 3.12 / pathlib / 正则。无新依赖。

**Spec:** 2026-11-16 全量审查报告 F5/F13（证据：`server/vault/fs/vault_ops.py:15-32`、`server/agent/tools/tool.py:48-67`、`server/ai/search.py:114-116`、`server/agent/tools/list_files.py:76-79`）；AGENTS.md「扫描面规则」条目（新扫描面必须加 `".trash" in parts` 检查）。基于 F5 的 Windows 实测在 macOS 上不可行，计划按 pathlib 语义推导 + 跨平台纯字符串判定 + POSIX 可跑的测试钉住。

## Global Constraints

- 测试夹具写样例文件一律 `write_bytes`（Windows 换行翻译破坏哈希断言）。
- 不新增路由（`tests/test_routes_order.py` 钉住路由表）；不引入鉴权/构建步骤。
- 扫描面排除遵循 AGENTS.md 既有惯例：`".trash" in f.parts` 全路径段匹配（不止首段）。
- 守卫报错语义保持两层各自现状：`safe_rel_path` 返回 None（路由层 400），`_check_md_path` 抛 ValueError（模型可读错误串）。
- 每任务 RED→GREEN→commit。

## Review Focus

1. **盘符大小写与无斜杠变体**（`c:/x.md`、`C:`、`C:` 后接反斜杠）：期望一律拒绝——Task 1 测试钉。
2. **路径中间的冒号**（`notes/c:x.md`、`my-c:.md`）：期望保持允许（仅拒绝**起始**盘符段）——Task 1 测试钉。
3. **降级检索**（index.db 缺失/不可读时的 TF 兜底）下回收站与 `.obsidian` 插件文档存在：期望搜索结果零泄漏——Task 2 测试钉。
4. **两层守卫的一致性**：`_check_md_path`（L1）与 policy L2 对盘符路径都拒绝，报错串仍属"outside vault"语义族，不与 SuggestionsGate/边界检查的措辞冲突——Task 1 测试钉。
5. **守卫加严不误伤既有合法路径**：全量既有测试面（upload/manage/trash/agent 工具）零回归——Task 3 全量回归步骤钉。

---

### Task 1: `safe_rel_path` + `_check_md_path` 盘符守卫（F5）

**Files:**
- Modify: `server/vault/fs/vault_ops.py:15-32`（`safe_rel_path`；文件头需补 `import re`）
- Modify: `server/agent/tools/tool.py:48-67`（`_check_md_path` / `_check_optional_path`；文件头需补 `import re`）
- Test: `tests/test_trash.py`（safe_rel_path 测试块在 :357 起）+ `tests/test_agent.py`（追加 `_check_md_path` 盘符用例；若文件内已有验证器测试段则放同段）

**Interfaces:**
- Consumes: 无。
- Produces: `safe_rel_path(raw) -> Path | None`——新增拒绝条件「反斜杠归一后匹配 `^[A-Za-z]:($|/)`」（UNC 前导 `//` 已被既有 `startswith("/")` 覆盖）；`_check_md_path(v)` / `_check_optional_path(v)` 对同一模式抛 `ValueError("path outside vault: …")`。所有 web 路由与 agent 工具调用点签名不变。

- [ ] **Step 1: 写失败测试**

`tests/test_trash.py` 的 safe_rel_path 段（:357 起）追加：

```python
def test_safe_rel_path_rejects_drive_absolute_paths():
    # F5：Windows 盘符绝对路径在 Windows 上会覆盖 join 基逃出 vault。
    # 正则为纯字符串判定，POSIX 上同样可测。
    for bad in ("C:/Users/x/evil.md", "c:/notes/a.md", "C:\\x.md", "C:", "z:/a/b.md"):
        assert safe_rel_path(bad) is None, bad
    # 中间冒号不误伤：首段非盘符段即合法。
    assert safe_rel_path("notes/c:x.md") is not None
    assert safe_rel_path("my-c:.md") is not None
```

`tests/test_agent.py` 追加（放在现有 `_check_md_path` 相关用例旁；无则文件末尾）：

```python
def test_check_md_path_rejects_drive_absolute():
    from server.agent.tools.tool import _check_md_path, _check_optional_path

    for bad in ("C:/Users/x/evil.md", "c:\\x.md", "C:"):
        with pytest.raises(ValueError, match="outside vault"):
            _check_md_path(bad)
        with pytest.raises(ValueError, match="outside vault"):
            _check_optional_path(bad)
    assert _check_md_path("notes/c:x.md") == "notes/c:x.md"
```

（`tests/test_agent.py` 若未导入 `pytest` 则补 import。）

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_trash.py::test_safe_rel_path_rejects_drive_absolute_paths tests/test_agent.py::test_check_md_path_rejects_drive_absolute -v`
Expected: FAIL——`assert Path('C:/Users/x/evil.md') is None`（守卫放行了盘符路径）；agent 用例 `DID NOT RAISE`

- [ ] **Step 3: 实现**

`server/vault/fs/vault_ops.py`——文件头补 `import re`；`safe_rel_path` 在 `raw.replace("\\", "/")` 与 `startswith("/")` 检查之间插入：

```python
    if re.match(r"^[A-Za-z]:($|/)", raw):
        return None  # Windows drive-absolute: escapes the vault join on win32
```

`server/agent/tools/tool.py`——文件头补 `import re`；`_check_md_path` 在 `..` 检查后、`.md` 检查前插入：

```python
    if re.match(r"^[A-Za-z]:($|/)", v.replace("\\", "/")):
        raise ValueError(f"path outside vault: '{v}'")
```

`_check_optional_path` 在 `..` 检查后插入同款（措辞一致）。

- [ ] **Step 4: 跑测试确认通过 + 守卫面回归**

Run: `.venv/bin/python -m pytest tests/test_trash.py tests/test_agent.py tests/test_api.py -q`
Expected: 全部 PASS（web 路由 400 化的盘符路径、agent 验证器、既有 safe_rel_path 用例互不干扰）

- [ ] **Step 5: Commit**

```bash
git add server/vault/fs/vault_ops.py server/agent/tools/tool.py tests/test_trash.py tests/test_agent.py
git commit -m "fix(guard): reject Windows drive-absolute paths in safe_rel_path and agent path validators (review F5)"
```

### Task 2: web 边界端到端钉子（盘符路径 → 400）

**Files:**
- Test: `tests/test_api.py`（追加到 safe_rel_path 相关测试附近，:23 `test_safe_rel_path_alias_is_shared_vault_guard` 之后）

**Interfaces:**
- Consumes: Task 1 的 `safe_rel_path` 行为。
- Produces: 无（纯测试钉子——把守卫钉在 HTTP 边界，防止未来有人绕过 `_safe_rel_path` 直写）。

- [ ] **Step 1: 写测试**（本测试在 Task 1 落地后即绿，属边界回归钉；若想先看红，可在 Task 1 的 commit 前运行确认 200/201 而非 400）

```python
def test_upload_and_get_reject_drive_absolute_path(client) -> None:
    # F5 端到端：盘符路径在 web 边界必须 400，绝不落盘、绝不可读。
    r = client.post("/api/docs/C:/evil.md", files={"file": ("evil.md", b"pwn", "text/markdown")})
    assert r.status_code == 400
    r = client.get("/api/docs/C:/evil.md")
    assert r.status_code == 400
```

- [ ] **Step 2: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_api.py -k drive -v`
Expected: PASS（Task 1 已落地；此测试的价值是把守卫钉在路由边界，防止守卫被绕过或移除而无测试报警）

- [ ] **Step 3: Commit**

```bash
git add tests/test_api.py
git commit -m "test(api): pin drive-absolute rejection at the web boundary (review F5)"
```

### Task 3: 降级检索与 list_files 扫描面补齐（F13）

**Files:**
- Modify: `server/ai/search.py:114-116`（`_legacy_rag_search` 的 parts 检查补 `.trash`/`.obsidian`）
- Modify: `server/agent/tools/list_files.py:76-79`（parts 检查补 `.obsidian`）
- Test: `tests/test_ai.py`（追加）+ `tests/test_list_files_case.py`（追加）

**Interfaces:**
- Consumes: 无。
- Produces: `_legacy_rag_search` 与 `ListFilesTool` 的扫描面排除集合与其余 vault 扫描面一致（`.sync`/`.obsidian`/`.trash` 全段匹配）。

- [ ] **Step 1: 写失败测试**

`tests/test_ai.py` 追加：

```python
def test_legacy_rag_search_excludes_trash_and_obsidian(tmp_path):
    # F13：降级检索（index.db 缺失时的 TF 兜底）不得泄漏回收站与 .obsidian。
    from server.ai.search import _legacy_rag_search

    vault = tmp_path / "vault"
    (vault / "notes").mkdir(parents=True)
    (vault / "notes" / "live.md").write_bytes(b"quokka live content")
    batch = vault / ".trash" / "20261117T000000Z-deadbeef" / "notes"
    batch.mkdir(parents=True)
    (batch / "leaked.md").write_bytes(b"quokka deleted content")
    (vault / ".obsidian" / "plugins" / "p").mkdir(parents=True)
    (vault / ".obsidian" / "plugins" / "p" / "README.md").write_bytes(b"quokka plugin doc")

    hits = _legacy_rag_search(str(vault), "quokka", top_k=10)
    paths = [h["path"] for h in hits]
    assert hits, "sanity: live content must match"
    assert all(not p.startswith(".trash/") for p in paths)
    assert all(not p.startswith(".obsidian/") for p in paths)
    assert "notes/live.md" in paths
```

`tests/test_list_files_case.py` 追加（沿用该文件既有的夹具与调用惯例；核心断言如下）：

```python
def test_list_files_excludes_obsidian(vault_dir):
    # list_files 与 _iter_vault_md 的排除集合对齐（.sync/.trash 已排除，补 .obsidian）。
    (vault_dir / ".obsidian" / "plugins" / "p").mkdir(parents=True, exist_ok=True)
    (vault_dir / ".obsidian" / "plugins" / "p" / "README.md").write_bytes(b"plugin doc")
    from server.agent.tools.list_files import ListFilesTool
    from server.agent.tools.tool import ToolContext

    out = ListFilesTool()._execute(
        __import__("types").SimpleNamespace(dir=""),
        ToolContext(vault_dir=vault_dir),
    )
    assert ".obsidian/plugins/p/README.md" not in out.splitlines()
```

（若 `tests/test_list_files_case.py` 已有构造 ToolContext/args 的现成惯例，按该惯例改写夹具构造，断言保持不变。）

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_ai.py -k legacy tests/test_list_files_case.py -q`
Expected: FAIL——`_legacy_rag_search` 命中 `.trash/.../leaked.md`；list_files 输出含 `.obsidian/...`

- [ ] **Step 3: 实现**

`server/ai/search.py:114`：`if ".sync" in md_file.parts:` → `if ".sync" in md_file.parts or ".obsidian" in md_file.parts or ".trash" in md_file.parts: continue`

`server/agent/tools/list_files.py:76-79`：过滤器补 `and ".obsidian" not in f.parts`。

- [ ] **Step 4: 跑测试确认通过 + 扫描面回归**

Run: `.venv/bin/python -m pytest tests/test_ai.py tests/test_list_files_case.py tests/test_agent.py tests/test_api.py -q`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add server/ai/search.py server/agent/tools/list_files.py tests/test_ai.py tests/test_list_files_case.py
git commit -m "fix(scan): legacy rag fallback + list_files exclude .trash/.obsidian (review F13)"
```

### Task 4: 全量回归

- [ ] **Step 1**: Run `.venv/bin/python -m pytest tests/ -q` — Expected: 仅 4 个既有基线失败（3 时间炸弹 + 1 机器状态，审查 F14，见路线图 Plan 5），零新增失败。
- [ ] **Step 2**: Commit（如有遗漏文件）并推送分支。

## Self-Review 记录

- **覆盖**：F5（Task 1/2 守卫 + 边界钉）、F13 两半（Task 3 降级检索 + list_files）全部落点；list_files 的 `.obsidian` 缺口在审查 F13/F15 中均被点名，此处一并收口。
- **Step scan**：每步单动作；实现步骤仅给插入点与精确模式，其余由测试唯一确定。
- **一致性**：两处守卫用同一锚定正则 `^[A-Za-z]:($|/)`；测试断言与报错语义（None / ValueError "outside vault"）与既有约定一致。
- **Review Focus**：5 条全部有对应钉子（Task 1 大小写/无斜杠/中间冒号、Task 2 边界、Task 3 降级泄漏、Task 4 全量回归）。
