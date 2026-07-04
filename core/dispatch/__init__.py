"""core.dispatch — sub-package for dispatcher leaf modules.

Functions extracted from scripts/daedalus_dispatch.py to keep the main
dispatcher file navigable.  Each module holds a cohesive, low-fan-in
cluster of helpers:

  resolvers  — pure config/execution-dict extractors and repo-path resolvers
  dedup      — kanban comment-marker deduplication helpers
  history    — dispatch history JSONL I/O
  delivery   — hermes-send wrappers and notification delivery helpers

The dispatcher re-exports every moved symbol so the public surface
(and test monkeypatching against the ``disp`` module) is unchanged.
"""
