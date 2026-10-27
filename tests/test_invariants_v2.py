"""Spec invariants for reversible-automation v2 (P2) — full-flow integration.

Invariant 1 (iron rule: 用户区零自动化写入): the user area is zero-write
through the ENTIRE v2 pipeline — propose (auto + review) → executor merge
(entity_index → index/entities.md; relatedness → DB-only; summary →
index/summaries/; errata → index/errata/) — no user-area byte changes, no
user-area files added or moved.

Invariant 6 (machine zone is FTS-excluded): after the full flow and
re-indexing, ZERO segments exist for index/* (or suggestions/*) paths —
the machine zone never enters recall; the user's notes stay fully
searchable. (Mechanism: EXCLUDED_INDEX_DIRS — the MD walker and
apply_change both skip the excluded top dirs.)
"""
from __future__ import annotations

import hashlib

from server import proposals
from server.agent.tools.propose import ProposeTool
from server.agent.tools.tool import ToolContext
from server.bgagent.executor import run_executor
from server.vault.fs.vault_ops import EXCLUDED_INDEX_DIRS

_TOP_EXCLUDED = EXCLUDED_INDEX_DIRS | frozenset({".sync"})


def _user_area(vault_dir) -> dict[str, str]:
    """(path, sha256) of every user-area byte — everything except the
    machine zone (index/), the proposal zone (suggestions/) and .sync/."""
    out = {}
    for p in sorted(vault_dir.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(vault_dir).as_posix()
        if rel.split("/", 1)[0] in _TOP_EXCLUDED:
            continue
        out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def test_user_area_zero_writes_through_full_flow(vault_dir, db_conn):
    (vault_dir / "notes" / "other.md").write_bytes(b"# Other\n\nbody text\n")
    before = _user_area(vault_dir)

    tool = ProposeTool()
    ctx = ToolContext(vault_dir=vault_dir, write_source="consolidator",
                      run_id="consolidator-20261026T000000")
    # Auto channel: entity_index + relatedness → adopted immediately.
    assert tool.call(
        {"type": "entity_index",
         "payload": {"entity": "e1", "attrs": {"f": "v"}},
         "source_refs": ["notes/test.md"], "justification": "j"}, ctx
    ).startswith("proposal #")
    assert tool.call(
        {"type": "relatedness", "payload": {"target": "notes/other.md"},
         "source_refs": ["notes/test.md"], "justification": "j"}, ctx
    ).startswith("proposal #")
    # Review channel: errata → awaiting_review, the human adopts it.
    assert tool.call(
        {"type": "errata",
         "payload": {"contradiction": "the notes disagree",
                     "excerpts": [{"path": "notes/test.md", "quote": "A"},
                                  {"path": "notes/other.md", "quote": "B"}],
                     "candidate": "reading A", "rationale": "dates"},
         "source_refs": ["notes/test.md"], "justification": "j"}, ctx
    ).startswith("proposal #")
    for r in proposals.scan(db_conn):
        if r["status"] == "awaiting_review":
            proposals.advance(db_conn, vault_dir, r["id"], "adopted", actor="human")

    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 3
    assert summary["errors"] == []

    # Machine-zone artifacts only: file landings in index/, the edge DB-only.
    assert (vault_dir / "index" / "entities.md").is_file()
    assert (vault_dir / "index" / "errata" / "test.md").is_file()
    assert db_conn.execute("SELECT COUNT(*) FROM relatedness").fetchone()[0] == 1
    assert not (vault_dir / "archive").is_dir()

    assert _user_area(vault_dir) == before  # invariant 1


def test_machine_zone_absent_from_segments_after_flow(vault_dir, db_conn):
    """After the full flow lands index/* artifacts and re-indexing runs, no
    segment may carry a machine-zone path (invariant 6) — while the user
    notes remain searchable."""
    from server.vault.orchestration.indexer import ensure_indexed

    rec = proposals.create(
        db_conn, vault_dir, "entity_index", actor="consolidator", run_id="b",
        source_refs=["notes/test.md"], justification="j",
        payload={"entity": "e1", "attrs": {"f": "v"}},
    )
    proposals.advance(db_conn, vault_dir, rec["id"], "processing", actor="t")
    proposals.advance(db_conn, vault_dir, rec["id"], "adopted", actor="t")
    summary_rec = proposals.create(
        db_conn, vault_dir, "summary", actor="consolidator", run_id="b",
        source_refs=["notes/test.md"], justification="j",
        payload={"form": "cache", "body": "s"},
    )
    proposals.advance(db_conn, vault_dir, summary_rec["id"], "processing", actor="t")
    proposals.advance(db_conn, vault_dir, summary_rec["id"], "awaiting_review", actor="t")
    proposals.advance(db_conn, vault_dir, summary_rec["id"], "adopted", actor="human")
    assert run_executor(vault_dir, db_conn)["merged"] == 2

    # Machine-zone landings exist on disk; v2a (D25) the proposal channel
    # landed no file (the relpath is a synthetic card identifier).
    assert (vault_dir / "index" / "entities.md").is_file()
    assert (vault_dir / "index" / "summaries" / "test-summary.md").is_file()
    assert not (vault_dir / rec["relpath"]).is_file()

    ensure_indexed(db_conn, vault_dir)
    paths = {r[0] for r in db_conn.execute("SELECT DISTINCT path FROM segments")}
    assert "notes/test.md" in paths  # user notes fully indexed
    assert not any(p.startswith("index/") for p in paths)
    assert not any(p.startswith("suggestions/") for p in paths)
