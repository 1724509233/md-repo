"""Architecture ratchet: import-dependency rules over ``server/**/*.py``.

Detection is AST-based (import nodes only), per the 2026-11-18 target-
architecture spec §4: AST parsing is why baseline entries may contain plain
identifier literals — the repo's "self-hit" text-scan convention
(test_ledger_import_discipline.py) does not apply here, since docstrings and
comments never appear as import nodes.

Violations are pinned in the external baseline file
``tests/architecture_baseline.json`` (rule name -> sorted list of
``path:lineno:target`` strings). The ratchet: the current violation set must
be a SUBSET of the baseline (only shrink, never grow). Baseline entries that
no longer reproduce are reported as shrinks — run

    pytest --update-arch-baseline

to rewrite the JSON after legitimately fixing violations.

First slice (card 1a) pins exactly one rule:

    R5_core_upward — ``core ⊄ anything``: no module under ``server/core/``
    may import any module inside ``server/`` other than ``server.core``
    itself. The shared kernel sits on the floor; it may depend on stdlib and
    third-party packages only.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_DIR = REPO_ROOT / "server"
BASELINE_PATH = Path(__file__).resolve().parent / "architecture_baseline.json"

# Rule ids must stay stable: they are the JSON keys in the baseline.
R5_CORE_UPWARD = "R5_core_upward"


def _module_of(py: Path) -> str:
    """Absolute dotted module name for a file under ``server/``."""
    parts = list(py.relative_to(REPO_ROOT).parts)
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1][:-len(".py")]
    return ".".join(parts)


def _import_targets(node: ast.Import | ast.ImportFrom, pkg: str) -> list[str]:
    """Resolve an import node to absolute dotted target modules.

    Relative imports resolve against ``pkg`` (the importing file's package).
    A target is the imported *module*; when the module is just the
    ``server`` root (``from server import X`` / ``from .. import X``), the
    imported names are the submodule candidates and are expanded instead.
    """
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]

    if node.level == 0:
        base = node.module or ""
    else:
        pkg_parts = pkg.split(".")
        if node.level > 1:
            pkg_parts = pkg_parts[: len(pkg_parts) - (node.level - 1)]
        base = ".".join(pkg_parts + ([node.module] if node.module else []))
    if base and base != "server":
        return [base]
    return [f"{base}.{alias.name}" if base else alias.name
            for alias in node.names]


def _is_upward(target: str) -> bool:
    """True if a resolved import target is a non-core module under server/."""
    if target == "server" or not target.startswith("server."):
        return False
    return not (target == "server.core" or target.startswith("server.core."))


def _core_violations() -> list[str]:
    """R5_core_upward: ``server/core/**`` imports of anything under server/."""
    violations: list[str] = []
    for py in sorted(SERVER_DIR.rglob("*.py")):
        if py.relative_to(SERVER_DIR).parts[0] != "core":
            continue
        pkg = _module_of(py).rsplit(".", 1)[0]
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            for target in _import_targets(node, pkg):
                if _is_upward(target):
                    rel = py.relative_to(REPO_ROOT).as_posix()
                    violations.append(f"{rel}:{node.lineno}:{target}")
    return sorted(violations)


def _load_baseline() -> dict[str, list[str]]:
    if not BASELINE_PATH.exists():
        return {}
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def test_core_has_no_upward_imports(request: pytest.FixtureRequest) -> None:
    """R5 (first slice): core ⊄ anything — zero violations, pinned."""
    violations = _core_violations()
    if request.config.getoption("update_arch_baseline"):
        baseline = _load_baseline()
        baseline[R5_CORE_UPWARD] = violations
        BASELINE_PATH.write_text(
            json.dumps(baseline, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"updated {BASELINE_PATH.name}: {R5_CORE_UPWARD} = {len(violations)}")
        return

    baseline = _load_baseline()
    pinned = set(baseline.get(R5_CORE_UPWARD, []))
    current = set(violations)
    extra = sorted(current - pinned)
    shrunk = sorted(pinned - current)
    assert not extra, (
        "new `core ⊄ anything` violations (server/core/** importing "
        f"non-core server modules):\n{chr(10).join(extra)}\n"
        "(if this is a legitimate grandfather, run: pytest --update-arch-baseline)"
    )
    if shrunk:
        print(
            f"RATCHET SHRANK {R5_CORE_UPWARD} by {len(shrunk)} — "
            "commit the tightened baseline: pytest --update-arch-baseline"
        )
