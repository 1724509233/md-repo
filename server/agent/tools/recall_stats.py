"""recall_stats tool: the recall-QUALITY read face (spec 2026-11-13 §1.5,
D1 revised 2026-11-14).

Single responsibility: exposure → failure → consumption. The output is
COMPUTED signals — ratios, trend deltas and threshold-exceeded anomalies
are calculated here in code, plus a few evidence rows; the LLM only
interprets, never computes. This is NOT a recall data browser: raw detail
(the traffic table, all failed queries, all clicks) stays on the
``/api/recall/stats`` endpoint (the UI deep-dive face).

Answer quality (useful / grounded / cited-clicked) is the SEPARATE
``recall_feedback_stats`` tool (D23) — one output, one judgment.
"""
from __future__ import annotations

import time

from pydantic import BaseModel, Field

from .tool import Tool, ToolContext


class RecallStatsArgs(BaseModel):
    """Recall quality over a window: zero-hit / low-score / click signals,
    with the previous window as trend baseline."""
    days: int = Field(default=30, description="Lookback window in days (the previous window of the same length is the trend baseline)")
    half_life_days: float = Field(default=30.0, description="Time-decay half-life in days (demand-coverage weights)")


# Signal thresholds (spec §1.5: "Signals (threshold-exceeded only)").
# Boundary semantics (验收 1: 恰在阈值不出现): the signal appears only when
# the value EXCEEDS the threshold — exactly at the threshold it is silent.
REPEAT_FAILURE_THRESHOLD = 3      # same normalized query zero-hit > 3x
EXPOSED_NOT_CONSUMED_HITS = 10    # doc hits > 10 with zero clicks
DEMAND_HUMAN_W = 3.0              # demand-coverage: human_w >= 3 AND
DEMAND_ON_DEMAND_W = 1.0          # ... on_demand_w < 1 (G2 tier contrast)
LEAKAGE_CAP = 5                   # leaked paths listed per leakage signal


class RecallStatsTool(Tool):
    name = "recall_stats"
    args_model = RecallStatsArgs

    @staticmethod
    def no_data() -> str:
        return ("No recall data in window. Telemetry started recently, or no "
                "searches have been made yet.")

    # ── window metrics (one dict per window; prev = trend baseline) ──────

    def _window_metrics(self, conn, since: float, until: float,
                        half_life_days: float) -> dict:
        """All §1.5 formulas over (since, until]: query counts by source,
        zero-hit rate (overall + per source), low-score rate, exposed docs,
        consumed share, CTR@5, and the per-doc aggregates the signals need."""
        q_rows = conn.execute(
            "SELECT source, hits FROM recall_queries WHERE ts > ? AND ts <= ?",
            (since, until),
        ).fetchall()
        n_queries = len(q_rows)
        n_failed = sum(1 for r in q_rows if r["hits"] == 0)
        by_source: dict[str, dict] = {}
        for r in q_rows:
            s = by_source.setdefault(r["source"] or "", {"q": 0, "f": 0})
            s["q"] += 1
            if r["hits"] == 0:
                s["f"] += 1

        n_log = conn.execute(
            "SELECT count(*) FROM recall_log WHERE ts > ? AND ts <= ?",
            (since, until),
        ).fetchone()[0]
        # Same predicate as scan_recall_arming (score < 0.2, non-synthetic)
        # — the wakeup signal and its read face share one predicate.
        n_low = conn.execute(
            "SELECT count(*) FROM recall_log "
            "WHERE ts > ? AND ts <= ? AND score < 0.2 AND traffic != 'synthetic'",
            (since, until),
        ).fetchone()[0]

        # Exposed docs (≥1 hit) and consumed docs (≥1 click), per window.
        hit_paths = {r["path"] for r in conn.execute(
            "SELECT DISTINCT path FROM recall_log WHERE ts > ? AND ts <= ?",
            (since, until),
        ).fetchall()}
        click_paths = {r["path"] for r in conn.execute(
            "SELECT DISTINCT path FROM recall_clicks WHERE ts > ? AND ts <= ?",
            (since, until),
        ).fetchall()}
        exposed = len(hit_paths)
        consumed = len(hit_paths & click_paths)

        # CTR@5 (§4.5): clicks landing in the source search's top-5 /
        # all attributed (ranked) clicks.
        ranked = conn.execute(
            "SELECT count(*) FROM recall_clicks "
            "WHERE ts > ? AND ts <= ? AND rank IS NOT NULL",
            (since, until),
        ).fetchone()[0]
        top5 = conn.execute(
            "SELECT count(*) FROM recall_clicks "
            "WHERE ts > ? AND ts <= ? AND rank IS NOT NULL AND rank <= 5",
            (since, until),
        ).fetchone()[0]

        # Per-doc hits + decayed tier weights (demand-coverage signal).
        now = time.time()
        doc_rows = conn.execute(
            "SELECT path, traffic, ts FROM recall_log "
            "WHERE ts > ? AND ts <= ? ORDER BY ts DESC",
            (since, until),
        ).fetchall()
        docs: dict[str, dict] = {}
        for r in doc_rows:
            d = docs.get(r["path"])
            if d is None:
                d = docs[r["path"]] = {"hits": 0, "human_w": 0.0,
                                       "on_demand_w": 0.0}
            d["hits"] += 1
            decay = 0.5 ** (((now - r["ts"]) / 86400) / half_life_days)
            if r["traffic"] == "human":
                d["human_w"] += decay
            elif r["traffic"] == "on_demand":
                d["on_demand_w"] += decay
        click_counts = {r["path"]: r["c"] for r in conn.execute(
            "SELECT path, count(*) AS c FROM recall_clicks "
            "WHERE ts > ? AND ts <= ? GROUP BY path",
            (since, until),
        ).fetchall()}

        # Failed queries: per normalized query, per source (repeat failures)
        # + evidence rows (newest first).
        failed_rows = conn.execute(
            "SELECT ts, source, query, recall FROM recall_queries "
            "WHERE hits = 0 AND ts > ? AND ts <= ? ORDER BY ts DESC",
            (since, until),
        ).fetchall()
        fail_by_query: dict[str, dict] = {}
        for r in failed_rows:
            q = (r["query"] or "").strip().lower()
            entry = fail_by_query.setdefault(
                q, {"count": 0, "sources": {}, "evidence": []})
            entry["count"] += 1
            src = r["source"] or ""
            entry["sources"][src] = entry["sources"].get(src, 0) + 1
            if len(entry["evidence"]) < 5:
                entry["evidence"].append(r)

        # Leakage (§4.5): via=ai clicks whose path is NOT in that session's
        # recall_log path set — the agent cited a doc the search never
        # recalled (recall-gap signal).
        from ...vault.db import recall as recall_db
        ai_clicks = conn.execute(
            "SELECT path, session_id, query FROM recall_clicks "
            "WHERE ts > ? AND ts <= ? AND via = 'ai' AND session_id IS NOT NULL",
            (since, until),
        ).fetchall()
        leaked: dict[str, int] = {}
        for r in ai_clicks:
            recalled = recall_db._session_recall_paths(
                conn, r["session_id"], r["query"])
            if r["path"] not in recalled:
                leaked[r["path"]] = leaked.get(r["path"], 0) + 1

        return {
            "n_queries": n_queries, "n_failed": n_failed,
            "by_source": by_source,
            "zero_hit_rate": (n_failed / n_queries) if n_queries else None,
            "n_log": n_log,
            "low_score_rate": (n_low / n_log) if n_log else None,
            "exposed": exposed,
            "consumed_rate": (consumed / exposed) if exposed else None,
            "ctr5": (top5 / ranked) if ranked else None,
            "docs": docs,
            "click_counts": click_counts,
            "fail_by_query": fail_by_query,
            "leaked": leaked,
        }

    # ── rendering ─────────────────────────────────────────────────────────

    @staticmethod
    def _pct(rate: float | None) -> str:
        return "—" if rate is None else f"{rate * 100:.0f}%"

    @staticmethod
    def _pt_delta(cur: float | None, prev: float | None) -> str:
        """Trend delta in percentage points (None on either side → no note)."""
        if cur is None or prev is None:
            return ""
        d = (cur - prev) * 100
        if abs(d) < 0.5:
            return " → flat"
        return f" {'↑' if d > 0 else '↓'}{abs(d):.0f}pt"

    def _signals(self, m: dict) -> list[str]:
        """Threshold-exceeded signals only (spec §1.5). Boundary: exactly at
        the threshold the signal is silent (验收 1)."""
        lines: list[str] = []
        # repeat failures: same normalized query zero-hit > 3x in window.
        repeats = sorted(
            ((q, e) for q, e in m["fail_by_query"].items()
             if e["count"] > REPEAT_FAILURE_THRESHOLD),
            key=lambda ke: (-ke[1]["count"], ke[0]))
        for q, e in repeats:
            srcs = "/".join(f"{s}×{n}" for s, n in
                            sorted(e["sources"].items(),
                                   key=lambda kv: (-kv[1], kv[0])))
            lines.append(
                f'- repeat failures (>{REPEAT_FAILURE_THRESHOLD}x): '
                f'"{q}" {srcs} → suspected content gap')
        # exposed-not-consumed: hits > 10 with zero clicks.
        for path, d in sorted(m["docs"].items(), key=lambda kv: -kv[1]["hits"]):
            if d["hits"] > EXPOSED_NOT_CONSUMED_HITS \
                    and m["click_counts"].get(path, 0) == 0:
                lines.append(
                    f"- exposed-not-consumed: {path} "
                    f"({d['hits']} hits, 0 clicks)")
        # demand-coverage (G2): human demand with no task consumption.
        for path, d in sorted(m["docs"].items(),
                              key=lambda kv: -kv[1]["human_w"]):
            if d["human_w"] >= DEMAND_HUMAN_W \
                    and d["on_demand_w"] < DEMAND_ON_DEMAND_W:
                lines.append(
                    f"- demand-coverage: {path} (human_w {d['human_w']:.1f}, "
                    f"on_demand_w {d['on_demand_w']:.1f}) → task gap")
        # leakage: ai clicks on docs the session's search never recalled.
        if m["leaked"]:
            n = len(m["leaked"])
            paths = sorted(m["leaked"], key=lambda p: (-m["leaked"][p], p))
            shown = ", ".join(paths[:LEAKAGE_CAP])
            more = f" (+{n - LEAKAGE_CAP} more)" if n > LEAKAGE_CAP else ""
            lines.append(
                f"- leakage: {n} ai-cited docs not in session recall set "
                f"({shown}{more}) → recall gap")
        return lines

    def _execute(self, args, ctx: ToolContext) -> str:
        from ...vault.db import recall as recall_db
        from ...vault.db.conn import get_index_conn

        conn = get_index_conn()
        if conn is None:
            return "Error: database not available"
        now = time.time()
        window = args.days * 86400
        cur = self._window_metrics(conn, now - window, now,
                                   args.half_life_days)
        prev = self._window_metrics(conn, now - 2 * window, now - window,
                                    args.half_life_days)
        if cur["n_queries"] == 0 and cur["n_log"] == 0 and not cur["docs"]:
            return self.no_data()
        ctx.last_meta = {"count": cur["exposed"]}

        # ── summary lines (computed signals, not raw rows) ────────────────
        # G4: the zero-hit rate is split by source (web/agent) — the
        # sentinel must tell "the user can't find it" from "an external
        # agent can't find it".
        srcs = "/".join(
            f"{s} {n['q']}" for s, n in sorted(cur["by_source"].items()))
        src_zero = "/".join(
            f"{s} {self._pct(n['f'] / n['q'] if n['q'] else None)}"
            for s, n in sorted(cur["by_source"].items()))
        zero_hit = f"zero-hit {self._pct(cur['zero_hit_rate'])}"
        if src_zero:
            zero_hit += f" ({src_zero}"
            if prev["zero_hit_rate"] is not None:
                zero_hit += (f"; prev {self._pct(prev['zero_hit_rate'])}"
                             f"{self._pt_delta(cur['zero_hit_rate'], prev['zero_hit_rate'])}")
            zero_hit += ")"
        elif prev["zero_hit_rate"] is not None:
            zero_hit += (f" (prev {self._pct(prev['zero_hit_rate'])}"
                         f"{self._pt_delta(cur['zero_hit_rate'], prev['zero_hit_rate'])})")
        low_score = f"low-score {self._pct(cur['low_score_rate'])}"
        delta = self._pt_delta(cur["low_score_rate"], prev["low_score_rate"])
        if delta:
            low_score += f" ({delta.strip()})"
        ctr5 = f"CTR@5 {cur['ctr5']:.2f}" if cur["ctr5"] is not None \
            else "CTR@5 —"
        if cur["ctr5"] is not None and prev["ctr5"] is not None:
            ctr5 += f" (prev {prev['ctr5']:.2f})"
        queries_line = f"queries {cur['n_queries']}"
        if srcs:
            queries_line += f" ({srcs})"
        lines = [
            f"Recall health ({args.days}d, vs prev {args.days}d)",
            f"{queries_line} | {zero_hit} | {low_score}",
            (f"consumed {self._pct(cur['consumed_rate'])} of exposed docs"
             f" | {ctr5}"),
        ]

        # ── signals (threshold-exceeded only) + evidence ──────────────────
        signals = self._signals(cur)
        if signals:
            lines.append("")
            lines.append("Signals (threshold-exceeded only):")
            lines.extend(signals)
            # Evidence: the newest failed-query rows backing the repeat-
            # failure signals (ts source tier query — the G3 tier contrast).
            evidence = []
            for q, e in cur["fail_by_query"].items():
                if e["count"] > REPEAT_FAILURE_THRESHOLD:
                    evidence.extend(e["evidence"])
            if evidence:
                evidence.sort(key=lambda r: r["ts"], reverse=True)
                lines.append("Evidence (top rows backing each signal):")
                for r in evidence[:10]:
                    ts = time.strftime("%m-%d %H:%M", time.localtime(r["ts"]))
                    lines.append(
                        f"{ts} {r['source'] or ''} {r['recall'] or ''} "
                        f"{r['query']}")
        return "\n".join(lines)
