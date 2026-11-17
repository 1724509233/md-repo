"""Trash: server-side move semantics for deletion (P4-4, design 2026-11-10).

A deletion is a MOVE into ``<vault>/.trash/<batch>/`` — not an unlink:

- **Inbound** (:func:`move_to_trash`): meta.json is written first, then the
  files are renamed into the batch (original relative path preserved); the
  batch is immutable afterwards. A partial failure rolls the moved files
  back; if every rename-back succeeds the batch dir is removed (there is
  never a half-trash state), and if a rename-back fails the batch dir is
  KEPT on disk — it holds the only surviving copies, so it is never
  destroyed (review F12). The change_log row is booked with
  ``remove_links=False``:
  referrer content is left untouched so restore is fully reversible (links
  revive); the dead-link cleanup is deferred to the purge.
- **Restore** (:func:`restore`): rename back + ``restore`` row (5th
  canonical op, design §12.1 — a restored file is not a new file) +
  referrer graph re-extraction (backlinks revive).
- **Purge** (:func:`purge` / :func:`empty`): unlink the trash copy +
  ``remove_dead_links`` on the original path (referrer content cleanup) +
  drop the batch dir/meta when it is empty. No new change_log row — the
  deletion was already booked at inbound.

Batch name = UTC ``YYYYMMDDTHHMMSSZ-<8 hex>`` (uuid4 prefix): unique and
sortable. ``.trash/`` is excluded from every index/scan surface (see the
per-surface ``".trash" in parts`` checks across the codebase).
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ... import ledger
from ..db.graph_sql import _find_referrers
from ..fs.vault_ops import _iter_vault_md
from .indexer import graph_extract_file
from .refs import remove_dead_links

TRASH_DIR = ".trash"


class TrashError(Exception):
    """Trash operation failed (unknown batch/path, missing file, ...)."""


class TrashConflict(TrashError):
    """Restore target path already exists on disk (route → 409)."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_batch_name() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{ts}-{uuid.uuid4().hex[:8]}"


def trash_root(vault_dir: Path) -> Path:
    return Path(vault_dir) / TRASH_DIR


def batch_dir(vault_dir: Path, batch: str) -> Path:
    return trash_root(vault_dir) / batch


def batch_exists(vault_dir: Path, batch: str) -> bool:
    """True when the batch dir and its meta.json are on disk."""
    d = batch_dir(vault_dir, batch)
    return d.is_dir() and (d / "meta.json").is_file()


def read_meta(vault_dir: Path, batch: str) -> dict:
    """A batch's meta.json. Raises TrashError when the batch is absent."""
    if not batch_exists(vault_dir, batch):
        raise TrashError(f"unknown batch: {batch}")
    try:
        return json.loads(
            (batch_dir(vault_dir, batch) / "meta.json").read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise TrashError(f"unreadable meta for batch {batch}: {e}") from e


def list_trash(vault_dir: Path) -> list[dict]:
    """All batches, newest (deleted_at) first.

    File entries are filtered to what is actually on disk (a single-file
    restore/purge leaves the batch dir behind; the meta is the inbound
    record, the disk is the current state). Batches with no file left are
    not listed.
    """
    root = trash_root(vault_dir)
    items: list[dict] = []
    if root.is_dir():
        for d in sorted(root.iterdir()):
            meta_file = d / "meta.json"
            if not d.is_dir() or not meta_file.is_file():
                continue
            try:
                meta = json.loads(meta_file.read_text("utf-8"))
            except (OSError, json.JSONDecodeError):
                continue  # corrupt or foreign dir — not a batch
            files = [
                f for f in meta.get("files", []) if (d / f["path"]).is_file()
            ]
            if not files:
                continue  # fully restored/purged on disk — stale meta
            items.append({**meta, "files": files})
    items.sort(key=lambda m: m.get("deleted_at") or "", reverse=True)
    return items


def _drop_empty_root(vault_dir: Path) -> None:
    """Remove the .trash root when it is empty (best-effort)."""
    root = trash_root(vault_dir)
    if root.is_dir():
        try:
            root.rmdir()
        except OSError:
            pass  # not empty (another batch) — keep


def _selected(meta: dict, path: str | None) -> list[str]:
    """The batch's file paths, or the single ``path`` (must be a meta entry)."""
    paths = {f["path"] for f in meta.get("files", [])}
    if path is not None:
        if path not in paths:
            raise TrashError(f"path not in batch: {path}")
        return [path]
    return [f["path"] for f in meta.get("files", [])]


def move_to_trash(
    db_conn,
    vault_dir: Path,
    rel_paths: list[str],
    source: str,
    batch_id: str = "",
) -> str:
    """Move files into a new trash batch; book the inbound ledger rows.

    ``rel_paths``: vault-relative POSIX paths of existing .md files (a
    folder deletion = one batch holding all internal .md, same scope as
    the old recursive delete). Returns the batch name.

    ``db_conn`` may be None (degraded, no index DB): the disk move
    happens, the ledger rows are skipped.

    Raises TrashError on failure. When every rename-back succeeds the
    batch dir is removed (never a half-trash state); when a rename-back
    fails the batch dir is kept on disk — it holds the only surviving
    copies, so it is never destroyed — and the error reads
    "rollback incomplete — batch kept at <bdir>" (review F12).
    """
    if not rel_paths:
        raise TrashError("no files to move")
    batch = _new_batch_name()
    bdir = batch_dir(vault_dir, batch)
    bdir.mkdir(parents=True)
    moved: list[tuple[Path, Path]] = []
    try:
        # 1. Hash + size BEFORE the move — meta is the pre-image record
        #    (the sync lane uses the hash for stale-copy detection).
        entries = []
        for rel in rel_paths:
            fp = vault_dir / rel
            if not fp.is_file():
                raise TrashError(f"not found: {rel}")
            data = fp.read_bytes()
            entries.append({
                "path": rel,
                "size": len(data),
                "hash": hashlib.sha256(data).hexdigest(),
            })
        # 2. meta first, files after — the batch is immutable once complete.
        meta = {
            "batch": batch,
            "deleted_at": _now_iso(),
            "source": source,
            "files": entries,
        }
        (bdir / "meta.json").write_text(json.dumps(meta, indent=2), "utf-8")
        for rel in rel_paths:
            fp = vault_dir / rel
            dest = bdir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            fp.rename(dest)
            moved.append((fp, dest))
    except (OSError, TrashError) as e:
        rollback_failed = False
        for src, dest in moved:
            try:
                dest.rename(src)
            except OSError:
                rollback_failed = True  # best-effort；绝不为它销毁 batch
        if rollback_failed:
            # The batch holds the only surviving copies — keep it on disk
            # for manual recovery; destroying it would lose user content.
            raise TrashError(
                f"move to trash failed: {e}; rollback incomplete — "
                f"batch kept at {bdir}"
            ) from e
        shutil.rmtree(bdir, ignore_errors=True)
        _drop_empty_root(vault_dir)
        raise TrashError(f"move to trash failed: {e}") from e
    # 3. Ledger: one inbound row per file. remove_links=False — referrer
    #    content stays untouched so restore is reversible (design §2).
    #    pre_hash = the pre-delete SHA-256 (spec 2026-11-15 §2: every
    #    deletion entry point books it at the deletion point; the sync
    #    lane derives its stale-copy map from these rows).
    if db_conn is not None:
        for rel, entry in zip(rel_paths, entries):
            ledger.record(db_conn, vault_dir, rel, "deleted", source,
                          batch_id=batch_id, remove_links=False,
                          pre_hash=entry["hash"])
    return batch


def _content_referrers(vault_dir: Path, rel: str) -> list[str]:
    """Vault files whose content contains a wikilink to ``rel``.

    Same target patterns as ``remove_dead_links``: full path, path without
    .md, and the short stem when the filename is unique in the vault
    (``.trash/`` copies are not live files).
    """
    filename = Path(rel).name
    stem = Path(rel).stem
    no_ext = rel[:-3] if rel.endswith(".md") else rel
    short_unique = True
    for f in vault_dir.rglob(filename):
        if ".sync" in f.parts or ".obsidian" in f.parts or ".trash" in f.parts:
            continue
        try:
            if f.is_file() and f.relative_to(vault_dir).as_posix() != rel:
                short_unique = False
                break
        except ValueError:
            continue
    patterns = [re.escape(rel), re.escape(no_ext)]
    if short_unique:
        patterns.append(re.escape(stem))
    rx = re.compile(r"\[\[(" + "|".join(patterns) + r")(\|[^\]]+)?\]\]")
    out = []
    for f in _iter_vault_md(vault_dir):
        r = f.relative_to(vault_dir).as_posix()
        if r == rel:
            continue
        try:
            content = f.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            continue
        if rx.search(content):
            out.append(r)
    return out


def _referrers(conn, vault_dir: Path, rel: str) -> list[str]:
    """Referrer paths for ``rel``: edges lookup first, content-scan fallback.

    After a trash inbound, ``graph_delete_file`` removed the
    referrer→target edges, so the edges lookup alone finds nothing; the
    O(vault) content scan is the design's accepted fallback (§11-4:
    restore/purge are low-frequency user actions).
    """
    referrers = [r for r in _find_referrers(conn, vault_dir, rel) if r != rel]
    if referrers:
        return referrers
    return _content_referrers(vault_dir, rel)


def restore(
    db_conn,
    vault_dir: Path,
    batch: str,
    path: str | None = None,
    source: str = "web",
) -> list[str]:
    """Restore a whole batch, or one file of it, to its original path.

    Raises TrashConflict when any original path already exists (route →
    409) and TrashError for an unknown batch/path or a missing trash file.
    Returns the restored original paths.
    """
    meta = read_meta(vault_dir, batch)
    rels = _selected(meta, path)
    # Conflict check BEFORE any rename — atomic decision, no partial restore.
    for rel in rels:
        if (vault_dir / rel).exists():
            raise TrashConflict(f"original path exists: {rel}")
    bdir = batch_dir(vault_dir, batch)
    restored: list[str] = []
    for rel in rels:
        src = bdir / rel
        if not src.is_file():
            raise TrashError(f"trash file missing: {rel}")
        dst = vault_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        restored.append(rel)
    if db_conn is not None:
        for rel in restored:
            ledger.record(db_conn, vault_dir, rel, "restore", source)
        # Referrer graph re-extraction: the inbound graph_delete_file
        # removed the referrer→target edges, so re-extract the referrers
        # to revive the backlinks (design §2: links revive on restore).
        for rel in restored:
            for ref in _referrers(db_conn, vault_dir, rel):
                graph_extract_file(db_conn, vault_dir, ref)
    # Drop the batch when no tracked file remains (whole-batch restore, or
    # the last file of a batch restored individually).
    remaining = [
        f for f in meta.get("files", []) if (bdir / f["path"]).is_file()
    ]
    if not remaining:
        shutil.rmtree(bdir, ignore_errors=True)
        _drop_empty_root(vault_dir)
    return restored


def purge(
    db_conn,
    vault_dir: Path,
    batch: str,
    path: str | None = None,
) -> int:
    """Permanently delete a batch, or one file of it, from the trash.

    = unlink the trash copy + ``remove_dead_links`` on the original path
    (referrer content cleanup — the deferred half of the inbound
    remove_links=False) + drop the batch dir/meta when no tracked file
    remains. No new change_log row — the deletion was already booked at
    inbound (design §2). Returns the number of files purged.
    """
    meta = read_meta(vault_dir, batch)
    rels = _selected(meta, path)
    bdir = batch_dir(vault_dir, batch)
    for rel in rels:
        (bdir / rel).unlink(missing_ok=True)
        # The referrer→target graph edges were removed at inbound, so the
        # edges-based lookup inside remove_dead_links finds nothing — pass
        # the referrer list (edges lookup first, content-scan fallback)
        # explicitly.
        remove_dead_links(
            db_conn, vault_dir, rel, src_paths=_referrers(db_conn, vault_dir, rel))
    remaining = [
        f for f in meta.get("files", []) if (bdir / f["path"]).is_file()
    ]
    if not remaining:
        shutil.rmtree(bdir, ignore_errors=True)
        _drop_empty_root(vault_dir)
    return len(rels)


def empty(db_conn, vault_dir: Path) -> int:
    """Purge every batch. Returns the total number of files purged."""
    total = 0
    for meta in list_trash(vault_dir):
        total += purge(db_conn, vault_dir, meta["batch"])
    return total
