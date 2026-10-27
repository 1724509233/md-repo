"""Tests for the memory-console routes (memory-runtime).

Covers: the proposals list (shape, sort, age_days, trail summary — DB
records are the source; the response shape stays stable vs the v1
frontmatter list: ``reversible`` derives from the authorization tier,
``title`` from source_refs/type), the adopt/reject one-click review
endpoints (record-level state-machine transitions ledgered with actor
"human", path guards, reason threading), and the /api/memory/status
pulse (proposal counts, trigger_state agent panel incl. the
config-derived arm caps, the executor activity block, recall demand).
"""
from __future__ import annotations

import time
from datetime import datetime

from fastapi.testclient import TestClient

from server import proposals
from server.api import create_app
from server.vault.db.changes import get_changes, log_change
from tests.conftest import simulate_consumer


# ── helpers ────────────────────────────────────────────────────────────────

def _create(vault_dir, conn, type="entity_index", source_refs=None,
            now=None, actor="consolidator", run_id="batch-test"):
    return proposals.create(
        conn, vault_dir, type, actor=actor, run_id=run_id,
        source_refs=source_refs or ["notes/test.md"],
        justification="proposal body", payload=None, now=now,
    )


def _to_awaiting(vault_dir, conn, rec, actor="tester"):
    """Drive a pending review-type record to awaiting_review."""
    proposals.advance(conn, vault_dir, rec["id"], "processing", actor=actor)
    return proposals.advance(conn, vault_dir, rec["id"], "awaiting_review", actor=actor)


# ── GET /api/memory/proposals ──────────────────────────────────────────────

def test_list_shape_sort_age(client, vault_dir, db_conn):
    # awaiting_review (review type → summary), created now.
    r_ar = _create(vault_dir, db_conn, type="summary")
    _to_awaiting(vault_dir, db_conn, r_ar)
    # two pending proposals with distinct ages (tiebreak: age desc).
    r_old = _create(vault_dir, db_conn, type="entity_index",
                    now=time.time() - 5 * 86400)
    r_new = _create(vault_dir, db_conn, type="entity_index")
    # adopted (auto type → relatedness).
    r_adopted = _create(vault_dir, db_conn, type="relatedness")
    proposals.advance(db_conn, vault_dir, r_adopted["id"], "processing", actor="t")
    proposals.advance(db_conn, vault_dir, r_adopted["id"], "adopted", actor="t")

    resp = client.get("/api/memory/proposals")
    assert resp.status_code == 200
    items = resp.json()["proposals"]
    assert len(items) == 4

    # awaiting_review first.
    assert items[0]["relpath"] == r_ar["relpath"]
    assert items[0]["status"] == "awaiting_review"
    # within pending, older-before-newer (age desc).
    pend = [i["relpath"] for i in items if i["status"] == "pending"]
    assert pend == [r_old["relpath"], r_new["relpath"]]

    # field shape on a normal item.
    old_item = next(i for i in items if i["relpath"] == r_old["relpath"])
    assert old_item["status"] == "pending"
    assert old_item["type"] == "entity_index"
    assert old_item["actor"] == "consolidator"
    assert old_item["reversible"] is True  # auto type
    assert old_item["source_refs"] == ["notes/test.md"]
    assert old_item["title"] == "notes/test.md"  # derived: first source ref
    assert old_item["created"]  # ISO date string from created_at
    assert old_item["age_days"] == 5


def test_list_empty_when_no_records(client, vault_dir, db_conn):
    # Nothing in the proposals table (and no suggestions/ dir either).
    assert not (vault_dir / "suggestions").is_dir()
    resp = client.get("/api/memory/proposals")
    assert resp.status_code == 200
    assert resp.json() == {"proposals": []}


# ── POST .../adopt ─────────────────────────────────────────────────────────

def test_adopt_success_and_ledger(client, vault_dir, db_conn):
    rec = _create(vault_dir, db_conn, type="summary")
    _to_awaiting(vault_dir, db_conn, rec)
    rel = rec["relpath"]

    resp = client.post(f"/api/memory/proposals/{rel}/adopt")
    assert resp.status_code == 200
    assert resp.json() == {"relpath": rel, "status": "adopted"}
    assert proposals.record_by_relpath(db_conn, rel)["status"] == "adopted"

    # The adoption's ledger row lands via the watcher consumer.
    simulate_consumer(db_conn, vault_dir, rel, "modified")
    # Ledgered with the human actor (source="human", op="update").
    rows = get_changes(db_conn, since=0, path=rel, source="human")
    assert rows
    assert all(r["op"] == "update" for r in rows)


def test_adopt_wrong_status_and_bad_paths(client, vault_dir, db_conn):
    # pending → 400 (with the current status in the detail).
    rec = _create(vault_dir, db_conn, type="entity_index")
    resp = client.post(f"/api/memory/proposals/{rec['relpath']}/adopt")
    assert resp.status_code == 400
    assert "pending" in resp.json()["detail"]

    # bad paths → 400/404, never 500.
    assert client.post("/api/memory/proposals/../x/adopt").status_code in (400, 404)
    assert client.post("/api/memory/proposals//abs.md/adopt").status_code in (400, 404)
    assert client.post(
        "/api/memory/proposals/notes/test.md/adopt").status_code in (400, 404)
    assert client.post(
        "/api/memory/proposals/suggestions/sub/x.md/adopt").status_code in (400, 404)

    # missing record → 404 (v1: missing file → 404).
    assert client.post(
        "/api/memory/proposals/suggestions/nope.md/adopt").status_code == 404


# ── POST .../reject ────────────────────────────────────────────────────────

def test_reject_threads_reason(client, vault_dir, db_conn):
    rec = _create(vault_dir, db_conn, type="summary")
    _to_awaiting(vault_dir, db_conn, rec)
    rel = rec["relpath"]

    resp = client.post(
        f"/api/memory/proposals/{rel}/reject",
        json={"reason": "  summary is wrong  "},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "rejected"
    assert body["relpath"] == rel
    # Reason is stripped and threaded into the record.
    assert body["reject_reason"] == "summary is wrong"
    p = proposals.record_by_relpath(db_conn, rel)
    assert p["status"] == "rejected"
    assert p["reject_reason"] == "summary is wrong"
    # And the projection file was re-rendered with the new state.
    assert "status: rejected" in (vault_dir / rel).read_text(encoding="utf-8")


def test_reject_reason_required(client, vault_dir, db_conn):
    rec = _create(vault_dir, db_conn, type="summary")
    _to_awaiting(vault_dir, db_conn, rec)
    rel = rec["relpath"]

    assert client.post(
        f"/api/memory/proposals/{rel}/reject", json={}).status_code == 400
    assert client.post(
        f"/api/memory/proposals/{rel}/reject", json={"reason": "   "}
    ).status_code == 400
    # status unchanged after the failed rejects.
    assert proposals.record_by_relpath(db_conn, rel)["status"] == "awaiting_review"


def test_reject_wrong_status(client, vault_dir, db_conn):
    rec = _create(vault_dir, db_conn, type="entity_index")
    resp = client.post(
        f"/api/memory/proposals/{rec['relpath']}/reject", json={"reason": "nope"})
    assert resp.status_code == 400
    assert "pending" in resp.json()["detail"]


# ── GET /api/memory/status ─────────────────────────────────────────────────

def test_status_shape_and_counts(client, vault_dir, db_conn):
    # Seed records with distinct statuses.
    r_ar = _create(vault_dir, db_conn, type="summary")
    _to_awaiting(vault_dir, db_conn, r_ar)
    _create(vault_dir, db_conn, type="entity_index")

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
    # Records cannot be malformed (the DB row IS the state) — the key stays
    # in the shape for the poller with a zero count.
    assert by_status["malformed"] == 0
    assert by_status["merged"] == 0
    assert by_status["adopted"] == 0
    assert len(by_status) == 9
    assert data["proposals"]["awaiting_review"][0]["relpath"] == r_ar["relpath"]

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


# ── /api/memory/status: agents[] config-derived caps ──────────────────────

def test_status_agent_caps_from_bg_targets(vault_dir, db_conn):
    """bg_targets wired → armed_target/runs_cap take the config values; an
    alias absent from the config keeps null (no error, shape unchanged)."""
    today = datetime.now().strftime("%Y-%m-%d")
    db_conn.execute(
        "INSERT INTO trigger_state "
        "(agent, last_run_ts, armed, last_armed_scan, last_armed_seq, "
        "runs_date, runs_count) VALUES (?, ?, ?, 0.0, 0, ?, ?)",
        ("consolidator", 1234.0, 2, today, 1),
    )
    db_conn.execute(
        "INSERT INTO trigger_state "
        "(agent, last_run_ts, armed, last_armed_scan, last_armed_seq, "
        "runs_date, runs_count) VALUES (?, ?, ?, 0.0, 0, ?, ?)",
        ("narrator", 0.0, 0, "", 0),
    )
    db_conn.commit()

    app = create_app(
        vault_dir=vault_dir, db_conn=db_conn,
        bg_targets={"consolidator": {"armed_target": 3, "runs_cap": 3}},
    )
    resp = TestClient(app).get("/api/memory/status")
    assert resp.status_code == 200
    agents = {a["alias"]: a for a in resp.json()["agents"]}
    # Config entry present → the config values (the design's arm 2/3 · 1/3).
    assert agents["consolidator"]["armed_target"] == 3
    assert agents["consolidator"]["runs_cap"] == 3
    assert agents["consolidator"]["armed"] == 2
    assert agents["consolidator"]["runs_today"] == 1
    # No config entry → null, the pre-wiring shape.
    assert agents["narrator"]["armed_target"] is None
    assert agents["narrator"]["runs_cap"] is None


def test_status_agent_caps_null_without_bg_targets(vault_dir, db_conn):
    """create_app without bg_targets (default None) → the caps stay null:
    the pre-memory-console shape, backward compatible for existing callers
    (conftest's app fixture exercises the same path)."""
    today = datetime.now().strftime("%Y-%m-%d")
    db_conn.execute(
        "INSERT INTO trigger_state "
        "(agent, last_run_ts, armed, last_armed_scan, last_armed_seq, "
        "runs_date, runs_count) VALUES (?, 0.0, 0, 0.0, 0, ?, 0)",
        ("consolidator", today),
    )
    db_conn.commit()

    app = create_app(vault_dir=vault_dir, db_conn=db_conn)
    resp = TestClient(app).get("/api/memory/status")
    assert resp.status_code == 200
    agent = resp.json()["agents"][0]
    assert agent["armed_target"] is None
    assert agent["runs_cap"] is None
    # Shape unchanged: the same key set as when caps are filled.
    assert set(agent) == {"alias", "armed", "armed_target", "last_run",
                          "runs_today", "runs_cap"}


# ── /api/memory/status: executor block ────────────────────────────────────

def test_status_executor_block(client, vault_dir, db_conn):
    """The executor's write attribution lands in change_log with
    source='executor' (batch_id = the merged proposal's run_id); the block
    aggregates it: last op, today's op count (local date, same basis as
    runs_today), and the newest batch."""
    now = time.time()
    midnight = datetime.now().replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp()
    mid = midnight + (now - midnight) / 2  # earlier today, any hour of the day
    # A row from two days ago (strictly before local midnight) → excluded
    # from ops_today, and older rows don't set last_op_ts either.
    log_change(db_conn, "index/entities.md", "update", source="executor",
               ts=now - 2 * 86400, batch_id="batch-old")
    log_change(db_conn, "notes/a.md", "update", source="executor",
               ts=mid, batch_id="batch-mid")
    log_change(db_conn, "index/entities.md", "update", source="executor",
               ts=now, batch_id="batch-new")
    # A same-day non-executor row must not count.
    log_change(db_conn, "notes/b.md", "update", source="web", ts=now)
    db_conn.commit()

    resp = client.get("/api/memory/status")
    assert resp.status_code == 200
    ex = resp.json()["executor"]
    assert ex["last_op_ts"] == int(now)
    assert ex["ops_today"] == 2
    assert ex["last_batch"] == "batch-new"


def test_status_executor_block_empty(vault_dir, db_conn):
    """No executor rows → {last_op_ts: 0, ops_today: 0, last_batch: null}."""
    app = create_app(vault_dir=vault_dir, db_conn=db_conn)
    resp = TestClient(app).get("/api/memory/status")
    assert resp.status_code == 200
    assert resp.json()["executor"] == {
        "last_op_ts": 0, "ops_today": 0, "last_batch": None}


# ── /api/memory/proposals: trail summary ──────────────────────────────────

def test_list_trail_fields(client, vault_dir, db_conn):
    """List items carry a trail summary: the newest 3 change_log rows
    matching the /trail endpoint's match (path = the projection file OR
    batch_id = run_id) + the total match count. Records without ledger
    entries get []/0."""
    # Record A (run_id "batch-test", the _create helper's batch): 4
    # ledgered state transitions on the projection file + one executor
    # file merge carrying the batch id but a different path.
    rec_a = _create(vault_dir, db_conn, type="summary")
    proposals.advance(db_conn, vault_dir, rec_a["id"], "processing", actor="tester")
    simulate_consumer(db_conn, vault_dir, rec_a["relpath"], "modified")
    proposals.advance(db_conn, vault_dir, rec_a["id"], "awaiting_review", actor="tester")
    simulate_consumer(db_conn, vault_dir, rec_a["relpath"], "modified")
    resp = client.post(f"/api/memory/proposals/{rec_a['relpath']}/adopt")
    assert resp.status_code == 200  # +adopted row (source=human)
    simulate_consumer(db_conn, vault_dir, rec_a["relpath"], "modified")
    proposals.advance(db_conn, vault_dir, rec_a["id"], "merged", actor="executor")
    simulate_consumer(db_conn, vault_dir, rec_a["relpath"], "modified")
    # +the executor's file merge (same batch, other path) — what the
    # 30-minute loop ledgered via apply_change.
    log_change(db_conn, "index/entities.md", "update", source="executor",
               batch_id="batch-test")
    db_conn.commit()

    # Record B: a run of its own with no ledger rows → []/0 (the shared
    # "batch-test" batch must not leak into it).
    rec_b = _create(vault_dir, db_conn, type="entity_index", run_id="batch-quiet")

    resp = client.get("/api/memory/proposals")
    assert resp.status_code == 200
    items = {i["relpath"]: i for i in resp.json()["proposals"]}

    a = items[rec_a["relpath"]]
    assert a["trail_count"] == 5  # 4 state rows + 1 batch file row
    assert len(a["trail"]) == 3   # truncated to the newest 3
    assert [t["seq"] for t in a["trail"]] == sorted(
        (t["seq"] for t in a["trail"]), reverse=True)
    # /trail-endpoint row shape (no batch_id) — the batch file row is the
    # newest, the human adopt the oldest of the three.
    assert all(set(t) == {"seq", "ts", "source", "op", "path"} for t in a["trail"])
    assert a["trail"][0]["path"] == "index/entities.md"
    assert a["trail"][0]["source"] == "executor"
    assert a["trail"][2]["path"] == rec_a["relpath"]
    assert a["trail"][2]["source"] == "human"

    # Same basis as the drill endpoint: the list trail is its first rows.
    full = client.get(
        f"/api/memory/proposals/{rec_a['relpath']}/trail").json()["trail"]
    assert len(full) == 5
    assert [t["seq"] for t in a["trail"]] == [t["seq"] for t in full[:3]]

    b = items[rec_b["relpath"]]
    assert b["trail"] == []
    assert b["trail_count"] == 0
