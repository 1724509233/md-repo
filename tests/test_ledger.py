"""Pin tests for the change ledger (server/services/ledger.py).

The ledger is the sole write surface for change_log + files (AGENTS.md
"Change ledger discipline"): writers register BEFORE writing (register) or
book the row synchronously (record); the watcher consumer (consume) books
exactly one correctly-attributed row; folder rows (record_folder) never
touch the registry.

Pinned:
- register → _pop returns the 3-tuple (source, batch_id, skip); the consumer
  passes both through to the change_log row.
- Unregistered / expired paths fall back to ("watchdog", "", False).
- record books its row synchronously (before any consume) and marks skip, so
  a later consumer books nothing (no double row).
- consume honors a registered move's from_path on the row.
- record_folder books a row without touching the registry.
"""
from __future__ import annotations

import time

from server.services import ledger


def test_register_pop_returns_tuple(tmp_path):
    ledger.register("a/b.md", "executor", tmp_path, batch_id="run-1")
    assert ledger._pop("a/b.md") == ("executor", "run-1", False)


def test_pop_unregistered_falls_back_watchdog():
    assert ledger._pop("nope.md") == ("watchdog", "", False)


def test_expired_entry_falls_back_watchdog(monkeypatch, tmp_path):
    # Shrink the TTL; an expired registration must no longer be
    # attributable (a late/duplicate event for a failed write must not
    # steal a source).
    monkeypatch.setattr(ledger, "_REGISTRY_TTL", 0.01)
    ledger.register("reg/stale.md", "agent", tmp_path)
    time.sleep(0.05)
    assert ledger._pop("reg/stale.md") == ("watchdog", "", False)


def test_record_books_row_synchronously(vault_dir, db_conn):
    """record() must land the change_log row synchronously — the row exists
    immediately, before any watcher consume (web UI: the console feed must
    respond instantly)."""
    (vault_dir / "notes" / "x.md").write_bytes(b"content\n")
    ledger.record(db_conn, vault_dir, "notes/x.md", "created", "web")
    rows = db_conn.execute(
        "SELECT source FROM change_log WHERE path = 'notes/x.md'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "web"


def test_record_skip_mark_consumed_by_consumer(vault_dir, db_conn):
    """record() marks skip=True in the shared registry; the consumer pops
    the mark like any normal entry (mechanism equivalence) but books
    NOTHING — no second row, no files-table work (the double-row fix)."""
    (vault_dir / "notes" / "y.md").write_bytes(b"content\n")
    ledger.record(db_conn, vault_dir, "notes/y.md", "created", "web")
    attribution = ledger.consume(db_conn, vault_dir, "notes/y.md", "created")
    assert attribution == ("web", "", True)
    assert db_conn.execute(
        "SELECT COUNT(*) FROM change_log WHERE path = 'notes/y.md'"
    ).fetchone()[0] == 1


def test_consumer_books_one_row_with_batch(vault_dir, db_conn):
    """Regression: the consumer books the row with the registered
    batch_id (the run stamp), not an empty string."""
    (vault_dir / "notes" / "z.md").write_bytes(b"content\n")
    ledger.register("notes/z.md", "executor", vault_dir, batch_id="run-9")
    attribution = ledger.consume(db_conn, vault_dir, "notes/z.md", "created")
    assert attribution == ("executor", "run-9", False)
    rows = [tuple(r) for r in db_conn.execute(
        "SELECT source, batch_id FROM change_log WHERE path = 'notes/z.md'"
    ).fetchall()]
    assert rows == [("executor", "run-9")]


def test_consumer_books_moved_with_from_path(vault_dir, db_conn):
    """A move: the writer registers only the destination; the watcher event
    carries from_path. The consumer must book the moved row WITH the
    from_path column."""
    (vault_dir / "new.md").write_bytes(b"content\n")
    ledger.register("new.md", "agent", vault_dir)
    attribution = ledger.consume(
        db_conn, vault_dir, "new.md", "moved", from_path="old.md")
    assert attribution == ("agent", "", False)
    rows = db_conn.execute(
        "SELECT old_path FROM change_log "
        "WHERE path = 'new.md' AND op = 'move'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "old.md"


def test_record_folder_books_row(vault_dir, db_conn):
    (vault_dir / "index" / "tmp").mkdir(parents=True)
    ledger.record_folder(db_conn, vault_dir, "index/tmp", "created", "web")
    rows = db_conn.execute(
        "SELECT source FROM change_log WHERE path = 'index/tmp'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "web"


def test_record_folder_does_not_register(vault_dir, db_conn):
    """Folder rows must never touch the registry (the watcher has no folder
    callbacks — no double-row risk): _pop stays the watchdog fallback
    before AND after record_folder."""
    assert ledger._pop("f") == ("watchdog", "", False)
    ledger.record_folder(db_conn, vault_dir, "f", "created", "web")
    assert ledger._pop("f") == ("watchdog", "", False)


def test_registry_residue_neutralized_between_boundaries(vault_dir, db_conn):
    """自证钉（卡 11a）：registry 清洗 fixture 有效且语义不伤。

    ① 进入任意测试时 `_registry` 恒空——上一测试遗留的未消费 skip 标记
    （跨测试残留形态，TTL 10s 内本会存活）已被 autouse fixture 中和；
    ② 清洗只斩跨测试残留：测试内 record→consume 的 skip 配对语义原样，
    清洗后同路径二次 record 照常 book 行。"""
    assert ledger._registry == {}          # ① 残留恒零（否则本钉红）

    (vault_dir / "notes" / "dup.md").write_bytes(b"v1\n")
    ledger.record(db_conn, vault_dir, "notes/dup.md", "created", "web")
    n1 = db_conn.execute(
        "SELECT count(*) FROM change_log WHERE path = 'notes/dup.md'"
    ).fetchone()[0]
    assert n1 == 1
    # 测试内配对原语义：skip 标记被 consume 弹出，记零行
    _, _, skip = ledger.consume(db_conn, vault_dir, "notes/dup.md", "modified")
    assert skip is True

    # 测试边界动作（=autouse fixture 每测前后所行）：
    ledger._registry.clear()
    # ② 清洗后同路径二次 record 照常 book
    (vault_dir / "notes" / "dup.md").write_bytes(b"v2\n")
    ledger.record(db_conn, vault_dir, "notes/dup.md", "modified", "web")
    n2 = db_conn.execute(
        "SELECT count(*) FROM change_log WHERE path = 'notes/dup.md'"
    ).fetchone()[0]
    assert n2 == 2
    assert db_conn.execute(
        "SELECT hash FROM files WHERE path = 'notes/dup.md'").fetchone() is not None
