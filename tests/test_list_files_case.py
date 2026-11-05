"""list_files directory-name case normalization (bug e774d7a0).

On-disk directory case is authoritative: querying a directory with a
different case variant (``index`` / ``INDEX`` / ``Index``) must resolve
to the same on-disk directory and the same file set — on case-sensitive
volumes the old plain ``vault / dir`` join silently yielded zero files.

Files are written in binary mode so the on-disk bytes are stable
(Windows newline translation — see AGENTS.md Gotchas).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from server.agent import execute_tool
from server.agent.tools.list_files import ListFilesTool
from server.agent.tools.tool import ToolContext

SAMPLE = b"# Note\n"


def _lines(result: str) -> list[str]:
    """Parse a list_files result string into its file lines (empty → [])."""
    if result.startswith("No markdown files"):
        return []
    return [ln for ln in result.splitlines() if ln]


@pytest.fixture
def case_vault(tmp_path: Path) -> Path:
    """Vault whose on-disk directory is uppercase ``INDEX`` with a nested subdir."""
    vault = tmp_path / "vault"
    (vault / ".sync").mkdir(parents=True)
    (vault / ".sync" / "skip.md").write_bytes(SAMPLE)
    idx = vault / "INDEX"
    sub = idx / "sub"
    sub.mkdir(parents=True)
    (idx / "a.md").write_bytes(SAMPLE)
    (sub / "b.md").write_bytes(SAMPLE)
    (sub / "c.md").write_bytes(SAMPLE)
    (vault / "root.md").write_bytes(SAMPLE)
    return vault


def test_list_files_dir_case_variants_same_files(case_vault: Path):
    """All case variants of the same directory return the identical on-disk file set."""
    baseline_ctx = ToolContext(vault_dir=case_vault)
    baseline = _lines(execute_tool("list_files", {"dir": "INDEX"}, baseline_ctx))
    assert baseline == ["INDEX/a.md", "INDEX/sub/b.md", "INDEX/sub/c.md"]
    assert baseline_ctx.last_meta == {"count": 3}

    for variant in ("index", "Index", "InDeX"):
        ctx = ToolContext(vault_dir=case_vault)
        result = execute_tool("list_files", {"dir": variant}, ctx)
        assert _lines(result) == baseline, f"variant {variant!r} diverged"
        assert ctx.last_meta == {"count": 3}


def test_list_files_nested_dir_case_variant(case_vault: Path):
    """Case normalization applies per path component (nested dirs)."""
    ctx = ToolContext(vault_dir=case_vault)
    result = execute_tool("list_files", {"dir": "index/Sub"}, ctx)
    assert _lines(result) == ["INDEX/sub/b.md", "INDEX/sub/c.md"]
    assert ctx.last_meta == {"count": 2}


def test_list_files_nonexistent_dir_empty_no_error(case_vault: Path):
    """A directory that does not exist in any case still yields the empty hint, no error."""
    for missing in ("nope", "INDEX/missing", "notes"):
        ctx = ToolContext(vault_dir=case_vault)
        result = execute_tool("list_files", {"dir": missing}, ctx)
        assert result == ListFilesTool.list_empty(missing)
        assert ctx.last_meta == {"count": 0}


def test_list_files_root_listing_unchanged(case_vault: Path):
    """dir='' root listing keeps its shape (disk case, .sync excluded)."""
    ctx = ToolContext(vault_dir=case_vault)
    result = execute_tool("list_files", {}, ctx)
    assert _lines(result) == [
        "INDEX/a.md", "INDEX/sub/b.md", "INDEX/sub/c.md", "root.md"
    ]
    assert ".sync" not in result
    assert ctx.last_meta == {"count": 4}


def test_list_files_conftest_fixture_vault(vault_dir: Path):
    """Same fix verified against the shared conftest vault_dir fixture."""
    idx = vault_dir / "INDEX"
    idx.mkdir()
    (idx / "note.md").write_bytes(SAMPLE)

    ctx = ToolContext(vault_dir=vault_dir)
    result = execute_tool("list_files", {"dir": "index"}, ctx)
    assert "INDEX/note.md" in result
    assert ctx.last_meta == {"count": 1}

    ctx2 = ToolContext(vault_dir=vault_dir)
    result2 = execute_tool("list_files", {"dir": "INDEX"}, ctx2)
    assert _lines(result2) == _lines(result)
