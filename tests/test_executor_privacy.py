"""Executor privacy merge guards (spec 2026-11-18 §2.3, plan Task 5).

Two auto-track surfaces leak privacy paths into the machine zone unless
guarded — both have NO human gate after adoption:

- ``entity_index``: ``source_refs`` render verbatim as ``- source: <ref>``
  lines into ``index/entities.md`` → privacy refs dropped line-wise; all
  dropped → ``_MergeFailed`` (zero provenance).
- ``relatedness``: source/target endpoint privacy-scoped → ``_MergeFailed``
  (the proposal channel must not mint a visible edge on a private path).

Equivalence pins: ``privacy_dirs=()`` (and ``privacy=None`` on the
scheduler chain) reproduce the pre-guard behavior byte-for-byte —
test_executor.py runs unmodified as the suite-wide pin; the explicit-()
tests here pin the parameter itself.

Reuses the executor fixture pattern of tests/test_executor.py (the
``_adopted`` legal-transition helper).
"""
from __future__ import annotations

import asyncio
import functools

from server.privacy import PrivacyConfig
from server.bgagent.executor import run_executor
from server import proposals
from tests.test_executor import _adopted

_PRIVACY = ("diary",)


def _private_file(vault_dir, rel="diary/a.md"):
    (vault_dir / "diary").mkdir(parents=True, exist_ok=True)
    fp = vault_dir / rel
    fp.write_bytes(b"# Private\n")  # binary mode: Windows hash rule
    return fp


# ── entity_index: source-ref line dropping ────────────────────────────────

def test_entity_index_privacy_ref_dropped_mixed(vault_dir, ledger_conn):
    # Mixed refs: the privacy line is removed wholesale, the rest render.
    _adopted(vault_dir, ledger_conn, "entity_index",
             source_refs=["diary/a.md", "notes/test.md"],
             payload={"entity": "e-mixed", "attrs": {"f": "v"}})
    summary = run_executor(vault_dir, ledger_conn, privacy_dirs=_PRIVACY)
    assert summary["merged"] == 1
    assert summary["errors"] == []

    ent = (vault_dir / "index" / "entities.md").read_text(encoding="utf-8")
    assert "- source: notes/test.md" in ent
    assert "- source: diary/a.md" not in ent
    assert "diary" not in ent  # the private path never appears at all


def test_entity_index_all_privacy_failed(vault_dir, ledger_conn):
    # Every source ref privacy-scoped → typed failure, record parked.
    rec = _adopted(vault_dir, ledger_conn, "entity_index",
                   source_refs=["diary/a.md", "diary/deep/b.md"],
                   payload={"entity": "e-all", "attrs": {"f": "v"}})
    summary = run_executor(vault_dir, ledger_conn, privacy_dirs=_PRIVACY)
    assert summary["merged"] == 0
    assert summary["errors"] == []  # typed failure, not a retryable error

    p = proposals.record(ledger_conn, rec["id"])
    assert p["status"] == "failed"
    assert "privacy" in p["reject_reason"]
    assert "source_refs" in p["reject_reason"]
    # Nothing landed: the section never reached entities.md.
    assert not (vault_dir / "index" / "entities.md").exists()


def test_entity_index_case_insensitive_drop(vault_dir, ledger_conn):
    # §2.2 ASCII-insensitive both sides: list "DIARY" hits ref "diary/a.md".
    _adopted(vault_dir, ledger_conn, "entity_index",
             source_refs=["diary/a.md", "notes/test.md"],
             payload={"entity": "e-case", "attrs": {"f": "v"}})
    summary = run_executor(vault_dir, ledger_conn, privacy_dirs=("DIARY",))
    assert summary["merged"] == 1
    ent = (vault_dir / "index" / "entities.md").read_text(encoding="utf-8")
    assert "- source: diary/a.md" not in ent
    assert "- source: notes/test.md" in ent


def test_entity_index_deep_same_name_not_hit(vault_dir, ledger_conn):
    # First-segment semantics: list "diary" must NOT hit "notes/diary/x".
    _adopted(vault_dir, ledger_conn, "entity_index",
             source_refs=["notes/diary/x.md"],
             payload={"entity": "e-deep", "attrs": {"f": "v"}})
    summary = run_executor(vault_dir, ledger_conn, privacy_dirs=_PRIVACY)
    assert summary["merged"] == 1
    ent = (vault_dir / "index" / "entities.md").read_text(encoding="utf-8")
    assert "- source: notes/diary/x.md" in ent


def test_entity_index_empty_dirs_identical(vault_dir, ledger_conn):
    # Equivalence pin: privacy_dirs=() renders the privacy-named ref as
    # before the guard existed (line kept, merge succeeds).
    _adopted(vault_dir, ledger_conn, "entity_index",
             source_refs=["diary/a.md", "notes/test.md"],
             payload={"entity": "e-legacy", "attrs": {"f": "v"}})
    summary = run_executor(vault_dir, ledger_conn, privacy_dirs=())
    assert summary["merged"] == 1
    ent = (vault_dir / "index" / "entities.md").read_text(encoding="utf-8")
    assert "- source: diary/a.md" in ent
    assert "- source: notes/test.md" in ent


# ── relatedness: endpoint guard ───────────────────────────────────────────

def test_relatedness_privacy_source_failed(vault_dir, ledger_conn):
    _private_file(vault_dir)
    rec = _adopted(vault_dir, ledger_conn, "relatedness",
                   source_refs=["diary/a.md"],
                   payload={"target": "notes/test.md"})
    summary = run_executor(vault_dir, ledger_conn, privacy_dirs=_PRIVACY)
    assert summary["merged"] == 0
    assert summary["errors"] == []

    p = proposals.record(ledger_conn, rec["id"])
    assert p["status"] == "failed"
    assert "privacy" in p["reject_reason"]
    # No edge minted for a private endpoint.
    assert ledger_conn.execute("SELECT COUNT(*) FROM relatedness").fetchone()[0] == 0


def test_relatedness_privacy_target_failed(vault_dir, ledger_conn):
    _private_file(vault_dir, "diary/b.md")
    rec = _adopted(vault_dir, ledger_conn, "relatedness",
                   source_refs=["notes/test.md"],
                   payload={"target": "diary/b.md"})
    summary = run_executor(vault_dir, ledger_conn, privacy_dirs=_PRIVACY)
    assert summary["merged"] == 0
    p = proposals.record(ledger_conn, rec["id"])
    assert p["status"] == "failed"
    assert "privacy" in p["reject_reason"]
    assert ledger_conn.execute("SELECT COUNT(*) FROM relatedness").fetchone()[0] == 0


def test_relatedness_guard_precedes_existence_probe(vault_dir, ledger_conn):
    # Ordering pin: a private endpoint that ALSO does not exist on disk is
    # refused as privacy-scoped, not as "not found" (even the liveness of
    # a private path is not for the proposal channel to learn).
    rec = _adopted(vault_dir, ledger_conn, "relatedness",
                   source_refs=["diary/absent.md"],
                   payload={"target": "notes/test.md"})
    summary = run_executor(vault_dir, ledger_conn, privacy_dirs=_PRIVACY)
    assert summary["merged"] == 0
    p = proposals.record(ledger_conn, rec["id"])
    assert p["status"] == "failed"
    assert "privacy" in p["reject_reason"]
    assert "not found" not in p["reject_reason"]


def test_relatedness_empty_dirs_identical(vault_dir, ledger_conn):
    # Equivalence pin: privacy_dirs=() lands the same edge as the pre-guard
    # executor (the diary endpoint merges).
    _private_file(vault_dir)
    rec = _adopted(vault_dir, ledger_conn, "relatedness",
                   source_refs=["diary/a.md"],
                   payload={"target": "notes/test.md"})
    summary = run_executor(vault_dir, ledger_conn, privacy_dirs=())
    assert summary["merged"] == 1
    assert summary["errors"] == []
    rows = ledger_conn.execute(
        "SELECT source, target FROM relatedness").fetchall()
    assert len(rows) == 1
    assert dict(rows[0])["source"] == "diary/a.md"


# ── scheduler parameter chain ─────────────────────────────────────────────

def _bg_cfg(tmp_path, alias="a"):
    from server.agent.spec import ModelConfig
    from server.config import BackgroundAgentConfig

    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    prompt = tmp_path / f"{alias}.md"
    prompt.write_text("p", encoding="utf-8")
    return BackgroundAgentConfig(
        name=alias, alias=alias, enabled=True, cron="0 3 * * *",
        target_dir=(vault / alias).resolve(), prompt=prompt.resolve(),
        report_dir=(vault / alias / ".reports" / alias).resolve(),
        max_rounds=5, timeout_s=10,
        model=ModelConfig(model_id="m", base_url="http://x"))


def _write_privacy_cfg(tmp_path, dirs: list[str]):
    cfg = tmp_path / "config_priv.yaml"
    body = "".join(f"  - {d}\n" for d in dirs)
    cfg.write_text(f"server:\n  host: localhost\nprivacy:\n  dirs:\n{body}",
                   encoding="utf-8")
    return cfg


def test_start_background_agents_wires_live_privacy_dirs(tmp_path, vault_dir,
                                                         ledger_conn, monkeypatch):
    """start_background_agents → executor chain: the default run_executor
    receives privacy_dirs from the PrivacyConfig, RE-READ every cycle
    (a config edit applies to the next cycle without a restart, §2.7)."""
    from server.bgagent import scheduler as sch

    cfg_path = _write_privacy_cfg(tmp_path, ["diary"])
    seen: list[tuple] = []
    monkeypatch.setattr(sch, "run_executor",
                        lambda v, c, privacy_dirs=(): seen.append(tuple(privacy_dirs)))

    captured: dict = {}

    async def _instant():
        return None

    def fake_loop(vault, conn, executor_fn, index_conn=None):
        captured["fn"] = executor_fn
        return _instant()

    monkeypatch.setattr(sch, "_executor_loop", fake_loop)

    async def go():
        tasks = sch.start_background_agents(
            [], vault_dir, ledger_conn=ledger_conn,
            privacy=PrivacyConfig(cfg_path))
        await asyncio.gather(*tasks)

    asyncio.run(go())
    fn = captured["fn"]

    fn(vault_dir, ledger_conn)
    assert seen == [("diary",)]

    # Live view: edit the config (deterministic mtime bump) → next cycle.
    cfg_path = _write_privacy_cfg(tmp_path, ["notes"])
    st = cfg_path.stat()
    import os
    os.utime(cfg_path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    fn(vault_dir, ledger_conn)
    assert seen == [("diary",), ("notes",)]


def test_start_background_agents_forwards_privacy_to_run_once(tmp_path, monkeypatch):
    """run_fn chain: the default run_once gets the SAME PrivacyConfig
    partial-bound (T3's run_once privacy kwarg — runner.py untouched)."""
    from server.bgagent import scheduler as sch

    cfg_path = _write_privacy_cfg(tmp_path, ["diary"])
    pc = PrivacyConfig(cfg_path)
    captured: dict = {}

    async def _instant():
        return None

    def fake_loop(cfg, vault, lock, hooks, run_fn, db_conn, all_aliases):
        captured["run_fn"] = run_fn
        return _instant()

    monkeypatch.setattr(sch, "_agent_loop", fake_loop)

    async def go():
        tasks = sch.start_background_agents(
            [_bg_cfg(tmp_path, "a")], tmp_path / "vault", privacy=pc)
        await asyncio.gather(*tasks)

    asyncio.run(go())
    rf = captured["run_fn"]
    assert isinstance(rf, functools.partial)
    assert rf.func is sch.run_once
    assert rf.keywords == {"privacy": pc}


def test_start_background_agents_privacy_none_is_identity(tmp_path, vault_dir,
                                                          ledger_conn, monkeypatch):
    """Equivalence pin: privacy=None → run_fn/run_executor stay the bare
    module functions — every call identical to the pre-privacy chain."""
    from server.bgagent import scheduler as sch

    captured: dict = {}

    async def _instant():
        return None

    def fake_agent_loop(cfg, vault, lock, hooks, run_fn, db_conn, all_aliases):
        captured["run_fn"] = run_fn
        return _instant()

    def fake_exec_loop(vault, conn, executor_fn, index_conn=None):
        captured["executor_fn"] = executor_fn
        return _instant()

    monkeypatch.setattr(sch, "_agent_loop", fake_agent_loop)
    monkeypatch.setattr(sch, "_executor_loop", fake_exec_loop)

    async def go():
        tasks = sch.start_background_agents(
            [_bg_cfg(tmp_path, "a")], vault_dir, ledger_conn=ledger_conn)
        await asyncio.gather(*tasks)

    asyncio.run(go())
    assert captured["run_fn"] is sch.run_once
    assert captured["executor_fn"] is sch.run_executor
