"""FastAPI application factory and API routes."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
import sys
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .agent import SessionManager
from .agent.agent import Agent
from .agent.prompts import SYSTEM_PROMPT, sse
from .agent.reminders import DocPathUserReminder, TimeSystemReminder
from .agent.spec import AgentSpec, ModelConfig
from .agent.tools import TOOL_REGISTRY
from .vault.service import (
    apply_change,
    get_all_files,
    upsert_file,
)
from .vault.db.changes import log_change
from .vault.db.schema import _db_lock
from .vault.fs.vault_ops import EXCLUDED_INDEX_DIRS
from .sync import compare_files
logger = logging.getLogger("md-vault.api")

# In-memory sync lock: {"locked_at": float} or None
_sync_lock: dict | None = None
_SYNC_LOCK_TIMEOUT = 300  # 5 minutes


def _acquire_lock() -> bool:
    """Try to acquire the sync lock. Returns True if acquired."""
    global _sync_lock
    now = time.time()
    if _sync_lock is not None and (now - _sync_lock["locked_at"]) < _SYNC_LOCK_TIMEOUT:
        return False
    _sync_lock = {"locked_at": now}
    return True


def _release_lock() -> None:
    global _sync_lock
    _sync_lock = None


def _safe_rel_path(raw: str) -> Path | None:
    """Validate a URL-decoded relative path. Returns None if unsafe.

    Paths are normalized to forward slashes (vault-relative).
    """
    raw = raw.replace("\\", "/")
    if not raw or raw.startswith("/"):
        return None
    parts = Path(raw).parts
    if any(p in ("..", "") for p in parts):
        return None
    return Path(*parts)


def _hash_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def create_app(vault_dir: Path, db_conn, ai_config=None) -> FastAPI:
    """Create the FastAPI application."""
    app = FastAPI(title="md-sync server")
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "templates")), name="static")

    session_mgr = SessionManager(db_conn, vault_dir)

    # Agent instances per session (created on first chat, reused afterwards).
    agent_cache: dict[str, Agent] = {}

    def _build_agent_spec() -> AgentSpec:
        """Declarative agent spec from the server's AI config."""
        return AgentSpec(
            system_prompt=SYSTEM_PROMPT,
            model=ModelConfig(
                model_id=ai_config.model,
                provider="openai",
                base_url=ai_config.base_url,
                api_key=ai_config.api_key,
                context_window=ai_config.context_window,
            ),
            tools=list(TOOL_REGISTRY.values()),
            system_reminders=[TimeSystemReminder()],
            user_reminders=[DocPathUserReminder()],
            max_rounds=ai_config.max_rounds,
            vault_dir=vault_dir,
            read_doc_max_chars=ai_config.read_doc_max_chars,
        )

    def _get_agent(session_id: str) -> Agent:
        agent = agent_cache.get(session_id)
        if agent is None:
            messages = session_mgr.get_or_create(session_id)
            agent = Agent(_build_agent_spec(), messages=messages)
            agent_cache[session_id] = agent
        return agent

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "index.html")

    @app.get("/api/health")
    async def health() -> dict:
        files = get_all_files(db_conn)
        return {"status": "ok", "file_count": len(files)}

    @app.get("/api/docs")
    async def list_docs(request: Request):
        # memory-runtime v1: suggestions/index are system dirs — neither the
        # document tree nor its file list may surface them (top-level match
        # only; deeper same-named dirs are user content).
        files = [
            f for f in get_all_files(db_conn)
            if len(f["path"].split("/")) <= 1
            or f["path"].split("/")[0] not in EXCLUDED_INDEX_DIRS
        ]
        dirs = []
        for root, dirnames, _ in os.walk(vault_dir):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]  # 剪枝：不 descend 隐藏目录
            rel_root = Path(root).relative_to(vault_dir)
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
        from .vault.db.changes import get_changes as _get_changes

        return _get_changes(db_conn, since=since)

    @app.get("/api/graph/backlinks")
    async def graph_backlinks(path: str) -> list[dict]:
        from .vault.service import graph_get_backlinks

        return graph_get_backlinks(db_conn, path)

    @app.get("/api/graph/related")
    async def graph_related(path: str, hops: int = 1) -> list[dict]:
        from .vault.service import graph_get_related

        return graph_get_related(db_conn, path, hops=hops)

    @app.get("/api/docs/{path:path}")
    async def get_doc(path: str) -> PlainTextResponse:
        rel = _safe_rel_path(path)
        if rel is None:
            raise HTTPException(status_code=400, detail="invalid path")
        file_path = vault_dir / rel
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
            rel = _safe_rel_path(body.get("path") or "")
            if rel is None:
                raise HTTPException(status_code=400, detail="invalid path")
            file_path = vault_dir / rel
            if file_path.exists():
                raise HTTPException(status_code=409, detail="already exists")
            content = (body.get("content") or "").encode("utf-8")
            from .write_registry import register_write

            register_write(rel.as_posix(), "web")
            try:
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_bytes(content)
            except OSError as e:
                raise HTTPException(status_code=500, detail=f"failed to create file: {e}")
            apply_change(db_conn, vault_dir, rel.as_posix(), "created", source="web")
            return {"ok": True, "path": rel.as_posix()}
        if action == "create_folder":
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
            log_change(db_conn, rel.as_posix(), "created", source="web")
            return {"ok": True, "path": rel.as_posix()}
        if action in ("rename", "move"):
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
            from .vault.orchestration.refs import _update_references_batch

            if src.is_file():
                moves = [(src_rel.as_posix(), dst_rel.as_posix())]
            else:  # folder: expand to internal .md files, preserving nested structure
                moves = [
                    (p.relative_to(vault_dir).as_posix(), (dst_rel / p.relative_to(src)).as_posix())
                    for p in sorted(src.rglob("*.md")) if p.is_file()
                ]
            from .write_registry import register_write

            for _, new in moves:
                register_write(new, "web")
            # pre_rename=True: update refs before rename so uniqueness check is correct
            _update_references_batch(db_conn, vault_dir, moves, pre_rename=True)
            was_dir = src.is_dir()
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                src.rename(dst)
            except OSError as e:
                raise HTTPException(status_code=500, detail=f"failed to rename: {e}")
            if was_dir:
                log_change(db_conn, dst_rel.as_posix(), "moved", from_path=src_rel.as_posix(), source="web")
            # segments_fts_* triggers keep FTS in sync per row; no batch suppression needed
            for old, new in moves:
                apply_change(
                    db_conn,
                    vault_dir,
                    new,
                    "moved",
                    source="web",
                    from_path=old,
                    update_refs=False,
                )
            return {"ok": True, "path": dst_rel.as_posix()}
        if action == "delete":
            rel = _safe_rel_path(body.get("path") or "")
            if rel is None:
                raise HTTPException(status_code=400, detail="invalid path")
            target = vault_dir / rel
            if not target.exists():
                raise HTTPException(status_code=404, detail="not found")
            from .write_registry import register_write

            if target.is_file():
                register_write(rel.as_posix(), "web")
                try:
                    target.unlink()
                except OSError as e:
                    raise HTTPException(status_code=500, detail=f"failed to delete: {e}")
                apply_change(db_conn, vault_dir, rel.as_posix(), "deleted", source="web")
                return {"ok": True, "path": rel.as_posix(), "deleted_count": 1}
            # Folder: delete .md files one by one, clean up empty dirs bottom-up
            md_files = sorted(p for p in target.rglob("*.md") if p.is_file())
            rels = []
            for p in md_files:
                r = p.relative_to(vault_dir).as_posix()
                register_write(r, "web")
                p.unlink()
                rels.append(r)
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
                log_change(db_conn, rel.as_posix(), "deleted", source="web")
            # segments_fts_* triggers keep FTS in sync per row; no batch suppression needed
            for r in rels:
                apply_change(db_conn, vault_dir, r, "deleted", source="web")
            return {"ok": True, "path": rel.as_posix(), "deleted_count": len(rels)}
        raise HTTPException(status_code=400, detail=f"unknown action: {action}")

    @app.post("/api/docs/{path:path}")
    async def upload_doc(path: str, file: UploadFile = File(...)) -> dict:
        rel = _safe_rel_path(path)
        if rel is None:
            raise HTTPException(status_code=400, detail="invalid path")
        file_path = vault_dir / rel
        try:
            content = await file.read()
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_bytes(content)
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"failed to save file: {e}")
        digest = _hash_bytes(content)
        rel_str = rel.as_posix()
        from .write_registry import register_write

        register_write(rel_str, "web")
        return {"path": rel_str, "hash": digest}

    @app.get("/api/notifications")
    async def list_notifications(unread: int = 0) -> list[dict]:
        query = ("SELECT id, ts, alias, name, status, report_path, summary, read, "
                 "ops_total, files_changed FROM notifications")
        params: list = []
        if unread:
            query += " WHERE read = 0"
        query += " ORDER BY ts DESC LIMIT 50"
        rows = db_conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    @app.post("/api/notifications/read")
    async def mark_notifications_read(request: Request) -> dict:
        body = await request.json()
        if body.get("all"):
            db_conn.execute("UPDATE notifications SET read = 1 WHERE read = 0")
        else:
            ids = body.get("ids", [])
            if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
                raise HTTPException(status_code=400, detail="ids must be a list of ints")
            if ids:
                marks = ",".join("?" * len(ids))
                db_conn.execute(f"UPDATE notifications SET read = 1 WHERE id IN ({marks})", ids)
        db_conn.commit()
        return {"ok": True}

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

        if not _acquire_lock():
            return JSONResponse(
                status_code=409, content={"error": "sync in progress"}
            )

        try:
            server_files = {f["path"]: f["hash"] for f in get_all_files(db_conn)}

            # Fetch recent moved events since the client's last sync to
            # detect renames (prevents the client from pushing back a stale
            # pre-rename copy, which would recreate the old path).
            recent_moves = None
            if last_sync:
                from .vault.db.changes import get_changes as _get_changes

                all_changes = _get_changes(db_conn, since=float(last_sync), limit=200)
                recent_moves = [
                    c for c in all_changes
                    if c["action"] == "moved" and c.get("from_path")
                ]

            result = compare_files(local_files, server_files, recent_moves=recent_moves)

            # Background agent notifications since the client's last sync (cap 50).
            rows = db_conn.execute(
                "SELECT ts, alias, name, status, report_path, summary FROM notifications "
                "WHERE read = 0 AND ts > ? ORDER BY ts DESC LIMIT 50",
                (float(last_sync),)).fetchall()
            result["notifications"] = [dict(r) for r in rows]

            # Save client logs to vault_dir/.sync/logs/{device_name}.log
            safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", device_name)
            logs_dir = vault_dir / ".sync" / "logs"
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
            _release_lock()

    @app.post("/api/ai/search")
    async def ai_search(request: Request) -> dict:
        body = await request.json()
        query = body.get("query", "")
        top_k = int(body.get("top_k", 5))
        recall = body.get("recall", "strict")
        if recall not in ("strict", "near", "relaxed", "fuzzy"):
            raise HTTPException(status_code=400, detail="invalid recall tier")
        from .ai import rag_search
        results = rag_search(str(vault_dir), query, top_k=top_k, recall=recall)
        # Recall telemetry (memory-runtime v1, minimal): one row per hit.
        # Telemetry must never break the search response.
        try:
            now = time.time()
            rows = [
                (now, "web", query, r["path"], r.get("score"), recall)
                for r in results if isinstance(r, dict) and r.get("path")
            ]
            if rows:
                with _db_lock:
                    db_conn.executemany(
                        "INSERT INTO recall_log (ts, source, query, path, score, recall) "
                        "VALUES (?, ?, ?, ?, ?, ?)", rows)
                    db_conn.commit()
        except Exception:
            logger.exception("recall telemetry insert failed")
        return {"results": results, "recall": recall}

    @app.get("/api/recall")
    async def recall_log(since: float = 0.0) -> list[dict]:
        rows = db_conn.execute(
            "SELECT ts, source, query, path, score, recall FROM recall_log "
            "WHERE ts > ? ORDER BY ts DESC LIMIT 500",
            (since,),
        ).fetchall()
        return [dict(r) for r in rows]

    @app.post("/api/ai/chat")
    async def ai_chat(request: Request):
        if ai_config is None:
            raise HTTPException(status_code=503, detail="AI not configured")
        body = await request.json()
        message = body.get("message", "")
        mode = body.get("mode", "current_doc")
        doc_path = body.get("doc_path", "")

        # Build context
        messages = [{"role": "system", "content": "You are a helpful assistant for markdown documents."}]
        if mode == "current_doc" and doc_path:
            rel = _safe_rel_path(doc_path)
            if rel:
                fp = vault_dir / rel
                if fp.is_file():
                    content = fp.read_bytes().decode("utf-8", errors="replace")
                    messages.append({"role": "system", "content": f"Current document:\n\n{content}"})
        elif mode == "rag_search":
            from .ai import rag_search
            results = rag_search(str(vault_dir), message, top_k=3)
            if results:
                parts = []
                for r in results:
                    segs = "\n".join(
                        f"  [{seg['heading_path'] or 'no heading'}] {seg['content'][:200]}"
                        for seg in r["segments"] if seg["content"]
                    )
                    parts.append(f"[{r['path']}]:\n{segs}")
                context = "\n\n".join(parts)
                messages.append({"role": "system", "content": f"Relevant documents:\n\n{context}"})

        messages.append({"role": "user", "content": message})

        from .ai import chat_stream
        from fastapi.responses import StreamingResponse

        async def event_generator():
            async for line in chat_stream(
                base_url=ai_config.base_url,
                api_key=ai_config.api_key,
                model=ai_config.model,
                messages=messages,
                max_tokens=ai_config.max_tokens,
                temperature=ai_config.temperature,
            ):
                yield f"{line}\n\n"

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    @app.post("/api/agent/chat")
    async def agent_chat(request: Request):
        if ai_config is None:
            raise HTTPException(status_code=503, detail="AI not configured")
        body = await request.json()
        message = body.get("message", "")
        session_id = body.get("session_id", "default")
        doc_path = body.get("doc_path", "")

        session_mgr.cleanup()
        session_mgr.get_or_create(session_id)

        # Pre-check doc_path（不修改 session，只验证；409 走 HTTP 异常）
        doc_context = None
        if doc_path:
            rel = _safe_rel_path(doc_path)
            if rel is None:
                logger.info("doc_context skipped: _safe_rel_path rejected %r", doc_path)
            elif not (vault_dir / rel).is_file():
                raise HTTPException(status_code=409, detail=f"doc_path '{rel.as_posix()}' not on disk")
            else:
                doc_context = rel

        lock = session_mgr.get_lock(session_id)
        await lock.acquire()

        async def event_generator():
            agent = _get_agent(session_id)
            exc_to_raise: BaseException | None = None
            try:
                # doc_context 注入 + user message 由 agent.chat 处理（user reminder）
                async for event in agent.chat(
                    message, doc_path=doc_context.as_posix() if doc_context else None
                ):
                    yield sse(event.type, event.data)
            except (GeneratorExit, asyncio.CancelledError):
                logger.error(
                    "SSE stream disconnected (session=%s, pending_confirms=%d)",
                    session_id, len(agent.pending_confirms),
                    exc_info=True,
                )
                exc_to_raise = sys.exc_info()[1]
            except Exception:
                logger.exception("Agent chat failed (session=%s)", session_id)
                exc_to_raise = sys.exc_info()[1]
            finally:
                # Persist session on every exit (normal, interrupt, or error)
                try:
                    session_mgr.save(session_id, agent.messages)
                except Exception:
                    logger.exception("Failed to persist session (session=%s)", session_id)
                lock.release()
                if exc_to_raise is not None:
                    raise exc_to_raise

        from fastapi.responses import StreamingResponse

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    @app.post("/api/agent/confirm")
    async def agent_confirm(request: Request) -> dict:
        body = await request.json()
        session_id = body.get("session_id", "default")
        tool_call_id = body.get("id", "")
        approved = bool(body.get("approved", False))

        agent = agent_cache.get(session_id)
        if agent is None:
            raise HTTPException(status_code=404, detail="session not found")

        if not agent.confirm(tool_call_id, approved):
            raise HTTPException(status_code=404, detail="no pending confirm for this tool call")

        return {"ok": True}

    @app.post("/api/agent/new-session")
    async def agent_new_session(request: Request) -> dict:
        new_id = session_mgr.create_new()
        return {"session_id": new_id}

    @app.put("/api/docs/{path:path}")
    async def save_doc(path: str, request: Request) -> dict:
        rel = _safe_rel_path(path)
        if rel is None:
            raise HTTPException(status_code=400, detail="invalid path")
        file_path = vault_dir / rel
        content = await request.body()
        existed = file_path.is_file()
        try:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_bytes(content)
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"failed to save: {e}")
        from .write_registry import register_write

        register_write(rel.as_posix(), "web")
        return {"ok": True}

    return app
