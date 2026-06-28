"""Tests for issues_map miss fallback at all call sites (issue #115).

Verifies that when issues_map.get() returns None, the dispatcher falls back
to calling provider.get_issue() with retry logic at all 5 call sites:

1. _check_completed_pm (already tested in test_pipeline_scenarios.py)
2. _check_confirmed_validators - validator BLOCKED path
3. _check_confirmed_validators - gh-comment fallback path
4. _check_confirmed_validators - validator confirmed path
5. _check_team_blockers
"""

from conftest import FakeProvider, _load_dispatch, check
from core.providers.base import IssueSummary
import pytest


# ── _check_confirmed_validators: validator BLOCKED path ──────────────────────


def test_validator_blocked_fallback_recovers_on_retry():
    """_check_confirmed_validators: validator BLOCKED with issue not in issues_map
    falls back to get_issue() and recovers after transient failures."""
    disp = _load_dispatch()
    monkeypatch_sleep(disp)

    _summary = "BLOCKED: cannot resolve import"
    disp.kanban.list_tasks = lambda slug, status="done": [
        {
            "id": "t_v42",
            "title": "#42 feature",
            "assignee": "validator-daedalus",
            "status": "done",
            "summary": _summary,
            "last_summary": _summary,
        }
    ]
    disp.kanban.list_blocked = lambda s: []

    # Issue 42 not in issues_map, but provider.get_issue() succeeds after 2 failures
    provider = FakeProvider(
        issues={42: _make_issue_summary(42, "feature")},
        get_issue_failures=2,
    )

    triggered = disp._check_confirmed_validators(
        slug="slug",
        repo="owner/repo",
        issues_map={},  # empty — forces fallback
        iterations=3,
        workdir="/tmp",
        notify_target="",
        base_branch="dev",
        provider_name="github",
        provider=provider,
    )

    check("validator BLOCKED fallback recovers after retry", 42 in triggered)
    check("get_issue called 3 times (1 initial + 2 retries)", provider.get_issue_calls == 3)


def test_validator_blocked_fallback_persistent_failure_skips():
    """_check_confirmed_validators: validator BLOCKED with persistent get_issue()
    failures skips PM consultation creation."""
    disp = _load_dispatch()
    monkeypatch_sleep(disp)

    _summary = "BLOCKED: cannot resolve import"
    disp.kanban.list_tasks = lambda slug, status="done": [
        {
            "id": "t_v43",
            "title": "#43 feature",
            "assignee": "validator-daedalus",
            "status": "done",
            "summary": _summary,
            "last_summary": _summary,
        }
    ]
    disp.kanban.list_blocked = lambda s: []

    # Issue 43 not in issues_map, provider never returns it
    provider = FakeProvider(issues={}, get_issue_failures=99)

    triggered = disp._check_confirmed_validators(
        slug="slug",
        repo="owner/repo",
        issues_map={},
        iterations=3,
        workdir="/tmp",
        notify_target="",
        base_branch="dev",
        provider_name="github",
        provider=provider,
    )

    check("persistent failure skips creation", triggered == [])
    check("bounded retries exhausted", provider.get_issue_calls == len(disp._GET_ISSUE_RETRY_DELAYS) + 1)


# ── _check_confirmed_validators: validator confirmed path ────────────────────


def test_validator_confirmed_fallback_recovers_on_retry():
    """_check_confirmed_validators: validator CONFIRMED with issue not in issues_map
    falls back to get_issue() and recovers after transient failures."""
    disp = _load_dispatch()
    monkeypatch_sleep(disp)

    _summary = "CONFIRMED: issue is valid"
    disp.kanban.list_tasks = lambda slug, status="done": [
        {
            "id": "t_v44",
            "title": "#44 feature",
            "assignee": "validator-daedalus",
            "status": "done",
            "summary": _summary,
            "last_summary": _summary,
        }
    ]
    disp.kanban.list_blocked = lambda s: []

    # Issue 44 not in issues_map, but provider.get_issue() succeeds after 1 failure
    provider = FakeProvider(
        issues={44: _make_issue_summary(44, "feature")},
        get_issue_failures=1,
    )

    triggered = disp._check_confirmed_validators(
        slug="slug",
        repo="owner/repo",
        issues_map={},
        iterations=3,
        workdir="/tmp",
        notify_target="",
        base_branch="dev",
        provider_name="github",
        provider=provider,
    )

    check("validator CONFIRMED fallback recovers after retry", 44 in triggered)
    check("get_issue called 2 times (1 initial + 1 retry)", provider.get_issue_calls == 2)


# ── _check_confirmed_validators: gh-comment fallback path ────────────────────


def test_gh_comment_fallback_recovers_on_retry():
    """_check_confirmed_validators: gh-comment fallback path with issue not in
    issues_map falls back to get_issue() and recovers after transient failures."""
    from unittest import mock
    disp = _load_dispatch()
    monkeypatch_sleep(disp)

    # Validator with None summary — triggers gh-comment fallback path
    disp.kanban.list_tasks = lambda slug, status="done": [
        {
            "id": "t_v45",
            "title": "#45 feature",
            "assignee": "validator-daedalus",
            "status": "done",
            "summary": None,
            "last_summary": None,
        }
    ]
    disp.kanban.get_latest_summary = lambda s, t: None

    # Issue 45 not in issues_map, but provider.get_issue() succeeds after 1 failure
    provider = FakeProvider(
        issues={45: _make_issue_summary(45, "feature")},
        get_issue_failures=1,
    )

    with mock.patch.object(disp, "_validator_github_comment_outcome", return_value="confirmed"):
        triggered = disp._check_confirmed_validators(
            slug="slug",
            repo="owner/repo",
            issues_map={},
            iterations=3,
            workdir="/tmp",
            notify_target="",
            base_branch="dev",
            provider_name="github",
            provider=provider,
        )

    check("gh-comment fallback recovers after retry", 45 in triggered)
    check("get_issue called 2 times (1 initial + 1 retry)", provider.get_issue_calls == 2)


# ── _check_team_blockers ─────────────────────────────────────────────────────


def test_team_blockers_fallback_recovers_on_retry():
    """_check_team_blockers: with issue not in issues_map, falls back to
    get_issue() and recovers after transient failures."""
    disp = _load_dispatch()
    monkeypatch_sleep(disp)

    _summary = "BLOCKED: cannot resolve import"
    disp.kanban.list_blocked = lambda s: [
        {
            "id": "t_dev46",
            "title": "#46 feature",
            "assignee": "developer-daedalus",
            "summary": _summary,
        }
    ]
    disp.kanban.get_latest_summary = lambda s, tid: _summary
    disp.kanban.list_tasks = lambda s: []  # no active consultation

    # Issue 46 not in issues_map, but provider.get_issue() succeeds after 2 failures
    provider = FakeProvider(
        issues={46: _make_issue_summary(46, "feature")},
        get_issue_failures=2,
    )

    triggered = disp._check_team_blockers(
        slug="slug",
        repo="owner/repo",
        issues_map={},
        workdir="/tmp",
        base_branch="dev",
        provider_name="github",
        provider=provider,
    )

    check("team blocker fallback recovers after retry", 46 in triggered)
    check("get_issue called 3 times (1 initial + 2 retries)", provider.get_issue_calls == 3)


def test_team_blockers_fallback_persistent_failure_skips():
    """_check_team_blockers: with persistent get_issue() failures, skips
    PM consultation creation."""
    disp = _load_dispatch()
    monkeypatch_sleep(disp)

    _summary = "BLOCKED: cannot resolve import"
    disp.kanban.list_blocked = lambda s: [
        {
            "id": "t_dev47",
            "title": "#47 feature",
            "assignee": "developer-daedalus",
            "summary": _summary,
        }
    ]
    disp.kanban.get_latest_summary = lambda s, tid: _summary
    disp.kanban.list_tasks = lambda s: []

    # Issue 47 not in issues_map, provider never returns it
    provider = FakeProvider(issues={}, get_issue_failures=99)

    triggered = disp._check_team_blockers(
        slug="slug",
        repo="owner/repo",
        issues_map={},
        workdir="/tmp",
        base_branch="dev",
        provider_name="github",
        provider=provider,
    )

    check("persistent failure skips creation", triggered == [])
    check("bounded retries exhausted", provider.get_issue_calls == len(disp._GET_ISSUE_RETRY_DELAYS) + 1)


# ── helpers ───────────────────────────────────────────────────────────────────


def monkeypatch_sleep(disp):
    """Disable sleep() in retry logic to speed up tests."""
    disp.time.sleep = lambda _: None


def _make_issue_summary(number: int, title: str) -> IssueSummary:
    """Create a minimal IssueSummary for testing."""
    return IssueSummary(
        number=number,
        title=title,
        body="",
        labels=[],
        state="open",
    )
