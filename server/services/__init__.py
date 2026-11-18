"""Application-layer domain services (spec 2026-11-18 §1, card 4).

Migrated root-level service modules: ``ledger`` (the sole write surface for
change_log + files), ``sync`` (three-way classification, pure functions),
``egress`` (outbound-endpoint ledger write surface). Like everything here:
no imports from agent / bgagent / routes upward (R5 pins it from card 5).
"""
