"""Proposal-channel hard-constraint audit (todo 65e328d7, validation-only).

The todo is the 10-22 problem statement from the md-file-proposal era:
"external MCP writes bypass frontmatter validation and the state machine;
the internal agent is held by prompt constraints only". After the v2.1
rebuild (DB record + write-entry hard validation) the user's ruling is that
it should be covered; this file is the *evidence* for that ruling.

Every test asserts the CURRENT behavior and assumes it is correct:
each malformed input must be refused with an error string and zero disk
writes (no file, no proposals row, no suggestions/ dir). If any assertion
here FAILS, the audit found a real bypass — do not patch product code,
report the gap (plan: docs/superpowers/plans/2026-11-02-proposals-
validation-audit.md, Task 2).

Coverage map (audit objects 1-5 of the plan):

1. write-entry validation (server/agent/tools/propose.py):
   envelope (VALID_TYPES) + retired redirect + privilege (ctx.verbs) +
   per-verb _validate_payload. This file adds the malformed-payload grid
   (2-4 per verb) that the existing tests left open (test_proposals.py
   covers: entity empty-attrs, relatedness empty-refs, summary no-form,
   summary assertion-refs, unknown type, privilege, retired redirect,
   v1 prose shape, MCP-surface errata-missing-candidate).
2. trusted stamps: ProposeArgs exposes exactly the four LLM-supplied
   fields — actor/run_id are NOT model inputs (runner-injected via
   ToolContext; stamp behavior pinned in
   test_proposals.py::test_propose_success_auto_and_review and
   test_mcp_gate.py::test_mcp_propose_creates_stamped_record).
3. merge-time structural re-validation (server/bgagent/executor.py
   _merge_*): covered by tests/test_executor.py + test_executor_errata_
   reject.py (entity nested/v1-shape, relatedness missing source file /
   missing target, summary ref-file missing, errata single-excerpt +
   occupied slot) — cited in the plan matrix, not re-tested here.
4. channel surface: SuggestionsGate (vault invariant) + McpExternalGate
   (MCP channel rule) — existing rows in test_proposals.py and
   test_mcp_gate.py; this file adds the composite validate_tool_call
   check so the matrix row is self-contained.
5. state machine (server/proposals.py advance): covered by
   test_proposals.py (illegal transitions), test_proposals_revive.py
   (invariant 12: revive human-only, no agent entry),
   test_proposals_advance_race.py (invariant 11), test_proposals_dead_
   edge.py (terminal states) — cited in the plan matrix.
"""
from __future__ import annotations

from pathlib import Path

from server.agent.policy import ChannelPolicy, validate_tool_call
from server.agent.tools.propose import ProposeArgs, ProposeTool
from server.agent.tools.tool import ToolContext
from server.mcp_server import McpExternalGate


def _ctx(vault_dir: Path, verbs=None):
    return ToolContext(vault_dir=vault_dir, write_source="consolidator",
                       run_id="audit-20261102T000000", verbs=verbs)


def _count_files(vault_dir: Path) -> int:
    return sum(1 for p in vault_dir.rglob("*") if p.is_file())


def _zero_disk(vault_dir: Path, ledger_conn, before_files: int, before_rows: int):
    """The refusal left nothing behind: no file, no row, no buffer dir."""
    assert _count_files(vault_dir) == before_files, "a file appeared on a refused proposal"
    assert ledger_conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == before_rows, \
        "a proposals row was booked for a refused proposal"
    assert not (vault_dir / "suggestions").is_dir(), "suggestions/ was created on a refused proposal"


def _call(tool, vault_dir: Path, type_: str, payload, source_refs=None, verbs=None):
    out = tool.call(
        {"type": type_, "payload": payload,
         "source_refs": source_refs if source_refs is not None else ["notes/test.md"],
         "justification": "audit"},
        _ctx(vault_dir, verbs=verbs))
    assert out.startswith("Error:"), f"expected a refusal, got: {out!r}"
    return out


def _snapshot(vault_dir: Path, ledger_conn):
    return _count_files(vault_dir), ledger_conn.execute(
        "SELECT COUNT(*) FROM proposals").fetchone()[0]


# ── audit object 1: write-entry, per-verb malformed grid ─────────────────
#
# Each verb gets 2-4 abnormal payloads; the plan names null payload,
# empty source_refs, errata single excerpt, summary empty assertions,
# unknown type, and bgagent verb overreach explicitly.

def test_entity_index_null_payload_rejected_zero_disk(vault_dir, ledger_conn):
    tool, before = ProposeTool(), _snapshot(vault_dir, ledger_conn)
    out = _call(tool, vault_dir, "entity_index", None)
    assert "requires a payload dict" in out
    _zero_disk(vault_dir, ledger_conn, *before)


def test_entity_index_empty_entity_rejected_zero_disk(vault_dir, ledger_conn):
    tool, before = ProposeTool(), _snapshot(vault_dir, ledger_conn)
    out = _call(tool, vault_dir, "entity_index",
                {"entity": "   ", "attrs": {"f": "v"}})
    assert "non-empty 'entity'" in out
    _zero_disk(vault_dir, ledger_conn, *before)


def test_entity_index_nested_attrs_rejected_zero_disk(vault_dir, ledger_conn):
    """D7 at the write entry: attrs values must be scalars — a nested
    structure (a prose-shaped list) is refused before anything lands."""
    tool, before = ProposeTool(), _snapshot(vault_dir, ledger_conn)
    out = _call(tool, vault_dir, "entity_index",
                {"entity": "e", "attrs": {"f": ["a", "b"]}})
    assert "must be a scalar" in out
    _zero_disk(vault_dir, ledger_conn, *before)


def test_entity_index_empty_source_refs_rejected_zero_disk(vault_dir, ledger_conn):
    tool, before = ProposeTool(), _snapshot(vault_dir, ledger_conn)
    out = _call(tool, vault_dir, "entity_index",
                {"entity": "e", "attrs": {"f": "v"}}, source_refs=[])
    assert "at least one source_ref" in out
    _zero_disk(vault_dir, ledger_conn, *before)


def test_relatedness_null_payload_rejected_zero_disk(vault_dir, ledger_conn):
    tool, before = ProposeTool(), _snapshot(vault_dir, ledger_conn)
    out = _call(tool, vault_dir, "relatedness", None)
    assert "non-empty 'target'" in out
    _zero_disk(vault_dir, ledger_conn, *before)


def test_relatedness_blank_target_rejected_zero_disk(vault_dir, ledger_conn):
    tool, before = ProposeTool(), _snapshot(vault_dir, ledger_conn)
    out = _call(tool, vault_dir, "relatedness", {"target": "  "})
    assert "non-empty 'target'" in out
    _zero_disk(vault_dir, ledger_conn, *before)


def test_relatedness_empty_source_refs_rejected_zero_disk(vault_dir, ledger_conn):
    tool, before = ProposeTool(), _snapshot(vault_dir, ledger_conn)
    out = _call(tool, vault_dir, "relatedness",
                {"target": "notes/other.md"}, source_refs=[])
    assert "at least one source_ref" in out
    _zero_disk(vault_dir, ledger_conn, *before)


def test_summary_null_payload_rejected_zero_disk(vault_dir, ledger_conn):
    tool, before = ProposeTool(), _snapshot(vault_dir, ledger_conn)
    out = _call(tool, vault_dir, "summary", None)
    assert "requires a payload dict" in out
    _zero_disk(vault_dir, ledger_conn, *before)


def test_summary_empty_assertions_list_rejected_zero_disk(vault_dir, ledger_conn):
    """D8 at the write entry: a report form with an *empty* assertions
    list (as opposed to a missing key — that one is pinned in
    test_proposals.py::test_propose_summary_report_refs_rejected) is
    equally a report with no supportable claims."""
    tool, before = ProposeTool(), _snapshot(vault_dir, ledger_conn)
    out = _call(tool, vault_dir, "summary",
                {"form": "report", "body": "b", "assertions": []})
    assert "non-empty 'assertions' list" in out
    _zero_disk(vault_dir, ledger_conn, *before)


def test_summary_empty_source_refs_rejected_zero_disk(vault_dir, ledger_conn):
    tool, before = ProposeTool(), _snapshot(vault_dir, ledger_conn)
    out = _call(tool, vault_dir, "summary",
                {"form": "cache", "body": "b"}, source_refs=[])
    assert "at least one source_ref" in out
    _zero_disk(vault_dir, ledger_conn, *before)


def test_errata_single_excerpt_rejected_zero_disk(vault_dir, ledger_conn):
    """The plan's named case: one excerpt is a claim, not a
    contradiction — refused at the write entry."""
    tool, before = ProposeTool(), _snapshot(vault_dir, ledger_conn)
    out = _call(tool, vault_dir, "errata",
                {"contradiction": "c",
                 "excerpts": [{"path": "notes/a.md", "quote": "q"}],
                 "candidate": "k", "rationale": "r"})
    assert "at least 2" in out
    _zero_disk(vault_dir, ledger_conn, *before)


def test_errata_null_payload_rejected_zero_disk(vault_dir, ledger_conn):
    tool, before = ProposeTool(), _snapshot(vault_dir, ledger_conn)
    out = _call(tool, vault_dir, "errata", None)
    assert "requires a payload dict" in out
    _zero_disk(vault_dir, ledger_conn, *before)


def test_errata_excerpt_blank_path_rejected_zero_disk(vault_dir, ledger_conn):
    """Structural half of the known boundary: the excerpt *fields* must
    be non-empty at the write entry (path *existence* is the recorded
    boundary — see the plan's 已知边界)."""
    tool, before = ProposeTool(), _snapshot(vault_dir, ledger_conn)
    out = _call(tool, vault_dir, "errata",
                {"contradiction": "c",
                 "excerpts": [{"path": " ", "quote": "q"},
                              {"path": "notes/b.md", "quote": "q2"}],
                 "candidate": "k", "rationale": "r"})
    assert "non-empty 'path'" in out
    _zero_disk(vault_dir, ledger_conn, *before)


def test_reminder_null_payload_rejected_zero_disk(vault_dir, ledger_conn):
    tool, before = ProposeTool(), _snapshot(vault_dir, ledger_conn)
    out = _call(tool, vault_dir, "reminder", None)
    assert "non-empty 'body'" in out
    _zero_disk(vault_dir, ledger_conn, *before)


def test_reminder_blank_body_rejected_zero_disk(vault_dir, ledger_conn):
    tool, before = ProposeTool(), _snapshot(vault_dir, ledger_conn)
    out = _call(tool, vault_dir, "reminder", {"body": ""})
    assert "non-empty 'body'" in out
    _zero_disk(vault_dir, ledger_conn, *before)


def test_unknown_type_rejected_zero_disk(vault_dir, ledger_conn):
    """Envelope half (existing row: test_proposals.py::test_propose_
    unknown_type_rejected); kept here so the five-verb grid reads
    complete: an unknown verb refuses before any per-verb validation."""
    tool, before = ProposeTool(), _snapshot(vault_dir, ledger_conn)
    out = _call(tool, vault_dir, "bogus_verb", None, source_refs=[])
    assert "unknown proposal type" in out
    _zero_disk(vault_dir, ledger_conn, *before)


def test_bgagent_privilege_escalation_rejected_zero_disk(vault_dir, ledger_conn):
    """The plan's named case: a bgagent-style role carrying an auto-tier
    verbs whitelist tries a review-tier verb (errata). Privilege is
    checked BEFORE payload validation, and the error names the allowed
    verbs so the model can self-correct. Zero writes, no suggestions/."""
    tool, before = ProposeTool(), _snapshot(vault_dir, ledger_conn)
    out = _call(tool, vault_dir, "errata",
                {"contradiction": "c",
                 "excerpts": [{"path": "notes/a.md", "quote": "q"},
                              {"path": "notes/b.md", "quote": "q2"}],
                 "candidate": "k", "rationale": "r"},
                verbs=("entity_index", "relatedness"))
    assert "your role cannot propose" in out
    assert "entity_index" in out and "relatedness" in out
    _zero_disk(vault_dir, ledger_conn, *before)


# ── audit object 2: trusted stamps (structural half) ─────────────────────

def test_envelope_args_surface_has_no_actor_or_run_id():
    """Actor and run_id cannot be forged by the model: the propose
    tool's argument schema exposes exactly the four content fields.
    (The stamping half — ctx values winning on the landed record — is
    pinned in test_proposals.py::test_propose_success_auto_and_review
    and test_mcp_gate.py::test_mcp_propose_creates_stamped_record.)"""
    assert set(ProposeArgs.model_fields) == {
        "type", "payload", "source_refs", "justification"}


# ── audit object 4: channel surface (composite check) ────────────────────

def test_channel_surface_mcp_writes_rejected(vault_dir):
    """MCP channel, composite: the main-area rule (McpExternalGate) and
    the vault invariant (SuggestionsGate) are both applied by
    validate_tool_call — every external write is refused with a pointer
    at the propose tool, and reads pass. (Existing rows: test_mcp_gate.py
    and test_proposals.py::test_write_doc_into_suggestions_rejected;
    this makes the audit matrix row self-contained.)"""
    policy = ChannelPolicy(vault_dir, gates=(McpExternalGate(),))

    err_main = validate_tool_call(
        "write_doc", {"path": "notes/x.md", "content": "x"}, policy)
    assert err_main is not None and "NOT performed" in err_main
    assert "propose tool" in err_main

    err_buffer = validate_tool_call(
        "write_doc", {"path": "suggestions/2026-11-02_audit_x.md",
                      "content": "x"}, policy)
    assert err_buffer is not None and "propose" in err_buffer

    assert not (vault_dir / "suggestions").is_dir()

    # The same policy lets the read through — the gate is write-scoped.
    assert validate_tool_call("read_doc", {"path": "notes/x.md"}, policy) is None
