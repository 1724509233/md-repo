"""Client configuration loading."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml
import os

@dataclass
class ClientConfig:
    server_url: str
    vault_dir: Path
    device_name: str
    poll_interval: int


def load_client_config(config_path: str) -> ClientConfig:
    """Read a YAML config file and extract the 'client' section.

    Raises FileNotFoundError if the config file does not exist.
    """
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    client = data.get("client")
    if not isinstance(client, dict):
        raise ValueError(f"Config file {config_path} missing 'client' section")

    return ClientConfig(
        server_url=client["server_url"].rstrip("/"),
        vault_dir=Path(os.path.expanduser(client["vault_dir"])).resolve(),
        device_name=client["device_name"],
        poll_interval=int(client.get("poll_interval", 300)),
    )
