"""refs: prepare/apply split — rename-failure leaves referrers untouched
(F9); in-batch referrer redirect; decode skip; atomic writes (F10/F11).

Graph setup note (plan 2026-11-17 Task 4, verified against the fixtures):
the ``db_conn`` fixture's ``init_db`` runs ``init_graph`` — the edges table
EXISTS but is EMPTY, so ``_find_referrers`` takes its documented fallback
(full-vault md scan). No ``graph_extract_file`` seeding is needed; the
fallback path supplies the referrer set and the plan's assertions hold.
"""
import pathlib

from server.vault.fs.vault_ops import safe_rel_path
from server.vault.orchestration.refs import (
    apply_reference_updates,
    prepare_reference_updates,
)


def _seed(vault_dir):
    (vault_dir / "notes").mkdir(parents=True, exist_ok=True)
    (vault_dir / "notes" / "r1.md").write_bytes(b"links [[notes/moved.md]]")
    (vault_dir / "notes" / "moved.md").write_bytes(b"payload")


def test_failed_rename_leaves_referrers_untouched(vault_dir, db_conn, monkeypatch):
    _seed(vault_dir)
    real_rename = pathlib.Path.rename

    def fake_rename(self, target):
        if self.name == "moved.md":  # web move 的 src.rename(dst) 失败
            raise OSError("locked by obsidian")
        return real_rename(self, target)

    monkeypatch.setattr(pathlib.Path, "rename", fake_rename)
    moves = [("notes/moved.md", "notes/renamed.md")]
    prepared = prepare_reference_updates(db_conn, vault_dir, moves)
    try:
        (vault_dir / "notes" / "moved.md").rename(vault_dir / "notes" / "renamed.md")
    except OSError:
        pass  # 与 console.move/move_docs 的失败路径同构：不 apply
    monkeypatch.undo()
    assert b"[[notes/moved.md]]" in (vault_dir / "notes" / "r1.md").read_bytes()


def test_in_batch_referrer_redirect(vault_dir, db_conn):
    # r1 链接 r2，两者同批移动：prepare 读 r1 旧路径，apply 落 r1 新路径。
    (vault_dir / "notes").mkdir(parents=True, exist_ok=True)
    (vault_dir / "notes" / "r1.md").write_bytes(b"see [[notes/r2.md]]")
    (vault_dir / "notes" / "r2.md").write_bytes(b"payload")
    moves = [("notes/r1.md", "notes/dst/r1.md"), ("notes/r2.md", "notes/dst/r2.md")]
    prepared = prepare_reference_updates(db_conn, vault_dir, moves)
    assert prepared, "r1 must be a referrer of r2"
    for old, new in moves:  # rename 全部成功
        (vault_dir / new).parent.mkdir(parents=True, exist_ok=True)
        (vault_dir / old).rename(vault_dir / new)
    n = apply_reference_updates(
        vault_dir, prepared, redirect={o: n_ for o, n_ in moves})
    assert n == 1
    assert b"[[notes/dst/r2.md]]" in (vault_dir / "notes" / "dst" / "r1.md").read_bytes()


def test_prepare_skips_non_utf8_referrer(vault_dir, db_conn):
    _seed(vault_dir)
    (vault_dir / "notes" / "gbk.md").write_bytes(b"also [[notes/moved.md]] \xd6\xd0")
    prepared = prepare_reference_updates(db_conn, vault_dir,
                                         [("notes/moved.md", "notes/new.md")])
    rels = [rel for rel, _ in prepared]
    assert "notes/r1.md" in rels
    assert "notes/gbk.md" not in rels  # 跳过，不写回
    assert (vault_dir / "notes" / "gbk.md").read_bytes() == \
        b"also [[notes/moved.md]] \xd6\xd0"
