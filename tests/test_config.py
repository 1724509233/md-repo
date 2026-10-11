"""Tests for server.config and client.config."""
from __future__ import annotations

from pathlib import Path

import pytest

from client.config import load_client_config
from server.config import load_server_config


def test_load_server_config(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "server:\n"
        "  host: 0.0.0.0\n"
        "  port: 9888\n"
        "  vault_dir: ./vault\n",
        encoding="utf-8",
    )
    cfg = load_server_config(str(cfg_file))
    assert cfg.host == "0.0.0.0"
    assert cfg.port == 9888
    assert cfg.vault_dir == Path("./vault").resolve()


def test_load_server_config_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_server_config(str(tmp_path / "nope.yaml"))


def test_load_client_config(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "client:\n"
        "  server_url: http://192.168.1.100:9888\n"
        "  vault_dir: ./vault\n"
        "  device_name: mac\n"
        "  poll_interval: 300\n",
        encoding="utf-8",
    )
    cfg = load_client_config(str(cfg_file))
    assert cfg.server_url == "http://192.168.1.100:9888"
    assert cfg.vault_dir == Path("./vault")
    assert cfg.device_name == "mac"
    assert cfg.poll_interval == 300


def test_load_client_config_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_client_config(str(tmp_path / "nope.yaml"))
