"""Route-table order snapshot: the 50 routes' (methods, path) sequence,
the /static mount, and the no-duplicate invariant.

Starlette matches the FIRST fully-matching route in registration order
and silently shadows later duplicates (reviewer-measured), so order IS
behavior: within console, manage must precede the upload catchall and
docs/changes must precede get_doc; within memory, the {relpath}/subpath
routes must precede the detail catchall. This snapshot pins the whole
table — a missing, moved, or duplicated route turns it red.
"""
from __future__ import annotations

from fastapi.routing import APIRoute
from starlette.routing import Mount

from server.api import create_app  # noqa: F401  (api 面 import 存活证明；app fixture 来自 conftest)


# (methods, path) — in registration order (ROUTE_MODULES: console →
# notifications → sync → recall → egress → memory → agent → trash →
# settings). 50 routes.
EXPECTED = (
    # console (10): manage ⪯ upload, changes ⪯ get_doc (both order-sensitive).
    (("GET",), "/"),                                   # 1  index
    (("GET",), "/api/health"),                         # 2
    (("GET",), "/api/docs"),                           # 3  list (ETag/304)
    (("GET",), "/api/docs/changes"),                   # 4  ⪯ get_doc
    (("GET",), "/api/graph/backlinks"),                # 5
    (("GET",), "/api/graph/related"),                  # 6
    (("GET",), "/api/docs/{path:path}"),               # 7  get_doc (catchall)
    (("POST",), "/api/docs/manage"),                   # 8  ⪯ upload
    (("POST",), "/api/docs/{path:path}"),              # 9  upload (catchall)
    (("PUT",), "/api/docs/{path:path}"),               # 10 save_doc
    # notifications (2).
    (("GET",), "/api/notifications"),                  # 11
    (("POST",), "/api/notifications/read"),            # 12
    # sync (1).
    (("POST",), "/api/sync"),                          # 13
    # recall (5).
    (("POST",), "/api/ai/search"),                     # 14
    (("POST",), "/api/recall/click"),                  # 15 click ledger (fire-and-forget)
    (("GET",), "/api/tags"),                           # 16
    (("GET",), "/api/recall"),                         # 17
    (("GET",), "/api/recall/stats"),                   # 18
    # egress (2).
    (("GET",), "/api/egress"),                          # 19 ts DESC feed
    (("GET",), "/api/egress/status"),                   # 20 cloud set + last_ts
    # memory (13): {relpath}/subpath ⪯ detail (all four ⪯ the catchall).
    (("GET",), "/api/memory/proposals"),               # 21 list
    (("POST",), "/api/memory/proposals/{relpath:path}/adopt"),   # 22 ⪯ detail
    (("POST",), "/api/memory/proposals/{relpath:path}/revive"),  # 23 ⪯ detail
    (("POST",), "/api/memory/proposals/{relpath:path}/reject"),  # 24 ⪯ detail
    (("GET",), "/api/memory/proposals/{relpath:path}/trail"),    # 25 ⪯ detail
    (("GET",), "/api/memory/proposals/{relpath:path}"),          # 26 detail (catchall)
    (("POST",), "/api/memory/rollback"),                # 27
    (("GET",), "/api/memory/rollback-calls"),           # 28
    (("GET",), "/api/memory/reports"),                  # 29
    (("GET",), "/api/memory/reports/{relpath:path}"),   # 30
    (("GET",), "/api/memory/doc/{relpath:path}"),             # 31 (v2b② doc face, spec §6.4)
    (("POST",), "/api/memory/relatedness/{edge_id}/ignore"),  # 32 (v2b② D12/D15)
    (("GET",), "/api/memory/status"),                   # 33
    # agent (13): 运行脱离连接 T3 加 stream/stop/queue.remove（chat 纯投递后
    # 的订阅/停止/队列面，spec 2026-11-20 §6）。
    (("POST",), "/api/ai/chat"),                        # 34
    (("POST",), "/api/agent/chat"),                     # 35 纯投递 202
    (("GET",), "/api/agent/stream"),                    # 36 订阅 SSE
    (("POST",), "/api/agent/stop"),                     # 37
    (("POST",), "/api/agent/queue/remove"),             # 38
    (("POST",), "/api/agent/confirm"),                  # 39
    (("GET",), "/api/agent/settings"),                  # 40
    (("POST",), "/api/agent/settings"),                 # 41
    (("POST",), "/api/agent/new-session"),              # 42
    (("POST",), "/api/agent/feedback"),                 # 43 (lane B, same batch)
    (("GET",), "/api/agent/sessions"),                  # 44 list (会话管理 T2)
    (("GET",), "/api/agent/sessions/{sid}"),            # 45 detail
    (("DELETE",), "/api/agent/sessions/{sid}"),         # 46 delete
    # trash (2).
    (("GET",), "/api/trash"),                           # 47 list
    (("POST",), "/api/trash"),                          # 48 restore/purge/empty
    # settings (2): config.yaml 页面化编辑（spec 2026-11-22 §4）——GET 掩码回显 /
    # PUT 字段级写回，追加在域序末尾（trash 之后，计划 Global Constraints）。
    (("GET",), "/api/settings"),                        # 49 masked echo + _meta
    (("PUT",), "/api/settings"),                        # 50 校验即启动/热生效
)


def _table(app):
    return [
        (frozenset(r.methods), r.path)
        for r in app.router.routes
        if isinstance(r, APIRoute)
    ]


def test_route_order_snapshot(app):
    assert _table(app) == [(frozenset(m), p) for m, p in EXPECTED]
    mounts = [r for r in app.router.routes if isinstance(r, Mount)]
    assert [m.path for m in mounts] == ["/static"]


def test_no_duplicate_routes(app):
    seen = set()
    for methods, path in _table(app):
        for m in methods:
            assert (m, path) not in seen, f"duplicate {m} {path} (silent shadowing)"
            seen.add((m, path))
