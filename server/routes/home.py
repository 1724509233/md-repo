"""Home route domain: 首页·管家日报（spec 2026-11-22-home-butler-landing）。

``GET /api/home`` —— 缺席窗聚合：system_groups（非 web 行按 §4.2 三桶，
config 别名集为分桶唯一判据）+ my_days（固定 3d 足迹窗，与请求 since
解耦）+ actions（Task 2 填实）。事实全部来自 change_log 行，LLM 无渲染权
（§4.1）。``POST /api/home/summary``（§5.2）在同文件 Task 3 落地。
"""
from __future__ import annotations

import time

from ..vault.db.changes import get_changes, get_changes_sources
from ..vault.db.file_index import get_all_files

SYSTEM_WINDOW_DAYS = 7   # 系统活动窗下界（§5.1 钳制）
MY_WINDOW_DAYS = 3       # 足迹窗固定值（§3-G3，与缺席窗解耦）

_OP_KEYS = ("seq", "ts", "action", "path", "from_path")  # ops 行形（seq 供已读游标）


def _effective_since(since: float | None, now: float) -> float:
    """双向钳制 ``[now − SYSTEM_WINDOW_DAYS, now]``（§5.1；上界防未来窗）。"""
    lo = now - SYSTEM_WINDOW_DAYS * 86400
    if since is None:
        return lo
    return max(lo, min(float(since), now))


def _bucket(source: str, aliases: frozenset[str]) -> tuple[str, str | None]:
    """§4.2：agent 自桶；config 别名集成员成 bgagent 桶；其余一切值
    （watchdog/unknown/reconcile/未来新值）归外部桶——绝不按「非保留词
    即别名」推断（否则渲染「我让 reconcile 办的」）。"""
    if source == "agent":
        return ("agent", None)
    if source in aliases:
        return ("bgagent", source)
    return ("external", None)


def _ops_of(rows: list[dict]) -> list[dict]:
    return [{k: r[k] for k in _OP_KEYS} for r in rows]


def build_home_payload(db_conn, *, vault_dir, aliases: frozenset[str],
                       since: float | None, last_seen_seq: int,
                       ledger_conn=None, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    eff = _effective_since(since, now)

    # ── system_groups：缺席窗内非 web 行 → 三桶，组 last_ts 降序、组内 ts 降序
    rows = get_changes_sources(db_conn, since=eff, exclude_sources={"web"}, limit=2000)
    groups: dict[tuple, dict] = {}
    for r in rows:
        actor, alias = _bucket(r["source"], aliases)
        g = groups.setdefault((actor, alias),
                              {"actor": actor, "alias": alias, "last_ts": r["ts"], "ops": []})
        g["ops"].append({k: r[k] for k in _OP_KEYS})
        g["last_ts"] = max(g["last_ts"], r["ts"])
    for g in groups.values():
        # ts 主序（组内行序 = 时间降序，spec 语义）；seq 只做同 ts 平手的
        # 稳定序——seq 是插入序不是时间序，越窗造数/补记场景两序可不一致。
        g["ops"].sort(key=lambda op: (op["ts"], op["seq"]), reverse=True)
    system_groups = sorted(groups.values(), key=lambda g: g["last_ts"], reverse=True)

    # ── my_days：source=web，固定近 3d（服务器本地日期分组；单机 LAN 单时区）
    my_rows = get_changes(db_conn, since=now - MY_WINDOW_DAYS * 86400,
                          source="web", limit=500)
    days: dict[str, dict] = {}
    for r in my_rows:
        date = time.strftime("%Y-%m-%d", time.localtime(r["ts"]))
        d = days.setdefault(date, {"date": date, "counts": {}, "ops": []})
        d["counts"][r["op"]] = d["counts"].get(r["op"], 0) + 1
        d["ops"].append({k: r[k] for k in _OP_KEYS})
    for d in days.values():
        d["ops"].sort(key=lambda op: (op["ts"], op["seq"]), reverse=True)
    my_days = sorted(days.values(), key=lambda d: d["date"], reverse=True)

    # ── actions（§3-G4 / §5.1）：proposals 走 ledger 域；unseen = seq 游标
    # × 别名 allow-filter——watchdog（含用户自己的 Obsidian 编辑与 MCP 直写）
    # 恒不计，违者即违 §4.1 真账铁律（oracle 送审 B2）。
    actions = {"pending_proposals": 0, "unseen_agent_changes": 0,
               "conflicts": 0, "trash_batches": 0}
    if ledger_conn is not None:
        from ..services.proposals import status_summary
        actions["pending_proposals"] = status_summary(
            ledger_conn)["by_status"]["awaiting_review"]
    actions["conflicts"] = sum(
        1 for f in get_all_files(db_conn) if ".conflict-" in f["path"])
    from ..services.trash import list_trash
    actions["trash_batches"] = len(list_trash(vault_dir))
    cursor_rows = get_changes_sources(
        db_conn, since_seq=max(int(last_seen_seq or 0), 0),
        exclude_sources={"web"}, limit=5000)
    actions["unseen_agent_changes"] = sum(
        1 for r in cursor_rows
        if r["source"] == "agent" or r["source"] in aliases)

    return {"window": {"since": eff, "until": now},
            "system_groups": system_groups,
            "my_days": my_days,
            "actions": actions}


def register(app, ctx) -> None:
    @app.get("/api/home")
    async def home(since: float | None = None, last_seen_seq: int = 0) -> dict:
        aliases = frozenset((ctx.bg_targets or {}).keys())
        return build_home_payload(
            ctx.db_conn, vault_dir=ctx.vault_dir, aliases=aliases,
            since=since, last_seen_seq=last_seen_seq, ledger_conn=ctx.ledger_conn)
