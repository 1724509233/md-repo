"""Memory domain: proposal console + rollback + report channel (URL prefix ``/api/memory``).

Order-sensitive within this module: the ``{relpath:path}/adopt|revive|reject|trail``
subpath routes must be registered before the ``GET {relpath:path}`` detail catchall —
the ``:path`` converter is greedy (``.*``, includes ``/``) and would shadow them,
turning adopt/revive/reject/trail into 400 "not a top-level proposal file" (measured).
"""
from __future__ import annotations

import time
from datetime import datetime

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from .. import ledger
from ..vault.fs.vault_ops import safe_rel_path as _safe_rel_path


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
    from .. import proposals

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


def register(app, ctx) -> None:
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
        from .. import proposals

        now = time.time()
        items: list[dict] = []
        for p in proposals.scan(ctx.db_conn):
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
        trails = _proposal_trails(ctx.db_conn, items)
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
        from .. import proposals

        rel = _guard_proposal_relpath(relpath)
        rec = proposals.record_by_relpath(ctx.db_conn, rel)
        if rec is None:
            raise HTTPException(status_code=404, detail="proposal not found")
        if rec["status"] != "awaiting_review":
            raise HTTPException(
                status_code=400,
                detail=f"status is {rec['status']!r}, must be awaiting_review")
        try:
            proposals.advance(ctx.db_conn, ctx.vault_dir, rec["id"], "adopted", actor="human")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="proposal not found")
        return {"relpath": rel, "status": "adopted"}

    @app.post("/api/memory/proposals/{relpath:path}/revive")
    async def memory_proposal_revive(relpath: str) -> dict:
        """Revive an expired proposal back into the review queue (invariant
        12: expired → awaiting_review is human-only — the agent tool surface
        has no revive entry; proposals.revive seals it at the state layer).

        The TTL window re-arms on revive (Q5): ``created_at`` is the expiry
        window base and ``proposals.revive`` sets it to now — the original
        creation moment stays in the state ledger (no audit loss).
        """
        from .. import proposals

        rel = _guard_proposal_relpath(relpath)
        rec = proposals.record_by_relpath(ctx.db_conn, rel)
        if rec is None:
            raise HTTPException(status_code=404, detail="proposal not found")
        if rec["status"] != "expired":
            raise HTTPException(
                status_code=400,
                detail=f"status is {rec['status']!r}, must be expired")
        try:
            proposals.revive(ctx.db_conn, ctx.vault_dir, rec["id"])
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="proposal not found")
        return {"relpath": rel, "status": "awaiting_review"}

    @app.post("/api/memory/proposals/{relpath:path}/reject")
    async def memory_proposal_reject(relpath: str, request: Request) -> dict:
        """Move an awaiting_review proposal to rejected, threading the reason
        into reject_reason (the trust-upgrade data source)."""
        from .. import proposals

        rel = _guard_proposal_relpath(relpath)
        rec = proposals.record_by_relpath(ctx.db_conn, rel)
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
            proposals.advance(ctx.db_conn, ctx.vault_dir, rec["id"], "rejected",
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
        from .. import proposals

        rel = _guard_proposal_relpath(relpath)
        rec = proposals.record_by_relpath(ctx.db_conn, rel)
        rows: list[dict] = []
        if rec is not None:
            for t in ctx.db_conn.execute(
                "SELECT id, ts, actor, to_status FROM proposal_transitions "
                "WHERE proposal_id = ? ORDER BY ts",
                (rec["id"],),
            ).fetchall():
                rows.append({"seq": t["id"], "ts": t["ts"],
                             "source": t["actor"], "op": t["to_status"],
                             "path": rel})
            if rec["run_id"]:
                for r in ctx.db_conn.execute(
                    "SELECT seq, ts, source, op, path FROM change_log "
                    "WHERE batch_id = ? AND path != ? ORDER BY ts",
                    (rec["run_id"], rel),
                ).fetchall():
                    rows.append(dict(r))
        else:
            for r in ctx.db_conn.execute(
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
        from .. import proposals

        rel = _guard_proposal_relpath(relpath)
        rec = proposals.record_by_relpath(ctx.db_conn, rel)
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
        from .. import snapshots as snap_mod

        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="invalid JSON body")
        call_id = body.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            raise HTTPException(status_code=400, detail="call_id is required")
        result = snap_mod.rollback(ctx.db_conn, ctx.vault_dir, call_id)
        if not result["ok"]:
            return JSONResponse(
                status_code=409,
                content={"ok": False, "conflicts": result["conflicts"]},
            )
        # Attribute the restored writes to the watcher ("web" = human via UI).
        for path in result["restored"]:
            ledger.register(path, "web")
        return {"ok": True, "restored": result["restored"]}

    @app.get("/api/memory/rollback-calls")
    async def memory_rollback_calls(limit: int = 50) -> dict:
        """The human's rollback entry (spec §3.1): recent call groups.

        The rollback unit is the tool call; this list is how a human finds
        the call_id to POST to /api/memory/rollback. Human-readable surface
        only — not an agent tool (rollback stays human-exclusive).
        """
        from .. import snapshots as snap_mod

        calls = snap_mod.recent_calls(ctx.db_conn, limit=limit)
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
        root = ctx.vault_dir / "index" / "reports"
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
                    rel = fp.relative_to(ctx.vault_dir).as_posix()
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
        fp = ctx.vault_dir / rel
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
        from .. import proposals
        from .. import snapshots
        from ..vault.db import recall as recall_db

        # proposals: by_status counts + the awaiting_review review queue.
        # DB records are the source (suggestions/ files are inert legacy projections).
        items = []
        now = time.time()
        for p in proposals.scan(ctx.db_conn):
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
        rows = ctx.db_conn.execute(
            "SELECT agent, last_run_ts, armed, runs_date, runs_count "
            "FROM trigger_state ORDER BY agent"
        ).fetchall()
        for r in rows:
            runs_today = r["runs_count"] or 0 if r["runs_date"] == today else 0
            caps = (ctx.bg_targets or {}).get(r["agent"]) or {}
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
            all_traffic = recall_db.doc_traffic(ctx.db_conn, time.time() - 30 * 86400)
            recall_out = {
                "failed_7d": len(recall_db.failed_queries(ctx.db_conn, time.time() - 7 * 86400)),
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
        exec_row = ctx.db_conn.execute(
            "SELECT ts, batch_id FROM change_log WHERE source = 'executor' "
            "ORDER BY ts DESC, seq DESC LIMIT 1"
        ).fetchone()
        exec_ops_today = ctx.db_conn.execute(
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
