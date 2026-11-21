"""Interrupt-signal injection as a SystemReminder (memory-pipeline spec
2026-11-21-interrupt-memory-pipeline §1 ① 消费层, on the channel-2 join
contract of 2026-11-20-stop-signals-reasoning-capture §4) — the first
consumer of ``chat_abort_log`` (「用户写的提示词，被用户中断过的每一轮都在
替它压测」).

Mount shape: the runner gates this by HARDCODED role (spec §6 S1 r1.6 用户裁
——通道二「挂点全局无 alias 特判」旧裁废止): only ``_INTERRUPT_ROLES``
(abort-analyzer) builds this reminder; other bgagents never construct it
(零注入、零 egress 行). Inside the role the data gate still self-holds:
no conn / no admitted signal in the 7-day window → ``render()`` None →
zero injection.

Admission gate (§1 准入门, r1 用户裁 — 本层核心增量):
- 入选 = 锚行（``message_id`` 非空）之后、后窗范围内存在真实 user 行
  （``[当前打开:`` 框架行不算）。「停后再无下文」不可归因（r9 同构），
  整条不注入；无锚行（空白停止/等锁取消）一律不注入（r1.5 blob 契约 ④
  ——旧「无锚点信号」渲染分支废止）。

Window assembly (§1 前后窗, blob 数组序即因果序, 永不 sort):
- 前窗 = 锚行前最近 ``WINDOW_TURNS`` 轮；一轮 = 连续 user 行段（连发算
  一段）+ 其后 assistant 行。user 行全文；assistant content 截
  ``ASSISTANT_CLIP`` 字（content ``None``/``""`` 按空跳过）；工具结果体
  与工具调用参数不注入（体积面，备案防重新发明）。
- 后窗 = 锚行后连续 user 行全集直到下一个非 user 边界（覆盖连发增补）。
- 锚行自身（半截文 + reasoning）只以锚身份出现一次。

Byte hard-cap (§1 r1.5 M6): rendered body ≤ ``MAX_INJECT_BYTES``; fixed
degradation order = drop reasoning → shrink pre-window turn depth → drop
signal count (keep newest, cut oldest); cut sites carry a 「[truncated]」
marker line. ``last_bytes`` is always the actually-injected byte count.

``MAX_SIGNALS`` keeps the NEWEST 50 (S6: id is the monotonic provenance
stamp; the old ``rows[:50]`` in session order dropped the newest signals).

防重② (§5, M5): each signal line is suffixed with its proposal state
(已出提案#N / 已被拒 / 落地失败: reason) resolved via
``proposals.scan`` + a PYTHON filter — the ``source_refs`` column stores
JSON array text, so an SQL LIKE would silently miss (零 SQL 出 db 层).
Fail-open: proposals unreadable → no annotations.

Facts vs claims (§4 报告纪律): the row's stamped columns are behavioral
FACTS; the joined content/reasoning are the model's CLAIMS — not
evidence. The labels ride the injected text itself. 零裁决：本模块只拼
原始事实，不出现放弃/修正/转向等语义标签——分类全归②层 agent。
"""
from __future__ import annotations

import logging
import time

from ..agent.reminders import SystemReminder
from ..vault.db import chat_abort_records

logger = logging.getLogger("md-vault.bgagent")

WINDOW_S = 7 * 86400      # §4: 近 7 天，快照式报告（窗口重叠，无消费水位线）
MAX_SIGNALS = 50          # bounded injection — keeps the NEWEST 50 (S6)
WINDOW_TURNS = 3          # 前窗轮深（spec §1 r1.4：硬编码常量，不开 config 键）
ASSISTANT_CLIP = 500      # 前窗 assistant 行截断字数（user 行全文）
MAX_INJECT_BYTES = 24576  # 24KB：reminder 全文节字节硬帽（r1.5 M6，qwen2.5:14b 32K 档余量）

_TRUNC = "[truncated]"


class InterruptSignalsSystemReminder(SystemReminder):
    """Admitted chat-abort signals with pre/post-window join material,
    rendered once at Agent init into a ``<system-reminder>`` message
    (skipped when None/empty — the data gate)."""

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

    # ── rendering ────────────────────────────────────────────────────────

    def _render(self) -> str | None:
        rows = chat_abort_records.recent(self._conn,
                                         since=time.time() - WINDOW_S)
        if not rows:
            return None
        # S6 保最新：id 是全局单调 provenance 戳（AUTOINCREMENT），按 id 留
        # 最新 MAX_SIGNALS 条（读侧全量后切片，ORDER BY 语义不动）；行仍按
        # 会话内因果序渲染。
        dropped_by_cap = 0
        if len(rows) > MAX_SIGNALS:
            keep = {r["id"] for r in sorted(rows, key=lambda r: r["id"])[-MAX_SIGNALS:]}
            dropped_by_cap = len(rows) - len(keep)
            rows = [r for r in rows if r["id"] in keep]

        notes = self._proposal_marks()
        sessions: dict[str, list] = {}
        signals: list[dict] = []
        for r in rows:
            if r["message_id"] is None:
                continue  # 无锚行一律不注入（r1.5 blob 契约 ④，S7）
            blob = self._blob(sessions, r["session_id"])
            idx = next((i for i, m in enumerate(blob)
                        if m.get("message_id") == r["message_id"]), None)
            if idx is None:
                continue  # 会话已删/行已修剪：锚不可得 → 下文无从判定，不注入
            post = self._post_window(blob, idx)
            if not post:
                continue  # 准入门：停后无下文（或只有框架行）→ 整条不注入
            signals.append({
                "id": r["id"],
                "head": self._head(r, notes.get(r["id"])),
                "pre_blocks": self._pre_blocks(blob, idx),
                "anchor_lines": self._anchor_lines(blob[idx]),
                "post_lines": [f"  停后补发（用户连发全集）: {t!r}" for t in post],
            })
        if not signals:
            return None

        header = [
            "## 近期取消信号（chat_abort_log · 近 7 天）",
            "以下每条是一次用户停止（stop），各带锚行前后窗口：**编号/ts/session/phase/tool/model "
            "是服务端盖章的行为事实**；窗口内容是从会话存储 join 出的原文——"
            "用户行是用户写的话，**中断轮思考是模型宣称（非证据）**，不得作为中断归因（报告须分开表述）。",
        ]
        overflow = dropped_by_cap
        # 固定降级顺序（r1.5 M6，逐级累加、不回头）：先砍 reasoning →
        # 再砍前窗轮深（3→0）→ 最后砍信号条数（保最新砍最旧）。
        # 被砍处在 _signal_lines / 截断标记行内留「[truncated]」。
        total = len(signals)
        states = [(False, WINDOW_TURNS, 0), (True, WINDOW_TURNS, 0)]
        states += [(True, d, 0) for d in range(WINDOW_TURNS - 1, -1, -1)]
        states += [(True, 0, cut) for cut in range(1, total)]
        final = None
        for drop_reasoning, depth, cut in states:
            injected = signals[cut:]
            lines = list(header)
            dropped = overflow + cut
            if dropped:
                lines.append(f"- …{_TRUNC} 另有 {dropped} 条最旧信号未列出")
            for sig in injected:
                lines += self._signal_lines(sig, drop_reasoning, depth)
            body = "\n".join(lines)
            final = (body, len(injected))
            if len(body.encode("utf-8")) <= MAX_INJECT_BYTES:
                self.last_signals = len(injected)
                return body
        # 降级穷尽仍超帽（单条信号的用户全文自身 > 帽）：如实注入最新一条
        # （user 全文是窗口契约不可再砍；last_bytes 恒实注字节数，台账诚实）。
        self.last_signals = final[1]
        return final[0]

    @staticmethod
    def _signal_lines(sig: dict, drop_reasoning: bool, depth: int) -> list[str]:
        lines = [sig["head"]]
        blocks = sig["pre_blocks"]
        if blocks:
            hidden = len(blocks) - depth
            if depth > 0:
                lines.append(f"  停前文（最近 {depth} 轮；一轮=连续 user 段+其后 agent 行，"
                             f"agent 行截 {ASSISTANT_CLIP} 字）:")
            if hidden > 0:
                lines.append(f"    {_TRUNC}")
            for users, assistants in blocks[-depth:] if depth > 0 else []:
                for u in users:
                    lines.append(f"    user: {u!r}")
                for a in assistants:
                    lines.append(f"    agent: {a!r}")
        anchors = sig["anchor_lines"]
        if drop_reasoning:
            # 降级第一级：reasoning 可整字段砍掉，砍处留截断标记
            kept = [l for l in anchors if not l.startswith("  中断轮思考")]
            if len(kept) != len(anchors):
                kept.append(f"  {_TRUNC}")
            lines += kept
        else:
            lines += anchors
        lines += sig["post_lines"]
        return lines

    @staticmethod
    def _head(r: dict, mark: str | None) -> str:
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["ts"]))
        tool = f" tool={r['tool_name']}" if r["tool_name"] else ""
        head = (f"- #{r['id']} {stamp} session={r['session_id']} "
                f"phase={r['phase']}{tool} model={r['model']}")
        return f"{head} {mark}" if mark else head

    @staticmethod
    def _anchor_lines(anchor: dict) -> list[str]:
        """锚行只以锚身份出现一次（半截文 + reasoning）。"""
        lines = [f"  中断轮原文（半截正文，行为事实的载体）: {str(anchor.get('content') or '')!r}"]
        reasoning = str(anchor.get("reasoning") or "")
        if reasoning:
            lines.append(f"  中断轮思考（模型宣称）: {reasoning!r}")
        return lines

    # ── windows (blob 数组序即因果序, 永不 sort) ─────────────────────────

    def _blob(self, cache: dict, session_id: str) -> list:
        """Session message list, cached per run (snapshot read)."""
        from ..vault.db.sessions import load_session

        if session_id not in cache:
            saved = load_session(self._conn, session_id)
            cache[session_id] = saved["messages"] if saved else []
        return cache[session_id]

    @staticmethod
    def _is_framework_user(m: dict) -> bool:
        """``[当前打开:`` 框架行（agent.py 注入）：不是用户话语——准入门
        「存在 user 行」判定与前窗轮计数一律跳过（blob 契约 ①）。"""
        from ..vault.db.sessions import REMINDER_PREFIX

        return (m.get("role") == "user"
                and str(m.get("content", "")).startswith(REMINDER_PREFIX))

    @classmethod
    def _post_window(cls, blob: list, idx: int) -> list[str]:
        """锚行后连续 user 行全集直到下一个非 user 边界（框架行跳过不计数
        也不封段）。返回 [] = 无下文（准入门拒）。"""
        out: list[str] = []
        for m in blob[idx + 1:]:
            if m.get("role") != "user":
                break
            if cls._is_framework_user(m):
                continue
            out.append(str(m.get("content", "")))
        return out

    @classmethod
    def _pre_blocks(cls, blob: list, idx: int) -> list[tuple[list[str], list[str]]]:
        """锚行前最近 WINDOW_TURNS 轮（oldest→newest）。一轮 = 连续 user 行
        段 + 其后 assistant 行（content None/"" 按空跳过，tool_calls 中间行
        与工具结果体不注入）；锚行所在轮即末轮（其 assistant 面就是锚行，
        只渲染其 user 段——锚不重复）。"""
        turns: list[tuple[list[str], list[str]]] = []
        cur_users: list[str] = []
        cur_asst: list[str] = []
        for m in blob[:idx]:
            role = m.get("role")
            if role == "user":
                if cls._is_framework_user(m):
                    continue
                if cur_asst:
                    # 新 user 段开新轮；先封上一轮
                    if cur_users:
                        turns.append((cur_users, cur_asst))
                    cur_users, cur_asst = [], []
                cur_users.append(str(m.get("content", "")))
            elif role == "assistant":
                content = m.get("content")
                if content is None or str(content) == "":
                    continue  # tool_calls 中间行 / 双空白归一行（blob 契约 ②）
                text = str(content)
                cur_asst.append(text[:ASSISTANT_CLIP])
        if cur_users or cur_asst:
            turns.append((cur_users, cur_asst))
        return turns[-WINDOW_TURNS:]

    # ── 防重②：提案状态标注（proposals.scan + Python 过滤，M5）──────────

    def _proposal_marks(self) -> dict[int, str]:
        """abort 信号 id → 标注文本。``source_refs`` 列是 JSON 数组文本，
        ``_row_to_record`` 已解析回 list——**禁止 SQL LIKE**（字面恒假）。
        匹配 = actor == 本角色 ∧ refs 含 ``abort_log:{id}`` 项。
        fail-open：proposals 不可用 → 无标注。"""
        try:
            from ..services import proposals
            recs = proposals.scan(self._conn)
        except Exception:
            logger.warning("[bg:%s] proposal marks scan failed (fail-open, "
                           "no annotations)", self._alias, exc_info=True)
            return {}
        latest: dict[int, dict] = {}
        for rec in recs:
            if rec.get("actor") != self._alias:
                continue
            for ref in rec.get("source_refs") or []:
                s = str(ref)
                if not s.startswith("abort_log:"):
                    continue
                try:
                    sig_id = int(s.split(":", 1)[1])
                except ValueError:
                    continue
                # scan 按 id ASC——后见覆盖先见 = 取该信号最新一条提案
                latest[sig_id] = rec
        marks: dict[int, str] = {}
        for sig_id, rec in latest.items():
            status = rec.get("status")
            if status == "rejected":
                marks[sig_id] = "已被拒"
            elif status == "failed":
                reason = str(rec.get("reject_reason") or "")[:40]
                marks[sig_id] = f"已出提案#{rec['id']}（落地失败: {reason}）"
            else:
                marks[sig_id] = f"已出提案#{rec['id']}"
        return marks
