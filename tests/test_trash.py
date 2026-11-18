"""Trash core + API tests (P4-4, design 2026-11-10).

Covers the design §9 backend list: inbound (disk layout / meta / files
table / segments / change_log / referrer content untouched), restore
(whole batch / single file / 409 / graph re-extraction), purge
(remove_dead_links / no new change_log row), empty, partial-failure
rollback, API batch validation, safe_rel_path first-segment rejection,
and the .trash exclusion surfaces.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from server.vault.watcher import _full_scan, _norm_rel
from server.vault.db.changes import count_changes_sources, get_changes
from server.vault.db.file_index import get_all_files
from server.vault.db.graph_sql import graph_get_backlinks
from server.vault.orchestration import trash
from server.vault.orchestration.indexer import (
    ensure_indexed,
    graph_extract_file,
    index_segments,
)
from server.vault.fs.vault_ops import _iter_vault_md, safe_rel_path

SAMPLE = b"# Test note\n\nSample content.\n"


def _file_paths(db_conn) -> set[str]:
    return {f["path"] for f in get_all_files(db_conn)}


def _segment_paths(db_conn) -> set[str]:
    return {
        r[0] for r in db_conn.execute("SELECT DISTINCT path FROM segments").fetchall()
    }


# ── inbound: move_to_trash ─────────────────────────────────────────────────

def test_move_to_trash_file_disk_layout_and_meta(vault_dir, db_conn):
    batch = trash.move_to_trash(db_conn, vault_dir, ["notes/test.md"], "web")
    # Batch name: UTC timestamp + 8 hex.
    ts, hex8 = batch.rsplit("-", 1)
    assert len(hex8) == 8
    int(hex8, 16)  # valid hex
    assert ts.endswith("Z") and "T" in ts
    # Original relative path preserved inside the batch.
    bdir = vault_dir / ".trash" / batch
    assert (bdir / "notes" / "test.md").is_file()
    assert (bdir / "notes" / "test.md").read_bytes() == SAMPLE
    assert not (vault_dir / "notes" / "test.md").exists()
    # meta.json: batch / deleted_at / source / files[{path, size, hash}].
    meta = json.loads((bdir / "meta.json").read_text("utf-8"))
    assert meta["batch"] == batch
    assert meta["source"] == "web"
    assert meta["deleted_at"].endswith("Z")
    assert meta["files"] == [{
        "path": "notes/test.md",
        "size": len(SAMPLE),
        "hash": hashlib.sha256(SAMPLE).hexdigest(),
    }]


def test_move_to_trash_books_ledger_and_cleans_tables(vault_dir, db_conn):
    index_segments(db_conn, vault_dir, "notes/test.md")
    assert "notes/test.md" in _segment_paths(db_conn)
    trash.move_to_trash(db_conn, vault_dir, ["notes/test.md"], "web")
    # files row removed, segments cleared, one change_log row (op=delete).
    assert "notes/test.md" not in _file_paths(db_conn)
    assert "notes/test.md" not in _segment_paths(db_conn)
    rows = [c for c in get_changes(db_conn) if c["path"] == "notes/test.md"]
    assert len(rows) == 1
    assert rows[0]["op"] == "delete"
    assert rows[0]["source"] == "web"
    # pre_hash = the pre-delete SHA-256 (actor separation 2026-11-15 §2:
    # every deletion entry point books it; the sync lane derives its
    # stale-copy map from these rows).
    assert rows[0]["pre_hash"] == hashlib.sha256(SAMPLE).hexdigest()


def test_move_to_trash_folder_one_batch(vault_dir, db_conn):
    sub = vault_dir / "notes" / "sub"
    (sub / "deep").mkdir(parents=True)
    (sub / "a.md").write_bytes(b"# A")
    (sub / "deep" / "b.md").write_bytes(b"# B")
    batch = trash.move_to_trash(
        db_conn, vault_dir, ["notes/sub/a.md", "notes/sub/deep/b.md"], "web")
    meta = trash.read_meta(vault_dir, batch)
    assert {f["path"] for f in meta["files"]} == {
        "notes/sub/a.md", "notes/sub/deep/b.md"}
    assert (vault_dir / ".trash" / batch / "notes" / "sub" / "deep" / "b.md").is_file()


def test_move_to_trash_referrer_content_untouched(vault_dir, db_conn):
    # remove_links=False: referrer content stays as-is (link temporarily
    # dead — restore is fully reversible, design §2).
    (vault_dir / "notes" / "target.md").write_bytes(b"# Target")
    (vault_dir / "notes" / "ref.md").write_bytes(b"See [[notes/target]] for info.")
    trash.move_to_trash(db_conn, vault_dir, ["notes/target.md"], "web")
    assert (vault_dir / "notes" / "ref.md").read_bytes() == b"See [[notes/target]] for info."


def test_move_to_trash_missing_file_raises(vault_dir, db_conn):
    with pytest.raises(trash.TrashError):
        trash.move_to_trash(db_conn, vault_dir, ["nope.md"], "web")
    assert not (vault_dir / ".trash").exists()


def test_partial_move_failure_rolls_back(vault_dir, db_conn, monkeypatch):
    # Partial failure → moved files renamed back + batch dir removed:
    # there is never a half-trash state (design §5.1).
    (vault_dir / "a.md").write_bytes(b"A")
    (vault_dir / "b.md").write_bytes(b"B")
    real_rename = Path.rename
    state = {"n": 0}

    def flaky(self, target):
        state["n"] += 1
        if state["n"] == 2:  # the second move fails
            raise OSError("simulated rename failure")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", flaky)
    with pytest.raises(trash.TrashError):
        trash.move_to_trash(db_conn, vault_dir, ["a.md", "b.md"], "web")
    assert (vault_dir / "a.md").is_file()
    assert (vault_dir / "b.md").is_file()
    assert not (vault_dir / ".trash").exists()
    assert get_changes(db_conn) == []


def test_move_to_trash_double_failure_keeps_batch(vault_dir, db_conn, monkeypatch):
    # F12：forward 第 2 个 rename 失败 + 第 1 个文件的 rename-back 也失败
    # → 旧行为 rmtree 销毁 batch 里仅存的唯一副本；新行为保留 batch 并抛错。
    import pathlib
    from server.vault.orchestration import trash

    (vault_dir / "notes").mkdir(exist_ok=True)
    f1 = vault_dir / "notes" / "one.md"
    f2 = vault_dir / "notes" / "two.md"
    f1.write_bytes(b"one")
    f2.write_bytes(b"two")
    real_rename = pathlib.Path.rename

    state = {"forward": 0}

    def fake_rename(self, target):
        if str(target).startswith(str(trash.trash_root(vault_dir))):
            state["forward"] += 1
            if state["forward"] >= 2:  # forward 第 2 个失败
                raise OSError("disk full (forward #2)")
            return real_rename(self, target)
        # rename-back 阶段：one.md 的回滚也失败
        if self.name == "one.md":
            raise OSError("disk full (rename-back)")
        return real_rename(self, target)

    monkeypatch.setattr(pathlib.Path, "rename", fake_rename)
    try:
        exc = None
        try:
            trash.move_to_trash(db_conn, vault_dir,
                                ["notes/one.md", "notes/two.md"], "web")
        except trash.TrashError as e:
            exc = e
        assert exc is not None and "rollback incomplete" in str(exc)
        batches = list(trash.trash_root(vault_dir).iterdir())
        assert len(batches) == 1
        assert (batches[0] / "notes" / "one.md").is_file()  # 唯一副本幸存
        assert (vault_dir / "notes" / "two.md").is_file()  # 未搬走的原地幸存
    finally:
        monkeypatch.undo()


# ── restore ────────────────────────────────────────────────────────────────

def test_restore_whole_batch(vault_dir, db_conn):
    (vault_dir / "notes" / "two.md").write_bytes(b"# Two")
    batch = trash.move_to_trash(
        db_conn, vault_dir, ["notes/test.md", "notes/two.md"], "web")
    restored = trash.restore(db_conn, vault_dir, batch)
    assert sorted(restored) == ["notes/test.md", "notes/two.md"]
    assert (vault_dir / "notes" / "test.md").read_bytes() == SAMPLE
    assert (vault_dir / "notes" / "two.md").read_bytes() == b"# Two"
    # restore rows (5th canonical op, design §12.1) + files rows back.
    rows = [c for c in get_changes(db_conn) if c["op"] == "restore"]
    assert {r["path"] for r in rows} == {"notes/test.md", "notes/two.md"}
    assert {"notes/test.md", "notes/two.md"} <= _file_paths(db_conn)
    # Batch dir dropped (no tracked file remains).
    assert not (vault_dir / ".trash" / batch).exists()


def test_restore_does_not_arm_bgagent(vault_dir, db_conn):
    # design §12.1: restore is NOT op=create — the bgagent arm surface
    # (count_changes_sources ops=("create",)) must not see restore rows.
    batch = trash.move_to_trash(db_conn, vault_dir, ["notes/test.md"], "web")
    trash.restore(db_conn, vault_dir, batch)
    assert count_changes_sources(db_conn, ops=("create",)) == 0


def test_restore_single_file(vault_dir, db_conn):
    (vault_dir / "notes" / "two.md").write_bytes(b"# Two")
    batch = trash.move_to_trash(
        db_conn, vault_dir, ["notes/test.md", "notes/two.md"], "web")
    restored = trash.restore(db_conn, vault_dir, batch, path="notes/two.md")
    assert restored == ["notes/two.md"]
    assert (vault_dir / "notes" / "two.md").is_file()
    # The other file stays in the trash; the batch survives.
    assert not (vault_dir / "notes" / "test.md").exists()
    assert (vault_dir / ".trash" / batch / "notes" / "test.md").is_file()
    assert trash.batch_exists(vault_dir, batch)


def test_restore_conflict_raises_and_moves_nothing(vault_dir, db_conn):
    batch = trash.move_to_trash(db_conn, vault_dir, ["notes/test.md"], "web")
    (vault_dir / "notes" / "test.md").write_bytes(b"recreated")
    with pytest.raises(trash.TrashConflict):
        trash.restore(db_conn, vault_dir, batch)
    # Nothing moved: trash copy intact, recreated file untouched.
    assert (vault_dir / ".trash" / batch / "notes" / "test.md").is_file()
    assert (vault_dir / "notes" / "test.md").read_bytes() == b"recreated"


def test_restore_unknown_batch_or_path(vault_dir, db_conn):
    with pytest.raises(trash.TrashError):
        trash.restore(db_conn, vault_dir, "no-such-batch")
    batch = trash.move_to_trash(db_conn, vault_dir, ["notes/test.md"], "web")
    with pytest.raises(trash.TrashError):
        trash.restore(db_conn, vault_dir, batch, path="other.md")


def test_restore_revives_backlinks_edges_empty_fallback(vault_dir, db_conn):
    # Minimal vault: after inbound the edges table is empty, so
    # _find_referrers takes its full-scan fallback and the referrer is
    # re-extracted → the backlink revives.
    (vault_dir / "notes" / "target.md").write_bytes(b"# Target")
    (vault_dir / "notes" / "ref.md").write_bytes(b"See [[notes/target]] for info.")
    graph_extract_file(db_conn, vault_dir, "notes/ref.md")
    graph_extract_file(db_conn, vault_dir, "notes/target.md")
    assert [b["path"] for b in graph_get_backlinks(db_conn, "notes/target.md")] == ["notes/ref.md"]

    batch = trash.move_to_trash(db_conn, vault_dir, ["notes/target.md"], "web")
    assert graph_get_backlinks(db_conn, "notes/target.md") == []

    trash.restore(db_conn, vault_dir, batch)
    assert [b["path"] for b in graph_get_backlinks(db_conn, "notes/target.md")] == ["notes/ref.md"]


def test_restore_revives_backlinks_content_scan(vault_dir, db_conn):
    # General case: the edges table is NOT empty after inbound (other.md
    # keeps a tag edge), so the edges lookup finds nothing (inbound removed
    # the referrer→target edges) and the content-scan fallback must find
    # the referrer (design §11-4: O(vault) scan, low-frequency op).
    (vault_dir / "notes" / "target.md").write_bytes(b"# Target")
    (vault_dir / "notes" / "ref.md").write_bytes(b"See [[notes/target]].")
    (vault_dir / "other.md").write_bytes(b"---\ntags: [keep]\n---\n# Other\n")
    graph_extract_file(db_conn, vault_dir, "notes/ref.md")
    graph_extract_file(db_conn, vault_dir, "notes/target.md")
    graph_extract_file(db_conn, vault_dir, "other.md")
    assert db_conn.execute("SELECT count(*) FROM edges").fetchone()[0] > 0

    batch = trash.move_to_trash(db_conn, vault_dir, ["notes/target.md"], "web")
    # Inbound removed the referrer→target edge (edges table still non-empty).
    assert graph_get_backlinks(db_conn, "notes/target.md") == []

    trash.restore(db_conn, vault_dir, batch)
    assert [b["path"] for b in graph_get_backlinks(db_conn, "notes/target.md")] == ["notes/ref.md"]


# ── purge / empty ──────────────────────────────────────────────────────────

def test_purge_cleans_referrer_content_and_no_new_change_row(vault_dir, db_conn):
    (vault_dir / "notes" / "target.md").write_bytes(b"# Target")
    (vault_dir / "notes" / "ref.md").write_bytes(b"See [[notes/target]] for info.")
    batch = trash.move_to_trash(db_conn, vault_dir, ["notes/target.md"], "web")
    before = len(get_changes(db_conn))
    n = trash.purge(db_conn, vault_dir, batch)
    assert n == 1
    # Trash file unlinked, batch dir gone.
    assert not (vault_dir / ".trash" / batch).exists()
    # Referrer content cleaned (the deferred half of remove_links=False).
    assert "[[notes/target]]" not in (vault_dir / "notes" / "ref.md").read_bytes().decode()
    # No new change_log row — the deletion was booked at inbound.
    assert len(get_changes(db_conn)) == before


def test_purge_single_file_keeps_batch(vault_dir, db_conn):
    (vault_dir / "notes" / "two.md").write_bytes(b"# Two")
    batch = trash.move_to_trash(
        db_conn, vault_dir, ["notes/test.md", "notes/two.md"], "web")
    n = trash.purge(db_conn, vault_dir, batch, path="notes/test.md")
    assert n == 1
    # Batch survives with the other file; the listing reflects disk state.
    assert trash.batch_exists(vault_dir, batch)
    assert not (vault_dir / ".trash" / batch / "notes" / "test.md").exists()
    assert (vault_dir / ".trash" / batch / "notes" / "two.md").is_file()
    items = trash.list_trash(vault_dir)
    assert len(items) == 1
    assert {f["path"] for f in items[0]["files"]} == {"notes/two.md"}


def test_empty_purges_all(vault_dir, db_conn):
    (vault_dir / "notes" / "two.md").write_bytes(b"# Two")
    trash.move_to_trash(db_conn, vault_dir, ["notes/test.md"], "web")
    trash.move_to_trash(db_conn, vault_dir, ["notes/two.md"], "web")
    n = trash.empty(db_conn, vault_dir)
    assert n == 2
    assert not (vault_dir / ".trash").exists()
    assert trash.list_trash(vault_dir) == []


def test_list_trash_newest_first(vault_dir, monkeypatch):
    stamps = iter(["2026-11-10T12:00:00Z", "2026-11-10T12:05:00Z"])
    monkeypatch.setattr(trash, "_now_iso", lambda: next(stamps))
    b1 = trash.move_to_trash(None, vault_dir, ["notes/test.md"], "web")
    (vault_dir / "notes" / "two.md").write_bytes(b"# Two")
    b2 = trash.move_to_trash(None, vault_dir, ["notes/two.md"], "web")
    items = trash.list_trash(vault_dir)
    assert [m["batch"] for m in items] == [b2, b1]


# ── API ────────────────────────────────────────────────────────────────────

def test_api_trash_list(client, vault_dir, db_conn):
    r = client.post("/api/docs/manage", json={"action": "delete", "path": "notes/test.md"})
    assert r.status_code == 200
    batch = r.json()["batch"]
    r = client.get("/api/trash")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["batch"] == batch
    assert items[0]["source"] == "web"
    assert items[0]["files"] == [{"path": "notes/test.md", "size": len(SAMPLE)}]
    # hash is internal (sync lane) — not exposed.
    assert "hash" not in items[0]["files"][0]


def test_api_trash_empty_list(client):
    r = client.get("/api/trash")
    assert r.status_code == 200
    assert r.json() == {"items": []}


def test_api_trash_restore(client, vault_dir, db_conn):
    r = client.post("/api/docs/manage", json={"action": "delete", "path": "notes/test.md"})
    batch = r.json()["batch"]
    r = client.post("/api/trash", json={"action": "restore", "batch": batch})
    assert r.status_code == 200
    assert r.json()["restored"] == ["notes/test.md"]
    assert (vault_dir / "notes" / "test.md").read_bytes() == SAMPLE


def test_api_trash_restore_conflict_409(client, vault_dir, db_conn):
    r = client.post("/api/docs/manage", json={"action": "delete", "path": "notes/test.md"})
    batch = r.json()["batch"]
    (vault_dir / "notes" / "test.md").write_bytes(b"recreated")
    r = client.post("/api/trash", json={"action": "restore", "batch": batch})
    assert r.status_code == 409


def test_api_trash_batch_not_found_404(client):
    r = client.post("/api/trash", json={"action": "restore", "batch": "nope"})
    assert r.status_code == 404
    r = client.post("/api/trash", json={"action": "purge", "batch": "nope"})
    assert r.status_code == 404


def test_api_trash_path_not_in_meta_400(client, vault_dir, db_conn):
    r = client.post("/api/docs/manage", json={"action": "delete", "path": "notes/test.md"})
    batch = r.json()["batch"]
    r = client.post(
        "/api/trash", json={"action": "restore", "batch": batch, "path": "other.md"})
    assert r.status_code == 400


def test_api_trash_purge_and_empty(client, vault_dir, db_conn):
    r = client.post("/api/docs/manage", json={"action": "delete", "path": "notes/test.md"})
    batch = r.json()["batch"]
    r = client.post("/api/trash", json={"action": "purge", "batch": batch})
    assert r.status_code == 200
    assert r.json()["purged"] == 1
    assert not (vault_dir / ".trash" / batch).exists()
    assert client.get("/api/trash").json() == {"items": []}
    # empty on an empty trash is a no-op
    r = client.post("/api/trash", json={"action": "empty"})
    assert r.status_code == 200 and r.json()["purged"] == 0


def test_api_trash_unknown_action_400(client):
    r = client.post("/api/trash", json={"action": "nuke"})
    assert r.status_code == 400


# ── safe_rel_path: first-segment rejection (P4-4 + bug 0eac2e40) ───────────

def test_safe_rel_path_rejects_trash_and_sync_first_segment():
    assert safe_rel_path(".trash/20261110T120000Z-a1b2c3d4/notes/a.md") is None
    assert safe_rel_path(".trash") is None
    assert safe_rel_path(".sync/index.db") is None
    assert safe_rel_path(".sync") is None
    # Deeper same-named dirs are user content — still allowed.
    assert safe_rel_path("notes/.trash/a.md") == Path("notes/.trash/a.md")
    assert safe_rel_path("notes/.sync/a.md") == Path("notes/.sync/a.md")
    # Ordinary paths unaffected.
    assert safe_rel_path("notes/a.md") == Path("notes/a.md")


def test_safe_rel_path_rejects_drive_qualified_paths():
    # F5：Windows 盘符限定路径在 win32 上会丢弃 join 基逃出 vault
    # （drive-absolute "C:/x" 与 drive-relative "c:x.md" 皆然）。
    # 纯字符串判定，POSIX 上同样可测。
    for bad in ("C:/Users/x/evil.md", "c:/notes/a.md", "C:\\x.md", "C:", "z:/a/b.md", "c:x.md",
                "\\\\server\\share.md", "\\\\?\\C:\\x.md"):  # 反斜杠 UNC/扩展长度（归一先于检查，既有行为钉住）
        assert safe_rel_path(bad) is None, bad
    # 中间冒号不误伤：仅拒绝起始的 字母+冒号。
    assert safe_rel_path("notes/c:x.md") is not None
    assert safe_rel_path("my-c:.md") is not None


def test_web_routes_cannot_touch_trash(client, vault_dir, db_conn):
    # Upload into .trash via the upload catchall → 400.
    r = client.post(
        "/api/docs/.trash/evil.md",
        files={"file": ("x.md", b"evil")},
    )
    assert r.status_code == 400
    # Read a trashed file via get_doc → 400.
    r = client.post("/api/docs/manage", json={"action": "delete", "path": "notes/test.md"})
    batch = r.json()["batch"]
    r = client.get(f"/api/docs/.trash/{batch}/notes/test.md")
    assert r.status_code == 400


# ── exclusion surfaces (.trash) ────────────────────────────────────────────

def _seed_trash(vault_dir, db_conn):
    # One live file + one trashed file (notes/test.md). The live file needs
    # body text — a heading-only document segments to zero rows.
    (vault_dir / "notes" / "live.md").write_bytes(b"# Live\n\nBody text.\n")
    batch = trash.move_to_trash(db_conn, vault_dir, ["notes/test.md"], "web")
    return batch


def test_iter_vault_md_excludes_trash(vault_dir, db_conn):
    _seed_trash(vault_dir, db_conn)
    paths = {
        f.relative_to(vault_dir).as_posix() for f in _iter_vault_md(vault_dir)
    }
    assert ".trash" not in {p.split("/")[0] for p in paths}
    assert "notes/live.md" in paths


def test_full_scan_excludes_trash(vault_dir, db_conn):
    _seed_trash(vault_dir, db_conn)
    snap = _full_scan(vault_dir)
    assert not any(p.startswith(".trash/") for p in snap)
    assert "notes/live.md" in snap


def test_norm_rel_excludes_trash(vault_dir):
    (vault_dir / ".trash" / "b" / "notes").mkdir(parents=True)
    (vault_dir / ".trash" / "b" / "notes" / "x.md").write_bytes(b"x")
    assert _norm_rel(str(vault_dir / ".trash" / "b" / "notes" / "x.md"), vault_dir) is None
    # Ordinary paths still pass.
    assert _norm_rel(str(vault_dir / "notes" / "test.md"), vault_dir) == "notes/test.md"


def test_list_files_tool_excludes_trash(vault_dir, db_conn):
    from server.agent import ToolContext, execute_tool

    _seed_trash(vault_dir, db_conn)
    result = execute_tool("list_files", {"dir": ""}, ToolContext(vault_dir=vault_dir))
    assert ".trash" not in result
    assert "notes/live.md" in result


def test_ensure_indexed_rebuild_excludes_trash(vault_dir, db_conn):
    _seed_trash(vault_dir, db_conn)
    ensure_indexed(db_conn, vault_dir)
    assert "notes/live.md" in _segment_paths(db_conn)
    assert not any(p.startswith(".trash/") for p in _segment_paths(db_conn))


def test_delete_doc_result_wording(vault_dir, db_conn):
    from server.agent import ToolContext, execute_tool

    result = execute_tool("delete_doc", {"path": "notes/test.md"},
                          ToolContext(vault_dir=vault_dir))
    assert "Moved to trash" in result
    assert (vault_dir / "notes" / "test.md").exists() is False


# ── sync lane: stale-copy detection (design §6) ─────────────────────────────


def test_api_sync_stale_copy_after_trash_delete(client, vault_dir, db_conn):
    # Server moved notes/test.md to the trash; the client still holds the
    # identical local copy. The sync must report it in to_delete, not
    # to_push (pushing would resurrect the file).
    pre_hash = hashlib.sha256(SAMPLE).hexdigest()
    trash.move_to_trash(db_conn, vault_dir, ["notes/test.md"], "web")
    r = client.post(
        "/api/sync",
        json={
            "device_name": "mac",
            "local_files": {"notes/test.md": pre_hash},
            "logs": [],
            "last_sync": 1.0,  # truthy → change window is consulted
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["to_delete"] == ["notes/test.md"]
    assert body["to_push"] == []
    assert body["to_pull"] == []
    assert body["conflicts"] == []


def test_api_sync_local_rewrite_after_trash_delete(client, vault_dir, db_conn):
    # Same deletion, but the client's copy was rewritten locally after the
    # server-side deletion (hash differs from the pre-delete hash) — a
    # genuine new file, pushed normally.
    trash.move_to_trash(db_conn, vault_dir, ["notes/test.md"], "web")
    r = client.post(
        "/api/sync",
        json={
            "device_name": "mac",
            "local_files": {"notes/test.md": "rewritten-hash"},
            "logs": [],
            "last_sync": 1.0,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["to_push"] == ["notes/test.md"]
    assert body["to_delete"] == []
