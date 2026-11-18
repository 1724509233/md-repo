"""Database connection creation, schema init, and the shared mutation lock."""
from __future__ import annotations

import contextlib
import sqlite3
import threading
import time
from pathlib import Path

# Global lock guarding all mutations of the shared db connection. The same
# connection is used from the watchdog thread and the async event loop, so
# every write path must hold this lock to avoid interleaved transactions.
_db_lock = threading.Lock()

# Second lock, same discipline, narrower domain (2026-10-31 ledger-file
# split, L4): guards the compound critical sections of the four
# authoritative-state tables (proposals / proposal_transitions / snapshots /
# relatedness) on their own connection (.sync/ledger.db). Import discipline
# is the same as card 5's for ``_db_lock``: this identifier stays inside
# ``server/vault/db`` — the lock-holding named functions (proposal_records,
# snapshot_records, relatedness.merge_edge) are the interface; nothing
# outside the package imports or holds the lock. Read helpers stay
# lock-free (a non-reentrant lock held by a reader would deadlock the
# thread). ``_db_lock`` now guards only the index/file-ledger domain.
_ledger_lock = threading.Lock()

# Map legacy action names (old file_changes.action) to new op names (change_log.op).
# Public (card 10b): the canonical op normalizer is also read by callers
# outside the package (file_ops publishes the canonical op to the event
# bus) — the vocabulary contract is the AGENTS.md "do not fix op
# vocabulary in callers" rule.
ACTION_TO_OP = {
    "created": "create",
    "modified": "update",
    "deleted": "delete",
    "moved": "move",
}

# Reverse mapping: new op names back to legacy action names for backward compat.
_OP_TO_ACTION = {v: k for k, v in ACTION_TO_OP.items()}


@contextlib.contextmanager
def index_write():
    """Public index-write lock interface (card 10b): hold the shared
    mutation lock across a compound read-then-write critical section.

    The named lock-holding functions inside ``vault.db`` are the normal
    interface; this context manager exists for the one legitimate caller
    that cannot be a single db function — ``file_ops.apply_change``'s
    composite (disk read + files + change_log + FTS + graph in ONE
    section). Lock discipline unchanged: non-reentrant, mutation-only,
    never nested (callers must not call a lock-holding named function
    while holding this)."""
    with _db_lock:
        yield


def count_table_rows(conn: sqlite3.Connection, table: str) -> int:
    """Row count of one table (recovery drill ledger-preservation check,
    card 6b sink). The table name is interpolated directly into the SQL:
    callers must pass a constant identifier (the drill's LEDGER_TABLES),
    never user input."""
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def _ensure_notification_cols(conn: sqlite3.Connection) -> None:
    """Idempotent migration: add missing columns to the notifications table.

    PRAGMA table_info check, ALTER only when the column is missing, safe to
    call on every startup.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(notifications)")}
    for col, ddl in (
        ("ops_total", "ALTER TABLE notifications ADD COLUMN ops_total INTEGER NOT NULL DEFAULT 0"),
        ("files_changed", "ALTER TABLE notifications ADD COLUMN files_changed INTEGER NOT NULL DEFAULT 0"),
        ("kind", "ALTER TABLE notifications ADD COLUMN kind TEXT NOT NULL DEFAULT 'batch'"),
        # JSON array string of vault-relative proposal paths the run produced
        # ('' when none) — lets the console UI deep-link without parsing summary.
        ("proposal_paths", "ALTER TABLE notifications ADD COLUMN proposal_paths TEXT DEFAULT ''"),
        # JSON array string of report-channel files (index/reports/**) the run
        # wrote ('' when none) — console report-view deep links (debt #8).
        ("report_paths", "ALTER TABLE notifications ADD COLUMN report_paths TEXT DEFAULT ''"),
        # The run's batch stamp (run_id: join key to the proposals the run
        # produced) and the trigger source (why the batch fired: cron tick vs
        # event arming). Old rows keep the defaults ('' / 'cron').
        ("run_id", "ALTER TABLE notifications ADD COLUMN run_id TEXT DEFAULT ''"),
        ("trigger", "ALTER TABLE notifications ADD COLUMN trigger TEXT DEFAULT 'cron'"),
    ):
        if col not in cols:
            conn.execute(ddl)


def _ensure_trigger_cols(conn: sqlite3.Connection) -> None:
    """Idempotent migration: old trigger_state rows get the last_armed_seq
    column (the seq cursor for the arming scan) and the two recall cursors
    (independent id sequences, v2).

    Same idiom as :func:`_ensure_notification_cols` — PRAGMA table_info check,
    ALTER only when the column is missing, safe to call on every startup.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(trigger_state)")}
    for col in ("last_armed_seq", "last_recall_query_seq", "last_recall_hit_seq"):
        if col not in cols:
            conn.execute(
                f"ALTER TABLE trigger_state ADD COLUMN {col} "
                "INTEGER NOT NULL DEFAULT 0"
            )


def _ensure_session_cols(conn: sqlite3.Connection) -> None:
    """Idempotent migration: add the per-session auto_approve column.

    auto_approve is the session-level runtime switch that skips the per-write
    confirm prompt (the Web UI checkbox; bgagent is unaffected — it runs with
    auto_approve=True directly). Same idiom as
    :func:`_ensure_notification_cols` — PRAGMA table_info check, ALTER only
    when the column is missing, safe to call on every startup.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
    if "auto_approve" not in cols:
        conn.execute(
            "ALTER TABLE sessions ADD COLUMN auto_approve INTEGER NOT NULL DEFAULT 0"
        )


def _ensure_recall_traffic_col(conn: sqlite3.Connection) -> None:
    """Idempotent migration: v1 recall_log DBs get the traffic column.

    Added only when the column is missing (fresh DBs create it inline), then
    backfilled from source: web → human, agent → on_demand, else synthetic.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(recall_log)")}
    if "traffic" not in cols:
        conn.execute(
            "ALTER TABLE recall_log ADD COLUMN traffic TEXT NOT NULL DEFAULT ''"
        )
        conn.execute(
            "UPDATE recall_log SET traffic = "
            "CASE WHEN source = 'web' THEN 'human' "
            "WHEN source = 'agent' THEN 'on_demand' "
            "ELSE 'synthetic' END WHERE traffic = ''"
        )


def _ensure_recall_session_cols(conn: sqlite3.Connection) -> None:
    """Idempotent migration: add the session_id column to the recall ledger.

    spec 2026-11-13 §3.3 F3 / §5 (same batch): the agent channel's searches are
    attributed to their session. ``recall_queries`` and ``recall_log`` each get
    a nullable ``session_id TEXT`` (web/mcp channels write NULL — no session).
    Same idiom as :func:`_ensure_recall_traffic_col` — PRAGMA table_info check,
    ALTER only when the column is missing, safe to call on every startup.
    """
    for table in ("recall_queries", "recall_log"):
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if "session_id" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN session_id TEXT")


def _ensure_change_log_batch_id(conn: sqlite3.Connection) -> None:
    """Idempotent migration: add the batch_id column to change_log.

    The batch id is the run_id that produced the change (proposal
    frontmatter ``run_id``); old rows keep the default '' — nothing may
    require a batch_id to be non-empty. Same idiom as
    :func:`_ensure_notification_cols`: PRAGMA table_info check, ALTER only
    when the column is missing, safe to call on every startup.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(change_log)")}
    if "batch_id" not in cols:
        conn.execute("ALTER TABLE change_log ADD COLUMN batch_id TEXT DEFAULT ''")


def _ensure_change_log_pre_hash(conn: sqlite3.Connection) -> None:
    """Idempotent migration: add the pre_hash column to change_log.

    Delete rows store the SHA-256 of the pre-delete content (spec
    2026-11-15 actor-separation §2): the sync lane derives its stale-copy
    pre-hash map from these rows — unbounded (no 30-day boundary, D5 audit
    fix: the map must outlive the snapshot GC window so a long-offline
    client's stale copy is still detected). Old rows keep the default ''.
    Same idiom as :func:`_ensure_change_log_batch_id`: PRAGMA table_info
    check, ALTER only when the column is missing, safe to call on every
    startup. Must run before :func:`_ensure_change_log_restore_op` so the
    table recreation's row copy carries the column.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(change_log)")}
    if "pre_hash" not in cols:
        conn.execute("ALTER TABLE change_log ADD COLUMN pre_hash TEXT DEFAULT ''")


# change_log DDL — the single source for both the fresh-DB CREATE in
# :func:`init_db` and the table recreation in
# :func:`_ensure_change_log_restore_op` (the two must not drift).
# op is the 5-set: 'restore' is the 5th canonical op (design 2026-11-10
# §12.1) — a restored file is not a new file, and the bgagent arm surface
# deliberately does NOT include it.
# pre_hash: the SHA-256 of the pre-delete content on delete rows (spec
# 2026-11-15 §2) — the sync lane derives its stale-copy pre-hash map from
# these rows; '' on all other ops.
_CHANGE_LOG_DDL = """
    CREATE TABLE change_log (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        source TEXT NOT NULL DEFAULT 'unknown',
        op TEXT NOT NULL
            CHECK(op IN ('create','update','delete','move','restore')),
        path TEXT NOT NULL,
        old_path TEXT DEFAULT '',
        new_hash TEXT DEFAULT '',
        batch_id TEXT DEFAULT '',
        pre_hash TEXT DEFAULT ''
    )
    """


def _ensure_change_log_restore_op(conn: sqlite3.Connection) -> None:
    """Idempotent migration: the change_log.op CHECK constraint gains
    'restore' (5th canonical op, design 2026-11-10 §12.1).

    SQLite cannot ALTER a CHECK constraint — when the stored DDL is stale,
    recreate the table (rename → create → copy → drop → reindex) inside one
    transaction. Rows are preserved: the sync lane's 200-row window and the
    UI change feed must not reset. Must run after
    :func:`_ensure_change_log_batch_id` and :func:`_ensure_change_log_pre_hash`
    (the copy carries both columns) and after the legacy file_changes merge.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='change_log'"
    ).fetchone()
    if row and "'restore'" in (row[0] or ""):
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("ALTER TABLE change_log RENAME TO change_log_old")
        # Indexes follow the renamed table (names unchanged) — drop them
        # explicitly so the reindex below recreates them on the new table.
        conn.execute("DROP INDEX IF EXISTS idx_changelog_ts")
        conn.execute("DROP INDEX IF EXISTS idx_changelog_path")
        conn.execute(_CHANGE_LOG_DDL)
        conn.execute(
            "INSERT INTO change_log "
            "(seq, ts, source, op, path, old_path, new_hash, batch_id, pre_hash) "
            "SELECT seq, ts, source, op, path, old_path, new_hash, batch_id, pre_hash "
            "FROM change_log_old"
        )
        conn.execute("DROP TABLE change_log_old")
        conn.execute("CREATE INDEX idx_changelog_ts ON change_log(ts)")
        conn.execute("CREATE INDEX idx_changelog_path ON change_log(path)")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def init_db(db_path: Path, *, busy_timeout_ms: int = 0) -> sqlite3.Connection:
    """Create (or open) the index database and ensure the schema exists.

    Enables WAL mode for concurrent read access. Migrates the legacy
    ``file_changes`` table into the new ``change_log`` table if present.
    ``busy_timeout_ms`` > 0 arms the writer-lock wait (card 8: the MCP
    stdio process runs against the main server's index.db — 5000 there,
    the PRAGMA lives HERE, not at the channel).
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    if busy_timeout_ms:
        # PRAGMA takes no bind params; int() is the injection guard.
        conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS files (
            path TEXT PRIMARY KEY,
            hash TEXT NOT NULL,
            mtime REAL NOT NULL
        )
        """
    )
    conn.execute(
        _CHANGE_LOG_DDL.replace(
            "CREATE TABLE change_log", "CREATE TABLE IF NOT EXISTS change_log", 1
        )
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_changelog_ts ON change_log(ts)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_changelog_path ON change_log(path)"
    )
    _ensure_change_log_batch_id(conn)
    _ensure_change_log_pre_hash(conn)
    # Trigger arming checkpoints (memory-runtime v1/v2): per bgagent row
    # tracking arming counter, cooldown anchor, the daily run budget spend,
    # and the two recall-ledger cursors (v2, independent id sequences).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trigger_state (
            agent TEXT PRIMARY KEY,
            last_run_ts REAL,
            armed INTEGER,
            last_armed_scan REAL,
            last_armed_seq INTEGER NOT NULL DEFAULT 0,
            last_recall_query_seq INTEGER NOT NULL DEFAULT 0,
            last_recall_hit_seq INTEGER NOT NULL DEFAULT 0,
            runs_date TEXT,
            runs_count INTEGER
        )
        """
    )
    # Recall telemetry (memory-runtime v1, minimal): one row per document
    # hit in /api/ai/search, fed to the v2 sentinel role. traffic dimensioned
    # the recall source (human / on_demand / synthetic).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS recall_log (
            id INTEGER PRIMARY KEY,
            ts REAL,
            source TEXT,
            query TEXT,
            path TEXT,
            score REAL,
            recall TEXT,
            traffic TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_recall_ts ON recall_log(ts)"
    )
    # Recall query ledger (memory-runtime v2): one row per search (including
    # zero-hit searches — the failed-recall signal), written by recall.py.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS recall_queries (
            id INTEGER PRIMARY KEY,
            ts REAL,
            source TEXT,
            traffic TEXT,
            query TEXT,
            recall TEXT,
            hits INTEGER
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_recall_queries_ts ON recall_queries(ts)"
    )
    # Click telemetry (spec 2026-11-13 §4): one row per user click on a
    # recalled/recommended document (via = search | ai | preview; filetree
    # clicks are NOT ledgered — navigation, not consumption). Telemetry in
    # index.db (D6: rebuildable, same domain as recall_log/recall_queries);
    # a new table rather than a recall_log extension (D7: different event
    # kind + column set — via/rank/session_id/recall_id, no score/tier).
    # Links back to the hit ledger through recall_id (best-effort match,
    # nullable — see recall.log_click).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS recall_clicks (
            id INTEGER PRIMARY KEY,
            ts REAL NOT NULL,
            path TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'web',
            traffic TEXT NOT NULL DEFAULT 'human',
            via TEXT,
            query TEXT,
            session_id TEXT,
            rank INTEGER,
            recall_id INTEGER,
            message_id TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_recall_clicks_ts ON recall_clicks(ts)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_recall_clicks_path ON recall_clicks(path, ts)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_recall_clicks_recall ON recall_clicks(recall_id)"
    )
    # User feedback on an agent answer (spec 2026-11-13 §5.3): one row per
    # (session_id, message_id) — the unique key is the D22 feedback anchor (id,
    # not the ts measurement). Telemetry in index.db (D6: rebuildable, same
    # domain as the other recall tables). ``citations`` is the persisted
    # message snapshot (a copy of the done-time validation); ``click_paths`` is
    # the via=ai clicks attributed to that message_id (nullable).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS recall_feedback (
            id INTEGER PRIMARY KEY,
            ts REAL NOT NULL,
            session_id TEXT NOT NULL,
            message_id TEXT NOT NULL,
            verdict TEXT NOT NULL,
            citations TEXT NOT NULL,
            click_paths TEXT
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_recall_feedback_key "
        "ON recall_feedback(session_id, message_id)"
    )
    # F3 (spec 2026-11-13 §3.3): the agent channel's recall ledger rows carry
    # the session id (web/mcp write NULL).
    _ensure_recall_session_cols(conn)
    _ensure_recall_traffic_col(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            messages TEXT NOT NULL,
            token_count INTEGER NOT NULL DEFAULT 0,
            last_active REAL NOT NULL,
            created_at REAL NOT NULL,
            auto_approve INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    _ensure_session_cols(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            alias TEXT NOT NULL,
            name TEXT NOT NULL,
            status TEXT NOT NULL,
            report_path TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            read INTEGER NOT NULL DEFAULT 0,
            ops_total INTEGER NOT NULL DEFAULT 0,
            files_changed INTEGER NOT NULL DEFAULT 0,
            kind TEXT NOT NULL DEFAULT 'batch',
            proposal_paths TEXT DEFAULT '',
            report_paths TEXT DEFAULT '',
            run_id TEXT DEFAULT '',
            trigger TEXT DEFAULT 'cron'
        )
        """
    )
    _ensure_notification_cols(conn)
    _ensure_trigger_cols(conn)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_notifications_ts ON notifications(ts)"
    )

    # Ensure the segments search index exists.
    from .segments_sql import init_segments

    init_segments(conn)

    # Ensure the knowledge graph tables exist.
    from .graph_sql import init_graph

    init_graph(conn)

    # Migrate legacy file_changes table into change_log, then drop it.
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='file_changes'"
    )
    if cursor.fetchone() is not None:
        conn.execute(
            """
            INSERT INTO change_log (ts, source, op, path, old_path)
            SELECT ts, 'unknown',
                   CASE action
                       WHEN 'created' THEN 'create'
                       WHEN 'modified' THEN 'update'
                       WHEN 'deleted' THEN 'delete'
                       WHEN 'moved' THEN 'move'
                       ELSE action
                   END,
                   path, from_path
            FROM file_changes
            """
        )
        conn.execute("DROP TABLE file_changes")

    # Stale-DB constraint migration (after the merge above, so the copy
    # carries the legacy rows too): old change_log tables reject op='restore'.
    _ensure_change_log_restore_op(conn)

    conn.commit()
    return conn


# The four authoritative-state tables + indexes (2026-10-31 ledger-file
# split, L4). Every statement carries exactly ONE %s = the schema
# qualifier: "" for the connection's main database (init_ledger_db),
# "ledger." for an ATTACHed ledger database (migrate_ledger, inside the
# migration transaction). It goes on the CREATE TABLE name and on the
# CREATE INDEX *index* name — NOT on the index's table (SQLite 3.53
# rejects a schema-qualified table in CREATE INDEX: "near '.': syntax
# error"); the unqualified index table resolves in the index's own
# schema, which is why "ledger.idx ON t" still lands on ledger.t even
# while the pre-migration source tables still sit in main. REFERENCES
# is likewise unqualified (SQLite resolves it in the creating table's
# schema; a schema-qualified REFERENCES is a syntax error).
_LEDGER_DDL: tuple[str, ...] = (
    # Proposal records (reversible-automation v2, P0): the DB row is the
    # single source of truth for proposal state — and its only carrier
    # (D25: v2a is zero file-landing; the suggestions/ projection file is
    # gone, existing files are inert, the channel books no file row).
    # relpath is the synthetic card addressing key (no file behind it).
    # State transitions update the row and append a proposal_transitions
    # row — the sole ledger of the proposal channel.
    """
    CREATE TABLE IF NOT EXISTS %sproposals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        type TEXT NOT NULL,
        actor TEXT NOT NULL,
        run_id TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL,
        payload TEXT DEFAULT '',
        source_refs TEXT DEFAULT '[]',
        justification TEXT NOT NULL DEFAULT '',
        reject_reason TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        merged_at TEXT NOT NULL DEFAULT '',
        relpath TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS %sidx_proposals_status ON proposals(status)",
    "CREATE INDEX IF NOT EXISTS %sidx_proposals_actor ON proposals(actor)",
    "CREATE INDEX IF NOT EXISTS %sidx_proposals_relpath ON proposals(relpath)",
    """
    CREATE TABLE IF NOT EXISTS %sproposal_transitions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        proposal_id INTEGER NOT NULL REFERENCES proposals(id),
        from_status TEXT,
        to_status TEXT NOT NULL,
        actor TEXT NOT NULL,
        ts REAL NOT NULL,
        reason TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS %sidx_proposal_transitions_pid ON proposal_transitions(proposal_id)",
    # Pre-image snapshots (reversible-automation v2, P1): the reversibility
    # layer. One row per file touched by one tool call (call_id = the
    # rollback unit); blob_name points at the full pre-image bytes under
    # .sync/snapshots/. Single live slot per path (live=1): finalizing a
    # new capture demotes the previous live row — GC reclaims superseded
    # rows, never the live one (spec invariant 2). post_hash is '' until
    # the write is confirmed (snapshots.finalize). finalized=1 marks a
    # confirmed write; in-flight rows (captured, not finalized) are crash
    # residue — GC never reclaims them (oracle gate P1-B1: the row may be
    # the file's only pre-image, and it stays rollback-able).
    """
    CREATE TABLE IF NOT EXISTS %ssnapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        call_id TEXT NOT NULL,
        path TEXT NOT NULL,
        old_path TEXT NOT NULL DEFAULT '',
        new_path TEXT NOT NULL DEFAULT '',
        op TEXT NOT NULL CHECK(op IN ('create','update','delete','move')),
        pre_hash TEXT NOT NULL DEFAULT '',
        post_hash TEXT NOT NULL DEFAULT '',
        actor TEXT NOT NULL,
        run_id TEXT NOT NULL DEFAULT '',
        blob_name TEXT NOT NULL DEFAULT '',
        live INTEGER NOT NULL DEFAULT 0,
        finalized INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS %sidx_snapshots_path_live ON snapshots(path, live)",
    "CREATE INDEX IF NOT EXISTS %sidx_snapshots_call ON snapshots(call_id)",
    "CREATE INDEX IF NOT EXISTS %sidx_snapshots_run ON snapshots(run_id)",
    "CREATE INDEX IF NOT EXISTS %sidx_snapshots_ts ON snapshots(ts)",
    # Relatedness ledger (reversible-automation v2, P2): machine-discovered
    # document-level relation edges (spec §四 relatedness, §4.4 分层呈现).
    # One row = one edge (source note, target note, basis, run provenance).
    # DB-only: never projected into user files. Reversal = delete the row or
    # mark ignored. ``ignored`` is the human's one-click dismissal — a DB
    # flag, not a file change (spec §4.4 忽略 = DB 行写标记). Ignored rows
    # are kept as telemetry (忽略→遥测→回读: the consolidator's readback
    # shows ignored pairs so it proposes them less).
    """
    CREATE TABLE IF NOT EXISTS %srelatedness (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source TEXT NOT NULL,
        target TEXT NOT NULL,
        basis TEXT NOT NULL DEFAULT '',
        actor TEXT NOT NULL,
        generated_at REAL NOT NULL,
        run_id TEXT NOT NULL DEFAULT '',
        ignored INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS %sidx_relatedness_pair ON relatedness(source, target)",
    "CREATE INDEX IF NOT EXISTS %sidx_relatedness_actor ON relatedness(actor)",
    # Egress audit ledger (security redline 7, spec 2026-11-18 §3.1): one
    # row per document that actually entered a model context on a
    # non-loopback endpoint, per run (authoritative — ledger.db, not the
    # rebuildable index cache; a 2026-11-18 addition to the four-table set,
    # same lock domain). Write face: server/vault/db/egress_sql.py.
    """
    CREATE TABLE IF NOT EXISTS %segress_log (
        id            INTEGER PRIMARY KEY,
        ts            REAL NOT NULL,
        channel       TEXT NOT NULL,      -- 'agent' | 'bgagent'
        actor         TEXT NOT NULL,      -- 'agent' | bgagent alias (proposals.actor 同语义: channel-injected, not forgeable)
        run_id        TEXT NOT NULL DEFAULT '',   -- bgagent batch; interactive always ''
        endpoint_host TEXT NOT NULL,      -- host[:port] of base_url; never key/path/query
        model         TEXT NOT NULL DEFAULT '',
        path          TEXT NOT NULL,      -- vault-relative, one row per document
        via           TEXT NOT NULL       -- 'read' | 'search' | 'related' | 'recent' (first touch)
    )
    """,
    "CREATE INDEX IF NOT EXISTS %sidx_egress_path ON egress_log(path)",
    "CREATE INDEX IF NOT EXISTS %sidx_egress_ts ON egress_log(ts)",
    "CREATE INDEX IF NOT EXISTS %sidx_egress_run ON egress_log(run_id)",
)


def ensure_ledger_tables(conn: sqlite3.Connection, prefix: str = "") -> None:
    """Create the four authoritative-state tables + indexes (idempotent).

    ``prefix``: the schema qualifier for the CREATE statements — "" = the
    connection's main database (the ``init_ledger_db`` path), "ledger." =
    an ATTACHed ledger database (the ``migrate_ledger`` path; called
    INSIDE the migration's explicit transaction, so a concurrent first
    boot serializes on ``BEGIN IMMEDIATE`` instead of racing on a second
    connection).

    v2b① provenance slot: the ``_ensure_*`` migration that adds the
    provenance column to ``proposals`` lands here (reserve the slot now,
    add the column when v2b① ships — spec
    ``2026-10-31-ledger-file-split-design.md`` §3.5).
    """
    for stmt in _LEDGER_DDL:
        # exactly one slot per statement (the table / index name) — see
        # the _LEDGER_DDL header for why the index table and REFERENCES
        # stay unqualified
        conn.execute(stmt % prefix)


def _set_wal(conn: sqlite3.Connection) -> None:
    """Put ``conn``'s file in WAL mode, surviving a concurrent first boot.

    The journal-mode SWITCH (delete→WAL, i.e. initializing a brand-new
    file) needs an exclusive lock and does NOT busy-wait for it: against
    a peer process that is mid-init on the same 0-byte file the pragma
    fails immediately with "database is locked" (measured 17/30 crashes
    in a two-process race). A peer's file init takes <1ms, so a bounded
    retry settles the double start (server + MCP, spec §3.3 anchor). The
    already-WAL case is a no-op that needs no exclusive lock at all —
    short-circuited by the probe query, which also keeps steady-state
    boots from ever taking the switch path.
    """
    if conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal":
        return
    deadline = time.monotonic() + 10.0
    while True:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) or time.monotonic() > deadline:
                raise
            time.sleep(0.05)


def init_ledger_db(db_path: Path) -> sqlite3.Connection:
    """Create (or open) the authoritative-state ledger database.

    The four tables that have no rebuild source (``proposals`` /
    ``proposal_transitions`` / ``snapshots`` / ``relatedness``) live here, in
    a separate file (``.sync/ledger.db``) from the rebuildable index cache
    (``.sync/index.db``, :func:`init_db`). Same connection conventions as
    :func:`init_db`: WAL mode, per-thread reuse (``check_same_thread=False``),
    ``sqlite3.Row``. The two conns share one lifecycle — see
    :mod:`server.vault.db.conn`.

    v2b① provenance slot: the ``_ensure_*`` migration that adds the
    provenance column to ``proposals`` lands in this function (reserve the
    slot now, add the column when v2b① ships — spec
    ``2026-10-31-ledger-file-split-design.md`` §3.5).
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    _set_wal(conn)
    ensure_ledger_tables(conn)
    conn.commit()
    return conn
