"""Tests for the stale-card sweeper (issues #186, #232).

Covers core.sweeper: blocked_since timestamp fallback, the pure find_stale_blocked
detector, and sweep_stale_blocked (warn / archive / dry-run / DB-enrich), plus the
running-card detection added in #232 (find_stale_running / sweep_stale_running).

Dual-mode: runs under pytest AND standalone (``python tests/test_sweeper.py``)
via the shared check() helper, mirroring the other suites.
"""

from __future__ import annotations

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


def test_find_stale_running_default_threshold_is_24h():
    cards = [_card("t1", status="running", last_heartbeat_at=NOW - 25 * HOUR)]
    check("25h running card stale at default threshold",
          len(sweeper.find_stale_running(cards, now=NOW)) == 1)
    check("DEFAULT_RUNNING_STALE_HOURS is 24", sweeper.DEFAULT_RUNNING_STALE_HOURS == 24)


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


# ── sweep_stale_running: warn-only, no archive ────────────────────────────────


def test_sweep_running_returns_stale_ids():
    cards = [_card("t1", status="running", last_heartbeat_at=NOW - 30 * HOUR),
             _card("t2", status="running", last_heartbeat_at=NOW - 1 * HOUR)]
    with mock.patch.object(kanban, "list_tasks", return_value=cards):
        ids = sweeper.sweep_stale_running("daedalus", now=NOW)
    check("only the 30h running card is returned", ids == ["t1"])


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
    test_sweep_returns_stale_ids_and_does_not_archive_by_default,
    test_sweep_archives_when_enabled,
    test_sweep_dry_run_does_not_archive,
    test_sweep_empty_board,
    test_sweep_enriches_heartbeat_from_db,
    test_find_stale_running_detects_old_running,
    test_find_stale_running_ignores_fresh_running,
    test_find_stale_running_ignores_blocked,
    test_find_stale_running_default_threshold_is_24h,
    test_find_stale_running_skips_unaged_card,
    test_find_stale_running_sorts_oldest_first,
    test_sweep_running_returns_stale_ids,
    test_sweep_running_queries_running_status,
    test_sweep_running_empty_board,
    test_sweep_running_enriches_heartbeat_from_db,
    test_heartbeats_missing_db_returns_empty,
    test_heartbeats_empty_ids_returns_empty,
]


if __name__ == "__main__":
    print("stale blocked-card sweeper tests (issue #186)")
    print("-" * 60)
    for fn in ALL_TESTS:
        fn()
    print("-" * 60)
    print(f"Results: {conftest._passed} passed, {conftest._failed} failed")
    sys.exit(1 if conftest._failed else 0)
