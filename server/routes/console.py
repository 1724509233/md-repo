"""Console domain: document-tree read + CRUD (URL prefixes ``/``, ``/api/docs``, ``/api/graph``).

Order-sensitive within this module: ``POST /api/docs/manage`` must be
registered before the ``POST /api/docs/{path:path}`` upload catchall, and
``GET /api/docs/changes`` must precede the get_doc catchall — the ``{path:path}``
converter is greedy and would swallow both.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from fastapi import File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from fastapi.templating import Jinja2Templates

from .. import ledger
from ..vault.service import get_all_files
from ..vault.fs.vault_ops import EXCLUDED_INDEX_DIRS, safe_rel_path as _safe_rel_path


def _hash_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def create_file(db_conn, vault_dir, body) -> dict:
    rel = _safe_rel_path(body.get("path") or "")
    if rel is None:
        raise HTTPException(status_code=400, detail="invalid path")
    file_path = vault_dir / rel
    if file_path.exists():
        raise HTTPException(status_code=409, detail="already exists")
    content = (body.get("content") or "").encode("utf-8")
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(content)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"failed to create file: {e}")
    # Web op: logged synchronously (the console feed must respond
    # instantly while the user watches); record() marks skip —
    # consumer books nothing.
    ledger.record(db_conn, vault_dir, rel.as_posix(), "created", "web")
    return {"ok": True, "path": rel.as_posix()}


def create_folder(db_conn, vault_dir, body) -> dict:
    rel = _safe_rel_path(body.get("path") or "")
    if rel is None:
        raise HTTPException(status_code=400, detail="invalid path")
    dir_path = vault_dir / rel
    if dir_path.exists():
        raise HTTPException(status_code=409, detail="already exists")
    try:
        dir_path.mkdir(parents=True)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"failed to create folder: {e}")
    ledger.record_folder(db_conn, vault_dir, rel.as_posix(), "created", "web")
    return {"ok": True, "path": rel.as_posix()}


def move(db_conn, vault_dir, body) -> dict:
    src_rel = _safe_rel_path(body.get("from_path") or "")
    dst_rel = _safe_rel_path(body.get("to_path") or "")
    if src_rel is None or dst_rel is None:
        raise HTTPException(status_code=400, detail="invalid path")
    src, dst = vault_dir / src_rel, vault_dir / dst_rel
    if not src.exists():
        raise HTTPException(status_code=404, detail="not found")
    if dst.exists():
        raise HTTPException(status_code=409, detail="already exists")
    if dst_rel.as_posix().startswith(src_rel.as_posix() + "/"):
        raise HTTPException(
            status_code=400, detail="cannot move into own subdirectory"
        )
    from ..vault.orchestration.refs import _update_references_batch

    if src.is_file():
        moves = [(src_rel.as_posix(), dst_rel.as_posix())]
    else:  # folder: expand to internal .md files, preserving nested structure
        moves = [
            (p.relative_to(vault_dir).as_posix(), (dst_rel / p.relative_to(src)).as_posix())
            for p in sorted(src.rglob("*.md")) if p.is_file()
        ]
    # Web op: per-file moved rows logged synchronously (console
    # feed); record() marks skip — consumer books nothing.
    # Reference rewriting ran in batch above — the per-file passes
    # skip their own ref update (idempotent either way).
    # pre_rename=True: update refs before rename so uniqueness check is correct
    _update_references_batch(db_conn, vault_dir, moves, pre_rename=True)
    was_dir = src.is_dir()
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"failed to rename: {e}")
    if was_dir:
        ledger.record_folder(
            db_conn, vault_dir, dst_rel.as_posix(), "moved", "web",
            from_path=src_rel.as_posix())
    # segments_fts_* triggers keep FTS in sync per row; no batch suppression needed
    for old, new in moves:
        ledger.record(
            db_conn,
            vault_dir,
            new,
            "moved",
            "web",
            from_path=old,
            update_refs=False,
        )
    return {"ok": True, "path": dst_rel.as_posix()}


def delete(db_conn, vault_dir, body) -> dict:
    rel = _safe_rel_path(body.get("path") or "")
    if rel is None:
        raise HTTPException(status_code=400, detail="invalid path")
    target = vault_dir / rel
    if not target.exists():
        raise HTTPException(status_code=404, detail="not found")

    if target.is_file():
        # 可逆层（P1，工作项 6）：人在 Web UI 删文件 = 全文快照兜底
        # （删除类快照单独保留 30 天，回滚端点可恢复）。
        from ..snapshots import capture, finalize, gc_after_capture

        snap = capture(db_conn, vault_dir, rel.as_posix(), "delete", actor="web")
        try:
            target.unlink()
        except OSError as e:
            if snap is not None:
                from ..snapshots import discard

                discard(db_conn, vault_dir, snap)
            raise HTTPException(status_code=500, detail=f"failed to delete: {e}")
        if snap is not None:
            finalize(db_conn, snap, "")  # post-state: file absent
            gc_after_capture(db_conn, vault_dir)
        # Web op: logged synchronously; record() marks skip —
        # consumer books nothing.
        ledger.record(db_conn, vault_dir, rel.as_posix(), "deleted", "web")
        return {"ok": True, "path": rel.as_posix(), "deleted_count": 1}
    # Folder: delete .md files one by one, clean up empty dirs bottom-up
    from ..snapshots import capture, discard, finalize, gc_after_capture, new_call_id

    md_files = sorted(p for p in target.rglob("*.md") if p.is_file())
    # One human action, many files → one call_id rollback group:
    # capture every member's full pre-image before any unlink.
    call_id = new_call_id()
    snaps = [
        (p.relative_to(vault_dir).as_posix(),
         capture(db_conn, vault_dir, p.relative_to(vault_dir).as_posix(),
                 "delete", actor="web", call_id=call_id))
        for p in md_files
    ]
    rels = []
    failed = None
    for p in md_files:
        r = p.relative_to(vault_dir).as_posix()
        try:
            p.unlink()
        except OSError as e:
            failed = (r, str(e))
            break
        rels.append(r)
    # Reconcile the capture group with what actually happened
    # (partial failure leaves the call rollback-able for exactly
    # the files that were deleted; the rest are discarded, oracle
    # gate P1-B1):
    done = set(rels)
    for r, snap in snaps:
        if snap is None:
            continue
        if r in done:
            finalize(db_conn, snap, "")  # post-state: file absent
        else:
            discard(db_conn, vault_dir, snap)
    if any(s is not None for _, s in snaps):
        gc_after_capture(db_conn, vault_dir)
    if failed:
        raise HTTPException(
            status_code=500,
            detail=f"partial delete: {failed[0]}: {failed[1]}; "
                   f"{len(rels)} file(s) already deleted "
                   f"(recoverable via /api/memory/rollback)")
    for d in sorted(
        [target] + list(target.rglob("*")),
        key=lambda x: len(x.parts),
        reverse=True,
    ):
        if not d.is_dir():
            continue
        try:
            d.rmdir()
        except OSError:
            pass  # non-empty (contains non-.md files) → keep
    if not target.exists():
        ledger.record_folder(db_conn, vault_dir, rel.as_posix(), "deleted", "web")
    # Web op: per-file rows logged synchronously; record() marks
    # skip — consumer books nothing.
    # segments_fts_* triggers keep FTS in sync per row; no batch suppression needed
    for r in rels:
        ledger.record(db_conn, vault_dir, r, "deleted", "web")
    return {"ok": True, "path": rel.as_posix(), "deleted_count": len(rels)}


def register(app, ctx) -> None:
    templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "index.html")

    @app.get("/api/health")
    async def health() -> dict:
        files = get_all_files(ctx.db_conn)
        return {"status": "ok", "file_count": len(files)}

    @app.get("/api/docs")
    async def list_docs(request: Request):
        # memory-runtime v1: suggestions/index are system dirs — neither the
        # document tree nor its file list may surface them (top-level match
        # only; deeper same-named dirs are user content).
        files = [
            f for f in get_all_files(ctx.db_conn)
            if len(f["path"].split("/")) <= 1
            or f["path"].split("/")[0] not in EXCLUDED_INDEX_DIRS
        ]
        dirs = []
        for root, dirnames, _ in os.walk(ctx.vault_dir):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]  # 剪枝：不 descend 隐藏目录
            rel_root = Path(root).relative_to(ctx.vault_dir)
            # memory-runtime v1: suggestions/index are system dirs — the
            # document tree (and descent) must not surface them.
            if rel_root == Path("."):
                dirnames[:] = [d for d in dirnames if d not in EXCLUDED_INDEX_DIRS]
            dirs.extend((rel_root / d).as_posix() for d in dirnames)
        dirs.sort()
        body = json.dumps({"files": files, "dirs": dirs}, sort_keys=True).encode()
        etag = hashlib.sha256(body).hexdigest()
        if request.headers.get("if-none-match") == f'"{etag}"':
            return Response(content=b"", status_code=304, headers={"ETag": f'"{etag}"'})
        return Response(content=body, media_type="application/json", headers={"ETag": f'"{etag}"'})

    @app.get("/api/docs/changes")
    async def get_changes(since: float = 0.0) -> list[dict]:
        from ..vault.db.changes import get_changes as _get_changes

        return _get_changes(ctx.db_conn, since=since)

    @app.get("/api/graph/backlinks")
    async def graph_backlinks(path: str) -> list[dict]:
        from ..vault.service import graph_get_backlinks

        return graph_get_backlinks(ctx.db_conn, path)

    @app.get("/api/graph/related")
    async def graph_related(path: str, hops: int = 1) -> list[dict]:
        from ..vault.service import graph_get_related

        return graph_get_related(ctx.db_conn, path, hops=hops)

    @app.get("/api/docs/{path:path}")
    async def get_doc(path: str) -> PlainTextResponse:
        rel = _safe_rel_path(path)
        if rel is None:
            raise HTTPException(status_code=400, detail="invalid path")
        file_path = ctx.vault_dir / rel
        if not file_path.is_file():
            raise HTTPException(status_code=404, detail="file not found")
        content = file_path.read_bytes().decode("utf-8", errors="replace")
        return PlainTextResponse(content, media_type="text/markdown; charset=utf-8")

    @app.post("/api/docs/manage")
    async def manage_docs(request: Request) -> dict:
        # Must be registered before POST /api/docs/{path:path} (upload),
        # otherwise the path converter swallows "manage"
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="invalid JSON body")
        action = body.get("action")
        if action == "create_file":
            return create_file(ctx.db_conn, ctx.vault_dir, body)
        if action == "create_folder":
            return create_folder(ctx.db_conn, ctx.vault_dir, body)
        if action in ("rename", "move"):
            return move(ctx.db_conn, ctx.vault_dir, body)
        if action == "delete":
            return delete(ctx.db_conn, ctx.vault_dir, body)
        raise HTTPException(status_code=400, detail=f"unknown action: {action}")

    @app.post("/api/docs/{path:path}")
    async def upload_doc(path: str, file: UploadFile = File(...)) -> dict:
        rel = _safe_rel_path(path)
        if rel is None:
            raise HTTPException(status_code=400, detail="invalid path")
        file_path = ctx.vault_dir / rel
        try:
            content = await file.read()
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_bytes(content)
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"failed to save file: {e}")
        digest = _hash_bytes(content)
        rel_str = rel.as_posix()
        ledger.register(rel_str, "web")
        return {"path": rel_str, "hash": digest}

    @app.put("/api/docs/{path:path}")
    async def save_doc(path: str, request: Request) -> dict:
        rel = _safe_rel_path(path)
        if rel is None:
            raise HTTPException(status_code=400, detail="invalid path")
        file_path = ctx.vault_dir / rel
        content = await request.body()
        try:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_bytes(content)
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"failed to save: {e}")
        ledger.register(rel.as_posix(), "web")
        return {"ok": True}
