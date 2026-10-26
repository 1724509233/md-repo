"""Tests for the archive proposal type (memory-runtime v2 sentinel channel).

Covers: authorization policy (auto), create/process round-trip, fresh move,
idempotent crash window, dest-exists conflict, missing-both failure,
system-dir guard, missing-path payload, end-to-end executor run.
"""
from __future__ import annotations

from server import proposals
from server.bgagent.executor import run_executor
from server.vault.db.changes import get_changes


def _adopted_archive(vault_dir, conn, path, justification="archiving"):
    """Create an archive proposal record and drive it to ``adopted`` via legal transitions."""
    rec = proposals.create(
        conn, vault_dir, "archive", actor="sentinel", run_id="batch-test",
        source_refs=[path], justification=justification,
        payload={"path": path},
    )
    proposals.advance(conn, vault_dir, rec["id"], "processing", actor="sentinel")
    proposals.advance(conn, vault_dir, rec["id"], "adopted", actor="sentinel")
    return rec


# ── proposals: authorization & lifecycle ───────────────────────────────────

def test_archive_authorization():
    assert proposals.authorization("archive") == "adopted"


def test_archive_create_roundtrip(vault_dir, db_conn):
    rec = proposals.create(
        db_conn, vault_dir, "archive", actor="sentinel", run_id="b",
        source_refs=["notes/test.md"], justification="archiving",
        payload={"path": "notes/test.md"},
    )
    assert rec["relpath"].startswith("suggestions/")
    assert rec["type"] == "archive"
    assert rec["status"] == "pending"
    assert rec["payload"] == {"path": "notes/test.md"}
    # auto type: reversible cold storage. No per-record reversibility flag
    # in the record world — the authorization tier IS the reversibility.
    assert "archive" in proposals.AUTO_TYPES


def test_archive_process_proposal_adopted(vault_dir, db_conn):
    rec = proposals.create(
        db_conn, vault_dir, "archive", actor="sentinel", run_id="b",
        source_refs=["notes/test.md"], justification="x",
        payload={"path": "notes/test.md"},
    )
    p = proposals.process_proposal(db_conn, vault_dir, rec["id"], actor="sentinel")
    assert p["status"] == "adopted"


# ── executor: fresh move ───────────────────────────────────────────────────

def test_archive_fresh_move(vault_dir, db_conn):
    src = vault_dir / "notes" / "old-doc.md"
    src.write_bytes(b"# Old Doc\n\nContent to archive.\n")

    rec = _adopted_archive(vault_dir, db_conn, "notes/old-doc.md")
    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 1
    assert summary["errors"] == []

    # Source gone, dest present with subpath preserved.
    assert not src.exists()
    dest = vault_dir / "archive" / "notes" / "old-doc.md"
    assert dest.is_file()
    assert dest.read_bytes() == b"# Old Doc\n\nContent to archive.\n"

    # Proposal merged.
    assert proposals.record(db_conn, rec["id"])["status"] == "merged"

    # Change log: move row with correct path/old_path/source.
    rows = [r for r in get_changes(db_conn, since=0)
            if r["source"] == "executor" and r["op"] == "move"]
    assert rows
    assert any(
        r["path"] == "archive/notes/old-doc.md"
        and r["from_path"] == "notes/old-doc.md"
        for r in rows
    )


# ── executor: idempotent crash window ──────────────────────────────────────

def test_archive_idempotent_crash_window(vault_dir, db_conn):
    # Source missing, dest pre-created: move already completed in a crashed cycle.
    dest = vault_dir / "archive" / "notes" / "old-doc.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"# Old Doc\n\nContent to archive.\n")
    # notes/old-doc.md does NOT exist

    rec = _adopted_archive(vault_dir, db_conn, "notes/old-doc.md")
    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 1
    assert summary["errors"] == []

    assert proposals.record(db_conn, rec["id"])["status"] == "merged"


# ── executor: dest exists AND source exists → failed ────────────────────────

def test_archive_both_exist_failed(vault_dir, db_conn):
    src = vault_dir / "notes" / "doc.md"
    src.write_bytes(b"# Doc\n")
    dest = vault_dir / "archive" / "notes" / "doc.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"# Different Doc\n")

    rec = _adopted_archive(vault_dir, db_conn, "notes/doc.md")
    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 0
    # _MergeFailed → failed state (not in errors list).
    p = proposals.record(db_conn, rec["id"])
    assert p["status"] == "failed"
    assert "destination" in p["reject_reason"]
    # Neither file was modified.
    assert src.read_bytes() == b"# Doc\n"
    assert dest.read_bytes() == b"# Different Doc\n"


# ── executor: source missing AND dest missing → failed ─────────────────────

def test_archive_missing_both_failed(vault_dir, db_conn):
    # notes/gone.md does not exist anywhere.
    rec = _adopted_archive(vault_dir, db_conn, "notes/gone.md")
    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 0
    p = proposals.record(db_conn, rec["id"])
    assert p["status"] == "failed"
    assert "notes/gone.md" in p["reject_reason"]


# ── executor: system-dir guard ─────────────────────────────────────────────

def test_archive_system_dir_failed(vault_dir, db_conn):
    # suggestions/x.md: proposals cannot be archived.
    (vault_dir / "suggestions").mkdir(exist_ok=True)
    (vault_dir / "suggestions" / "x.md").write_bytes(b"# X\n")
    rec = _adopted_archive(vault_dir, db_conn, "suggestions/x.md")
    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 0
    p = proposals.record(db_conn, rec["id"])
    assert p["status"] == "failed"
    assert "system path" in p["reject_reason"]
    # File untouched.
    assert (vault_dir / "suggestions" / "x.md").read_bytes() == b"# X\n"


def test_archive_system_dir_sync_failed(vault_dir, db_conn):
    # .sync/index.db: internals cannot be archived.
    rec = _adopted_archive(vault_dir, db_conn, ".sync/index.db")
    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 0
    p = proposals.record(db_conn, rec["id"])
    assert p["status"] == "failed"
    assert "system path" in p["reject_reason"]


# ── executor: payload without path → adopted + error ───────────────────────

def test_archive_no_path_payload_failed(vault_dir, db_conn):
    """A payload without ``path`` can never succeed; under the structural
    (ValueError) classification it is advanced to ``failed``, not retried
    forever (v2 archive's old design had treated it as retryable)."""
    (vault_dir / "notes" / "doc.md").write_bytes(b"# Doc\n")
    rec = proposals.create(
        db_conn, vault_dir, "archive", actor="sentinel", run_id="b",
        source_refs=["notes/doc.md"], justification="x",
        payload={"reason": "missing path key"},  # no 'path' key
    )
    proposals.advance(db_conn, vault_dir, rec["id"], "processing", actor="sentinel")
    proposals.advance(db_conn, vault_dir, rec["id"], "adopted", actor="sentinel")

    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 0
    assert summary["errors"] == []
    p = proposals.record(db_conn, rec["id"])
    assert p["status"] == "failed"
    assert "path" in p["reject_reason"]


# ── executor: end-to-end (archive + unrelated non-adopted) ─────────────────

def test_archive_run_executor_end_to_end(vault_dir, db_conn):
    src = vault_dir / "notes" / "to-archive.md"
    src.write_bytes(b"# To Archive\n")

    # Adopted archive proposal.
    rec_archive = _adopted_archive(vault_dir, db_conn, "notes/to-archive.md")

    # Unrelated pending summary record (not adopted → executor skips it).
    rec_pending = proposals.create(
        db_conn, vault_dir, "summary", actor="consolidator", run_id="b",
        source_refs=["notes/test.md"], justification="x",
        payload={"body": "x"},
    )

    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 1

    # Archive file moved.
    assert not src.exists()
    assert (vault_dir / "archive" / "notes" / "to-archive.md").is_file()

    # Archive record merged; pending summary record untouched.
    assert proposals.record(db_conn, rec_archive["id"])["status"] == "merged"
    assert proposals.record(db_conn, rec_pending["id"])["status"] == "pending"
