"""Server configuration loading."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class ServerConfig:
    host: str
    port: int
    vault_dir: Path


def load_server_config(config_path: str) -> ServerConfig:
    """Read a YAML config file and extract the 'server' section.

    Raises FileNotFoundError if the config file does not exist.
    """
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    server = data.get("server")
    if not isinstance(server, dict):
        raise ValueError(f"Config file {config_path} missing 'server' section")

    return ServerConfig(
        host=server["host"],
        port=int(server["port"]),
        vault_dir=Path(server["vault_dir"]),
    )
