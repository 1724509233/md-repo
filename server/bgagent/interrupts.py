"""Interrupt-signal injection as a SystemReminder (channel-2 spec
2026-11-20-stop-signals-reasoning-capture §4) — the first lightweight
consumer of ``chat_abort_log`` (「用户写的提示词，被用户中断过的每一轮都在
替它压测」).

Mount shape follows the ``ReadbackSystemReminder`` precedent verbatim:
``runner.run_once`` declares it in ``AgentSpec.system_reminders`` for every
bgagent run; the gate is DATA (self-gating like the readback sections) —
no conn / no signal in the 7-day window → ``render()`` None → zero
injection, zero egress row. No alias special-casing, no config keys (§6).

Data plane (§4 join contract, single DB — sessions was promoted to
ledger.db 2026-11-20):
- read the last 7 days of chat_abort_log via the db-layer reader
  (``chat_abort_records.recent``, ORDER BY session_id, id = 会话内因果序);
- ``message_id`` equality-joins the interrupted boundary row in the
  sessions blob (锚行 content + reasoning) — blob 永不 sort, 数组序即因果序;
- 「停后补发」= the first ``role:"user"`` row AFTER the anchor (relative
  position; the identity stamp guarantees no misalignment). Injected
  ``[当前打开:`` reminder rows are skipped — they are framework text, not
  the user's follow-up.
- ``message_id`` NULL = un-anchored stop (blank stop / lock-wait cancel):
  honestly labeled 「无锚点信号」, never guessed (§4 残留边界).

Facts vs claims (§4 报告纪律, notes §2 第 3 层): the row's stamped columns
(ts/phase/tool_name/model/session_id) are behavioral FACTS; the joined
content/reasoning are the model's CLAIMS — not evidence. The labels ride
the injected text itself so a report cannot launder the distinction.

Injection audit payload: ``last_bytes`` / ``last_signals`` are left on the
instance after render (the runner writes the synthetic ``via='reminder'``
egress row, redline 7 / review M2 — 出域可见性由台账完成).
"""
from __future__ import annotations

import logging
import time

from ..agent.reminders import SystemReminder
from ..vault.db import chat_abort_records

logger = logging.getLogger("md-vault.bgagent")

WINDOW_S = 7 * 86400   # §4: 近 7 天，快照式报告（窗口重叠，无消费水位线）
MAX_SIGNALS = 50       # bounded injection (single-user weekly volume is
                       # single-digit; the cap is prompt-shape hygiene)


class InterruptSignalsSystemReminder(SystemReminder):
    """Recent chat-abort signals + the sessions-join material, rendered
    once at Agent init into a ``<system-reminder>`` message (skipped when
    None/empty — the data gate)."""

    def __init__(self, alias: str, ledger_conn=None):
        self._alias = alias
        self._conn = ledger_conn
        # injection audit payload for the runner's synthetic egress row
        # (bytes of the rendered body / signals injected, both 0 = silent)
        self.last_bytes = 0
        self.last_signals = 0

    def render(self) -> str | None:
        self.last_bytes = 0
        self.last_signals = 0
        if self._conn is None:
            return None  # degraded no-conn run (card 11 shape): stay silent
        try:
            body = self._render()
        except Exception:
            # fail-open（仿 readback 各节降级）：信号读取炸了不得咬 run
            logger.warning("[bg:%s] interrupt-signal readback failed",
                           self._alias, exc_info=True)
            return None
        if body:
            self.last_bytes = len(body.encode("utf-8"))
        return body

    def _render(self) -> str | None:
        rows = chat_abort_records.recent(self._conn,
                                         since=time.time() - WINDOW_S)
        if not rows:
            return None
        self.last_signals = min(len(rows), MAX_SIGNALS)

        lines: list[str] = [
            "## 近期取消信号（chat_abort_log · 近 7 天）",
            "以下每条是一次用户停止（stop）：**编号/ts/session/phase/tool/model 是服务端盖章的"
            "行为事实**；「中断轮原文 / 中断轮思考 / 停后补发」是从会话存储 join 出的内容——"
            "原文与补发是用户写的话，**思考是模型宣称（非证据）**，不得作为中断归因（评审报告须分开表述）。",
        ]
        sessions: dict[str, list] = {}
        for r in rows[:MAX_SIGNALS]:
            stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["ts"]))
            tool = f" tool={r['tool_name']}" if r["tool_name"] else ""
            lines.append(
                f"- #{r['id']} {stamp} session={r['session_id']} "
                f"phase={r['phase']}{tool} model={r['model']}")
            if r["message_id"] is None:
                lines.append(
                    "  （无锚点信号：空白停止或等锁期取消——无内容可 join，不猜）")
                continue
            anchor, follow = self._join(self._conn, sessions,
                                        r["session_id"], r["message_id"])
            if anchor is not None:
                lines.append(f"  中断轮原文（半截正文，行为事实的载体）: {anchor[0]!r}")
                if anchor[1]:
                    lines.append(f"  中断轮思考（模型宣称）: {anchor[1]!r}")
            if follow is not None:
                lines.append(f"  停后补发（用户下一条）: {follow!r}")
        if len(rows) > MAX_SIGNALS:
            lines.append(f"- …另有 {len(rows) - MAX_SIGNALS} 条未列出")
        return "\n".join(lines)

    @staticmethod
    def _join(conn, cache: dict, session_id: str, message_id: str):
        """Anchor + follow-up lookup in the session blob (cached per run —
        a snapshot read, blob order is causal order, never sorted).

        Returns ``(anchor, follow)``: anchor = ``(content, reasoning)`` of
        the interrupted row or None (session deleted / row trimmed —
        report the signal bare); follow = the first real post-stop user
        message or None (still unanswered).
        """
        from ..vault.db.sessions import _REMINDER_PREFIX, load_session

        if session_id not in cache:
            saved = load_session(conn, session_id)
            cache[session_id] = saved["messages"] if saved else []
        msgs = cache[session_id]
        idx = next((i for i, m in enumerate(msgs)
                    if m.get("message_id") == message_id), None)
        if idx is None:
            return None, None
        anchor = msgs[idx]
        follow = None
        for m in msgs[idx + 1:]:
            if m.get("role") == "user":
                content = str(m.get("content", ""))
                if content.startswith(_REMINDER_PREFIX):
                    continue  # 注入提醒行不是用户补发
                follow = content
                break
        return (str(anchor.get("content") or ""),
                str(anchor.get("reasoning") or "")), follow
