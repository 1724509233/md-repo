"""Server entry point: python -m server.main [config.yaml]"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import sys
from pathlib import Path

import uvicorn

from .api import create_app
from .config import load_server_config
from .db import graph_rebuild_all, init_db, upsert_file
from .logrotate import start_logrotate
from .watcher import start_watcher

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


def _initial_index(vault_dir: Path, conn) -> int:
    """Scan vault_dir for .md files and populate the db. Returns count added."""
    added = 0
    for f in vault_dir.rglob("*.md"):
        if ".sync" in f.parts:
            continue
        rel = f.relative_to(vault_dir).as_posix()
        try:
            content = f.read_bytes()
        except OSError:
            continue
        digest = hashlib.sha256(content).hexdigest()
        mtime = f.stat().st_mtime
        upsert_file(conn, rel, digest, mtime)
        added += 1
    return added


def _self_heal(vault_dir: Path, conn) -> int:
    """Migrate DB paths that no longer exist on disk to their renamed locations.

    Matches stale DB entries to live files by SHA-256 content hash.
    Returns the number of migrated paths.
    """
    logger = logging.getLogger(__name__)

    # 1. Stale DB paths: in DB but missing on disk
    db_paths = {r[0] for r in conn.execute("SELECT path FROM files")}
    stale = [p for p in db_paths if not (vault_dir / p).is_file()]
    if not stale:
        return 0

    # 2. Live files not in DB, indexed by content hash
    live_by_hash: dict[str, list[str]] = {}
    for f in vault_dir.rglob("*.md"):
        if ".sync" in f.parts or ".obsidian" in f.parts:
            continue
        rel = f.relative_to(vault_dir).as_posix()
        if rel in db_paths:
            continue
        try:
            digest = hashlib.sha256(f.read_bytes()).hexdigest()
        except OSError:
            continue
        live_by_hash.setdefault(digest, []).append(rel)

    # 3. Match stale -> live by hash; migrate unambiguous matches
    from .db import apply_change

    migrated = 0
    for old in stale:
        row = conn.execute("SELECT hash FROM files WHERE path = ?", (old,)).fetchone()
        if row is None:
            continue
        candidates = live_by_hash.get(row[0], [])
        if len(candidates) != 1:
            logger.warning("self-heal: no unique match for %s (%d candidates)", old, len(candidates))
            continue
        new = candidates[0]
        apply_change(conn, vault_dir, new, "moved", source="self-heal", from_path=old)
        live_by_hash[row[0]].remove(new)
        migrated += 1
        logger.info("self-heal: migrated %s -> %s", old, new)

    if migrated:
        logger.info("self-heal: migrated %d path(s)", migrated)
    return migrated


def main(argv: list[str] | None = None) -> None:
    argv = argv if argv is not None else sys.argv[1:]
    config_path = argv[0] if argv else "config.yaml"

    config = load_server_config(config_path)
    vault_dir = config.vault_dir
    vault_dir.mkdir(parents=True, exist_ok=True)

    db_path = vault_dir / ".sync" / "index.db"
    conn = init_db(db_path)

    # First run: build the initial index if the db is empty
    cursor = conn.execute("SELECT COUNT(*) FROM files")
    if cursor.fetchone()[0] == 0:
        _initial_index(vault_dir, conn)

    # Self-heal: migrate DB paths that were renamed while the server was down.
    _self_heal(vault_dir, conn)

    # Ensure the FTS and graph indexes are populated. On first run they are
    # empty; on later runs the FTS table may also be empty (e.g. an older
    # version of the server never built it), so rebuild whenever it is.
    fts_count = conn.execute("SELECT count(*) FROM segments_fts").fetchone()[0]
    if fts_count == 0:
        from .db import rebuild_segments_all

        rebuild_segments_all(conn, vault_dir)
        graph_rebuild_all(conn, vault_dir)

    app = create_app(vault_dir=vault_dir, db_conn=conn, ai_config=config.ai)

    async def _run() -> None:
        logs_dir = vault_dir / ".sync" / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        start_logrotate(None, logs_dir)
        start_watcher(vault_dir, conn)
        await asyncio.sleep(0)  # let the task start

    config_obj = uvicorn.Config(
        app, host=config.host, port=config.port, log_level="info"
    )
    server = uvicorn.Server(config_obj)

    async def _serve() -> None:
        await _run()
        await server.serve()

    asyncio.run(_serve())


if __name__ == "__main__":
    main()
