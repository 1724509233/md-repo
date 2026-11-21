"""Tests for server/config/doc_io.py — settings-page config document IO leaf.

法源：docs/superpowers/specs/2026-11-22-settings-page-design.md §4.1/§4.2；
Interfaces 块钉：round-trip 注释/CRLF 保真、同目录临时文件 + PermissionError
退避重试、备份滚动 3、api_key 掩码三态、bgagent 路径反归一与密钥回填三态。
"""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

import pytest

from server.config.doc_io import (
    SettingsWriteError,
    backup_file,
    bgagent_to_payload,
    mask_api_key,
    open_roundtrip,
    payload_to_bgagent_entry,
    revision_of,
    save_doc,
)

# Round-trip 样本：注释 + CRLF（write_bytes 保字节，Windows 换行翻译红线）。
ROUNDTRIP_SAMPLE = (
    b"# top-level hand-written comment\r\n"
    b"server:\r\n"
    b"  host: 127.0.0.1\r\n"
    b"  port: 9888  # keep this comment\r\n"
    b"  vault_dir: ./vault\r\n"
    b"ai:\r\n"
    b"  base_url: http://localhost:11434\r\n"
    b'  model: "qwen2.5:14b"\r\n'
    b"background_agents: []\r\n"
)


def _make_config(tmp_path: Path) -> Path:
    target = tmp_path / "config.yaml"
    target.write_bytes(ROUNDTRIP_SAMPLE)
    return target


# ---------------------------------------------------------------------------
# revision_of
# ---------------------------------------------------------------------------

def test_revision_of_matches_file_hash(tmp_path):
    target = _make_config(tmp_path)
    rev = revision_of(target)
    expected = "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()
    assert rev == expected
    # 手改一个字节 → revision 变
    target.write_bytes(target.read_bytes() + b"\r\n")
    assert revision_of(target) != rev


# ---------------------------------------------------------------------------
# round-trip: open_roundtrip + save_doc（注释 + CRLF 保真、revision 一致）
# ---------------------------------------------------------------------------

def test_roundtrip_preserves_comments_and_crlf(tmp_path):
    target = _make_config(tmp_path)
    yaml_obj, doc = open_roundtrip(target)
    doc["server"]["port"] = 9889

    rev = save_doc(yaml_obj, doc, target)

    raw = target.read_bytes()
    # 注释逐字节在场
    assert b"# top-level hand-written comment" in raw
    assert b"# keep this comment" in raw
    # 目标值已变、旧值不在
    assert b"port: 9889" in raw
    assert b"port: 9888" not in raw
    # CRLF 全文件保真：不存在裸 \n
    assert raw.count(b"\r\n") > 0
    assert raw.replace(b"\r\n", b"").count(b"\n") == 0
    # 引号保真（preserve_quotes）
    assert b'"qwen2.5:14b"' in raw
    # 返回 revision 与文件哈希一致
    assert rev == "sha256:" + hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# backup_file：微秒尾防撞名、滚动留 3
# ---------------------------------------------------------------------------

def test_backup_names_differ_and_roll_keeps_three(tmp_path):
    target = _make_config(tmp_path)

    first = backup_file(target)
    time.sleep(0.001)
    second = backup_file(target)
    # 微秒尾：同秒双保存不撞名
    assert first.name != second.name
    assert first.name.startswith("config.yaml.bak-")
    assert first.parent == target.parent

    time.sleep(0.001)
    backup_file(target)
    time.sleep(0.001)
    fourth = backup_file(target)

    baks = sorted(target.parent.glob("config.yaml.bak-*"))
    assert len(baks) == 3
    # 留最近 3 份：最旧的（first）被删，第四份在场
    assert first.name not in [b.name for b in baks]
    assert fourth in baks
    # 备份内容与当时目标一致
    assert second.read_bytes() == ROUNDTRIP_SAMPLE


# ---------------------------------------------------------------------------
# mask_api_key 三例
# ---------------------------------------------------------------------------

def test_mask_api_key_long_key():
    out = mask_api_key("sk-abcdef1234567890wxyz")
    assert out == {"set": True, "mask": "sk-a-…wxyz"}


def test_mask_api_key_short_key_no_fragment():
    out = mask_api_key("short7")  # len < 8 → 只回「已设置」不回片段
    assert out == {"set": True, "mask": None}


def test_mask_api_key_none_and_empty():
    assert mask_api_key(None) == {"set": False, "mask": None}
    assert mask_api_key("") == {"set": False, "mask": None}


# ---------------------------------------------------------------------------
# bgagent_to_payload：反归一 + report_dir 缺省省略 + 掩码
# ---------------------------------------------------------------------------

def _bg_entry(vault: Path, **overrides) -> dict:
    entry = {
        "alias": "daily",
        "name": "Daily",
        "enabled": True,
        "cron": "0 8 * * *",
        # 绝对路径样本（Windows 反斜杠形状），加载器归一同款
        "target_dir": str(vault / "reports" / "daily"),
        "prompt": "prompts/daily.md",
        # 等于缺省 <target_dir>/.reports/<alias> → 应省略
        "report_dir": str(vault / "reports" / "daily" / ".reports" / "daily"),
        "escalation": {"zero_recall_candidate": "notify"},  # 未建模键直通
        "model": {"model_id": "m1", "api_key": "sk-abcdef1234567890wxyz"},
    }
    entry.update(overrides)
    return entry


def test_bgagent_to_payload_denormalizes_and_defaults(tmp_path):
    vault = tmp_path / "vault"
    entry = _bg_entry(vault)

    payload = bgagent_to_payload(entry, vault)

    # target_dir 反归一为 vault 相对 posix
    assert payload["target_dir"] == "reports/daily"
    # report_dir 等于缺省 → 键省略
    assert "report_dir" not in payload
    # 未建模键直通
    assert payload["escalation"] == {"zero_recall_candidate": "notify"}
    assert payload["cron"] == "0 8 * * *"
    # model.api_key 换掩码形态，明文绝不出
    assert payload["model"]["api_key"] == mask_api_key("sk-abcdef1234567890wxyz")
    assert "sk-abcdef1234567890wxyz" not in repr(payload)
    # 原 entry 不被就地修改
    assert entry["model"]["api_key"] == "sk-abcdef1234567890wxyz"


def test_bgagent_to_payload_custom_report_dir_kept(tmp_path):
    vault = tmp_path / "vault"
    entry = _bg_entry(
        vault,
        report_dir=str(vault / "custom" / "reports"),
        target_dir="reports/daily",  # 相对 posix 直通
    )
    payload = bgagent_to_payload(entry, vault)

    assert payload["target_dir"] == "reports/daily"
    assert payload["report_dir"] == "custom/reports"


# ---------------------------------------------------------------------------
# payload_to_bgagent_entry：api_key 三态
# ---------------------------------------------------------------------------

_OLD = {"model": {"model_id": "m1", "api_key": "sk-old-secret"}}


def test_payload_to_bgagent_entry_key_absent_backfills():
    payload = {"alias": "daily", "model": {"model_id": "m2"}}
    out = payload_to_bgagent_entry(payload, _OLD)
    assert out["model"]["api_key"] == "sk-old-secret"
    # 原 payload 不被就地修改
    assert "api_key" not in payload["model"]


def test_payload_to_bgagent_entry_key_null_backfills():
    payload = {"alias": "daily", "model": {"model_id": "m2", "api_key": None}}
    out = payload_to_bgagent_entry(payload, _OLD)
    assert out["model"]["api_key"] == "sk-old-secret"


def test_payload_to_bgagent_entry_key_empty_string_clears():
    payload = {"alias": "daily", "model": {"model_id": "m2", "api_key": ""}}
    out = payload_to_bgagent_entry(payload, _OLD)
    assert out["model"]["api_key"] == ""


def test_payload_to_bgagent_entry_replaces_with_new_value():
    payload = {"alias": "daily", "model": {"model_id": "m2", "api_key": "sk-brand-new"}}
    out = payload_to_bgagent_entry(payload, _OLD)
    assert out["model"]["api_key"] == "sk-brand-new"


def test_payload_to_bgagent_entry_no_old_writes_no_key():
    payload = {"alias": "daily", "model": {"model_id": "m2", "api_key": None}}
    out = payload_to_bgagent_entry(payload, None)
    assert "api_key" not in out["model"]

    # old_entry 在场但无 api_key → 同样不写键
    out2 = payload_to_bgagent_entry(payload, {"model": {"model_id": "m1"}})
    assert "api_key" not in out2["model"]


# ---------------------------------------------------------------------------
# save_doc：Windows PermissionError 退避重试（os.replace 被瞬时句柄挡）
# ---------------------------------------------------------------------------

def _flaky_replace(monkeypatch, fail_times: int):
    """Patch os.replace：前 fail_times 次抛 PermissionError，之后放行。"""
    real_replace = os.replace
    calls = {"n": 0}

    def flaky(src, dst, **kwargs):
        calls["n"] += 1
        if calls["n"] <= fail_times:
            raise PermissionError(13, "denied by transient handle")
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr(os, "replace", flaky)
    return calls


def test_save_doc_permission_error_retries_then_succeeds(tmp_path, monkeypatch):
    target = _make_config(tmp_path)
    yaml_obj, doc = open_roundtrip(target)
    doc["server"]["port"] = 9999

    calls = _flaky_replace(monkeypatch, fail_times=2)
    rev = save_doc(yaml_obj, doc, target)

    assert calls["n"] == 3  # 初次 + 退避重试 ≤2
    raw = target.read_bytes()
    assert b"port: 9999" in raw
    assert rev == "sha256:" + hashlib.sha256(raw).hexdigest()
    # 无临时文件残留
    assert not list(tmp_path.glob(".config-*.yaml.tmp"))


def test_save_doc_all_retries_fail_cleans_tmp(tmp_path, monkeypatch):
    target = _make_config(tmp_path)
    before = target.read_bytes()
    yaml_obj, doc = open_roundtrip(target)
    doc["server"]["port"] = 9999

    calls = _flaky_replace(monkeypatch, fail_times=99)
    with pytest.raises(SettingsWriteError) as excinfo:
        save_doc(yaml_obj, doc, target)

    assert calls["n"] == 3  # 全败：同样止于初次 + 2 次重试
    # 如实「未保存」：目标字节未动
    assert target.read_bytes() == before
    # 临时文件必须清掉
    assert not list(tmp_path.glob(".config-*.yaml.tmp"))
    # 异常携带 tmp 路径（已删除的记录值）
    assert excinfo.value.tmp_path.name.startswith(".config-")
    assert not excinfo.value.tmp_path.exists()
