"""Import discipline for the two vault.db locks (L4 ledger-file split).

Card 5 pinned the rule for ``_db_lock``: it stays inside ``server/vault/db`` —
outside the package the named lock-holding functions are the interface, not
the lock. L4 adds a second lock (``_ledger_lock``) with the same discipline for
the four authoritative-state tables on ``ledger.db``. This test pins both rules
by scanning the ``server/`` source tree.

The identifiers are constructed from parts so this test file (itself outside
``server/vault/db``) does not match the scan — the card 5 pattern: the
identifier must not appear in the test that asserts on it.
"""
from __future__ import annotations

from pathlib import Path

# Constructed from parts (see module docstring) so the literals below never
# contain the full identifier as a contiguous substring.
DB_LOCK = "_".join(("_", "db_lock"))
LEDGER_LOCK = "_".join(("_", "ledger_lock"))

REPO = Path(__file__).resolve().parent.parent
SERVER = REPO / "server"
DB_PKG = SERVER / "vault" / "db"

# The three ledger-domain write faces that now hold _ledger_lock (card 5 shape).
_LEDGER_MODULES = ("proposal_records.py", "snapshot_records.py", "relatedness.py")


def test_db_lock_not_referenced_by_ledger_domain_modules():
    """_db_lock must not appear in the ledger-domain write modules — they hold
    _ledger_lock now, and referencing the index lock would blur the domain
    boundary (the two locks guard disjoint tables on disjoint files)."""
    for name in _LEDGER_MODULES:
        src = (DB_PKG / name).read_text(encoding="utf-8")
        assert DB_LOCK not in src, f"{name} still references {DB_LOCK}"


def test_ledger_lock_stays_inside_db_package():
    """_ledger_lock must not appear in any server/ file outside vault/db — the
    named functions (proposal_records.*, snapshot_records.*,
    relatedness.merge_edge) are the interface; the lock is package-internal."""
    offenders = []
    for f in sorted(SERVER.rglob("*.py")):
        if f.is_relative_to(DB_PKG):
            continue
        if LEDGER_LOCK in f.read_text(encoding="utf-8"):
            offenders.append(f.relative_to(REPO).as_posix())
    assert offenders == [], f"{LEDGER_LOCK} outside server/vault/db: {offenders}"
