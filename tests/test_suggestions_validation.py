"""Write-path guard for the suggestions/ subtree (v2: the propose channel).

The DB record is the single source of truth for proposal state; v2a (D25)
the channel lands NO file (the suggestions/ dir stays zero-change —
legacy projection files are inert artifacts). Consequently EVERY direct
tool write/move/delete touching the suggestions/ subtree (any depth) is
rejected by the vault-level
:class:`SuggestionsGate` — unconditional across channels, no per-tool
branching — and the error points at the propose tool.

``validate_suggestions_write`` (the v1 content-semantic hook) is retired:
in the record world there is no legitimate direct-write shape for it to
accept, so the guard is now a pure path scan.
"""
from __future__ import annotations

import pytest

from server import proposals
from server.agent import ChannelPolicy, validate_tool_call

PATH = "suggestions/2026-10-26_summary_x.md"


# ── v1 API retired ─────────────────────────────────────────────────────────

def test_v1_content_hook_retired():
    """The file-frontmatter API is gone: no validate_suggestions_write, no
    parse_proposal/create_proposal (record semantics replaced them)."""
    assert not hasattr(proposals, "validate_suggestions_write")
    assert not hasattr(proposals, "parse_proposal")
    assert not hasattr(proposals, "create_proposal")
    assert not hasattr(proposals, "_created_ts")


# ── write_doc ──────────────────────────────────────────────────────────────

def test_write_doc_rejected(vault_dir):
    err = validate_tool_call(
        "write_doc", {"path": PATH, "content": "x"}, ChannelPolicy(vault_dir))
    assert err is not None
    assert err.startswith("Error:")
    assert "propose" in err


def test_write_doc_valid_v1_content_now_rejected(vault_dir):
    """v1 accepted this shape (a fresh pending proposal file); v2 rejects
    even a well-formed direct write — propose is the only path."""
    content = "---\ntype: summary\nstatus: pending\n---\n# T\n\nbody\n"
    err = validate_tool_call(
        "write_doc", {"path": PATH, "content": content}, ChannelPolicy(vault_dir))
    assert err is not None
    assert "propose" in err


def test_write_doc_nested_subtree_rejected(vault_dir):
    """Any depth inside suggestions/ is territory (subdir hiding is dead)."""
    err = validate_tool_call(
        "write_doc",
        {"path": "suggestions/sub/2026-10-26_summary_x.md", "content": "x"},
        ChannelPolicy(vault_dir))
    assert err is not None
    assert "propose" in err


# ── edit_doc ───────────────────────────────────────────────────────────────

def test_edit_doc_rejected_existing_file(vault_dir):
    """v1 deferred to the tool's canonical error when the file was missing;
    v2 rejects unconditionally (there is no legitimate edit shape)."""
    fp = vault_dir / PATH
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_bytes(b"---\nstatus: pending\n---\nbody\n")
    err = validate_tool_call(
        "edit_doc", {"path": PATH, "old_string": "a", "new_string": "b"},
        ChannelPolicy(vault_dir))
    assert err is not None
    assert "propose" in err


def test_edit_doc_rejected_missing_file(vault_dir):
    err = validate_tool_call(
        "edit_doc", {"path": PATH, "old_string": "a", "new_string": "b"},
        ChannelPolicy(vault_dir))
    assert err is not None
    assert "propose" in err


# ── delete_doc ─────────────────────────────────────────────────────────────

def test_delete_doc_rejected(vault_dir):
    err = validate_tool_call(
        "delete_doc", {"path": PATH}, ChannelPolicy(vault_dir))
    assert err is not None
    assert "propose" in err


# ── move_docs ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "moves",
    [
        # into the subtree (renaming a proposal was possible in v1 — dead)
        [{"from_path": "notes/a.md", "to_path": PATH}],
        # out of the subtree (hiding a proposal from the console)
        [{"from_path": PATH, "to_path": "notes/a.md"}],
        # within the subtree
        [{"from_path": "suggestions/sub/x.md", "to_path": "suggestions/other.md"}],
    ],
)
def test_move_docs_rejected(vault_dir, moves):
    err = validate_tool_call(
        "move_docs", {"moves": moves}, ChannelPolicy(vault_dir))
    assert err is not None
    assert "propose" in err


# ── the guard is a path scan: everything else passes ──────────────────────

def test_main_area_write_not_rejected(vault_dir):
    assert validate_tool_call(
        "write_doc", {"path": "notes/x.md", "content": "x"},
        ChannelPolicy(vault_dir)) is None


def test_move_outside_suggestions_not_rejected(vault_dir):
    assert validate_tool_call(
        "move_docs",
        {"moves": [{"from_path": "notes/a.md", "to_path": "notes/b.md"}]},
        ChannelPolicy(vault_dir)) is None


def test_read_tools_noop(vault_dir):
    """Reads into suggestions/ are fine (legacy projection files are
    inert but readable; the guard is write-only)."""
    assert validate_tool_call(
        "read_doc", {"path": PATH}, ChannelPolicy(vault_dir)) is None
    assert validate_tool_call(
        "search_docs", {"query": "x"}, ChannelPolicy(vault_dir)) is None


def test_similar_name_not_slipped(vault_dir):
    """suggestions2/ etc. must not be confused with the territory."""
    assert validate_tool_call(
        "write_doc", {"path": "suggestions2/x.md", "content": "x"},
        ChannelPolicy(vault_dir)) is None


# ── gate-level pin (validate_tool_call wiring) ─────────────────────────────

def test_gate_structural_ask_check(vault_dir):
    """The gate keys on permission == 'ask' via the registry: a registered
    ask tool touching the subtree is rejected, a read tool is a no-op."""
    from server.agent.policy import SuggestionsGate

    gate = SuggestionsGate()
    assert gate.check("write_doc", {"path": "suggestions/a/b/c.md"},
                      vault_dir) is not None
    assert gate.check("delete_doc", {"path": "suggestions/x.md"},
                      vault_dir) is not None
    assert gate.check("read_doc", {"path": "suggestions/a.md"}, vault_dir) is None
    assert gate.check("search_docs", {"query": "x"}, vault_dir) is None
