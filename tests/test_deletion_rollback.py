"""Deletion recovery by actor separation (spec 2026-11-15): the workbench
(web + interactive agent, write_source = web/agent) deletes via the trash
with NO snapshot; the non-workbench (bgagent alias + mcp) deletes via direct
unlink + snapshot with NO trash. The two lifecycles never cross on the same
file.

Covers:

- web delete: into the trash, no snapshot row, change_log row WITH pre_hash.
- agent (interactive) delete: same (write_source="agent").
- bgagent delete (write_source=alias): no trash, unlink, snapshot row,
  pre_hash row, batch_id=run_id; two files in one run = ONE call
  (call_id == run_id — run-level batch restore).
- mcp delete (write_source="mcp"): no trash, unlink, snapshot row, single
  file single call (no run_id → a fresh call per file).
- snapshot-side failure semantics (unlink fail → discard; record fail →
  file gone but the snapshot is rollback-able).
- sync deleted_hashes derived from change_log delete rows (stale →
  to_delete; local recreate → to_push; external delete without pre_hash →
  to_delete; recreate still to_push after the 30-day window — pre_hash is
  unbounded; latest row per path wins).
- refs liveness check: target on disk → 0 files modified.
- GC expiry boundary: remove_dead_links with liveness check (live target →
  zero strip) + per-run cap.
- rollback delete-restore re-extracts referrer graph edges (G1).
- rollback route books op "restore" (NOT "created"; arm regression).

Test files are written in binary mode (AGENTS.md: Windows newline
translation would break hash equality).
"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path

import pytest

from server import snapshots
from server.agent import ToolContext, execute_tool
from server.ledger import consume
from server.vault.db.changes import (
    count_changes_sources,
    get_changes,
    log_change,
)
from server.vault.db.graph_sql import graph_get_backlinks
from server.vault.orchestration.indexer import graph_extract_file
from server.vault.orchestration.refs import remove_dead_links


SAMPLE = b"# Test note\n\nSample content.\n"  # == notes/test.md in the vault_dir fixture


def _call_id_for(ledger_conn, path: str) -> str:
    """The snapshot call_id of the latest capture for one path."""
    row = ledger_conn.execute(
        "SELECT call_id FROM snapshots WHERE path = ? ORDER BY id DESC LIMIT 1",
        (path,),
    ).fetchone()
    assert row is not None, f"no snapshot row for {path}"
    return row["call_id"]


def _snapshot_count(ledger_conn, path: str) -> int:
    return ledger_conn.execute(
        "SELECT count(*) FROM snapshots WHERE path = ?", (path,)
    ).fetchone()[0]


# ── workbench: web + agent delete = trash, no snapshot ────────────────────


def test_web_delete_trash_no_snapshot_and_pre_hash_row(client, vault_dir, db_conn,
                                                       ledger_conn):
    (vault_dir / "notes" / "target.md").write_bytes(b"# Target")
    (vault_dir / "notes" / "ref.md").write_bytes(b"See [[notes/target]] for info.")
    pre_hash = hashlib.sha256(b"# Target").hexdigest()

    r = client.post("/api/docs/manage",
                    json={"action": "delete", "path": "notes/target.md"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["deleted_count"] == 1
    assert body["batch"]  # the trash batch
    # The file is in the trash (the workbench's recovery channel).
    bdir = vault_dir / ".trash" / body["batch"]
    assert (bdir / "notes" / "target.md").read_bytes() == b"# Target"
    assert not (vault_dir / "notes" / "target.md").exists()
    # No snapshot row (actor separation: the workbench is trash-only).
    assert _snapshot_count(ledger_conn, "notes/target.md") == 0
    # Referrer content untouched (trash restore is fully reversible).
    assert (vault_dir / "notes" / "ref.md").read_bytes() == b"See [[notes/target]] for info."
    # change_log row carries the pre-delete hash (sync lane, spec §2).
    rows = [c for c in get_changes(db_conn) if c["path"] == "notes/target.md"]
    assert len(rows) == 1
    assert rows[0]["op"] == "delete"
    assert rows[0]["pre_hash"] == pre_hash


def test_web_folder_delete_trash_no_snapshots(client, vault_dir, db_conn,
                                              ledger_conn):
    sub = vault_dir / "notes" / "sub"
    (sub / "deep").mkdir(parents=True)
    (sub / "a.md").write_bytes(b"# A")
    (sub / "deep" / "b.md").write_bytes(b"# B")
    r = client.post("/api/docs/manage", json={"action": "delete", "path": "notes/sub"})
    assert r.status_code == 200 and r.json()["deleted_count"] == 2
    batch = r.json()["batch"]
    assert (vault_dir / ".trash" / batch / "notes" / "sub" / "a.md").is_file()
    assert (vault_dir / ".trash" / batch / "notes" / "sub" / "deep" / "b.md").is_file()
    assert not (vault_dir / "notes" / "sub").exists()
    # No snapshot rows (workbench = trash only).
    assert ledger_conn.execute(
        "SELECT count(*) FROM snapshots WHERE path LIKE 'notes/sub/%'"
    ).fetchone()[0] == 0
    # Per-file rows, each with its pre_hash. The folder-level
    # record_folder row (path "notes/sub", same op) is excluded.
    rows = {c["path"]: c for c in get_changes(db_conn)
            if c["op"] == "delete" and c["path"].endswith(".md")}
    assert set(rows) == {"notes/sub/a.md", "notes/sub/deep/b.md"}
    assert rows["notes/sub/a.md"]["pre_hash"] == hashlib.sha256(b"# A").hexdigest()
    assert rows["notes/sub/deep/b.md"]["pre_hash"] == hashlib.sha256(b"# B").hexdigest()


def test_agent_delete_trash_no_snapshot_and_pre_hash_row(vault_dir, db_conn,
                                                         ledger_conn):
    (vault_dir / "notes" / "del.md").write_bytes(b"agent delete me")
    pre_hash = hashlib.sha256(b"agent delete me").hexdigest()
    result = execute_tool("delete_doc", {"path": "notes/del.md"},
                          ToolContext(vault_dir=vault_dir, run_id="run-x"))
    assert "Moved to trash" in result
    assert "restorable via the trash UI" in result
    assert not (vault_dir / "notes" / "del.md").exists()
    # No snapshot row (the interactive agent IS the workbench = trash only).
    assert _snapshot_count(ledger_conn, "notes/del.md") == 0
    rows = [c for c in get_changes(db_conn) if c["path"] == "notes/del.md"]
    assert len(rows) == 1
    assert rows[0]["op"] == "delete"
    assert rows[0]["source"] == "agent"
    assert rows[0]["pre_hash"] == pre_hash
    # batch_id = run_id (the trash batch attribution).
    batch_id = db_conn.execute(
        "SELECT batch_id FROM change_log WHERE path = 'notes/del.md'"
    ).fetchone()[0]
    assert batch_id == "run-x"
    # The watcher flush books nothing (record marked skip).
    consume(db_conn, vault_dir, "notes/del.md", "deleted")
    assert len([c for c in get_changes(db_conn) if c["path"] == "notes/del.md"]) == 1


# ── non-workbench: bgagent + mcp delete = unlink + snapshot, no trash ─────


def test_bgagent_delete_snapshot_side_and_run_level_call(vault_dir, db_conn,
                                                         ledger_conn):
    (vault_dir / "notes" / "x.md").write_bytes(b"x")
    (vault_dir / "notes" / "y.md").write_bytes(b"y")
    r1 = execute_tool("delete_doc", {"path": "notes/x.md"},
                      ToolContext(vault_dir=vault_dir, write_source="nightly",
                                  run_id="run-1"))
    r2 = execute_tool("delete_doc", {"path": "notes/y.md"},
                      ToolContext(vault_dir=vault_dir, write_source="nightly",
                                  run_id="run-1"))
    assert "Deleted:" in r1 and "restorable via rollback within 30 days" in r1
    assert "Deleted:" in r2
    # No trash (the non-workbench channel never touches it).
    assert not (vault_dir / ".trash").exists()
    assert not (vault_dir / "notes" / "x.md").exists()
    assert not (vault_dir / "notes" / "y.md").exists()
    # Snapshot rows: actor = alias, run-level call grouping (Q1: two files
    # in one run = ONE call → the batch restore).
    rows = [dict(r) for r in ledger_conn.execute(
        "SELECT * FROM snapshots WHERE op = 'delete' ORDER BY path")]
    assert len(rows) == 2
    by_path = {r["path"]: r for r in rows}
    assert by_path["notes/x.md"]["actor"] == "nightly"
    assert by_path["notes/x.md"]["run_id"] == "run-1"
    assert by_path["notes/x.md"]["call_id"] == "run-1"
    assert by_path["notes/y.md"]["call_id"] == "run-1"
    # One call batch: rollback restores both files at once.
    result = snapshots.rollback(ledger_conn, vault_dir, "run-1")
    assert result["ok"] is True and result["restored"] == ["notes/x.md", "notes/y.md"]
    # change_log rows carry pre_hash + batch_id.
    hx = db_conn.execute(
        "SELECT pre_hash, batch_id FROM change_log "
        "WHERE path = 'notes/x.md' AND op = 'delete'"
    ).fetchone()
    assert hx[0] == hashlib.sha256(b"x").hexdigest()
    assert hx[1] == "run-1"


def test_mcp_delete_single_file_single_call(vault_dir, db_conn, ledger_conn):
    (vault_dir / "notes" / "m.md").write_bytes(b"mcp delete me")
    result = execute_tool("delete_doc", {"path": "notes/m.md"},
                          ToolContext(vault_dir=vault_dir, write_source="mcp"))
    assert "Deleted:" in result and "restorable via rollback within 30 days" in result
    assert not (vault_dir / ".trash").exists()
    assert not (vault_dir / "notes" / "m.md").exists()
    # mcp has no run_id → capture generates a fresh call per file: single
    # file single call.
    row = dict(ledger_conn.execute(
        "SELECT * FROM snapshots WHERE path = 'notes/m.md'").fetchone())
    assert row["op"] == "delete" and row["actor"] == "mcp"
    assert row["run_id"] == ""
    assert row["call_id"]  # generated per file (not the empty run_id)
    # Rollback restores the bytes.
    result = snapshots.rollback(ledger_conn, vault_dir, row["call_id"])
    assert result["ok"] is True
    assert (vault_dir / "notes" / "m.md").read_bytes() == b"mcp delete me"


def test_snapshot_side_unlink_failure_discards_snapshot(vault_dir, db_conn,
                                                        ledger_conn, monkeypatch):
    (vault_dir / "notes" / "gone.md").write_bytes(b"precious")
    real_unlink = Path.unlink

    def flaky(self, missing_ok=False):
        if self.name == "gone.md" and ".sync" not in self.parts:
            raise OSError("simulated unlink failure")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", flaky)
    result = execute_tool("delete_doc", {"path": "notes/gone.md"},
                          ToolContext(vault_dir=vault_dir, write_source="mcp"))
    assert result.startswith("Error:")
    # File intact; the capture was discarded (no snapshot residue).
    assert (vault_dir / "notes" / "gone.md").read_bytes() == b"precious"
    assert _snapshot_count(ledger_conn, "notes/gone.md") == 0
    # No ledger row (record never ran).
    assert [c for c in get_changes(db_conn) if c["path"] == "notes/gone.md"] == []


def test_snapshot_side_record_failure_leaves_snapshot_recoverable(vault_dir,
                                                                  db_conn,
                                                                  ledger_conn,
                                                                  monkeypatch):
    """record fails → the file is gone but the snapshot is live: rollback
    recovers it (the snapshot IS the backstop — no half state)."""
    (vault_dir / "notes" / "gone.md").write_bytes(b"precious")
    monkeypatch.setattr(
        "server.ledger.record",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("simulated db failure")),
    )
    # The tool does not catch record failures — the error surfaces, but the
    # disk + snapshot state is the recovery credential.
    with pytest.raises(RuntimeError):
        execute_tool("delete_doc", {"path": "notes/gone.md"},
                     ToolContext(vault_dir=vault_dir, write_source="mcp"))
    assert not (vault_dir / "notes" / "gone.md").exists()
    # The snapshot row is in-flight (finalize never ran — record raised
    # first): live=0/finalized=0, but GC never reclaims in-flight rows
    # (P1-B1) and by_call does not filter on live → it stays rollback-able.
    row = dict(ledger_conn.execute(
        "SELECT * FROM snapshots WHERE path = 'notes/gone.md'").fetchone())
    assert row["op"] == "delete"
    assert (row["live"], row["finalized"]) == (0, 0)
    # Rollback restores the bytes (the backstop works).
    result = snapshots.rollback(ledger_conn, vault_dir, row["call_id"])
    assert result["ok"] is True
    assert (vault_dir / "notes" / "gone.md").read_bytes() == b"precious"


# ── sync: deleted_hashes from change_log delete rows (spec §3) ────────────


def test_sync_stale_copy_after_snapshot_side_delete(client, vault_dir, db_conn,
                                                    ledger_conn):
    """The resurrection-hole regression: a snapshot-side delete (bgagent/mcp)
    NEVER enters the trash — if the sync derived its pre-hash map from the
    trash, the client's stale copy would be judged to_push and push the file
    back. The change_log-derived map keeps it in to_delete."""
    pre_hash = hashlib.sha256(SAMPLE).hexdigest()
    assert "Deleted:" in execute_tool(
        "delete_doc", {"path": "notes/test.md"},
        ToolContext(vault_dir=vault_dir, write_source="mcp"))
    assert not (vault_dir / ".trash").exists()  # the hole's premise
    # Client still holds the identical local copy → to_delete, not to_push.
    r = client.post("/api/sync", json={
        "device_name": "mac",
        "local_files": {"notes/test.md": pre_hash},
        "logs": [],
        "last_sync": 1.0,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["to_delete"] == ["notes/test.md"]
    assert body["to_push"] == []


def test_sync_local_recreate_after_delete_stays_to_push(client, vault_dir,
                                                        db_conn, ledger_conn):
    assert "Deleted:" in execute_tool(
        "delete_doc", {"path": "notes/test.md"},
        ToolContext(vault_dir=vault_dir, write_source="mcp"))
    # Local copy rewritten after the deletion (hash differs from pre_hash)
    # → a genuine new file, pushed normally.
    r = client.post("/api/sync", json={
        "device_name": "mac",
        "local_files": {"notes/test.md": "rewritten-hash"},
        "logs": [],
        "last_sync": 1.0,
    })
    body = r.json()
    assert body["to_push"] == ["notes/test.md"]
    assert body["to_delete"] == []


def test_sync_external_delete_without_pre_hash_goes_to_delete(client, vault_dir,
                                                              db_conn):
    # External delete: no pre_hash on the row (the watcher channel does not
    # compute one) → always stale.
    (vault_dir / "notes" / "ext.md").write_bytes(b"external")
    (vault_dir / "notes" / "ext.md").unlink()
    from server import ledger

    ledger.record(db_conn, vault_dir, "notes/ext.md", "deleted", "watchdog")
    r = client.post("/api/sync", json={
        "device_name": "mac",
        "local_files": {"notes/ext.md": "whatever"},
        "logs": [],
        "last_sync": 1.0,
    })
    body = r.json()
    assert body["to_delete"] == ["notes/ext.md"]
    assert body["to_push"] == []


def test_sync_recreate_still_push_after_30_day_window(client, vault_dir, db_conn):
    """D5: the pre_hash map derives from change_log (unbounded), NOT from a
    30-day-lifetime store — a client offline >30 days that recreates the
    path must still push, not be deleted."""
    old_ts = time.time() - 40 * 86400
    pre_hash = hashlib.sha256(b"old content").hexdigest()
    log_change(db_conn, "notes/old.md", "deleted", source="web",
               pre_hash=pre_hash, ts=old_ts)
    # Recreated locally with different content → to_push.
    r = client.post("/api/sync", json={
        "device_name": "mac",
        "local_files": {"notes/old.md": "new-content-hash"},
        "logs": [],
        "last_sync": 1.0,
    })
    assert r.json()["to_push"] == ["notes/old.md"]
    # Identical to the pre-delete content → stale copy → to_delete.
    r = client.post("/api/sync", json={
        "device_name": "mac",
        "local_files": {"notes/old.md": pre_hash},
        "logs": [],
        "last_sync": 1.0,
    })
    assert r.json()["to_delete"] == ["notes/old.md"]


def test_sync_latest_delete_row_wins(client, vault_dir, db_conn, ledger_conn):
    """Two deletes of the same path: the LATEST row's pre_hash wins."""
    h1 = hashlib.sha256(SAMPLE).hexdigest()
    assert "Deleted:" in execute_tool(
        "delete_doc", {"path": "notes/test.md"},
        ToolContext(vault_dir=vault_dir, write_source="mcp"))
    # Restore + rewrite + delete again (newer pre_hash).
    (vault_dir / "notes" / "test.md").write_bytes(b"rewritten")
    h2 = hashlib.sha256(b"rewritten").hexdigest()
    assert "Deleted:" in execute_tool(
        "delete_doc", {"path": "notes/test.md"},
        ToolContext(vault_dir=vault_dir, write_source="mcp"))
    # Local copy identical to the NEWER pre-delete content → stale → to_delete.
    r = client.post("/api/sync", json={
        "device_name": "mac",
        "local_files": {"notes/test.md": h2},
        "logs": [],
        "last_sync": 1.0,
    })
    assert r.json()["to_delete"] == ["notes/test.md"]
    # Local copy identical to the OLDER pre-delete content → differs from
    # the winning (latest) pre_hash → a rewrite → to_push.
    r = client.post("/api/sync", json={
        "device_name": "mac",
        "local_files": {"notes/test.md": h1},
        "logs": [],
        "last_sync": 1.0,
    })
    assert r.json()["to_push"] == ["notes/test.md"]


# ── refs liveness check (spec §4) ─────────────────────────────────────────


def test_remove_dead_links_live_target_strips_nothing(vault_dir, db_conn):
    """The target is back on disk (recreated after the deletion) → 0 files
    modified, nothing stripped (safe for any caller)."""
    (vault_dir / "notes" / "target.md").write_bytes(b"# Target")
    (vault_dir / "notes" / "ref.md").write_bytes(b"See [[notes/target]] for info.")
    graph_extract_file(db_conn, vault_dir, "notes/ref.md")
    count = remove_dead_links(db_conn, vault_dir, "notes/target.md")
    assert count == 0
    assert (vault_dir / "notes" / "ref.md").read_bytes() == b"See [[notes/target]] for info."


# ── GC expiry boundary: remove_dead_links + liveness + cap (spec §5) ──────


def test_gc_deadlink_expired_absent_target_strips(vault_dir, db_conn, ledger_conn):
    now = time.time()
    (vault_dir / "notes" / "t.md").write_bytes(b"original")
    (vault_dir / "notes" / "ref.md").write_bytes(b"See [[notes/t]] for info.")
    # Snapshot-side deletion (mcp): GC IS its link-cleanup boundary.
    snap = snapshots.capture(ledger_conn, vault_dir, "notes/t.md", "delete",
                             "mcp", now=now)
    (vault_dir / "notes" / "t.md").unlink()
    snapshots.finalize(ledger_conn, snap, "")
    # The GC cleanup path set derives from change_log (the snapshot row is
    # reclaimed by the same sweep — rediscovery is by design).
    log_change(db_conn, "notes/t.md", "deleted", source="mcp", ts=now)
    assert snapshots.gc(ledger_conn, vault_dir, now=now + 31 * 86400) == 1
    # The deferred cleanup ran at the expiry boundary: the dead link is
    # stripped from the referrer.
    assert b"[[notes/t]]" not in (vault_dir / "notes" / "ref.md").read_bytes()


def test_gc_deadlink_workbench_delete_not_stripped(vault_dir, db_conn,
                                                   ledger_conn):
    """Regression (actor separation): a WORKBENCH deletion (source web/agent)
    older than 30 days whose trash batch the user has NOT purged must keep
    its referrers' link text intact — the trash purge is its cleanup
    boundary, and trash restore is content-reversible: link text GC stripped
    first cannot be re-extracted on restore (the file would come back with
    its backlinks permanently lost)."""
    now = time.time()
    old_ts = now - 40 * 86400
    (vault_dir / "notes" / "t.md").write_bytes(b"original")
    (vault_dir / "notes" / "ref.md").write_bytes(b"See [[notes/t]] for info.")
    # Mirrors the legacy dual-channel rows: a snapshot row (reclaimed by
    # the sweep) + a workbench change_log row past the retention window.
    snap = snapshots.capture(ledger_conn, vault_dir, "notes/t.md", "delete",
                             "web", now=old_ts)
    (vault_dir / "notes" / "t.md").unlink()
    snapshots.finalize(ledger_conn, snap, "")
    log_change(db_conn, "notes/t.md", "deleted", source="web", ts=old_ts)
    assert snapshots.gc(ledger_conn, vault_dir, now=now) == 1
    # The snapshot row was reclaimed, but the referrer's link text is
    # intact — the trash purge (not GC) cleans workbench deletions.
    assert snapshots.live_preimage(ledger_conn, "notes/t.md") is None
    assert b"[[notes/t]]" in (vault_dir / "notes" / "ref.md").read_bytes()


def test_gc_deadlink_live_target_strips_nothing(vault_dir, db_conn, ledger_conn):
    """Liveness check (spec §4): the file was recreated before the GC
    expiry → remove_dead_links returns 0, nothing is stripped (safe for
    any caller)."""
    now = time.time()
    (vault_dir / "notes" / "t.md").write_bytes(b"original")
    (vault_dir / "notes" / "ref.md").write_bytes(b"See [[notes/t]] for info.")
    # Snapshot-side deletion (mcp): GC IS its link-cleanup boundary.
    snap = snapshots.capture(ledger_conn, vault_dir, "notes/t.md", "delete",
                             "mcp", now=now)
    (vault_dir / "notes" / "t.md").unlink()
    snapshots.finalize(ledger_conn, snap, "")
    # The GC cleanup path set derives from change_log (see the sibling
    # test) — the liveness pre-check must skip this live path.
    log_change(db_conn, "notes/t.md", "deleted", source="mcp", ts=now)
    # The user recreates the file before the window closes.
    (vault_dir / "notes" / "t.md").write_bytes(b"rebuilt")
    assert snapshots.gc(ledger_conn, vault_dir, now=now + 31 * 86400) == 1
    assert b"[[notes/t]]" in (vault_dir / "notes" / "ref.md").read_bytes()


def test_gc_deadlink_cap_respected(vault_dir, db_conn, ledger_conn):
    """Cost cap (spec §5): at most GC_DEADLINK_CAP expired delete paths are
    cleaned per GC run — the rest wait for the next GC."""
    now = time.time()
    for i in range(6):
        (vault_dir / f"g{i}.md").write_bytes(f"content {i}".encode())
    ref = b"".join(f"[[g{i}]] ".encode() for i in range(6))
    (vault_dir / "ref.md").write_bytes(ref)
    for i in range(6):
        # Snapshot-side deletions (mcp): GC IS their link-cleanup boundary.
        snap = snapshots.capture(ledger_conn, vault_dir, f"g{i}.md", "delete",
                                 "mcp", now=now)
        (vault_dir / f"g{i}.md").unlink()
        snapshots.finalize(ledger_conn, snap, "")
        # The GC cleanup path set derives from change_log (persistent —
        # the snapshot rows are reclaimed by the first sweep, so the
        # second sweep must rediscover the remainder from here).
        log_change(db_conn, f"g{i}.md", "deleted", source="mcp", ts=now)
    assert snapshots.gc(ledger_conn, vault_dir, now=now + 31 * 86400) == 6
    content = (vault_dir / "ref.md").read_bytes().decode()
    stripped = sum(1 for i in range(6) if f"[[g{i}]]" not in content)
    assert stripped == snapshots.GC_DEADLINK_CAP  # 5 of 6 this run
    # The next GC cleans the remainder (change_log rediscovery; the
    # already-clean paths re-scan to a no-op and consume no cap slot).
    assert snapshots.gc(ledger_conn, vault_dir, now=now + 31 * 86400) == 0
    content = (vault_dir / "ref.md").read_bytes().decode()
    assert all(f"[[g{i}]]" not in content for i in range(6))


# ── rollback: delete-restore re-extracts referrer edges (G1, spec §6) ─────


def test_rollback_delete_restore_revives_backlinks(client, vault_dir, db_conn,
                                                   ledger_conn):
    (vault_dir / "notes" / "a.md").write_bytes(b"# A\n\nSee [[notes/b]] for info.\n")
    (vault_dir / "notes" / "b.md").write_bytes(b"# B\n\nBody text.\n")
    graph_extract_file(db_conn, vault_dir, "notes/a.md")
    graph_extract_file(db_conn, vault_dir, "notes/b.md")
    assert [x["path"] for x in graph_get_backlinks(db_conn, "notes/b.md")] == ["notes/a.md"]

    assert "Deleted:" in execute_tool(
        "delete_doc", {"path": "notes/b.md"},
        ToolContext(vault_dir=vault_dir, write_source="mcp"))
    # The deletion removed the referrer→target edge (and the entity).
    assert graph_get_backlinks(db_conn, "notes/b.md") == []

    call_id = _call_id_for(ledger_conn, "notes/b.md")
    r = client.post("/api/memory/rollback", json={"call_id": call_id})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "restored": ["notes/b.md"]}
    # Bytes restored exactly (pre-image).
    assert (vault_dir / "notes" / "b.md").read_bytes() == b"# B\n\nBody text.\n"
    # G1: the referrer→target edge is re-extracted — the backlink revives.
    assert [x["path"] for x in graph_get_backlinks(db_conn, "notes/b.md")] == ["notes/a.md"]


def test_rollback_delete_restore_content_scan_fallback(client, vault_dir, db_conn,
                                                       ledger_conn):
    """The edges table is NOT empty after the delete (other.md keeps a tag
    edge), so the edges lookup finds nothing for the restored target and
    the O(vault) content-scan fallback must find the referrer (design
    §11-4)."""
    (vault_dir / "notes" / "a.md").write_bytes(b"# A\n\nSee [[notes/b]].\n")
    (vault_dir / "notes" / "b.md").write_bytes(b"# B\n\nBody text.\n")
    (vault_dir / "other.md").write_bytes(b"---\ntags: [keep]\n---\n# Other\n")
    graph_extract_file(db_conn, vault_dir, "notes/a.md")
    graph_extract_file(db_conn, vault_dir, "notes/b.md")
    graph_extract_file(db_conn, vault_dir, "other.md")
    assert db_conn.execute("SELECT count(*) FROM edges").fetchone()[0] > 0

    assert "Deleted:" in execute_tool(
        "delete_doc", {"path": "notes/b.md"},
        ToolContext(vault_dir=vault_dir, write_source="mcp"))
    assert graph_get_backlinks(db_conn, "notes/b.md") == []
    # The edges table is still non-empty (other.md's tag edge survives).
    assert db_conn.execute("SELECT count(*) FROM edges").fetchone()[0] > 0

    call_id = _call_id_for(ledger_conn, "notes/b.md")
    r = client.post("/api/memory/rollback", json={"call_id": call_id})
    assert r.status_code == 200
    assert [x["path"] for x in graph_get_backlinks(db_conn, "notes/b.md")] == ["notes/a.md"]


# ── rollback route: synchronous "restore" rows (spec §6) ──────────────────


def test_rollback_route_books_restore_op_not_created(client, vault_dir, db_conn,
                                                     ledger_conn):
    assert "Deleted:" in execute_tool(
        "delete_doc", {"path": "notes/test.md"},
        ToolContext(vault_dir=vault_dir, write_source="mcp"))
    call_id = _call_id_for(ledger_conn, "notes/test.md")
    r = client.post("/api/memory/rollback", json={"call_id": call_id})
    assert r.status_code == 200
    rows = [c for c in get_changes(db_conn) if c["path"] == "notes/test.md"]
    ops = [c["op"] for c in rows]
    assert "restore" in ops
    assert "created" not in ops  # the old register+watcher path booked created
    restore_rows = [c for c in rows if c["op"] == "restore"]
    assert len(restore_rows) == 1
    assert restore_rows[0]["source"] == "web"
    # Arm regression: the bgagent arm surface (ops=("create",)) must not see
    # the human restore (restore is deliberately excluded).
    assert count_changes_sources(db_conn, ops=("create",)) == 0
    # The files table row is back (upserted by the restore branch).
    assert (vault_dir / "notes" / "test.md").read_bytes() == SAMPLE


def test_rollback_route_books_delete_for_create_op(client, vault_dir, db_conn,
                                                   ledger_conn):
    # F7：create-op 回滚 = unlink。路由必须记 op='delete'（旧行为记 'restore'
    # → apply_change 查盘无文件 → 零行，且 skip 标记压制 watcher → 幽灵行）。
    from server import snapshots as snap_mod
    from tests.conftest import simulate_consumer

    (vault_dir / "notes").mkdir(exist_ok=True)
    fp = vault_dir / "notes" / "created.md"
    fp.write_bytes(b"new content")
    snap = snap_mod.capture(ledger_conn, vault_dir, "notes/created.md", "create", "agent")
    snap_mod.finalize(ledger_conn, snap, snap_mod.hash_bytes(b"new content"))

    r = client.post("/api/memory/rollback", json={"call_id": snap["call_id"]})
    assert r.status_code == 200
    assert not fp.exists()
    rows = {(row["path"], row["op"], row["source"]) for row in db_conn.execute(
        "SELECT path, op, source FROM change_log")}
    assert ("notes/created.md", "delete", "web") in rows
    assert ("notes/created.md", "restore", "web") not in rows
    # 恰好一行：skip 标记压制后续 watcher 事件
    simulate_consumer(db_conn, vault_dir, "notes/created.md", "deleted")
    assert db_conn.execute(
        "SELECT count(*) FROM change_log WHERE path='notes/created.md'"
    ).fetchone()[0] == 1


def test_rollback_route_books_move_pair(client, vault_dir, db_conn, ledger_conn):
    # F7：move-op 回滚 = 旧路径 restore + 新路径 delete。
    from server import snapshots as snap_mod

    (vault_dir / "notes").mkdir(exist_ok=True)
    (vault_dir / "notes" / "a.md").write_bytes(b"payload")
    snap = snap_mod.capture(ledger_conn, vault_dir, "notes/a.md", "move", "agent",
                            new_path="notes/b.md")
    (vault_dir / "notes" / "a.md").rename(vault_dir / "notes" / "b.md")
    snap_mod.finalize(ledger_conn, snap, snap_mod.hash_bytes(b"payload"))

    r = client.post("/api/memory/rollback", json={"call_id": snap["call_id"]})
    assert r.status_code == 200
    assert (vault_dir / "notes" / "a.md").is_file()
    assert not (vault_dir / "notes" / "b.md").exists()
    rows = {(row["path"], row["op"]) for row in db_conn.execute(
        "SELECT path, op FROM change_log WHERE source='web'")}
    assert ("notes/a.md", "restore") in rows
    assert ("notes/b.md", "delete") in rows


def test_rollback_move_reverses_refs_and_relatedness(vault_dir, db_conn, ledger_conn):
    # F8：move 的回滚必须逆转 move 时的两处副作用——referrer 正文改写
    # （[[a]]→[[b]]）与 relatedness 路径改写（D19）。
    from server import ledger, snapshots as snap_mod
    from server.vault.db.relatedness import merge_edge

    (vault_dir / "notes").mkdir(exist_ok=True)
    (vault_dir / "notes" / "a.md").write_bytes(b"a content")
    (vault_dir / "notes" / "referrer.md").write_bytes(b"see [[notes/a.md]] here")
    ledger.record(db_conn, vault_dir, "notes/a.md", "created", "agent")
    ledger.record(db_conn, vault_dir, "notes/referrer.md", "created", "agent")
    merge_edge(ledger_conn, "notes/a.md", "notes/referrer.md",
               basis="b", actor="t", run_id="r")

    # move 调用的真实时序：capture（旧路径仍在盘）→ rename → record（同步
    # 记账，refs/relatedness 在此改写）→ finalize。
    snap = snap_mod.capture(ledger_conn, vault_dir, "notes/a.md", "move", "agent",
                            new_path="notes/b.md")
    (vault_dir / "notes" / "a.md").rename(vault_dir / "notes" / "b.md")
    ledger.record(db_conn, vault_dir, "notes/b.md", "moved", "agent",
                  from_path="notes/a.md")
    snap_mod.finalize(ledger_conn, snap,
                      snap_mod.hash_bytes((vault_dir / "notes" / "b.md").read_bytes()))
    assert b"[[notes/b.md]]" in (vault_dir / "notes" / "referrer.md").read_bytes()
    assert ledger_conn.execute(
        "SELECT source FROM relatedness").fetchone()["source"] == "notes/b.md"

    result = snap_mod.rollback(ledger_conn, vault_dir, snap["call_id"])
    assert result["ok"]
    assert b"[[notes/a.md]]" in (vault_dir / "notes" / "referrer.md").read_bytes()
    assert ledger_conn.execute(
        "SELECT source FROM relatedness").fetchone()["source"] == "notes/a.md"
