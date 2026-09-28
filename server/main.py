"""Server entry point: python -m server.main [config.yaml]"""
from __future__ import annotations

import asyncio
import hashlib
import sys
from pathlib import Path

import uvicorn

from .api import create_app
from .config import load_server_config
from .db import init_db, upsert_file
from .logrotate import start_logrotate


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

    app = create_app(vault_dir, conn)

    async def _run() -> None:
        logs_dir = vault_dir / ".sync" / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        start_logrotate(None, logs_dir)
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
