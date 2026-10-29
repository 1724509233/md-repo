"""Tests for server.recovery (the v1 real recovery drill)."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from server.recovery import main as recovery_main
from server.recovery import run_recovery_drill
from server.vault.db.schema import init_db
from server.vault.db.file_index import upsert_file

SAMPLE_CONTENT = b"# Test note\n\nSample content.\n"
SECONDARY_CONTENT = b"# Secondary note\n\nMore content here.\n"
SUGGESTION_CONTENT = b"# Suggestion\n\nA pending-review suggestion.\n"

# (vault-relative path, on-disk bytes) for every file the drill should index.
SEED_FILES = {
    "notes/test.md": SAMPLE_CONTENT,
    "notes/secondary.md": SECONDARY_CONTENT,
    "suggestions/pending.md": SUGGESTION_CONTENT,
}


def _seed_vault(vault_dir: Path) -> None:
    """Write the seed files (incl. a suggestions/ file) into the fixture vault.

    Binary writes keep the bytes exact (no newline translation) so hashes are
    stable across platforms.
    """
    for rel, content in SEED_FILES.items():
        fp = vault_dir / rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_bytes(content)


def _prepopulate_db(vault_dir: Path) -> None:
    """Create the DB, index every seed file, and plant a trigger_state and a
    recall_log row so the drill's 'accepted losses' are observable pre-wipe."""
    conn = init_db(vault_dir / ".sync" / "index.db")
    for rel, content in SEED_FILES.items():
        digest = hashlib.sha256(content).hexdigest()
        upsert_file(conn, rel, digest, (vault_dir / rel).stat().st_mtime)
    conn.execute(
        "INSERT INTO trigger_state (agent, armed, runs_date, runs_count) "
        "VALUES ('x', 1, '2026-10-20', 3)"
    )
    conn.execute(
        "INSERT INTO recall_log (ts, source, query, path, score, recall, traffic) "
        "VALUES (?, 'web', 'test', 'notes/test.md', 1.0, 'strict', 'human')",
        (1700000000.0,),
    )
    conn.commit()
    conn.close()


def _files_on_disk(vault_dir: Path) -> int:
    """Count of vault .md files the production files-index would hold:
    everything under the vault except .sync/ and .obsidian/ (suggestions/ and
    index/ included). Mirrors snapshot_watcher._full_scan's file set."""
    n = 0
    for f in vault_dir.rglob("*.md"):
        if ".sync" in f.parts or ".obsidian" in f.parts:
            continue
        if f.is_file():
            n += 1
    return n


def test_recovery_drill(vault_dir: Path) -> None:
    """Full drill on a seeded vault with a pre-populated DB."""
    _seed_vault(vault_dir)
    _prepopulate_db(vault_dir)

    # Sanity: the pre-wipe DB holds the planted rows (makes the loss observable).
    pre = sqlite3.connect(vault_dir / ".sync" / "index.db")
    assert pre.execute("SELECT count(*) FROM trigger_state").fetchone()[0] == 1
    assert pre.execute("SELECT count(*) FROM recall_log").fetchone()[0] == 1
    pre.close()

    report = run_recovery_drill(vault_dir)

    # A real backup was made (not a no-op).
    assert report["backup"] is not None
    backup = Path(report["backup"])
    assert backup.is_file()
    assert report["backup_size"] and report["backup_size"] > 0

    # Files index rebuilt to match disk: suggestions/ included, .sync/ excluded.
    expected = _files_on_disk(vault_dir)
    assert expected == 3
    assert report["files_rebuilt"] == expected
    assert report["files_expected"] == expected

    # FTS is healthy after the rebuild.
    assert report["fts_ok"] is True
    assert report["fts_sample"]

    # Accepted losses are observable as zero rows in the rebuilt DB.
    assert report["change_log_rows"] == 0
    assert report["trigger_state_rows"] == 0
    assert report["recall_log_rows"] == 0

    assert report["ok"] is True


def test_recovery_drill_no_db(vault_dir: Path) -> None:
    """Drill on a vault with no pre-existing DB: backup is a documented no-op,
    the rebuild still produces a verified, healthy index."""
    _seed_vault(vault_dir)

    report = run_recovery_drill(vault_dir)

    assert report["backup"] is None
    assert any("backup" in n for n in report["notes"])
    assert report["files_rebuilt"] == _files_on_disk(vault_dir)
    assert report["fts_ok"] is True
    assert report["ok"] is True


def test_recovery_cli_main(vault_dir: Path, capsys) -> None:
    """CLI smoke: main() prints parseable JSON and returns 0 on success."""
    _seed_vault(vault_dir)
    _prepopulate_db(vault_dir)

    rc = recovery_main(["drill", str(vault_dir)])
    out = capsys.readouterr().out

    report = json.loads(out)
    assert rc == 0
    assert report["ok"] is True
    assert report["files_rebuilt"] == _files_on_disk(vault_dir)
    assert report["change_log_rows"] == 0
