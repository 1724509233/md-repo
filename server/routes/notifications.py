"""Notifications domain: in-app notification feed (URL prefix ``/api/notifications``)."""
from __future__ import annotations

from fastapi import HTTPException, Request

from ..vault.db import notifications as notifications_db


def register(app, ctx) -> None:
    @app.get("/api/notifications")
    async def list_notifications(unread: int = 0) -> list[dict]:
        # SQL lives in the db layer (card 7); this route is validation-free
        # by contract — the unread flag is the whole query surface.
        return notifications_db.list_notifications(
            ctx.db_conn, unread_only=bool(unread))

    @app.post("/api/notifications/read")
    async def mark_notifications_read(request: Request) -> dict:
        body = await request.json()
        if body.get("all"):
            notifications_db.mark_all_read(ctx.db_conn)
        else:
            ids = body.get("ids", [])
            if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
                raise HTTPException(status_code=400, detail="ids must be a list of ints")
            notifications_db.mark_read(ctx.db_conn, ids)
        return {"ok": True}
