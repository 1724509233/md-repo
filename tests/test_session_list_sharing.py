"""回归钉（会话 list 共享契约，2026-11-20）：persist_user 曾因为 Agent 的
messages list 与 SessionManager 缓存分家而用陈旧空缓存覆写库、抹掉历史。

Repro of the reported bugs "stop chat, resend -> history invisible until done"
+ "点赞点踩 feedback 404" (2026-11-20, same root cause). Chain (pre-fix):
new session -> SessionManager cache list is [] -> Agent.__init__
`if not messages:` REPLACED it with its own [system,...] list -> cache stayed
[] forever -> every persist_user(sid) saved list(cache)+[user] = [user] only
-> snapshot frames read DB = [user] -> history gone; and the feedback handler
reading get_or_create() saw the stale empty cache -> "message not found".

Fixed in server/agent/agent.py: the empty passed list is seeded IN PLACE
(identity preserved, see test_agent_empty_shared_list_seeded_in_place). This
pin keeps the whole downstream chain green.
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace

from server.agent.agent import Agent
from server.agent.session import SessionManager
from server.vault.db.schema import init_db
from server.vault.db.sessions import load_session


def make_conn(tmp: Path) -> sqlite3.Connection:
    return init_db(tmp / "sessions.db")


def spec_stub():
    return SimpleNamespace(system_prompt="sys", system_reminders=[], user_reminders=[])


def test_no_divergence_no_clobber():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        conn = make_conn(tmp)
        sm = SessionManager(conn, tmp)

        # 1. user clicks 新会话
        sid = sm.create_new()

        # 2. first chat builds the Agent from the (empty) cache list
        cached = sm.get_or_create(sid)
        agent = Agent(spec_stub(), messages=cached, session_id=sid)
        # FIX 1: empty list seeded in place — the cache object IS still the
        # agent's list (identity is the sharing contract).
        assert agent.messages is cached

        # 3. run round 1: agent appends; round-end persist writes DB correctly
        agent.messages.extend([
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
        ])
        sm.save(sid, agent.messages)
        assert len(load_session(conn, sid)["messages"]) == 3  # sys + q1 + a1

        # 4. user interrupts (stop): terminal persist — still full
        agent.messages.append({"role": "user", "content": "q2"})
        agent.messages.append({"role": "assistant", "content": "half", "interrupted": True})
        sm.save(sid, agent.messages)
        assert len(load_session(conn, sid)["messages"]) == 5

        # 5. RESEND: RunHub._drive dequeue-time persist_user (wiring verbatim:
        #    routes/agent.py persist_user lambda)
        sm.save(sid, list(sm.get_or_create(sid)) + [{"role": "user", "content": "q3"}])

        # FIX 2: the DB row still holds the FULL history + the new user row —
        # no clobber. (Pre-fix this was a lone user row.)
        db_msgs = load_session(conn, sid)["messages"]
        assert [m["content"] for m in db_msgs if m.get("role") == "user"] == \
            ["q1", "q2", "q3"], f"CLOBBER REGRESSION, db={db_msgs}"

        # 6. snapshot frame source reads exactly this: history intact
        saved = load_session(conn, sid)
        snapshot_prefix = saved["messages"] if saved else []
        assert len([m for m in snapshot_prefix if m.get("role") == "user"]) == 3

        # 7. round end persist -> full history (7 rows)
        agent.messages.append({"role": "user", "content": "q3"})
        agent.messages.append({"role": "assistant", "content": "a3"})
        sm.save(sid, agent.messages)
        assert len(load_session(conn, sid)["messages"]) == 7

        conn.close()
