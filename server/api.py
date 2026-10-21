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
from datetime import datetime
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
from .vault.fs.vault_ops import EXCLUDED_INDEX_DIRS, safe_rel_path as _safe_rel_path
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


def _hash_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


# ── memory console helpers (proposals list / adopt / reject / pulse) ──────

# Console sort priority: actionable states first, terminal + parse failures
# last. awaiting_review is the human's review queue, so it always comes first.
_PROPOSAL_STATUS_ORDER = {
    "awaiting_review": 0,
    "pending": 1,
    "adopted": 2,
    "processing": 3,
    "failed": 4,
    "rejected": 5,
    "expired": 6,
    "merged": 7,
    "malformed": 8,
}
_PROPOSAL_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?=[_\- ])")


def _proposal_date_info(relpath: str) -> tuple:
    """(created_iso, created_ts) parsed from the ``YYYY-MM-DD`` filename
    prefix, or ``(None, None)`` when the prefix is absent/unparseable.

    Proposals carry no created_at frontmatter; the filename date is the
    creation date (mirrors proposals._created_ts).
    """
    m = _PROPOSAL_DATE_RE.match(Path(relpath).stem)
    if not m:
        return None, None
    try:
        y, mo, d = (int(x) for x in m.groups())
        dt = datetime(y, mo, d)
    except ValueError:
        return None, None
    return dt.strftime("%Y-%m-%d"), dt.timestamp()


def _proposal_sort_key(item: dict) -> tuple:
    """(status rank, -age_days): review queue first, then newest within rank."""
    return (_PROPOSAL_STATUS_ORDER.get(item["status"], 100), -item["age_days"])


def _guard_proposal_relpath(relpath: str) -> str:
    """Validate a proposal relpath is a top-level file under suggestions/.

    Returns the normalized posix relpath; raises HTTPException otherwise
    (unsafe path or not a top-level proposal file → 400).
    """
    from . import proposals

    safe = _safe_rel_path(relpath)
    if safe is None:
        raise HTTPException(status_code=400, detail="invalid path")
    parts = safe.parts
    if (
        len(parts) != 2
        or parts[0] != proposals.PROPOSALS_DIR
        or not parts[1].endswith(".md")
    ):
        raise HTTPException(status_code=400, detail="not a top-level proposal file")
    return safe.as_posix()


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
                 "ops_total, files_changed, proposal_paths FROM notifications")
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
                "SELECT ts, alias, name, status, report_path, summary, proposal_paths "
                "FROM notifications WHERE read = 0 AND ts > ? ORDER BY ts DESC LIMIT 50",
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
        # Recall telemetry (memory-runtime v2): one recall_queries row per
        # search + one recall_log row per hit. Must never break the response.
        from .vault.db import recall as recall_db
        try:
            recall_db.log_query(db_conn, "web", query, recall, results)
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

    @app.get("/api/recall/stats")
    async def recall_stats(days: float = 30, half_life: float = 30.0) -> dict:
        """Recall dimensioning over a window: per-doc traffic + decayed
        weights, and the zero-hit (failed-recall) ledger."""
        from .vault.db import recall as recall_db

        since = time.time() - days * 86400
        failed = recall_db.failed_queries(db_conn, since)
        return {
            "window_days": days,
            "half_life_days": half_life,
            "docs": recall_db.doc_traffic(db_conn, since,
                                          half_life_days=half_life)[:200],
            "failed_queries": failed[:200],
            "failed_count": len(failed),
        }

    # ── memory console (read-only list + one-click review + pulse) ────────
    # Constitution: the console only moves proposal state via the state
    # machine (ledgered with actor "human"); the executor — not the API —
    # applies adopted proposals on its next cycle.

    @app.get("/api/memory/proposals")
    async def memory_proposals() -> dict:
        """List every proposal in suggestions/ (top level) with frontmatter
        fields. A parse failure yields a ``malformed`` item, never a 500."""
        from . import proposals

        d = Path(vault_dir) / proposals.PROPOSALS_DIR
        if not d.is_dir():
            return {"proposals": []}

        now = time.time()
        items: list[dict] = []
        for fp in sorted(d.glob("*.md")):
            if not fp.is_file():
                continue
            rel = fp.relative_to(Path(vault_dir)).as_posix()
            stem = fp.stem
            created_iso, created_ts = _proposal_date_info(rel)
            age_days = int((now - created_ts) // 86400) if created_ts is not None else 0
            try:
                p = proposals.parse_proposal(fp)
            except Exception as e:
                items.append({
                    "relpath": rel, "type": "", "actor": "", "run_id": "",
                    "status": "malformed", "reversible": False, "source_refs": [],
                    "reject_reason": "", "merged_at": "", "title": stem,
                    "created": created_iso, "age_days": age_days, "error": str(e),
                })
                continue
            items.append({
                "relpath": rel, "type": p["type"], "actor": p["actor"],
                "run_id": p["run_id"], "status": p["status"],
                "reversible": p["reversible"], "source_refs": p["source_refs"],
                "reject_reason": p["reject_reason"], "merged_at": p["merged_at"],
                "title": p["title"] or stem,
                "created": created_iso, "age_days": age_days,
            })

        items.sort(key=_proposal_sort_key)
        return {"proposals": items}

    @app.post("/api/memory/proposals/{relpath:path}/adopt")
    async def memory_proposal_adopt(relpath: str) -> dict:
        """Move an awaiting_review proposal to adopted (actor "human").

        The API never applies the change; the executor merges adopted
        proposals on its next cycle.
        """
        from . import proposals

        rel = _guard_proposal_relpath(relpath)
        fp = Path(vault_dir) / rel
        if not fp.is_file():
            raise HTTPException(status_code=404, detail="proposal not found")
        try:
            cur = proposals.parse_proposal(fp)["status"]
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"malformed proposal: {e}")
        if cur != "awaiting_review":
            raise HTTPException(
                status_code=400, detail=f"status is {cur!r}, must be awaiting_review")
        try:
            proposals.advance(vault_dir, db_conn, rel, "adopted", actor="human")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="proposal not found")
        return {"relpath": rel, "status": "adopted"}

    @app.post("/api/memory/proposals/{relpath:path}/reject")
    async def memory_proposal_reject(relpath: str, request: Request) -> dict:
        """Move an awaiting_review proposal to rejected, threading the reason
        into reject_reason (the trust-upgrade data source)."""
        from . import proposals

        rel = _guard_proposal_relpath(relpath)
        fp = Path(vault_dir) / rel
        if not fp.is_file():
            raise HTTPException(status_code=404, detail="proposal not found")
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="invalid JSON body")
        reason = body.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise HTTPException(status_code=400, detail="reason is required")
        reason = reason.strip()
        try:
            cur = proposals.parse_proposal(fp)["status"]
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"malformed proposal: {e}")
        if cur != "awaiting_review":
            raise HTTPException(
                status_code=400, detail=f"status is {cur!r}, must be awaiting_review")
        try:
            proposals.advance(vault_dir, db_conn, rel, "rejected",
                              actor="human", reject_reason=reason)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="proposal not found")
        return {"relpath": rel, "status": "rejected", "reject_reason": reason}

    @app.get("/api/memory/status")
    async def memory_status() -> dict:
        """The 30-second scan target: proposal counts + review queue, the
        background-agent trigger state, and recall demand over 7/30 days."""
        from . import proposals
        from .vault.db import recall as recall_db

        # proposals: by_status counts + the awaiting_review review queue.
        items = []
        d = Path(vault_dir) / proposals.PROPOSALS_DIR
        if d.is_dir():
            now = time.time()
            for fp in sorted(d.glob("*.md")):
                if not fp.is_file():
                    continue
                rel = fp.relative_to(Path(vault_dir)).as_posix()
                stem = fp.stem
                created_iso, created_ts = _proposal_date_info(rel)
                age_days = int((now - created_ts) // 86400) if created_ts is not None else 0
                try:
                    p = proposals.parse_proposal(fp)
                except Exception:
                    p = {"status": "malformed", "type": "", "actor": "", "title": stem}
                items.append({
                    "relpath": rel, "type": p.get("type", ""), "actor": p.get("actor", ""),
                    "age_days": age_days, "title": p.get("title") or stem, "status": p["status"],
                })
        # by_status always carries the full status set (stable shape for the
        # poller); awaiting holds the review queue (oldest first).
        by_status = {
            s: 0 for s in ("pending", "processing", "awaiting_review", "adopted",
                           "rejected", "expired", "merged", "failed", "malformed")}
        awaiting = []
        for it in items:
            by_status[it["status"]] = by_status.get(it["status"], 0) + 1
            if it["status"] == "awaiting_review":
                awaiting.append({
                    "relpath": it["relpath"], "type": it["type"], "actor": it["actor"],
                    "age_days": it["age_days"], "title": it["title"],
                })
        # by_status keys are stable regardless of seed order; awaiting first.
        awaiting.sort(key=lambda x: -x["age_days"])

        # agents: read trigger_state directly. The background-agent configs
        # (arm_count, budget_runs_per_day) are loaded in main.py and NOT passed
        # to create_app, so the config-derived caps are unavailable here → null.
        today = datetime.now().strftime("%Y-%m-%d")
        agents = []
        rows = db_conn.execute(
            "SELECT agent, last_run_ts, armed, runs_date, runs_count "
            "FROM trigger_state ORDER BY agent"
        ).fetchall()
        for r in rows:
            runs_today = r["runs_count"] or 0 if r["runs_date"] == today else 0
            agents.append({
                "alias": r["agent"], "armed": r["armed"] or 0,
                "armed_target": None,  # config-derived (arm_count), unavailable here
                "last_run": r["last_run_ts"] or 0, "runs_today": runs_today,
                "runs_cap": None,  # config-derived (budget_runs_per_day), unavailable
            })

        # recall: demand over 7d (failed) + 30d (top docs). Old DBs may lack
        # the recall tables → zeros + recall_error, never a 500.
        recall_out: dict = {}
        try:
            all_traffic = recall_db.doc_traffic(db_conn, time.time() - 30 * 86400)
            recall_out = {
                "failed_7d": len(recall_db.failed_queries(db_conn, time.time() - 7 * 86400)),
                "docs_tracked": len(all_traffic),
                "top": [{"path": t["path"], "weighted": sum(t["weights"].values()),
                         "last_hit": t["last_hit"]} for t in all_traffic[:5]],
            }
        except Exception as e:
            recall_out = {
                "failed_7d": 0, "docs_tracked": 0, "top": [], "recall_error": str(e),
            }

        return {"proposals": {"by_status": by_status, "awaiting_review": awaiting},
                "agents": agents, "recall": recall_out}

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
