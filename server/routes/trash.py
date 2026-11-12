"""Trash domain: /api/trash (list / restore / purge / empty).

Path validation: ``batch`` is checked against the on-disk ``.trash/``
directory list (absent → 404); ``path`` must be an entry of that batch's
meta.json (otherwise → 400). No free-form path input → no traversal
surface (and ``safe_rel_path`` rejects a first segment of ``.trash``).
"""
from __future__ import annotations

from fastapi import HTTPException, Request

from ..vault.orchestration import trash as trash_core


def _meta(ctx, batch: str) -> dict:
    """Validate batch against the on-disk .trash/ dir list; return its meta."""
    if not isinstance(batch, str) or not batch:
        raise HTTPException(status_code=400, detail="batch required")
    if not trash_core.batch_exists(ctx.vault_dir, batch):
        raise HTTPException(status_code=404, detail="batch not found")
    return trash_core.read_meta(ctx.vault_dir, batch)


def _selected_path(meta: dict, path) -> str | None:
    """Validate an optional single-file path against the batch meta."""
    if path is None:
        return None
    if not isinstance(path, str) or path not in {f["path"] for f in meta.get("files", [])}:
        raise HTTPException(status_code=400, detail="path not in batch")
    return path


def list_trash(ctx) -> dict:
    items = [
        {
            "batch": m["batch"],
            "deleted_at": m.get("deleted_at", ""),
            "source": m.get("source", ""),
            "files": [
                {"path": f["path"], "size": f.get("size", 0)}
                for f in m.get("files", [])
            ],
        }
        for m in trash_core.list_trash(ctx.vault_dir)
    ]
    return {"items": items}


async def act(request: Request, ctx) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    action = body.get("action")
    if action == "restore":
        meta = _meta(ctx, body.get("batch"))
        path = _selected_path(meta, body.get("path"))
        try:
            restored = trash_core.restore(
                ctx.db_conn, ctx.vault_dir, body["batch"], path)
        except trash_core.TrashConflict as e:
            raise HTTPException(status_code=409, detail=str(e))
        except trash_core.TrashError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"ok": True, "batch": body["batch"], "restored": restored}
    if action == "purge":
        meta = _meta(ctx, body.get("batch"))
        path = _selected_path(meta, body.get("path"))
        try:
            n = trash_core.purge(
                ctx.db_conn, ctx.vault_dir, body["batch"], path)
        except trash_core.TrashError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"ok": True, "batch": body["batch"], "purged": n}
    if action == "empty":
        n = trash_core.empty(ctx.db_conn, ctx.vault_dir)
        return {"ok": True, "purged": n}
    raise HTTPException(status_code=400, detail=f"unknown action: {action}")


def register(app, ctx) -> None:
    @app.get("/api/trash")
    async def get_trash() -> dict:
        return list_trash(ctx)

    @app.post("/api/trash")
    async def post_trash(request: Request) -> dict:
        return await act(request, ctx)
