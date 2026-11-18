"""Egress audit policy/shaping layer (security redline 7, spec 2026-11-18 §3).

The half that decides WHAT gets audited and HOW — endpoint parsing, loopback
gating, the privacy double-insurance skip, fail-open. The SQL half is
``server/vault/db/egress_sql.py`` (lock-holding write face); the runtime
collection + per-run flush wiring is Task 7 (LoopRuntime + the two channel
target constructions).

Invariants pinned here:
- ``parse_endpoint`` never raises and never carries a key (userinfo is
  stripped) or a path/query segment (§3.1 endpoint_host comment).
- ``should_record`` is False ONLY for a pure loopback host — the
  ``(provider default)`` sentinel (and an empty host) counts as cloud
  (§3.3: local Ollama keeps the table empty, everything else records).
- ``flush_touches`` is fail-open (audit must never bite the run — recall
  ledger's discipline) and re-checks ``is_privacy`` per path: the double
  insurance (§3.4) — even if a future read surface leaks a privacy path
  into ``touched``, the audit table stays clean (path ∩ privacy = ∅).
"""
from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from server.vault.db import egress_sql

logger = logging.getLogger(__name__)

# base_url absent → the provider SDK's built-in endpoint → treat as cloud
# (spec §3.3: the fact that would silently break must not be silent).
DEFAULT_ENDPOINT = "(provider default)"


# ------------------------------------------------------------------ endpoints


def parse_endpoint(base_url: str | None) -> str:
    """``base_url`` → ``host[:port]``. Never raises.

    Absent/blank → ``DEFAULT_ENDPOINT``. Userinfo (a key carrier) is
    stripped so ``endpoint_host`` can never contain a credential; the
    path/query/fragment are dropped by construction. Malformed input
    degrades to the raw string (minus userinfo) — never an exception.
    """
    if base_url is None or not (raw := base_url.strip()):
        return DEFAULT_ENDPOINT
    try:
        netloc = urlparse(raw).netloc
        if not netloc:
            # no-scheme form "host[:port]/path" — the first segment is host
            netloc = raw.split("/", 1)[0]
        host = netloc.rsplit("@", 1)[-1]  # drop userinfo (key never appears)
        return host or DEFAULT_ENDPOINT
    except Exception:
        # urlparse raises ValueError on some malformed IPv6 literals — the
        # helper's contract is "never throws"; degrade, don't propagate.
        return raw.rsplit("@", 1)[-1].split("/", 1)[0] or DEFAULT_ENDPOINT


def _is_loopback_host(host: str) -> bool:
    """Loopback classification for an endpoint host (may carry a port).

    单一来源 = ``server.config.core.is_loopback``（spec §4.2：回环分类只此
    一处真值）。曾经的本地回退集合是第二处真值（评审 P2d 已删）：import
    失败就让它直接抛——沉默回退到一份可能漂移的分类表，正是「第二处真值」
    要消灭的。
    """
    from server.config.core import is_loopback

    h = (host or "").strip()
    # is_loopback takes a bare host; an endpoint host may carry a port
    # (and IPv6 brackets).
    if h.startswith("[") and "]" in h:
        h = h[1:h.index("]")]
    elif h.count(":") == 1:
        h = h.split(":", 1)[0]
    return is_loopback(h)


# ------------------------------------------------------------------ target


@dataclass
class EgressTarget:
    """Where one run's documents were shipped: built by the CHANNEL code
    (bgagent runner / interactive agent route), never from model-supplied
    data (trusted stamp, redline 3). ``model_id`` lands in the table's
    ``model`` column."""

    channel: str       # 'agent' | 'bgagent'
    actor: str         # 'agent' | bgagent alias
    run_id: str        # bgagent batch; interactive ''
    endpoint_host: str  # parse_endpoint(base_url)
    model_id: str      # model column


def should_record(target: EgressTarget) -> bool:
    """False ONLY for a pure loopback endpoint host (local model → the
    table stays empty); the ``(provider default)`` sentinel and anything
    else record."""
    host = (target.endpoint_host or "").strip()
    if not host or host == DEFAULT_ENDPOINT:
        return True
    return not _is_loopback_host(host)


# ------------------------------------------------------------------ flush


def _is_privacy_path(dirs: tuple[str, ...], path: str) -> bool:
    """Double-insurance privacy check (§3.4). Delayed import: the predicate
    is Task 2's ``server/privacy.py`` — while that module is absent nothing
    can be declared private (the mechanism itself does not exist), so the
    check is inert rather than fatal; once it lands the real predicate
    rules. Empty dirs short-circuits without importing."""
    if not dirs:
        return False
    try:
        from server.privacy import is_privacy
    except ImportError:
        return False
    return is_privacy(tuple(dirs), path)


def flush_touches(
    conn: sqlite3.Connection,
    privacy_dirs: tuple[str, ...],
    target: EgressTarget,
    touched: dict[str, str],
) -> None:
    """Write one ``egress_log`` row per touched document (path → via).

    Fail-open (spec §3.2, like the recall ledger): ANY failure — closed or
    broken conn, a predicate blowing up — is swallowed with a logged trace;
    auditing must never break the run it audits. Privacy paths are skipped
    here even though the upstream surfaces already filter them (§3.4).

    ``touched`` is the run's deduped first-touch set (path → via), collected
    by LoopRuntime (Task 7).
    """
    try:
        ts = time.time()
        for path, via in touched.items():
            if _is_privacy_path(privacy_dirs, path):
                continue
            egress_sql.insert_touch(
                conn,
                ts,
                target.channel,
                target.actor,
                target.run_id,
                target.endpoint_host,
                target.model_id,
                path,
                via,
            )
    except Exception:
        logger.exception("egress flush failed (fail-open, run continues)")


# ------------------------------------------------------------- startup broadcast


def log_model_endpoints(ai_config=None, bg_agents=()) -> list[str]:
    """Startup broadcast (spec 2026-11-18 §3.5, redline 7): one classified
    line per EFFECTIVE model endpoint — main AI + every background agent —
    tagged cloud/local. 出域必须可知: a fact-basis break must be a visible
    log event, never a silent fact.

    The api_key NEVER appears (only host[:port] + model id). Duck-typed
    inputs (``ai_config.base_url/.model``, ``bg_agent.alias/.model.base_url/
    .model.model_id``) so the helper stays config-module-agnostic. Returns
    the lines — the caller logs them, tests assert on them."""
    lines: list[str] = []

    def _fmt(scope: str, base_url, model_id) -> str:
        host = parse_endpoint(base_url)
        cloud = should_record(EgressTarget("", "", "", host, ""))
        return (f"model endpoint: {host} [{'cloud' if cloud else 'local'}] "
                f"{scope} model={model_id}")

    if ai_config is not None:
        lines.append(_fmt("main AI", ai_config.base_url, ai_config.model))
    for a in bg_agents:
        lines.append(_fmt(f"bgagent {a.alias}", a.model.base_url, a.model.model_id))
    return lines
