"""propose tool: the agent's only write path into suggestions/ (v2).

The LLM supplies exactly four parameters (type, payload, source_refs,
justification). ``actor`` and ``run_id`` are *trusted stamps* injected via
ToolContext by the runner (spec §三.3.1): the model can neither claim an
identity nor forge a batch id.

Validation (every failure returns an error to the agent — ZERO disk writes):

1. envelope — ``type`` must be in VALID_TYPES
2. privilege — when the context carries a role verb whitelist (``ctx.verbs``,
   from the bgagent config ``verbs:`` field), ``type`` must be in it; the
   error lists the allowed verbs so the model can self-correct
3. per-verb payload schema (spec §4.1 catalog)

Success: insert the record (pending) → process (processing →
adopted/awaiting_review per the authorization tier). The projection file
under suggestions/ is rendered on each step (system-exclusive, idempotent).
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from .tool import Tool, ToolContext


class ProposeArgs(BaseModel):
    """Create a proposal (state + projection). Use instead of writing into suggestions/."""
    type: str = Field(description="Proposal type: entity_index | link | summary | correction | reminder | archive")
    payload: dict | None = Field(default=None, description="Type-specific content (e.g. {entity, value} for entity_index, {body} for summary/reminder)")
    source_refs: list[str] = Field(default_factory=list, description="Vault-relative paths of the notes this proposal is based on")
    justification: str = Field(default="", description="Why this proposal is warranted (projection body; human-readable)")


def _validate_payload(type_: str, payload: dict | None, source_refs: list[str]) -> str | None:
    """Per-verb payload schema (spec §4.1 catalog). Error string or None."""
    if type_ == "entity_index":
        if not isinstance(payload, dict):
            return "Error: entity_index requires a payload with 'entity' and 'value'"
        for k in ("entity", "value"):
            if not str(payload.get(k) or "").strip():
                return f"Error: entity_index payload requires a non-empty '{k}' field"
        return None
    if type_ == "link":
        if not isinstance(payload, dict) or not str(payload.get("target") or "").strip():
            return "Error: link requires a payload with a non-empty 'target' (the note to link to)"
        if not source_refs:
            return "Error: link requires at least one source_ref (the note that should gain the link)"
        return None
    if type_ == "summary":
        if not isinstance(payload, dict) or not str(payload.get("body") or "").strip():
            return "Error: summary requires a payload with a non-empty 'body' (the summary text)"
        if not source_refs:
            return "Error: summary requires at least one source_ref (the note being summarized)"
        return None
    if type_ == "correction":
        if not isinstance(payload, dict):
            return "Error: correction requires a payload with 'file', 'old' and 'new'"
        for k in ("file", "old", "new"):
            if not str(payload.get(k) or "").strip():
                return f"Error: correction payload requires a non-empty '{k}' field"
        return None
    if type_ == "archive":
        if not isinstance(payload, dict) or not str(payload.get("path") or "").strip():
            return "Error: archive requires a payload with a non-empty 'path' (the note to archive)"
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
        # 1. envelope
        from ...proposals import VALID_TYPES
        if args.type not in VALID_TYPES:
            return (f"Error: unknown proposal type '{args.type}'. "
                    f"Valid types: {', '.join(sorted(VALID_TYPES))}")

        # 2. privilege (role verb whitelist; None = unrestricted channel)
        if ctx.verbs is not None and args.type not in ctx.verbs:
            return (f"Error: your role cannot propose '{args.type}'. "
                    f"Allowed proposal types for this role: {', '.join(ctx.verbs)}. "
                    f"Propose only within your role's verbs.")

        # 3. per-verb payload schema
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
