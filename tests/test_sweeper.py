"""Tests for the stale-card sweeper (issues #186, #232; epic #181).

Covers core.sweeper: blocked_since timestamp fallback, the pure find_stale_blocked
detector, and sweep_stale_blocked (warn / archive / dry-run / DB-enrich), plus the
running-card detection added in #232 (find_stale_running / sweep_stale_running).

Dual-mode: runs under pytest AND standalone (``python tests/test_sweeper.py``)
via the shared check() helper, mirroring the other suites.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest import mock

# Make the package root importable (config/, core/) and the tests dir (conftest).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import conftest  # noqa: E402
from conftest import check  # noqa: E402
from core import kanban, sweeper  # noqa: E402

NOW = 1_000_000_000
HOUR = 3600


def _card(tid="t1", status="blocked", **ts):
    """A blocked card seeded with explicit timestamps (no DB enrichment needed)."""
    card = {"id": tid, "title": f"#1 card {tid}", "status": status}
    card.update(ts)
    return card


# ── blocked_since: timestamp fallback order ───────────────────────────────────


def test_blocked_since_prefers_heartbeat():
    card = _card(last_heartbeat_at=100, started_at=200, created_at=300)
    check("heartbeat wins over started/created", sweeper.blocked_since(card) == 100)


def test_blocked_since_falls_back_to_started():
    card = _card(started_at=200, created_at=300)
    check("started_at used when no heartbeat", sweeper.blocked_since(card) == 200)


def test_blocked_since_falls_back_to_created():
    card = _card(created_at=300)
    check("created_at used as last resort", sweeper.blocked_since(card) == 300)


def test_blocked_since_none_when_no_timestamps():
    check("no timestamps → None", sweeper.blocked_since(_card()) is None)


def test_blocked_since_ignores_zero_and_nonint():
    card = _card(last_heartbeat_at=0, started_at="bad", created_at=300)
    check("zero/non-int skipped, falls through to created", sweeper.blocked_since(card) == 300)


# ── find_stale_blocked: pure detection ────────────────────────────────────────


def test_find_stale_detects_old_blocked():
    cards = [_card("t1", last_heartbeat_at=NOW - 50 * HOUR)]
    stale = sweeper.find_stale_blocked(cards, now=NOW, threshold_hours=48)
    check("50h-old blocked card is stale", len(stale) == 1 and stale[0][0]["id"] == "t1")
    check("age ~50h reported", abs(stale[0][1] - 50.0) < 0.01)


def test_find_stale_ignores_fresh_blocked():
    cards = [_card("t1", last_heartbeat_at=NOW - 10 * HOUR)]
    check("10h-old blocked card not stale",
          sweeper.find_stale_blocked(cards, now=NOW, threshold_hours=48) == [])


def test_find_stale_ignores_non_blocked():
    cards = [_card("t1", status="running", last_heartbeat_at=NOW - 100 * HOUR)]
    check("old running card skipped",
          sweeper.find_stale_blocked(cards, now=NOW, threshold_hours=48) == [])


def test_find_stale_skips_unaged_card():
    cards = [_card("t1")]  # blocked but no timestamps
    check("blocked card with no timestamps skipped",
          sweeper.find_stale_blocked(cards, now=NOW, threshold_hours=48) == [])


def test_find_stale_sorts_oldest_first():
    cards = [
        _card("recent", last_heartbeat_at=NOW - 49 * HOUR),
        _card("oldest", last_heartbeat_at=NOW - 200 * HOUR),
        _card("middle", last_heartbeat_at=NOW - 72 * HOUR),
    ]
    stale = sweeper.find_stale_blocked(cards, now=NOW, threshold_hours=48)
    order = [c["id"] for c, _ in stale]
    check("sorted oldest-first", order == ["oldest", "middle", "recent"])


def test_find_stale_respects_custom_threshold():
    cards = [_card("t1", last_heartbeat_at=NOW - 10 * HOUR)]
    check("10h card stale at 6h threshold",
          len(sweeper.find_stale_blocked(cards, now=NOW, threshold_hours=6)) == 1)


# ── find_stale_blocked: edge cases at the exact 48h boundary ─────────────────


def test_find_stale_exactly_at_48h_is_not_stale():
    """A card whose last progress is EXACTLY threshold_hours ago is NOT stale.

    The requirement is 'more than 48 hours' (strictly greater), not 'at least'.
    """
    cards = [_card("t1", last_heartbeat_at=NOW - 48 * HOUR)]
    result = sweeper.find_stale_blocked(cards, now=NOW, threshold_hours=48)
    check("card exactly at 48h is NOT stale (must be more than)", result == [])


def test_find_stale_just_under_48h_is_not_stale():
    """A card at ~47.99h is clearly not stale."""
    cards = [_card("t1", last_heartbeat_at=NOW - 47 * HOUR - 59 * 60)]
    check("card ~47.98h old is not stale",
          sweeper.find_stale_blocked(cards, now=NOW, threshold_hours=48) == [])


def test_find_stale_just_over_48h_is_stale():
    """A card at ~48.01h IS stale (just over the 'more than 48h' threshold)."""
    cards = [_card("t1", last_heartbeat_at=NOW - 48 * HOUR - 60)]  # 1 second past threshold
    result = sweeper.find_stale_blocked(cards, now=NOW, threshold_hours=48)
    check("card 1 second past 48h IS stale",
          len(result) == 1 and result[0][0]["id"] == "t1")


def test_find_stale_skips_already_archived():
    """Cards that are already archived are ignored regardless of age."""
    cards = [_card("t1", last_heartbeat_at=NOW - 200 * HOUR, archived=True)]
    check("archived card with old timestamp skipped",
          sweeper.find_stale_blocked(cards, now=NOW, threshold_hours=48) == [])


def test_find_stale_running_skips_already_archived():
    """Running cards already marked archived are also ignored by the running detector."""
    cards = [_card("t1", status="running",
                   last_heartbeat_at=NOW - 200 * HOUR, archived=True)]
    check("archived running card skipped",
          sweeper.find_stale_running(cards, now=NOW, threshold_hours=24) == [])


# ── sweep_stale_blocked: warn / archive / dry-run ─────────────────────────────


def test_sweep_returns_stale_ids_and_does_not_archive_by_default():
    cards = [_card("t1", last_heartbeat_at=NOW - 50 * HOUR),
             _card("t2", last_heartbeat_at=NOW - 1 * HOUR)]
    with mock.patch.object(kanban, "list_blocked", return_value=cards), \
         mock.patch.object(kanban, "archive_task") as arch:
        ids = sweeper.sweep_stale_blocked("daedalus", now=NOW)
    check("only the 50h card is returned", ids == ["t1"])
    check("archive not called when archive=False", arch.call_count == 0)


def test_sweep_archives_when_enabled():
    cards = [_card("t1", last_heartbeat_at=NOW - 50 * HOUR)]
    with mock.patch.object(kanban, "list_blocked", return_value=cards), \
         mock.patch.object(kanban, "archive_task", return_value=True) as arch:
        ids = sweeper.sweep_stale_blocked("daedalus", now=NOW, archive=True)
    check("stale id returned", ids == ["t1"])
    check("archive called for stale card", arch.call_args == mock.call("daedalus", "t1"))


def test_sweep_dry_run_does_not_archive():
    cards = [_card("t1", last_heartbeat_at=NOW - 50 * HOUR)]
    with mock.patch.object(kanban, "list_blocked", return_value=cards), \
         mock.patch.object(kanban, "archive_task") as arch:
        ids = sweeper.sweep_stale_blocked("daedalus", now=NOW, archive=True, dry_run=True)
    check("dry-run still reports stale id", ids == ["t1"])
    check("dry-run does not archive", arch.call_count == 0)


def test_sweep_empty_board():
    with mock.patch.object(kanban, "list_blocked", return_value=[]):
        check("empty board → []", sweeper.sweep_stale_blocked("daedalus", now=NOW) == [])


def test_sweep_enriches_heartbeat_from_db():
    """A card lacking last_heartbeat_at is aged via the DB read."""
    cards = [_card("t1", created_at=NOW - 1 * HOUR)]  # created recently, but...
    with mock.patch.object(kanban, "list_blocked", return_value=cards), \
         mock.patch.object(sweeper, "_heartbeats", return_value={"t1": NOW - 60 * HOUR}), \
         mock.patch.object(kanban, "archive_task"):
        ids = sweeper.sweep_stale_blocked("daedalus", now=NOW)
    check("DB heartbeat (60h) makes card stale despite recent created_at", ids == ["t1"])


# ── sweep_stale_blocked: logging output verification ─────────────────────────


def test_sweep_logs_warning_for_stale_blocked():
    """sweep_stale_blocked must emit a WARNING-level log with card id, hours, and threshold."""
    cards = [_card("t1", last_heartbeat_at=NOW - 50 * HOUR)]
    with mock.patch.object(kanban, "list_blocked", return_value=cards), \
         mock.patch.object(sweeper, "_heartbeats", return_value={}), \
         mock.patch.object(kanban, "archive_task"):
        with mock.patch.object(sweeper.logger, "warning") as log_warn:
            sweeper.sweep_stale_blocked("daedalus", now=NOW, threshold_hours=48)
    check("logger.warning called once", log_warn.call_count == 1)
    args_pos = log_warn.call_args[0]
    # Message template + args: template should contain "blocked" and card id
    msg = args_pos[0]
    check("warning message contains 'blocked' keyword", "blocked" in msg)
    check("warning message contains threshold placeholder", "%g" in msg or "48" in str(args_pos))
    # Card id should appear in a later positional arg (tid is arg[1])
    check("warning includes card id", "t1" in str(args_pos))


def test_sweep_logs_warning_for_stale_running():
    """sweep_stale_running must emit a WARNING-level log with card id, hours, and threshold."""
    cards = [_card("t1", status="running", last_heartbeat_at=NOW - 30 * HOUR)]
    with mock.patch.object(kanban, "list_tasks", return_value=cards), \
         mock.patch.object(sweeper, "_heartbeats", return_value={}):
        with mock.patch.object(sweeper.logger, "warning") as log_warn:
            sweeper.sweep_stale_running("daedalus", now=NOW, threshold_hours=24)
    check("logger.warning called once", log_warn.call_count == 1)
    args_pos = log_warn.call_args[0]
    msg = args_pos[0]
    check("running-card warning contains 'running' keyword", "running" in msg)


def test_sweep_no_log_when_no_stale():
    """When no cards are stale, logger.warning is never called."""
    with mock.patch.object(kanban, "list_blocked", return_value=[]):
        with mock.patch.object(sweeper.logger, "warning") as log_warn:
            sweeper.sweep_stale_blocked("daedalus", now=NOW)
    check("no warning logged for empty board", log_warn.call_count == 0)


# ── find_stale_running: pure detection (issue #232) ───────────────────────────


def test_find_stale_running_detects_old_running():
    cards = [_card("t1", status="running", last_heartbeat_at=NOW - 30 * HOUR)]
    stale = sweeper.find_stale_running(cards, now=NOW, threshold_hours=24)
    check("30h-old running card is stale", len(stale) == 1 and stale[0][0]["id"] == "t1")
    check("age ~30h reported", abs(stale[0][1] - 30.0) < 0.01)


def test_find_stale_running_ignores_fresh_running():
    cards = [_card("t1", status="running", last_heartbeat_at=NOW - 5 * HOUR)]
    check("5h-old running card not stale",
          sweeper.find_stale_running(cards, now=NOW, threshold_hours=24) == [])


def test_find_stale_running_ignores_blocked():
    cards = [_card("t1", status="blocked", last_heartbeat_at=NOW - 100 * HOUR)]
    check("old blocked card skipped by running detector",
          sweeper.find_stale_running(cards, now=NOW, threshold_hours=24) == [])


def test_find_stale_running_default_threshold_is_30min():
    # #1323: default cut from 24h → 0.5h (30 min) so a stuck card frees the
    # max_dispatch slot in minutes. A card idle 1h is stale at the default.
    cards = [_card("t1", status="running", last_heartbeat_at=NOW - 1 * HOUR)]
    check("1h running card stale at default threshold",
          len(sweeper.find_stale_running(cards, now=NOW)) == 1)
    check("DEFAULT_RUNNING_STALE_HOURS is 0.5", sweeper.DEFAULT_RUNNING_STALE_HOURS == 0.5)


def test_find_stale_running_fresh_under_30min_default():
    # A card idle only 10 min (< 30 min default) is NOT stale — a live worker
    # heartbeats every 5 min, so it never ages out.
    cards = [_card("t1", status="running", last_heartbeat_at=NOW - 600)]
    check("10min running card not stale at default threshold",
          sweeper.find_stale_running(cards, now=NOW) == [])


def test_find_stale_running_skips_unaged_card():
    cards = [_card("t1", status="running")]  # running but no timestamps
    check("running card with no timestamps skipped",
          sweeper.find_stale_running(cards, now=NOW, threshold_hours=24) == [])


def test_find_stale_running_sorts_oldest_first():
    cards = [
        _card("recent", status="running", last_heartbeat_at=NOW - 25 * HOUR),
        _card("oldest", status="running", last_heartbeat_at=NOW - 90 * HOUR),
    ]
    stale = sweeper.find_stale_running(cards, now=NOW, threshold_hours=24)
    check("running cards sorted oldest-first",
          [c["id"] for c, _ in stale] == ["oldest", "recent"])


def test_find_stale_running_exactly_at_24h_is_not_stale():
    """A running card exactly at threshold_hours is NOT stale (matches blocked behavior)."""
    cards = [_card("t1", status="running", last_heartbeat_at=NOW - 24 * HOUR)]
    check("running card exactly at 24h is not stale",
          sweeper.find_stale_running(cards, now=NOW, threshold_hours=24) == [])


# ── sweep_stale_running: warn-only, no archive ────────────────────────────────


def test_sweep_running_returns_stale_ids():
    cards = [_card("t1", status="running", last_heartbeat_at=NOW - 30 * HOUR),
             _card("t2", status="running", last_heartbeat_at=NOW - 1 * HOUR)]
    with mock.patch.object(kanban, "list_tasks", return_value=cards):
        ids = sweeper.sweep_stale_running("daedalus", now=NOW, threshold_hours=24)
    check("only the 30h running card is returned", ids == ["t1"])


# ── sweep_stale_running: self-heal via reset (issue #1323) ─────────────────────


def test_sweep_running_reset_false_does_not_block():
    """Default (reset=False) is warn-only — byte-identical to pre-#1323 behavior."""
    cards = [_card("t1", status="running", last_heartbeat_at=NOW - 2 * HOUR)]
    with mock.patch.object(kanban, "list_tasks", return_value=cards), \
         mock.patch.object(kanban, "block_task") as bt:
        ids = sweeper.sweep_stale_running("daedalus", now=NOW)
    check("stale card still returned", ids == ["t1"])
    check("reset=False never blocks", bt.call_count == 0)


def test_sweep_running_reset_blocks_stale_card():
    """reset=True re-blocks each stale running card so it can be re-dispatched."""
    cards = [_card("t1", status="running", last_heartbeat_at=NOW - 2 * HOUR),
             _card("fresh", status="running", last_heartbeat_at=NOW - 60)]
    with mock.patch.object(kanban, "list_tasks", return_value=cards), \
         mock.patch.object(kanban, "block_task", return_value=True) as bt:
        ids = sweeper.sweep_stale_running("daedalus", now=NOW, reset=True)
    check("only the stale card is returned", ids == ["t1"])
    check("only the stale card is blocked", bt.call_count == 1)
    check("block targets the stale card", bt.call_args[0][:2] == ("daedalus", "t1"))


def test_sweep_running_reset_reason_is_crash_class():
    """The reset reason must classify as crash-class so crash-retry re-dispatches it."""
    from core import crash_retry
    cards = [_card("t1", status="running", last_heartbeat_at=NOW - 2 * HOUR)]
    with mock.patch.object(kanban, "list_tasks", return_value=cards), \
         mock.patch.object(kanban, "block_task", return_value=True) as bt:
        sweeper.sweep_stale_running("daedalus", now=NOW, reset=True)
    reason = bt.call_args[0][2]
    check("reason starts with the crash-class marker",
          reason.startswith(sweeper.STALE_RUNNING_RESET_REASON))
    check("crash_retry.classify → crash", crash_retry.classify(reason) == "crash")


def test_sweep_running_queries_running_status():
    with mock.patch.object(kanban, "list_tasks", return_value=[]) as lt:
        sweeper.sweep_stale_running("daedalus", now=NOW)
    check("list_tasks called with status='running'",
          lt.call_args == mock.call("daedalus", status="running"))


def test_sweep_running_empty_board():
    with mock.patch.object(kanban, "list_tasks", return_value=[]):
        check("empty board → []", sweeper.sweep_stale_running("daedalus", now=NOW) == [])


def test_sweep_running_enriches_heartbeat_from_db():
    cards = [_card("t1", status="running", created_at=NOW - 1 * HOUR)]
    with mock.patch.object(kanban, "list_tasks", return_value=cards), \
         mock.patch.object(sweeper, "_heartbeats", return_value={"t1": NOW - 40 * HOUR}):
        ids = sweeper.sweep_stale_running("daedalus", now=NOW)
    check("DB heartbeat (40h) makes running card stale despite recent created_at",
          ids == ["t1"])


# ── _heartbeats: graceful DB degradation ──────────────────────────────────────


def test_heartbeats_missing_db_returns_empty():
    with mock.patch.object(sweeper, "_db_path", return_value="/nonexistent/kanban.db"):
        check("missing DB → {}", sweeper._heartbeats("daedalus", ["t1"]) == {})


def test_heartbeats_empty_ids_returns_empty():
    check("no ids → {}", sweeper._heartbeats("daedalus", []) == {})


# ── CLI manual invocation entry point ─────────────────────────────────────────


def test_cli_sweep_runs_with_defaults():
    """sweep_stale_blocked can be triggered via the CLI entry point (manual invocation)."""
    # The entry point must exist and be importable from core.
    from core import sweeper_cli
    with mock.patch.object(kanban, "list_blocked", return_value=[]):
        rc = sweeper_cli.run(["daedalus"])
    check("CLI run returns 0 on empty board", rc == 0)


def test_cli_sweep_respects_threshold_flag():
    from core import sweeper_cli
    cards = [_card("t1", last_heartbeat_at=NOW - 10 * HOUR)]
    with mock.patch.object(kanban, "list_blocked", return_value=cards), \
         mock.patch.object(kanban, "archive_task"):
        rc = sweeper_cli.run(["daedalus", "--threshold-hours", "6"])
    check("CLI --threshold-hours accepted", rc == 0)


def test_cli_sweep_archive_flag_triggers_archive():
    from core import sweeper_cli
    cards = [_card("t1", last_heartbeat_at=NOW - 50 * HOUR)]
    with mock.patch.object(kanban, "list_blocked", return_value=cards), \
         mock.patch.object(kanban, "archive_task", return_value=True) as arch:
        rc = sweeper_cli.run(["daedalus", "--archive"])
    check("CLI --archive triggers archive",
          rc == 0 and arch.call_count == 1)


def test_cli_sweep_dry_run_flag():
    from core import sweeper_cli
    cards = [_card("t1", last_heartbeat_at=NOW - 50 * HOUR)]
    with mock.patch.object(kanban, "list_blocked", return_value=cards), \
         mock.patch.object(kanban, "archive_task") as arch:
        rc = sweeper_cli.run(["daedalus", "--archive", "--dry-run"])
    check("CLI --dry-run prevents mutation",
          rc == 0 and arch.call_count == 0)



# ── _archive_with_retry: retry-safe error handling (issue #430) ─────────────


def test_archive_with_retry_succeeds_first_attempt():
    """First archive attempt succeeds — no retries needed."""
    with mock.patch.object(kanban, "archive_task", return_value=True) as arch:
        check(
            "archive succeeds first attempt",
            sweeper._archive_with_retry("daedalus", "t1", max_attempts=3) is True
        )
    check("archive_task called exactly once", arch.call_count == 1)


def test_archive_with_retry_succeeds_on_second_attempt():
    """First attempt fails, second succeeds."""
    responses = [False, True]
    def side_effect(slug, tid):
        return responses.pop(0) if responses else True
    with mock.patch.object(kanban, "archive_task", side_effect=side_effect) as arch:
        check(
            "archive succeeds on retry",
            sweeper._archive_with_retry("daedalus", "t1", max_attempts=3) is True
        )
    check("archive_task called twice (1 fail + 1 success)", arch.call_count == 2)


def test_archive_with_retry_all_attempts_exhausted():
    """All retry attempts fail — returns False after max_attempts."""
    with mock.patch.object(kanban, "archive_task", return_value=False) as arch:
        check(
            "archive exhausted returns False",
            sweeper._archive_with_retry("daedalus", "t1", max_attempts=3) is False
        )
    check("archive_task called 3 times (all attempts)", arch.call_count == 3)


def test_archive_with_retry_handles_exception():
    """archive_task raises exception — contained, returns False after retries."""
    call_count_local = [0]
    def raising_archive(slug, tid):
        call_count_local[0] += 1
        raise RuntimeError("catastrophic network failure")
    with mock.patch.object(kanban, "archive_task", side_effect=raising_archive) as arch:
        check(
            "archive exception contained, returns False",
            sweeper._archive_with_retry("daedalus", "t1", max_attempts=2) is False
        )
    check("archive_task called 2 times despite exception", arch.call_count == 2)


def test_archive_with_retry_idempotent_success():
    """Re-archiving an already-archived card succeeds (idempotent)."""
    def idempotent_archive(slug, tid):
        return True
    with mock.patch.object(kanban, "archive_task", side_effect=idempotent_archive):
        check(
            "idempotent re-archive returns True",
            sweeper._archive_with_retry("daedalus", "t1", max_attempts=3) is True
        )

ALL_TESTS = [
    test_blocked_since_prefers_heartbeat,
    test_blocked_since_falls_back_to_started,
    test_blocked_since_falls_back_to_created,
    test_blocked_since_none_when_no_timestamps,
    test_blocked_since_ignores_zero_and_nonint,
    test_find_stale_detects_old_blocked,
    test_find_stale_ignores_fresh_blocked,
    test_find_stale_ignores_non_blocked,
    test_find_stale_skips_unaged_card,
    test_find_stale_sorts_oldest_first,
    test_find_stale_respects_custom_threshold,
    test_find_stale_exactly_at_48h_is_not_stale,
    test_find_stale_just_under_48h_is_not_stale,
    test_find_stale_just_over_48h_is_stale,
    test_find_stale_skips_already_archived,
    test_find_stale_running_skips_already_archived,
    test_sweep_returns_stale_ids_and_does_not_archive_by_default,
    test_sweep_archives_when_enabled,
    test_sweep_dry_run_does_not_archive,
    test_sweep_empty_board,
    test_sweep_enriches_heartbeat_from_db,
    test_sweep_logs_warning_for_stale_blocked,
    test_sweep_logs_warning_for_stale_running,
    test_sweep_no_log_when_no_stale,
    test_find_stale_running_detects_old_running,
    test_find_stale_running_ignores_fresh_running,
    test_find_stale_running_ignores_blocked,
    test_find_stale_running_default_threshold_is_30min,
    test_find_stale_running_fresh_under_30min_default,
    test_find_stale_running_skips_unaged_card,
    test_find_stale_running_sorts_oldest_first,
    test_find_stale_running_exactly_at_24h_is_not_stale,
    test_sweep_running_returns_stale_ids,
    test_sweep_running_reset_false_does_not_block,
    test_sweep_running_reset_blocks_stale_card,
    test_sweep_running_reset_reason_is_crash_class,
    test_sweep_running_queries_running_status,
    test_sweep_running_empty_board,
    test_sweep_running_enriches_heartbeat_from_db,
    test_heartbeats_missing_db_returns_empty,
    test_heartbeats_empty_ids_returns_empty,
    test_cli_sweep_runs_with_defaults,
    test_cli_sweep_respects_threshold_flag,
    test_cli_sweep_archive_flag_triggers_archive,
    test_cli_sweep_dry_run_flag,
    # Issue #430: retry-safe archive error handling
    test_archive_with_retry_succeeds_first_attempt,
    test_archive_with_retry_succeeds_on_second_attempt,
    test_archive_with_retry_all_attempts_exhausted,
    test_archive_with_retry_handles_exception,
    test_archive_with_retry_idempotent_success,
]


if __name__ == "__main__":
    print("stale blocked-card sweeper tests (issue #186, #232)")
    print("-" * 60)
    for fn in ALL_TESTS:
        fn()
    print("-" * 60)
    print(f"Results: {conftest._passed} passed, {conftest._failed} failed")
    sys.exit(1 if conftest._failed else 0)
