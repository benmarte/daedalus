#!/usr/bin/env python3
"""Tests for retry cap notification coverage gaps.

Covers paths not addressed by existing test files:
  1. GitHub comment posting on retry cap exhaustion (validator + PM)
  2. GitHub comment failure handling (returns False, raises)
  3. _has_notified_block idempotency integration (prevents re-send)
  4. _mark_notified_block skipped in dry_run mode
  5. PM role-specific retry attempt body content
  6. Validator role-specific retry attempt body content

Run: pytest tests/test_retry_cap_notification_gaps.py -v
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from conftest import _load_dispatch  # noqa: E402


@pytest.fixture
def disp():
    return _load_dispatch()


def _minimal_resolved(*, notifications=None, execution=None):
    cron = {}
    if notifications is not None:
        cron["notifications"] = notifications
    result = {"cron": cron}
    if execution is not None:
        result["execution"] = execution
    return result


def _default_profile():
    return {"validator": "validator-daedalus", "pm": "project-manager-daedalus"}


def _fake_provider():
    """Return a mock provider with post_issue_comment."""
    provider = mock.MagicMock()
    provider.post_issue_comment.return_value = True
    return provider


# ── 1. GitHub comment on retry cap exhaustion (validator) ─────────────────────


class TestGitHubCommentOnValidatorCap:
    """When validator retry cap is exhausted and provider is available, a GitHub
    comment is posted on the issue."""

    def test_posts_github_comment_on_validator_cap_exhaustion(self, disp):
        """provider.post_issue_comment called with retry-cap comment for validator."""
        fake_tasks = [
            {"id": "t1", "title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done"},
            {"id": "t2", "title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done"},
            {"id": "t3", "title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done"},
        ]
        provider = _fake_provider()
        resolved = _minimal_resolved(
            notifications=[{"platform": "Slack", "target": "slack:ops", "events": ["retry-cap-exhausted"]}]
        )

        with mock.patch.object(disp.kanban, "list_tasks", return_value=fake_tasks), \
             mock.patch.object(disp.kanban, "show_card", return_value={"comments": []}), \
             mock.patch.object(disp.kanban, "comment"), \
             mock.patch.object(disp, "_validator_github_comment_outcome", return_value=""), \
             mock.patch.object(disp, "_send_retry_cap_notification"), \
             mock.patch.object(disp, "_send_retry_attempt_notification"):

            disp._check_confirmed_validators(
                "slug", "owner/repo",
                {42: {"number": 42, "title": "fix bug", "body": ""}},
                iterations=1, workdir="/tmp", notify_target="", base_branch="main",
                provider_name="github", provider=provider, resolved=resolved,
                profiles=_default_profile(),
            )

            provider.post_issue_comment.assert_called()
            comment_body = provider.post_issue_comment.call_args[0][1]
            assert "#42" in comment_body
            assert "retry cap" in comment_body.lower() or "cap exhausted" in comment_body.lower()
            assert "manual intervention" in comment_body.lower()

    def test_no_github_comment_when_provider_is_none(self, disp):
        """When provider is None, no GitHub comment is attempted."""
        fake_tasks = [
            {"id": "t1", "title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done"},
            {"id": "t2", "title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done"},
            {"id": "t3", "title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done"},
        ]
        resolved = _minimal_resolved(
            notifications=[{"platform": "Slack", "target": "slack:ops", "events": ["retry-cap-exhausted"]}]
        )

        with mock.patch.object(disp.kanban, "list_tasks", return_value=fake_tasks), \
             mock.patch.object(disp.kanban, "show_card", return_value={"comments": []}), \
             mock.patch.object(disp.kanban, "comment"), \
             mock.patch.object(disp, "_validator_github_comment_outcome", return_value=""), \
             mock.patch.object(disp, "_send_retry_cap_notification") as mock_notify:

            disp._check_confirmed_validators(
                "slug", "owner/repo",
                {42: {"number": 42, "title": "fix bug", "body": ""}},
                iterations=1, workdir="/tmp", notify_target="", base_branch="main",
                provider_name="github", provider=None, resolved=resolved,
                profiles=_default_profile(),
            )

            # Notification still fires — but no GitHub comment to post
            mock_notify.assert_called()


# ── 2. GitHub comment on retry cap exhaustion (PM) ───────────────────────────


class TestGitHubCommentOnPMCap:
    """When PM retry cap is exhausted and provider is available, a GitHub
    comment is posted on the issue."""

    def test_posts_github_comment_on_pm_cap_exhaustion(self, disp):
        """provider.post_issue_comment called with retry-cap comment for PM."""
        fake_tasks = [
            {"id": "t_v1", "title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done",
             "summary": "CONFIRMED: valid issue"},
        ]
        provider = _fake_provider()
        resolved = _minimal_resolved(
            notifications=[{"platform": "Slack", "target": "slack:ops", "events": ["retry-cap-exhausted"]}]
        )

        def fake_pm_task_state(slug, issue_nr, pm_profile):
            return ("stale", 3)  # stale_count=3 >= _MAX_PM_RETRIES=3

        with mock.patch.object(disp.kanban, "list_tasks", return_value=fake_tasks), \
             mock.patch.object(disp.kanban, "show_card", return_value={"comments": []}), \
             mock.patch.object(disp.kanban, "comment"), \
             mock.patch.object(disp, "_validator_github_comment_outcome", return_value=""), \
             mock.patch.object(disp, "_pm_task_state", side_effect=fake_pm_task_state), \
             mock.patch.object(disp, "_send_retry_cap_notification"), \
             mock.patch.object(disp, "_send_retry_attempt_notification"):

            disp._check_confirmed_validators(
                "slug", "owner/repo",
                {42: {"number": 42, "title": "fix bug", "body": ""}},
                iterations=1, workdir="/tmp", notify_target="", base_branch="main",
                provider_name="github", provider=provider, resolved=resolved,
                profiles=_default_profile(),
            )

            provider.post_issue_comment.assert_called()
            comment_body = provider.post_issue_comment.call_args[0][1]
            assert "#42" in comment_body
            assert "project manager" in comment_body.lower() or "pm" in comment_body.lower()
            assert "spec:" in comment_body.lower() or "manual intervention" in comment_body.lower()


# ── 3. GitHub comment failure handling ────────────────────────────────────────


class TestGitHubCommentFailureHandling:
    """When provider.post_issue_comment fails, the error is logged but not raised."""

    def test_post_issue_comment_returns_false_does_not_raise(self, disp):
        """provider.post_issue_comment returning False → logged, not raised."""
        fake_tasks = [
            {"id": "t1", "title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done"},
            {"id": "t2", "title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done"},
            {"id": "t3", "title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done"},
        ]
        provider = _fake_provider()
        provider.post_issue_comment.return_value = False  # Failure
        resolved = _minimal_resolved(
            notifications=[{"platform": "Slack", "target": "slack:ops", "events": ["retry-cap-exhausted"]}]
        )

        with mock.patch.object(disp.kanban, "list_tasks", return_value=fake_tasks), \
             mock.patch.object(disp.kanban, "show_card", return_value={"comments": []}), \
             mock.patch.object(disp.kanban, "comment"), \
             mock.patch.object(disp, "_validator_github_comment_outcome", return_value=""), \
             mock.patch.object(disp, "_send_retry_cap_notification"), \
             mock.patch.object(disp, "_send_retry_attempt_notification"):
            # Should not raise even though post_issue_comment returned False
            disp._check_confirmed_validators(
                "slug", "owner/repo",
                {42: {"number": 42, "title": "fix bug", "body": ""}},
                iterations=1, workdir="/tmp", notify_target="", base_branch="main",
                provider_name="github", provider=provider, resolved=resolved,
                profiles=_default_profile(),
            )
        # Test passes if no exception was raised

    def test_post_issue_comment_raises_does_not_raise(self, disp):
        """provider.post_issue_comment raising Exception → logged, not raised."""
        fake_tasks = [
            {"id": "t1", "title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done"},
            {"id": "t2", "title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done"},
            {"id": "t3", "title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done"},
        ]
        provider = _fake_provider()
        provider.post_issue_comment.side_effect = RuntimeError("Network unavailable")
        resolved = _minimal_resolved(
            notifications=[{"platform": "Slack", "target": "slack:ops", "events": ["retry-cap-exhausted"]}]
        )

        with mock.patch.object(disp.kanban, "list_tasks", return_value=fake_tasks), \
             mock.patch.object(disp.kanban, "show_card", return_value={"comments": []}), \
             mock.patch.object(disp.kanban, "comment"), \
             mock.patch.object(disp, "_validator_github_comment_outcome", return_value=""), \
             mock.patch.object(disp, "_send_retry_cap_notification"), \
             mock.patch.object(disp, "_send_retry_attempt_notification"):
            # Should not raise even though post_issue_comment raised
            disp._check_confirmed_validators(
                "slug", "owner/repo",
                {42: {"number": 42, "title": "fix bug", "body": ""}},
                iterations=1, workdir="/tmp", notify_target="", base_branch="main",
                provider_name="github", provider=provider, resolved=resolved,
                profiles=_default_profile(),
            )
        # Test passes if no exception was raised


# ── 4. _has_notified_block idempotency integration ────────────────────────────


class TestHasNotifiedBlockIdempotency:
    """When _has_notified_block returns True (already notified), cap notification
    is NOT sent again on subsequent ticks."""

    def test_already_notified_skips_notification_and_comment(self, disp):
        """_has_notified_block returning True prevents re-sending cap notification."""
        fake_tasks = [
            {"id": "t1", "title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done"},
            {"id": "t2", "title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done"},
            {"id": "t3", "title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done"},
        ]
        provider = _fake_provider()
        resolved = _minimal_resolved(
            notifications=[{"platform": "Slack", "target": "slack:ops", "events": ["retry-cap-exhausted"]}]
        )

        with mock.patch.object(disp.kanban, "list_tasks", return_value=fake_tasks), \
             mock.patch.object(disp.kanban, "show_card", return_value={"comments": []}), \
             mock.patch.object(disp.kanban, "comment"), \
             mock.patch.object(disp, "_validator_github_comment_outcome", return_value=""), \
             mock.patch.object(disp, "_has_notified_block", return_value=True), \
             mock.patch.object(disp, "_mark_notified_block") as mock_mark, \
             mock.patch.object(disp, "_send_retry_cap_notification") as mock_notify, \
             mock.patch.object(disp, "_send_retry_attempt_notification"):

            disp._check_confirmed_validators(
                "slug", "owner/repo",
                {42: {"number": 42, "title": "fix bug", "body": ""}},
                iterations=1, workdir="/tmp", notify_target="", base_branch="main",
                provider_name="github", provider=provider, resolved=resolved,
                profiles=_default_profile(),
            )

            mock_notify.assert_not_called()
            mock_mark.assert_not_called()
            provider.post_issue_comment.assert_not_called()


# ── 5. _mark_notified_block skipped in dry_run mode ───────────────────────────


class TestMarkNotifiedBlockDryRun:
    """In dry_run mode, _mark_notified_block is NOT called (no marker stamping)."""

    def test_dry_run_does_not_stamp_marker(self, disp):
        """dry_run=True → _mark_notified_block not called."""
        fake_tasks = [
            {"id": "t1", "title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done"},
            {"id": "t2", "title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done"},
            {"id": "t3", "title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done"},
        ]
        resolved = _minimal_resolved(
            notifications=[{"platform": "Slack", "target": "slack:ops", "events": ["retry-cap-exhausted"]}]
        )

        with mock.patch.object(disp.kanban, "list_tasks", return_value=fake_tasks), \
             mock.patch.object(disp.kanban, "show_card", return_value={"comments": []}), \
             mock.patch.object(disp.kanban, "comment"), \
             mock.patch.object(disp, "_validator_github_comment_outcome", return_value=""), \
             mock.patch.object(disp, "_has_notified_block", return_value=False), \
             mock.patch.object(disp, "_mark_notified_block") as mock_mark, \
             mock.patch.object(disp, "_send_retry_cap_notification") as mock_notify, \
             mock.patch.object(disp, "_send_retry_attempt_notification"):

            disp._check_confirmed_validators(
                "slug", "owner/repo",
                {42: {"number": 42, "title": "fix bug", "body": ""}},
                iterations=1, workdir="/tmp", notify_target="", base_branch="main",
                provider_name="github", provider=None, resolved=resolved,
                profiles=_default_profile(),
                dry_run=True,
            )

            mock_notify.assert_called()  # Notification fires even in dry_run
            mock_mark.assert_not_called()  # But marker is NOT stamped


# ── 6. PM role-specific retry attempt body content ────────────────────────────


class TestPMRetryAttemptBodyContent:
    """PM retry attempt notification has correct PM-specific body content."""

    def test_pm_retry_attempt_includes_context(self, disp):
        """PM retry attempt body includes PM-specific context about SPEC: summary."""
        with mock.patch.object(disp, "_notify_targets", return_value=["slack:C123"]), \
             mock.patch.object(disp, "_hermes_send", return_value=(True, "msg")) as mock_send:
            resolved = _minimal_resolved()
            disp._send_retry_attempt_notification(
                role="pm",
                issue_number=200,
                retry_count=2,
                max_retries=3,
                resolved=resolved,
                dry_run=False,
            )
            body = mock_send.call_args[0][1]
            assert "PM" in body
            assert "#200" in body
            assert "2" in body and "3" in body
            assert "SPEC:" in body
            assert "Retry queued" in body or "dispatcher will spawn" in body

    def test_validator_retry_attempt_includes_context(self, disp):
        """Validator retry attempt body includes validator-specific context about CONFIRMED."""
        with mock.patch.object(disp, "_notify_targets", return_value=["slack:C123"]), \
             mock.patch.object(disp, "_hermes_send", return_value=(True, "msg")) as mock_send:
            resolved = _minimal_resolved()
            disp._send_retry_attempt_notification(
                role="validator",
                issue_number=300,
                retry_count=1,
                max_retries=2,
                resolved=resolved,
                dry_run=False,
            )
            body = mock_send.call_args[0][1]
            assert "validator" in body.lower() or "VALIDATOR" in body
            assert "#300" in body
            assert "CONFIRMED" in body
            assert "Retry queued" in body or "dispatcher will spawn" in body


# ── 7. Config-driven retry cap resolution ─────────────────────────────────────


class TestConfigDrivenRetryCap:
    """_resolve_max_validator_retries and _resolve_max_pm_retries are tested in
    test_config_options.py, but we test that the integration path through
    _check_confirmed_validators correctly reads from resolved config."""

    def test_validator_uses_config_max_retries(self, disp):
        """Custom max_validator_retries from config is used for cap check."""
        fake_tasks = [
            {"id": f"t{i}", "title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done"}
            for i in range(1, 6)  # 5 tasks → retry_count=5
        ]
        resolved = _minimal_resolved(
            execution={"max_validator_retries": 4},  # Cap at 4 → 5 >= 4+1 → triggers
            notifications=[{"platform": "Slack", "target": "slack:ops", "events": ["retry-cap-exhausted"]}]
        )

        with mock.patch.object(disp.kanban, "list_tasks", return_value=fake_tasks), \
             mock.patch.object(disp.kanban, "show_card", return_value={"comments": []}), \
             mock.patch.object(disp.kanban, "comment"), \
             mock.patch.object(disp, "_validator_github_comment_outcome", return_value=""), \
             mock.patch.object(disp, "_send_retry_cap_notification") as mock_notify, \
             mock.patch.object(disp, "_send_retry_attempt_notification"):

            disp._check_confirmed_validators(
                "slug", "owner/repo",
                {42: {"number": 42, "title": "fix bug", "body": ""}},
                iterations=1, workdir="/tmp", notify_target="", base_branch="main",
                provider_name="github", provider=None, resolved=resolved,
                profiles=_default_profile(),
            )

            mock_notify.assert_called()
            kw = mock_notify.call_args.kwargs
            assert kw["retry_count"] == 5
            assert kw["max_retries"] == 4

    def test_validator_default_max_retries_when_no_config(self, disp):
        """Default max_validator_retries (2) is used when no config provided."""
        fake_tasks = [
            {"id": "t1", "title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done"},
            {"id": "t2", "title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done"},
            {"id": "t3", "title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done"},
        ]
        resolved = _minimal_resolved()  # No execution config

        with mock.patch.object(disp.kanban, "list_tasks", return_value=fake_tasks), \
             mock.patch.object(disp.kanban, "show_card", return_value={"comments": []}), \
             mock.patch.object(disp.kanban, "comment"), \
             mock.patch.object(disp, "_validator_github_comment_outcome", return_value=""), \
             mock.patch.object(disp, "_send_retry_cap_notification") as mock_notify, \
             mock.patch.object(disp, "_send_retry_attempt_notification"):

            disp._check_confirmed_validators(
                "slug", "owner/repo",
                {42: {"number": 42, "title": "fix bug", "body": ""}},
                iterations=1, workdir="/tmp", notify_target="", base_branch="main",
                provider_name="github", provider=None, resolved=resolved,
                profiles=_default_profile(),
            )

            # retry_count=3 >= default max_validator_retries (2) + 1 = 3 → triggers
            mock_notify.assert_called()
            kw = mock_notify.call_args.kwargs
            assert kw["max_retries"] == 2


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
