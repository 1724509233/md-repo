"""FastAPI application factory and API routes."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .db import get_all_files, upsert_file
from .sync import compare_files

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

    # Agent session store: session_id -> {"messages": [...], "confirm_queue": asyncio.Queue, "last_active": float}
    _agent_sessions: dict[str, dict] = {}
    _SESSION_TTL = 1800  # 30 minutes

    def _cleanup_sessions():
        now = time.time()
        expired = [sid for sid, s in _agent_sessions.items() if now - s.get("last_active", 0) > _SESSION_TTL]
        for sid in expired:
            del _agent_sessions[sid]

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "index.html")

    @app.get("/api/health")
    async def health() -> dict:
        files = get_all_files(db_conn)
        return {"status": "ok", "file_count": len(files)}

    @app.get("/api/docs")
    async def list_docs(request: Request):
        files = get_all_files(db_conn)
        body = json.dumps(files, sort_keys=True).encode()
        etag = hashlib.sha256(body).hexdigest()
        if request.headers.get("if-none-match") == f'"{etag}"':
            return Response(content=b"", status_code=304, headers={"ETag": f'"{etag}"'})
        return Response(content=body, media_type="application/json", headers={"ETag": f'"{etag}"'})

    @app.get("/api/docs/changes")
    async def get_changes(since: float = 0.0) -> list[dict]:
        from .db import get_changes as _get_changes

        return _get_changes(db_conn, since=since)

    @app.get("/api/graph/backlinks")
    async def graph_backlinks(path: str) -> list[dict]:
        from .db import graph_get_backlinks

        return graph_get_backlinks(db_conn, path)

    @app.get("/api/graph/related")
    async def graph_related(path: str, hops: int = 1) -> list[dict]:
        from .db import graph_get_related

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
        from .watcher import register_write

        register_write(rel_str, "web")
        upsert_file(db_conn, rel_str, digest, time.time())
        return {"path": rel_str, "hash": digest}

    @app.post("/api/sync")
    async def sync(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="invalid JSON body")

        device_name = body.get("device_name")
        local_files = body.get("local_files")
        logs = body.get("logs", [])

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
            result = compare_files(local_files, server_files)

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
        from .ai import rag_search
        results = rag_search(str(vault_dir), query, top_k=top_k)
        return {"results": results}

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
                context = "\n\n".join(f"[{r['path']}]: {r['snippet']}" for r in results)
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

        _cleanup_sessions()

        # Get or create session (restore from SQLite if persisted)
        if session_id not in _agent_sessions:
            from .agent import SYSTEM_PROMPT, _build_time_block
            from .db import load_session

            saved = load_session(db_conn, session_id) if db_conn else None
            if saved:
                _agent_sessions[session_id] = {
                    "messages": saved["messages"],
                    "confirm_queue": asyncio.Queue(),
                    "last_active": saved["last_active"],
                    "token_count": saved.get("token_count", 0),
                }
            else:
                _agent_sessions[session_id] = {
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "system", "content": _build_time_block()},
                    ],
                    "confirm_queue": asyncio.Queue(),
                    "last_active": time.time(),
                }
        session = _agent_sessions[session_id]
        session["last_active"] = time.time()

        # Inject current doc context: one-line path + change signal (not full content)
        if doc_path:
            rel = _safe_rel_path(doc_path)
            if rel:
                rel_str = rel.as_posix()
                fp = vault_dir / rel
                if fp.is_file():
                    # Check for external changes since agent last read this doc
                    last_read_ts = session.get("last_read_ts", {}).get(rel_str, 0)
                    warning = ""
                    if last_read_ts > 0 and db_conn:
                        from .db import get_changes
                        changes = get_changes(db_conn, since=last_read_ts, path=rel_str, limit=1)
                        if changes:
                            warning = " ⚠ 自上次读取后有修改"
                    doc_context = f"[当前打开: {rel_str}{warning}]"
                    # Remove any previous doc context (user message prefix)
                    session["messages"] = [
                        m for m in session["messages"]
                        if not (m.get("role") == "user" and m.get("content", "").startswith("[当前打开:"))
                    ]
                    session["messages"].append({"role": "user", "content": doc_context})

        session["messages"].append({"role": "user", "content": message})

        from .agent import TOOLS, agent_loop

        async def event_generator():
            try:
                async for event in agent_loop(
                    messages=session["messages"],
                    tools=TOOLS,
                    ai_config=ai_config,
                    confirm_queue=session["confirm_queue"],
                    vault_dir=vault_dir,
                    db_conn=db_conn,
                    last_read_ts=session.setdefault("last_read_ts", {}),
                    context_window=ai_config.context_window,
                    max_rounds=ai_config.max_rounds,
                    read_doc_max_chars=ai_config.read_doc_max_chars,
                ):
                    yield event
                # Persist session after agent_loop completes
                from .db import save_session

                save_session(db_conn, session_id, session["messages"], session.get("token_count", 0))
            except (GeneratorExit, asyncio.CancelledError):
                return

        from fastapi.responses import StreamingResponse

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    @app.post("/api/agent/confirm")
    async def agent_confirm(request: Request) -> dict:
        body = await request.json()
        session_id = body.get("session_id", "default")
        approved = bool(body.get("approved", False))

        session = _agent_sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")

        session["confirm_queue"].put_nowait(approved)
        return {"ok": True}

    @app.post("/api/agent/new-session")
    async def agent_new_session(request: Request) -> dict:
        import uuid

        body = await request.json()
        old_session_id = body.get("old_session_id", "")

        new_id = str(uuid.uuid4())

        from .db import load_session, save_session
        from .agent import SYSTEM_PROMPT, _build_time_block

        new_messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "system", "content": _build_time_block()},
        ]

        _agent_sessions[new_id] = {
            "messages": new_messages,
            "confirm_queue": asyncio.Queue(),
            "last_active": time.time(),
            "token_count": 0,
        }

        if db_conn:
            save_session(db_conn, new_id, new_messages, token_count=0)

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
        from .watcher import register_write

        register_write(rel.as_posix(), "web")
        digest = _hash_bytes(content)
        upsert_file(db_conn, rel.as_posix(), digest, time.time())
        return {"ok": True}

    return app
