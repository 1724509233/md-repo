"""Write-path validation for direct suggestions/ writes.

Raw file writes (write_doc/edit_doc via the agent tools and MCP) must never
forge proposal state: validate_suggestions_write enforces the filename /
frontmatter / status invariants, and status changes are rejected so that
transitions still go through the state machine (proposals.advance).
"""
from __future__ import annotations

import pytest

from server import proposals
from server.agent import ChannelPolicy, ToolContext, execute_tool, validate_tool_call

PATH = "suggestions/2026-10-22_summary_x.md"


def _fm(status="pending", type_="summary", extra="", body="body text"):
    return f"---\ntype: {type_}\nstatus: {status}{extra}\n---\n# T\n\n{body}\n"


# ── new file (old_text=None) ──────────────────────────────────────────────

def test_new_file_pending_allowed():
    assert proposals.validate_suggestions_write(PATH, _fm("pending")) is None


def test_new_file_awaiting_review_allowed():
    assert proposals.validate_suggestions_write(PATH, _fm("awaiting_review")) is None


@pytest.mark.parametrize("status", ["processing", "merged", ""])
def test_new_file_illegal_status_rejected(status):
    err = proposals.validate_suggestions_write(PATH, _fm(status))
    assert err is not None
    assert err.startswith("Error: new proposals start with status: pending")
    assert f"got '{status}'" in err


def test_new_file_missing_status_key_rejected():
    err = proposals.validate_suggestions_write(
        PATH, "---\ntype: summary\n---\nbody\n")
    assert err is not None
    assert "new proposals start with status: pending" in err


# ── frontmatter block required ────────────────────────────────────────────

def test_new_file_no_frontmatter_rejected():
    err = proposals.validate_suggestions_write(PATH, "# T\n\nbody\n")
    assert err is not None
    assert "YAML frontmatter block" in err


def test_new_file_frontmatter_not_dict_rejected():
    err = proposals.validate_suggestions_write(PATH, "---\njust a string\n---\nbody\n")
    assert err is not None
    assert "YAML frontmatter block" in err


def test_new_file_empty_frontmatter_block_rejected():
    err = proposals.validate_suggestions_write(PATH, "---\n\n---\nbody\n")
    assert err is not None
    assert "YAML frontmatter block" in err


# ── type: whitelist + filename consistency ────────────────────────────────

def test_type_not_in_whitelist_rejected():
    err = proposals.validate_suggestions_write(
        "suggestions/2026-10-22_bogus_x.md", _fm("pending", type_="bogus"))
    assert err is not None
    assert "frontmatter type must be one of" in err
    # Sorted whitelist is spelled out for the LLM.
    for t in ("archive", "correction", "entity_index", "link", "reminder", "summary"):
        assert t in err


def test_type_mismatch_filename_rejected():
    err = proposals.validate_suggestions_write(PATH, _fm("pending", type_="link"))
    assert err is not None
    assert "must match the filename type segment" in err


# ── path shape: top level only ────────────────────────────────────────────

def test_subdirectory_path_rejected():
    err = proposals.validate_suggestions_write(
        "suggestions/sub/x.md", _fm("pending"))
    assert err is not None
    assert "top-level files" in err
    assert "suggestions/sub/x.md" in err


# ── filename shape ────────────────────────────────────────────────────────

def test_invalid_calendar_date_rejected():
    err = proposals.validate_suggestions_write(
        "suggestions/2026-13-40_summary_x.md", _fm("pending"))
    assert err is not None
    assert "YYYY-MM-DD_<type>_<slug>.md" in err
    assert "2026-13-40_summary_x.md" in err


@pytest.mark.parametrize("name", [
    "summary_x.md",                       # missing date
    "2026-10-22_x.md",                    # missing type segment
    "2026-10-22_summary_.md",             # empty slug
    "2026-13-40_summary_x.txt",           # wrong extension
])
def test_bad_filename_rejected(name):
    err = proposals.validate_suggestions_write(f"suggestions/{name}", _fm("pending"))
    assert err is not None
    assert f"got '{name}'" in err


# ── existing file: status invariant ───────────────────────────────────────

def _existing_proposal(vault_dir, status="pending"):
    """Write a top-level proposal file directly and return its relpath."""
    (vault_dir / "suggestions").mkdir(parents=True, exist_ok=True)
    rel = "suggestions/2026-10-22_summary_x.md"
    (vault_dir / rel).write_bytes(_fm(status).encode("utf-8"))
    return rel


def test_existing_body_edit_same_status_allowed(vault_dir):
    rel = _existing_proposal(vault_dir, "pending")
    assert proposals.validate_suggestions_write(
        rel, _fm("pending", body="changed body"),
        _fm("pending", body="original body")) is None


@pytest.mark.parametrize("new_status", ["processing", "rejected", "merged"])
def test_existing_status_change_rejected(vault_dir, new_status):
    rel = _existing_proposal(vault_dir, "pending")
    err = proposals.validate_suggestions_write(
        rel, _fm(new_status), _fm("pending"))
    assert err is not None
    assert "cannot be changed by a direct write" in err
    assert "'pending' →" in err
    assert "proposals.advance" in err


def test_existing_without_frontmatter_new_valid_pending_allowed(vault_dir):
    rel = "suggestions/2026-10-22_summary_x.md"
    (vault_dir / "suggestions").mkdir(parents=True, exist_ok=True)
    (vault_dir / rel).write_bytes(b"# no frontmatter\n")
    assert proposals.validate_suggestions_write(
        rel, _fm("pending"), "# no frontmatter\n") is None


def test_existing_without_frontmatter_forged_status_rejected(vault_dir):
    rel = "suggestions/2026-10-22_summary_x.md"
    (vault_dir / "suggestions").mkdir(parents=True, exist_ok=True)
    (vault_dir / rel).write_bytes(b"# no frontmatter\n")
    err = proposals.validate_suggestions_write(
        rel, _fm("merged"), "# no frontmatter\n")
    assert err is not None
    assert "new proposals start with status: pending" in err


# ── integration: channel validation entry (validate_tool_call) ────────────
# The gate is the channel's real path: the suggestions/ invariant is
# enforced at the validate stage (before confirm), not inside _execute.

def test_write_doc_tool_rejects_status_change(vault_dir):
    _existing_proposal(vault_dir, "pending")
    result = validate_tool_call(
        "write_doc", {"path": PATH, "content": _fm("rejected")},
        ChannelPolicy(vault_dir))
    assert result is not None
    assert "cannot be changed by a direct write" in result
    # The write must NOT have happened.
    assert (vault_dir / PATH).read_bytes() == _fm("pending").encode("utf-8")


def test_write_doc_tool_allows_valid_new_proposal(vault_dir):
    result = validate_tool_call(
        "write_doc", {"path": PATH, "content": _fm("pending")},
        ChannelPolicy(vault_dir))
    assert result is None


def test_edit_doc_tool_rejects_status_change(vault_dir):
    _existing_proposal(vault_dir, "pending")
    result = validate_tool_call(
        "edit_doc", {"path": PATH, "old_string": "status: pending",
                     "new_string": "status: rejected"},
        ChannelPolicy(vault_dir))
    assert result is not None
    assert "cannot be changed by a direct write" in result
    assert (vault_dir / PATH).read_bytes() == _fm("pending").encode("utf-8")


def test_edit_doc_tool_allows_body_edit(vault_dir):
    _existing_proposal(vault_dir, "pending")
    result = validate_tool_call(
        "edit_doc", {"path": PATH, "old_string": "body text", "new_string": "new body"},
        ChannelPolicy(vault_dir))
    assert result is None


def test_edit_doc_tool_nonunique_match_defers_to_edit_file(vault_dir):
    """Count != 1: the gate defers (returns None); edit_file's canonical
    error is returned at the execution layer."""
    rel = "suggestions/2026-10-22_summary_x.md"
    (vault_dir / "suggestions").mkdir(parents=True, exist_ok=True)
    (vault_dir / rel).write_bytes(
        _fm("pending", body="dup dup").encode("utf-8"))
    assert validate_tool_call(
        "edit_doc", {"path": rel, "old_string": "dup", "new_string": "x"},
        ChannelPolicy(vault_dir)) is None
    result = execute_tool(
        "edit_doc", {"path": rel, "old_string": "dup", "new_string": "x"},
        ToolContext(vault_dir=vault_dir))
    assert "Error" in result
    assert "2 times" in result


# ── move/delete: suggestions subtree is state-machine territory ──────────
# Gap closure (spec §4 #6/#7): a move could rename a proposal (breaking the
# filename convention) or hide it under a subdirectory (out of scan()'s
# sight); a delete kills a proposal while the ledger keeps an orphan row.
# Subtree = any depth (a top-segment check would leave a subdirectory
# bypass). Cleanup goes through the Web UI human channel.

TERRITORY = "suggestions/ is state-machine territory"


def test_move_docs_rejects_suggestions_out(vault_dir):
    """Move a proposal OUT of the buffer (any direction touches the subtree)."""
    _existing_proposal(vault_dir, "pending")
    result = validate_tool_call(
        "move_docs", {"moves": [{"from_path": PATH, "to_path": "notes/moved.md"}]},
        ChannelPolicy(vault_dir))
    assert result is not None
    assert TERRITORY in result


def test_move_docs_rejects_suggestions_in_subdirectory(vault_dir):
    """Move INTO a suggestions subdirectory — out of scan()'s top-level sight."""
    _existing_proposal(vault_dir, "pending")
    result = validate_tool_call(
        "move_docs", {"moves": [{"from_path": "notes/in.md",
                                 "to_path": "suggestions/sub/deep.md"}]},
        ChannelPolicy(vault_dir))
    assert result is not None
    assert TERRITORY in result


def test_move_docs_rejects_subdirectory_to_subdirectory(vault_dir):
    """Subtree at any depth: even a move that stays inside suggestions/."""
    (vault_dir / "suggestions" / "sub").mkdir(parents=True)
    (vault_dir / "suggestions" / "sub" / "2026-10-22_summary_x.md").write_bytes(
        _fm("pending").encode("utf-8"))
    result = validate_tool_call(
        "move_docs", {"moves": [{"from_path": "suggestions/sub/2026-10-22_summary_x.md",
                                 "to_path": "suggestions/sub2/deeper.md"}]},
        ChannelPolicy(vault_dir))
    assert result is not None
    assert TERRITORY in result


def test_delete_doc_rejects_suggestions(vault_dir):
    _existing_proposal(vault_dir, "pending")
    result = validate_tool_call(
        "delete_doc", {"path": PATH}, ChannelPolicy(vault_dir))
    assert result is not None
    assert TERRITORY in result
    # The file must NOT have been deleted.
    assert (vault_dir / PATH).is_file()


def test_delete_doc_rejects_suggestions_subdirectory(vault_dir):
    (vault_dir / "suggestions" / "sub").mkdir(parents=True)
    (vault_dir / "suggestions" / "sub" / "2026-10-22_summary_x.md").write_bytes(
        _fm("pending").encode("utf-8"))
    result = validate_tool_call(
        "delete_doc", {"path": "suggestions/sub/2026-10-22_summary_x.md"},
        ChannelPolicy(vault_dir))
    assert result is not None
    assert TERRITORY in result


def test_move_docs_and_delete_doc_allow_main_area(vault_dir):
    """Sanity: the rejection is suggestions-subtree-only — main-area
    move/delete decisions are unchanged."""
    (vault_dir / "suggestions").mkdir(parents=True, exist_ok=True)
    assert validate_tool_call(
        "move_docs", {"moves": [{"from_path": "notes/a.md",
                                 "to_path": "notes/b.md"}]},
        ChannelPolicy(vault_dir)) is None
    assert validate_tool_call(
        "delete_doc", {"path": "notes/a.md"}, ChannelPolicy(vault_dir)) is None


# ── meta-test: every registered ask tool is gate-covered ─────────────────

def _ask_tools() -> list[str]:
    from server.agent.tools import TOOL_REGISTRY
    return [n for n, t in TOOL_REGISTRY.items() if t.permission == "ask"]


def _meta_probe(tool_name: str) -> dict:
    """Probe call pointing into suggestions/ for the meta-test.

    Known tools get a probe that must be rejected for the reason the gate
    covers them (write/edit: content invariant; move/delete: territory).
    A future ask tool falls back to the standard ``path`` probe: standard
    arg names + uncovered branch → fail-closed rejection (green = enforced);
    non-standard arg names → no path reaches the gate → red (wire it).
    """
    return {
        "write_doc": {"path": PATH, "content": "no frontmatter"},
        "edit_doc": {"path": PATH, "old_string": "status: pending",
                     "new_string": "status: merged"},
        "move_docs": {"moves": [{"from_path": PATH, "to_path": "notes/moved.md"}]},
        "delete_doc": {"path": PATH},
    }.get(tool_name, {"path": PATH})


@pytest.mark.parametrize("tool_name", _ask_tools())
def test_meta_ask_tool_suggestions_probe_rejected(tool_name, vault_dir):
    """spec §5 meta-test: iterate every permission=='ask' tool, probe into
    suggestions/, assert validate_tool_call rejects — when a new ask tool
    lands, an uncovered gate branch fails CI here instead of silently
    allowing writes through."""
    _existing_proposal(vault_dir, "pending")
    result = validate_tool_call(
        tool_name, _meta_probe(tool_name), ChannelPolicy(vault_dir))
    assert result is not None, f"gate does not cover ask tool {tool_name!r}"
