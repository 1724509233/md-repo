"""OpenAI-compatible mock LLM for memory-runtime Tier-2 functional tests.

Scripted playbooks, selected by the request's system prompt:

- system prompt contains ``sentinel`` → **sentinel playbook**
  - turn 0: tool_call ``recall_stats`` {"days": 30}   (bypass recall read)
  - turn 1: text — read-only governance report per server/prompts/sentinel.md
    (v2: the archive verb is retired, the sentinel files no proposals; the
    report names the cold docs and says the move is a human drag in Obsidian)
- otherwise → **consolidator playbook**
  - turn 0: tool_call ``recent_changes`` (in the default full-tool registry;
    all its args are optional: path/since/source/limit)
  - turn 1: tool_call ``propose`` → a type: summary (form: cache) proposal
    per server/prompts/consolidator.md (v2: write_doc into suggestions/ is
    gated — the propose tool is the channel; state lands in the ledger DB)
  - turn 2: Chinese summary text

Wire format: the agent runtime (server/agent/runtime.py) calls
``client.chat.completions.create(stream=True, stream_options={"include_usage":
True})`` — so responses are SSE chunks in the OpenAI chat-completions schema
(delta.content / delta.tool_calls / finish_reason, trailing usage chunk +
``data: [DONE]``). Verified against openai SDK 3.26.0.

Server:  stdlib http.server only (no fastapi/uvicorn dependency).
        ``python -m tests.functional.mock_llm --port 11435``
Self-check (no server, prints both playbooks' request→response sequences):
        ``python -m tests.functional.mock_llm --once``

Playbook constants (must match the scratch-vault seeds, see spec §5 smoke):
- sentinel reports on ``notes/cold-doc.md`` (read-only) — seed it into the vault.
- consolidator's summary references the same doc as its source_ref.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MOCK_MODEL = "mock"
RUN_ID = f"batch-{datetime.now():%Y%m%d-%H%M}"   # per process, sentinel.md: batch-<YYYYMMDD-HHMM>
TODAY = datetime.now().strftime("%Y-%m-%d")

COLD_DOC = "notes/cold-doc.md"                # 观测对象：哨兵判冷报告对象（scratch vault 须已 seed）
CONSOL_SOURCE = "notes/cold-doc.md"           # consolidator summary 的 source_ref

# v2a card naming: YYYY-MM-DD_<type>_p<id>.md (id = 记录主键; 合成标识, 无文件).
_CONSOL_PROPOSAL_PATH = f"suggestions/{TODAY}_summary_p1.md"


def sentinel_report_text() -> str:
    """sentinel.md「结束」段要求的只读治理报告（v2：无提案，无写动作）。"""
    return (
        f"检查了近 30 天召回遥测（human/on_demand/synthetic 三分流量，半衰期 30d）：\n"
        f"- 判冷对象 {COLD_DOC}：human 0 / on_demand 0 / synthetic 0（seed 数据，"
        f"时间衰减加权 0）。\n"
        f"- 失败查询对账：召回账本中无与该文档相似的失败查询（不存在「想找到却"
        f"没找到」的缺口），判定真冷（非检索质量缺口）。\n"
        f"- 建议归档清单（≤3 条，宁缺毋滥）：{COLD_DOC}（判冷依据：窗口内零命中 + "
        f"无相似失败查询；90 天零召回，functional-test scripted run）。\n"
        f"搬动 = 人在 Obsidian 把它拖到 archive/（v2：哨兵只读，不产提案）。"
        f"未发现其他 query gap。"
    )


def consolidator_propose_args() -> dict:
    """consolidator.md 模板：propose 通道的 summary（cache 形态）调用参数。

    actor / run_id / status 由系统填写；summary 双形态中 cache = 对用户自己
    文字的可再生有损压缩（body = 决策记录草稿，不是复述）。
    """
    stem = CONSOL_SOURCE.removeprefix("notes/").removesuffix(".md")
    return {
        "type": "summary",
        "payload": {
            "form": "cache",
            "body": (
                "决策记录草稿：cold-doc 承载一条值得留存的部署结论"
                "（scripted：内容可从原文笔记再生）。采纳后执行器落机器区 "
                f"index/summaries/{stem}-summary.md。"
            ),
        },
        "source_refs": [CONSOL_SOURCE],
        "justification": "对账近期人类通道 create 事件后蒸馏；正文是决策记录草稿，"
                         "不是复述（consolidator.md 纪律）。",
    }


# ── playbook scripting ────────────────────────────────────────────────────

def _script(role: str, turn: int) -> tuple[str, str, dict] | str:
    """Return ("tool", name, args) for tool turns, or summary text once the
    script's tool turns are exhausted (v2: the sentinel stops at turn 1 with
    a read-only report — it files no proposals)."""
    if role == "sentinel":
        if turn == 0:
            return ("tool", "recall_stats", {"days": 30})
        return sentinel_report_text()
    if turn == 0:
        return ("tool", "recent_changes", {"source": "web", "since": "24h", "limit": 50})
    if turn == 1:
        return ("tool", "propose", consolidator_propose_args())
    return ("对账了近期人类通道 create 事件，固化 1 条碎片，产出 1 条 summary 提案"
            f"（{_CONSOL_PROPOSAL_PATH}，awaiting_review）。对账命中 0 条旧情景。")


def _tool_turns(role: str) -> int:
    n = 0
    while isinstance(_script(role, n), tuple):
        n += 1
    return n


def _detect_role(messages: list[dict]) -> str:
    system = " ".join(
        str(m.get("content") or "") for m in messages if m.get("role") == "system")
    return "sentinel" if "sentinel" in system.lower() else "consolidator"


def _turn_count(messages: list[dict]) -> int:
    """Completed assistant turns in the request = scripted round number."""
    return sum(1 for m in messages if m.get("role") == "assistant")


# ── SSE wire format ───────────────────────────────────────────────────────

def _chunk(created: int, choices: list) -> str:
    body = {"id": "chatcmpl-mock", "object": "chat.completion.chunk",
            "created": created, "model": MOCK_MODEL, "choices": choices}
    return json.dumps(body, ensure_ascii=False)


def _sse_stream(kind: str, name: str | None, args: dict | None, text: str) -> str:
    """Build the full SSE body the agent runtime's streaming client expects."""
    created = int(time.time())
    lines: list[str] = []
    if kind == "tool":
        args_json = json.dumps(args, ensure_ascii=False)
        lines.append(_chunk(created, [{
            "index": 0, "delta": {"role": "assistant", "content": None},
            "finish_reason": None}]))
        lines.append(_chunk(created, [{
            "index": 0,
            "delta": {"tool_calls": [{
                "index": 0, "id": "call_mock_1", "type": "function",
                "function": {"name": name, "arguments": args_json}}]},
            "finish_reason": None}]))
        lines.append(_chunk(created, [{"index": 0, "delta": {},
                                       "finish_reason": "tool_calls"}]))
        usage = {"prompt_tokens": 1200, "completion_tokens": 40, "total_tokens": 1240}
    else:
        lines.append(_chunk(created, [
            {"index": 0, "delta": {"role": "assistant", "content": None},
             "finish_reason": None}]))
        lines.append(_chunk(created, [
            {"index": 0, "delta": {"content": text}, "finish_reason": None}]))
        lines.append(_chunk(created, [{"index": 0, "delta": {}, "finish_reason": "stop"}]))
        usage = {"prompt_tokens": 1200, "completion_tokens": len(text),
                 "total_tokens": 1200 + len(text)}
    lines.append(json.dumps(
        {"id": "chatcmpl-mock", "object": "chat.completion.chunk", "created": created,
         "model": MOCK_MODEL, "choices": [], "usage": usage}, ensure_ascii=False))
    return "".join(f"data: {ln}\n\n" for ln in lines) + "data: [DONE]\n\n"


def respond(messages: list[dict]) -> str:
    """Full SSE body for one /v1/chat/completions request (shared by server
    and --once so both print/serve exactly the same bytes)."""
    role = _detect_role(messages)
    turn = _turn_count(messages)
    spec = _script(role, turn)
    if isinstance(spec, str):
        return _sse_stream("text", None, None, spec)
    _, name, args = spec
    return _sse_stream("tool", name, args, "")


# ── HTTP server ───────────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 (http.server API)
        if self.path.rstrip("/") != "/v1/chat/completions":
            body = json.dumps({"error": "not found"}).encode()
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        length = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self.send_response(400)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")
            return

        messages = req.get("messages") or []
        role = _detect_role(messages)
        turn = _turn_count(messages)
        sse = respond(messages)
        print(f"[mock] role={role} turn={turn} model={req.get('model')} "
              f"stream={req.get('stream')} tools={len(req.get('tools') or [])}",
              file=sys.stderr)
        payload = sse.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args) -> None:  # quieter: 1 line per request
        print(f"[mock] {self.address_string()} {fmt % args}", file=sys.stderr)


def serve(host: str, port: int) -> None:
    httpd = ThreadingHTTPServer((host, port), _Handler)
    print(f"[mock] serving {MOCK_MODEL} on http://{host}:{port} "
          f"(run_id={RUN_ID} cold_doc={COLD_DOC}); Ctrl-C to stop",
          file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


# ── --once self-check: print both playbooks' request→response sequences ──

def _brief(m: dict, limit: int = 120) -> str:
    role = m.get("role")
    if role == "tool":
        return f"tool[{m.get('tool_call_id')}]: {str(m.get('content'))[:limit]}"
    content = m.get("content")
    if isinstance(content, list):  # not expected from this client; keep safe
        content = "".join(str(c) for c in content)
    out = str(content)
    if role == "assistant" and m.get("tool_calls"):
        tcs = ", ".join(
            f"{tc['function']['name']}({tc['function']['arguments'][:60]})"
            for tc in m["tool_calls"])
        out = f"{out} tool_calls[{tcs}]"
    if len(out) > limit:
        out = out[:limit] + "…"
    return f"{role}: {out}"


def _simulate(role: str) -> list[dict]:
    """Replay the client's message history as the runtime would build it."""
    system = (f"# Role: {role}（mock 角色探针）"
              + "\n你是 sentinel（记忆哨兵）。" if role == "sentinel"
              else "你是 consolidator（记忆固化者）。")
    msgs = [{"role": "system", "content": system},
            {"role": "user", "content": f"开始整理 suggestions/"}]
    tool_results = {
        "sentinel": {
            "recall_stats": ("Recall traffic (window: 30d, half-life: 30d)\n"
                             f"{COLD_DOC} | 0 | 0 | 0 | 0.0 | 30d ago"),
        },
        "consolidator": {
            "recent_changes": "14:05 web       create   notes/cold-doc.md",
            "propose": "proposal #1 created → "
                       f"{_CONSOL_PROPOSAL_PATH} (status: awaiting_review)",
        },
    }[role]
    turn = 0
    while True:
        spec = _script(role, turn)
        if isinstance(spec, str):
            break
        _kind, name, args = spec
        tc_id = f"call_mock_{turn + 1}"
        msgs.append({"role": "assistant", "content": None, "tool_calls": [
            {"id": tc_id, "type": "function",
             "function": {"name": name,
                          "arguments": json.dumps(args, ensure_ascii=False)}}]})
        msgs.append({"role": "tool", "tool_call_id": tc_id,
                     "content": tool_results[name]})
        turn += 1
    return msgs


def run_once_selfcheck() -> int:
    for role in ("sentinel", "consolidator"):
        print(f"════ {role} playbook ════ (run_id={RUN_ID})")
        full = _simulate(role)          # [system, user] + n×(assistant, tool)
        turns = _tool_turns(role) + 1   # + 1: the final text turn
        for turn in range(turns):
            base = full[: 2 + 2 * turn]  # request as it stands at this turn
            print(f"── turn {turn} ──")
            for m in base:
                print(f"  req  { _brief(m) }")
            body = respond(base)
            for ln in body.splitlines():
                if ln.startswith("data: "):
                    data = ln[6:]
                    if data == "[DONE]":
                        print("  resp data: [DONE]")
                    else:
                        o = json.loads(data)
                        ch = o.get("choices") or [{}]
                        delta = (ch[0] or {}).get("delta") or {}
                        if "usage" in o and not ch:
                            print(f"  resp usage: {o['usage']}")
                        elif delta.get("tool_calls"):
                            tc = delta["tool_calls"][0]
                            fn = tc.get("function") or {}
                            args = fn.get("arguments") or ""
                            print(f"  resp tool_call: {fn.get('name')}({args})")
                        elif delta.get("content"):
                            print(f"  resp content: {delta['content']!r}")
                        elif (ch[0] or {}).get("finish_reason"):
                            pass  # role/finish-only chunk
            print()
    print("self-check done: sequences above are byte-identical to what the "
          "server sends (respond() shared).")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m tests.functional.mock_llm",
        description="OpenAI-compatible scripted mock (sentinel/consolidator).")
    ap.add_argument("--port", type=int, default=11435)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--once", action="store_true",
                    help="no server: print both playbooks' request→response sequences")
    args = ap.parse_args(argv)
    if args.once:
        return run_once_selfcheck()
    serve(args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
