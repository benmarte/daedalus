"""Regression tests for planner 'NOT SUITABLE FOR DECOMPOSITION' bug fix.

These tests verify the fix for the bug where the planner returning 'NOT SUITABLE
FOR DECOMPOSITION' leaves issues stuck in 'In progress' forever because no
validator is created. The tests cover the regex pattern, validator body template,
retry logic, and end-to-end state machine transitions.

Refs: issue #931 / epic #918 / task t_5f0f4f85
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from unittest import mock
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import FakeKanban, FakeProvider, _load_dispatch  # noqa: E402

disp = _load_dispatch()


# ── Unit tests for _NOT_SUITABLE_RE ────────────────────────────────────────────


class TestNotSuitableRegex:
    """Test the regex pattern directly for signal detection."""

    def test_matches_full_signal(self):
        assert disp._NOT_SUITABLE_RE.search("NOT SUITABLE FOR DECOMPOSITION: reason")

    def test_matches_short_signal(self):
        assert disp._NOT_SUITABLE_RE.search("NOT SUITABLE: reason")

    def test_matches_case_insensitive(self):
        assert disp._NOT_SUITABLE_RE.search("not suitable for decomposition")
        assert disp._NOT_SUITABLE_RE.search("Not Suitable For Decomposition")
        assert disp._NOT_SUITABLE_RE.search("NOT suitable for decomposition")

    def test_matches_with_whitespace(self):
        assert disp._NOT_SUITABLE_RE.search("not  suitable  for  decomposition")
        assert disp._NOT_SUITABLE_RE.search("not\tsuitable\tfor\tdecomposition")
        assert disp._NOT_SUITABLE_RE.search("not\nsuitable\nfor\ndecomposition")

    def test_does_not_match_partial_signal(self):
        assert not disp._NOT_SUITABLE_RE.search("PLANNING COMPLETE: ready")
        assert not disp._NOT_SUITABLE_RE.search("just rambling")
        assert not disp._NOT_SUITABLE_RE.search("SUITABLE")
        assert not disp._NOT_SUITABLE_RE.search("DECOMPOSITION")

    def test_matches_embedded_in_summary(self):
        assert disp._NOT_SUITABLE_RE.search("Issue is NOT SUITABLE FOR DECOMPOSITION: too small")
        assert disp._NOT_SUITABLE_RE.search("Determined NOT SUITABLE: already fixed")


# ── Unit tests for _planner_not_suitable_validator_body ────────────────────────


class TestPlannerNotSuitableValidatorBody:
    """Test the validator body template directly."""

    def test_body_contains_issue_reference(self):
        issue = {
            "number": 42,
            "title": "Test issue",
            "body": "Issue body content",
            "labels": [],
        }
        body = disp._planner_not_suitable_validator_body(
            repo="test/repo",
            issue=issue,
            planner_summary="NOT SUITABLE FOR DECOMPOSITION: too small",
            workdir="/tmp/work",
            base_branch="dev",
            provider_name="github",
        )
        assert "#42" in body
        assert "Test issue" in body
        assert "Issue body content" in body

    def test_body_contains_planner_summary(self):
        issue = {"number": 42, "title": "Test", "body": "Body", "labels": []}
        body = disp._planner_not_suitable_validator_body(
            repo="test/repo",
            issue=issue,
            planner_summary="NOT SUITABLE FOR DECOMPOSITION: already fixed",
            workdir="/tmp/work",
            base_branch="main",
            provider_name="github",
        )
        assert "NOT SUITABLE FOR DECOMPOSITION: already fixed" in body

    def test_body_includes_validation_instructions(self):
        issue = {"number": 42, "title": "Test", "body": "Body", "labels": []}
        body = disp._planner_not_suitable_validator_body(
            repo="test/repo",
            issue=issue,
            planner_summary="NOT SUITABLE: reason",
            workdir="/tmp/work",
            base_branch="dev",
            provider_name="github",
        )
        assert "CONFIRMED" in body
        assert "CANNOT_REPRODUCE" in body
        assert "ALREADY_FIXED" in body
        assert "DUPLICATE" in body
        assert "NEEDS_MORE_INFO" in body

    def test_body_mentions_not_suitable_path(self):
        issue = {"number": 42, "title": "Test", "body": "Body", "labels": []}
        body = disp._planner_not_suitable_validator_body(
            repo="test/repo",
            issue=issue,
            planner_summary="NOT SUITABLE: reason",
            workdir="/tmp/work",
            base_branch="dev",
            provider_name="github",
        )
        assert "NOT suitable" in body or "not suitable" in body.lower()
        assert "standard (non-epic)" in body.lower() or "non-epic" in body.lower()

    def test_body_contains_read_only_injection(self):
        issue = {"number": 42, "title": "Test", "body": "Body", "labels": []}
        body = disp._planner_not_suitable_validator_body(
            repo="test/repo",
            issue=issue,
            planner_summary="NOT SUITABLE: reason",
            workdir="/tmp/work",
            base_branch="dev",
            provider_name="github",
        )
        assert "READ-ONLY" in body or "read only" in body.lower()


# ── Unit tests for _fetch_issue_with_retry in NOT SUITABLE context ─────────────


class TestFetchIssueWithRetryForNotSuitable:
    """Test the retry logic when fetching issues for the NOT SUITABLE handler."""

    def test_successful_fetch_on_first_attempt(self):
        provider = mock.MagicMock()
        provider.get_issue.return_value = mock.MagicMock()
        result = disp._fetch_issue_with_retry(provider, 42)
        assert result is not None
        assert provider.get_issue.call_count == 1

    def test_successful_fetch_on_first_retry(self):
        provider = mock.MagicMock()
        issue_obj = mock.MagicMock()
        provider.get_issue.side_effect = [None, issue_obj]
        with mock.patch("time.sleep"):  # Skip actual sleep
            result = disp._fetch_issue_with_retry(provider, 42)
        assert result is not None
        assert provider.get_issue.call_count == 2

    def test_successful_fetch_on_second_retry(self):
        provider = mock.MagicMock()
        issue_obj = mock.MagicMock()
        provider.get_issue.side_effect = [None, None, issue_obj]
        with mock.patch("time.sleep"):  # Skip actual sleep
            result = disp._fetch_issue_with_retry(provider, 42)
        assert result is not None
        assert provider.get_issue.call_count == 3

    def test_exhausted_retries_returns_none(self):
        provider = mock.MagicMock()
        provider.get_issue.return_value = None
        with mock.patch("time.sleep"):  # Skip actual sleep
            result = disp._fetch_issue_with_retry(provider, 42)
        assert result is None
        assert provider.get_issue.call_count == 3  # 1 initial + 2 retries


# ── Integration tests for end-to-end state machine transition ──────────────────


class TestNotSuitableStateMachineTransition:
    """Integration tests verifying the issue doesn't get stuck in 'In progress'."""

    def test_handler_creates_validator_when_issue_in_progress(self):
        """When an issue is 'In progress' and planner signals NOT SUITABLE,
        a validator task must be created to continue the pipeline."""
        fake_kb = FakeKanban()
        provider = FakeProvider()

        # Simulate an issue in "In progress" state
        issue_num = 42
        issue_dict = {
            "number": issue_num,
            "title": "Epic issue",
            "body": "Epic body",
            "labels": [],
        }

        # Seed a planner card with NOT SUITABLE signal
        planner_tid = fake_kb.seed(
            assignee="planner-daedalus",
            title=f"#{issue_num} Epic issue",
            status="done",
            summary="NOT SUITABLE FOR DECOMPOSITION: issue is already small",
            body=f"#{issue_num} Epic body",
        )

        issues_map = {issue_num: issue_dict}

        with (
            mock.patch.object(disp.kanban, "list_tasks", side_effect=fake_kb.list_tasks),
            mock.patch.object(disp.kanban, "show_card", side_effect=lambda slug, tid: fake_kb.tasks.get(tid)),
            mock.patch.object(disp.kanban, "create_task", side_effect=fake_kb.create_task),
        ):
            triggered = disp._check_planner_not_suitable(
                "test-slug",
                repo="test/repo",
                issues_map=issues_map,
                workdir="/tmp/test",
                base_branch="dev",
                provider_name="github",
                dry_run=False,
                provider=provider,
            )

        # Handler must have triggered for the issue
        assert triggered == [issue_num], "Handler must trigger for the issue"

        # Validator task must have been created
        validator_tasks = [t for t in fake_kb.tasks.values()
                          if "validator" in t.get("assignee", "")]
        assert len(validator_tasks) == 1, "Exactly one validator task must be created"
        assert f"#{issue_num}" in validator_tasks[0].get("title", "")

    def test_multiple_planner_cards_same_issue_single_validator(self):
        """When multiple planner cards for the same issue signal NOT SUITABLE,
        only one validator task is created (idempotency at kanban level)."""
        fake_kb = FakeKanban()
        provider = FakeProvider()

        issue_num = 42
        issue_dict = {
            "number": issue_num,
            "title": "Epic issue",
            "body": "Epic body",
            "labels": [],
        }

        # Seed two planner cards with NOT SUITABLE signal for same issue
        fake_kb.seed(
            assignee="planner-daedalus",
            title=f"#{issue_num} Epic issue",
            status="done",
            summary="NOT SUITABLE FOR DECOMPOSITION: first card",
            body=f"#{issue_num} Epic body",
            tid="t_planner_1",
        )
        fake_kb.seed(
            assignee="planner-daedalus",
            title=f"#{issue_num} Epic issue",
            status="done",
            summary="NOT SUITABLE FOR DECOMPOSITION: second card",
            body=f"#{issue_num} Epic body",
            tid="t_planner_2",
        )

        issues_map = {issue_num: issue_dict}

        with (
            mock.patch.object(disp.kanban, "list_tasks", side_effect=fake_kb.list_tasks),
            mock.patch.object(disp.kanban, "show_card", side_effect=lambda slug, tid: fake_kb.tasks.get(tid)),
            mock.patch.object(disp.kanban, "create_task", side_effect=fake_kb.create_task),
        ):
            triggered = disp._check_planner_not_suitable(
                "test-slug",
                repo="test/repo",
                issues_map=issues_map,
                workdir="/tmp/test",
                base_branch="dev",
                provider_name="github",
                dry_run=False,
                provider=provider,
            )

        # Handler triggers for each card that has the NOT SUITABLE signal
        # Both cards trigger, so we expect [42, 42] in the triggered list
        assert len(triggered) == 2, "Both cards should trigger"
        assert all(t == issue_num for t in triggered), "Both triggers should be for issue 42"

        # Only ONE validator task must have been created (idempotency at kanban level)
        validator_tasks = [t for t in fake_kb.tasks.values()
                          if "validator" in t.get("assignee", "")]
        assert len(validator_tasks) == 1, "Idempotency must prevent duplicate validator tasks"

    def test_issue_fetch_retry_on_missing_issue(self):
        """When the issue is not in issues_map, the handler must attempt to fetch
        it from the provider with retry logic."""
        from tests.conftest import FakeKanban, FakeProvider

        # Arrange
        fake_kb = FakeKanban()
        provider = FakeProvider()

        # Issue NOT in issues_map
        issue_num = 42
        issue_dict = {
            "number": issue_num,
            "title": "Fetched issue",
            "body": "Fetched body",
            "labels": [],
        }

        # Provider returns issue object (but NOT in issues_map)
        issue_obj = mock.Mock()
        issue_obj.as_dict.return_value = issue_dict
        provider._issues[issue_num] = issue_obj

        # Seed a planner card with NOT SUITABLE signal
        fake_kb.seed(
            assignee="planner-daedalus",
            title=f"#{issue_num} Fetched issue",
            status="done",
            summary="NOT SUITABLE FOR DECOMPOSITION: issue fetched from provider",
            body=f"#{issue_num} Fetched body",
        )

        issues_map = {}  # Empty - issue not in map

        with (
            mock.patch.object(disp.kanban, "list_tasks", side_effect=fake_kb.list_tasks),
            mock.patch.object(disp.kanban, "show_card", side_effect=lambda slug, tid: fake_kb.tasks.get(tid)),
            mock.patch.object(disp.kanban, "create_task", side_effect=fake_kb.create_task),
            mock.patch.object(disp, "_fetch_issue_with_retry", wraps=disp._fetch_issue_with_retry) as mock_retry,
        ):
            triggered = disp._check_planner_not_suitable(
                "test-slug",
                repo="test/repo",
                issues_map=issues_map,
                workdir="/tmp/test",
                base_branch="dev",
                provider_name="github",
                dry_run=False,
                provider=provider,
            )

        # Assert
        # Handler must have called _fetch_issue_with_retry since issue not in map
        assert mock_retry.called, "_fetch_issue_with_retry must be called when issue not in map"
        mock_retry.assert_called_with(provider, issue_num)

        # Handler must have triggered for the issue (after fetching)
        assert triggered == [issue_num], "Handler must trigger even when issue not in map"

        # Validator task must have been created
        validator_tasks = [t for t in fake_kb.tasks.values()
                          if "validator" in t.get("assignee", "")]
        assert len(validator_tasks) == 1, "Validator task must be created"
        assert f"#{issue_num}" in validator_tasks[0].get("title", "")

    def test_blocked_and_done_cards_same_issue_single_validator(self):
        """When both blocked and done planner cards for the same issue signal
        NOT SUITABLE, only one validator task is created."""
        fake_kb = FakeKanban()
        provider = FakeProvider()

        issue_num = 42
        issue_dict = {
            "number": issue_num,
            "title": "Epic issue",
            "body": "Epic body",
            "labels": [],
        }

        # Seed a done planner card
        fake_kb.seed(
            assignee="planner-daedalus",
            title=f"#{issue_num} Epic issue",
            status="done",
            summary="NOT SUITABLE FOR DECOMPOSITION: done card",
            body=f"#{issue_num} Epic body",
            tid="t_done",
        )

        # Seed a blocked planner card for same issue
        fake_kb.seed(
            assignee="planner-daedalus",
            title=f"#{issue_num} Epic issue",
            status="blocked",
            summary="NOT SUITABLE FOR DECOMPOSITION: blocked card",
            body=f"#{issue_num} Epic body",
            tid="t_blocked",
        )

        issues_map = {issue_num: issue_dict}

        with (
            mock.patch.object(disp.kanban, "list_tasks", side_effect=fake_kb.list_tasks),
            mock.patch.object(disp.kanban, "show_card", side_effect=lambda slug, tid: fake_kb.tasks.get(tid)),
            mock.patch.object(disp.kanban, "create_task", side_effect=fake_kb.create_task),
        ):
            triggered = disp._check_planner_not_suitable(
                "test-slug",
                repo="test/repo",
                issues_map=issues_map,
                workdir="/tmp/test",
                base_branch="dev",
                provider_name="github",
                dry_run=False,
                provider=provider,
            )

        # Handler processes both cards (done and blocked), so both trigger
        # The return list is [42, 42] because issue 42 is extracted from each task
        assert triggered == [issue_num, issue_num], "Handler must trigger for each card"

        # Only ONE validator task must have been created (idempotency at kanban level)
        validator_tasks = [t for t in fake_kb.tasks.values()
                          if "validator" in t.get("assignee", "")]
        assert len(validator_tasks) == 1, "Only one validator task must be created despite both done and blocked cards"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
