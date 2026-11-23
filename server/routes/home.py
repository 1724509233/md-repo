"""Home route domain: 首页·管家日报（spec 2026-11-22-home-butler-landing）。

``GET /api/home`` —— 缺席窗聚合：system_groups（非 web 行按 §4.2 三桶，
config 别名集为分桶唯一判据）+ my_days（固定 3d 足迹窗，与请求 since
解耦）+ actions（Task 2 填实）。事实全部来自 change_log 行，LLM 无渲染权
（§4.1）。``POST /api/home/summary``（§5.2）在同文件 Task 3 落地。
"""
from __future__ import annotations

import asyncio
import logging
import time

from fastapi.responses import JSONResponse
from openai import AsyncOpenAI

from ..agent.prompts import HOME_SUMMARY_SYSTEM
from ..core.privacy import is_privacy
from ..services.egress import EgressTarget, flush_touches, parse_endpoint, should_record

logger = logging.getLogger("md-vault.api")
from ..vault.db.changes import get_changes, get_changes_sources
from ..vault.db.file_index import get_all_files

SYSTEM_WINDOW_DAYS = 7   # 系统活动窗下界（§5.1 钳制）
MY_WINDOW_DAYS = 3       # 足迹窗固定值（§3-G3，与缺席窗解耦）

_OP_KEYS = ("seq", "ts", "action", "path", "from_path")  # ops 行形（seq 只做同 ts 平手稳定序）


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
                       since: float | None, seen_ts: float | None,
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

    # ── actions（§3-G4 / §5.1）：proposals 走 ledger 域；unseen = 窗内 ∧
    # ts > seen_ts ∧ 别名 allow-filter 三合一（2026-11-23 游标 seq→ts 换尺，
    # 直接复用上面 system_groups 的窗内行集，不再另查 seq 游标）——
    # watchdog（含用户自己的 Obsidian 编辑与 MCP 直写）恒不计，违者即违
    # §4.1 真账铁律（oracle 送审 B2）。计数单位 = distinct path「篇」。
    actions = {"pending_proposals": 0, "unseen_agent_docs": 0,
               "conflicts": 0, "conflict_path": None, "trash_batches": 0}
    if ledger_conn is not None:
        from ..services.proposals import status_summary
        actions["pending_proposals"] = status_summary(
            ledger_conn)["by_status"]["awaiting_review"]
    # conflict_path：计数同一次扫描里取第一个冲突路径——前端点击只扫 /api/docs
    # 可见面，机器区（如 index/）被树排除会永远找不到，故由服务端给出可开路径。
    conflict_paths = [f["path"] for f in get_all_files(db_conn) if ".conflict-" in f["path"]]
    actions["conflicts"] = len(conflict_paths)
    actions["conflict_path"] = conflict_paths[0] if conflict_paths else None
    from ..services.trash import list_trash
    actions["trash_batches"] = len(list_trash(vault_dir))
    # seen_ts 缺席（旧 seq 游标键退役的迁移态）→ 恒 0：本次全不算未看，
    # 前端渲染后游标即落 window.until，下次起正常计。
    if seen_ts is not None:
        actions["unseen_agent_docs"] = len({
            r["path"] for r in rows
            if (r["source"] == "agent" or r["source"] in aliases)
            and r["ts"] > seen_ts
        })

    return {"window": {"since": eff, "until": now},
            "system_groups": system_groups,
            "my_days": my_days,
            "actions": actions}


# ── POST /api/home/summary（§5.2）：按需一句话 + 成本面防护 + 行级出域台账 ──

SUMMARY_COOLDOWN_S = 300
# 200→2048（2026-11-23）：思考型模型 reasoning 长度抖动实测 100→650+，小预算被思考吃光
# （finish_reason=length, content=None）→ 空摘要 502 + 300s 冷却。chat_template_kwargs
# thinking:false 实测被 Splash 代理无视，不可依赖；放大预算 provider 无关，每日一次封顶。
SUMMARY_MAX_TOKENS = 2048
_GROUP_LABELS = {"agent": "我自己动手的", "external": "我眼皮子底下的外部改动"}


class SummaryError(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status, self.code = status, code


class SummaryCtl:
    """进程内当日缓存 + 单飞 + 失败退避（uvicorn 单进程假设，spec §5.2 备案）。"""

    def __init__(self) -> None:
        self.cache: dict[str, str] = {}
        self._inflight: asyncio.Task | None = None
        self._last_fail: float = 0.0


def assemble_home_digest(payload: dict, *, privacy_dirs: tuple[str, ...],
                         personal_profile: dict | None = None) -> tuple[str, dict[str, str]]:
    """digest 文本 + touched 映射。只含组头/动作/计数/路径，零正文（§5.2）。

    privacy 命中路径在文本里打码为〈私密〉（文件名本身可能泄漏含义）；
    touched 只收**真实非隐私**路径——flush_touches 的隐私双保险依赖真实
    路径判定，占位符既泄漏不了什么也查不了（oracle 送审 M5）。
    personal_profile 恒 None：个人记忆注入点（spec §10 演进位）。
    """
    lines: list[str] = []
    touched: dict[str, str] = {}

    def _mask(path: str) -> str:
        real = privacy_dirs and is_privacy(privacy_dirs, path)
        if not real:
            touched[path] = "digest"
            return path
        return "〈私密〉"

    if payload["system_groups"]:
        for g in payload["system_groups"]:
            label = (f"我让{g['alias']}办的" if g["actor"] == "bgagent"
                     else _GROUP_LABELS[g["actor"]])
            lines.append(f"{label}:")
            for op in g["ops"][:10]:
                hm = time.strftime("%H:%M", time.localtime(op["ts"]))
                lines.append(f"  {hm} {op['action']} {_mask(op['path'])}")
    else:
        lines.append("这段时间没有动静")

    if payload["my_days"]:
        lines.append("用户本人的近期足迹:")
        for d in payload["my_days"]:
            counts = " · ".join(f"{k}{v}" for k, v in sorted(d["counts"].items()))
            lines.append(f"  {d['date']} {counts}")

    a = payload["actions"]
    lines.append(f"待办计数: 待审提案{a['pending_proposals']} 冲突{a['conflicts']} "
                 f"回收站批次{a['trash_batches']}")
    return "\n".join(lines), touched


async def generate_summary(ctx, ctl: SummaryCtl) -> str:
    if ctx.ai_config is None:
        raise SummaryError(400, "model_not_configured")
    today = time.strftime("%Y-%m-%d")
    if today in ctl.cache:
        return ctl.cache[today]
    if time.time() - ctl._last_fail < SUMMARY_COOLDOWN_S:
        raise SummaryError(429, "cooldown")
    if ctl._inflight is not None and not ctl._inflight.done():
        return await ctl._inflight          # 单飞：并发双发共用一次真实调用
    task = asyncio.create_task(_generate_once(ctx, ctl))
    ctl._inflight = task
    return await task


async def _generate_once(ctx, ctl: SummaryCtl) -> str:
    aliases = frozenset((ctx.bg_targets or {}).keys())
    payload = build_home_payload(ctx.db_conn, vault_dir=ctx.vault_dir,
                                 aliases=aliases, since=None, seen_ts=None,
                                 ledger_conn=ctx.ledger_conn)
    pdirs = tuple(ctx.privacy.dirs) if ctx.privacy is not None else ()
    digest, touched = assemble_home_digest(payload, privacy_dirs=pdirs,
                                           personal_profile=None)
    ai = ctx.ai_config
    # 身份由通道注入（红线 3）：全部取自服务端 config，请求参数不含身份。
    target = EgressTarget("home", "agent", "", parse_endpoint(ai.base_url), ai.model)
    record = should_record(target) and bool(touched)
    try:
        client = AsyncOpenAI(base_url=ai.base_url, api_key=ai.api_key, timeout=120)
        resp = await client.chat.completions.create(
            model=ai.model,
            messages=[{"role": "system", "content": HOME_SUMMARY_SYSTEM},
                      {"role": "user", "content": digest}],
            temperature=ai.temperature,
            max_tokens=SUMMARY_MAX_TOKENS)
        text = (resp.choices[0].message.content or "").strip()
        if not text:
            raise RuntimeError("empty summary")
    except Exception:
        logger.warning("home summary generate failed", exc_info=True)
        if record:  # 失败也先落账：payload 已出域，红线 7 要的是事实不是体面
            flush_touches(ctx.ledger_conn, pdirs, target, touched)
        ctl._last_fail = time.time()
        raise SummaryError(502, "summary_failed")
    if record:
        flush_touches(ctx.ledger_conn, pdirs, target, touched)
    ctl.cache[time.strftime("%Y-%m-%d")] = text
    return text


def register(app, ctx) -> None:
    ctl = SummaryCtl()          # 测试 seam：app.state.home_summary
    app.state.home_summary = ctl

    @app.get("/api/home")
    async def home(since: float | None = None, seen_ts: float | None = None) -> dict:
        aliases = frozenset((ctx.bg_targets or {}).keys())
        return build_home_payload(
            ctx.db_conn, vault_dir=ctx.vault_dir, aliases=aliases,
            since=since, seen_ts=seen_ts, ledger_conn=ctx.ledger_conn)

    @app.post("/api/home/summary")
    async def home_summary():
        try:
            text = await generate_summary(ctx, ctl)
        except SummaryError as e:
            return JSONResponse(status_code=e.status, content={"error": e.code})
        return {"text": text}
