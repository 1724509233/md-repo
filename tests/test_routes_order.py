"""Route-table order snapshot: the 36 routes' (methods, path) sequence,
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
# notifications → sync → recall → memory → agent → trash). 36 routes.
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
    # recall (4).
    (("POST",), "/api/ai/search"),                     # 14
    (("GET",), "/api/tags"),                           # 15
    (("GET",), "/api/recall"),                         # 16
    (("GET",), "/api/recall/stats"),                   # 17
    # memory (11): {relpath}/subpath ⪯ detail (all four ⪯ the catchall).
    (("GET",), "/api/memory/proposals"),               # 18 list
    (("POST",), "/api/memory/proposals/{relpath:path}/adopt"),   # 19 ⪯ detail
    (("POST",), "/api/memory/proposals/{relpath:path}/revive"),  # 20 ⪯ detail
    (("POST",), "/api/memory/proposals/{relpath:path}/reject"),  # 21 ⪯ detail
    (("GET",), "/api/memory/proposals/{relpath:path}/trail"),    # 22 ⪯ detail
    (("GET",), "/api/memory/proposals/{relpath:path}"),          # 23 detail (catchall)
    (("POST",), "/api/memory/rollback"),                # 24
    (("GET",), "/api/memory/rollback-calls"),           # 25
    (("GET",), "/api/memory/reports"),                  # 26
    (("GET",), "/api/memory/reports/{relpath:path}"),   # 27
    (("GET",), "/api/memory/status"),                   # 28
    # agent (6).
    (("POST",), "/api/ai/chat"),                        # 29
    (("POST",), "/api/agent/chat"),                     # 30
    (("POST",), "/api/agent/confirm"),                  # 31
    (("GET",), "/api/agent/settings"),                  # 32
    (("POST",), "/api/agent/settings"),                 # 33
    (("POST",), "/api/agent/new-session"),              # 34
    # trash (2).
    (("GET",), "/api/trash"),                           # 35 list
    (("POST",), "/api/trash"),                          # 36 restore/purge/empty
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
