"""Tests for the relatedness ledger (reversible-automation v2, D9).

Machine-discovered relations live **only** in the ``relatedness`` DB table —
never in user files, never in notes. Pinned here:

- ``upsert_edge`` — insert; re-proposing the same LIVE pair refreshes in
  place (no pile-up); a pair that exists only as *ignored* rows gets a
  fresh live row (a human dismissed it, the agent re-proposed — each such
  event is a separate signal for the readback's ignored grouping).
- ``mark_ignored`` — the human's one-click dismissal is a DB flag, not a
  file change; the row is kept as telemetry.
- ``edges_for_actor`` — the readback consumer: live + ignored, newest first.
- Readback injection (spec §5.3 忽略→遥测→回读): the runner's context-head
  block shows the role's live edges + ignored pairs in the dedicated
  machine section — nothing is written into notes.
"""
from __future__ import annotations

import time

from server.bgagent import runner
from server.vault.db import relatedness as rel


def test_upsert_insert_and_refresh(db_conn):
    eid = rel.upsert_edge(db_conn, "notes/a.md", "notes/b.md", "same topic",
                          "consolidator", run_id="r1")
    assert db_conn.execute("SELECT COUNT(*) FROM relatedness").fetchone()[0] == 1

    # Re-proposing the same live pair: refreshed in place, still one row.
    eid2 = rel.upsert_edge(db_conn, "notes/a.md", "notes/b.md", "refined basis",
                           "consolidator", run_id="r2")
    assert eid2 == eid
    row = db_conn.execute("SELECT basis, run_id FROM relatedness WHERE id = ?",
                          (eid,)).fetchone()
    assert (row["basis"], row["run_id"]) == ("refined basis", "r2")
    assert db_conn.execute("SELECT COUNT(*) FROM relatedness").fetchone()[0] == 1


def test_ignored_pair_repropose_fresh_live_row(db_conn):
    eid = rel.upsert_edge(db_conn, "notes/a.md", "notes/b.md", "first",
                          "consolidator")
    assert rel.mark_ignored(db_conn, eid) is True
    assert rel.mark_ignored(db_conn, eid) is False  # already ignored

    # The human dismissed it; the agent re-proposes → a FRESH live row is
    # inserted (the ignored row stays as telemetry).
    eid2 = rel.upsert_edge(db_conn, "notes/a.md", "notes/b.md", "again",
                           "consolidator")
    assert eid2 != eid
    live = db_conn.execute(
        "SELECT COUNT(*) FROM relatedness WHERE ignored = 0").fetchone()[0]
    ignored = db_conn.execute(
        "SELECT COUNT(*) FROM relatedness WHERE ignored = 1").fetchone()[0]
    assert (live, ignored) == (1, 1)


def test_edges_for_actor_newest_first(db_conn):
    t0 = time.time()
    rel.upsert_edge(db_conn, "notes/a.md", "notes/b.md", "old", "role-x",
                    now=t0)
    rel.upsert_edge(db_conn, "notes/c.md", "notes/d.md", "new", "role-x",
                    now=t0 + 100)
    rel.upsert_edge(db_conn, "notes/e.md", "notes/f.md", "other actor", "role-y",
                    now=t0 + 50)

    out = rel.edges_for_actor(db_conn, "role-x")
    assert [e["source"] for e in out] == ["notes/c.md", "notes/a.md"]


def test_readback_relatedness_section(tmp_path, db_conn):
    """spec §5.3: the role's context head shows its live machine edges and
    the ignored-pair telemetry in the dedicated section — DB-only, bounded."""
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    t0 = time.time()
    rel.upsert_edge(db_conn, "notes/a.md", "notes/b.md", "same topic", "test-org",
                    now=t0)
    ignored_eid = rel.upsert_edge(db_conn, "notes/c.md", "notes/d.md", "nope",
                                  "test-org", now=t0 + 1)
    rel.mark_ignored(db_conn, ignored_eid)

    block = runner._readback_block("test-org")
    assert "## 相关性边（机器建议层 · 只进账本，不进笔记）" in block
    assert "notes/a.md → notes/b.md" in block
    assert "same topic" in block
    assert "notes/c.md → notes/d.md（忽略 ×1）" in block
    # The section is silent for a role with nothing to report.
    assert "相关性边" not in runner._readback_block("other-role")
