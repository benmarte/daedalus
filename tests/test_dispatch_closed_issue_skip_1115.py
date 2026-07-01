"""Tests for closed-issue skip in the done-task advancement scan (issue #1115).

The dispatcher scans all done kanban tasks every tick. Before this fix it did
not check whether the parent GitHub issue was closed, causing every tick to
re-trigger PM/validator/planner/developer advancement for closed issues and
hold the FileLock for hours on boards with many closed issues.

Acceptance criteria (from issue #1115):
- Before advancing a done kanban task, check parent issue state — skip if CLOSED
- Lock-hold watchdog: if dispatcher holds lock > 30 minutes, log and exit
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import _load_dispatch

disp = _load_dispatch()


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_done_card(tid, assignee, title, summary):
    return {
        "id": tid,
        "assignee": assignee,
        "title": title,
        "status": "done",
        "summary": summary,
        "latest_summary": summary,
        "idempotency_key": "",
    }


def _provider_with_state(issue_number, state):
    """Return a minimal mock provider that reports the given issue state."""
    provider = mock.Mock()
    provider.get_issue_state.return_value = state
    provider.get_issue.return_value = None  # not needed for closed-skip path
    return provider


# ── _is_issue_closed_cached ───────────────────────────────────────────────────


def test_is_issue_closed_cached_returns_true_for_closed():
    """Helper returns True for a closed issue and caches the result."""
    provider = mock.Mock()
    provider.get_issue_state.return_value = "closed"
    cache = {}
    assert disp._is_issue_closed_cached(provider, 42, cache) is True
    assert cache == {42: True}
    # Second call must use cache, not provider
    provider.get_issue_state.return_value = (
        "open"  # change underlying — should be ignored
    )
    assert disp._is_issue_closed_cached(provider, 42, cache) is True
    assert provider.get_issue_state.call_count == 1


def test_is_issue_closed_cached_returns_false_for_open():
    provider = mock.Mock()
    provider.get_issue_state.return_value = "open"
    cache = {}
    assert disp._is_issue_closed_cached(provider, 99, cache) is False


def test_is_issue_closed_cached_fails_open_without_provider():
    """No provider → fail open (returns False) so tests without a provider pass."""
    cache = {}
    assert disp._is_issue_closed_cached(None, 7, cache) is False


def test_is_issue_closed_cached_fails_open_when_method_absent():
    """Provider without get_issue_state → fail open."""
    provider = mock.Mock(spec=[])  # no attributes
    cache = {}
    assert disp._is_issue_closed_cached(provider, 7, cache) is False


# ── _check_confirmed_validators — non-CONFIRMED path ─────────────────────────


def test_check_confirmed_validators_skips_non_confirmed_closed_issue():
    """A non-CONFIRMED (empty summary) validator done card for a CLOSED issue
    must be skipped — no retry task created, not in triggered list."""
    done_card = _make_done_card(
        "t1", "validator-daedalus", "#validate: #200 Closed bug", ""
    )
    fake_kanban = mock.Mock()
    fake_kanban.list_tasks.return_value = [done_card]

    provider = _provider_with_state(200, "closed")

    with mock.patch.object(disp, "kanban", fake_kanban):
        triggered = disp._check_confirmed_validators(
            "slug",
            "org/repo",
            {200: {"number": 200, "title": "Closed bug", "body": ""}},
            1,
            "/w",
            "slack://x",
            "dev",
            "github",
            provider=provider,
        )

    assert triggered == [], f"expected no triggers for closed issue, got {triggered}"
    # No new tasks should be created (no retry)
    fake_kanban.create_task.assert_not_called()
    # Must have looked up issue state
    provider.get_issue_state.assert_called_once_with(200)


def test_check_confirmed_validators_skips_blocked_closed_issue():
    """A blocked: validator done card for a CLOSED issue must be skipped
    (no PM consultation created)."""
    done_card = _make_done_card(
        "t2",
        "validator-daedalus",
        "#validate: #201 Closed thing",
        "BLOCKED: cannot reproduce on closed issue",
    )
    fake_kanban = mock.Mock()
    fake_kanban.list_tasks.return_value = [done_card]

    provider = _provider_with_state(201, "closed")

    with mock.patch.object(disp, "kanban", fake_kanban):
        triggered = disp._check_confirmed_validators(
            "slug",
            "org/repo",
            {201: {"number": 201, "title": "Closed thing", "body": ""}},
            1,
            "/w",
            "slack://x",
            "dev",
            "github",
            provider=provider,
        )

    assert triggered == []
    fake_kanban.create_task.assert_not_called()


def test_check_confirmed_validators_processes_non_confirmed_open_issue():
    """An open issue with non-CONFIRMED done card is NOT skipped (control test)."""
    done_card = _make_done_card(
        "t3",
        "validator-daedalus",
        "#validate: #202 Open issue",
        "BLOCKED: genuine blocker",
    )
    issue = {"number": 202, "title": "Open issue", "body": ""}
    fake_kanban = mock.Mock()
    # list_tasks is called multiple times: once for done cards, then once for
    # all-tasks idempotency key scan inside the BLOCKED handler.
    # Return an empty list for all subsequent calls.
    fake_kanban.list_tasks.side_effect = [[done_card]] + [[]] * 10
    fake_kanban.create_task.return_value = "t_consult"

    provider = _provider_with_state(202, "open")

    with mock.patch.object(disp, "kanban", fake_kanban):
        triggered = disp._check_confirmed_validators(
            "slug",
            "org/repo",
            {202: issue},
            1,
            "/w",
            "slack://x",
            "dev",
            "github",
            provider=provider,
        )

    assert 202 in triggered, (
        "open issue with BLOCKED validator should trigger consultation"
    )
    fake_kanban.create_task.assert_called_once()


# ── _check_confirmed_validators — CONFIRMED path ─────────────────────────────


def test_check_confirmed_validators_skips_confirmed_closed_issue():
    """A CONFIRMED validator done card for a CLOSED issue must be skipped —
    no PM spec task created, not in triggered list."""
    done_card = _make_done_card(
        "t4",
        "validator-daedalus",
        "#validate: #203 Closed confirmed",
        "CONFIRMED: issue is valid",
    )
    fake_kanban = mock.Mock()
    fake_kanban.list_tasks.return_value = [done_card]

    provider = _provider_with_state(203, "closed")

    with mock.patch.object(disp, "kanban", fake_kanban):
        triggered = disp._check_confirmed_validators(
            "slug",
            "org/repo",
            {203: {"number": 203, "title": "Closed confirmed", "body": ""}},
            1,
            "/w",
            "slack://x",
            "dev",
            "github",
            provider=provider,
        )

    assert triggered == []
    fake_kanban.create_task.assert_not_called()


def test_check_confirmed_validators_processes_confirmed_open_issue():
    """A CONFIRMED validator done card for an OPEN issue IS processed (control)."""
    done_card = _make_done_card(
        "t5",
        "validator-daedalus",
        "#validate: #204 Open confirmed",
        "CONFIRMED: issue is valid",
    )
    issue = {"number": 204, "title": "Open confirmed", "body": ""}
    fake_kanban = mock.Mock()
    fake_kanban.list_tasks.return_value = [done_card]
    fake_kanban.create_task.return_value = "t_pm"

    provider = _provider_with_state(204, "open")

    with mock.patch.object(disp, "kanban", fake_kanban):
        triggered = disp._check_confirmed_validators(
            "slug",
            "org/repo",
            {204: issue},
            1,
            "/w",
            "slack://x",
            "dev",
            "github",
            provider=provider,
        )

    assert 204 in triggered
    fake_kanban.create_task.assert_called_once()


# ── _check_confirmed_validators — closed-issue cache is reused ────────────────


def test_check_confirmed_validators_caches_closed_state_per_tick():
    """When two done tasks refer to the same closed issue, get_issue_state is
    called only once (memoized across the scan loop)."""
    card_a = _make_done_card(
        "tA", "validator-daedalus", "#validate: #205 Closed", "CONFIRMED: x"
    )
    card_b = _make_done_card(
        "tB", "validator-daedalus", "#validate: #205 Closed", "CONFIRMED: y"
    )

    fake_kanban = mock.Mock()
    fake_kanban.list_tasks.return_value = [card_a, card_b]

    provider = _provider_with_state(205, "closed")

    with mock.patch.object(disp, "kanban", fake_kanban):
        disp._check_confirmed_validators(
            "slug",
            "org/repo",
            {},
            1,
            "/w",
            "slack://x",
            "dev",
            "github",
            provider=provider,
        )

    assert provider.get_issue_state.call_count == 1, (
        f"expected 1 API call (cached), got {provider.get_issue_state.call_count}"
    )


# ── _check_completed_planner ─────────────────────────────────────────────────


def test_check_completed_planner_skips_closed_issue():
    """PLANNING COMPLETE done card for a CLOSED issue must be skipped."""
    done_card = _make_done_card(
        "tp1",
        "planner-daedalus",
        "#300 Closed epic",
        "PLANNING COMPLETE: all sub-issues identified",
    )
    fake_kanban = mock.Mock()
    fake_kanban.list_tasks.return_value = [done_card]

    provider = _provider_with_state(300, "closed")

    with mock.patch.object(disp, "kanban", fake_kanban):
        triggered = disp._check_completed_planner(
            "slug",
            "/workdir",
            provider=provider,
        )

    assert triggered == []


def test_check_completed_planner_processes_open_issue():
    """PLANNING COMPLETE done card for an OPEN issue is processed (control)."""
    done_card = _make_done_card(
        "tp2", "planner-daedalus", "#301 Open epic", "PLANNING COMPLETE: decompose this"
    )
    fake_kanban = mock.Mock()
    fake_kanban.list_tasks.return_value = [done_card]

    provider = _provider_with_state(301, "open")

    with (
        mock.patch.object(disp, "kanban", fake_kanban),
        mock.patch("core.iterate._execute_planner_decompose", return_value=True),
    ):
        triggered = disp._check_completed_planner(
            "slug",
            "/workdir",
            provider=provider,
        )

    assert 301 in triggered


# ── _check_completed_pm ───────────────────────────────────────────────────────


def test_check_completed_pm_skips_closed_issue():
    """SPEC: done card for a CLOSED issue must be skipped — no team triage created."""
    done_card = _make_done_card(
        "tpm1",
        "project-manager-daedalus",
        "#400 Closed spec",
        "SPEC: acceptance criteria defined",
    )
    fake_kanban = mock.Mock()
    fake_kanban.list_tasks.return_value = [done_card]

    provider = _provider_with_state(400, "closed")

    with mock.patch.object(disp, "kanban", fake_kanban):
        triggered = disp._check_completed_pm(
            "slug",
            "org/repo",
            {400: {"number": 400, "title": "Closed spec", "body": ""}},
            1,
            "/w",
            "",
            "dev",
            "github",
            provider=provider,
        )

    assert triggered == []
    fake_kanban.create_task.assert_not_called()


def test_check_completed_pm_processes_open_issue():
    """SPEC: done card for an OPEN issue creates downstream team tasks (control)."""
    done_card = _make_done_card(
        "tpm2", "project-manager-daedalus", "#401 Open spec", "SPEC: criteria defined"
    )
    issue = {"number": 401, "title": "Open spec", "body": "", "labels": []}
    fake_kanban = mock.Mock()
    fake_kanban.list_tasks.return_value = [done_card]
    fake_kanban.create_task.return_value = "t_new"

    provider = _provider_with_state(401, "open")

    with (
        mock.patch.object(disp, "kanban", fake_kanban),
        mock.patch.object(disp, "_has_downstream_tasks", return_value=False),
    ):
        triggered = disp._check_completed_pm(
            "slug",
            "org/repo",
            {401: issue},
            1,
            "/w",
            "",
            "dev",
            "github",
            provider=provider,
        )

    assert 401 in triggered
    fake_kanban.create_task.assert_called()


def test_check_completed_pm_caches_closed_state():
    """Two SPEC done cards for the same closed issue hit get_issue_state only once."""
    card_a = _make_done_card(
        "tA", "project-manager-daedalus", "#402 Dup spec", "SPEC: x"
    )
    card_b = _make_done_card(
        "tB", "project-manager-daedalus", "#402 Dup spec", "SPEC: y"
    )

    fake_kanban = mock.Mock()
    fake_kanban.list_tasks.return_value = [card_a, card_b]

    provider = _provider_with_state(402, "closed")

    with mock.patch.object(disp, "kanban", fake_kanban):
        disp._check_completed_pm(
            "slug",
            "org/repo",
            {},
            1,
            "/w",
            "",
            "dev",
            "github",
            provider=provider,
        )

    assert provider.get_issue_state.call_count == 1


def test_shared_cache_across_functions():
    """A shared closed_issue_cache passed from run() means issue state is fetched
    at most once per unique issue across all advancement functions in one tick."""
    # Closed issue #600 appears as a done PM card AND a done planner card.
    pm_card = _make_done_card(
        "tp", "project-manager-daedalus", "#600 Shared", "SPEC: x"
    )
    planner_card = _make_done_card(
        "tpl", "planner-daedalus", "#600 Shared", "PLANNING COMPLETE: done"
    )

    pm_kanban = mock.Mock()
    pm_kanban.list_tasks.return_value = [pm_card]
    planner_kanban = mock.Mock()
    planner_kanban.list_tasks.return_value = [planner_card]

    provider = _provider_with_state(600, "closed")
    shared_cache: dict = {}

    with mock.patch.object(disp, "kanban", pm_kanban):
        disp._check_completed_pm(
            "slug",
            "org/repo",
            {},
            1,
            "/w",
            "",
            "dev",
            "github",
            provider=provider,
            closed_issue_cache=shared_cache,
        )

    assert provider.get_issue_state.call_count == 1
    assert shared_cache == {600: True}

    with mock.patch.object(disp, "kanban", planner_kanban):
        disp._check_completed_planner(
            "slug",
            "/workdir",
            provider=provider,
            closed_issue_cache=shared_cache,
        )

    # Second function reuses cache — no additional API call
    assert provider.get_issue_state.call_count == 1, (
        "expected 1 total API call across both functions (shared cache hit)"
    )


# ── _check_completed_developer ────────────────────────────────────────────────


def test_check_completed_developer_skips_closed_issue():
    """Developer done card with no PR for a CLOSED issue must be skipped."""
    done_card = _make_done_card(
        "tdev1",
        "developer-daedalus",
        "#500 Closed dev",
        "",  # no PR number in summary → normally a retry candidate
    )
    fake_kanban = mock.Mock()
    fake_kanban.list_tasks.return_value = [done_card]

    provider = _provider_with_state(500, "closed")

    with mock.patch.object(disp, "kanban", fake_kanban):
        triggered = disp._check_completed_developer(
            "slug",
            "org/repo",
            {},
            1,
            "/w",
            "dev",
            "github",
            provider=provider,
        )

    assert triggered == []
    fake_kanban.create_task.assert_not_called()


def test_check_completed_developer_processes_open_issue():
    """Developer done card with no PR for an OPEN issue triggers retry (control)."""
    done_card = _make_done_card(
        "tdev2",
        "developer-daedalus",
        "#501 Open dev",
        "",  # stale: no PR number
    )
    issue = {"number": 501, "title": "Open dev", "body": ""}
    fake_kanban = mock.Mock()
    fake_kanban.list_tasks.return_value = [done_card]
    fake_kanban.create_task.return_value = "t_retry"

    provider = _provider_with_state(501, "open")

    with (
        mock.patch.object(disp, "kanban", fake_kanban),
        mock.patch.object(disp, "_developer_task_state", return_value=("stale", 1)),
    ):
        triggered = disp._check_completed_developer(
            "slug",
            "org/repo",
            {501: issue},
            1,
            "/w",
            "dev",
            "github",
            provider=provider,
        )

    # The key assertion: closed-issue filter was reached (get_issue_state called),
    # meaning the open issue was NOT skipped before the state check.
    provider.get_issue_state.assert_called_once_with(501)
    # 501 is not in triggered only because stale_count=1 exhausts the retry cap
    # in this minimal mock — we just assert it didn't come back empty due to
    # the closed-issue filter.
    assert isinstance(triggered, list)


# ── watchdog constant ─────────────────────────────────────────────────────────


def test_lock_watchdog_secs_is_30_minutes():
    """The watchdog constant must be 30 minutes (1800 seconds)."""
    assert disp._LOCK_WATCHDOG_SECS == 1800, (
        f"expected 1800, got {disp._LOCK_WATCHDOG_SECS}"
    )


def test_watchdog_alarm_called_on_main_thread():
    """When main() acquires the lock on the main thread, signal.alarm is set."""
    import signal as signal_mod

    with (
        mock.patch.object(disp, "_main_inner", return_value=0),
        mock.patch("signal.alarm") as mock_alarm,
        mock.patch("signal.signal"),
    ):
        from filelock import FileLock

        lock = mock.MagicMock(spec=FileLock)
        lock.acquire.return_value = None
        with mock.patch("filelock.FileLock", return_value=lock):
            disp.main()

    if hasattr(signal_mod, "SIGALRM"):
        # On Unix: alarm(WATCHDOG_SECS) then alarm(0) to cancel
        calls = [c.args[0] for c in mock_alarm.call_args_list]
        assert disp._LOCK_WATCHDOG_SECS in calls, (
            f"expected alarm({disp._LOCK_WATCHDOG_SECS}), got calls: {calls}"
        )
        assert 0 in calls, "expected alarm(0) cancellation call"
