"""Tests for the reversibility layer (reversible-automation v2, P1).

Covers:

- spec invariant 2 (test anchor): single live pre-image slot per file;
  GC reclaims superseded pre-images and **never deletes the live one**;
  after N writes the live pre-image is exactly the version before the
  latest write.
- spec invariant 10: every LLM-originated write (agent direct-write tools
  + executor merge handlers) produces a pre-image row; summary slot
  overwrite keeps the old full text readable back.
- rollback (spec §3.1): verify-all-then-write-all, collision guard
  (file modified after the call → whole batch refused + conflicting file
  surfaced, nothing written), full restore when all untouched.
- delete-class snapshots: full pre-image captured (agent delete tool +
  Web UI delete), 30-day retention with injected time.
- bgagent channel: delete_doc hard-removed from the tool set even when
  the config whitelist explicitly lists it (spec §3.2, D3).

Snapshot test files are written in binary mode (AGENTS.md: Windows
newline translation would break hash equality).
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from server import snapshots
from server.agent import ToolContext, execute_tool
from server.agent.events import Event
from server.agent.runtime import Runtime
from server.bgagent.executor import run_executor
from server.bgagent.runner import run_once
from server import proposals


SAMPLE = b"# Test note\n\nSample content.\n"  # == notes/test.md in the vault_dir fixture


def _blob(vault_dir: Path, snap: dict) -> bytes:
    return (vault_dir / ".sync" / "snapshots" / snap["blob_name"]).read_bytes()


def _adopted(vault_dir, conn, type, source_refs=None, payload=None, run_id="batch-snap"):
    """Create a proposal record and drive it to ``adopted`` via legal transitions."""
    rec = proposals.create(
        conn, vault_dir, type, actor="consolidator", run_id=run_id,
        source_refs=source_refs or ["notes/test.md"],
        justification="body", payload=payload,
    )
    proposals.advance(conn, vault_dir, rec["id"], "processing", actor="consolidator")
    target = "adopted" if proposals.authorization(type) == "adopted" else "awaiting_review"
    proposals.advance(conn, vault_dir, rec["id"], target, actor="consolidator")
    if target == "awaiting_review":
        proposals.advance(conn, vault_dir, rec["id"], "adopted", actor="human")
    return rec


# ── invariant 2: single live slot + GC never deletes the live one ─────────

def test_single_live_slot_and_gc(vault_dir, db_conn):
    """Write the same file N times: after each GC sweep exactly one live
    pre-image remains — the version before the LATEST write — and the
    superseded blobs are reclaimed (invariant 2 anchor)."""
    fp = vault_dir / "notes" / "test.md"
    versions = []
    for i in range(3):
        content = f"version {i}\n".encode()
        snap = snapshots.capture(db_conn, vault_dir, "notes/test.md", "update",
                                 "agent", run_id="t")
        assert snap is not None
        fp.write_bytes(content)
        snapshots.finalize(db_conn, snap, snapshots.hash_bytes(content))
        versions.append(content)
        # write #1 has nothing to reclaim; each later write supersedes the
        # previous live slot → exactly one GC candidate.
        assert snapshots.gc(db_conn, vault_dir) == (1 if i > 0 else 0)

    live = snapshots.live_preimage(db_conn, "notes/test.md")
    assert live is not None
    # live pre-image = bytes before the 3rd (latest) write = version 1's content
    assert _blob(vault_dir, live) == versions[1]
    # exactly one live row for the path; no superseded rows left after GC
    rows = db_conn.execute(
        "SELECT * FROM snapshots WHERE path = 'notes/test.md'"
    ).fetchall()
    assert [r["id"] for r in rows if r["live"] == 1] == [live["id"]]
    assert len(rows) == 1  # the two superseded rows were reclaimed

    # GC again: the live row must survive (invariant 2: GC never deletes live)
    snapshots.gc(db_conn, vault_dir)
    assert snapshots.live_preimage(db_conn, "notes/test.md") == live
    assert _blob(vault_dir, live) == versions[1]


def test_gc_never_reclaims_inflight_rows(vault_dir, db_conn):
    """Oracle gate P1-B1: a crash between capture and finalize leaves an
    in-flight row (live=0, finalized=0). GC must NOT reclaim it: it may be
    the file's only pre-image, and it must stay rollback-able. (Before the
    fix the ``live = 0`` predicate reclaimed it — the only pre-image was
    destroyed and the 30-day delete retention was breakable by crash+GC.)
    Residue is bounded: at most one row per crashed write."""
    snap = snapshots.capture(db_conn, vault_dir, "notes/test.md", "update",
                             "agent", run_id="t")
    assert snap is not None
    # The write never finalized (crash). A GC sweep must leave row + blob.
    assert snapshots.gc(db_conn, vault_dir) == 0
    row = db_conn.execute(
        "SELECT live, finalized FROM snapshots WHERE id = ?", (snap["id"],)
    ).fetchone()
    assert (row["live"], row["finalized"]) == (0, 0)
    assert _blob(vault_dir, snap) == SAMPLE  # pre-image bytes survived
    # It stays rollback-able: the call batch resolves to the in-flight row.
    batch = snapshots.by_call(db_conn, snap["call_id"])
    assert [r["id"] for r in batch] == [snap["id"]]


def test_create_op_has_no_preimage_bytes(vault_dir, db_conn):
    snap = snapshots.capture(db_conn, vault_dir, "notes/brand-new.md", "create",
                             "agent", run_id="t")
    assert snap is not None and snap["blob_name"] == "" and snap["pre_hash"] == ""
    (vault_dir / "notes" / "brand-new.md").write_bytes(b"new")
    snapshots.finalize(db_conn, snap, snapshots.hash_bytes(b"new"))
    live = snapshots.live_preimage(db_conn, "notes/brand-new.md")
    assert live is not None and live["op"] == "create"


# ── invariant 10: LLM-originated writes produce pre-image rows ────────────

def test_agent_write_doc_produces_preimage(vault_dir, db_conn):
    result = execute_tool(
        "write_doc", {"path": "notes/test.md", "content": "agent overwrite"},
        ToolContext(vault_dir=vault_dir))
    assert "Saved" in result
    live = snapshots.live_preimage(db_conn, "notes/test.md")
    assert live is not None and live["op"] == "update"
    assert live["actor"] == "agent"
    assert _blob(vault_dir, live) == SAMPLE  # pre-image = the full original text


def test_agent_edit_doc_produces_preimage(vault_dir, db_conn):
    (vault_dir / "notes" / "edit.md").write_bytes(b"# T\n\nOld text here.\n")
    result = execute_tool(
        "edit_doc",
        {"path": "notes/edit.md", "old_string": "Old text here.",
         "new_string": "New text here."},
        ToolContext(vault_dir=vault_dir))
    assert "Edited" in result
    live = snapshots.live_preimage(db_conn, "notes/edit.md")
    assert live is not None and live["op"] == "update"
    assert _blob(vault_dir, live) == b"# T\n\nOld text here.\n"


def test_agent_edit_doc_failure_does_not_take_the_slot(vault_dir, db_conn):
    """A failed edit (old_string absent) must NOT displace the file's live
    pre-image slot — the previous write's pre-image stays recoverable."""
    fp = vault_dir / "notes" / "slot.md"
    fp.write_bytes(b"v0")
    s0 = snapshots.capture(db_conn, vault_dir, "notes/slot.md", "update", "agent")
    fp.write_bytes(b"v1")
    snapshots.finalize(db_conn, s0, snapshots.hash_bytes(b"v1"))

    result = execute_tool(
        "edit_doc", {"path": "notes/slot.md", "old_string": "absent", "new_string": "x"},
        ToolContext(vault_dir=vault_dir))
    assert result.startswith("Error")

    live = snapshots.live_preimage(db_conn, "notes/slot.md")
    assert live["id"] == s0["id"]  # the slot is still the successful write's
    assert _blob(vault_dir, live) == b"v0"  # pre-image of the v1-write


def test_agent_move_docs_produces_preimage(vault_dir, db_conn):
    (vault_dir / "mv.md").write_bytes(b"move me")
    result = execute_tool(
        "move_docs", {"moves": [{"from_path": "mv.md", "to_path": "sub/mv.md"}]},
        ToolContext(vault_dir=vault_dir))
    assert "Moved 1 file(s)" in result
    live = snapshots.live_preimage(db_conn, "mv.md")
    assert live is not None and live["op"] == "move"
    assert live["new_path"] == "sub/mv.md"
    assert _blob(vault_dir, live) == b"move me"


def test_executor_summary_slot_overwrite_keeps_old_full_text(vault_dir, db_conn):
    """Invariant 10 (spec anchor): with the summary cache slot overwritten a
    second time, the pre-image row still holds the old full text readable
    back (LLM-unique output, forced pre-image)."""
    _adopted(vault_dir, db_conn, "summary", payload={"body": "first body"})
    assert run_executor(vault_dir, db_conn)["merged"] == 1
    out = vault_dir / "notes" / "test-summary.md"
    first = out.read_bytes()
    assert b"first body" in first

    _adopted(vault_dir, db_conn, "summary", payload={"body": "second body"})
    assert run_executor(vault_dir, db_conn)["merged"] == 1
    assert b"second body" in out.read_bytes()

    live = snapshots.live_preimage(db_conn, "notes/test-summary.md")
    assert live is not None and live["op"] == "update"
    assert live["actor"] == "executor"
    assert live["run_id"] == "batch-snap"
    assert _blob(vault_dir, live) == first  # old full text readable back

    # batch-by-run lookup works (work item 1: 按 run_id 查一批). After the
    # second cycle's retention sweep only the live row remains: the first
    # capture was superseded (single live slot, D18) and reclaimed.
    batch = snapshots.by_run(db_conn, "batch-snap")
    assert all(r["run_id"] == "batch-snap" for r in batch)
    assert len(batch) == 1
    assert batch[0]["id"] == live["id"]


def test_executor_entity_index_produces_preimage(vault_dir, db_conn):
    _adopted(vault_dir, db_conn, "entity_index",
             payload={"entity": "e1", "value": "v1"})
    assert run_executor(vault_dir, db_conn)["merged"] == 1
    live = snapshots.live_preimage(db_conn, "index/entities.md")
    assert live is not None and live["op"] == "create"  # first write: no pre-image bytes
    _adopted(vault_dir, db_conn, "entity_index",
             payload={"entity": "e1", "value": "v2"})
    assert run_executor(vault_dir, db_conn)["merged"] == 1
    live2 = snapshots.live_preimage(db_conn, "index/entities.md")
    assert live2["op"] == "update"
    assert b"## e1" in _blob(vault_dir, live2) and b"v1" in _blob(vault_dir, live2)


# ── rollback: verify-all-then-write-all + collision guard ─────────────────

def _seed_call(vault_dir, conn, paths: list[tuple[str, bytes]]):
    """Seed one call batch: capture every path, write the new bytes, finalize.
    Returns (call_id, {path: original_bytes})."""
    call_id = snapshots.new_call_id()
    originals = {}
    for rel, new_bytes in paths:
        fp = vault_dir / rel
        originals[rel] = fp.read_bytes()
        snap = snapshots.capture(conn, vault_dir, rel, "update", "agent",
                                 run_id="rb", call_id=call_id)
        fp.write_bytes(new_bytes)
        snapshots.finalize(conn, snap, snapshots.hash_bytes(new_bytes))
    return call_id, originals


def test_rollback_restores_bytes_when_untouched(vault_dir, db_conn):
    (vault_dir / "notes" / "two.md").write_bytes(b"# Two\n\norigin\n")
    call_id, originals = _seed_call(vault_dir, db_conn, [
        ("notes/test.md", b"changed A"),
        ("notes/two.md", b"changed B"),
    ])
    result = snapshots.rollback(db_conn, vault_dir, call_id)
    assert result["ok"] is True
    assert sorted(result["restored"]) == ["notes/test.md", "notes/two.md"]
    assert (vault_dir / "notes" / "test.md").read_bytes() == originals["notes/test.md"]
    assert (vault_dir / "notes" / "two.md").read_bytes() == originals["notes/two.md"]


def test_rollback_collision_refuses_whole_batch(vault_dir, db_conn):
    """Any file modified after the call → the whole batch is refused, the
    conflicting file is surfaced, and NOTHING is written (no blind writes)."""
    (vault_dir / "notes" / "two.md").write_bytes(b"# Two\n\norigin\n")
    call_id, originals = _seed_call(vault_dir, db_conn, [
        ("notes/test.md", b"changed A"),
        ("notes/two.md", b"changed B"),
    ])
    # a later write touches one file of the batch
    (vault_dir / "notes" / "two.md").write_bytes(b"changed B + later edit")

    result = snapshots.rollback(db_conn, vault_dir, call_id)
    assert result["ok"] is False
    assert any("two.md" in c for c in result["conflicts"])
    assert not any("test.md" in c for c in result["conflicts"])
    # verify-all-then-write-all: the clean file was NOT restored either
    assert (vault_dir / "notes" / "test.md").read_bytes() == b"changed A"
    assert (vault_dir / "notes" / "two.md").read_bytes() == b"changed B + later edit"


def test_rollback_deleted_after_call_is_conflict(vault_dir, db_conn):
    call_id, _ = _seed_call(vault_dir, db_conn, [("notes/test.md", b"changed A")])
    (vault_dir / "notes" / "test.md").unlink()
    result = snapshots.rollback(db_conn, vault_dir, call_id)
    assert result["ok"] is False
    assert any("test.md" in c for c in result["conflicts"])


def test_rollback_unknown_call(vault_dir, db_conn):
    result = snapshots.rollback(db_conn, vault_dir, "no-such-call")
    assert result["ok"] is False
    assert result["conflicts"]


# ── delete-class snapshots: full pre-image + 30-day retention ─────────────

def test_agent_delete_tool_captures_full_preimage(vault_dir, db_conn):
    (vault_dir / "notes" / "gone.md").write_bytes(b"precious content")
    result = execute_tool("delete_doc", {"path": "notes/gone.md"},
                          ToolContext(vault_dir=vault_dir))
    assert "Deleted" in result
    assert not (vault_dir / "notes" / "gone.md").exists()
    live = snapshots.live_preimage(db_conn, "notes/gone.md")
    assert live is not None and live["op"] == "delete"
    assert live["pre_hash"] == snapshots.hash_bytes(b"precious content")
    assert _blob(vault_dir, live) == b"precious content"


def test_delete_snapshot_retention_30_days(vault_dir, db_conn):
    """Delete-class snapshots are retained 30 days (injected time): GC
    keeps them at 29 days, reclaims them past the window."""
    (vault_dir / "notes" / "d.md").write_bytes(b"to be deleted")
    now = time.time()
    snap = snapshots.capture(db_conn, vault_dir, "notes/d.md", "delete",
                             "web", now=now)
    (vault_dir / "notes" / "d.md").unlink()
    snapshots.finalize(db_conn, snap, "")

    assert snapshots.gc(db_conn, vault_dir, now=now + 29 * 86400) == 0
    assert snapshots.live_preimage(db_conn, "notes/d.md") is not None

    assert snapshots.gc(db_conn, vault_dir, now=now + 31 * 86400) == 1
    assert snapshots.live_preimage(db_conn, "notes/d.md") is None
    assert not (vault_dir / ".sync" / "snapshots" / snap["blob_name"]).exists()


# ── Web UI delete: snapshot backstop (work item 6) ────────────────────────

def test_web_delete_single_captures_preimage(client, vault_dir, db_conn):
    (vault_dir / "notes" / "gone.md").write_bytes(b"web delete me")
    r = client.post("/api/docs/manage",
                    json={"action": "delete", "path": "notes/gone.md"})
    assert r.status_code == 200
    live = snapshots.live_preimage(db_conn, "notes/gone.md")
    assert live is not None and live["op"] == "delete" and live["actor"] == "web"
    assert _blob(vault_dir, live) == b"web delete me"


def test_web_delete_folder_captures_group(client, vault_dir, db_conn):
    sub = vault_dir / "notes" / "sub"
    (sub / "deep").mkdir(parents=True)
    (sub / "a.md").write_bytes(b"# A")
    (sub / "deep" / "b.md").write_bytes(b"# B")
    r = client.post("/api/docs/manage",
                    json={"action": "delete", "path": "notes/sub"})
    assert r.status_code == 200 and r.json()["deleted_count"] == 2
    la = snapshots.live_preimage(db_conn, "notes/sub/a.md")
    lb = snapshots.live_preimage(db_conn, "notes/sub/deep/b.md")
    assert la is not None and lb is not None
    assert la["call_id"] == lb["call_id"]  # one human action = one rollback group
    assert _blob(vault_dir, la) == b"# A" and _blob(vault_dir, lb) == b"# B"


# ── rollback endpoint (human-only, spec §3.1) ─────────────────────────────

def test_rollback_endpoint_success_and_conflict(client, vault_dir, db_conn):
    (vault_dir / "notes" / "two.md").write_bytes(b"# Two\n\norigin\n")
    call_id, originals = _seed_call(vault_dir, db_conn, [
        ("notes/test.md", b"changed A"),
        ("notes/two.md", b"changed B"),
    ])
    r = client.post("/api/memory/rollback", json={"call_id": call_id})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert (vault_dir / "notes" / "two.md").read_bytes() == b"# Two\n\norigin\n"

    # a second batch that will collide
    call2, _ = _seed_call(vault_dir, db_conn, [("notes/two.md", b"changed again")])
    (vault_dir / "notes" / "two.md").write_bytes(b"interfered")
    r2 = client.post("/api/memory/rollback", json={"call_id": call2})
    assert r2.status_code == 409
    body = r2.json()
    assert body["ok"] is False
    assert any("two.md" in c for c in body["conflicts"])
    assert (vault_dir / "notes" / "two.md").read_bytes() == b"interfered"


def test_rollback_endpoint_requires_call_id(client):
    r = client.post("/api/memory/rollback", json={})
    assert r.status_code == 400


def test_rollback_calls_endpoint_lists_call_groups(client, vault_dir, db_conn):
    """Oracle gate P1-B2: GET /api/memory/rollback-calls is how a human
    discovers the call_id for the rollback POST. Also pins the recent_calls
    SQL (GROUP_CONCAT separator char(10) — chr(10) raises
    OperationalError on this SQLite build and the function was untested)."""
    call_id, _ = _seed_call(vault_dir, db_conn, [("notes/test.md", b"changed A")])
    r = client.get("/api/memory/rollback-calls")
    assert r.status_code == 200
    calls = r.json()["calls"]
    mine = [c for c in calls if c["call_id"] == call_id]
    assert len(mine) == 1
    assert mine[0]["op"] == "update"
    assert mine[0]["files"] == ["notes/test.md"]


def test_rollback_not_an_agent_tool():
    """Rollback is human-exclusive: no tool named rollback exists in the
    registry (and bgagent cannot get it through any channel)."""
    from server.agent.tools import TOOL_REGISTRY

    assert not any("rollback" in name for name in TOOL_REGISTRY)


# ── bgagent channel: delete_doc hard-removal (spec §3.2, D3) ──────────────

def _runner_cfg(tmp_path: Path, tools=None):
    from server.agent.spec import ModelConfig
    from server.config import BackgroundAgentConfig

    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    target = (vault / "notes").resolve()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("p", encoding="utf-8")
    return BackgroundAgentConfig(
        name="t", alias="t", enabled=True, cron="0 3 * * *",
        target_dir=target, prompt=prompt.resolve(),
        report_dir=(target / ".reports" / "t").resolve(),
        max_rounds=2, timeout_s=10,
        model=ModelConfig(model_id="m"),
        tools=tools,
    )


class _FakeRuntime(Runtime):
    """Scripted no-op stream; records the agent for spec inspection."""

    def __init__(self):
        self.agent = None

    async def run(self, agent):
        self.agent = agent
        yield Event("round", {"n": 1})
        yield Event("done", {"reason": "final"})
def test_bgagent_default_tools_exclude_delete(tmp_path: Path):
    cfg = _runner_cfg(tmp_path)  # tools=None → full registry branch
    rt = _FakeRuntime()
    asyncio.run(run_once(cfg, tmp_path / "vault", runtime_cls=lambda: rt))
    names = [t.name for t in rt.agent.spec.tools]
    assert "delete_doc" not in names
    assert "read_doc" in names


def test_bgagent_whitelist_cannot_obtain_delete(tmp_path: Path):
    """Config explicitly lists delete_doc → still filtered (the registry is
    the mechanism, the whitelist is hygiene)."""
    cfg = _runner_cfg(tmp_path, tools=("read_doc", "delete_doc"))
    rt = _FakeRuntime()
    asyncio.run(run_once(cfg, tmp_path / "vault", runtime_cls=lambda: rt))
    names = [t.name for t in rt.agent.spec.tools]
    assert names == ["read_doc"]


def test_bgagent_delete_only_whitelist_fails_fast(tmp_path: Path):
    """A whitelist of ONLY delete_doc contradicts the constitution: the run
    fails with an explicit error instead of running bare with zero tools."""
    cfg = _runner_cfg(tmp_path, tools=("delete_doc",))
    with pytest.raises(ValueError, match="delete_doc"):
        asyncio.run(run_once(cfg, tmp_path / "vault", runtime_cls=lambda: _FakeRuntime()))


def test_desktop_channel_still_has_delete_doc():
    """The hard removal is bgagent-channel only: the desktop/Web agent gets
    the full registry (interactive delete = user-ask + snapshot backstop)."""
    from server.agent.tools import TOOL_REGISTRY

    assert "delete_doc" in TOOL_REGISTRY
