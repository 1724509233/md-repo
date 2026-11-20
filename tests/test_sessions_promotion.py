"""sessions 表升格 ledger.db（spec 2026-11-20-sessions-ledger-promotion，r2）。

法源钉清单（spec §5）：

1. 迁移机制：有数据搬迁行数一致；幂等重跑空操作；「同 PK 异内容」→ kept-both
   拒绝 + 警告不炸启动；_ensure_session_cols 新列在 ledger 表生效；
   zombie 容忍（空/非空两态）——升级机器上 init_db 恰在 promote 之前刚建过
   空 sessions 表，空 zombie 直接改名让位，非空走 §2.2 裁决。
2. DDL 摘除根钉：init_db 重启 N 轮，index.db 永不再现 sessions 名。
3. 端到端不回归（路由跑真 ledger_conn 的本文件切片；chat→persist 全链
   由 test_agent_stream_routes / test_recall_feedback 全绿兜底）。
4. 恢复演练根钉：删 index.db 重建 → sessions 必须存活 + LEDGER_TABLES 含 sessions。
5. 审计静态钉：§3 接线清单各点用 ctx.ledger_conn；无 sessions 面上的
   index conn 残留。启动站位（main / mcp 双入口 promote + busy_timeout 顺手项）。
"""
from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path

import pytest

from server.vault.db.migrate_ledger import promote_sessions
from server.vault.db.schema import (
    _ensure_session_cols,
    ensure_ledger_tables,
    init_db,
    init_ledger_db,
)
from server.vault.db.sessions import load_session, save_session

REPO_ROOT = Path(__file__).resolve().parents[1]

SESSION_COLS = {"id", "messages", "token_count", "last_active", "created_at",
                "auto_approve"}


# ── helpers ─────────────────────────────────────────────────────────────────

def _tables(db_path: Path) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
    finally:
        conn.close()


def _make_pre_promotion_sessions(index_conn: sqlite3.Connection) -> None:
    """Pre-promotion index.db sessions 表形态（历史真实形状：5 列 CREATE +
    auto_approve 由 _ensure_session_cols ALTER 追加）。"""
    index_conn.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            messages TEXT NOT NULL,
            token_count INTEGER NOT NULL DEFAULT 0,
            last_active REAL NOT NULL,
            created_at REAL NOT NULL
        )
        """
    )
    index_conn.execute(
        "ALTER TABLE sessions ADD COLUMN auto_approve INTEGER NOT NULL DEFAULT 0"
    )


def _seed_session(index_conn: sqlite3.Connection, sid: str, msgs: list,
                  token_count: int = 0, auto_approve: int = 0) -> None:
    save_session(index_conn, sid, msgs, token_count=token_count,
                 auto_approve=auto_approve)


# ── §5-2 DDL 摘除根钉 ───────────────────────────────────────────────────────

def test_init_db_never_recreates_sessions_across_restarts(tmp_path):
    """C1 根钉：promote 后再跑 N 轮 init_db，index.db 永不再现 sessions 名
    （否则幂等前提恒假 → 行数校验恒不等 → 永久启动失败环）。"""
    p = tmp_path / "index.db"
    for _ in range(3):
        conn = init_db(p)
        conn.close()
    assert "sessions" not in _tables(p)
    src = inspect.getsource(init_db)
    # 精确钉：index DDL 面不许有 sessions 建表/补列（注释提及不算）
    assert "CREATE TABLE IF NOT EXISTS sessions" not in src
    assert "_ensure_session_cols" not in src


def test_ledger_ddl_owns_sessions_with_full_columns(tmp_path):
    """_LEDGER_DDL 含 sessions + 索引；列全集一致。"""
    p = tmp_path / "ledger.db"
    conn = init_ledger_db(p)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
    assert cols == SESSION_COLS
    idx = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index'"
        " AND tbl_name = 'sessions'")}
    assert "idx_sessions_active" in idx
    conn.close()


def test_ensure_session_cols_adds_auto_approve_on_ledger(tmp_path):
    """_ensure_session_cols 改跑在 ledger 表上：缺列的 sessions 表经
    ensure_ledger_tables 补齐 auto_approve；幂等可重复调。"""
    p = tmp_path / "ledger-legacy.db"
    conn = sqlite3.connect(p)
    conn.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            messages TEXT NOT NULL,
            token_count INTEGER NOT NULL DEFAULT 0,
            last_active REAL NOT NULL,
            created_at REAL NOT NULL
        )
        """
    )
    conn.commit()
    ensure_ledger_tables(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
    assert "auto_approve" in cols
    _ensure_session_cols(conn)  # 幂等：列已在，不再 ALTER
    conn.close()


# ── §5-1 迁移机制 ───────────────────────────────────────────────────────────

def test_promote_migrates_rows_and_renames_source(tmp_path, caplog):
    """非空 zombie + 空目标（常规首迁）：行数一致、内容等价、源表改名让位。"""
    index_path = tmp_path / ".sync" / "index.db"
    ledger_path = tmp_path / ".sync" / "ledger.db"
    index_conn = init_db(index_path)
    _make_pre_promotion_sessions(index_conn)
    _seed_session(index_conn, "a", [{"role": "user", "content": "旧话一"}],
                  token_count=5)
    _seed_session(index_conn, "b", [{"role": "user", "content": "旧话二"}],
                  auto_approve=1)
    before = {sid: load_session(index_conn, sid) for sid in ("a", "b")}

    report = promote_sessions(index_conn, ledger_path)
    assert report == "migrated"

    lconn = init_ledger_db(ledger_path)
    assert load_session(lconn, "a") == before["a"]
    assert load_session(lconn, "b") == before["b"]
    n = lconn.execute("SELECT count(*) FROM sessions").fetchone()[0]
    assert n == 2
    lconn.close()

    # 源表改名让位（藏表不删，回滚 = 改回名）
    tables = _tables(index_path)
    assert "sessions" not in tables
    assert "sessions_pre_ledger_migration" in tables
    index_conn.close()


def test_promote_idempotent_reruns_are_noops(tmp_path):
    """§2.3 幂等：完成态重跑 = skipped；数据与 index 面不变。"""
    index_path = tmp_path / ".sync" / "index.db"
    ledger_path = tmp_path / ".sync" / "ledger.db"
    index_conn = init_db(index_path)
    _make_pre_promotion_sessions(index_conn)
    _seed_session(index_conn, "a", [{"role": "user", "content": "hi"}])
    assert promote_sessions(index_conn, ledger_path) == "migrated"

    for _ in range(3):
        assert promote_sessions(index_conn, ledger_path) == "skipped"
    check = init_ledger_db(ledger_path)
    assert load_session(check, "a") is not None
    assert check.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1
    check.close()
    # zombie 也不许复活
    assert "sessions" not in _tables(index_path)
    index_conn.close()


def test_promote_kept_both_on_same_pk_diverged_content(tmp_path, caplog):
    """H1 裁决：目标非空（同 id 行内容已分叉）→ kept-both——警告 + 跳过迁移
    + 继续启动（不炸），两侧副本原样保留，人工裁决哪份为准。"""
    index_path = tmp_path / ".sync" / "index.db"
    ledger_path = tmp_path / ".sync" / "ledger.db"
    index_conn = init_db(index_path)
    _make_pre_promotion_sessions(index_conn)
    _seed_session(index_conn, "s1", [{"role": "user", "content": "index 版"}])
    lconn = init_ledger_db(ledger_path)
    save_session(lconn, "s1", [{"role": "user", "content": "ledger 版"}])
    lconn.close()

    report = promote_sessions(index_conn, ledger_path)   # 不 raise = 不炸启动
    assert report == "kept-both"
    warns = [m for m in caplog.messages if "sessions" in m and "kept" in m]
    assert warns, f"警告缺失：{caplog.messages}"

    # ledger 权威副本未被改写；index 副本未动（人工裁决面保留）
    lconn = init_ledger_db(ledger_path)
    assert load_session(lconn, "s1")["messages"] == [
        {"role": "user", "content": "ledger 版"}]
    lconn.close()
    assert load_session(index_conn, "s1")["messages"] == [
        {"role": "user", "content": "index 版"}]
    assert "sessions" in _tables(index_path)
    assert "sessions_pre_ledger_migration" not in _tables(index_path)
    index_conn.close()


def test_promote_tolerates_empty_zombie(tmp_path):
    """§2.0 zombie 容忍（空态）：升级机器上 init_db 刚建出的空 sessions
    直接改名让位，迁移照常完成。"""
    index_path = tmp_path / ".sync" / "index.db"
    ledger_path = tmp_path / ".sync" / "ledger.db"
    # 模拟旧代码 boot 的产物：index 里一个空 sessions 表
    conn = init_db(index_path)
    conn.execute("CREATE TABLE IF NOT EXISTS sessions ("
                 "id TEXT PRIMARY KEY, messages TEXT NOT NULL, "
                 "token_count INTEGER NOT NULL DEFAULT 0, "
                 "last_active REAL NOT NULL, created_at REAL NOT NULL, "
                 "auto_approve INTEGER NOT NULL DEFAULT 0)")
    conn.commit()
    conn.close()

    index_conn = init_db(index_path)   # 新代码 boot：zombie 仍在
    report = promote_sessions(index_conn, ledger_path)
    assert report == "migrated"
    assert "sessions" not in _tables(index_path)
    assert "sessions_pre_ledger_migration" in _tables(index_path)
    lconn = init_ledger_db(ledger_path)
    assert lconn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
    lconn.close()
    index_conn.close()


def test_promote_survives_source_missing_auto_approve(tmp_path):
    """远古 zombie（auto_approve 列都没有）不许炸启动：promote 前对源表
    跑 _ensure_session_cols 补齐，再显式列名搬迁。"""
    index_path = tmp_path / ".sync" / "index.db"
    ledger_path = tmp_path / ".sync" / "ledger.db"
    index_conn = init_db(index_path)
    index_conn.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            messages TEXT NOT NULL,
            token_count INTEGER NOT NULL DEFAULT 0,
            last_active REAL NOT NULL,
            created_at REAL NOT NULL
        )
        """
    )
    index_conn.execute(
        "INSERT INTO sessions (id, messages, token_count, last_active, created_at)"
        " VALUES ('ancient', '[]', 0, 1.0, 1.0)")
    index_conn.commit()

    assert promote_sessions(index_conn, ledger_path) == "migrated"
    lconn = init_ledger_db(ledger_path)
    assert load_session(lconn, "ancient") is not None
    lconn.close()
    index_conn.close()


# ── §5-4 恢复演练根钉 ───────────────────────────────────────────────────────

def test_recovery_drill_sessions_survive_and_pinned(vault_dir):
    """删 index.db 重建后 sessions 必须存活（权威数据），且 LEDGER_TABLES
    核查含 sessions；旧的「sessions empty is expected」口径必须翻假为真。"""
    from server.services.recovery import LEDGER_TABLES, run_recovery_drill

    assert "sessions" in LEDGER_TABLES

    # 盘上文件（drill 的 files/FTS 核查面）
    (vault_dir / "notes").mkdir(exist_ok=True)
    (vault_dir / "notes" / "a.md").write_bytes("# 会话幸存钉\n\nsurvive\n".encode("utf-8"))

    # index.db：drill 前状态（promote 完成后 index 里没有 sessions）
    index_conn = init_db(vault_dir / ".sync" / "index.db")
    index_conn.close()

    # ledger.db：会话在权威库里
    lconn = init_ledger_db(vault_dir / ".sync" / "ledger.db")
    save_session(lconn, "keep1", [{"role": "user", "content": "必须活着"}])
    save_session(lconn, "keep2", [{"role": "user", "content": "也必须活着"}])
    lconn.close()

    report = run_recovery_drill(vault_dir)

    assert report["ok"] is True
    assert report["ledger"]["preserved"] is True
    assert report["ledger"]["rows_before"]["sessions"] == 2
    assert report["ledger"]["rows_after"]["sessions"] == 2

    # 会话内容在演练后原样存活
    check = init_ledger_db(vault_dir / ".sync" / "ledger.db")
    assert load_session(check, "keep1")["messages"] == [
        {"role": "user", "content": "必须活着"}]
    check.close()

    # 重建后的 index.db 不许再长出 sessions
    assert "sessions" not in _tables(vault_dir / ".sync" / "index.db")
    # 假口径翻案：旧文案不得复活
    joined = "\n".join(report["notes"])
    assert "sessions empty is expected" not in joined


# ── §5-5 审计静态钉 + 启动站位钉 ────────────────────────────────────────────

def test_routes_agent_session_surface_uses_ledger_conn():
    """§3 清单点名钉：sessions 面上的函数调用点一律 ctx.ledger_conn；
    不得残留 index conn。"""
    src = (REPO_ROOT / "server" / "routes" / "agent.py").read_text(
        encoding="utf-8")
    # 总开关：SessionManager 构造
    assert "SessionManager(ctx.ledger_conn" in src
    assert "SessionManager(ctx.db_conn" not in src
    # 各触点正向点名
    assert "get_auto_approve(ctx.ledger_conn" in src
    assert "set_auto_approve(ctx.ledger_conn" in src
    assert "list_session_summaries(ctx.ledger_conn" in src
    assert "delete_session(ctx.ledger_conn" in src
    assert src.count("load_session(ctx.ledger_conn") >= 3   # _load_messages +
    # feedback 读侧 + GET 单会话
    # 反向：sessions 函数绝不允许再吃 index conn
    for fn in ("load_session", "get_auto_approve", "set_auto_approve",
               "list_session_summaries", "delete_session"):
        assert f"{fn}(ctx.db_conn" not in src, fn


def test_boot_sequence_promotes_sessions_at_both_entries():
    """启动站位：main / mcp 双入口都在 migrate_ledger 之后 promote_sessions；
    §7 顺手项 = 主进程 init_db 带 busy_timeout。"""
    main_src = (REPO_ROOT / "server" / "main.py").read_text(encoding="utf-8")
    mcp_src = (REPO_ROOT / "server" / "mcp_server.py").read_text(
        encoding="utf-8")
    for name, src in (("main.py", main_src), ("mcp_server.py", mcp_src)):
        assert "promote_sessions" in src, name
        assert src.index("migrate_ledger(") < src.index("promote_sessions("), name
    assert "init_db(db_path, busy_timeout_ms=5000)" in main_src


# ── §5-3 端到端（路由跑真 ledger_conn 的切片）───────────────────────────────

def test_session_routes_end_to_end_on_ledger(client, ledger_conn):
    """seed 在 ledger → list/detail 可见 → auto_approve 读写 → delete 删除
    （index.db 全程无 sessions，路由不回归）。"""
    save_session(ledger_conn, "e2e", [{"role": "user", "content": "端到端"}],
                 token_count=3)
    r = client.get("/api/agent/sessions")
    assert [s["id"] for s in r.json()["sessions"]] == ["e2e"]

    r = client.get("/api/agent/sessions/e2e")
    assert r.status_code == 200
    assert r.json()["token_count"] == 3

    assert client.post("/api/agent/settings",
                       json={"session_id": "e2e", "auto_approve": True}
                       ).status_code == 200
    assert client.get("/api/agent/settings",
                      params={"session_id": "e2e"}).json() == {"auto_approve": True}

    assert client.delete("/api/agent/sessions/e2e").status_code == 200
    assert load_session(ledger_conn, "e2e") is None
    assert client.get("/api/agent/sessions/e2e").status_code == 404
