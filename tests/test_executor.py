"""Tests for the deterministic executor (memory-runtime v1).

Covers: one happy path per merge handler, correction failure → failed,
no-handler type → errors, changelog source="executor", merged back-fill,
path-traversal guard, correction ambiguity guard, atomic vault writes.
"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path

from server import proposals
from server.bgagent.executor import run_executor
from server.vault.db.changes import get_changes
from server.vault.fs.vault_ops import atomic_write_bytes, safe_rel_path, _iter_vault_md


def _adopted(vault_dir, conn, type, source_refs=None, payload=None, title="T",
             body="body", reversible=None, now=None):
    """Create a proposal and drive it to ``adopted`` via legal transitions."""
    rel = proposals.create_proposal(
        vault_dir, type=type, actor="consolidator", run_id="batch-test",
        source_refs=source_refs or ["notes/test.md"], title=title, body=body,
        payload=payload, reversible=reversible, now=now,
    )
    proposals.advance(vault_dir, conn, rel, "processing", actor="consolidator")
    target = "adopted" if proposals.authorization(type) == "adopted" else "awaiting_review"
    proposals.advance(vault_dir, conn, rel, target, actor="consolidator")
    if target == "awaiting_review":
        proposals.advance(vault_dir, conn, rel, "adopted", actor="human")
    return rel


# ── entity_index ─────────────────────────────────────────────────────────

def test_entity_index_merge_and_upsert(vault_dir, db_conn):
    rel = _adopted(vault_dir, db_conn, "entity_index",
                   payload={"entity": "mirror-x", "value": "v0.9.2"})
    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 1
    assert summary["errors"] == []

    ent = (vault_dir / "index" / "entities.md").read_text(encoding="utf-8")
    assert "## mirror-x" in ent
    assert "v0.9.2" in ent

    p = proposals.parse_proposal(vault_dir / rel)
    assert p["status"] == "merged"
    assert p["merged_at"]

    # Re-merge with a new value: section replaced, not duplicated.
    rel2 = _adopted(vault_dir, db_conn, "entity_index",
                    payload={"entity": "mirror-x", "value": "v1.0"})
    other = _adopted(vault_dir, db_conn, "entity_index",
                     payload={"entity": "person-y", "value": "human"})
    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 2
    ent2 = (vault_dir / "index" / "entities.md").read_text(encoding="utf-8")
    assert ent2.count("## mirror-x") == 1
    assert "v1.0" in ent2 and "v0.9.2" not in ent2
    assert "## person-y" in ent2


def test_entity_index_bad_payload_errors(vault_dir, db_conn):
    rel = _adopted(vault_dir, db_conn, "entity_index", payload={"value": "no key"})
    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 0
    assert any("entity" in e for e in summary["errors"])
    # Stays adopted (retryable), not silently dropped.
    assert proposals.parse_proposal(vault_dir / rel)["status"] == "adopted"


# ── link ─────────────────────────────────────────────────────────────────

def test_link_merge_and_idempotent(vault_dir, db_conn):
    rel = _adopted(vault_dir, db_conn, "link",
                   source_refs=["notes/test.md"],
                   payload={"target": "notes/other.md"})
    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 1

    content = (vault_dir / "notes" / "test.md").read_text(encoding="utf-8")
    assert "<!-- related -->" in content
    assert "[[notes/other.md]]" in content

    # Second link proposal to the same target: merged, but no duplicate line.
    rel2 = _adopted(vault_dir, db_conn, "link",
                    source_refs=["notes/test.md"],
                    payload={"target": "notes/other.md"})
    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 1
    content2 = (vault_dir / "notes" / "test.md").read_text(encoding="utf-8")
    assert content2.count("[[notes/other.md]]") == 1


def test_link_missing_source_ref_errors(vault_dir, db_conn):
    rel = _adopted(vault_dir, db_conn, "link",
                   source_refs=["notes/absent.md"],
                   payload={"target": "notes/x.md"})
    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 0
    assert summary["errors"]
    assert proposals.parse_proposal(vault_dir / rel)["status"] == "adopted"


# ── summary ──────────────────────────────────────────────────────────────

def test_summary_merge(vault_dir, db_conn):
    rel = _adopted(vault_dir, db_conn, "summary", title="Decision D",
                   body="The decision body.", reversible=True)
    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 1

    out = vault_dir / "notes" / "test-summary.md"
    assert out.is_file()
    text = out.read_text(encoding="utf-8")
    assert "Decision D" in text
    assert "The decision body." in text
    assert proposals.parse_proposal(vault_dir / rel)["status"] == "merged"


# ── correction ───────────────────────────────────────────────────────────

def test_correction_merge(vault_dir, db_conn):
    (vault_dir / "notes" / "fix.md").write_bytes(
        b"# Doc\n\nold text here\n")
    rel = _adopted(vault_dir, db_conn, "correction",
                   payload={"file": "notes/fix.md", "old": "old text here",
                            "new": "new text"},
                   reversible=False)
    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 1
    content = (vault_dir / "notes" / "fix.md").read_bytes()
    assert b"new text" in content
    assert b"old text here" not in content
    assert proposals.parse_proposal(vault_dir / rel)["status"] == "merged"


def test_correction_old_missing_fails_not_merges(vault_dir, db_conn):
    (vault_dir / "notes" / "fix.md").write_bytes(b"# Doc\n\nnothing\n")
    rel = _adopted(vault_dir, db_conn, "correction",
                   payload={"file": "notes/fix.md", "old": "absent text",
                            "new": "x"},
                   reversible=False)
    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 0
    assert summary["errors"] == []  # typed failure: failed state, not error

    p = proposals.parse_proposal(vault_dir / rel)
    assert p["status"] == "failed"
    assert "absent" in p["reject_reason"]
    # Original text untouched.
    assert (vault_dir / "notes" / "fix.md").read_bytes() == b"# Doc\n\nnothing\n"


# ── reminder (no v1 handler) ─────────────────────────────────────────────

def test_reminder_no_handler_goes_to_errors(vault_dir, db_conn):
    rel = _adopted(vault_dir, db_conn, "reminder", title="Wake up",
                   body="at 07:00", reversible=False)
    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 0
    assert any("reminder" in e for e in summary["errors"])
    assert proposals.parse_proposal(vault_dir / rel)["status"] == "adopted"


# ── expiry + ledger ──────────────────────────────────────────────────────

def test_run_executor_expires_overdue(vault_dir, db_conn):
    now = time.time()
    # Backdate the filename date past the 14-day window, then leave it in
    # the review queue.
    rel = proposals.create_proposal(
        vault_dir, type="summary", actor="consolidator", run_id="b",
        source_refs=["notes/test.md"], title="Backdated", body="x",
        now=now - 20 * 86400)
    proposals.advance(vault_dir, db_conn, rel, "processing", actor="t")
    proposals.advance(vault_dir, db_conn, rel, "awaiting_review", actor="t")

    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 0
    assert summary["expired"] == 1
    assert proposals.parse_proposal(vault_dir / rel)["status"] == "expired"


def test_merge_ledgered_with_executor_source(vault_dir, db_conn):
    rel = _adopted(vault_dir, db_conn, "entity_index",
                   payload={"entity": "e1", "value": "v1"})
    run_executor(vault_dir, db_conn)
    rows = [r for r in get_changes(db_conn, since=0) if r["source"] == "executor"]
    assert rows
    # Both the target file change and the proposal state transition are ledgered.
    assert any(r["path"] == "index/entities.md" for r in rows)
    assert any(r["path"] == rel and r["op"] == "update" for r in rows)


def test_merge_artifact_rows_carry_batch_id(vault_dir, db_conn):
    # _adopted creates both proposals with run_id="batch-test"; after the
    # merge, the artifact file rows and the proposal state rows all carry it.
    rel_e = _adopted(vault_dir, db_conn, "entity_index",
                     payload={"entity": "batch-e", "value": "v1"})
    rel_s = _adopted(vault_dir, db_conn, "summary", title="Batch S",
                     body="body text")
    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 2
    for path in ("index/entities.md", "notes/test-summary.md", rel_e, rel_s):
        rows = [r[0] for r in db_conn.execute(
            "SELECT batch_id FROM change_log WHERE path = ?", (path,))]
        assert rows
        assert all(b == "batch-test" for b in rows)


# ── path traversal guard ─────────────────────────────────────────────────

def test_traversal_link_escape_parked_failed(vault_dir, db_conn):
    # "../escape.md" would land at the vault SIBLING if unguarded.
    decoy = vault_dir.parent / "escape.md"
    decoy.write_bytes(b"decoy\n")
    rel = _adopted(vault_dir, db_conn, "link",
                   source_refs=["../escape.md"],
                   payload={"target": "notes/x.md"})
    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 0
    assert summary["errors"] == []  # typed failure: failed state, not error

    p = proposals.parse_proposal(vault_dir / rel)
    assert p["status"] == "failed"
    assert p["reject_reason"]
    assert "illegal vault path" in p["reject_reason"]
    # Sibling decoy untouched, no new file created there.
    assert decoy.read_bytes() == b"decoy\n"


def test_traversal_correction_escape_parked_failed(vault_dir, db_conn):
    # "../../escape.md" would land above the vault if unguarded; the decoy
    # contains the correction target text so an unguarded run would rewrite it.
    decoy = vault_dir.parent.parent / "escape.md"
    if decoy.exists():
        decoy.unlink()
    decoy.write_bytes(b"# doc\nold text\n")
    try:
        rel = _adopted(vault_dir, db_conn, "correction",
                       payload={"file": "../../escape.md", "old": "old text",
                                "new": "new text"},
                       reversible=False)
        summary = run_executor(vault_dir, db_conn)
        assert summary["merged"] == 0
        assert summary["errors"] == []

        p = proposals.parse_proposal(vault_dir / rel)
        assert p["status"] == "failed"
        assert p["reject_reason"]
        assert "illegal vault path" in p["reject_reason"]
        assert decoy.read_bytes() == b"# doc\nold text\n"
    finally:
        decoy.unlink(missing_ok=True)


def test_traversal_summary_escape_parked_failed(vault_dir, db_conn):
    # "../notes/note.md" would land at a vault-sibling notes/ dir; the
    # derived summary would land there too.
    decoy_dir = vault_dir.parent / "notes"
    decoy_dir.mkdir(parents=True, exist_ok=True)
    decoy = decoy_dir / "note.md"
    decoy.write_bytes(b"# Note\n")
    rel = _adopted(vault_dir, db_conn, "summary", title="S",
                   source_refs=["../notes/note.md"])
    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 0
    assert summary["errors"] == []

    p = proposals.parse_proposal(vault_dir / rel)
    assert p["status"] == "failed"
    assert p["reject_reason"]
    assert "illegal vault path" in p["reject_reason"]
    # Sibling decoy dir: no new files, decoy unmodified.
    assert decoy.read_bytes() == b"# Note\n"
    assert sorted(f.name for f in decoy_dir.iterdir()) == ["note.md"]


def test_traversal_absolute_path_parked_failed(vault_dir, db_conn):
    rel = _adopted(vault_dir, db_conn, "correction",
                   payload={"file": "/etc/passwd", "old": "root", "new": "x"},
                   reversible=False)
    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 0
    assert summary["errors"] == []

    p = proposals.parse_proposal(vault_dir / rel)
    assert p["status"] == "failed"
    assert p["reject_reason"]
    assert "illegal vault path" in p["reject_reason"]


# ── correction ambiguity guard ───────────────────────────────────────────

def test_correction_ambiguous_old_parked_failed(vault_dir, db_conn):
    content = b"# Doc\n\nline X one\nline X two\n"
    (vault_dir / "notes" / "fix.md").write_bytes(content)
    before = hashlib.sha256(content).hexdigest()
    rel = _adopted(vault_dir, db_conn, "correction",
                   payload={"file": "notes/fix.md", "old": "X", "new": "Y"},
                   reversible=False)
    summary = run_executor(vault_dir, db_conn)
    assert summary["merged"] == 0
    assert summary["errors"] == []

    p = proposals.parse_proposal(vault_dir / rel)
    assert p["status"] == "failed"
    assert "2 occurrences" in p["reject_reason"]
    # No silent partial replace: target bytes unchanged.
    after = hashlib.sha256((vault_dir / "notes" / "fix.md").read_bytes()).hexdigest()
    assert before == after


# ── atomic vault writes ──────────────────────────────────────────────────

def test_atomic_write_bytes_content_lands_tmp_gone(vault_dir):
    fp = vault_dir / "notes" / "atomic.md"
    atomic_write_bytes(fp, b"hello")
    assert fp.read_bytes() == b"hello"
    assert not (vault_dir / "notes" / ".atomic.md.writing-tmp").exists()


def test_atomic_write_bytes_tmp_same_dir_dot_prefixed_non_md(vault_dir, monkeypatch):
    import os

    from server.vault.fs import vault_ops

    calls = []
    real_replace = os.replace

    def fake_replace(src, dst):
        calls.append((Path(src), Path(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", fake_replace)
    fp = vault_dir / "notes" / "atomic.md"
    vault_ops.atomic_write_bytes(fp, b"data")

    assert len(calls) == 1
    src, dst = calls[0]
    assert src.parent == dst.parent == fp.parent  # same directory => same volume
    assert src.name.startswith(".")               # dot-prefixed
    assert not src.name.endswith(".md")           # invisible to *.md walkers
    assert src == fp.with_name("." + fp.name + ".writing-tmp")
    assert fp.read_bytes() == b"data"


def test_stray_writing_tmp_inert_to_walkers(vault_dir):
    from client.sync import scan_local_files

    stray = vault_dir / "notes" / ".x.md.writing-tmp"
    stray.write_bytes(b"partial write")

    walked = [p.name for p in _iter_vault_md(vault_dir)]
    assert ".x.md.writing-tmp" not in walked
    assert "test.md" in walked  # normal files still visible

    scanned = scan_local_files(vault_dir)
    assert not any(k.endswith(".writing-tmp") for k in scanned)
    assert "notes/test.md" in scanned


# ── guard policy (shared vault-path guard) ───────────────────────────────

def test_safe_rel_path_policy():
    assert safe_rel_path("a/b.md") == Path("a/b.md")
    assert safe_rel_path("../x") is None
    assert safe_rel_path("/x") is None
    assert safe_rel_path("a/../b") is None
    assert safe_rel_path("") is None
