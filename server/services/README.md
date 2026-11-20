# server/services

> 源出 AGENTS.md Gotchas「Change ledger」条尾部细节，2026-11-20 原文迁出；写时机矩阵与红线仍钉在 AGENTS。

## Change ledger 补注

Unregistered writes (external processes such as MCP stdio, direct disk edits) fall back to `source="watchdog"` on the consumer side — that is the consumer's fallback, not a writer exception. op strings are normalized to the canonical 4-set by `ACTION_TO_OP` at the `log_change` entry — do not "fix" op vocabulary in callers.
