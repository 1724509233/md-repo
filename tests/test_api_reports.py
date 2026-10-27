"""Console report channel API (v2a debt #8, ADR-0004 report channel).

The report channel lands read-only advisory reports under the machine zone
(``index/reports/<alias>/YYYY-MM-DD.md``); the console lists them and reads
one. The path guard mirrors ``_guard_proposal_relpath`` (the report channel
is the machine-zone advisory surface, the proposals channel is the card
surface — same defense, different root).
"""
from __future__ import annotations

import time
from pathlib import Path


def _write_report(vault_dir: Path, relpath: str, body: bytes) -> None:
    fp = vault_dir / relpath
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_bytes(body)


def test_reports_list_fields(client, vault_dir):
    """One report under index/reports/sentinel/ → one item, all fields correct."""
    _write_report(
        vault_dir, "index/reports/sentinel/2026-10-26.md",
        b"# Sentinel weekly review\n\nadvisory body\n",
    )
    resp = client.get("/api/memory/reports")
    assert resp.status_code == 200
    items = resp.json()["reports"]
    assert len(items) == 1
    it = items[0]
    assert it["relpath"] == "index/reports/sentinel/2026-10-26.md"
    assert it["alias"] == "sentinel"
    assert it["date"] == "2026-10-26"
    assert it["title"] == "Sentinel weekly review"
    assert isinstance(it["size"], int) and it["size"] > 0
    assert isinstance(it["mtime"], int)
    # mtime matches the on-disk file.
    assert it["mtime"] == int(
        (vault_dir / "index/reports/sentinel/2026-10-26.md").stat().st_mtime)


def test_reports_list_multiple_sorted_by_mtime_desc(client, vault_dir):
    """mtime descending: the newer report comes first."""
    _write_report(vault_dir, "index/reports/sentinel/2026-10-26.md", b"# A\n")
    _write_report(vault_dir, "index/reports/narrator/2026-10-27.md", b"# B\n")
    time.sleep(1.1)  # ensure a strictly larger mtime tick
    _write_report(vault_dir, "index/reports/narrator/2026-10-28.md", b"# C\n")
    resp = client.get("/api/memory/reports")
    items = resp.json()["reports"]
    assert [it["relpath"] for it in items] == [
        "index/reports/narrator/2026-10-28.md",
        "index/reports/narrator/2026-10-27.md",
        "index/reports/sentinel/2026-10-26.md",
    ]


def test_reports_list_only_two_level_md(client, vault_dir):
    """Only the two-level pattern index/reports/<alias>/*.md is listed: a
    non-.md sibling and a deeper file are excluded."""
    _write_report(vault_dir, "index/reports/sentinel/2026-10-26.md", b"# A\n")
    (vault_dir / "index/reports/sentinel").joinpath("notes.txt").write_bytes(b"x")
    (vault_dir / "index/reports/sentinel/sub").mkdir(parents=True)
    (vault_dir / "index/reports/sentinel/sub/deep.md").write_bytes(b"# deep\n")
    resp = client.get("/api/memory/reports")
    items = resp.json()["reports"]
    assert [it["relpath"] for it in items] == ["index/reports/sentinel/2026-10-26.md"]


def test_reports_title_fallback(client, vault_dir):
    """No ``# `` heading / unreadable file → empty title, never 500."""
    _write_report(vault_dir, "index/reports/sentinel/2026-10-26.md", b"no heading here\n")
    resp = client.get("/api/memory/reports")
    items = resp.json()["reports"]
    assert items[0]["title"] == ""


def test_reports_detail_content(client, vault_dir):
    """GET detail → the markdown content, byte-identical (utf-8)."""
    body = b"# Sentinel weekly review\n\n- recall telemetry\n- decay buckets\n"
    _write_report(vault_dir, "index/reports/sentinel/2026-10-26.md", body)
    resp = client.get("/api/memory/reports/index/reports/sentinel/2026-10-26.md")
    assert resp.status_code == 200
    data = resp.json()
    assert data["relpath"] == "index/reports/sentinel/2026-10-26.md"
    assert data["content"] == body.decode("utf-8")


def test_reports_detail_bad_path_guarded(client, vault_dir):
    """A path outside index/reports/ → 400 (the guard), never 500. A guarded
    but missing file → 404."""
    _write_report(vault_dir, "index/entities.md", b"# entities\n")
    # not under index/reports/ → 400
    assert client.get("/api/memory/reports/index/entities.md").status_code == 400
    # traversal → 400/404
    assert client.get("/api/memory/reports/../notes/test.md").status_code in (400, 404)
    # guarded relpath, missing file → 404
    assert client.get(
        "/api/memory/reports/index/reports/sentinel/2099-01-01.md").status_code == 404


def test_reports_list_empty_when_dir_missing(client, vault_dir):
    """No index/reports/ dir → empty list, 200 (never 500)."""
    assert not (vault_dir / "index/reports").is_dir()
    resp = client.get("/api/memory/reports")
    assert resp.status_code == 200
    assert resp.json() == {"reports": []}
