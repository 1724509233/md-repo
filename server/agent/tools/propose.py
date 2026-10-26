"""propose tool: the agent's only write path into suggestions/ (v2).

The LLM supplies exactly four parameters (type, payload, source_refs,
justification). ``actor`` and ``run_id`` are *trusted stamps* injected via
ToolContext by the runner (spec §三.3.1): the model can neither claim an
identity nor forge a batch id.

Validation (every failure returns an error to the agent — ZERO disk writes):

1. retired — the three v1 verbs (link / correction / archive) are rejected
   with an *operational* reason naming the replacement (spec §四 "退役三件";
   D5) — this runs BEFORE the envelope check so the model gets the
   redirect, not a bare "unknown type"
2. envelope — ``type`` must be in VALID_TYPES (the v2 verb table)
3. privilege — when the context carries a role verb whitelist (``ctx.verbs``,
   from the bgagent config ``verbs:`` field), ``type`` must be in it; the
   error lists the allowed verbs so the model can self-correct
4. per-verb payload schema (spec §四 verb catalog — structural validation
   at the write entry point, §五.1)

Success: insert the record (pending) → process (processing →
adopted/awaiting_review per the authorization tier). The projection file
under suggestions/ is rendered on each step (system-exclusive, idempotent).
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from .tool import Tool, ToolContext


class ProposeArgs(BaseModel):
    """Create a proposal (state + projection). Use instead of writing into suggestions/."""
    type: str = Field(
        description="Proposal type: entity_index | relatedness | summary | errata | reminder"
    )
    payload: dict | None = Field(
        default=None,
        description=(
            "Type-specific content. entity_index: {entity, attrs: {field: value}}; "
            "relatedness: {target}; summary: {form: 'cache'|'report', body, "
            "assertions?}; errata: {contradiction, excerpts, candidate, rationale}; "
            "reminder: {body}"
        ),
    )
    source_refs: list[str] = Field(
        default_factory=list,
        description="Vault-relative paths of the notes this proposal is based on",
    )
    justification: str = Field(
        default="", description="Why this proposal is warranted (projection body; human-readable)"
    )


# Operational redirects for the three retired verbs (D5, spec §四). Checked
# before the envelope so the model learns the replacement, not just the ban.
_RETIRED_REDIRECTS = {
    "link": (
        "The 'link' verb is retired (v2). Machine relations go to the relatedness "
        "ledger (DB), never into user files — the in-note 'related' block is "
        "obsolete. Use propose(type='relatedness', payload={'target': <note path>}) "
        "instead; the agent sees the suggestion layer, the user decides."
    ),
    "correction": (
        "The 'correction' verb is retired (v2). Exact user-text replacement is the "
        "human-only 'rebase' operation — automation never rewrites the user area. "
        "To surface a contradiction, use propose(type='errata', ...): an additive "
        "card ('here is a contradiction; candidate reading: …') that a human "
        "confirms; the original text is never touched."
    ),
    "archive": (
        "The 'archive' verb is retired (v2). Automation never moves user files — "
        "archiving is a human act (drag in Obsidian). The sentinel role reports "
        "read-only ('N docs untouched for 90d, suggest archiving'); it proposes "
        "nothing."
    ),
}


def _scalar_error(k: str, v) -> str | None:
    """D7: entity attrs values must be scalars (field: value), never prose."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return None
    if isinstance(v, str):
        if not v.strip():
            return f"Error: entity_index payload attrs['{k}'] must be a non-empty scalar (prose values are forbidden — attrs are fields, not narrative)"
        return None
    return (
        f"Error: entity_index payload attrs['{k}'] must be a scalar "
        f"(str/int/float/bool), got {type(v).__name__} — nested structures and "
        "prose are forbidden (D7: attrs are fields, not narrative)"
    )


def _validate_payload(type_: str, payload: dict | None, source_refs: list[str]) -> str | None:
    """Per-verb payload schema (spec §四 catalog). Error string or None."""
    if type_ == "entity_index":
        if not isinstance(payload, dict):
            return "Error: entity_index requires a payload dict {entity, attrs}"
        if not str(payload.get("entity") or "").strip():
            return "Error: entity_index payload requires a non-empty 'entity' field"
        if "value" in payload and "attrs" not in payload:
            return (
                "Error: entity_index payload must use the structured shape "
                "{entity, attrs: {field: value}} — the v1 prose shape "
                "{entity, value} is rejected (D7: values are fields, not narrative)"
            )
        attrs = payload.get("attrs")
        if not isinstance(attrs, dict) or not attrs:
            return (
                "Error: entity_index payload requires a non-empty 'attrs' dict "
                "{field: scalar value}"
            )
        for k, v in attrs.items():
            err = _scalar_error(str(k), v)
            if err is not None:
                return err
        if not source_refs:
            return "Error: entity_index requires at least one source_ref (the user note the entity is drawn from)"
        return None

    if type_ == "relatedness":
        if not isinstance(payload, dict) or not str(payload.get("target") or "").strip():
            return "Error: relatedness requires a payload with a non-empty 'target' (the note this one relates to)"
        if not source_refs:
            return "Error: relatedness requires at least one source_ref (the note the relation is observed from)"
        return None

    if type_ == "summary":
        if not isinstance(payload, dict):
            return "Error: summary requires a payload dict with 'form' and 'body'"
        form = payload.get("form")
        if form == "cache":
            if not str(payload.get("body") or "").strip():
                return "Error: summary cache form requires a non-empty 'body' (the summary text)"
        elif form == "report":
            if not str(payload.get("body") or "").strip():
                return "Error: summary report form requires a non-empty 'body'"
            assertions = payload.get("assertions")
            if not isinstance(assertions, list) or not assertions:
                return (
                    "Error: summary report form requires a non-empty 'assertions' list "
                    "[{text, refs}] — every assertion carries its own source refs (D8)"
                )
            for i, a in enumerate(assertions):
                if not isinstance(a, dict):
                    return f"Error: summary assertion #{i} must be a dict {{text, refs}}"
                if not str(a.get("text") or "").strip():
                    return f"Error: summary assertion #{i} requires a non-empty 'text'"
                refs = a.get("refs")
                if not isinstance(refs, list) or not refs or not all(isinstance(r, str) and r.strip() for r in refs):
                    return (
                        f"Error: summary assertion #{i} requires non-empty 'refs' "
                        "(vault-relative paths supporting this assertion — a report "
                        "assertion without refs is rejected, D8)"
                    )
        else:
            return "Error: summary payload requires 'form': 'cache' or 'report'"
        if not source_refs:
            return "Error: summary requires at least one source_ref (the note being summarized)"
        return None

    if type_ == "errata":
        if not isinstance(payload, dict):
            return "Error: errata requires a payload dict {contradiction, excerpts, candidate, rationale}"
        for k in ("contradiction", "candidate", "rationale"):
            if not str(payload.get(k) or "").strip():
                return f"Error: errata payload requires a non-empty '{k}' field"
        excerpts = payload.get("excerpts")
        if not isinstance(excerpts, list) or len(excerpts) < 2:
            return (
                "Error: errata payload requires 'excerpts': a list of at least 2 "
                "entries [{path, quote, label?}] — one per contradicting passage "
                "(a single excerpt is a claim, not a contradiction)"
            )
        for i, e in enumerate(excerpts):
            if not isinstance(e, dict):
                return f"Error: errata excerpt #{i} must be a dict {{path, quote, label?}}"
            if not str(e.get("path") or "").strip():
                return f"Error: errata excerpt #{i} requires a non-empty 'path'"
            if not str(e.get("quote") or "").strip():
                return f"Error: errata excerpt #{i} requires a non-empty 'quote' (the verbatim passage)"
        return None

    if type_ == "reminder":
        if not isinstance(payload, dict) or not str(payload.get("body") or "").strip():
            return "Error: reminder requires a payload with a non-empty 'body' (the reminder text)"
        return None

    return None  # unreachable: the envelope check runs first


class ProposeTool(Tool):
    name = "propose"
    args_model = ProposeArgs
    permission = "allow"

    def _execute(self, args, ctx: ToolContext) -> str:
        # 1. retired verbs → operational redirect (D5)
        if args.type in _RETIRED_REDIRECTS:
            return f"Error: {_RETIRED_REDIRECTS[args.type]}"

        # 2. envelope
        from ...proposals import VALID_TYPES
        if args.type not in VALID_TYPES:
            return (f"Error: unknown proposal type '{args.type}'. "
                    f"Valid types: {', '.join(sorted(VALID_TYPES))}")

        # 3. privilege (role verb whitelist; None = unrestricted channel)
        if ctx.verbs is not None and args.type not in ctx.verbs:
            return (f"Error: your role cannot propose '{args.type}'. "
                    f"Allowed proposal types for this role: {', '.join(ctx.verbs)}. "
                    f"Propose only within your role's verbs.")

        # 4. per-verb payload schema
        err = _validate_payload(args.type, args.payload, list(args.source_refs))
        if err is not None:
            return err

        # Success path: DB unavailable → error, nothing on disk.
        from ...proposals import create, process_proposal
        from ...vault.db.conn import get_conn

        conn = get_conn()
        if conn is None:
            return "Error: proposal database unavailable (server DB not initialized)"

        actor = ctx.write_source or "agent"
        try:
            rec = create(
                conn, ctx.vault_dir, args.type, actor, ctx.run_id,
                list(args.source_refs), args.justification, args.payload,
            )
            final = process_proposal(conn, ctx.vault_dir, rec["id"], actor=actor)
        except ValueError as e:
            return f"Error: {e}"

        return (f"proposal #{rec['id']} created → {rec['relpath']} "
                f"(status: {final['status']})")
