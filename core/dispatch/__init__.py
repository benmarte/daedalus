"""core.dispatch — sub-package for dispatcher leaf modules.

Functions extracted from scripts/daedalus_dispatch.py to keep the main
dispatcher file navigable.  Each module holds a cohesive, low-fan-in
cluster of helpers:

  resolvers         — pure config/execution-dict extractors and repo-path resolvers
  dedup             — kanban comment-marker deduplication helpers
  history           — dispatch history JSONL I/O
  delivery          — hermes-send wrappers and notification delivery helpers
  bodies            — agent body template engine and delegation helpers (PR 2/4)
  validator_comment — GitHub comment scanners for validator/PM outcome fallback (PR 2/4)
  housekeeping      — issue fetch, follow-up extraction, orphan/worktree sweepers (PR 2/4)
  # NOTE: notifications.py not created — those functions call _hermes_send/_notify_targets
  # which are sibling-patched by tests via disp.*; they stay in daedalus_dispatch.py

The dispatcher re-exports every moved symbol so the public surface
(and test monkeypatching against the ``disp`` module) is unchanged.
"""
