# DEPRECATED shim (card 2, spec 2026-11-18 §2.2): FileEntry moved to
# server/core/types.py. This one-line re-export keeps every existing
# import (including the server/vault/__init__.py 38-name shim and
# legacy test imports) resolving unchanged. RETIRE WITH CARD 12.
from ..core.types import FileEntry

__all__ = ["FileEntry"]
