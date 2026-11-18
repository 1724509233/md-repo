"""Tests for the relatedness ranker wiring (v2b②, spec 2026-11-13 §6).

Acceptance items (spec §6 验收, L673–687):
 1. ignored mechanical filter — executor bidirectional (D12/D13) + readback
 2. live_edges predicate anchor — ignored rows never enter the ranker
 3. derived guardrails — layer order + boost ≡ 0 (rag_search zero-derived
    is pinned in test_invariants_v2.py — the v1 anchor)
 4. tie-break — relaxed/fuzzy reorder epsilon ties; strict unchanged
 5. telemetry feedback — static: no new upsert_edge/merge_edge callers
 7. delete — output drops the dead endpoint, row stays, restore revives
 8. delete — propose after delete → failed (v2a structural check)
 9. move — D19 in-place rewrite; provenance + ignored survive; filter holds
10. move — recall_clicks telemetry keeps the old path
11-13. D20 read-time dedup — adopted / reversed / revived
14. proposal immutability — source_refs history vs the live row

(6 = regression: the existing suites test_relatedness.py /
test_recall_full.py / the get_related_docs format tests run in the same
batch; their anchors are unchanged or updated in place.)
"""
from __future__ import annotations

from pathlib import Path

from server.services import proposals
from server.bgagent import runner
from server.bgagent.readback import ReadbackSystemReminder
from server.agent.tools import TOOL_REGISTRY
from server.bgagent.executor import run_executor
from server.vault.db import relatedness as rel
from server.vault.db.graph_sql import graph_get_related_with_machine
from server.vault.db.relatedness_rank import is_derived, live_edges, relatedness_boost
from server.vault.db.segments_sql import segments_search
from server.vault.orchestration.file_ops import apply_change
from server.vault.orchestration.indexer import graph_extract_file, index_segments
from tests.test_executor import _adopted


def _write(vault_dir, relpath: str, content: bytes = b"# doc\n") -> None:
    fp = vault_dir / relpath
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_bytes(content)


def _tool_output(vault_dir, path: str) -> str:
    from server.agent import ToolContext
    from server.agent.tools.get_related_docs import GetRelatedDocsArgs, GetRelatedDocsTool

    return GetRelatedDocsTool()._execute(
        GetRelatedDocsArgs(path=path), ToolContext(vault_dir=vault_dir))


def _index_doc(vault_dir, conn, relpath: str, content: bytes) -> None:
    _write(vault_dir, relpath, content)
    index_segments(conn, vault_dir, relpath)


# ── 1. ignored mechanical filter (D12/D13) ───────────────────────────────

def test_ignored_pair_repropose_failed_both_directions(vault_dir, db_conn, ledger_conn):
    """人忽略 (A,B) → agent 再 propose (A,B) 与 (B,A) → executor 均不落地，
    提案 failed + reason 含 "mechanical filter"；回读注入含该 failed 行。"""
    _write(vault_dir, "notes/a.md")
    _write(vault_dir, "notes/b.md")
    eid = rel.upsert_edge(ledger_conn, "notes/a.md", "notes/b.md", "same topic",
                          "consolidator")
    assert rel.mark_ignored(ledger_conn, eid)

    # Forward re-propose → NOT landed; failed + mechanical-filter reason.
    rec = _adopted(vault_dir, ledger_conn, "relatedness",
                   source_refs=["notes/a.md"], payload={"target": "notes/b.md"})
    summary = run_executor(vault_dir, ledger_conn)
    assert summary["merged"] == 0 and summary["errors"] == []
    p = proposals.record(ledger_conn, rec["id"])
    assert p["status"] == "failed"
    assert "mechanical filter" in p["reject_reason"]

    # Reversed re-propose (D13: the dismissal must not be bypassed) → same.
    rec2 = _adopted(vault_dir, ledger_conn, "relatedness",
                    source_refs=["notes/b.md"], payload={"target": "notes/a.md"})
    run_executor(vault_dir, ledger_conn)
    p2 = proposals.record(ledger_conn, rec2["id"])
    assert p2["status"] == "failed"
    assert "mechanical filter" in p2["reject_reason"]

    # Nothing landed: the single ignored row stays, no fresh live row.
    assert ledger_conn.execute("SELECT COUNT(*) FROM relatedness").fetchone()[0] == 1
    assert ledger_conn.execute(
        "SELECT COUNT(*) FROM relatedness WHERE ignored = 0").fetchone()[0] == 0

    # Readback injection surfaces the failed rows (the agent sees
    # "human dismissed, stop proposing" next run).
    tools = list(TOOL_REGISTRY.values())  # effective surface incl. propose → gate open
    block = ReadbackSystemReminder("consolidator", tools).render() or ""
    assert "mechanical filter" in block


# ── 2. ranker input predicate (ignored never enters) ─────────────────────

def test_live_edges_ignored_predicate_and_path_filter(vault_dir, ledger_conn):
    """ignored 行构造后 live_edges 零返回（谓词锚点 WHERE ignored = 0）。"""
    for name in ("a", "b", "c", "d"):
        _write(vault_dir, f"notes/{name}.md")
    e1 = rel.upsert_edge(ledger_conn, "notes/a.md", "notes/b.md", "t1", "t")
    rel.upsert_edge(ledger_conn, "notes/c.md", "notes/d.md", "t2", "t")

    assert len(live_edges(ledger_conn, vault_dir)) == 2
    assert len(live_edges(ledger_conn, vault_dir, path="notes/a.md")) == 1
    assert len(live_edges(ledger_conn, vault_dir, path="notes/d.md")) == 1

    assert rel.mark_ignored(ledger_conn, e1)
    out = live_edges(ledger_conn, vault_dir)
    assert len(out) == 1
    assert {out[0]["source"], out[0]["target"]} == {"notes/c.md", "notes/d.md"}
    assert live_edges(ledger_conn, vault_dir, path="notes/a.md") == []
    assert live_edges(ledger_conn, vault_dir, path="notes/b.md") == []


def test_live_edges_dead_endpoint_filter(vault_dir, ledger_conn):
    """D18: a deleted endpoint drops the edge from the ranker input; the row
    is never deleted (provenance) — restore revives it automatically."""
    _write(vault_dir, "notes/a.md")
    _write(vault_dir, "notes/b.md")
    rel.upsert_edge(ledger_conn, "notes/a.md", "notes/b.md", "t", "t")
    assert len(live_edges(ledger_conn, vault_dir)) == 1

    (vault_dir / "notes" / "a.md").unlink()
    assert live_edges(ledger_conn, vault_dir) == []
    assert ledger_conn.execute("SELECT COUNT(*) FROM relatedness").fetchone()[0] == 1

    _write(vault_dir, "notes/a.md")  # restore
    assert len(live_edges(ledger_conn, vault_dir)) == 1


# ── 3. derived guardrails (invariant 6) ──────────────────────────────────

def test_is_derived_first_segment_single_source(vault_dir):
    """derived 判定 = 首段 ∈ EXCLUDED_INDEX_DIRS（与 v1 索引排除同一集合）。"""
    assert is_derived(vault_dir, "index/entities.md")
    assert is_derived(vault_dir, "suggestions/x.md")
    # Deeper same-named dirs are user content (the v1 exclusion rule).
    assert not is_derived(vault_dir, "notes/index.md")
    assert not is_derived(vault_dir, "index.md")  # a top-level FILE, not a dir
    # Absolute paths resolve against the vault root.
    assert is_derived(vault_dir, str(vault_dir / "index" / "e.md"))
    assert not is_derived(vault_dir, "/etc/passwd")


def test_boost_zero_for_derived_candidates(vault_dir, ledger_conn):
    """tie-break 面：derived 候选的 boost 恒 0（防御性护栏）。"""
    _write(vault_dir, "index/entities.md")
    _write(vault_dir, "notes/a.md")
    _write(vault_dir, "notes/b.md")
    rel.upsert_edge(ledger_conn, "notes/a.md", "index/entities.md", "e1", "t")
    rel.upsert_edge(ledger_conn, "notes/b.md", "index/entities.md", "e2", "t")
    rel.upsert_edge(ledger_conn, "notes/a.md", "notes/b.md", "e3", "t")

    boost = relatedness_boost(
        ledger_conn, ["notes/a.md", "index/entities.md", "notes/b.md"])
    assert boost["index/entities.md"] == 0.0  # derived never outranks
    assert boost["notes/a.md"] > 0.0

    # Context-linked priority: b is live-linked to context a → b outranks a.
    boost2 = relatedness_boost(
        ledger_conn, ["notes/a.md", "notes/b.md"], context="notes/a.md")
    assert boost2["notes/b.md"] > boost2["notes/a.md"]
    assert max(boost2.values()) <= 1.0
    # No live edges → all-zero boost (the tie-break is a no-op).
    assert relatedness_boost(ledger_conn, ["notes/lonely.md"]) == {
        "notes/lonely.md": 0.0}


def test_get_related_docs_two_layer_order_derived_last(vault_dir, db_conn, ledger_conn):
    """§6.3-② anchor: user layer first, machine layer after, derived targets
    LAST within the machine layer — even when the derived doc has MORE live
    edges than the user doc."""
    _write(vault_dir, "notes/a.md", b"# A\n\nSee [[c]].\n")
    _write(vault_dir, "notes/b.md")
    _write(vault_dir, "notes/c.md")
    _write(vault_dir, "index/entities.md")
    graph_extract_file(db_conn, vault_dir, "notes/a.md")

    # Machine edges: a→b (user doc, 1 edge); a→entities + c→entities
    # (derived doc, 2 edges — the derived doc has MORE).
    rel.upsert_edge(ledger_conn, "notes/a.md", "notes/b.md", "topic", "t")
    rel.upsert_edge(ledger_conn, "notes/a.md", "index/entities.md", "ent", "t")
    rel.upsert_edge(ledger_conn, "notes/c.md", "index/entities.md", "ent2", "t")

    out = graph_get_related_with_machine(db_conn, ledger_conn, vault_dir, "notes/a.md")
    assert [r["path"] for r in out] == ["notes/c.md", "notes/b.md", "index/entities.md"]
    assert [r["layer"] for r in out] == ["user", "machine", "machine"]
    assert out[0]["type"] == "wikilink"
    assert out[1]["type"] == "relatedness" and out[1]["basis"] == "topic"
    assert out[2]["score"] == 0.0

    # Tool output: two-layer format, same order.
    text = _tool_output(vault_dir, "notes/a.md")
    assert "Your links:" in text
    assert "Machine suggestions (via relatedness" in text
    assert (text.index("notes/c.md") < text.index("notes/b.md")
            < text.index("index/entities.md"))


# ── 4. tie-break (D11: reorder epsilon ties, never a weight change) ──────

def test_relaxed_tie_break_prefers_live_edge_doc(vault_dir, db_conn, ledger_conn):
    """FTS 同分两文档（一有 live 边一无）→ relaxed 档输出有边者优先。"""
    body = "# X\n\n搜索引擎 分层召回 设计文档。\n".encode("utf-8")
    _index_doc(vault_dir, db_conn, "notes/x.md", body)
    _index_doc(vault_dir, db_conn, "notes/y.md", body)  # identical → identical score
    q = "搜索引擎 分层召回"

    base = segments_search(db_conn, q, top_k=5, recall="relaxed")
    assert {r["path"] for r in base} == {"notes/x.md", "notes/y.md"}
    assert base[0]["score"] == base[1]["score"]  # the tie

    # A live edge for y → y outranks x in the relaxed tier.
    _write(vault_dir, "notes/z.md")
    rel.upsert_edge(ledger_conn, "notes/y.md", "notes/z.md", "topic", "t")
    after = segments_search(db_conn, q, top_k=5, recall="relaxed")
    assert [r["path"] for r in after] == ["notes/y.md", "notes/x.md"]
    # The boost never enters the FTS score: the score values are unchanged.
    assert [r["score"] for r in after] == [r["score"] for r in base]


def test_fuzzy_tie_break_prefers_live_edge_doc(vault_dir, db_conn, ledger_conn):
    body = "# X\n\n搜索引擎 分层召回 设计文档。\n".encode("utf-8")
    _index_doc(vault_dir, db_conn, "notes/x.md", body)
    _index_doc(vault_dir, db_conn, "notes/y.md", body)
    q = "搜索引擎 分层召回"
    _write(vault_dir, "notes/z.md")
    rel.upsert_edge(ledger_conn, "notes/y.md", "notes/z.md", "topic", "t")
    after = segments_search(db_conn, q, top_k=5, recall="fuzzy")
    assert [r["path"] for r in after] == ["notes/y.md", "notes/x.md"]


def test_strict_tier_untouched_by_relatedness(vault_dir, db_conn, ledger_conn):
    """strict 档输出不变（回归）：bm25 单 MATCH 排序是 FTS 内建，tie-break
    只动 Python 侧排序的档（relaxed/fuzzy）；strict 输出逐字节相同。"""
    body = "# X\n\n搜索引擎 分层召回 设计文档。\n".encode("utf-8")
    _index_doc(vault_dir, db_conn, "notes/x.md", body)
    _index_doc(vault_dir, db_conn, "notes/y.md", body)
    q = "搜索引擎 分层召回"
    before = segments_search(db_conn, q, top_k=5, recall="strict")
    _write(vault_dir, "notes/z.md")
    rel.upsert_edge(ledger_conn, "notes/y.md", "notes/z.md", "topic", "t")
    after = segments_search(db_conn, q, top_k=5, recall="strict")
    assert before == after


# ── 5. telemetry feedback: static assertion (D14) ────────────────────────

def test_no_new_upsert_edge_or_merge_edge_callers():
    """relatedness 表零自动写入：无新调用方调 upsert_edge/merge_edge（除
    executor）。merge_edge 的唯一生产调用方 = executor；upsert_edge 仅被
    包内的 merge_edge 调用（持锁入口）。"""
    import server

    root = Path(server.__file__).resolve().parent
    callers: set[str] = set()
    for f in sorted(root.rglob("*.py")):
        text = f.read_text(encoding="utf-8")
        if "upsert_edge(" in text or "merge_edge(" in text:
            callers.add(str(f.relative_to(root.parent)))
    assert callers == {
        "server/vault/db/relatedness.py",   # definitions + merge_edge→upsert_edge
        "server/bgagent/executor.py",       # the only merge_edge caller
    }


# ── 7. delete: row stays, output filters, restore revives (D18) ──────────

def test_delete_drops_edge_row_stays_restore_revives(vault_dir, db_conn, ledger_conn):
    """删文档 A（有 live 边 A-B）→ 输出无 A；relatedness 行仍在（ledger 不可
    重建锚点）；restore A → 输出自动恢复，零人工。"""
    _write(vault_dir, "notes/a.md")
    _write(vault_dir, "notes/b.md")
    rel.upsert_edge(ledger_conn, "notes/a.md", "notes/b.md", "topic", "t")

    out = graph_get_related_with_machine(db_conn, ledger_conn, vault_dir, "notes/b.md")
    assert [r["path"] for r in out] == ["notes/a.md"]
    assert out[0]["layer"] == "machine"

    # Delete A → A drops out of every read surface; the row stays.
    (vault_dir / "notes" / "a.md").unlink()
    assert graph_get_related_with_machine(
        db_conn, ledger_conn, vault_dir, "notes/b.md") == []
    rows = ledger_conn.execute(
        "SELECT source, target, ignored FROM relatedness").fetchall()
    assert len(rows) == 1
    assert (rows[0]["source"], rows[0]["target"], rows[0]["ignored"]) == (
        "notes/a.md", "notes/b.md", 0)

    # Restore A → the output revives, zero writes (no-op on the ledger).
    _write(vault_dir, "notes/a.md")
    out2 = graph_get_related_with_machine(db_conn, ledger_conn, vault_dir, "notes/b.md")
    assert [r["path"] for r in out2] == ["notes/a.md"]


# ── 8. delete: propose after delete → failed (v2a structural check) ──────

def test_propose_after_delete_failed(vault_dir, db_conn, ledger_conn):
    """A 删除后 agent propose (A,B) → executor failed + reason（既有 v2a
    结构校验锚点：drift → surface，不盲落）。"""
    _write(vault_dir, "notes/a.md")
    _write(vault_dir, "notes/b.md")
    rel.upsert_edge(ledger_conn, "notes/a.md", "notes/b.md", "topic", "t")
    (vault_dir / "notes" / "a.md").unlink()

    rec = _adopted(vault_dir, ledger_conn, "relatedness",
                   source_refs=["notes/a.md"], payload={"target": "notes/b.md"})
    summary = run_executor(vault_dir, ledger_conn)
    assert summary["merged"] == 0 and summary["errors"] == []
    p = proposals.record(ledger_conn, rec["id"])
    assert p["status"] == "failed"
    assert "not found" in p["reject_reason"]


# ── 9/10/14. move: D19 in-place rewrite ──────────────────────────────────

def _move_a_to_b(vault_dir, db_conn) -> None:
    """Rename on disk + drive the production moved branch (apply_change)."""
    (vault_dir / "notes" / "a.md").rename(vault_dir / "notes" / "b.md")
    apply_change(db_conn, vault_dir, "notes/b.md", "moved",
                 from_path="notes/a.md", source="test")


def test_move_rewrites_paths_preserves_verdicts(vault_dir, db_conn, ledger_conn):
    """移动 A→B（live 边 A-C + ignored 边 A-D）→ B-C live + B-D ignored
    （provenance/ignored 保留）；旧路径 A 零行；get_related_docs(B) 含 C；
    agent 再提 (B,D) → 机械过滤仍触发。"""
    _write(vault_dir, "notes/a.md")
    _write(vault_dir, "notes/c.md")
    _write(vault_dir, "notes/d.md")
    rel.upsert_edge(ledger_conn, "notes/a.md", "notes/c.md", "topic",
                    "consolidator", run_id="r1")
    eid_d = rel.upsert_edge(ledger_conn, "notes/a.md", "notes/d.md", "nope",
                            "consolidator", run_id="r1")
    assert rel.mark_ignored(ledger_conn, eid_d)

    _move_a_to_b(vault_dir, db_conn)

    rows = {(r["source"], r["target"]): dict(r) for r in ledger_conn.execute(
        "SELECT * FROM relatedness").fetchall()}
    assert set(rows) == {("notes/b.md", "notes/c.md"), ("notes/b.md", "notes/d.md")}
    assert rows[("notes/b.md", "notes/c.md")]["ignored"] == 0
    assert rows[("notes/b.md", "notes/d.md")]["ignored"] == 1
    # Provenance preserved (the relation is about the document, not the path).
    for r in rows.values():
        assert r["actor"] == "consolidator" and r["run_id"] == "r1"
    assert rows[("notes/b.md", "notes/c.md")]["basis"] == "topic"
    assert rows[("notes/b.md", "notes/d.md")]["basis"] == "nope"
    # Zero rows at the old path.
    assert ledger_conn.execute(
        "SELECT COUNT(*) FROM relatedness WHERE source = ? OR target = ?",
        ("notes/a.md", "notes/a.md")).fetchone()[0] == 0

    # get_related_docs(B) contains C (machine layer) — and NOT D (ignored).
    out = graph_get_related_with_machine(db_conn, ledger_conn, vault_dir, "notes/b.md")
    paths = [r["path"] for r in out]
    assert "notes/c.md" in paths and "notes/d.md" not in paths

    # The human's dismissal survives the move: re-propose (B,D) → filtered.
    rec = _adopted(vault_dir, ledger_conn, "relatedness",
                   source_refs=["notes/b.md"], payload={"target": "notes/d.md"})
    run_executor(vault_dir, ledger_conn)
    p = proposals.record(ledger_conn, rec["id"])
    assert p["status"] == "failed"
    assert "mechanical filter" in p["reject_reason"]


def test_move_duplicate_pair_rewrite_noop(vault_dir, db_conn, ledger_conn):
    """重复对：目标路径已有同 pair live 行 → 改写 no-op（关系已在目的地记录）；
    旧行留在旧路径、经 D18 死端点过滤出读面，永不删除。"""
    _write(vault_dir, "notes/a.md")
    _write(vault_dir, "notes/b.md")
    _write(vault_dir, "notes/c.md")
    rel.upsert_edge(ledger_conn, "notes/b.md", "notes/c.md", "dest", "t")
    eid = rel.upsert_edge(ledger_conn, "notes/a.md", "notes/c.md", "old", "t")

    _move_a_to_b(vault_dir, db_conn)

    # The destination pair keeps its own row; the old row stays at A (now
    # dead — A no longer exists) and is never deleted.
    rows = sorted((r["source"], r["target"], r["ignored"]) for r in ledger_conn.execute(
        "SELECT source, target, ignored FROM relatedness").fetchall())
    assert rows == [
        ("notes/a.md", "notes/c.md", 0),   # old row, dead endpoint, kept
        ("notes/b.md", "notes/c.md", 0),   # the pre-existing destination row
    ]
    assert ledger_conn.execute(
        "SELECT basis FROM relatedness WHERE id = ?", (eid,)).fetchone()[0] == "old"
    # The read surface shows the pair exactly once (via the live destination).
    out = graph_get_related_with_machine(db_conn, ledger_conn, vault_dir, "notes/b.md")
    assert [r["path"] for r in out] == ["notes/c.md"]


def test_move_recall_clicks_keep_old_path(vault_dir, db_conn, ledger_conn):
    """移动：recall_clicks 旧路径行不动（无改写）——index.db 时间点遥测。"""
    _write(vault_dir, "notes/a.md")
    _write(vault_dir, "notes/b.md")
    db_conn.execute(
        "INSERT INTO recall_clicks (ts, path, source, traffic, via) "
        "VALUES (?, ?, ?, ?, ?)",
        (1000.0, "notes/a.md", "web", "human", "search"))
    db_conn.commit()

    _move_a_to_b(vault_dir, db_conn)

    rows = db_conn.execute(
        "SELECT path FROM recall_clicks").fetchall()
    assert [r[0] for r in rows] == ["notes/a.md"]


def test_proposal_source_refs_history_after_move(vault_dir, db_conn, ledger_conn):
    """提案不可变：移动 A→B 后，生成该边的提案 source_refs[0] 仍为 A（历史
    锚点），relatedness 行为 B（活状态锚点），两读面各自正确。"""
    _write(vault_dir, "notes/a.md")
    _write(vault_dir, "notes/c.md")
    rec = _adopted(vault_dir, ledger_conn, "relatedness",
                   source_refs=["notes/a.md"], payload={"target": "notes/c.md"})
    summary = run_executor(vault_dir, ledger_conn)
    assert summary["merged"] == 1

    _move_a_to_b(vault_dir, db_conn)

    # History copy: the proposal record keeps the path at propose time.
    p = proposals.record(ledger_conn, rec["id"])
    assert p["source_refs"][0] == "notes/a.md"
    # Live state: the relatedness row is normalized to the current path.
    row = ledger_conn.execute(
        "SELECT source, target FROM relatedness").fetchone()
    assert (row["source"], row["target"]) == ("notes/b.md", "notes/c.md")
    # The readback read surface (edges_for_actor) shows the current path.
    edges = rel.edges_for_actor(ledger_conn, "consolidator")
    assert {(e["source"], e["target"]) for e in edges} == {
        ("notes/b.md", "notes/c.md")}


# ── 11-13. D20 read-time dedup (unordered pair) ──────────────────────────

def _dedup_setup(vault_dir, db_conn, ledger_conn, machine_edge: tuple[str, str]):
    """a.md with a [[b]] wikilink (user layer) + one live machine edge."""
    _write(vault_dir, "notes/a.md", b"# A\n\nSee [[b]].\n")
    _write(vault_dir, "notes/b.md")
    graph_extract_file(db_conn, vault_dir, "notes/a.md")
    rel.upsert_edge(ledger_conn, *machine_edge, "topic", "t")


def test_dedup_adopted_pair_drained(vault_dir, db_conn, ledger_conn):
    """机器边 A-B live + 用户在 A 正文补 [[B]] → get_related_docs(A) 中 B
    恰好一次、在用户层；机器层无 B；relatedness 行仍 live（provenance）。"""
    _dedup_setup(vault_dir, db_conn, ledger_conn, ("notes/a.md", "notes/b.md"))

    out = graph_get_related_with_machine(db_conn, ledger_conn, vault_dir, "notes/a.md")
    assert [r["path"] for r in out] == ["notes/b.md"]
    assert out[0]["layer"] == "user"
    assert all(r["layer"] != "machine" for r in out)

    # The row stays live (provenance anchor) — dedup is read-time only.
    row = ledger_conn.execute(
        "SELECT source, target, ignored FROM relatedness").fetchone()
    assert (row["source"], row["target"], row["ignored"]) == (
        "notes/a.md", "notes/b.md", 0)

    # Tool output: B once, in the user layer.
    text = _tool_output(vault_dir, "notes/a.md")
    assert text.count("notes/b.md") == 1
    assert "Machine suggestions" not in text


def test_dedup_reversed_machine_edge(vault_dir, db_conn, ledger_conn):
    """反向（机器边 B→A + 用户链 A→B）→ 同样去重（无序对锚点，D13 同口径）。"""
    _dedup_setup(vault_dir, db_conn, ledger_conn, ("notes/b.md", "notes/a.md"))

    out = graph_get_related_with_machine(db_conn, ledger_conn, vault_dir, "notes/a.md")
    assert [r["path"] for r in out] == ["notes/b.md"]
    assert out[0]["layer"] == "user"
    assert all(r["layer"] != "machine" for r in out)


def test_dedup_link_removed_suggestion_revives(vault_dir, db_conn, ledger_conn):
    """用户删掉正文链接 → B 重新出现在机器层（建议复活）：机器层语义 =
    「正文尚未收录的建议」。"""
    _dedup_setup(vault_dir, db_conn, ledger_conn, ("notes/a.md", "notes/b.md"))

    # The user removes the link; re-extraction clears the outgoing edge.
    _write(vault_dir, "notes/a.md", b"# A\n\nNo links here.\n")
    graph_extract_file(db_conn, vault_dir, "notes/a.md")

    out = graph_get_related_with_machine(db_conn, ledger_conn, vault_dir, "notes/a.md")
    assert [r["path"] for r in out] == ["notes/b.md"]
    assert out[0]["layer"] == "machine"
    assert out[0]["type"] == "relatedness"
    assert out[0]["basis"] == "topic"


# ── ignore endpoint (D15: the human-side entry of the mechanical filter) ─

def test_ignore_endpoint_flags_row_and_404s(client, vault_dir, db_conn, ledger_conn):
    _write(vault_dir, "notes/a.md")
    _write(vault_dir, "notes/b.md")
    eid = rel.upsert_edge(ledger_conn, "notes/a.md", "notes/b.md", "topic", "t")

    resp = client.post(f"/api/memory/relatedness/{eid}/ignore")
    assert resp.status_code == 200
    assert resp.json() == {"edge_id": eid, "ignored": True}
    assert ledger_conn.execute(
        "SELECT ignored FROM relatedness WHERE id = ?", (eid,)).fetchone()[0] == 1

    # Re-ignoring the same row → 404 (the flag is one-way per row).
    assert client.post(f"/api/memory/relatedness/{eid}/ignore").status_code == 404
    # Unknown id → 404.
    assert client.post("/api/memory/relatedness/99999/ignore").status_code == 404


# ── doc face endpoint (spec §6.4: the console's two-block read surface) ──

def test_doc_face_endpoint_two_layers(client, vault_dir, db_conn, ledger_conn):
    """文档面读端点：用户 wikilink 层在前 + 机器层在后（edge_id 供忽略按钮，
    basis 供依据摘要）；层序与 get_related_docs 输出一致。"""
    _write(vault_dir, "notes/a.md", b"# A\n\nSee [[b]].\n")
    _write(vault_dir, "notes/b.md")
    _write(vault_dir, "notes/c.md")
    graph_extract_file(db_conn, vault_dir, "notes/a.md")
    eid = rel.upsert_edge(ledger_conn, "notes/a.md", "notes/c.md", "topic", "t")

    resp = client.get("/api/memory/doc/notes/a.md")
    assert resp.status_code == 200
    data = resp.json()
    assert data["path"] == "notes/a.md"
    assert [r["path"] for r in data["links"]] == ["notes/b.md"]
    assert [r["layer"] for r in data["links"]] == ["user"]
    assert [r["path"] for r in data["suggestions"]] == ["notes/c.md"]
    assert data["suggestions"][0]["layer"] == "machine"
    assert data["suggestions"][0]["edge_id"] == eid
    assert data["suggestions"][0]["basis"] == "topic"


def test_doc_face_endpoint_404_deleted_doc(client, vault_dir, db_conn, ledger_conn):
    """文档已删 → 404（文档面渲染死文档态「该文档已不存在」）。"""
    _write(vault_dir, "notes/a.md")
    (vault_dir / "notes" / "a.md").unlink()
    assert client.get("/api/memory/doc/notes/a.md").status_code == 404


def test_doc_face_endpoint_ignored_and_drained(
        client, vault_dir, db_conn, ledger_conn):
    """忽略 → mark_ignored 落行，再次读文档面该建议不再出现（ignored 过滤）；
    D20 采纳（正文补链）→ 建议排干（无序对进用户层）。"""
    _write(vault_dir, "notes/a.md", b"# A\n\nSee [[b]].\n")
    _write(vault_dir, "notes/b.md")
    _write(vault_dir, "notes/c.md")
    graph_extract_file(db_conn, vault_dir, "notes/a.md")
    eid = rel.upsert_edge(ledger_conn, "notes/a.md", "notes/c.md", "topic", "t")
    eid_b = rel.upsert_edge(ledger_conn, "notes/a.md", "notes/b.md", "topic2", "t")

    # Ignore the C suggestion → the row is flagged, the face drops it.
    assert client.post(f"/api/memory/relatedness/{eid}/ignore").status_code == 200
    assert ledger_conn.execute(
        "SELECT ignored FROM relatedness WHERE id = ?", (eid,)).fetchone()[0] == 1

    # The B suggestion is drained by D20 ([[b]] is in the user layer);
    # the ignored C suggestion is filtered by the ranker predicate.
    data = client.get("/api/memory/doc/notes/a.md").json()
    assert [r["path"] for r in data["links"]] == ["notes/b.md"]
    assert data["suggestions"] == []
    # Provenance anchor: both rows stay (one ignored, one live-but-drained).
    assert ledger_conn.execute(
        "SELECT COUNT(*) FROM relatedness").fetchone()[0] == 2
    assert ledger_conn.execute(
        "SELECT COUNT(*) FROM relatedness WHERE ignored = 0").fetchone()[0] == 1
    assert eid_b != eid
