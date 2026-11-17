"""Sync domain: client file-sync endpoint (URL prefix ``/api/sync``)."""
from __future__ import annotations

import re

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from ..vault.db.file_index import get_all_files
from ..sync import compare_files, derive_deleted_hashes


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

            # F2 (review 2026-11-16): the deletion gate is ALL-TIME — replay
            # every move/delete row (seq ASC, unbounded per spec 2026-11-15
            # §2) into {client-held path: pre_hash}. Not gated by last_sync.
            # The replay resolves move chains: a client-held pre-move copy of
            # a moved-then-deleted file is caught (review F3). "" values =
            # external deletes with no pre-hash, treated as stale copies.
            # Paths still alive on the server skip the deletion gate —
            # enforced in compare_files itself (a to_push path present
            # in the server manifest is not checked against
            # deleted_hashes).

            replay_rows = ctx.db_conn.execute(
                "SELECT op, path, old_path, pre_hash FROM change_log "
                "WHERE op IN ('move', 'delete') ORDER BY seq ASC"
            ).fetchall()
            deleted_hashes = derive_deleted_hashes([dict(r) for r in replay_rows])

            # Move rule keeps its windowed recent_moves (rename detection):
            # moves since the client's last sync (200-row window, newest
            # first) — prevents pushing back a stale pre-rename copy.
            recent_moves = None
            if last_sync:
                from ..vault.db.changes import get_changes as _get_changes

                all_changes = _get_changes(ctx.db_conn, since=float(last_sync), limit=200)
                recent_moves = [
                    c for c in all_changes
                    if c["action"] == "moved" and c.get("from_path")
                ]

            # F1 (review 2026-11-16): the client's last-sync manifest enables
            # three-way attribution — server-modified → to_pull, client-
            # modified → to_push, both → conflict. Optional field: absent
            # (legacy client) or malformed → None = legacy conflict behavior.
            baseline = body.get("baseline_files")
            if not isinstance(baseline, dict) or not all(
                    isinstance(k, str) and isinstance(v, str)
                    for k, v in baseline.items()):
                baseline = None

            result = compare_files(
                local_files, server_files,
                recent_moves=recent_moves,
                deleted_hashes=deleted_hashes,
                baseline=baseline,
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
