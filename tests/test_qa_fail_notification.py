"""Tests for QA failure notification (closes #1002).

Verifies that when a qa-daedalus card reports 'qa-failed', the dispatcher:
  1. Returns the card info in qa_failed_cards (4th return from run_iterate)
  2. Fires a notification to configured 'qa-failed' targets via _notify_qa_failed
  3. Does NOT fire notifications for developer DEV_FIX_CI (CI-red) cards
  4. Works correctly under dry_run (logs intent, no actual send)
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import core.iterate as iterate
from conftest import FakeKanban, FakeProvider


# ── helpers ───────────────────────────────────────────────────────────────────

def _qa_card(pr: int = 99, summary: str = "qa-failed: tests broke"):
    return {
        "id": "t_qa_001",
        "title": "QA: Issue #42",
        "assignee": "qa-daedalus",
        "status": "blocked",
        "latest_summary": summary,
        "body": "Issue #42\nPR #99",
    }


def _dev_card(pr: int = 77):
    return {
        "id": "t_dev_001",
        "title": "Fix CI: Issue #55",
        "assignee": "developer-daedalus",
        "status": "blocked",
        "latest_summary": f"review-required: PR #{pr}",
        "body": "Issue #55",
    }


# ── run_iterate returns qa_failed_cards for QA failures ──────────────────────


class TestRunIterateQaFailedCards:

    def test_qa_failed_summary_returned_in_fourth_slot(self):
        """qa-daedalus card with 'qa-failed' → qa_failed_cards contains it."""
        card = _qa_card()
        fk = FakeKanban()
        with (
            mock.patch("core.iterate.kanban", fk),
            mock.patch("core.iterate.kanban.list_blocked", return_value=[card]),
            mock.patch("core.iterate.kanban.show_card", return_value=card),
            mock.patch("core.iterate.kanban.create_task", return_value="t_fix"),
            mock.patch("core.iterate.kanban.comment", return_value=True),
        ):
            _, _, _, qa_failed = iterate.run_iterate("slug", "O/R")

        assert len(qa_failed) == 1
        assert qa_failed[0]["pr"] == 99 or qa_failed[0]["issue_n"] == 42

    def test_developer_dev_fix_ci_not_in_qa_failed(self):
        """Developer CI-red card triggers DEV_FIX_CI but is NOT in qa_failed_cards."""
        provider = FakeProvider()
        provider._ci = "red"
        provider._open_prs = {77}
        card = _dev_card(pr=77)
        fk = FakeKanban()
        with (
            mock.patch("core.iterate.kanban", fk),
            mock.patch("core.iterate.kanban.list_blocked", return_value=[card]),
            mock.patch("core.iterate.kanban.show_card", return_value=card),
            mock.patch("core.iterate.kanban.create_task", return_value="t_fix"),
            mock.patch("core.iterate.kanban.comment", return_value=True),
        ):
            counts, _, _, qa_failed = iterate.run_iterate("slug", "O/R", provider=provider)

        assert qa_failed == []

    def test_empty_board_returns_empty_qa_failed(self):
        """No blocked cards → all return lists are empty."""
        with mock.patch("core.iterate.kanban.list_blocked", return_value=[]):
            _, _, _, qa_failed = iterate.run_iterate("slug", "O/R")
        assert qa_failed == []

    def test_qa_passed_not_in_qa_failed(self):
        """QA card with 'qa-passed' advances normally, not in qa_failed_cards."""
        card = {
            "id": "t_qa_pass",
            "title": "QA: Issue #30",
            "assignee": "qa-daedalus",
            "status": "blocked",
            "latest_summary": "qa-passed: PR #50 all green",
            "body": "Issue #30",
        }
        fk = FakeKanban()
        with (
            mock.patch("core.iterate.kanban", fk),
            mock.patch("core.iterate.kanban.list_blocked", return_value=[card]),
            mock.patch("core.iterate.kanban.show_card", return_value=card),
            mock.patch("core.iterate.kanban.complete", return_value=True),
        ):
            counts, _, _, qa_failed = iterate.run_iterate("slug", "O/R")

        assert qa_failed == []
        assert counts[iterate.ADVANCE] == 1


# ── _notify_qa_failed sends to configured targets ────────────────────────────


class TestNotifyQaFailed:

    def _import_dispatch(self):
        """Import daedalus_dispatch, patching away heavy optional imports."""
        with mock.patch.dict("sys.modules", {
            "filelock": mock.MagicMock(),
        }):
            import importlib
            import scripts.daedalus_dispatch as disp
            return disp

    def test_no_targets_no_send(self):
        """When no targets subscribe to 'qa-failed', nothing is sent."""
        try:
            import scripts.daedalus_dispatch as disp
        except ImportError:
            pytest.skip("dispatch import requires filelock")

        resolved = {"cron": {"notifications": []}}
        with mock.patch.object(disp, "_hermes_send") as mock_send:
            disp._notify_qa_failed(
                issue_number=42, pr_number=99,
                reason="tests failed", resolved=resolved,
            )
        mock_send.assert_not_called()

    def test_sends_to_configured_target(self):
        """When a target subscribes to 'qa-failed', _hermes_send is called."""
        try:
            import scripts.daedalus_dispatch as disp
        except ImportError:
            pytest.skip("dispatch import requires filelock")

        resolved = {
            "cron": {
                "notifications": [
                    {"platform": "slack", "target": "slack:C123", "events": ["qa-failed"]},
                ]
            }
        }
        with mock.patch.object(disp, "_hermes_send", return_value=(True, None)) as mock_send:
            disp._notify_qa_failed(
                issue_number=42, pr_number=99,
                reason="tests failed", resolved=resolved,
            )

        mock_send.assert_called_once()
        call_args = mock_send.call_args[0]
        assert call_args[0] == "slack:C123"
        assert "#42" in call_args[1]

    def test_dry_run_no_send(self):
        """In dry_run mode, _hermes_send is NOT called."""
        try:
            import scripts.daedalus_dispatch as disp
        except ImportError:
            pytest.skip("dispatch import requires filelock")

        resolved = {
            "cron": {
                "notifications": [
                    {"platform": "slack", "target": "slack:C123", "events": ["qa-failed"]},
                ]
            }
        }
        with mock.patch.object(disp, "_hermes_send") as mock_send:
            disp._notify_qa_failed(
                issue_number=42, pr_number=99,
                reason="tests failed", resolved=resolved,
                dry_run=True,
            )

        mock_send.assert_not_called()

    def test_non_qa_failed_event_subscriber_not_notified(self):
        """Target subscribed to 'retry-attempt' does NOT receive qa-failed notifications."""
        try:
            import scripts.daedalus_dispatch as disp
        except ImportError:
            pytest.skip("dispatch import requires filelock")

        resolved = {
            "cron": {
                "notifications": [
                    {"platform": "slack", "target": "slack:C123", "events": ["retry-attempt"]},
                ]
            }
        }
        with mock.patch.object(disp, "_hermes_send") as mock_send:
            disp._notify_qa_failed(
                issue_number=42, pr_number=99,
                reason="tests failed", resolved=resolved,
            )

        mock_send.assert_not_called()

    def test_message_body_contains_issue_and_pr_refs(self):
        """Notification body mentions both issue and PR numbers."""
        try:
            import scripts.daedalus_dispatch as disp
        except ImportError:
            pytest.skip("dispatch import requires filelock")

        resolved = {
            "cron": {
                "notifications": [
                    {"platform": "slack", "target": "slack:C123", "events": ["qa-failed"]},
                ]
            }
        }
        with mock.patch.object(disp, "_hermes_send", return_value=(True, None)) as mock_send:
            disp._notify_qa_failed(
                issue_number=42, pr_number=99,
                reason="unit tests failed", resolved=resolved,
            )

        body = mock_send.call_args[0][1]
        assert "#42" in body
        assert "PR #99" in body
        assert "unit tests failed" in body

    def test_qa_failed_in_notify_events(self):
        """'qa-failed' is listed in NOTIFY_EVENTS."""
        try:
            import scripts.daedalus_dispatch as disp
        except ImportError:
            pytest.skip("dispatch import requires filelock")

        assert "qa-failed" in disp.NOTIFY_EVENTS
