"""Functional-test tooling for the memory runtime (spec 2026-10-21).

Modules:
- seed:              Tier-1 deterministic seeds (proposals / recall / changes)
- run_executor_once: one executor cycle, JSON summary
- mock_llm:          OpenAI-compatible scripted LLM (sentinel / consolidator)
- run_sentinel_once: minimal real-entry (run_once) sentinel run helper

Plus config.example.fun.yaml (template for the untracked config.fun.yaml).
"""
