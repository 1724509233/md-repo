"""Local client state management (.sync/state.json)."""
from __future__ import annotations

import json
from pathlib import Path


def load_state(state_path: Path) -> dict:
    """Load the client state file.

    Returns {"last_sync": ..., "files": {path: hash}} or an empty dict
    if the file does not exist or is unreadable.
    """
    if not state_path.is_file():
        return {}
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def save_state(state_path: Path, state: dict) -> None:
    """Write the client state as JSON, creating parent directories."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
