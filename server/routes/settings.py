"""Settings domain: /api/settings — config.yaml 页面化编辑 (spec 2026-11-22 §4).

GET = 五段掩码回显（api_key 永不明文），PUT = 字段级写回 + 校验即启动 + 备份 +
编排热生效。写回模型（§4.2）：ruamel round-trip 原文档，只对页面可见键（下方
`_*_KEYS` 集合）set/delete——未建模键（bgagent 条目的 escalation 等）与页面外
section（server/client/mcp）天然逐字节保留。

payload 语义（与前端 SettingsPage buildPayload 契约一致）：PUT body = 页面五段
全量状态——键缺省 = 删除该可见键（回加载器缺省）；显式例外：
- `api_key` 缺省/`null` = 保持原明文（回填），`""` = 显式清除（spec §4.2.2，
  null ≡ 缺省钉死：防前端 undefined/null 序列化差异误删 key）；
- `tools: null` = 显式全量白名单（区别于键缺省的删除语义——写 null 进 yaml）；
- `privacy.dirs: []` = 显式空名单；
- `ai: null` = 删段；
- background_agents 按 alias 对账，payload 缺席 = 删条目（改名 = 删 + 增，
  新别名无处回填 api_key，spec §5.4/S3）。

串行化（§4.2 TOCTOU 收口）：PUT 的「revision 守卫→校验→落盘→热生效」全程持
路由级 asyncio.Lock——lock 在 register 闭包内建（非模块全局：多 create_app
实例互不染）。

apply 编排顺序（§5 分级热生效，applied 恒为稳定五段全量词表）：
ctx.ai_config 替换 → supervisor.apply(loaded) → ctx.bg_targets=agent_caps →
ctx.settings_gen += 1（agent 缓存代际戳，读侧失效在 Task 6——永不外清缓存）→
notif_hook.set_daily_cap → root_logger.setLevel + file_handler.backupCount →
端点集合 (host, model_id) 有变化才重跑 log_model_endpoints（红线 7）。
apply 段抛 = 文件已落盘、绝不反滚（回滚只制造第三态，spec §4.2.6 / 评审 M6.3）
→ 500 {saved: true, error}。
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from ..agent.tools import TOOL_REGISTRY, validate_tool_whitelists
from ..bgagent.scheduler import agent_caps
from ..config import (
    TRIGGER_OPS,
    load_background_agents,
    load_notifications,
    load_server_config,
)
from ..config.doc_io import (
    SettingsWriteError,
    backup_file,
    bgagent_to_payload,
    mask_api_key,
    open_roundtrip,
    payload_to_bgagent_entry,
    revision_of,
    save_doc,
)
from ..core.constants import VALID_TYPES
from ..services.egress import log_model_endpoints, parse_endpoint

logger = logging.getLogger("md-vault.settings")

# applied 稳定五段全量词表（spec §4.2 步 6：响应恒带全部段名）。
_APPLIED = ["ai", "background_agents", "notifications", "privacy", "log"]
# 重启标注常量（spec §4.1 _meta.restart_fields：就一个字段，不成机制）。
_RESTART_FIELDS = ["log.dir"]
_LOG_DEFAULT_DIR = ".sync/logs"

# 页面可见键集合——字段级写回只对 set 内键 set/delete（未建模键零触碰）。
_AI_KEYS = frozenset({"base_url", "model", "max_tokens", "temperature",
                      "context_window", "max_rounds", "read_doc_max_chars",
                      "confirm_timeout", "tools"})
_BG_KEYS = frozenset({"alias", "name", "enabled", "cron", "target_dir",
                      "prompt", "report_dir", "max_rounds", "timeout_s",
                      "budget_runs_per_day", "budget_max_proposals",
                      "trigger", "tools", "verbs", "model"})
_BG_MODEL_KEYS = frozenset({"provider", "model_id", "base_url", "api_key",
                            "context_window", "max_tokens", "temperature"})
_BG_TRIGGER_KEYS = frozenset({"source", "events", "metric", "arm_count",
                              "min_interval_h", "low_score"})
_NOTIF_KEYS = frozenset({"daily_cap"})
_PRIVACY_KEYS = frozenset({"dirs"})
# log 段页可见键 ↔ server 段扁平键名（config 的 log 配置本就平铺在 server 下）。
_LOG_FLAT_KEYS = {"level": "log_level", "dir": "log_dir",
                  "retention_days": "log_retention_days"}


class _Rejected(ValueError):
    """payload 形状守卫（加载器不覆盖的非映射类型）——与加载器异常同一 422 出口。"""

    def __init__(self, message: str, section: str):
        super().__init__(message)
        self.section = section


def _plain(value: Any) -> Any:
    """ruamel 节点 → 纯 JSON 值（CommentedMap/Seq 与 ScalarString 归纯）。"""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def _section_of(message: str) -> str:
    """422 section 定位启发式（加载器文案前缀即段名——前端据此高亮定位）。"""
    if "background_agents" in message or "alias" in message:
        return "background_agents"
    if message.startswith("ai.") or message.startswith("ai:"):
        return "ai"
    if "privacy" in message:
        return "privacy"
    if "notifications" in message:
        return "notifications"
    if "log_" in message or "log." in message:
        return "log"
    return "config"


def _json(status: int, content: Any) -> JSONResponse:
    return JSONResponse(status_code=status, content=content)


# ── GET ────────────────────────────────────────────────────────────────────


def _get(ctx) -> dict:
    """五段回显（spec §4.1）：raw 直通 + 缺省段补显示默认值 + 密钥掩码。"""
    path = Path(ctx.settings.config_path)
    _, doc = open_roundtrip(path)
    doc = doc if isinstance(doc, dict) else {}

    ai_raw = doc.get("ai")
    ai: dict | None = None
    if isinstance(ai_raw, dict):
        ai = _plain(ai_raw)
        ai["api_key"] = mask_api_key(ai.get("api_key"))  # 明文永不出 GET

    bgs_raw = doc.get("background_agents")
    bgs = ([bgagent_to_payload(e, ctx.vault_dir)
            for e in bgs_raw if isinstance(e, dict)]
           if isinstance(bgs_raw, list) else [])

    notifications = (_plain(doc.get("notifications"))
                     if isinstance(doc.get("notifications"), dict) else {})
    notifications.setdefault("daily_cap", 20)

    privacy = (_plain(doc.get("privacy"))
               if isinstance(doc.get("privacy"), dict) else {})
    dirs = privacy.get("dirs")
    privacy["dirs"] = list(dirs) if isinstance(dirs, list) else []

    server_raw = (_plain(doc.get("server"))
                  if isinstance(doc.get("server"), dict) else {})
    log = {
        "level": server_raw.get("log_level", "INFO"),
        "dir": server_raw.get("log_dir", _LOG_DEFAULT_DIR),
        "retention_days": server_raw.get("log_retention_days", 30),
    }

    return {
        "revision": revision_of(path),
        "ai": ai,
        "background_agents": bgs,
        "notifications": notifications,
        "privacy": privacy,
        "log": log,
        "_meta": {
            "tools": sorted(TOOL_REGISTRY),
            "verbs": sorted(VALID_TYPES),
            # trigger 事件全集随 _meta 下发（Task 9 前端镜像常量的收口点）
            "trigger_ops": sorted(TRIGGER_OPS),
            "restart_fields": list(_RESTART_FIELDS),
        },
    }


# ── PUT：字段级写回 ─────────────────────────────────────────────────────────


def _sync_keys(node: dict, src: dict, visible: frozenset) -> None:
    """payload 带键 = set；缺省 = delete（回加载器缺省）。唯一例外由调用方处理。"""
    for k in visible:
        if k in src:
            node[k] = src[k]
        else:
            node.pop(k, None)


def _write_ai(doc, payload) -> None:
    if payload is None:  # ai: null（或段缺省）= 删段（spec §4.2 显式删除语义）
        doc.pop("ai", None)
        return
    if not isinstance(payload, dict):
        raise _Rejected("ai must be an object or null", "ai")
    old = doc.get("ai")
    node = old if isinstance(old, dict) else None
    if node is None:
        creates = any(k in payload for k in _AI_KEYS) or \
            isinstance(payload.get("api_key"), str)
        if not creates:
            doc.pop("ai", None)
            return
        doc["ai"] = node = {}
    _sync_keys(node, payload, _AI_KEYS)
    # api_key 三态（§4.2.2）：str（含 ""）= 替换；缺省/null = 保持原明文
    # （旧节点原位保留；新节点无处回填 → 不写键）。
    if isinstance(payload.get("api_key"), str):
        node["api_key"] = payload["api_key"]
    if not node:
        doc.pop("ai", None)


def _write_bg(doc, payload) -> None:
    if payload is None:
        payload = []
    if not isinstance(payload, list):
        raise _Rejected("background_agents must be a list", "background_agents")
    old_list = doc.get("background_agents")
    old_list = old_list if isinstance(old_list, list) else []
    by_alias = {str(e["alias"]): e for e in old_list
                if isinstance(e, dict) and e.get("alias") is not None}

    new_entries: list = []
    for entry in payload:
        if not isinstance(entry, dict):
            raise _Rejected("background_agents entries must be objects",
                            "background_agents")
        alias = str(entry.get("alias") or "")
        old = by_alias.get(alias) if alias else None
        # 密钥回填三态（doc_io：缺省/null → 原明文；"" → 显式清除）在合并前完成。
        filled = payload_to_bgagent_entry(entry, _plain(old) if old is not None else None)
        if old is None:
            new_entries.append(filled)  # 新别名：无处回填 api_key（S3 语义）
            continue
        # 合并到旧条目节点（注释载体在旧节点上）；「可见键中 payload 未带」的
        # 键删除（回加载器缺省）——UI 直通 raw 已保证不丢，服务端双保险。
        _sync_keys(old, filled, _BG_KEYS)
        model = filled.get("model")
        if isinstance(model, dict):
            m_node = old.get("model") if isinstance(old.get("model"), dict) else None
            if m_node is None:
                old["model"] = m_node = {}
            _sync_keys(m_node, model, _BG_MODEL_KEYS)
            if not m_node:
                old.pop("model", None)
        trig = filled.get("trigger")
        if isinstance(trig, dict):
            t_node = old.get("trigger") if isinstance(old.get("trigger"), dict) else None
            if t_node is None:
                old["trigger"] = t_node = {}
            _sync_keys(t_node, trig, _BG_TRIGGER_KEYS)
            if not t_node:
                old.pop("trigger", None)
        new_entries.append(old)

    if new_entries:
        doc["background_agents"] = new_entries
    else:
        doc.pop("background_agents", None)


def _write_simple(doc, section: str, payload, visible: frozenset) -> None:
    """notifications / privacy：段对象缺省 = 删除全部可见键（回缺省）。"""
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise _Rejected(f"{section} must be an object", section)
    old = doc.get(section)
    node = old if isinstance(old, dict) else None
    if node is None:
        if not any(k in payload for k in visible):
            doc.pop(section, None)
            return
        doc[section] = node = {}
    _sync_keys(node, payload, visible)
    if not node:
        doc.pop(section, None)


def _write_log(doc, payload) -> None:
    """log 段 → server 段扁平键（log_level / log_dir / log_retention_days）。"""
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise _Rejected("log must be an object", "log")
    server = doc.get("server")
    node = server if isinstance(server, dict) else None
    if node is None:
        if not any(k in payload and payload[k] is not None for k in _LOG_FLAT_KEYS):
            return
        doc["server"] = node = {}
    for page_key, flat_key in _LOG_FLAT_KEYS.items():
        if page_key in payload and payload[page_key] is not None:
            node[flat_key] = payload[page_key]
        else:
            node.pop(flat_key, None)


# ── PUT：编排 ──────────────────────────────────────────────────────────────


def _endpoint_set(ai, bg_agents) -> set[tuple[str, str]]:
    """出域台账比较面：(host, model_id) 全集（spec §5.5——分类不进集合，行内容仍全套）。"""
    keys: set[tuple[str, str]] = set()
    if ai is not None:
        keys.add((parse_endpoint(ai.base_url), str(ai.model)))
    for a in bg_agents:
        keys.add((parse_endpoint(a.model.base_url), str(a.model.model_id)))
    return keys


async def _put(request: Request, ctx, lock: asyncio.Lock) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        body = {}
    path = Path(ctx.settings.config_path)

    async with lock:  # ① 锁内 revision 守卫（TOCTOU 收口：守卫与 replace 之间零交错）
        current = revision_of(path)
        if body.get("revision") != current:
            return _json(409, {"current_revision": current})

        yaml_obj, doc = open_roundtrip(path)
        doc = doc if isinstance(doc, dict) else {}
        # 保存前状态：log.dir 有效值（restart 判定）+ 出域端点集（§5.5 比较面）。
        pre_server = (_plain(doc.get("server"))
                      if isinstance(doc.get("server"), dict) else {})
        pre_endpoints = _endpoint_set(ctx.ai_config, load_background_agents(str(path), ctx.vault_dir))

        # ② 密钥回填 + ③ 字段级写回（可见键 set/delete；api_key 例外语义在内）。
        try:
            _write_ai(doc, body.get("ai"))
            _write_bg(doc, body.get("background_agents"))
            _write_simple(doc, "notifications", body.get("notifications"), _NOTIF_KEYS)
            _write_simple(doc, "privacy", body.get("privacy"), _PRIVACY_KEYS)
            _write_log(doc, body.get("log"))
        except _Rejected as exc:
            return _json(422, {"errors": [{"message": str(exc), "section": exc.section}]})

        # ④ 校验即启动（spec §3.1）：**同目录**临时文件上跑真加载器——加载器唯一
        #    的 config-dir 锚是 bgagent prompt（tmp 去 %TEMP% 会假阴性）；校验时的
        #    vault_dir 传运行进程现值。加载对象直接喂 apply，不重复加载。
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent),
                                        prefix=".config-validate-", suffix=".yaml.tmp")
        os.close(fd)
        try:
            with open(tmp_name, "wb") as f:
                yaml_obj.dump(doc, f)
            try:
                loaded_server = load_server_config(tmp_name)
                loaded_notifications = load_notifications(tmp_name)
                loaded_bg = load_background_agents(tmp_name, ctx.vault_dir)
                validate_tool_whitelists(loaded_server, loaded_bg)
            except (ValueError, FileNotFoundError) as exc:
                # 422 零落盘零备份；message = 加载器异常原文（bgagent 类含
                # [index] (alias) 定位前缀 = 前端契约；加载器不吐密钥）。
                msg = str(exc)
                return _json(422, {"errors": [{"message": msg,
                                               "section": _section_of(msg)}]})
        finally:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass

        # ⑤ 备份（replace 前产生；备份失败也算未保存）⑥ 原子落盘。
        try:
            backup_file(path)
            new_rev = save_doc(yaml_obj, doc, path)
        except SettingsWriteError as exc:
            return _json(500, {"saved": False, "error": str(exc)})
        except OSError as exc:  # 备份/写侧其它失败——如实「未保存」
            return _json(500, {"saved": False, "error": f"配置写入失败: {exc}"})

        # ⑦ 热生效编排（§5 顺序；任何一环抛 = 500 {saved:true}，绝不反滚文件）。
        rt = ctx.settings.runtime
        try:
            ctx.ai_config = loaded_server.ai          # §5.1 下一次派发新配置（Task 6 代际）
            rt.supervisor.apply(loaded_bg)            # §5.4（同步、同循环——承重契约）
            ctx.bg_targets = agent_caps(loaded_bg)    # 监控页预算显示随热切
            ctx.settings_gen += 1                     # agent_cache 代际戳（读侧失效）
            rt.notif_hook.set_daily_cap(loaded_notifications.daily_cap)  # §5.2
            rt.root_logger.setLevel(getattr(logging, loaded_server.log.level))
            rt.file_handler.backupCount = loaded_server.log.retention_days
            if _endpoint_set(loaded_server.ai, loaded_bg) != pre_endpoints:
                eg = logging.getLogger("md-vault.egress")
                for line in log_model_endpoints(loaded_server.ai, loaded_bg):
                    eg.info("%s", line)              # §5.5 出域事实变化 = 可见事件
        except Exception as exc:  # noqa: BLE001 — apply 失败窗口的如实上报面
            logger.exception("hot-apply failed after save (config stays committed)")
            return _json(500, {
                "saved": True,
                "error": f"部分设置即时生效失败，建议重启服务: {type(exc).__name__}: {exc}",
            })

        pre_dir = str(pre_server.get("log_dir") or _LOG_DEFAULT_DIR)
        log_body = body.get("log")
        post_dir = (str(log_body.get("dir") or _LOG_DEFAULT_DIR)
                    if isinstance(log_body, dict) else _LOG_DEFAULT_DIR)
        restart = ["log.dir"] if post_dir != pre_dir else []
        return _json(200, {"revision": new_rev, "applied": list(_APPLIED),
                           "restart_needed": restart})


def register(app, ctx) -> None:
    # 路由级串行锁在 register 闭包里建——不是模块全局：多 create_app 实例
    # （测试）互不染；锁跨 app 生命期复用。
    put_lock = asyncio.Lock()

    def _unwired() -> bool:
        # 未接线 503（spec §4.3，mirror ai=None→503 先例；conftest app 天然覆盖）。
        return ctx.settings is None or ctx.settings.runtime is None

    @app.get("/api/settings")
    async def get_settings() -> JSONResponse:
        if _unwired():
            return _json(503, {"error": "设置管理未接线"})
        return _json(200, _get(ctx))

    @app.put("/api/settings")
    async def put_settings(request: Request) -> JSONResponse:
        if _unwired():
            return _json(503, {"error": "设置管理未接线"})
        return await _put(request, ctx, put_lock)
