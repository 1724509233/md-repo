"""Architecture ratchet: the six import/dependency rules over ``server/**/*.py``.

Detection is AST-only (per the 2026-11-18 target-architecture spec §4):
import nodes and call nodes only — docstrings/comments never register, which
is why baseline entries may contain plain identifier literals and this file
is exempt from the repo's text-scan self-hit convention
(test_ledger_import_discipline.py). tests/** are not scanned (server tree only).
Dynamic imports (``importlib`` / ``__import__``) are OUT of the AST-only
aperture — server/ measures zero of them (Gate-2 ④); introducing one means
extending the detectors, not relaxing this note.

Violations are pinned in the external baseline
``tests/architecture_baseline.json`` (rule key -> sorted list of violation
strings). Ratchet: the current violation set must be a SUBSET of the baseline
(only shrink, never grow). Entries that no longer reproduce are reported as
shrinks — after legitimately fixing violations run

    pytest --update-arch-baseline

to rewrite the JSON. NOTE (Gate-1 M1): file moves re-register entries
(old string disappears + new string appears); that is a legal baseline
rewrite, not growth — reconcile the diff against the move before touching
the baseline. Reconciliation at re-freeze (card 5b): R1=72, R2=19 (⊂R1),
R3=24 live cross-dir statements, R4=38 call sites / 23 files,
R5 core=[] + upward_edges staged 2 (ai→orchestration, dies with card 8),
R6=0.

Rule keys (JSON keys, stable):

- ``R1_sql_outside_db`` — ``.execute/.executemany/.executescript`` and
  ``sqlite3.connect`` only inside ``server/vault/db/**``.
  Entry: ``path:lineno:method`` (method includes ``sqlite3.connect``).
  Exceptions frozen as baseline: recovery.py:100/:251 are the legitimate
  self-owned connections of the cold-rebuild / drill (spec §7-A1).
- ``R2_interface_sql`` — ``routes/**`` + ``api.py`` + ``mcp_server.py`` are
  SQL-free. Subset of R1, pinned separately so it zeroes first (cards 7/8).
  Once a module reaches zero its name is permanently banned — enforced by
  ``R2_ZEROED_MODULES`` (live violations AND baseline entries, card 7).
- ``R3_private_cross_dir`` — no ``from <other __init__ dir> import _x``
  (any ``__init__`` directory granularity, spec §7-A2; root modules belong to
  package ``server``). Parent-package-from-child and vice versa both count.
  Public-symbol re-exports (e.g. the FileEntry stub) are out of scope.
  Entry: ``path:lineno:_name[,_name…]``.
- ``R4_lazy_conn`` — ``get_index_conn`` / ``get_ledger_conn`` callers are a
  frozen baseline list (only shrinks; card 10/11 plan in spec §4). The
  defining module ``server/vault/db/conn.py`` is exempt. Entry:
  ``path:lineno:get_index_conn|get_ledger_conn``.
- ``R5_core_upward`` — ``core ⊄ anything``: ``server/core/**`` may import
  nothing under ``server/`` except ``server.core`` (bare ``import server``
  included — Gate-1 M6 blind-spot closed).
- ``R5_upward_edges`` — package-level forbidden edges (spec §3 rule 5):
  ``orchestration ⊄ {services, routes}``,
  ``services ⊄ {agent, bgagent, routes}``,
  ``db ⊄ {orchestration, services, agent, bgagent, routes}``,
  ``ai ⊄ {services, agent, bgagent, routes, vault.orchestration}`` (the
  orchestration edge was staged and died with card 8b; spec §7-A7 closed).
  ``_STAGED_EDGES`` is permanently empty since card 8b — pinned by
  test_detector_staged_edges_permanently_empty (M9). Entry:
  ``path:lineno:target_module``.
- ``R6_identity_assignment`` — ``agent/tools/**`` may never assign
  ``ctx.write_source`` / ``ctx.run_id`` / ``ctx.session_id`` (identity is
  injected by channel entry points only — red-line 3). AnnAssign dataclass
  field defaults (tool.py) are NOT violations and never enter the baseline.
  ``dataclasses.replace(ctx, write_source=…)`` is a documented blind spot.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path, PurePosixPath

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_DIR = REPO_ROOT / "server"
BASELINE_PATH = Path(__file__).resolve().parent / "architecture_baseline.json"

R1_SQL = "R1_sql_outside_db"
R2_INTERFACE_SQL = "R2_interface_sql"
R3_PRIVATE = "R3_private_cross_dir"
R4_LAZY_CONN = "R4_lazy_conn"
R5_CORE = "R5_core_upward"
R5_EDGES = "R5_upward_edges"
R6_IDENTITY = "R6_identity_assignment"

# Forbidden package edges: source dir prefix -> tuple of forbidden target roots.
_FORBIDDEN_EDGES: dict[str, tuple[str, ...]] = {
    "server/vault/orchestration": ("server.services", "server.routes"),
    "server/services": ("server.agent", "server.bgagent", "server.routes"),
    "server/vault/db": (
        "server.vault.orchestration", "server.services", "server.agent",
        "server.bgagent", "server.routes",
    ),
    "server/ai": ("server.services", "server.agent", "server.bgagent",
                  "server.routes",
                  # card 8b: ai ⊄ orchestration moved from staged to HARD
                  # forbidden (A7 closed — _normalize_tag → fs/tags.py,
                  # lazy rebuild deleted per 案 A).
                  "server.vault.orchestration"),
}
# Staged edges: permanently empty since card 8b (M9). The mechanism stays
# (R5 edges read both tables) but the table must never hold entries again —
# pinned by test_detector_staged_edges_permanently_empty. A future edge that
# genuinely needs staging must edit that test on purpose (forced review),
# never quietly land a zombie entry here.
_STAGED_EDGES: dict[str, tuple[str, ...]] = {}
_DB_PKG = "server/vault/db"
_CONN_MODULE = "server/vault/db/conn.py"
_SQL_METHODS = frozenset({"execute", "executemany", "executescript"})
_CONN_FUNCS = frozenset({"get_index_conn", "get_ledger_conn"})
_IDENTITY_ATTRS = frozenset({"write_source", "run_id", "session_id"})

# R2 zeroed modules (Gate-2 ③ review discipline made code, card 7): once a
# module's interface-layer SQL reaches zero its name is PERMANENTLY banned —
# live violations are banned, and baseline entries may not return via
# --update-arch-baseline (the banned checks run after the flag rewrite too).
# mcp_server.py joined at card 8b (PRAGMA → init_db(busy_timeout_ms=…)).
R2_ZEROED_MODULES: frozenset[str] = frozenset({
    "server/mcp_server.py",
    "server/routes/agent.py",
    "server/routes/egress.py",
    "server/routes/memory.py",
    "server/routes/notifications.py",
    "server/routes/recall.py",
    "server/routes/sync.py",
})


# --------------------------------------------------------------------------
# shared helpers
# --------------------------------------------------------------------------

def _package_of(rel: PurePosixPath) -> str:
    """Package containing a repo-relative file path — the Gate-1 M6 fix.

    Dropping the final path component yields the package for BOTH shapes:
    ``server/core/types.py`` → ``server.core`` (parent dir), and
    ``server/core/__init__.py`` → ``server.core`` (the package itself).
    The old bug reduced ``__init__.py`` to its module name ``server.core``
    first and then rsplit' off one more segment, producing ``server`` —
    which flagged legitimate intra-package relative imports as upward.
    """
    parts = list(PurePosixPath(rel).parts)
    assert parts and parts[-1] != "", rel
    return ".".join(parts[:-1])


def _iter_server_py(exclude: tuple[str, ...] = ()):
    for py in sorted(SERVER_DIR.rglob("*.py")):
        rel = py.relative_to(REPO_ROOT).as_posix()
        if rel.startswith(exclude):
            continue
        yield rel, py


def _import_targets(node: ast.Import | ast.ImportFrom, pkg: str) -> list[str]:
    """Resolve an import node to absolute dotted target modules.

    ``from server import X`` / ``from .. import X`` expand to the per-name
    submodule candidates. Bare ``import server`` yields ``server`` (flagged
    upward for core files — M6 blind-spot closed).
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
    """True if a target is a server module outside ``server.core`` (R5 core)."""
    if target == "server":  # bare ``import server`` — reaching the root
        return True         # package itself is already an upward edge
    if not target.startswith("server."):
        return False
    return not (target == "server.core" or target.startswith("server.core."))


def _source_dir_of(module: str) -> str:
    """Directory (package) that defines ``module`` (R3, any-__init__ rule).

    Filesystem-confirmed: a package source lives in the package dir itself,
    a plain module's symbols live one level up (its parent package).
    """
    if module == "server":
        return "server"
    sub = PurePosixPath(*module.split("."))
    if (SERVER_DIR.parent / sub / "__init__.py").exists():
        return module
    return module.rsplit(".", 1)[0]


def _is_sql_call(node: ast.Call) -> str | None:
    """Return the recorded method name for an SQL-executing Call, else None."""
    fn = node.func
    if isinstance(fn, ast.Attribute):
        if fn.attr in _SQL_METHODS:
            return fn.attr
        if fn.attr == "connect":
            v = fn.value
            if isinstance(v, ast.Name):
                root = v.id
            elif isinstance(v, ast.Attribute):
                root = v.attr
            else:
                root = None
            if root == "sqlite3":
                return "sqlite3.connect"
    return None


# --------------------------------------------------------------------------
# scanners (each returns sorted violation strings)
# --------------------------------------------------------------------------

def _rel_of(py: Path) -> str:
    return py.relative_to(REPO_ROOT).as_posix()


def _sql_violations(files) -> list[str]:
    out = []
    for rel, py in files:
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                m = _is_sql_call(node)
                if m:
                    out.append(f"{rel}:{node.lineno}:{m}")
    return sorted(out)


def _r1_violations() -> list[str]:
    files = [(r, p) for r, p in _iter_server_py()
             if not r.startswith(_DB_PKG + "/")]
    return _sql_violations(files)


def _r2_violations() -> list[str]:
    def iface(r: str) -> bool:
        return (r.startswith("server/routes/")
                or r in ("server/api.py", "server/mcp_server.py"))
    return _sql_violations([(r, p) for r, p in _iter_server_py() if iface(r)])


def _r3_violations() -> list[str]:
    out = []
    for rel, py in _iter_server_py():
        pkg = _package_of(PurePosixPath(rel))
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            priv = [a.name for a in node.names if a.name.startswith("_")]
            if not priv:
                continue
            base = _import_targets(node, pkg)
            if not base:
                continue
            src_dir = _source_dir_of(base[0])
            if src_dir != pkg:
                out.append(f"{rel}:{node.lineno}:{','.join(sorted(priv))}")
    return sorted(out)


def _r4_violations() -> list[str]:
    out = []
    for rel, py in _iter_server_py(exclude=(_CONN_MODULE,)):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else (
                fn.id if isinstance(fn, ast.Name) else None)
            if name in _CONN_FUNCS:
                out.append(f"{rel}:{node.lineno}:{name}")
    return sorted(out)


def _r5_core_violations() -> list[str]:
    out = []
    for rel, py in _iter_server_py():
        if PurePosixPath(rel).parts[1] != "core":
            continue
        pkg = _package_of(PurePosixPath(rel))  # M6: no naive rsplit
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            for target in _import_targets(node, pkg):
                if _is_upward(target):
                    out.append(f"{rel}:{node.lineno}:{target}")
    return sorted(out)


def _edges_for(src_dir: str) -> tuple[str, ...]:
    """Forbidden + staged target roots for a source directory.

    Gate-2 M8: PREFIX match, sub-package safe — a future
    ``server/services/sub/x.py`` inherits its parent group's edges (the
    old exact-dir lookup let any nested package escape R5 edges entirely;
    it was the only non-sub-package-safe key in the seven detectors).
    """
    out: list[str] = []
    for table in (_FORBIDDEN_EDGES, _STAGED_EDGES):
        for src, edges in table.items():
            if src_dir == src or src_dir.startswith(src + "/"):
                out.extend(edges)
    return tuple(dict.fromkeys(out))


def _r5_edge_violations() -> list[str]:
    out = []
    for rel, py in _iter_server_py():
        src_dir = PurePosixPath(rel).parent.as_posix()
        forbidden = _edges_for(src_dir)
        if not forbidden:
            continue
        pkg = _package_of(PurePosixPath(rel))
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            for target in _import_targets(node, pkg):
                if any(target == f or target.startswith(f + ".")
                       for f in forbidden):
                    out.append(f"{rel}:{node.lineno}:{target}")
    return sorted(out)


def _r6_violations() -> list[str]:
    out = []
    for rel, py in _iter_server_py():
        if not rel.startswith("server/agent/tools/"):
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AugAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) \
                else [node.target]
            for t in targets:
                for sub in ast.walk(t):
                    if isinstance(sub, ast.Attribute) \
                            and sub.attr in _IDENTITY_ATTRS:
                        out.append(f"{rel}:{node.lineno}:{sub.attr}")
    return sorted(out)


# --------------------------------------------------------------------------
# ratchet plumbing
# --------------------------------------------------------------------------

def _load_baseline() -> dict[str, list[str]]:
    if not BASELINE_PATH.exists():
        return {}
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def _write_baseline(rule: str, violations: list[str]) -> None:
    baseline = _load_baseline()
    old = set(baseline.get(rule, []))
    new = set(violations)
    baseline[rule] = violations
    BASELINE_PATH.write_text(
        json.dumps(baseline, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    # Gate-2 M11b: make a baseline wash loud — per-rule ± counts and the
    # entry list, so a drive-by --update-arch-baseline cannot hide in
    # terminal noise.
    gone, added = sorted(old - new), sorted(new - old)
    print(f"updated {BASELINE_PATH.name}: {rule}: {len(old)} -> {len(new)} "
          f"(−{len(gone)} +{len(added)})")
    for g in gone:
        print(f"  − {g}")
    for a in added:
        print(f"  + {a}")


def _check_ratchet(rule: str, current: list[str],
                   request: pytest.FixtureRequest) -> None:
    if request.config.getoption("update_arch_baseline"):
        _write_baseline(rule, current)
        return
    pinned = set(_load_baseline().get(rule, []))
    extra = sorted(set(current) - pinned)
    assert not extra, (
        f"{rule}: new architecture violations:\n{chr(10).join(extra)}\n"
        "(a file move re-registers entries — reconcile the diff first; "
        "only then: pytest --update-arch-baseline)"
    )
    shrunk = sorted(pinned - set(current))
    if shrunk:
        print(f"RATCHET SHRANK {rule} by {len(shrunk)} — "
              "commit the tightened baseline: pytest --update-arch-baseline")


# --------------------------------------------------------------------------
# the six rule tests
# --------------------------------------------------------------------------

def test_core_has_no_upward_imports(request: pytest.FixtureRequest) -> None:
    """R5 core slice: core ⊄ anything — zero violations, pinned."""
    _check_ratchet(R5_CORE, _r5_core_violations(), request)


def test_sql_only_inside_vault_db(request: pytest.FixtureRequest) -> None:
    """R1: SQL exec/connect outside server/vault/db are baseline-frozen."""
    _check_ratchet(R1_SQL, _r1_violations(), request)


def test_interface_layer_sql(request: pytest.FixtureRequest) -> None:
    """R2: routes/api/mcp_server SQL is a baseline-frozen subset of R1."""
    current = _r2_violations()
    assert set(current) <= set(_r1_violations()), \
        "R2 violations must always be a subset of R1"
    _check_ratchet(R2_INTERFACE_SQL, current, request)
    # Gate-2 M11a: the live-vs-live assertion above is structurally
    # tautological (R2's file set never intersects R1's db/ exclusion);
    # the meaningful one is pinned-vs-pinned, and it runs after a flag
    # rewrite too.
    pinned = _load_baseline()
    assert set(pinned.get(R2_INTERFACE_SQL, [])) <= set(
        pinned.get(R1_SQL, [])
    ), "pinned R2 must stay a subset of pinned R1"
    # Zeroed modules are permanently banned — live and baseline (the flag
    # rewrite above runs first, so a wash cannot smuggle them back).
    banned_live = {v for v in current
                   if v.split(":", 1)[0] in R2_ZEROED_MODULES}
    assert not banned_live, (
        f"R2-zeroed modules must not execute SQL again: {sorted(banned_live)}")
    banned_pinned = {
        e for e in _load_baseline().get(R2_INTERFACE_SQL, [])
        if e.split(":", 1)[0] in R2_ZEROED_MODULES
    }
    assert not banned_pinned, (
        f"R2-zeroed modules must not be in the baseline: {sorted(banned_pinned)}")


def test_no_private_cross_dir_imports(
        request: pytest.FixtureRequest) -> None:
    """R3: cross-``__init__``-dir imports of ``_private`` are frozen."""
    _check_ratchet(R3_PRIVATE, _r3_violations(), request)


def test_lazy_conn_callers_frozen(request: pytest.FixtureRequest) -> None:
    """R4: get_index_conn/get_ledger_conn callers = frozen baseline."""
    _check_ratchet(R4_LAZY_CONN, _r4_violations(), request)


def test_no_upward_package_edges(request: pytest.FixtureRequest) -> None:
    """R5 edges: orchestration/services/db/ai forbidden upward imports."""
    _check_ratchet(R5_EDGES, _r5_edge_violations(), request)


def test_identity_fields_not_assigned_in_tools(
        request: pytest.FixtureRequest) -> None:
    """R6: agent/tools never assign ctx.write_source/run_id/session_id."""
    _check_ratchet(R6_IDENTITY, _r6_violations(), request)


# --------------------------------------------------------------------------
# Gate-1 M6: detector meta-tests (synthetic ASTs — no repo files touched)
# --------------------------------------------------------------------------

def test_detector_init_relative_import_not_flagged() -> None:
    """``server/core/__init__.py: from .types import FileEntry`` is legal.

    The old package-parse bug turned this into ``server.types`` → false
    upward violation.
    """
    pkg = _package_of(PurePosixPath("server/core/__init__.py"))
    assert pkg == "server.core"
    node = ast.parse("from .types import FileEntry").body[0]
    targets = _import_targets(node, pkg)
    assert not any(_is_upward(t) for t in targets), targets


def test_detector_upward_import_from_core_init_flagged() -> None:
    """``server/core/__init__.py: from .. import events_bus`` is red."""
    pkg = _package_of(PurePosixPath("server/core/__init__.py"))
    node = ast.parse("from .. import events_bus").body[0]
    targets = _import_targets(node, pkg)
    assert any(t == "server.events_bus" and _is_upward(t) for t in targets)


def test_detector_bare_import_server_flagged_for_core() -> None:
    """Bare ``import server`` in core is an upward edge (M6 blind spot)."""
    node = ast.parse("import server").body[0]
    targets = _import_targets(node, "server.core")
    assert targets == ["server"] and _is_upward(targets[0])


def test_detector_regular_module_pkg_unchanged() -> None:
    pkg = _package_of(PurePosixPath("server/core/types.py"))
    assert pkg == "server.core"


def test_detector_r5_edges_subpackage_safe() -> None:
    """Gate-2 M8: a hypothetical sub-package inherits its group's edges.

    ``server/services/sub/x.py`` importing ``server.routes`` must be RED —
    under the old exact-dir lookup the nested dir matched no key and the
    whole sub-package escaped R5 edges.
    """
    assert "server.routes" in _edges_for("server/services/sub/deeper")
    assert "server.agent" in _edges_for("server/services")  # sanity: base
    assert "server.vault.orchestration" in _edges_for("server/vault/db/x")
    assert "server.vault.orchestration" in _edges_for("server/ai/sub")  # hard-forbidden since card 8b
    assert _edges_for("server") == ()          # root is no source group
    assert _edges_for("server/templates") == ()  # untracked dir: no edges


def test_detector_staged_edges_permanently_empty() -> None:
    """M9 (Gate-2 review, executed card 8b): the staged-edge channel is
    sealed forever.

    Card 5b's staging mechanism carried exactly the two ai→orchestration
    edges; card 8b cleared them for real (``normalize_tag`` moved to
    ``fs/tags.py``, the lazy rebuild deleted per 案 A) and emptied the
    table. A non-empty ``_STAGED_EDGES`` would re-open a pre-approved
    upward edge without any test noticing — the zombie staging channel.
    Any future staging must edit THIS assertion on purpose (forced review).
    """
    assert _STAGED_EDGES == {}
