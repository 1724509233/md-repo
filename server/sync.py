"""Sync comparison logic."""
from __future__ import annotations


def compare_files(
    local: dict[str, str],
    server: dict[str, str],
    recent_moves: list[dict] | None = None,
    recent_deletes: list[dict] | None = None,
    deleted_hashes: dict[str, str] | None = None,
) -> dict:
    """Compare local and server file manifests.

    Args:
        local: mapping of relative path -> sha256 hash (client side)
        server: mapping of relative path -> sha256 hash (server side)
        recent_moves: optional list of change_log moved events since the
            client's last sync. Each dict has keys ``from_path``, ``path``,
            ``ts``. Used to detect renames so the client deletes its stale
            local copy instead of pushing it back (which would recreate the
            old path on the server).
        recent_deletes: optional list of change_log deleted events since
            the client's last sync. Each dict has keys ``path``, ``ts``.
            A to_push path that was deleted on the server is a stale local
            copy — pushing it back would resurrect the file.
        deleted_hashes: optional mapping of original path -> pre-delete
            sha256 hash (from the change_log ``pre_hash`` column — every
            deletion entry point books it; the latest row per path wins,
            unbounded because change_log is persistent). When present for a
            deleted path, a local copy with a DIFFERENT hash is a
            deliberate local rewrite/recreation and stays in to_push; an
            identical hash (or a deletion with no recorded pre-hash, e.g.
            an external delete) is a stale copy and moves to to_delete.

    Returns:
        {
            "to_pull": [paths in server but not in local],
            "to_push": [paths in local but not in server],
            "to_delete": [local paths that were renamed or deleted on the
                          server; client should delete them],
            "conflicts": [paths in both with different hashes],
        }
        All lists sorted alphabetically.
    """
    local_paths = set(local)
    server_paths = set(server)

    to_pull = sorted(server_paths - local_paths)
    to_push = sorted(local_paths - server_paths)
    conflicts = sorted(
        p for p in local_paths & server_paths if local[p] != server[p]
    )

    # Rename detection: if a to_push path matches a recent move's from_path
    # AND the content hash matches the server's new path, the client's file
    # is a stale pre-rename copy. Move it from to_push to to_delete.
    to_delete: list[str] = []
    if recent_moves:
        move_map: dict[str, str] = {}  # from_path -> new path
        for m in recent_moves:
            fp = m.get("from_path", "")
            np = m.get("path", "")
            if fp and np:
                move_map[fp] = np
        new_to_push = []
        for p in to_push:
            new_path = move_map.get(p)
            if new_path and local.get(p) == server.get(new_path):
                to_delete.append(p)
            else:
                new_to_push.append(p)
        to_push = new_to_push

    # Deletion detection: a to_push path that was deleted on the server is
    # a stale local copy (pushing it back resurrects the file). Exception:
    # the deletion booked a pre-delete hash (change_log pre_hash) and the
    # local copy differs — the user recreated/rewrote the file after the
    # server-side deletion, so it is a genuine new file. (The move rule
    # above runs first; a path that both a move and a delete claim is
    # decided by the move rule.)
    if recent_deletes:
        deleted_paths = {c.get("path", "") for c in recent_deletes}
        new_to_push = []
        for p in to_push:
            if p in deleted_paths:
                pre_hash = (deleted_hashes or {}).get(p)
                if pre_hash and local.get(p) != pre_hash:
                    new_to_push.append(p)  # local rewrite after deletion
                else:
                    to_delete.append(p)  # stale copy (or external delete)
            else:
                new_to_push.append(p)
        to_push = new_to_push
    to_delete.sort()

    return {
        "to_pull": to_pull,
        "to_push": to_push,
        "to_delete": to_delete,
        "conflicts": conflicts,
    }


def derive_deleted_hashes(rows: list[dict]) -> dict[str, str]:
    """Replay all-time move/delete change_log rows (seq ASC) into
    {client-held original path: pre-delete hash}.

    ``rows``: change_log rows in ascending seq order, each shaped
    ``{"op": "move"|"delete", "path": ..., "old_path": ..., "pre_hash": ...}``
    (move rows: ``path`` = destination, ``old_path`` = source; delete rows:
    ``old_path`` is always empty). ``loc`` tracks where each historical
    path's content now lives; a delete row marks every path whose content
    ended up at the deleted path — the path itself plus all move-aliases
    (a client-held pre-move copy of a moved-then-deleted file is caught).

    Value "" = deleted with no recorded pre-hash (external delete) — still
    a stale-copy signal for the compare deletion gate. Later delete rows
    for the same live path overwrite earlier ones (latest pre-image wins).
    When a path was moved away and later re-created + re-deleted, the
    delete row at the vacated path refers to NEW content that no alias
    tracks — aliases are left untouched (documented ambiguity).
    """
    loc: dict[str, str] = {}
    out: dict[str, str] = {}
    for r in rows:
        op, p = r["op"], r["path"]
        if op == "move":
            f = r.get("old_path") or ""
            if not f:
                continue
            for k in [k for k, v in loc.items() if v == f]:
                loc[k] = p
            loc[f] = p
        elif op == "delete":
            h = r.get("pre_hash") or ""
            for k in [k for k, v in loc.items() if v == p]:
                out[k] = h
            if p not in loc:
                out[p] = h
    return out
