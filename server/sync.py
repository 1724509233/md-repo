"""Sync comparison logic."""
from __future__ import annotations


def compare_files(
    local: dict[str, str], server: dict[str, str]
) -> dict:
    """Compare local and server file manifests.

    Args:
        local: mapping of relative path -> sha256 hash (client side)
        server: mapping of relative path -> sha256 hash (server side)

    Returns:
        {
            "to_pull": [paths in server but not in local],
            "to_push": [paths in local but not in server],
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

    return {
        "to_pull": to_pull,
        "to_push": to_push,
        "conflicts": conflicts,
    }
