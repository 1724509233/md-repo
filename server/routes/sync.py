"""Sync domain: client file-sync endpoint (URL prefix ``/api/sync``)."""
from __future__ import annotations

import re

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from ..vault.db.file_index import get_all_files
from ..sync import compare_files


def register(app, ctx) -> None:
    @app.post("/api/sync")
    async def sync(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="invalid JSON body")

        device_name = body.get("device_name")
        local_files = body.get("local_files")
        logs = body.get("logs", [])
        last_sync = body.get("last_sync", 0.0)

        if not isinstance(device_name, str) or not device_name:
            raise HTTPException(status_code=400, detail="device_name is required")
        if not isinstance(local_files, dict):
            raise HTTPException(status_code=400, detail="local_files must be an object")

        if not ctx.sync_lock.try_acquire():
            return JSONResponse(
                status_code=409, content={"error": "sync in progress"}
            )

        try:
            server_files = {f["path"]: f["hash"] for f in get_all_files(ctx.db_conn)}

            # Fetch recent moved + deleted events since the client's last
            # sync (one query, 200-row window). Moves detect renames
            # (prevents pushing back a stale pre-rename copy, which would
            # recreate the old path); deletes detect server-side deletions
            # (prevents pushing back a stale copy, which would resurrect
            # the file). deleted_hashes maps original path -> pre-delete
            # hash from the change_log pre_hash column (every deletion
            # entry point books it), so a local rewrite after a deletion
            # is still pushed. Snapshot-side deletes (bgagent/mcp) never
            # enter the trash, so the trash batch scan is no longer the
            # source. seq ASC: the latest row per path wins. Unbounded
            # (change_log is persistent; clear_changes is dead code in
            # production) — the D5 bounded-window fix is deferred.
            recent_moves = None
            recent_deletes = None
            deleted_hashes = None
            if last_sync:
                from ..vault.db.changes import get_changes as _get_changes

                all_changes = _get_changes(ctx.db_conn, since=float(last_sync), limit=200)
                recent_moves = [
                    c for c in all_changes
                    if c["action"] == "moved" and c.get("from_path")
                ]
                recent_deletes = [
                    c for c in all_changes
                    if c["action"] == "deleted" and c.get("path")
                ]
                deleted_hashes = {
                    row["path"]: row["pre_hash"]
                    for row in ctx.db_conn.execute(
                        "SELECT path, pre_hash FROM change_log "
                        "WHERE op = 'delete' AND pre_hash != '' "
                        "ORDER BY seq ASC"
                    ).fetchall()
                }

            result = compare_files(
                local_files, server_files,
                recent_moves=recent_moves,
                recent_deletes=recent_deletes,
                deleted_hashes=deleted_hashes,
            )

            # Background agent notifications since the client's last sync (cap 50).
            rows = ctx.db_conn.execute(
                "SELECT ts, alias, name, status, report_path, summary, "
                "proposal_paths, report_paths, run_id, trigger "
                "FROM notifications WHERE read = 0 AND ts > ? ORDER BY ts DESC LIMIT 50",
                (float(last_sync),)).fetchall()
            result["notifications"] = [dict(r) for r in rows]

            # Save client logs to vault_dir/.sync/logs/{device_name}.log
            safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", device_name)
            logs_dir = ctx.vault_dir / ".sync" / "logs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            log_path = logs_dir / f"{safe_name}.log"
            if isinstance(logs, list):
                with open(log_path, "a", encoding="utf-8") as f:
                    for entry in logs:
                        if not isinstance(entry, dict):
                            continue
                        ts = entry.get("timestamp", "")
                        level = str(entry.get("level", "INFO")).upper()
                        message = entry.get("message", "")
                        f.write(f"{ts} [{level}] {message}\n")

            return JSONResponse(content=result)
        finally:
            ctx.sync_lock.release()
