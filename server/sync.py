"""Sync comparison logic."""
from __future__ import annotations


def compare_files(
    local: dict[str, str],
    server: dict[str, str],
    recent_moves: list[dict] | None = None,
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

    Returns:
        {
            "to_pull": [paths in server but not in local],
            "to_push": [paths in local but not in server],
            "to_delete": [local paths that were renamed on the server;
                          client should delete them],
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
    to_delete.sort()

    return {
        "to_pull": to_pull,
        "to_push": to_push,
        "to_delete": to_delete,
        "conflicts": conflicts,
    }
