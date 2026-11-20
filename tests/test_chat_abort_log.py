"""chat_abort_log storage layer (channel-2 spec
2026-11-20-stop-signals-reasoning-capture §2/§3/§5/§8 tests 1-2).

Anchors:

1. schema (§8-1): ``_LEDGER_DDL`` ships chat_abort_log + the ts index —
   ``ensure_ledger_tables`` idempotent, ATTACH prefix semantics ("" /
   "ledger.", family of test_ledger_migration.py);
2. upgrade path (§8-2): an old ledger.db that predates the table upgrades
   on next boot — no conflict, existing authoritative rows untouched;
3. redline: the index-side DDL never grows the table (authoritative data
   only in _LEDGER_DDL; the sessions-promotion C1 lesson);
4. column shape: name/type/nullability pinned per column — message_id /
   tool_name nullable, ts / session_id / phase / model NOT NULL (reverse-
   pinned via constraint violations, per the r9 finalized shape: no extra
   columns);
5. write face: record() INSERTs immediately, full readback; fail-open —
   any injected exception never propagates and leaves a WARNING trace
   carrying session_id / message_id context (§3, review L1 level pinned);
6. retention (r7): rows kept forever — the module ships no gc_sweep.
"""
from __future__ import annotations

import inspect
import logging
import sqlite3
import time
from pathlib import Path

import pytest

from server.vault.db import chat_abort_records
from server.vault.db.migrate_ledger import migrate_ledger
from server.vault.db.schema import ensure_ledger_tables, init_db, init_ledger_db

ABORT_COLS = {"id", "ts", "session_id", "message_id", "phase", "tool_name",
              "model"}


# ── helpers ─────────────────────────────────────────────────────────────────

def _tables(conn: sqlite3.Connection, schema: str = "main") -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            f"SELECT name FROM {schema}.sqlite_master WHERE type = 'table'"
        ).fetchall()
    }


def _record(conn: sqlite3.Connection, **overrides) -> None:
    """record() with a full valid row; overrides replace single fields."""
    fields = dict(
        session_id="s1",
        message_id="a1b2c3d4e5f6",
        ts=time.time(),
        phase="text_output",
        tool_name=None,
        model="qwen2.5:14b",
    )
    fields.update(overrides)
    chat_abort_records.record(conn, **fields)


class _ExplodingConn:
    """Proxy that fails every execute() — the fail-open injection seam
    (sqlite3.Connection instances reject attribute patching)."""

    def execute(self, *args, **kwargs):
        raise RuntimeError("injected INSERT failure")

    def commit(self) -> None:  # pragma: no cover - must not be reached
        pass


# ── §8-1 schema: table created, shape pinned, idempotent, prefix ───────────

def test_chat_abort_log_table_created(ledger_conn):
    """Fresh ledger.db gets the table + the ts index; re-running
    ensure_ledger_tables is a no-op (CREATE IF NOT EXISTS idempotency)."""
    assert "chat_abort_log" in _tables(ledger_conn)
    ensure_ledger_tables(ledger_conn)
    ensure_ledger_tables(ledger_conn)
    assert "chat_abort_log" in _tables(ledger_conn)
    idx = {r[0] for r in ledger_conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index'"
        " AND tbl_name = 'chat_abort_log'"
    ).fetchall()}
    assert "idx_chat_abort_log_ts" in idx


def test_chat_abort_log_column_shape_pinned(ledger_conn):
    """r9 finalized shape: exactly these columns, per-column nullability
    pinned — message_id / tool_name nullable, the rest NOT NULL; id is
    AUTOINCREMENT (provenance anchor: ids never reused)."""
    info = {r[1]: r for r in ledger_conn.execute(
        "PRAGMA table_info(chat_abort_log)"
    ).fetchall()}
    assert set(info) == ABORT_COLS
    notnull = {name for name, r in info.items() if r[3] and name != "id"}
    assert notnull == {"ts", "session_id", "phase", "model"}
    assert {info[c][2] for c in ("session_id", "message_id", "phase",
                                 "tool_name", "model")} == {"TEXT"}
    assert info["ts"][2] == "REAL"
    assert info["id"][5] == 1  # pk
    ddl = ledger_conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table'"
        " AND name = 'chat_abort_log'"
    ).fetchone()[0]
    assert "AUTOINCREMENT" in ddl


def test_attach_prefix_semantics(tmp_path):
    """prefix="ledger." lands the table + index in the ATTACHed ledger
    schema, never in main (the migrate_ledger transaction's call shape)."""
    index_conn = init_db(tmp_path / "index.db")
    ledger_path = tmp_path / "ledger.db"
    try:
        index_conn.execute("ATTACH DATABASE ? AS ledger", (str(ledger_path),))
        ensure_ledger_tables(index_conn, "ledger.")
        assert "chat_abort_log" in _tables(index_conn, "ledger")
        assert "chat_abort_log" not in _tables(index_conn, "main")
        idx = index_conn.execute(
            "SELECT count(*) FROM ledger.sqlite_master WHERE type = 'index'"
            " AND name = 'idx_chat_abort_log_ts'"
        ).fetchone()[0]
        assert idx == 1
        # writable through the qualified name
        index_conn.execute(
            "INSERT INTO ledger.chat_abort_log "
            "(ts, session_id, phase, model) VALUES (1.0, 's', 'pre_dispatch', 'm')"
        )
        assert index_conn.execute(
            "SELECT count(*) FROM ledger.chat_abort_log"
        ).fetchone()[0] == 1
        index_conn.execute("DETACH DATABASE ledger")
    finally:
        index_conn.close()


# ── redline: the index side never grows the table ───────────────────────────

def test_index_db_never_gets_chat_abort_log(tmp_path):
    """Authoritative-only table: init_db across restarts must never create
    chat_abort_log in index.db (mirror of the sessions C1 pin)."""
    p = tmp_path / "index.db"
    for _ in range(3):
        conn = init_db(p)
        assert "chat_abort_log" not in _tables(conn)
        conn.close()
    src = inspect.getsource(init_db)
    assert "chat_abort_log" not in src


# ── §8-2 upgrade path: old ledger.db gains the table, no conflict ───────────

def test_old_ledger_db_upgrade_gains_table(tmp_path):
    """An old ledger.db (predates chat_abort_log, carries authoritative
    rows) re-runs ensure on next boot: the table appears, existing rows
    untouched."""
    p = tmp_path / "ledger.db"
    conn = init_ledger_db(p)
    conn.execute("DROP TABLE chat_abort_log")  # rewind to the pre-feature shape
    conn.execute(
        "INSERT INTO sessions (id, messages, token_count, last_active, created_at)"
        " VALUES ('s1', '[]', 0, 1.0, 1.0)"
    )
    conn.commit()
    conn.close()

    conn = init_ledger_db(p)  # the upgrade boot
    assert "chat_abort_log" in _tables(conn)
    assert conn.execute(
        "SELECT count(*) FROM sessions WHERE id = 's1'"
    ).fetchone()[0] == 1
    conn.close()


def test_first_boot_migration_with_table_present(tmp_path):
    """migrate_ledger (which re-ensures the ledger DDL inside its
    transaction) must not trip over the new table — the pre-split index.db
    never has it, the ledger file gains it."""
    index_path = tmp_path / ".sync" / "index.db"
    index_conn = init_db(index_path)
    assert "chat_abort_log" not in _tables(index_conn)
    ledger_path = tmp_path / ".sync" / "ledger.db"
    report = migrate_ledger(index_conn, ledger_path)
    assert all(v == "skipped" for v in report.values())  # nothing to move
    lconn = sqlite3.connect(ledger_path)
    lconn.row_factory = sqlite3.Row
    assert "chat_abort_log" in _tables(lconn)
    lconn.close()
    index_conn.close()


# ── write face: record() ────────────────────────────────────────────────────

def test_record_lands_row_full_readback(ledger_conn):
    """record() INSERTs immediately (no buffering): every column reads back
    exactly; message_id / tool_name may be NULL (the un-anchored shape)."""
    ts = time.time()
    _record(ledger_conn, ts=ts)
    rows = ledger_conn.execute("SELECT * FROM chat_abort_log").fetchall()
    assert len(rows) == 1
    r = dict(rows[0])
    assert r["ts"] == ts
    assert r["session_id"] == "s1"
    assert r["message_id"] == "a1b2c3d4e5f6"
    assert r["phase"] == "text_output"
    assert r["tool_name"] is None
    assert r["model"] == "qwen2.5:14b"
    assert isinstance(r["id"], int)

    # un-anchored row (outer CancelledError / blank stop shape)
    _record(ledger_conn, message_id=None, phase="pre_dispatch")
    rows = ledger_conn.execute(
        "SELECT * FROM chat_abort_log ORDER BY id"
    ).fetchall()
    assert len(rows) == 2
    assert dict(rows[1])["message_id"] is None


def test_record_tool_exec_carries_tool_name(ledger_conn):
    _record(ledger_conn, phase="tool_exec", tool_name="write_doc")
    r = dict(ledger_conn.execute(
        "SELECT * FROM chat_abort_log").fetchone())
    assert r["phase"] == "tool_exec"
    assert r["tool_name"] == "write_doc"


def test_not_null_columns_reverse_pinned(ledger_conn):
    """Constraint violations on the four NOT NULL columns (raw INSERT —
    record() would swallow them): the DDL rejects NULLs."""
    base = dict(ts=1.0, session_id="s", phase="text_output", model="m")
    for col in ("ts", "session_id", "phase", "model"):
        fields = dict(base)
        fields[col] = None
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        with pytest.raises(sqlite3.IntegrityError):
            ledger_conn.execute(
                f"INSERT INTO chat_abort_log ({cols}) VALUES ({marks})",
                tuple(fields.values()),
            )


def test_id_autoincrement_never_reuses(ledger_conn):
    """Provenance anchor: ids are never reused — after a delete, the next
    id continues the sequence (AUTOINCREMENT, spec §2)."""
    _record(ledger_conn)
    _record(ledger_conn)
    ids = [r[0] for r in ledger_conn.execute(
        "SELECT id FROM chat_abort_log ORDER BY id").fetchall()]
    ledger_conn.execute("DELETE FROM chat_abort_log WHERE id = ?", (ids[0],))
    _record(ledger_conn)
    (last,) = ledger_conn.execute(
        "SELECT max(id) FROM chat_abort_log").fetchone()
    assert last > max(ids)


# ── fail-open (§3, review L1: warning level pinned) ────────────────────────

def test_record_fail_open_never_raises_and_warns(caplog):
    """Injected INSERT failure: record() swallows and logs at WARNING with
    the session_id / message_id context (a silently lost signal must stay
    visible to the consumer side)."""
    _record  # keep the helper name referenced for readers
    with caplog.at_level(logging.WARNING, logger="md-vault.chat_abort"):
        chat_abort_records.record(
            _ExplodingConn(),
            session_id="sess-x",
            message_id="msg-y",
            ts=time.time(),
            phase="reasoning",
            tool_name=None,
            model="m",
        )
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, caplog.messages
    joined = "\n".join(r.getMessage() for r in warnings)
    assert "sess-x" in joined
    assert "msg-y" in joined


# ── r7: no GC ───────────────────────────────────────────────────────────────

def test_module_has_no_gc_sweep():
    """r7 user ruling: rows are kept forever — the write module ships no
    gc_sweep and no retention parameter."""
    assert not hasattr(chat_abort_records, "gc_sweep")
    src = inspect.getsource(chat_abort_records)
    assert "DELETE" not in src.upper()
