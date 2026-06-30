#!/usr/bin/env python3
"""Regression test for issue t_928bfae8.

BUG: When validator retry_count == max_retries (e.g., 2/2), the dispatcher
fires _send_retry_attempt_notification with "run 2 of 2, Retry queued —
dispatcher will spawn another attempt" — which looks semantically identical to
the cap-exhausted notification that fires on the next tick.  Users receive two
nearly-identical notifications about the same failure.

FIX: Suppress _send_retry_attempt_notification when retry_count >= max_retries.
The cap-exhausted notification fires on the next tick, providing ONE distinct
notification instead of a duplicate pair.
"""
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_dispatch():
    import importlib.util
    p = ROOT / "scripts" / "daedalus_dispatch.py"
    spec = importlib.util.spec_from_file_location("disp_fix_t928", str(p))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _minimal_resolved(*, notifications=None):
    cron = {}
    if notifications is not None:
        cron["notifications"] = notifications
    return {"cron": cron}


def _default_profile():
    return {"validator": "validator-daedalus", "pm": "project-manager-daedalus"}


@pytest.fixture
def disp():
    return _load_dispatch()


# ── validator: at boundary → retry_attempt suppressed, only cap-exhausted fires ─

def test_validator_at_boundary_suppresses_retry_attempt(disp):
    """retry_count == max_retries (2/2) → retry-attempt suppressed, cap-exhausted will fire next tick."""
    # 2 completed validator tasks for #42 → retry_count = 2, which equals _MAX_VALIDATOR_RETRIES=2
    fake_tasks = [
        {"id": "t1", "title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done"},
        {"id": "t2", "title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done"},
    ]

    resolved = _minimal_resolved(notifications=[
        {"platform": "Slack", "target": "slack:ops", "events": ["retry-cap-exhausted"]},
        {"platform": "Slack", "target": "slack:ops2", "events": ["retry-attempt"]},
    ])

    with mock.patch.object(disp.kanban, "list_tasks", return_value=fake_tasks), \
         mock.patch.object(disp.kanban, "show_card", return_value={"comments": []}), \
         mock.patch.object(disp.kanban, "comment"), \
         mock.patch.object(disp, "_validator_github_comment_outcome", return_value=""), \
         mock.patch.object(disp, "_send_retry_cap_notification") as mock_cap, \
         mock.patch.object(disp, "_send_retry_attempt_notification") as mock_attempt:

        disp._check_confirmed_validators(
            "slug", "owner/repo",
            {42: {"number": 42, "title": "fix bug", "body": ""}},
            iterations=1, workdir="/tmp", notify_target="", base_branch="main",
            provider_name="github", provider=None, resolved=resolved,
            profiles=_default_profile(),
        )

        assert mock_attempt.call_count == 0, (
            "retry-attempt must be suppressed at boundary (retry_count == max_retries); "
            "cap-exhausted notification will fire on next tick instead"
        )
        # Cap-exhausted fires only when retry_count >= max_retries + 1 (i.e., 3/2),
        # so at 2/2 boundary it should NOT be called yet either.
        assert mock_cap.call_count == 0, (
            "cap-exhausted fires only when retry_count >= max_retries+1"
        )


def test_validator_below_boundary_fires_retry_attempt(disp):
    """retry_count < max_retries (1/2) → retry-attempt DOES fire (intermediate retry)."""
    fake_tasks = [
        {"id": "t1", "title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done"},
    ]
    # retry_count = 1 < _MAX_VALIDATOR_RETRIES=2 — intermediate retry
    resolved = _minimal_resolved(notifications=[
        {"platform": "Slack", "target": "slack:ops", "events": ["retry-attempt"]},
    ])

    with mock.patch.object(disp.kanban, "list_tasks", return_value=fake_tasks), \
         mock.patch.object(disp.kanban, "show_card", return_value={"comments": []}), \
         mock.patch.object(disp.kanban, "comment"), \
         mock.patch.object(disp, "_validator_github_comment_outcome", return_value=""), \
         mock.patch.object(disp, "_send_retry_cap_notification"), \
         mock.patch.object(disp, "_send_retry_attempt_notification") as mock_attempt:

        disp._check_confirmed_validators(
            "slug", "owner/repo",
            {42: {"number": 42, "title": "fix bug", "body": ""}},
            iterations=1, workdir="/tmp", notify_target="", base_branch="main",
            provider_name="github", provider=None, resolved=resolved,
            profiles=_default_profile(),
        )

        assert mock_attempt.call_count >= 1, "retry-attempt must fire below boundary"
        kw = mock_attempt.call_args_list[0].kwargs
        assert kw["retry_count"] == 1
        assert kw["max_retries"] == 2


def test_validator_over_boundary_fires_cap_exhausted(disp):
    """retry_count == max_retries + 1 (3/2) → cap-exhausted fires, retry-attempt suppressed."""
    # #916: a run only burns the cap with a real non-CONFIRMED verdict — empty summaries no longer do.
    _v = "ran but produced no clear verdict"
    fake_tasks = [
        {"id": "t1", "title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done", "summary": _v},
        {"id": "t2", "title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done", "summary": _v},
        {"id": "t3", "title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done", "summary": _v},
    ]

    resolved = _minimal_resolved(notifications=[
        {"platform": "Slack", "target": "slack:ops", "events": ["retry-cap-exhausted"]},
        {"platform": "Slack", "target": "slack:ops2", "events": ["retry-attempt"]},
    ])

    with mock.patch.object(disp.kanban, "list_tasks", return_value=fake_tasks), \
         mock.patch.object(disp.kanban, "show_card", return_value={"comments": []}), \
         mock.patch.object(disp.kanban, "comment"), \
         mock.patch.object(disp, "_validator_github_comment_outcome", return_value=""), \
         mock.patch.object(disp, "_has_notified_block", return_value=False), \
         mock.patch.object(disp, "_mark_notified_block"), \
         mock.patch.object(disp, "_send_retry_cap_notification") as mock_cap, \
         mock.patch.object(disp, "_send_retry_attempt_notification") as mock_attempt:

        disp._check_confirmed_validators(
            "slug", "owner/repo",
            {42: {"number": 42, "title": "fix bug", "body": ""}},
            iterations=1, workdir="/tmp", notify_target="", base_branch="main",
            provider_name="github", provider=None, resolved=resolved,
            profiles=_default_profile(),
        )

        assert mock_attempt.call_count == 0, (
            "retry-attempt must NOT fire past boundary (cap exhausted)"
        )
        assert mock_cap.called, "cap-exhausted must fire past boundary"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
