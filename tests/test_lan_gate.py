"""LAN hard gate (redline 6): a non-loopback bind host needs lan_exposure: true.

The gate is called from server/main.py's startup path only — never from
load_server_config (shared with MCP stdio, which listens on nothing and
must keep booting on legacy 0.0.0.0 configs; pinned in test_config.py).
"""
from __future__ import annotations

import pytest

from server.config.core import is_loopback, lan_gate_check


def test_gate_rejects_nonloopback_without_flag():
    """0.0.0.0 without the flag → ValueError; the message must name both
    the consequence and the explicit opt-in (spec §5-9 wording pin)."""
    with pytest.raises(ValueError) as exc:
        lan_gate_check("0.0.0.0", False)
    msg = str(exc.value)
    assert "局域网内任何人可读写 vault、批准提案" in msg
    assert "lan_exposure: true" in msg


def test_gate_allows_nonloopback_with_flag():
    lan_gate_check("0.0.0.0", True)  # must not raise


@pytest.mark.parametrize("host", [
    "localhost", "127.0.0.1", "127.2.3.4", "::1",
])
def test_is_loopback_true(host: str):
    assert is_loopback(host)


@pytest.mark.parametrize("host", [
    "0.0.0.0", "192.168.1.5", "",
])
def test_is_loopback_false(host: str):
    assert not is_loopback(host)


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1"])
def test_gate_allows_loopback_without_flag(host: str):
    lan_gate_check(host, False)  # must not raise
