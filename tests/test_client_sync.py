"""Tests for client notification console output."""
from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import httpx

from client.sync import sync_once


def _client_config(tmp_path: Path):
    from client.config import ClientConfig

    return ClientConfig(
        server_url="http://testserver",
        vault_dir=tmp_path / "vault",
        device_name="test",
        poll_interval=60,
    )


def test_sync_once_logs_notifications(tmp_path: Path, caplog):
    (tmp_path / "vault").mkdir()

    notifications = [{
        "ts": 1760000000.0, "alias": "notes-organizer", "name": "笔记整理",
        "status": "ok", "report_path": "notes/.reports/notes-organizer/2026-10-11_0300.md",
        "summary": "合并了 2 篇碎片",
    }]

    def handler(request: httpx.Request):
        return httpx.Response(200, json={
            "to_pull": [], "to_push": [], "to_delete": [], "conflicts": [],
            "notifications": notifications,
        })

    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs.pop("timeout", None)
        return real_client(transport=httpx.MockTransport(handler), *args, **kwargs)

    with patch("client.sync.httpx.Client", side_effect=fake_client):
        with caplog.at_level(logging.INFO):
            sync_once(_client_config(tmp_path), {"last_sync": ""})

    assert any("[bg:notes-organizer]" in r.message for r in caplog.records)
    assert any("整理完成" in r.message for r in caplog.records)
