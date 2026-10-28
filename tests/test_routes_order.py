"""Route-table order snapshot: the 31 routes' (methods, path) sequence,
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
# notifications → sync → recall → memory → agent). 31 routes.
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
    # recall (3).
    (("POST",), "/api/ai/search"),                     # 14
    (("GET",), "/api/recall"),                         # 15
    (("GET",), "/api/recall/stats"),                   # 16
    # memory (11): {relpath}/subpath ⪯ detail (all four ⪯ the catchall).
    (("GET",), "/api/memory/proposals"),               # 17 list
    (("POST",), "/api/memory/proposals/{relpath:path}/adopt"),   # 18 ⪯ detail
    (("POST",), "/api/memory/proposals/{relpath:path}/revive"),  # 19 ⪯ detail
    (("POST",), "/api/memory/proposals/{relpath:path}/reject"),  # 20 ⪯ detail
    (("GET",), "/api/memory/proposals/{relpath:path}/trail"),    # 21 ⪯ detail
    (("GET",), "/api/memory/proposals/{relpath:path}"),          # 22 detail (catchall)
    (("POST",), "/api/memory/rollback"),                # 23
    (("GET",), "/api/memory/rollback-calls"),           # 24
    (("GET",), "/api/memory/reports"),                  # 25
    (("GET",), "/api/memory/reports/{relpath:path}"),   # 26
    (("GET",), "/api/memory/status"),                   # 27
    # agent (4).
    (("POST",), "/api/ai/chat"),                        # 28
    (("POST",), "/api/agent/chat"),                     # 29
    (("POST",), "/api/agent/confirm"),                  # 30
    (("POST",), "/api/agent/new-session"),              # 31
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
