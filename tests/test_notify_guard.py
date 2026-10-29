"""Notification volume guards (v2a debt #6, D33): daily cap + same-kind merge.

GuardedNotificationHook writes through
``vault.db.notifications.guarded_insert`` (card 5: the constructor no longer
takes an inner hook — the count-then-act decision AND the write run in ONE
lock inside vault.db). Over the local-day cap the original row is not
inserted — a single kind='collapsed' summary row is upserted instead (count
accumulates); same-alias review rows whose proposal_paths intersect the day's
existing set are skipped (the console proposal face already shows the full
set). daily_cap is law from config.yaml (notifications.daily_cap, default
20) — the framework does not hardcode it.
"""
from __future__ import annotations

import inspect
import threading
import time

from server.bgagent.notify import GuardedNotificationHook, RunReport


def _report(**kw) -> RunReport:
    base = dict(
        alias="sentinel", name="哨兵", status="ok",
        started_at=time.time() - 300, finished_at=time.time(),
        report_path="index/reports/sentinel/2026-10-28.md",
        ops_total=1, files_changed=1, summary="s",
    )
    base.update(kw)
    return RunReport(**base)


def _rows(db_conn):
    return db_conn.execute(
        "SELECT alias, kind, summary, proposal_paths FROM notifications"
        " ORDER BY id").fetchall()


def test_below_cap_inserts_each_row(db_conn):
    """未封顶：N < cap → 逐条插（回归：普通行形状与既有落库口径一致）。"""
    hook = GuardedNotificationHook(db_conn, daily_cap=5)
    for i in range(3):
        hook.notify(_report(alias=f"a{i}"))
    rows = _rows(db_conn)
    assert [r["alias"] for r in rows] == ["a0", "a1", "a2"]
    assert all(r["kind"] != "collapsed" for r in rows)
    # 普通行经 vault.db.notifications 落库（空数组 → '' 口径同既有 proposal_paths）
    hook.notify(_report(alias="a3", report_paths=["index/reports/sentinel/2026-10-28.md"]))
    row = _rows(db_conn)[-1]
    assert row["proposal_paths"] == ""


def test_at_cap_collapses_and_count_accumulates(db_conn):
    """封顶：插到 cap 后再 notify → 无新普通行 + 恰一条 kind='collapsed'，
    计数逐次累加（summary 内 {n} 递增，折叠行不重复）。"""
    hook = GuardedNotificationHook(db_conn, daily_cap=2)
    hook.notify(_report(alias="a"))
    hook.notify(_report(alias="b"))
    hook.notify(_report(alias="c"))   # count == 2 → 折叠 n=1
    hook.notify(_report(alias="d"))   # 再折叠 n=2（同一折叠行 UPDATE）

    rows = _rows(db_conn)
    normal = [r for r in rows if r["kind"] != "collapsed"]
    assert [r["alias"] for r in normal] == ["a", "b"]
    collapsed = [r for r in rows if r["kind"] == "collapsed"]
    assert len(collapsed) == 1
    assert collapsed[0]["alias"] == "*"
    assert "今日通知超上限（2）" in collapsed[0]["summary"]
    assert "其余 2 条已折叠" in collapsed[0]["summary"]


def test_same_kind_merge_intersect_skips(db_conn):
    """同类合并：同 alias 两 review 行 proposal_paths 相交 → 第二条跳过；
    不相交 → 两行。"""
    hook = GuardedNotificationHook(db_conn, daily_cap=10)
    hook.notify(_report(alias="sentinel", kind="review",
                        proposal_paths=["sug/a.md", "sug/b.md"]))
    hook.notify(_report(alias="sentinel", kind="review",
                        proposal_paths=["sug/b.md"]))          # 相交 → 跳过
    assert len(_rows(db_conn)) == 1

    hook.notify(_report(alias="sentinel", kind="review",
                        proposal_paths=["sug/c.md"]))          # 不相交 → 插入
    rows = _rows(db_conn)
    assert len(rows) == 2
    assert rows[1]["alias"] == "sentinel"


def test_batch_kind_never_merged(db_conn):
    """合并只作用于 kind='review'：batch 行不做 proposal_paths 比对。"""
    hook = GuardedNotificationHook(db_conn, daily_cap=10)
    hook.notify(_report(alias="narrator", kind="batch"))
    hook.notify(_report(alias="narrator", kind="batch",
                        proposal_paths=["sug/a.md"]))
    assert len(_rows(db_conn)) == 2


def test_boundary_cap_equal_row_count(db_conn):
    """边界：cap 恰好等于当日行数 → 第 cap 条仍插、其后的第一条折叠。"""
    hook = GuardedNotificationHook(db_conn, daily_cap=1)
    hook.notify(_report(alias="a"))   # count 0 < 1 → 插
    hook.notify(_report(alias="b"))   # count == 1 → 折叠
    rows = _rows(db_conn)
    assert [r["alias"] for r in rows if r["kind"] != "collapsed"] == ["a"]
    assert [r for r in rows if r["kind"] == "collapsed"][0]["alias"] == "*"


def test_count_window_is_local_day(db_conn):
    """计数口径 = 本地日窗（ts >= 当日 0 点）：两天前的旧行不占额度。"""
    hook = GuardedNotificationHook(db_conn, daily_cap=1)
    db_conn.execute(
        "INSERT INTO notifications (ts, alias, name, status, report_path) "
        "VALUES (?, 'old', 'old', 'ok', 'x.md')",
        (time.time() - 2 * 86400,))
    db_conn.commit()
    hook.notify(_report(alias="a"))   # 当日计数 0 < 1 → 插（旧行不占额度）
    rows = _rows(db_conn)
    assert [r["alias"] for r in rows] == ["old", "a"]
    assert all(r["kind"] != "collapsed" for r in rows)


def test_default_cap_is_20():
    """daily_cap 缺省 20（法律层缺省值；config.yaml 可覆盖）。"""
    sig = inspect.signature(GuardedNotificationHook.__init__)
    assert sig.parameters["daily_cap"].default == 20


def test_concurrent_cap_boundary_is_a_hard_cap(db_conn):
    """并发规则（card 5 行为变更：软 cap → 硬 cap）。

    ≥8 线程在 cap 边界并发 notify：count-then-act 与写入在同一把锁内，
    普通行 ≤ cap、恰一条 collapsed 行，折叠计数 = 溢出条数。旧实现
    （锁内判定、锁外委托 insert）并发下可双通过 —— 普通行可超 cap。
    这是 notify 域「并发规则不可违反」的唯一真正钉住。
    """
    cap = 3
    hook = GuardedNotificationHook(db_conn, daily_cap=cap)
    n_threads = 8
    errors: list[BaseException] = []

    def worker(i: int) -> None:
        try:
            hook.notify(_report(alias=f"t{i}"))
        except BaseException as e:  # pragma: no cover - race diagnostics
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    rows = _rows(db_conn)
    normal = [r for r in rows if r["kind"] != "collapsed"]
    collapsed = [r for r in rows if r["kind"] == "collapsed"]
    assert len(normal) <= cap
    assert len(collapsed) == 1
    # Exactly cap normal rows pass the check; every overflow lands in the
    # single collapsed row, counter = overflow count.
    assert len(normal) == cap
    assert f"其余 {n_threads - cap} 条已折叠" in collapsed[0]["summary"]
