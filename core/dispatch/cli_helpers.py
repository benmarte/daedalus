"""core.dispatch.cli_helpers — CLI-layer utilities for the dispatcher.

Small pure helpers used by the dispatcher's CLI entry points (main,
_main_inner).  Extracted here because they have no dispatcher-internal
dependencies and are easy to unit-test in isolation.

  _sweep_exit_code  — derive the process exit code from per-project ok/err
                      tallies (issue #1112); 1 only when >=1 project ran and
                      every one errored, 0 otherwise.

Moved from scripts/daedalus_dispatch.py (issue #1153 PR 4/4).
The dispatcher re-exports every symbol so the public surface is unchanged.
"""

from __future__ import annotations


def _sweep_exit_code(n_ok: int, n_err: int) -> int:
    """Exit code for a dispatch sweep (issue #1112).

    Returns 1 only when at least one project ran and *every* one errored, so
    cron mail-on-error, CI status gates, and wrapper scripts can detect a total
    dispatch failure. Partial success (>=1 project ran cleanly) returns 0 —
    partial failure is normal operation — and a zero-project tick (empty
    registry / unresolved repo) returns 0 because nothing failed to run.
    """
    if n_err > 0 and n_ok == 0:
        return 1
    return 0
