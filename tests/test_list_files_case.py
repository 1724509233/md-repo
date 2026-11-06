"""list_files directory-name case normalization (bug e774d7a0).

On-disk directory case is authoritative: querying a directory with a
different case variant (``projects`` / ``PROJECTS`` / ``Projects``) must
resolve to the same on-disk directory and the same file set — on
case-sensitive volumes the old plain ``vault / dir`` join silently
yielded zero files.

Exclusion regression: system dirs (index/, suggestions/) must be hidden
from every read surface regardless of on-disk case (lowercase canonical
or wrong-case creation like INDEX/ / SUGGESTIONS/).

Files are written in binary mode so the on-disk bytes are stable
(Windows newline translation — see AGENTS.md Gotchas).
"""
from __future__ import annotations

import hashlib
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
    """Vault whose on-disk directory is uppercase ``PROJECTS`` with a nested subdir.

    Uses ``PROJECTS`` (not ``INDEX``) because system dirs are excluded
    from list_files by design (EXCLUDED_INDEX_DIRS). Case-resolution
    behavior is identical for any non-excluded top-level directory.
    """
    vault = tmp_path / "vault"
    (vault / ".sync").mkdir(parents=True)
    (vault / ".sync" / "skip.md").write_bytes(SAMPLE)
    proj = vault / "PROJECTS"
    sub = proj / "sub"
    sub.mkdir(parents=True)
    (proj / "a.md").write_bytes(SAMPLE)
    (sub / "b.md").write_bytes(SAMPLE)
    (sub / "c.md").write_bytes(SAMPLE)
    (vault / "root.md").write_bytes(SAMPLE)
    return vault


def test_list_files_dir_case_variants_same_files(case_vault: Path):
    """All case variants of the same directory return the identical on-disk file set."""
    baseline_ctx = ToolContext(vault_dir=case_vault)
    baseline = _lines(execute_tool("list_files", {"dir": "PROJECTS"}, baseline_ctx))
    assert baseline == ["PROJECTS/a.md", "PROJECTS/sub/b.md", "PROJECTS/sub/c.md"]
    assert baseline_ctx.last_meta == {"count": 3}

    for variant in ("projects", "Projects", "PrOjEcTs"):
        ctx = ToolContext(vault_dir=case_vault)
        result = execute_tool("list_files", {"dir": variant}, ctx)
        assert _lines(result) == baseline, f"variant {variant!r} diverged"
        assert ctx.last_meta == {"count": 3}


def test_list_files_nested_dir_case_variant(case_vault: Path):
    """Case normalization applies per path component (nested dirs)."""
    ctx = ToolContext(vault_dir=case_vault)
    result = execute_tool("list_files", {"dir": "projects/Sub"}, ctx)
    assert _lines(result) == ["PROJECTS/sub/b.md", "PROJECTS/sub/c.md"]
    assert ctx.last_meta == {"count": 2}


def test_list_files_nonexistent_dir_empty_no_error(case_vault: Path):
    """A directory that does not exist in any case still yields the empty hint, no error."""
    for missing in ("nope", "PROJECTS/missing", "notes"):
        ctx = ToolContext(vault_dir=case_vault)
        result = execute_tool("list_files", {"dir": missing}, ctx)
        assert result == ListFilesTool.list_empty(missing)
        assert ctx.last_meta == {"count": 0}


def test_list_files_root_listing_unchanged(case_vault: Path):
    """dir='' root listing keeps its shape (disk case, .sync excluded)."""
    ctx = ToolContext(vault_dir=case_vault)
    result = execute_tool("list_files", {}, ctx)
    assert _lines(result) == [
        "PROJECTS/a.md", "PROJECTS/sub/b.md", "PROJECTS/sub/c.md", "root.md"
    ]
    assert ".sync" not in result
    assert ctx.last_meta == {"count": 4}


def test_list_files_conftest_fixture_vault(vault_dir: Path):
    """Same fix verified against the shared conftest vault_dir fixture."""
    proj = vault_dir / "PROJECTS"
    proj.mkdir()
    (proj / "note.md").write_bytes(SAMPLE)

    ctx = ToolContext(vault_dir=vault_dir)
    result = execute_tool("list_files", {"dir": "projects"}, ctx)
    assert "PROJECTS/note.md" in result
    assert ctx.last_meta == {"count": 1}

    ctx2 = ToolContext(vault_dir=vault_dir)
    result2 = execute_tool("list_files", {"dir": "PROJECTS"}, ctx2)
    assert _lines(result2) == _lines(result)


# ── On-disk lowercase dir (case-resolution for a lowercase on-disk dir) ──


@pytest.fixture
def lowercase_vault(tmp_path: Path) -> Path:
    """Vault whose on-disk directory is lowercase ``docs/`` with a nested subdir."""
    vault = tmp_path / "vault"
    (vault / ".sync").mkdir(parents=True)
    (vault / ".sync" / "skip.md").write_bytes(SAMPLE)
    d = vault / "docs"
    sub = d / "sub"
    sub.mkdir(parents=True)
    (d / "a.md").write_bytes(SAMPLE)
    (sub / "b.md").write_bytes(SAMPLE)
    (sub / "c.md").write_bytes(SAMPLE)
    (vault / "root.md").write_bytes(SAMPLE)
    return vault


def test_list_files_lowercase_dir_case_variants(lowercase_vault: Path):
    """All case variants (DOCS/docs/Docs) of a lowercase on-disk dir resolve
    to the same on-disk file set."""
    baseline_ctx = ToolContext(vault_dir=lowercase_vault)
    baseline = _lines(execute_tool("list_files", {"dir": "docs"}, baseline_ctx))
    assert baseline == ["docs/a.md", "docs/sub/b.md", "docs/sub/c.md"]
    assert baseline_ctx.last_meta == {"count": 3}

    for variant in ("DOCS", "Docs", "DoCs"):
        ctx = ToolContext(vault_dir=lowercase_vault)
        result = execute_tool("list_files", {"dir": variant}, ctx)
        assert _lines(result) == baseline, f"variant {variant!r} diverged"
        assert ctx.last_meta == {"count": 3}

    # Nested variant: mixed-case sub dir resolves to on-disk lowercase
    ctx = ToolContext(vault_dir=lowercase_vault)
    result = execute_tool("list_files", {"dir": "Docs/Sub"}, ctx)
    assert _lines(result) == ["docs/sub/b.md", "docs/sub/c.md"]
    assert ctx.last_meta == {"count": 2}


def test_list_files_lowercase_root_listing(lowercase_vault: Path):
    """Root listing includes lowercase docs/ files, keeps .sync excluded."""
    ctx = ToolContext(vault_dir=lowercase_vault)
    result = execute_tool("list_files", {}, ctx)
    assert _lines(result) == [
        "docs/a.md", "docs/sub/b.md", "docs/sub/c.md", "root.md"
    ]
    assert ".sync" not in result
    assert ctx.last_meta == {"count": 4}


# ── Exclusion regression: system dirs hidden for every on-disk case ───────


def test_t4a_lowercase_index_excluded(tmp_path: Path):
    """On-disk lowercase index/ (canonical system dir case): every query
    case variant returns the empty hint; root listing excludes it."""
    vault = tmp_path / "vault"
    (vault / ".sync").mkdir(parents=True)
    (vault / ".sync" / "skip.md").write_bytes(SAMPLE)
    idx = vault / "index"
    idx.mkdir()
    (idx / "seed.md").write_bytes(SAMPLE)
    (vault / "root.md").write_bytes(SAMPLE)

    for variant in ("index", "INDEX", "Index"):
        ctx = ToolContext(vault_dir=vault)
        result = execute_tool("list_files", {"dir": variant}, ctx)
        assert result == ListFilesTool.list_empty(variant), (
            f"variant {variant!r}: system dir must be hidden"
        )
        assert ctx.last_meta == {"count": 0}

    # Root listing: no index/… paths, no .sync
    ctx = ToolContext(vault_dir=vault)
    result = execute_tool("list_files", {}, ctx)
    lines = _lines(result)
    assert lines == ["root.md"], f"root listing leaked system dir: {lines}"
    assert ".sync" not in result
    assert ctx.last_meta == {"count": 1}


def test_t4b_uppercase_INDEX_excluded(tmp_path: Path):
    """On-disk uppercase INDEX/ (wrong-case creation): still excluded by
    the casefolded compare. Pins the regression fix."""
    vault = tmp_path / "vault"
    (vault / ".sync").mkdir(parents=True)
    (vault / ".sync" / "skip.md").write_bytes(SAMPLE)
    idx = vault / "INDEX"
    idx.mkdir()
    (idx / "seed.md").write_bytes(SAMPLE)
    (vault / "root.md").write_bytes(SAMPLE)

    for variant in ("INDEX", "index", "Index"):
        ctx = ToolContext(vault_dir=vault)
        result = execute_tool("list_files", {"dir": variant}, ctx)
        assert result == ListFilesTool.list_empty(variant), (
            f"variant {variant!r}: wrong-case system dir must be hidden"
        )
        assert ctx.last_meta == {"count": 0}

    # Root listing: no INDEX/… paths, no .sync
    ctx = ToolContext(vault_dir=vault)
    result = execute_tool("list_files", {}, ctx)
    lines = _lines(result)
    assert lines == ["root.md"], f"root listing leaked wrong-case system dir: {lines}"
    assert ".sync" not in result
    assert ctx.last_meta == {"count": 1}


def test_t4c_web_surface_wrong_case_system_dir(client, vault_dir, db_conn):
    """Web /api/docs surface: a wrong-case SUGGESTIONS/ dir seeded in the
    files table must appear in neither the file list nor the dir tree."""
    (vault_dir / "SUGGESTIONS").mkdir()
    (vault_dir / "SUGGESTIONS" / "x.md").write_bytes(b"# x\n")
    # Seed the files table (the indexing pipeline would have done this).
    digest = hashlib.sha256(b"# x\n").hexdigest()
    mtime = (vault_dir / "SUGGESTIONS" / "x.md").stat().st_mtime
    db_conn.execute(
        "INSERT INTO files (path, hash, mtime) VALUES (?, ?, ?)",
        ("SUGGESTIONS/x.md", digest, mtime),
    )
    r = client.get("/api/docs")
    assert r.status_code == 200
    body = r.json()
    # File list: SUGGESTIONS/x.md must not appear.
    file_paths = [f["path"] for f in body["files"]]
    assert "SUGGESTIONS/x.md" not in file_paths, (
        f"wrong-case system dir file leaked into web file list: {file_paths}"
    )
    # Dir tree: SUGGESTIONS must not appear.
    assert "SUGGESTIONS" not in body["dirs"], (
        f"wrong-case system dir leaked into web dir tree: {body['dirs']}"
    )
