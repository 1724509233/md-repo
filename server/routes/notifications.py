"""Notifications domain: in-app notification feed (URL prefix ``/api/notifications``)."""
from __future__ import annotations

from fastapi import HTTPException, Request


def register(app, ctx) -> None:
    @app.get("/api/notifications")
    async def list_notifications(unread: int = 0) -> list[dict]:
        query = ("SELECT id, ts, alias, name, status, report_path, summary, read, "
                 "ops_total, files_changed, proposal_paths, report_paths FROM notifications")
        params: list = []
        if unread:
            query += " WHERE read = 0"
        query += " ORDER BY ts DESC LIMIT 50"
        rows = ctx.db_conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    @app.post("/api/notifications/read")
    async def mark_notifications_read(request: Request) -> dict:
        body = await request.json()
        if body.get("all"):
            ctx.db_conn.execute("UPDATE notifications SET read = 1 WHERE read = 0")
        else:
            ids = body.get("ids", [])
            if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
                raise HTTPException(status_code=400, detail="ids must be a list of ints")
            if ids:
                marks = ",".join("?" * len(ids))
                ctx.db_conn.execute(f"UPDATE notifications SET read = 1 WHERE id IN ({marks})", ids)
        ctx.db_conn.commit()
        return {"ok": True}
