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
    apply_folder_change,
    get_all_files,
    upsert_file,
)
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


def _proposal_title(p: dict) -> str:
    """Console display title for a record: the first source ref (the note
    the proposal is about), falling back to the type. Records carry no
    free-form title (v1's body H1); this keeps the response shape stable."""
    refs = p.get("source_refs") or []
    return refs[0] if refs else (p.get("type") or "")


def _proposal_sort_key(item: dict) -> tuple:
    """(status rank, -age_days): review queue first, then newest within rank."""
    return (_PROPOSAL_STATUS_ORDER.get(item["status"], 100), -item["age_days"])


def _proposal_trails(conn, items: list[dict]) -> list[list[dict]]:
    """Per-item trails (newest first) for the proposals list.

    Same match as the /trail endpoint: each record's state-machine
    transitions (the ``proposal_transitions`` rows — the authoritative
    per-record state ledger; pre-D25 the change_log tail of the projection
    file under-reported them, back-to-back re-projections coalescing into
    one file row) plus executor changes carrying the record's run_id as
    batch_id on other paths. One query per source for the whole list —
    a per-item query would be N round-trips; a batch row matching several
    items (a shared run_id) is repeated for each of them.

    Returns a list of row lists parallel to ``items``; each row dict is
    shaped like the /trail endpoint's (seq/ts/source/op/path, no
    batch_id).
    """
    if not items:
        return []
    paths = [it["relpath"] for it in items]
    marks = ",".join("?" * len(paths))
    tr_map: dict[str, list[dict]] = {}
    for t in conn.execute(
        "SELECT t.id, t.ts, t.actor, t.to_status, p.relpath "
        "FROM proposal_transitions t JOIN proposals p ON p.id = t.proposal_id "
        f"WHERE p.relpath IN ({marks}) ORDER BY t.ts",
        paths,
    ).fetchall():
        tr_map.setdefault(t["relpath"], []).append(
            {"seq": t["id"], "ts": t["ts"], "source": t["actor"],
             "op": t["to_status"], "path": t["relpath"]})

    run_ids = sorted({it["run_id"] for it in items if it.get("run_id")})
    batch_map: dict[str, list[dict]] = {}
    if run_ids:
        rows = conn.execute(
            "SELECT seq, ts, source, op, path, batch_id FROM change_log "
            f"WHERE batch_id IN ({','.join('?' * len(run_ids))})",
            run_ids,
        ).fetchall()
        for r in rows:
            d = dict(r)
            bid = d.pop("batch_id")
            batch_map.setdefault(bid, []).append(d)

    out: list[list[dict]] = []
    for it in items:
        rel = it["relpath"]
        merged = list(tr_map.get(rel, []))
        if it.get("run_id"):
            # Legacy (pre-D25) projection-file rows are the coalesced shadow
            # of the record's transitions (never a source); keep other paths.
            merged += [d for d in batch_map.get(it["run_id"], [])
                       if d["path"] != rel]
        out.append(sorted(merged, key=lambda x: x["ts"], reverse=True))
    return out


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


def _guard_report_relpath(relpath: str) -> str:
    """Validate a report relpath lives under the machine-zone report channel.

    The report channel is the advisory surface of the machine zone
    (``index/reports/<alias>/YYYY-MM-DD.md``, ADR-0004); the guard mirrors
    ``_guard_proposal_relpath`` (same defense, different root — the card
    surface is ``suggestions/``, the report surface is ``index/reports/``).

    Returns the normalized posix relpath; raises HTTPException otherwise
    (unsafe path or outside the report channel → 400).
    """
    safe = _safe_rel_path(relpath)
    if safe is None:
        raise HTTPException(status_code=400, detail="invalid path")
    rel = safe.as_posix()
    if not rel.startswith("index/reports/"):
        raise HTTPException(status_code=400, detail="not a report path")
    return rel


def create_app(vault_dir: Path, db_conn, ai_config=None,
               bg_targets: dict[str, dict] | None = None) -> FastAPI:
    """Create the FastAPI application.

    ``bg_targets``: optional per-alias caps from the background-agent
    config, ``{alias: {"armed_target": int | None, "runs_cap": int | None}}``
    (wired in main.py from BackgroundAgentConfig: trigger.arm_count /
    budget_runs_per_day). None → the status endpoint reports the
    config-derived caps as null (pre-memory-console shape, backward
    compatible).
    """
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

            # Web op: logged synchronously (the console feed must respond
            # instantly while the user watches); consumer_skip=True stops
            # the watcher from booking a second row for the same write.
            register_write(rel.as_posix(), "web", consumer_skip=True)
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
            apply_folder_change(db_conn, vault_dir, rel.as_posix(), "created", source="web")
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
            # Web op: per-file moved rows logged synchronously (console
            # feed); consumer_skip=True stops the watcher from doubling.
            # Reference rewriting ran in batch above — the per-file passes
            # skip their own ref update (idempotent either way).
            for _, new in moves:
                register_write(new, "web", consumer_skip=True)
            # pre_rename=True: update refs before rename so uniqueness check is correct
            _update_references_batch(db_conn, vault_dir, moves, pre_rename=True)
            was_dir = src.is_dir()
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                src.rename(dst)
            except OSError as e:
                raise HTTPException(status_code=500, detail=f"failed to rename: {e}")
            if was_dir:
                apply_folder_change(
                    db_conn, vault_dir, dst_rel.as_posix(), "moved", source="web",
                    from_path=src_rel.as_posix())
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
                # 可逆层（P1，工作项 6）：人在 Web UI 删文件 = 全文快照兜底
                # （删除类快照单独保留 30 天，回滚端点可恢复）。
                from .snapshots import capture, finalize, gc_after_capture

                snap = capture(db_conn, vault_dir, rel.as_posix(), "delete", actor="web")
                # Web op: logged synchronously below; consumer_skip=True
                # stops the watcher from booking a second row.
                register_write(rel.as_posix(), "web", consumer_skip=True)
                try:
                    target.unlink()
                except OSError as e:
                    if snap is not None:
                        from .snapshots import discard

                        discard(db_conn, vault_dir, snap)
                    raise HTTPException(status_code=500, detail=f"failed to delete: {e}")
                if snap is not None:
                    finalize(db_conn, snap, "")  # post-state: file absent
                    gc_after_capture(db_conn, vault_dir)
                apply_change(db_conn, vault_dir, rel.as_posix(), "deleted", source="web")
                return {"ok": True, "path": rel.as_posix(), "deleted_count": 1}
            # Folder: delete .md files one by one, clean up empty dirs bottom-up
            from .snapshots import capture, discard, finalize, gc_after_capture, new_call_id

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
                # Web op: per-file rows logged synchronously below;
                # consumer_skip=True stops the watcher from doubling.
                register_write(r, "web", consumer_skip=True)
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
                apply_folder_change(db_conn, vault_dir, rel.as_posix(), "deleted", source="web")
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
                 "ops_total, files_changed, proposal_paths, report_paths FROM notifications")
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
                "SELECT ts, alias, name, status, report_path, summary, "
                "proposal_paths, report_paths "
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
        """List every proposal record (DB = the state source of truth; the
        suggestions/ files are inert legacy projections, never scanned). Response shape
        is stable vs the v1 frontmatter list: ``reversible`` derives from
        the authorization tier, ``title`` from source_refs/type."""
        from . import proposals

        now = time.time()
        items: list[dict] = []
        for p in proposals.scan(db_conn):
            created_iso = datetime.fromtimestamp(p["created_at"]).strftime("%Y-%m-%d")
            age_days = int((now - p["created_at"]) // 86400)
            items.append({
                "relpath": p["relpath"], "type": p["type"], "actor": p["actor"],
                "run_id": p["run_id"], "status": p["status"],
                "reversible": p["type"] in proposals.AUTO_TYPES,
                "source_refs": p["source_refs"],
                "reject_reason": p["reject_reason"], "merged_at": p["merged_at"],
                "title": _proposal_title(p), "created": created_iso,
                "age_days": age_days,
            })

        items.sort(key=_proposal_sort_key)
        # Trail summary on each row (the design's anomaly card shows a one-line
        # trail without a drill): latest 3 trail rows per proposal (state
        # transitions + executor rows), same match as the /trail endpoint;
        # one query per source for the whole list.
        trails = _proposal_trails(db_conn, items)
        for it, tr in zip(items, trails):
            it["trail"] = tr[:3]
            it["trail_count"] = len(tr)
        return {"proposals": items}

    @app.post("/api/memory/proposals/{relpath:path}/adopt")
    async def memory_proposal_adopt(relpath: str) -> dict:
        """Move an awaiting_review proposal to adopted (actor "human").

        The API never applies the change; the executor merges adopted
        proposals on its next cycle.
        """
        from . import proposals

        rel = _guard_proposal_relpath(relpath)
        rec = proposals.record_by_relpath(db_conn, rel)
        if rec is None:
            raise HTTPException(status_code=404, detail="proposal not found")
        if rec["status"] != "awaiting_review":
            raise HTTPException(
                status_code=400,
                detail=f"status is {rec['status']!r}, must be awaiting_review")
        try:
            proposals.advance(db_conn, vault_dir, rec["id"], "adopted", actor="human")
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
        rec = proposals.record_by_relpath(db_conn, rel)
        if rec is None:
            raise HTTPException(status_code=404, detail="proposal not found")
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="invalid JSON body")
        reason = body.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise HTTPException(status_code=400, detail="reason is required")
        reason = reason.strip()
        if rec["status"] != "awaiting_review":
            raise HTTPException(
                status_code=400, detail=f"status is {rec['status']!r}, must be awaiting_review")
        try:
            proposals.advance(db_conn, vault_dir, rec["id"], "rejected",
                              actor="human", reject_reason=reason)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="proposal not found")
        return {"relpath": rel, "status": "rejected", "reject_reason": reason}

    @app.get("/api/memory/proposals/{relpath:path}/trail")
    async def memory_proposal_trail(relpath: str) -> dict:
        """Read-only trail for one proposal (newest first, max 12 rows).

        The transition segment is the record's state-machine history from
        ``proposal_transitions`` (the authoritative per-record state ledger
        — pre-D25 the change_log tail of the projection file under-reported
        it: back-to-back re-projections coalesced into one file row), plus
        executor changes that carry the record's run_id as batch_id on
        other paths (the API only moves state; the executor lands the
        change). Record-less (legacy) projection files fall back to the
        change_log tail on the file itself."""
        from . import proposals

        rel = _guard_proposal_relpath(relpath)
        rec = proposals.record_by_relpath(db_conn, rel)
        rows: list[dict] = []
        if rec is not None:
            for t in db_conn.execute(
                "SELECT id, ts, actor, to_status FROM proposal_transitions "
                "WHERE proposal_id = ? ORDER BY ts",
                (rec["id"],),
            ).fetchall():
                rows.append({"seq": t["id"], "ts": t["ts"],
                             "source": t["actor"], "op": t["to_status"],
                             "path": rel})
            if rec["run_id"]:
                for r in db_conn.execute(
                    "SELECT seq, ts, source, op, path FROM change_log "
                    "WHERE batch_id = ? AND path != ? ORDER BY ts",
                    (rec["run_id"], rel),
                ).fetchall():
                    rows.append(dict(r))
        else:
            for r in db_conn.execute(
                "SELECT seq, ts, source, op, path FROM change_log "
                "WHERE path = ? ORDER BY ts", (rel,),
            ).fetchall():
                rows.append(dict(r))
        rows.sort(key=lambda d: d["ts"], reverse=True)
        return {"relpath": rel, "trail": rows[:12]}

    @app.get("/api/memory/proposals/{relpath:path}")
    async def memory_proposal_detail(relpath: str) -> dict:
        """Read one proposal record — the console detail pane's content
        excerpt data source (D25: the projections/ files are gone; the
        excerpt body is the record's ``justification`` — byte-identical
        to the v1 projection's body paragraph).

        ``payload`` is the JSON-parsed object (empty column → None);
        ``created`` / ``updated`` are ISO dates (the list endpoint's
        style); ``merged_at`` passes the ledger's ISO timestamp through.
        """
        from . import proposals

        rel = _guard_proposal_relpath(relpath)
        rec = proposals.record_by_relpath(db_conn, rel)
        if rec is None:
            raise HTTPException(status_code=404, detail="proposal not found")
        return {
            "relpath": rec["relpath"], "type": rec["type"],
            "status": rec["status"], "actor": rec["actor"],
            "run_id": rec["run_id"], "payload": rec["payload"],
            "justification": rec["justification"],
            "source_refs": rec["source_refs"],
            "reject_reason": rec["reject_reason"],
            "merged_at": rec["merged_at"],
            "created": datetime.fromtimestamp(rec["created_at"]).strftime("%Y-%m-%d"),
            "updated": datetime.fromtimestamp(rec["updated_at"]).strftime("%Y-%m-%d"),
        }

    @app.post("/api/memory/rollback")
    async def memory_rollback(request: Request) -> JSONResponse:
        """Roll one tool call back — human-triggered only (spec §3.1, D2).

        Verify-all-then-write-all: every file in the call batch must still
        match the call's post-state on disk (hash == post_hash; absent for
        deletes). Any mismatch = a later write overwrote the call → the
        whole batch is refused (no blind writes) and the conflicting files
        are surfaced for the human to decide. Only when every file verifies
        are the pre-image bytes restored, one file at a time.

        Deliberately NOT an agent tool and NOT exposed to bgagent: rollback
        and rebase are human-exclusive verbs (spec §四 unmerge 行).
        """
        from . import snapshots as snap_mod

        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="invalid JSON body")
        call_id = body.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            raise HTTPException(status_code=400, detail="call_id is required")
        result = snap_mod.rollback(db_conn, vault_dir, call_id)
        if not result["ok"]:
            return JSONResponse(
                status_code=409,
                content={"ok": False, "conflicts": result["conflicts"]},
            )
        # Attribute the restored writes to the watcher ("web" = human via UI).
        from .write_registry import register_write as _rw

        for path in result["restored"]:
            _rw(path, "web")
        return {"ok": True, "restored": result["restored"]}

    @app.get("/api/memory/rollback-calls")
    async def memory_rollback_calls(limit: int = 50) -> dict:
        """The human's rollback entry (spec §3.1): recent call groups.

        The rollback unit is the tool call; this list is how a human finds
        the call_id to POST to /api/memory/rollback. Human-readable surface
        only — not an agent tool (rollback stays human-exclusive).
        """
        from . import snapshots as snap_mod

        calls = snap_mod.recent_calls(db_conn, limit=limit)
        for c in calls:
            c["files"] = c["files"].split("\n") if c["files"] else []
        return {"calls": calls}

    # ── memory console: report channel (read-only advisory, ADR-0004) ──────
    # The report channel lands under the machine zone (index/reports/<alias>/
    # YYYY-MM-DD.md); the console lists them and reads one. Advisory only —
    # never a write path, never a proposal-state change.

    @app.get("/api/memory/reports")
    async def memory_reports() -> dict:
        """List the report channel: index/reports/<alias>/*.md (two levels).

        Directory absent → empty list (never 500). Each item carries the
        file's first ``# `` heading as its title (absent/unreadable → ""),
        its size and mtime (epoch int). Sorted by mtime descending (newest
        report first).
        """
        reports = []
        root = vault_dir / "index" / "reports"
        if root.is_dir():
            for alias_dir in root.iterdir():
                if not alias_dir.is_dir():
                    continue
                for fp in alias_dir.iterdir():
                    if not (fp.is_file() and fp.suffix == ".md"):
                        continue
                    title = ""
                    try:
                        for line in fp.read_bytes().decode("utf-8", errors="replace").splitlines():
                            if line.startswith("# "):
                                title = line[2:].strip()
                                break
                    except OSError:
                        title = ""
                    rel = fp.relative_to(vault_dir).as_posix()
                    st = fp.stat()
                    reports.append({
                        "relpath": rel, "alias": alias_dir.name,
                        "date": fp.stem, "title": title,
                        "size": st.st_size, "mtime": int(st.st_mtime),
                    })
        reports.sort(key=lambda r: r["mtime"], reverse=True)
        return {"reports": reports}

    @app.get("/api/memory/reports/{relpath:path}")
    async def memory_report_detail(relpath: str) -> dict:
        """Read one report's markdown (the console detail face's content)."""
        rel = _guard_report_relpath(relpath)
        fp = vault_dir / rel
        if not fp.is_file():
            raise HTTPException(status_code=404, detail="report not found")
        try:
            content = fp.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            raise HTTPException(status_code=500, detail="failed to read report")
        return {"relpath": rel, "content": content}

    @app.get("/api/memory/status")
    async def memory_status() -> dict:
        """The 30-second scan target: proposal counts + review queue, the
        background-agent trigger state, and recall demand over 7/30 days."""
        from . import proposals
        from . import snapshots
        from .vault.db import recall as recall_db

        # proposals: by_status counts + the awaiting_review review queue.
        # DB records are the source (suggestions/ files are inert legacy projections).
        items = []
        now = time.time()
        for p in proposals.scan(db_conn):
            age_days = int((now - p["created_at"]) // 86400)
            items.append({
                "relpath": p["relpath"], "type": p["type"], "actor": p["actor"],
                "age_days": age_days, "title": _proposal_title(p), "status": p["status"],
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

        # agents: read trigger_state directly; the config-derived caps
        # (armed_target = trigger.arm_count, runs_cap = budget_runs_per_day)
        # arrive via bg_targets (wired in main.py) and are looked up per
        # alias — aliases absent from the config keep null.
        today = datetime.now().strftime("%Y-%m-%d")
        agents = []
        rows = db_conn.execute(
            "SELECT agent, last_run_ts, armed, runs_date, runs_count "
            "FROM trigger_state ORDER BY agent"
        ).fetchall()
        for r in rows:
            runs_today = r["runs_count"] or 0 if r["runs_date"] == today else 0
            caps = (bg_targets or {}).get(r["agent"]) or {}
            agents.append({
                "alias": r["agent"], "armed": r["armed"] or 0,
                "armed_target": caps.get("armed_target"),
                "last_run": r["last_run_ts"] or 0, "runs_today": runs_today,
                "runs_cap": caps.get("runs_cap"),
            })

        # recall: demand over 7d (failed) + 30d (top docs). Old DBs may lack
        # the recall tables → zeros + recall_error, never a 500.
        recall_out: dict = {}
        try:
            all_traffic = recall_db.doc_traffic(db_conn, time.time() - 30 * 86400)
            recall_out = {
                "failed_7d": len(recall_db.failed_queries(db_conn, time.time() - 7 * 86400)),
                "docs_tracked": len(all_traffic),
                "top": [{"path": t["path"], "weighted": t["weighted"],
                         "last_hit": t["last_hit"]} for t in all_traffic[:5]],
            }
        except Exception as e:
            recall_out = {
                "failed_7d": 0, "docs_tracked": 0, "top": [], "recall_error": str(e),
            }

        # executor: the deterministic 30-minute loop is a framework role, not
        # an LLM agent — it has no trigger_state row. Its activity is the
        # change_log rows it ledgered (source='executor', batch_id = the
        # merged proposal's run_id): last_op_ts = newest such row (0 when it
        # never ran), ops_today = the local-day window (same date basis as
        # runs_today), last_batch = that row's run_id (null when unrecorded).
        midnight = datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp()
        exec_row = db_conn.execute(
            "SELECT ts, batch_id FROM change_log WHERE source = 'executor' "
            "ORDER BY ts DESC, seq DESC LIMIT 1"
        ).fetchone()
        exec_ops_today = db_conn.execute(
            "SELECT count(*) FROM change_log "
            "WHERE source = 'executor' AND ts >= ?", (midnight,),
        ).fetchone()[0]
        if exec_row is None:
            executor_out = {"last_op_ts": 0, "ops_today": 0, "last_batch": None}
        else:
            executor_out = {
                "last_op_ts": int(exec_row["ts"]),
                "ops_today": exec_ops_today,
                "last_batch": exec_row["batch_id"] or None,
            }

        return {"proposals": {"by_status": by_status, "awaiting_review": awaiting},
                "agents": agents, "executor": executor_out, "recall": recall_out,
                "snapshot": {"degraded": snapshots.degraded_count()}}

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
