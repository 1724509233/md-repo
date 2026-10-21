"""Tests for the memory-console routes (memory-runtime).

Covers: the proposals list (shape, sort, malformed handling, age_days), the
adopt/reject one-click review endpoints (state-machine transitions ledgered
with actor "human", path guards, reason threading), and the /api/memory/status
pulse (proposal counts, trigger_state agent panel, recall demand).
"""
from __future__ import annotations

import time
from datetime import datetime

from server import proposals
from server.vault.db.changes import get_changes


# ── helpers ────────────────────────────────────────────────────────────────

def _create(vault_dir, conn, type="entity_index", title="T", source_refs=None,
            now=None, actor="consolidator"):
    return proposals.create_proposal(
        vault_dir, type=type, actor=actor, run_id="batch-test",
        source_refs=source_refs or ["notes/test.md"], title=title,
        body="proposal body", now=now,
    )


def _to_awaiting(vault_dir, conn, rel, actor="tester"):
    """Drive a pending review-type proposal to awaiting_review."""
    proposals.advance(vault_dir, conn, rel, "processing", actor=actor)
    return proposals.advance(vault_dir, conn, rel, "awaiting_review", actor=actor)


def _write_malformed(vault_dir, name="2026-01-01_bogus_bad.md"):
    """Raw bad-frontmatter file (unclosed YAML) → parse failure."""
    fp = vault_dir / "suggestions" / name
    fp.write_bytes(b"---\nstatus: [unclosed\n---\nmalformed body\n")
    return f"suggestions/{name}"


# ── GET /api/memory/proposals ──────────────────────────────────────────────

def test_list_shape_sort_malformed_age(client, vault_dir, db_conn):
    # awaiting_review (review type → summary), created now.
    r_ar = _create(vault_dir, db_conn, type="summary", title="Review me")
    _to_awaiting(vault_dir, db_conn, r_ar)
    # two pending proposals with distinct ages (tiebreak: age desc).
    r_old = _create(vault_dir, db_conn, type="entity_index", title="Old",
                    now=time.time() - 5 * 86400)
    r_new = _create(vault_dir, db_conn, type="entity_index", title="New")
    # adopted (auto type → link).
    r_adopted = _create(vault_dir, db_conn, type="link", title="Adopted")
    proposals.advance(vault_dir, db_conn, r_adopted, "processing", actor="t")
    proposals.advance(vault_dir, db_conn, r_adopted, "adopted", actor="t")
    # one malformed file.
    malformed_rel = _write_malformed(vault_dir)

    resp = client.get("/api/memory/proposals")
    assert resp.status_code == 200
    items = resp.json()["proposals"]
    assert len(items) == 5

    # awaiting_review first.
    assert items[0]["relpath"] == r_ar
    assert items[0]["status"] == "awaiting_review"
    # within pending, newer-before-older is broken by age desc: older first.
    pend = [i["relpath"] for i in items if i["status"] == "pending"]
    assert pend == [r_old, r_new]
    # malformed last.
    assert items[-1]["status"] == "malformed"
    assert items[-1]["relpath"] == malformed_rel
    assert items[-1]["error"]

    # field shape on a normal item.
    old_item = next(i for i in items if i["relpath"] == r_old)
    assert old_item["status"] == "pending"
    assert old_item["type"] == "entity_index"
    assert old_item["actor"] == "consolidator"
    assert old_item["reversible"] is True  # auto type default
    assert old_item["source_refs"] == ["notes/test.md"]
    assert old_item["created"]  # ISO date string from the filename prefix
    assert old_item["age_days"] == 5


def test_list_empty_when_no_dir(client, vault_dir, db_conn):
    # suggestions/ not created yet.
    assert not (vault_dir / "suggestions").is_dir()
    resp = client.get("/api/memory/proposals")
    assert resp.status_code == 200
    assert resp.json() == {"proposals": []}


# ── POST .../adopt ─────────────────────────────────────────────────────────

def test_adopt_success_and_ledger(client, vault_dir, db_conn):
    # entity_index with a bad payload: adoptable by the API, but the executor
    # (the only thing that applies changes) will fail to merge it.
    rel = _create(vault_dir, db_conn, type="summary", title="Adopt me")
    _to_awaiting(vault_dir, db_conn, rel)

    resp = client.post(f"/api/memory/proposals/{rel}/adopt")
    assert resp.status_code == 200
    assert resp.json() == {"relpath": rel, "status": "adopted"}
    assert proposals.parse_proposal(vault_dir / rel)["status"] == "adopted"

    # Ledgered with the human actor (source="human", op="update").
    rows = get_changes(db_conn, since=0, path=rel, source="human")
    assert rows
    assert all(r["op"] == "update" for r in rows)


def test_adopt_wrong_status_and_bad_paths(client, vault_dir, db_conn):
    # pending → 400 (with the current status in the detail).
    r_pend = _create(vault_dir, db_conn, type="entity_index", title="Pending")
    resp = client.post(f"/api/memory/proposals/{r_pend}/adopt")
    assert resp.status_code == 400
    assert "pending" in resp.json()["detail"]

    # bad paths → 400/404, never 500.
    assert client.post("/api/memory/proposals/../x/adopt").status_code in (400, 404)
    assert client.post("/api/memory/proposals//abs.md/adopt").status_code in (400, 404)
    assert client.post("/api/memory/proposals/notes/test.md/adopt").status_code in (400, 404)
    assert client.post(
        "/api/memory/proposals/suggestions/sub/x.md/adopt").status_code in (400, 404)

    # missing file → 404.
    assert client.post(
        "/api/memory/proposals/suggestions/nope.md/adopt").status_code == 404


# ── POST .../reject ────────────────────────────────────────────────────────

def test_reject_threads_reason(client, vault_dir, db_conn):
    rel = _create(vault_dir, db_conn, type="summary", title="Reject me")
    _to_awaiting(vault_dir, db_conn, rel)

    resp = client.post(
        f"/api/memory/proposals/{rel}/reject",
        json={"reason": "  summary is wrong  "},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "rejected"
    assert body["relpath"] == rel
    # Reason is stripped and threaded into the frontmatter on disk.
    assert body["reject_reason"] == "summary is wrong"
    p = proposals.parse_proposal(vault_dir / rel)
    assert p["status"] == "rejected"
    assert p["reject_reason"] == "summary is wrong"


def test_reject_reason_required(client, vault_dir, db_conn):
    rel = _create(vault_dir, db_conn, type="summary", title="Need reason")
    _to_awaiting(vault_dir, db_conn, rel)

    assert client.post(
        f"/api/memory/proposals/{rel}/reject", json={}).status_code == 400
    assert client.post(
        f"/api/memory/proposals/{rel}/reject", json={"reason": "   "}
    ).status_code == 400
    # status unchanged after the failed rejects.
    assert proposals.parse_proposal(vault_dir / rel)["status"] == "awaiting_review"


def test_reject_wrong_status(client, vault_dir, db_conn):
    r_pend = _create(vault_dir, db_conn, type="entity_index", title="Pending")
    resp = client.post(
        f"/api/memory/proposals/{r_pend}/reject", json={"reason": "nope"})
    assert resp.status_code == 400
    assert "pending" in resp.json()["detail"]


# ── GET /api/memory/status ─────────────────────────────────────────────────

def test_status_shape_and_counts(client, vault_dir, db_conn):
    # Seed proposals with distinct statuses + one malformed.
    r_ar = _create(vault_dir, db_conn, type="summary", title="AR")
    _to_awaiting(vault_dir, db_conn, r_ar)
    r_pend = _create(vault_dir, db_conn, type="entity_index", title="PEND")
    _write_malformed(vault_dir)

    # Trigger-state row (inserted directly, as the scheduler would).
    today = datetime.now().strftime("%Y-%m-%d")
    db_conn.execute(
        "INSERT INTO trigger_state "
        "(agent, last_run_ts, armed, last_armed_scan, last_armed_seq, "
        "runs_date, runs_count) VALUES (?, ?, ?, 0.0, 0, ?, ?)",
        ("agent-x", 1234.0, 2, today, 5),
    )
    # Recall demand rows (inserted directly with old ts).
    now_ts = time.time()
    db_conn.execute(
        "INSERT INTO recall_queries "
        "(ts, source, traffic, query, recall, hits) "
        "VALUES (?, 'web', 'human', 'no results', 'strict', 0)",
        (now_ts - 2 * 86400,),
    )
    db_conn.execute(
        "INSERT INTO recall_log "
        "(ts, source, query, path, score, recall, traffic) "
        "VALUES (?, 'web', 'q', 'notes/a.md', 0.9, 'strict', 'human')",
        (now_ts - 3 * 86400,),
    )
    db_conn.execute(
        "INSERT INTO recall_log "
        "(ts, source, query, path, score, recall, traffic) "
        "VALUES (?, 'web', 'q', 'notes/b.md', 0.5, 'strict', 'human')",
        (now_ts - 10 * 86400,),
    )
    db_conn.commit()

    resp = client.get("/api/memory/status")
    assert resp.status_code == 200
    data = resp.json()

    # proposals: by_status counts + review queue.
    by_status = data["proposals"]["by_status"]
    assert by_status["awaiting_review"] == 1
    assert by_status["pending"] == 1
    assert by_status["malformed"] == 1
    # Stable shape: the full status set is always present (zero-count too).
    assert by_status["merged"] == 0
    assert by_status["adopted"] == 0
    assert len(by_status) == 9
    assert data["proposals"]["awaiting_review"][0]["relpath"] == r_ar

    # agents: the trigger_state row; config-derived caps unavailable → null.
    agent = data["agents"][0]
    assert agent["alias"] == "agent-x"
    assert agent["armed"] == 2
    assert agent["armed_target"] is None
    assert agent["last_run"] == 1234.0
    assert agent["runs_today"] == 5
    assert agent["runs_cap"] is None

    # recall: failed_7d + top docs over 30d.
    assert data["recall"]["failed_7d"] == 1
    assert data["recall"]["docs_tracked"] == 2
    assert [t["path"] for t in data["recall"]["top"]] == ["notes/a.md", "notes/b.md"]
    assert "recall_error" not in data["recall"]


def test_status_recall_table_dropped(client, vault_dir, db_conn):
    # Old DBs may lack the recall tables → zeros + recall_error, 200 overall.
    db_conn.execute("DROP TABLE recall_log")
    db_conn.execute("DROP TABLE recall_queries")
    db_conn.commit()

    resp = client.get("/api/memory/status")
    assert resp.status_code == 200
    recall = resp.json()["recall"]
    assert recall["failed_7d"] == 0
    assert recall["docs_tracked"] == 0
    assert recall["top"] == []
    assert recall["recall_error"]
