"""Tests for the closed-issue guard gaps fixed in issue #1120.

PR #1117 (issue #1115) added ``_is_issue_closed_cached()`` to 5 advancement
scans but (a) missed ``_check_planner_not_suitable()`` — which kept spawning
validators for closed issues #1072/#1100 — and (b) let the helper *fail open*
(return False = "open") when ``provider.get_issue_state`` raised a 403
rate-limit error, so the stale scan reinforced the very rate limiting that
defeats the guard.

Acceptance criteria (issue #1120):
1. ``_check_planner_not_suitable()`` skips issues where the helper reports closed.
2. ``_is_issue_closed_cached()`` is three-state: True=closed, False=open,
   None=unknown/rate-limited; callers treat None as skip (do not process).
4. No regression in the 5 already-patched call sites.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import _load_dispatch  # noqa: E402
from test_dispatch_closed_issue_skip_1115 import (  # noqa: E402
    _make_done_card,
    _provider_with_state,
)
from test_planner_not_suitable import (  # noqa: E402
    _check,
    _issue_map_entry,
    _make_planner_task,
)

disp = _load_dispatch()


def _provider_raising(exc: Exception):
    """Return a mock provider whose get_issue_state raises (rate-limit sim)."""
    provider = mock.Mock()
    provider.get_issue_state.side_effect = exc
    return provider


# ── AC2: _is_issue_closed_cached three-state return ──────────────────────────


def test_is_issue_closed_cached_returns_none_on_exception():
    """A raising get_issue_state (403 rate limit) → None (unknown), not False."""
    provider = _provider_raising(RuntimeError("403 API rate limit exceeded"))
    cache = {}
    result = disp._is_issue_closed_cached(provider, 42, cache)
    assert result is None
    assert result is not False, "unknown must be distinguishable from confirmed-open"
    assert cache == {42: None}


def test_is_issue_closed_cached_caches_none_result():
    """The unknown result is cached so a rate-limited tick makes one call only."""
    provider = _provider_raising(RuntimeError("403 rate limit"))
    cache = {}
    assert disp._is_issue_closed_cached(provider, 7, cache) is None
    assert disp._is_issue_closed_cached(provider, 7, cache) is None
    assert provider.get_issue_state.call_count == 1


# ── AC1: _check_planner_not_suitable honours the closed-issue guard ──────────


def test_planner_not_suitable_skips_closed_issue():
    """Regression for #1072/#1100: no validator for a closed issue."""
    task = _make_planner_task(1072, "NOT SUITABLE FOR DECOMPOSITION: already tiny")
    provider = _provider_with_state(1072, "closed")
    triggered, created = _check(
        done_tasks=[task], issues_map=_issue_map_entry(1072), provider=provider
    )
    assert triggered == []
    assert created == []
    provider.get_issue_state.assert_called_once_with(1072)


def test_planner_not_suitable_processes_open_issue():
    """Control: an OPEN issue still routes to a validator."""
    task = _make_planner_task(1073, "NOT SUITABLE FOR DECOMPOSITION: single file")
    provider = _provider_with_state(1073, "open")
    triggered, created = _check(
        done_tasks=[task], issues_map=_issue_map_entry(1073), provider=provider
    )
    assert triggered == [1073]
    assert created, "an open issue must still create a validator task"


def test_planner_not_suitable_skips_when_rate_limited():
    """AC2 for the new call site: unknown (None) state → skip, do not process."""
    task = _make_planner_task(1100, "NOT SUITABLE FOR DECOMPOSITION: rate limited")
    provider = _provider_raising(RuntimeError("403 API rate limit exceeded"))
    triggered, created = _check(
        done_tasks=[task], issues_map=_issue_map_entry(1100), provider=provider
    )
    assert triggered == []
    assert created == []


def test_planner_not_suitable_without_provider_still_processes():
    """No provider → fail open (False) so provider-less tests are unaffected."""
    task = _make_planner_task(55, "NOT SUITABLE FOR DECOMPOSITION: reason")
    triggered, created = _check(
        done_tasks=[task], issues_map=_issue_map_entry(55), provider=None
    )
    assert triggered == [55]
    assert created


# ── AC4: existing patched sites treat unknown (None) as skip ─────────────────


def test_confirmed_validators_skips_on_rate_limit():
    """Representative already-patched site: a raising get_issue_state must NOT
    be treated as open — the stale scan skips rather than reinforcing the rate
    limit (#1120)."""
    done_card = _make_done_card(
        "t_rl",
        "validator-daedalus",
        "#validate: #300 Rate limited",
        "CONFIRMED: issue is valid",
    )
    fake_kanban = mock.Mock()
    fake_kanban.list_tasks.return_value = [done_card]

    provider = _provider_raising(RuntimeError("403 API rate limit exceeded"))

    with mock.patch.object(disp, "kanban", fake_kanban):
        triggered = disp._check_confirmed_validators(
            "slug",
            "org/repo",
            {300: {"number": 300, "title": "Rate limited", "body": ""}},
            1,
            "/w",
            "slack://x",
            "dev",
            "github",
            provider=provider,
        )

    assert triggered == []
    fake_kanban.create_task.assert_not_called()
