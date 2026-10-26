"""Tests for the retired ``archive`` verb (reversible-automation v2, D5).

In v1, ``archive`` was an auto-tier verb: the sentinel role proposed moving
a dormant note into ``archive/`` and the executor moved the file. In v2 the
three user-area placement/text verbs (link / correction / archive) are
**retired** — automation never moves or rewrites user files (constitution
iron rule: the user area gets zero automation writes). This file pins the
retirement semantics for ``archive``:

- write gate: ``proposals.create`` / ``proposals.authorization`` refuse
  retired types (the verb table is the write entry point);
- propose tool: operational redirect naming the replacement (the sentinel
  reports read-only — "N docs untouched for 90d, suggest archiving" — and
  proposes nothing), zero disk writes;
- read-path tolerance: legacy 存量 rows (link / correction / archive)
  predate the cutover and must be returned by the read paths (scan /
  record / records_by_run) without crashing — the read paths never
  validate row type;
- executor: a legacy ``adopted`` stock row is parked in ``failed`` with an
  actionable reason (no v2 handler, no migration — 存量提案人工处理).
"""
from __future__ import annotations

import json as _json
import time

from server import proposals
from server.agent.tools.propose import ProposeTool
from server.agent.tools.tool import ToolContext
from server.bgagent.executor import run_executor


def _ctx(vault_dir, verbs=None):
    return ToolContext(
        vault_dir=vault_dir, write_source="consolidator",
        run_id="consolidator-20261026T000000", verbs=verbs,
    )


def _planted_retired(conn, type, payload=None, source_refs=None, now=None):
    """Plant a legacy v1 存量 record directly in the DB as ``adopted``.

    ``proposals.create`` rejects retired types (the verb table is the write
    gate); these rows are the pre-cutover stock the spec says humans
    process (no migration).
    """
    now = now if now is not None else time.time()
    date = time.strftime("%Y-%m-%d", time.localtime(now))
    cur = conn.execute(
        """
        INSERT INTO proposals
            (type, actor, run_id, status, payload, source_refs, justification,
             reject_reason, created_at, updated_at, merged_at, relpath)
        VALUES (?, ?, ?, 'adopted', ?, ?, ?, '', ?, ?, '', '')
        """,
        (type, "sentinel", "legacy-run",
         _json.dumps(payload) if payload is not None else "",
         _json.dumps(source_refs or []), "legacy", now, now),
    )
    rid = cur.lastrowid
    conn.execute(
        "UPDATE proposals SET relpath = ? WHERE id = ?",
        (f"suggestions/{date}_{type}_p{rid}.md", rid),
    )
    return rid


# ── write gate: retired types are outside the verb table ─────────────────

def test_archive_authorization_raises():
    # v1: archive was auto (adopted). v2: outside the verb table entirely —
    # the dispatch refuses it (the propose tool redirects before reaching
    # this code path).
    import pytest
    with pytest.raises(ValueError):
        proposals.authorization("archive")


def test_archive_create_rejected_zero_writes(vault_dir, db_conn):
    import pytest
    before_files = sum(1 for p in vault_dir.rglob("*") if p.is_file())
    with pytest.raises(ValueError):
        proposals.create(
            db_conn, vault_dir, "archive", actor="sentinel", run_id="b",
            source_refs=["notes/test.md"], justification="archiving",
            payload={"path": "notes/test.md"},
        )
    assert db_conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 0
    assert sum(1 for p in vault_dir.rglob("*") if p.is_file()) == before_files
    assert not (vault_dir / "suggestions").is_dir()


# ── propose tool: operational redirect (read-only sentinel) ───────────────

def test_propose_archive_redirect_read_only(vault_dir, db_conn):
    """D5: archive → the model gets the operational redirect naming the
    read-only sentinel report, not a bare ban. Zero disk writes."""
    tool = ProposeTool()
    out = tool.call(
        {"type": "archive", "payload": {"path": "notes/test.md"},
         "source_refs": ["notes/test.md"], "justification": "dormant"},
        _ctx(vault_dir))
    assert out.startswith("Error:")
    assert "retired" in out
    assert "read-only" in out  # the replacement: sentinel reports, proposes nothing
    assert db_conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 0
    assert not (vault_dir / "suggestions").is_dir()
    assert not (vault_dir / "archive").is_dir()


# ── read-path tolerance: legacy 存量 rows never crash a read ──────────────

def test_legacy_rows_read_path_tolerance(vault_dir, db_conn):
    """The read paths (scan / record / records_by_run) never validate row
    type: the three retired v1 types are frozen 存量 and must surface in
    the console/counts exactly like any other row."""
    rid_link = _planted_retired(db_conn, "link",
                                payload={"target": "notes/x.md"},
                                source_refs=["notes/test.md"])
    rid_correction = _planted_retired(
        db_conn, "correction",
        payload={"file": "notes/test.md", "old": "a", "new": "b"},
        source_refs=["notes/test.md"])
    rid_archive = _planted_retired(db_conn, "archive",
                                   payload={"path": "notes/test.md"},
                                   source_refs=["notes/test.md"])

    rows = proposals.scan(db_conn)
    assert {r["id"] for r in rows} == {rid_link, rid_correction, rid_archive}
    assert {r["type"] for r in rows} == {"link", "correction", "archive"}

    for rid in (rid_link, rid_correction, rid_archive):
        rec = proposals.record(db_conn, rid)
        assert rec is not None and rec["status"] == "adopted"

    by_run = proposals.records_by_run(db_conn, "legacy-run", 0)
    assert {r["id"] for r in by_run} == {rid_link, rid_correction, rid_archive}


# ── executor: stock adopted rows park in failed, nothing moves ────────────

def test_legacy_archive_adopted_parked_no_moves(vault_dir, db_conn):
    """A pre-cutover ``archive`` record stuck at ``adopted``: the v2
    executor has no archive handler — it parks the row in ``failed`` with
    an actionable reason (human move). No file moves, no archive/ dir,
    user area byte-identical."""
    user_before = (vault_dir / "notes" / "test.md").read_bytes()
    rid = _planted_retired(db_conn, "archive",
                           payload={"path": "notes/test.md"},
                           source_refs=["notes/test.md"])

    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 0
    assert summary["errors"] == []  # parked: the human's queue, not ours

    p = proposals.record(db_conn, rid)
    assert p["status"] == "failed"
    assert "retired" in p["reject_reason"]
    assert "human move" in p["reject_reason"]

    assert (vault_dir / "notes" / "test.md").read_bytes() == user_before
    assert not (vault_dir / "archive").is_dir()
    # The projection was re-rendered on the transition (system zone).
    assert (vault_dir / p["relpath"]).is_file()


def test_retired_park_reasons_name_replacements(vault_dir, db_conn):
    """Each retired verb's park reason names its v2 replacement path —
    the human sees what to do, not just that the verb is gone."""
    rid_link = _planted_retired(db_conn, "link",
                                payload={"target": "notes/x.md"},
                                source_refs=["notes/test.md"])
    rid_correction = _planted_retired(
        db_conn, "correction",
        payload={"file": "notes/test.md", "old": "a", "new": "b"},
        source_refs=["notes/test.md"])
    run_executor(vault_dir, db_conn)
    reason_link = proposals.record(db_conn, rid_link)["reject_reason"]
    reason_correction = proposals.record(db_conn, rid_correction)["reject_reason"]
    assert "relatedness" in reason_link
    assert "errata" in reason_correction
