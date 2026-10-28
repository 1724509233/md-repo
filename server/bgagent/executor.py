"""Deterministic executor (memory-runtime v1, reversible-automation v2 P2).

The ONLY component allowed to translate ``status: adopted`` proposals into
vault changes (constitution iron rule 1). Pure determinism — no LLM.

One cycle (:func:`run_executor`):

1. Read ``status: adopted`` proposal records from the vault index DB and
   merge them through the per-type handlers (v2a: the suggestions/ files
   no longer exist — the record IS the state, never scanned).
2. Retired-type records (v1 ``link`` / ``correction`` / ``archive``) are
   parked in ``failed`` with an actionable reason — there is no v2 handler
   (the verbs were retired, spec §四 退役三件); human processes them
   (spec: 存量提案人工处理，不迁移).
3. Expire overdue ``awaiting_review`` records (14 days, created_at).
4. Return ``{"merged": n, "expired": n, "errors": [str, ...]}``.

Handler coverage (v2 verb table, spec §四):

- ``entity_index`` — upsert a ``## entity`` section into ``index/entities.md``
  (structured attrs, D7: fields not prose; machine zone).
- ``relatedness`` — insert/refresh one edge row in the ``relatedness`` table
  (DB-only, D9: zero file landing; the state transition ledger suffices).
- ``summary`` — dual form (D8): cache ``index/summaries/<stem>-summary.md``
  (slot, regenerable) / report ``index/summaries/<date>_summary_<slug>.md``
  (independent file, per-assertion refs validated for non-emptiness and
  existence, advisory title frame, never merged into user notes).
- ``errata`` — additive contradiction card ``index/errata/<stem>.md``
  (D6: surface + annotated candidate, single gate; the original text is
  never touched, no pointer added).
- ``reminder`` — intentionally absent (no landing, v1 design: the console
  notification card IS the delivery). Recorded in ``errors``, stays adopted.

Snapshot tiers (D12: machine-zone snapshots default OFF in v2b; LLM-unique
output forced ON in v2a — R2): every handler here runs the forced capture
pattern (capture before write → finalize after). v2b will gate the
*regenerable* handlers (``entity_index``) behind the config switch; no
switch exists in v2a (all forced on). Relatedness inserts no file, so it
has nothing to capture.

Merge handlers register the write as source="executor" with the run's
batch_id (ledger.register); the watcher consumer then ledgeres the file row
via ``apply_change`` (exactly one row, correctly attributed — the writer
never calls ``apply_change`` itself). Relatedness lands zero files (D9 +
D25): no file write, no file ledger row — the advance() state-ledger row
(proposal_transitions) is this handler's state record.

Failure taxonomy:

- ``_MergeFailed`` (typed business failure) → the proposal is advanced to
  ``failed`` with the reason, NOT merged.
- No handler for the type (``reminder``) → recorded in ``errors``, the
  proposal stays ``adopted`` (never silently swallowed).
- ``ValueError`` (structural validation failure, e.g. missing payload or
  empty ``source_refs``) → the proposal itself is defective: advanced to
  ``failed`` with the reason, logged in detail.
- Other exceptions (environment/process level, e.g. disk or DB lock) →
  recorded in ``errors``, proposal stays ``adopted`` (retry on the next
  cycle), logged in detail.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from .. import ledger
from ..proposals import RETIRED_TYPES, advance, expire_overdue, scan
from ..snapshots import capture, finalize, gc, hash_bytes
from ..vault.db.relatedness import upsert_edge
from ..vault.db.schema import _db_lock
from ..vault.fs.vault_ops import atomic_write_bytes, safe_rel_path

logger = logging.getLogger("md-vault.bgagent")

_SECTION_RE = re.compile(r"(?m)^## ")


class _MergeFailed(Exception):
    """Typed merge failure: the proposal is advanced to ``failed``."""


def _vault_path(vault: Path, rel: str) -> Path:
    """Resolve a vault-relative path; refuse escapes.

    Raises _MergeFailed (not ValueError): a permanent bad path must park
    the proposal in ``failed`` with an actionable reject_reason, not stay
    ``adopted`` and re-error every executor cycle.
    """
    safe = safe_rel_path(str(rel or ""))
    if safe is None:
        raise _MergeFailed(f"illegal vault path: {str(rel)[:120]!r}")
    return vault / safe


def _slugify(stem: str) -> str:
    """Filesystem-safe slug from a stem (CJK-safe); fallback 'report'."""
    s = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", stem, flags=re.UNICODE).strip("-")
    return s[:40] or "report"


# ── entity_index: upsert a markdown section into index/entities.md ───────
# R2 tier: regenerable machine-zone write (LLM 否 — 上游产出). v2a: capture
# forced on (no switch); v2b: the config switch lands here (D12: 机器区快照
# 默认关 — but the switch does not exist in v2a; all handlers forced on).

def _parse_sections(text: str) -> list[tuple[str, str]]:
    """Split a markdown doc into (## heading, body) pairs (order preserved)."""
    sections = []
    for part in _SECTION_RE.split(text)[1:]:
        nl = part.find("\n")
        if nl == -1:
            sections.append((part.strip(), ""))
        else:
            sections.append((part[:nl].strip(), part[nl + 1:].strip("\n")))
    return sections


def _render_entities(sections: list[tuple[str, str]]) -> str:
    lines = ["# Entity Index", ""]
    for heading, body in sections:
        lines.append(f"## {heading}")
        lines.append(body.rstrip("\n"))
        lines.append("")
    return "\n".join(lines)


def _merge_entity_index(conn: sqlite3.Connection, vault: Path, p: dict) -> None:
    """Upsert payload {entity, attrs: {field: scalar}} as a ``## entity``
    section in ``index/entities.md`` (replace existing section, append if
    absent). D7: attrs are fields, not narrative — the v1 prose shape
    ``{entity, value}`` is rejected structurally (parked as failed)."""
    payload = p.get("payload") or {}
    key = str(payload.get("entity") or "").strip()
    if not key:
        raise ValueError("entity_index payload requires an 'entity' key")
    if "value" in payload and "attrs" not in payload:
        raise ValueError(
            "entity_index payload must use the structured shape {entity, attrs} — "
            "the v1 prose shape {entity, value} is retired (D7: attrs are fields, not narrative)"
        )
    attrs = payload.get("attrs")
    if not isinstance(attrs, dict) or not attrs:
        raise ValueError("entity_index payload requires a non-empty 'attrs' dict {field: value}")

    # D7 structural re-validation at merge time (spec §5.2 v2a 复算 = 结构校验):
    # attrs values must be scalars; nested structures / empty values refuse.
    for k, v in attrs.items():
        if isinstance(v, (dict, list)):
            raise ValueError(f"entity_index attrs['{k}'] must be a scalar value, got {type(v).__name__}")
        if isinstance(v, str) and not v.strip():
            raise ValueError(f"entity_index attrs['{k}'] must be a non-empty value")

    body_lines = [f"- {k}: {v}" for k, v in attrs.items()]
    for ref in (p.get("source_refs") or []):
        body_lines.append(f"- source: {ref}")
    section_body = "\n".join(body_lines)

    rel = "index/entities.md"
    fp = vault / rel
    existed = fp.is_file()
    sections = _parse_sections(fp.read_bytes().decode("utf-8")) if existed else []
    replaced = False
    out: list[tuple[str, str]] = []
    for heading, body in sections:
        if heading == key:
            out.append((heading, section_body))
            replaced = True
        else:
            out.append((heading, body))
    if not replaced:
        out.append((key, section_body))

    fp.parent.mkdir(parents=True, exist_ok=True)
    new_bytes = _render_entities(out).encode("utf-8")
    # 可逆层（P1）：执行器落地强制 pre-image（不变量 10）；v2b 才配开关（R2）。
    snap = capture(conn, vault, rel, "update" if existed else "create", "executor",
                   run_id=str(p.get("run_id") or ""))
    # The file row is ledgered by the watcher consumer (ledger.consume →
    # apply_change) with this registration: exactly one row, attributed to
    # the run.
    ledger.register(rel, "executor", batch_id=str(p.get("run_id") or ""))
    atomic_write_bytes(fp, new_bytes)
    if snap is not None:
        finalize(conn, snap, hash_bytes(new_bytes))


# ── relatedness: one DB edge row, zero file landing (D9 + D25) ────────────
# No file write → no file ledger row; the advance() state-ledger row
# (proposal_transitions) is this handler's state record. Nothing to
# snapshot (no file).

def _merge_relatedness(conn: sqlite3.Connection, vault: Path, p: dict) -> None:
    """Insert/refresh one (source, target) edge in the ``relatedness`` table.

    source = source_refs[0], target = payload['target'], basis = the record
    justification. Both files must exist (spec §5.2 v2a 结构校验: drift →
    surface, 不盲落). Idempotent per pair (upsert_edge); a pair the human
    dismissed (ignored rows) gets a fresh live row when re-proposed — the
    readback's ignored grouping then shows the repeat.
    """
    refs = p.get("source_refs") or []
    if not refs:
        raise ValueError("relatedness proposal has no source_refs")
    payload = p.get("payload") or {}
    target = str(payload.get("target") or "").strip()
    if not target:
        raise ValueError("relatedness payload requires a 'target'")

    source = str(refs[0])
    for rel in (source, target):
        fp = _vault_path(vault, rel)
        if not fp.is_file():
            raise ValueError(f"relatedness file not found: {rel}")

    with _db_lock:
        upsert_edge(
            conn, source, target,
            basis=str(p.get("justification") or ""),
            actor=str(p.get("actor") or ""),
            run_id=str(p.get("run_id") or ""),
        )


# ── summary: dual form, machine zone (D8, spec §4.2) ──────────────────────
# R2 tier: LLM 是（上游）→ LLM-unique output → forced pre-image (invariant
# 10) in v2a; v2b 开关同样不触及 LLM 独一产出侧。

def _merge_summary(conn: sqlite3.Connection, vault: Path, p: dict) -> None:
    """Land the summary in the machine zone (spec §四: 机器区).

    - ``form: "cache"`` — ``index/summaries/<stem>-summary.md``: one slot per
      source doc, slot-overwrite (旧全文 = 该调用 pre-image, regenerable).
    - ``form: "report"`` — ``index/summaries/<date>_summary_<slug>.md``:
      independent file; per-assertion refs (non-empty + files exist, D8
      执行器结构校验); advisory title frame (「分析认为…」); never merged
      into user notes (记忆化 = 人的复制动作, §10 拍板记录).

    The v1 placement (next to the source doc in the user area) is retired:
    landing there would violate the user-area zero-write constraint.
    """
    refs = p.get("source_refs") or []
    if not refs:
        raise ValueError("summary proposal has no source_refs")
    src_rel = str(refs[0])
    src_fp = _vault_path(vault, src_rel)
    if not src_fp.is_file():
        raise ValueError(f"summary source file not found: {src_rel}")

    payload = p.get("payload") or {}
    form = payload.get("form")
    body = str(payload.get("body") or "").strip()
    stem = Path(src_rel).stem

    if form == "cache":
        if not body:
            raise ValueError("summary cache form requires a non-empty 'body'")
        out_rel = f"index/summaries/{stem}-summary.md"
        text = f"# {stem} summary\n\n{body}\n"
    elif form == "report":
        if not body:
            raise ValueError("summary report form requires a non-empty 'body'")
        assertions = payload.get("assertions")
        if not isinstance(assertions, list) or not assertions:
            raise ValueError("summary report form requires a non-empty 'assertions' list [{text, refs}]")
        # D8 执行器结构校验：refs 非空 + 文件存在（语义性支持度校验 = v2b 候选）。
        for i, a in enumerate(assertions):
            if not isinstance(a, dict):
                raise ValueError(f"summary assertion #{i} must be a dict {{text, refs}}")
            refs_i = a.get("refs")
            if not isinstance(refs_i, list) or not refs_i:
                raise ValueError(
                    f"summary assertion #{i} has empty refs — a report assertion "
                    "without source refs is rejected (D8)"
                )
            for r in refs_i:
                fp = _vault_path(vault, str(r))
                if not fp.is_file():
                    raise ValueError(
                        f"summary assertion #{i} refs a missing file: {r}"
                    )
        date = datetime.fromtimestamp(float(p.get("created_at") or 0)).strftime("%Y-%m-%d")
        out_rel = f"index/summaries/{date}_summary_{_slugify(stem)}.md"
        # Advisory 框架：标题固定，机器生成的新因果判断只以「假设」面目出现。
        text = f"# 分析认为\n\n{body}\n"
    else:
        raise ValueError(
            "summary payload requires 'form': 'cache' or 'report' "
            "(the v1 shape {body} without form is retired — D8 双形态)"
        )

    out_fp = vault / out_rel
    existed = out_fp.is_file()
    out_fp.parent.mkdir(parents=True, exist_ok=True)
    new_bytes = text.encode("utf-8")
    # 可逆层（P1）：summary = LLM 独一产出，槽覆盖强制 pre-image（不变量 10：
    # 旧全文即此 pre-image；报告型点态不可再生，pre-image 是唯一存档）。
    snap = capture(conn, vault, out_rel, "update" if existed else "create", "executor",
                   run_id=str(p.get("run_id") or ""))
    ledger.register(out_rel, "executor", batch_id=str(p.get("run_id") or ""))
    atomic_write_bytes(out_fp, new_bytes)
    if snap is not None:
        finalize(conn, snap, hash_bytes(new_bytes))


# ── errata: additive contradiction card, machine zone (D6, spec §4.3) ─────
# 单闸门：人 adopt = 确认，卡才落地（物化 = 确权）。落地后系统对该卡只读：
# merged 记录永不重跑 handler，所以「不重合并」即「只读」。原文永不改、
# 不加指针（D6：机器指出伤口，缝合线由人画）。

def _merge_errata(conn: sqlite3.Connection, vault: Path, p: dict) -> None:
    """Render the §4.3 card into ``index/errata/<stem>.md`` (stem = the
    first source ref). The candidate is annotated ``candidate, by: <actor>``
    (invariant 7: 候选是待人署名的草稿) — never a silent assertion."""
    payload = p.get("payload") or {}
    contradiction = str(payload.get("contradiction") or "").strip()
    candidate = str(payload.get("candidate") or "").strip()
    rationale = str(payload.get("rationale") or "").strip()
    if not contradiction or not candidate or not rationale:
        raise ValueError("errata payload requires non-empty {contradiction, candidate, rationale}")
    excerpts = payload.get("excerpts")
    if not isinstance(excerpts, list) or len(excerpts) < 2:
        raise ValueError(
            "errata payload requires at least 2 excerpts "
            "[(path, quote, label?)] — a single excerpt is a claim, not a contradiction"
        )
    for i, e in enumerate(excerpts):
        if not isinstance(e, dict):
            raise ValueError(f"errata excerpt #{i} must be a dict {{path, quote, label?}}")
        if not str(e.get("path") or "").strip() or not str(e.get("quote") or "").strip():
            raise ValueError(f"errata excerpt #{i} requires non-empty 'path' and 'quote'")

    refs = p.get("source_refs") or []
    if not refs:
        raise ValueError("errata proposal has no source_refs")
    stem = Path(str(refs[0])).stem
    actor = str(p.get("actor") or "")

    lines = ["## 矛盾", contradiction, ""]
    letters = "ABCDEFGH"
    for i, e in enumerate(excerpts):
        path = str(e["path"]).strip()
        label = str(e.get("label") or "").strip()
        quote = str(e["quote"]).strip()
        where = f"{path} {label}" if label else path
        lines.append(f"摘录 {letters[i]}（{where}）：{quote}")
    lines += ["", f"## 候选读法（candidate, by: {actor}）", candidate, "",
              "## 理由", rationale, ""]

    rel = f"index/errata/{stem}.md"
    fp = vault / rel
    # D23/#C1 (debt #2, invariant 7 — 生成类动词合规): a materialized errata
    # card is confirmed content (adopted = 确权) — system read-only after
    # materialization. Re-adopting into the occupied slot would silently
    # destroy confirmed content; reject instead. The raise goes BEFORE
    # capture(): no pre-image slot consumed, no file write, no snapshots
    # row (the occupied card's pre-image stays the recovery credential).
    # run_executor's ValueError branch then parks the record in failed with
    # reject_reason = this message; it stays ≤200 chars so the human exit
    # (delete the old card → re-materialize) survives str(e)[:200].
    if fp.is_file():
        raise ValueError(
            f"errata slot occupied: {rel} is a materialized card "
            "(read-only after adoption). Exit: delete the old card, "
            "then re-adopt to re-materialize. v2b unique names "
            "auto-resolve (D23)."
        )
    fp.parent.mkdir(parents=True, exist_ok=True)
    new_bytes = "\n".join(lines).encode("utf-8")
    # 可逆层（P1）：勘误卡 = LLM 独一产出（矛盾措辞/候选读法），强制
    # pre-image（不变量 10）。槽位空由上面的 pre-check 保证 → op "create"。
    snap = capture(conn, vault, rel, "create", "executor",
                   run_id=str(p.get("run_id") or ""))
    ledger.register(rel, "executor", batch_id=str(p.get("run_id") or ""))
    atomic_write_bytes(fp, new_bytes)
    if snap is not None:
        finalize(conn, snap, hash_bytes(new_bytes))


_HANDLERS = {
    "entity_index": _merge_entity_index,
    "relatedness": _merge_relatedness,
    "summary": _merge_summary,
    "errata": _merge_errata,
}


# ── Entry point ──────────────────────────────────────────────────────────

def run_executor(vault_dir, conn: sqlite3.Connection) -> dict:
    """One executor cycle. Returns {"merged": n, "expired": n, "errors": [...]}.

    See the module docstring for the failure taxonomy. ``reminder`` has no
    handler by design (no landing) and is reported in ``errors`` without
    merging. Retired v1 types (link / correction / archive) have no v2
    handler: their adopted records are parked in ``failed`` with an
    actionable reason (human processes 存量, no migration — spec §四).
    """
    vault = Path(vault_dir)
    merged = 0
    errors: list[str] = []

    for p in scan(conn, status="adopted"):
        rec_id = p["id"]
        rel = p["relpath"]

        # Retired v1 verbs: no v2 handler. Park in failed (actionable reason)
        # instead of re-erroring every cycle — the human's queue, not ours.
        if p["type"] in RETIRED_TYPES:
            logger.warning(
                "[executor] retired type parked as failed %s: type '%s' has no v2 handler",
                rel, p["type"],
            )
            advance(
                conn, vault, rec_id, "failed", actor="executor",
                reject_reason=(
                    f"type '{p['type']}' is retired in v2 (no executor handler) — "
                    "process manually: link → relatedness ledger; correction → "
                    "errata + human rebase; archive → human move"
                ),
            )
            continue

        handler = _HANDLERS.get(p["type"])
        try:
            if handler is None:
                # No handler (reminder): never silently swallowed.
                errors.append(f"proposal #{rec_id} ({rel}): no handler for type '{p['type']}'")
                continue
            handler(conn, vault, p)
            advance(conn, vault, rec_id, "merged", actor="executor")
            merged += 1
        except _MergeFailed as e:            # 已有：内容状态问题（old 缺失/多义等）
            logger.warning("[executor] merge failed %s: %s: %s", rel, type(e).__name__, e)
            advance(conn, vault, rec_id, "failed", actor="executor", reject_reason=str(e)[:200])
        except Exception as e:
            if isinstance(e, ValueError):
                # 结构性校验错误 = 提案本身畸形（payload 缺失 / source_refs 空），非进程级崩溃
                logger.warning("[executor] merge failed (structural) %s: %s: %s", rel, type(e).__name__, e)
                advance(conn, vault, rec_id, "failed", actor="executor", reject_reason=str(e)[:200])
            else:
                # 环境/进程级（磁盘、DB 锁）：提案无辜，留 adopted 重试
                logger.warning("[executor] merge error %s: %s: %s", rel, type(e).__name__, e)
                errors.append(f"{rel}: {type(e).__name__}: {e}")

    expired = expire_overdue(conn, vault)

    # Retention sweep (P1, spec D18): reclaim superseded pre-images (and
    # delete-class rows past the 30-day window); never the live slot of a
    # file (spec invariant 2). Best-effort: a GC failure must not break the
    # merge cycle — gc() itself guards, this keeps the belt on the braces.
    try:
        gc(conn, vault)
    except Exception:
        logger.warning("[executor] snapshot gc failed", exc_info=True)

    return {"merged": merged, "expired": expired, "errors": errors}
