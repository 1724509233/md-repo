"""Cross-layer vocabulary constants (card 13, 族一).

Pure literals shared by bottom and upper layers — the ``server/core``
"true sink" rule (R5 core: core ⊄ anything) holds by construction: this
module imports nothing.

Why here: ``config`` may not import ``services`` upward (``server/config``
pinned into ``_FORBIDDEN_EDGES``, card 13c), yet both the config verbs
whitelist (bottom layer) and the proposals service (application layer)
need ONE source of truth for the proposal vocabulary.
``services/proposals.py`` keeps same-name public re-exports (M2
adjudication: public symbols are not R3 violations) — consumers importing
``proposals.PROPOSALS_DIR`` / ``proposals.VALID_TYPES`` are unaffected.
"""
from __future__ import annotations

# The vault's proposal zone (ledger-only records; zero file writes v2a).
PROPOSALS_DIR = "suggestions"

# The v2 verb table (the propose tool's envelope validator + the config
# verbs whitelist both read this).
VALID_TYPES = frozenset({"entity_index", "relatedness", "summary", "errata", "reminder"})
