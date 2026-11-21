"""Task 4 (security redlines): model-surface filters — search / related / list /
recent + degraded RAG fallback + write/edit reference advisory (spec §2.3/§2.6).

Pins:
- rag_search / segments_search privacy_dirs zero-hit (SQL path, ESCAPE LIKE);
- degraded fallback (_legacy_rag_search, index.db renamed away) same zero-hit
  (Review Focus 2);
- search 连接收口 (2026-11-21 P0): rag_search releases its own index.db
  handle deterministically — rename without gc.collect(), 修复前必红;
- case pins on BOTH paths (名单 DIARY vs 路径 diary/a.md — Review Focus 3);
- graph_get_related: privacy neighbor not recorded AND not a BFS frontier
  (no indirect leak through a privacy middle node);
- graph_get_related_with_machine: user + machine layers both clean;
- list_files / recent_changes tool outputs carry no privacy paths;
  recent_changes success stamps last_meta["paths"] (path + from_path);
- MCP get_related_docs handler validates before serving (source wiring pin
  + policy behavior pin);
- write_doc/edit_doc advisory: non-privacy target + privacy refs → warning
  tail, privacy-zone target → no warning, both saves succeed;
- privacy_dirs=() / ctx.privacy=None everywhere → byte-equivalent behavior.
"""
from __future__ import annotations

import gc
import hashlib
import inspect
import time
from pathlib import Path

import pytest

from server.agent.tools.edit_doc import EditDocTool
from server.agent.tools.get_related_docs import GetRelatedDocsTool
from server.agent.tools.list_files import ListFilesTool
from server.agent.tools.recent_changes import RecentChangesTool
from server.agent.tools.tool import ToolContext
from server.agent.tools.write_doc import WriteDocTool
from server.ai.search import rag_search
from server.core.privacy import INVISIBLE_MSG, PrivacyConfig, privacy_clause
from server.vault.db.graph_sql import (
    graph_add_edge,
    graph_get_related,
    graph_get_related_with_machine,
    graph_upsert_entity,
)


def _write(vault: Path, rel: str, content: str) -> None:
    """Binary write (Windows-safe hash convention per AGENTS.md gotcha)."""
    fp = vault / rel
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_bytes(content.encode("utf-8"))


def _pcfg(tmp_path: Path, dirs_yaml: str) -> PrivacyConfig:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"server:\n  host: localhost\nprivacy:\n  dirs: {dirs_yaml}\n",
                   encoding="utf-8")
    return PrivacyConfig(cfg)


def _backup_index_db(vault: Path, db_conn) -> None:
    """Move index.db out of the way to force the degraded fallback.

    Windows refuses to rename a file with an open handle (WinError 32):
    ① close the fixture's SQLite connection first (harmless on POSIX; the
    db_conn teardown re-close is a no-op) and ② gc.collect() — the search
    adapter's own connection opened by the preceding rag_search call is
    only released when GC collects its reference cycle (refcount alone
    never drops it to zero). POSIX renames over open handles, so this pin
    needs both only on Windows.
    """
    db = vault / ".sync" / "index.db"
    db_conn.close()
    gc.collect()
    db.rename(db.with_suffix(".db.bak"))


@pytest.fixture
def search_vault(vault_dir, db_conn):
    """Two-file vault: 日记/a.md (zebrafish) + notes/b.md — indexed
    explicitly here (案 A: rag_search no longer lazily rebuilds an empty
    index; the fixture drives the canonical ensure_indexed entry)."""
    from server.vault.orchestration.indexer import ensure_indexed

    _write(vault_dir, "日记/a.md", "# 秘密\n\nzebrafish habitat notes\n")
    _write(vault_dir, "notes/b.md", "# Public\n\nzebrafish zoo plan\n")
    ensure_indexed(db_conn, vault_dir)
    return vault_dir


# ── search surface (rag_search: SQL path + tag axis + legacy fallback) ─────

def test_rag_search_privacy_dirs_zero_hits(search_vault):
    hits = rag_search(str(search_vault), "zebrafish", privacy_dirs=("日记",))
    assert [h["path"] for h in hits] == ["notes/b.md"]
    # without dirs → both docs (conservation: web callers never pass)
    hits = rag_search(str(search_vault), "zebrafish")
    assert {h["path"] for h in hits} == {"日记/a.md", "notes/b.md"}


def test_rag_search_releases_index_db_handle(search_vault, db_conn):
    """连接收口回归钉（2026-11-21 卡 1 P0）：rag_search 返回后，搜索适配器
    自开的 index.db 连接必须已被 with closing 块**确定性地**关闭——无需
    gc.collect() 即可直接 rename index.db（修复前必红的行为锁：旧代码句柄
    只靠引用循环被 GC 回收，见 _backup_index_db 注释；Windows 上打开的
    文件 rename 即 PermissionError/WinError 32）。

    gc.disable() 钉的是红的必然性：CPython 的分代 GC 可能在测试中途顺带
    回收泄漏循环而让修复前的代码侥幸转绿。POSIX rename 对打开句柄恒成功，
    本测试在 mac 上天然绿，无需 skip。fixture 的 db_conn 也先关（它同样
    握着 index.db，但不是本钉对象——只差那半句 gc.collect()）。
    """
    rag_search(str(search_vault), "zebrafish")                       # SQL 主路早退
    rag_search(str(search_vault), "zebrafish", tags=["nosuchtag"])   # tag 空集 []（with 内）
    db_conn.close()
    gc.disable()
    try:
        db = search_vault / ".sync" / "index.db"
        db.rename(db.with_suffix(".db.bak"))
        assert db.with_suffix(".db.bak").is_file()
    finally:
        gc.enable()


def test_rag_search_sql_path_case_insensitive(search_vault):
    """大小写钉（SQL 路）：名单 DIARY vs 路径 diary/a.md 同判。"""
    _write(search_vault, "diary/c.md", "zebrafish diary english\n")
    hits = rag_search(str(search_vault), "zebrafish", privacy_dirs=("DIARY",))
    assert {h["path"] for h in hits} == {"日记/a.md", "notes/b.md"}


def test_rag_search_degraded_fallback_privacy(search_vault, db_conn):
    """降级钉（Review Focus 2）：index.db 改名逼进 _legacy_rag_search，
    带名单仍零命中；不传则命中。"""
    rag_search(str(search_vault), "zebrafish")  # ensure index built
    _backup_index_db(search_vault, db_conn)
    hits = rag_search(str(search_vault), "zebrafish", privacy_dirs=("日记",))
    assert [h["path"] for h in hits] == ["notes/b.md"]
    hits = rag_search(str(search_vault), "zebrafish")
    assert {h["path"] for h in hits} == {"日记/a.md", "notes/b.md"}


def test_legacy_fallback_case_insensitive(search_vault, db_conn):
    """大小写钉（legacy 路）：名单 DIARY vs diary/ 小写路径。"""
    _write(search_vault, "diary/c.md", "zebrafish diary english\n")
    _backup_index_db(search_vault, db_conn)
    hits = rag_search(str(search_vault), "zebrafish", privacy_dirs=("DIARY",))
    assert {h["path"] for h in hits} == {"日记/a.md", "notes/b.md"}


def test_segments_search_privacy_clause_injection(search_vault, db_conn):
    """注入面直测：含 %_ 的名单项不得当通配符吞掉普通文档。"""
    from server.vault.db.segments_sql import segments_search

    rag_search(str(search_vault), "zebrafish")  # build segments first
    # 名单 "a%b" 转义后是字面量，不应命中任何现存路径
    hits = segments_search(db_conn, "zebrafish", privacy_dirs=("a%b",))
    assert {h["path"] for h in hits} == {"日记/a.md", "notes/b.md"}
    # 生成的参数形态钉（与 test_privacy.py 同源）
    clause, params = privacy_clause(("a%b",), "s.path")
    assert params == ["a\\%b/%"] and "ESCAPE" in clause


# ── related surface (graph_get_related / with_machine) ─────────────────────

def _mk_edge(conn, src: str, dst: str, etype: str = "wikilink") -> None:
    a = graph_upsert_entity(conn, src, "doc")
    b = graph_upsert_entity(conn, dst, "doc")
    graph_add_edge(conn, a, b, etype)


def test_graph_get_related_privacy_not_recorded_not_frontier(vault_dir, db_conn):
    """BFS 前沿钉：A→日记/P→C —— 带名单时 P 不出现，经 P 的间接 C 也不
    出现（隐私中间节点不进前沿）；不带名单 C 在（hops=2）。"""
    _mk_edge(db_conn, "notes/a.md", "日记/secret.md")
    _mk_edge(db_conn, "日记/secret.md", "notes/c.md")
    _write(vault_dir, "notes/a.md", "a\n")

    plain = graph_get_related(db_conn, "notes/a.md", hops=2)
    assert {r["path"] for r in plain} == {"日记/secret.md", "notes/c.md"}

    filtered = graph_get_related(db_conn, "notes/a.md", hops=2,
                                 privacy_dirs=("日记",))
    assert filtered == []


def test_graph_get_related_with_machine_two_layers(vault_dir, db_conn, ledger_conn):
    """user 层（wikilink A→B）+ machine 层（relatedness A↔日记/mach）——
    带名单两层均无隐私路径；不带时 machine 层含隐私路径。"""
    from server.vault.db.relatedness import merge_edge

    _write(vault_dir, "notes/a.md", "a\n")
    _write(vault_dir, "notes/b.md", "b\n")
    _write(vault_dir, "日记/mach.md", "m\n")
    _mk_edge(db_conn, "notes/a.md", "notes/b.md")
    merge_edge(ledger_conn, "notes/a.md", "日记/mach.md", "test", "consolidator")

    plain = graph_get_related_with_machine(
        db_conn, ledger_conn, vault_dir, "notes/a.md", hops=1)
    assert any(r["path"] == "日记/mach.md" and r["layer"] == "machine"
               for r in plain)

    filtered = graph_get_related_with_machine(
        db_conn, ledger_conn, vault_dir, "notes/a.md", hops=1,
        privacy_dirs=("日记",))
    assert [r["path"] for r in filtered] == ["notes/b.md"]


def test_get_related_docs_tool_privacy(vault_dir, db_conn, ledger_conn, tmp_path):
    """工具层：ctx.privacy 带名单 → 结果串无隐私路径；None → 有（等价）。"""
    _write(vault_dir, "notes/a.md", "a\n")
    _mk_edge(db_conn, "notes/a.md", "日记/secret.md")
    ctx_plain = ToolContext(vault_dir=vault_dir, db_conn=db_conn, ledger_conn=ledger_conn)
    ctx_priv = ToolContext(vault_dir=vault_dir, privacy=_pcfg(tmp_path, "[日记]"), db_conn=db_conn, ledger_conn=ledger_conn)
    out_plain = GetRelatedDocsTool().call({"path": "notes/a.md"}, ctx_plain)
    assert "日记/secret.md" in out_plain
    out_priv = GetRelatedDocsTool().call({"path": "notes/a.md"}, ctx_priv)
    assert "日记/secret.md" not in out_priv


# ── list_files / recent_changes ────────────────────────────────────────────

def test_list_files_tool_privacy(vault_dir, tmp_path):
    _write(vault_dir, "日记/a.md", "secret\n")
    ctx_plain = ToolContext(vault_dir=vault_dir)
    ctx_priv = ToolContext(vault_dir=vault_dir, privacy=_pcfg(tmp_path, "[日记]"))
    out_plain = ListFilesTool().call({"dir": ""}, ctx_plain)
    assert "日记/a.md" in out_plain and "notes/test.md" in out_plain
    out_priv = ListFilesTool().call({"dir": ""}, ctx_priv)
    assert "日记/a.md" not in out_priv and "notes/test.md" in out_priv


def _mk_change(conn, path: str, from_path: str = "", op: str = "update") -> None:
    conn.execute(
        "INSERT INTO change_log (ts, source, op, path, old_path) "
        "VALUES (?, ?, ?, ?, ?)",
        (time.time(), "agent", op, path, from_path))
    conn.commit()


def test_recent_changes_tool_privacy_and_meta(vault_dir, db_conn, tmp_path):
    """path + from_path 双列过滤 + 成功时 last_meta["paths"]（path+from_path）。

    评审 P2b：过滤在 SQL 层（get_changes(privacy_dirs=…)），工具层不再
    post-filter——直接测 get_changes 的钉见下方 SQL 层用例；本用例继续钉
    工具端到端行为（名单生效 + meta 干净）与守恒（None → 原样）。"""
    _mk_change(db_conn, "日记/a.md")                    # path 隐私 → 剔
    _mk_change(db_conn, "notes/b.md", from_path="日记/old.md")  # from 隐私 → 剔
    _mk_change(db_conn, "notes/c.md")                   # 正常

    ctx_priv = ToolContext(vault_dir=vault_dir, privacy=_pcfg(tmp_path, "[日记]"), db_conn=db_conn)
    out = RecentChangesTool().call({}, ctx_priv)
    assert "日记" not in out
    assert "notes/c.md" in out
    assert ctx_priv.last_meta is not None
    assert ctx_priv.last_meta["paths"] == ["notes/c.md"]

    ctx_plain = ToolContext(vault_dir=vault_dir, db_conn=db_conn)
    out2 = RecentChangesTool().call({}, ctx_plain)
    assert "日记/a.md" in out2 and "was: 日记/old.md" in out2
    paths = ctx_plain.last_meta["paths"]
    assert "日记/a.md" in paths and "notes/b.md" in paths and "日记/old.md" in paths


def test_get_changes_privacy_sql_layer(db_conn):
    """P2b SQL 层钉：get_changes(privacy_dirs=…) 对 path + old_path 双列
    privacy_clause 过滤；空名单零变化（web/reminder 等既有调用方守恒）。"""
    from server.vault.db.changes import get_changes

    _mk_change(db_conn, "日记/a.md")                          # path 隐私 → 剔
    _mk_change(db_conn, "notes/b.md", from_path="日记/old.md")  # old 隐私 → 剔
    _mk_change(db_conn, "notes/c.md")                          # 正常
    rows = get_changes(db_conn, privacy_dirs=("日记",))
    assert {r["path"] for r in rows} == {"notes/c.md"}
    # 守恒：空名单原样全量
    assert len(get_changes(db_conn, privacy_dirs=())) == 3
    assert len(get_changes(db_conn)) == 3


def test_get_changes_privacy_limit_after_filter(db_conn):
    """「隐私行吃 limit 窗口」钉：LIMIT 在过滤后生效——隐私行再多，非隐私
    行仍占满 limit（post-filter 实现做不到：limit 先截断、过滤后剩空）。"""
    from server.vault.db.changes import get_changes

    _mk_change(db_conn, "notes/p1.md")
    _mk_change(db_conn, "notes/p2.md")
    for _ in range(4):
        _mk_change(db_conn, "日记/x.md")
    rows = get_changes(db_conn, limit=2, privacy_dirs=("日记",))
    assert len(rows) == 2  # post-filter 版会得 0（窗口被隐私行吃光）
    assert [r["path"] for r in rows] == ["notes/p2.md", "notes/p1.md"]  # seq DESC
    # 对照：不传名单，limit=2 的窗口确实全被隐私行占据（它们最新）
    rows = get_changes(db_conn, limit=2)
    assert [r["path"] for r in rows] == ["日记/x.md", "日记/x.md"]


def test_recent_changes_tool_delegates_filter_to_sql() -> None:
    """接线钉（P2b 收口回归 spec §2.3「change_log SQL」）：工具层把名单
    交给 get_changes（privacy_dirs= 参），自身不再 import/调用 is_privacy。"""
    src = inspect.getsource(RecentChangesTool._execute)
    assert "privacy_dirs=" in src
    assert "is_privacy" not in src


# ── MCP get_related_docs: validate first (wiring pins) ────────────────────

def test_mcp_get_related_docs_wired_validate_and_privacy():
    """源码接线钉（计划：直接测注册前的 handler 函数体）：MCP handler 先
    validate_tool_call("get_related_docs", …, MCP_POLICY) 再查 graph。"""
    import server.mcp_server as m

    src = inspect.getsource(m.main)
    start = src.index("async def get_related_docs")
    body = src[start:src.index("@mcp.tool()", start)]
    assert 'validate_tool_call("get_related_docs"' in body
    # 注意取调用点（results = …(）而非同名 import 行：import 在 validate 之前，
    # 接线钉要钉的是「先 validate、后查询」。
    assert "results = graph_get_related_with_machine(" in body
    assert body.index('validate_tool_call("get_related_docs"') < \
        body.index("results = graph_get_related_with_machine(")
    assert "privacy" in body  # privacy dirs 透传给查询


def test_mcp_policy_privacy_read_denied(vault_dir, tmp_path):
    """行为面：MCP 同款 ChannelPolicy（privacy 接线）下隐私路径读 →
    INVISIBLE_MSG（MCP_POLICY 经 validate_tool_call 生效）。"""
    from server.agent.policy import ChannelPolicy, validate_tool_call

    policy = ChannelPolicy(vault_dir, privacy=_pcfg(tmp_path, "[日记]"))
    err = validate_tool_call("get_related_docs", {"path": "日记/a.md"}, policy)
    assert err is not None and INVISIBLE_MSG in err


# ── write_doc / edit_doc advisory (spec §2.6) ──────────────────────────────

REFS = "see [[日记/2026]] and 日记/x.md"


def test_write_doc_advisory_nonprivacy_target(vault_dir, tmp_path):
    ctx = ToolContext(vault_dir=vault_dir, privacy=_pcfg(tmp_path, "[日记]"))
    out = WriteDocTool().call({"path": "notes/a.md", "content": REFS}, ctx)
    assert out.startswith("Saved: notes/a.md")
    assert "warning: content references private paths:" in out
    assert "[[日记/2026]]" in out and "日记/x.md" in out
    assert (vault_dir / "notes/a.md").read_bytes().decode() == REFS  # 保存照常


def test_write_doc_no_advisory_inside_privacy_zone(vault_dir, tmp_path):
    ctx = ToolContext(vault_dir=vault_dir, privacy=_pcfg(tmp_path, "[日记]"))
    out = WriteDocTool().call({"path": "日记/secret.md", "content": REFS}, ctx)
    assert out.startswith("Saved: 日记/secret.md")
    assert "warning" not in out  # 区内写是合法动作，不警告
    assert (vault_dir / "日记/secret.md").is_file()


def test_write_doc_no_advisory_without_privacy(vault_dir):
    """ctx.privacy=None → 结果串与改动前逐字节一致（等价钉）。"""
    ctx = ToolContext(vault_dir=vault_dir)
    out = WriteDocTool().call({"path": "notes/a.md", "content": REFS}, ctx)
    assert out == f"Saved: notes/a.md ({len(REFS)} chars)"


def test_edit_doc_advisory(vault_dir, tmp_path):
    _write(vault_dir, "notes/e.md", "hello\n")
    ctx = ToolContext(vault_dir=vault_dir, privacy=_pcfg(tmp_path, "[日记]"))
    out = EditDocTool().call(
        {"path": "notes/e.md", "old_string": "hello", "new_string":
         "hello " + REFS}, ctx)
    assert not out.startswith("Error")
    assert "warning: content references private paths:" in out
    # edit_file 保留文件原有的尾随换行
    assert (vault_dir / "notes/e.md").read_bytes().decode() == "hello " + REFS + "\n"


def test_edit_doc_no_advisory_inside_privacy_zone(vault_dir, tmp_path):
    _write(vault_dir, "日记/e.md", "hello\n")
    ctx = ToolContext(vault_dir=vault_dir, privacy=_pcfg(tmp_path, "[日记]"))
    out = EditDocTool().call(
        {"path": "日记/e.md", "old_string": "hello", "new_string":
         "hello " + REFS}, ctx)
    assert not out.startswith("Error")
    assert "warning" not in out


def test_edit_doc_error_path_no_advisory(vault_dir, tmp_path):
    """失败路径（old_string 未命中）零 advisory、零行为变化。"""
    _write(vault_dir, "notes/e.md", "hello\n")
    ctx = ToolContext(vault_dir=vault_dir, privacy=_pcfg(tmp_path, "[日记]"))
    out = EditDocTool().call(
        {"path": "notes/e.md", "old_string": "nope", "new_string": REFS}, ctx)
    assert out.startswith("Error")
    assert "warning" not in out
