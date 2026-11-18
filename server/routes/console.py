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

from .. import ledger
from ..core.privacy import detect_privacy_refs, is_privacy
from ..vault.db.file_index import get_all_files
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
    from ..vault.orchestration.refs import (
        apply_reference_updates,
        prepare_reference_updates,
    )

    if src.is_file():
        moves = [(src_rel.as_posix(), dst_rel.as_posix())]
    else:  # folder: expand to internal .md files, preserving nested structure
        moves = [
            (p.relative_to(vault_dir).as_posix(), (dst_rel / p.relative_to(src)).as_posix())
            for p in sorted(src.rglob("*.md")) if p.is_file()
        ]
    # F9 split: prepare computes every referrer's rewritten content BEFORE
    # any rename (pre-rename uniqueness; in-batch referrers read at their
    # old paths) and touches no disk — a rename failure below 500s with
    # every referrer byte-identical (zero residue).
    prepared = prepare_reference_updates(db_conn, vault_dir, moves)
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
    # Web op: per-file moved rows logged synchronously (console
    # feed); record() marks skip — consumer books nothing.
    # update_refs=False: reference rewriting lands once, via
    # apply_reference_updates below.
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
    # All renames (and their ledger rows) succeeded → land the prepared
    # rewrites; referrers that are themselves in the move batch are
    # redirected to their new paths.
    apply_reference_updates(vault_dir, prepared, redirect=dict(moves))
    return {"ok": True, "path": dst_rel.as_posix()}


def delete(db_conn, vault_dir, body) -> dict:
    # Deletion = move into the trash (P4-4, actor separation 2026-11-15):
    # the file lands in .trash/<batch>/ — the workbench's sole recovery
    # channel (restorable via /api/trash). No snapshot capture here:
    # snapshot-side deletions are the bgagent/mcp channel's (direct unlink
    # + 30-day rollback). The pre-delete hash is booked inside move_to_trash
    # (change_log pre_hash) for the sync lane's stale-copy check.
    from ..vault.orchestration import trash

    rel = _safe_rel_path(body.get("path") or "")
    if rel is None:
        raise HTTPException(status_code=400, detail="invalid path")
    target = vault_dir / rel
    if not target.exists():
        raise HTTPException(status_code=404, detail="not found")

    if target.is_file():
        try:
            batch = trash.move_to_trash(db_conn, vault_dir, [rel.as_posix()], "web")
        except trash.TrashError as e:
            raise HTTPException(status_code=500, detail=f"failed to move to trash: {e}")
        # Inbound ledger rows (remove_links=False, pre_hash) are booked
        # inside move_to_trash; record() marks skip — consumer books nothing.
        return {"ok": True, "path": rel.as_posix(), "deleted_count": 1, "batch": batch}
    # Folder: move all internal .md files into one trash batch, then clean
    # up empty dirs bottom-up (same scope as the old recursive delete).
    md_files = sorted(p for p in target.rglob("*.md") if p.is_file())
    rels = [p.relative_to(vault_dir).as_posix() for p in md_files]
    batch = None
    if rels:
        try:
            batch = trash.move_to_trash(db_conn, vault_dir, rels, "web")
        except trash.TrashError as e:
            raise HTTPException(status_code=500, detail=f"failed to move to trash: {e}")
    for d in sorted(
        [target] + list(target.rglob("*")),
        key=lambda x: len(x.parts),
        reverse=True,
    ):
        if not d.is_dir():
            continue
        # Dot files are system cruft (e.g. macOS .DS_Store). The UI tree lists
        # only .md files, so a folder holding just dot files looks empty and
        # the user expects deletion to remove it — unlink those dot files
        # (best-effort; no snapshot, no per-file change_log row: the dir-level
        # record_folder below covers the user-visible semantics) so the rmdir
        # can succeed. Ordinary non-.md files are user content and still keep
        # the folder alive.
        for p in d.iterdir():
            if p.is_file() and p.name.startswith("."):
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass  # best-effort; rmdir below still decides keep/drop
        try:
            d.rmdir()
        except OSError:
            pass  # non-empty (contains non-.md files) → keep
    if not target.exists():
        ledger.record_folder(db_conn, vault_dir, rel.as_posix(), "deleted", "web")
    # Per-file inbound rows (remove_links=False) are booked inside
    # move_to_trash; record() marks skip — consumer books nothing.
    return {"ok": True, "path": rel.as_posix(), "deleted_count": len(rels),
            "batch": batch}


def register(app, ctx) -> None:
    templates_dir = Path(__file__).parent.parent / "templates"

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> Response:
        # Content-hashed ETag + manual 304 (Starlette's FileResponse ships
        # the header but only StaticFiles answers If-None-Match). The HTML
        # must always revalidate — it references the versioned vendor set.
        data = (templates_dir / "index.html").read_bytes()
        etag = '"' + hashlib.md5(data, usedforsecurity=False).hexdigest() + '"'
        headers = {"etag": etag, "cache-control": "no-cache"}
        if etag in request.headers.get("if-none-match", "").split(","):
            return Response(status_code=304, headers=headers)
        return Response(content=data, media_type="text/html", headers=headers)

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
            or f["path"].split("/")[0].lower() not in EXCLUDED_INDEX_DIRS
        ]
        dirs = []
        for root, dirnames, _ in os.walk(ctx.vault_dir):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]  # 剪枝：不 descend 隐藏目录
            rel_root = Path(root).relative_to(ctx.vault_dir)
            # memory-runtime v1: suggestions/index are system dirs — the
            # document tree (and descent) must not surface them.
            if rel_root == Path("."):
                dirnames[:] = [d for d in dirnames if d.lower() not in EXCLUDED_INDEX_DIRS]
            dirs.extend((rel_root / d).as_posix() for d in dirnames)
        dirs.sort()
        # privacy_dirs 标注（spec 2026-11-18 §2.5，display-only）：UI 据此
        # 灰显隐私目录；files/dirs 全集绝不隐私过滤（Web 面是用户自己的
        # 数据视图）。进 ETag：名单变更 → 前端缓存自然失效。
        privacy_dirs = (list(ctx.privacy.dirs) if ctx.privacy is not None
                        else [])
        body = json.dumps({"files": files, "dirs": dirs,
                           "privacy_dirs": privacy_dirs},
                          sort_keys=True).encode()
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
        from ..vault.db.graph_sql import graph_get_backlinks

        return graph_get_backlinks(ctx.db_conn, path)

    @app.get("/api/graph/related")
    async def graph_related(path: str, hops: int = 1) -> list[dict]:
        from ..vault.db.graph_sql import graph_get_related

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
        rel_str = rel.as_posix()
        try:
            content = await file.read()
            file_path.parent.mkdir(parents=True, exist_ok=True)
            # F6 write discipline: register BEFORE the write — the watcher
            # consumer pops our attribution when the write event fires;
            # registered after, it pops empty (watchdog misattribution +
            # stale registry entry mislabeling the next external write).
            ledger.register(rel_str, "web", ctx.vault_dir)
            file_path.write_bytes(content)
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"failed to save file: {e}")
        digest = _hash_bytes(content)
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
            # F6 write discipline: register BEFORE the write (see upload_doc).
            ledger.register(rel.as_posix(), "web", ctx.vault_dir)
            file_path.write_bytes(content)
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"failed to save: {e}")
        # 保存引用警告（spec §2.6，advisory 不拦截）：成功响应恒带
        # privacy_refs 键（统一形状，前端零分支）：非隐私目标 + 内容显式
        # 引用隐私目录 → 去重引用列表；隐私区内目标恒 []（区内写是合法
        # 动作）；ctx.privacy None/空名单 → 恒 []。保存语义不变。
        privacy_dirs = ctx.privacy.dirs if ctx.privacy is not None else ()
        privacy_refs: list[str] = []
        if privacy_dirs and not is_privacy(privacy_dirs, rel.as_posix()):
            privacy_refs = detect_privacy_refs(
                content.decode("utf-8", errors="replace"), privacy_dirs)
        return {"ok": True, "privacy_refs": privacy_refs}
