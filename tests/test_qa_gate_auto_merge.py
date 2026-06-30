"""Tests for QA pass gate before auto-merge (fix/qa-passed-signal).

Verifies that the iterate loop blocks auto-merge of a PR when the QA card
for that issue has not produced a 'qa-passed' signal.
"""
import pytest
from unittest.mock import patch, MagicMock

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))


class TestQAPassedForIssue:
    """Test the _qa_passed_for_issue helper function."""

    @patch('core.iterate.kanban.show_card')
    @patch('core.iterate.kanban.list_tasks')
    def test_qa_passed_for_issue_positive(self, mock_list_tasks, mock_show_card):
        """_qa_passed_for_issue returns True when QA card summary contains 'qa-passed'."""
        from core.iterate import _qa_passed_for_issue

        # QA card exists with qa-passed signal
        mock_list_tasks.return_value = [
            {
                'id': 'qa-card-1',
                'title': 'QA: Issue #42',
                'assignee': 'qa-daedalus',
                'status': 'blocked',
                'reason': 'qa-passed: PR #42',
                'latest_summary': 'qa-passed: PR #42',
                'body': '',
                'idempotency_key': 'qa-42'
            }
        ]

        mock_show_card.return_value = {
            'id': 'qa-card-1',
            'latest_summary': 'qa-passed: PR #42'
        }

        result = _qa_passed_for_issue('test-board', 42)
        assert result is True, "Should return True when QA card contains 'qa-passed'"
        mock_list_tasks.assert_called_once_with('test-board')
        mock_show_card.assert_called_once_with('test-board', 'qa-card-1')

    @patch('core.iterate.kanban.show_card')
    @patch('core.iterate.kanban.list_tasks')
    def test_qa_passed_for_issue_negative_failed(self, mock_list_tasks, mock_show_card):
        """_qa_passed_for_issue returns False when QA card summary contains 'qa-failed'."""
        from core.iterate import _qa_passed_for_issue

        # QA card exists with qa-failed signal
        mock_list_tasks.return_value = [
            {
                'id': 'qa-card-1',
                'title': 'QA: Issue #42',
                'assignee': 'qa-daedalus',
                'status': 'blocked',
                'reason': 'qa-failed: tests broken',
                'latest_summary': 'qa-failed: tests broken',
                'body': '',
                'idempotency_key': 'qa-42'
            }
        ]

        mock_show_card.return_value = {
            'id': 'qa-card-1',
            'latest_summary': 'qa-failed: tests broken'
        }

        result = _qa_passed_for_issue('test-board', 42)
        assert result is False, "Should return False when QA card contains 'qa-failed'"

    @patch('core.iterate.kanban.list_tasks')
    def test_qa_passed_for_issue_no_qa_card(self, mock_list_tasks):
        """_qa_passed_for_issue returns False when no QA card exists for issue."""
        from core.iterate import _qa_passed_for_issue

        # No QA card for this issue
        mock_list_tasks.return_value = []

        result = _qa_passed_for_issue('test-board', 42)
        assert result is False, "Should return False when no QA card exists"

    @patch('core.iterate.kanban.show_card')
    @patch('core.iterate.kanban.list_tasks')
    def test_qa_passed_for_issue_no_summary(self, mock_list_tasks, mock_show_card):
        """_qa_passed_for_issue returns False when QA card has no summary."""
        from core.iterate import _qa_passed_for_issue

        # QA card exists but no summary
        mock_list_tasks.return_value = [
            {
                'id': 'qa-card-1',
                'title': 'QA: Issue #42',
                'assignee': 'qa-daedalus',
                'status': 'blocked',
                'reason': '',
                'latest_summary': '',
                'body': '',
                'idempotency_key': 'qa-42'
            }
        ]

        mock_show_card.return_value = {
            'id': 'qa-card-1',
            'latest_summary': ''
        }

        result = _qa_passed_for_issue('test-board', 42)
        assert result is False, "Should return False when QA card has no summary"

    @patch('core.iterate.kanban.show_card')
    @patch('core.iterate.kanban.list_tasks')
    def test_qa_passed_for_issue_case_insensitive(self, mock_list_tasks, mock_show_card):
        """_qa_passed_for_issue is case-insensitive."""
        from core.iterate import _qa_passed_for_issue

        # QA card with uppercase QA-PASSED
        mock_list_tasks.return_value = [
            {
                'id': 'qa-card-1',
                'title': 'QA: Issue #42',
                'assignee': 'qa-daedalus',
                'status': 'blocked',
                'reason': 'QA-PASSED: PR #42',
                'latest_summary': 'QA-PASSED: PR #42',
                'body': '',
                'idempotency_key': 'qa-42'
            }
        ]

        mock_show_card.return_value = {
            'id': 'qa-card-1',
            'latest_summary': 'QA-PASSED: PR #42'
        }

        result = _qa_passed_for_issue('test-board', 42)
        assert result is True, "Should be case-insensitive"

    def test_qa_passed_for_issue_none_issue_number(self):
        """_qa_passed_for_issue returns False when issue_number is None."""
        from core.iterate import _qa_passed_for_issue

        result = _qa_passed_for_issue('test-board', None)
        assert result is False, "Should return False when issue_number is None"

    @patch('core.iterate.kanban.show_card')
    @patch('core.iterate.kanban.list_tasks')
    def test_qa_passed_for_issue_wrong_issue(self, mock_list_tasks, mock_show_card):
        """_qa_passed_for_issue returns False when QA card is for different issue."""
        from core.iterate import _qa_passed_for_issue

        # QA card exists but for issue #99 instead of #42
        mock_list_tasks.return_value = [
            {
                'id': 'qa-card-1',
                'title': 'QA: Issue #99',
                'assignee': 'qa-daedalus',
                'status': 'blocked',
                'reason': 'qa-passed: PR #99',
                'latest_summary': 'qa-passed: PR #99',
                'body': '',
                'idempotency_key': 'qa-99'
            }
        ]

        result = _qa_passed_for_issue('test-board', 42)
        assert result is False, "Should return False when QA card is for different issue"
        # show_card should not be called since we didn't find the right QA card
        mock_show_card.assert_not_called()

    @patch('core.iterate.kanban.show_card')
    @patch('core.iterate.kanban.list_tasks')
    def test_qa_passed_for_issue_card_details_none(self, mock_list_tasks, mock_show_card):
        """_qa_passed_for_issue returns False when show_card returns None."""
        from core.iterate import _qa_passed_for_issue

        mock_list_tasks.return_value = [
            {
                'id': 'qa-card-1',
                'title': 'QA: Issue #42',
                'assignee': 'qa-daedalus',
                'status': 'blocked',
                'reason': 'qa-passed: PR #42',
                'latest_summary': 'qa-passed: PR #42',
                'body': '',
                'idempotency_key': 'qa-42'
            }
        ]

        # show_card returns None
        mock_show_card.return_value = None

        result = _qa_passed_for_issue('test-board', 42)
        assert result is False, "Should return False when show_card returns None"


class TestAutoMergeQAGateIntegration:
    """Test that auto-merge gates on QA pass signal in run_iterate."""

    @patch('core.iterate._qa_passed_for_issue')
    @patch('core.iterate.kanban.list_blocked')
    @patch('core.iterate.kanban.show_card')
    @patch('core.iterate.kanban.complete')
    def test_auto_merge_blocked_when_qa_not_passed(
        self, mock_complete, mock_show_card, mock_list_blocked, mock_qa_passed
    ):
        """Auto-merge should NOT proceed when QA has not passed."""
        from core.iterate import run_iterate
        from tests.conftest import FakeProvider

        # Setup: docs card for PR #42
        docs_card = {
            'id': 'docs-card-1',
            'title': 'Documentation: Issue #42',
            'assignee': 'documentation-daedalus',
            'status': 'blocked',
            'reason': 'review-required: PR #42',
            'latest_summary': 'review-required: PR #42',
            'body': 'Issue #42\nPR #42',
            'idempotency_key': 'docs-42'
        }
        
        mock_list_blocked.return_value = [docs_card]
        mock_show_card.return_value = docs_card
        mock_complete.return_value = True

        # QA has NOT passed
        mock_qa_passed.return_value = False

        provider = FakeProvider()
        provider._ci = 'green'

        result = run_iterate(
            'test-board',
            'test/repo',
            resolved={'execution': {'auto_merge': True}},
            provider=provider
        )

        # Should NOT have called merge_pr
        assert len(provider.merged) == 0, "Auto-merge should not be called when QA has not passed"

    @patch('core.iterate._qa_passed_for_issue')
    @patch('core.iterate.kanban.list_blocked')
    @patch('core.iterate.kanban.show_card')
    @patch('core.iterate.kanban.complete')
    def test_auto_merge_allowed_when_qa_passed(
        self, mock_complete, mock_show_card, mock_list_blocked, mock_qa_passed
    ):
        """Auto-merge SHOULD proceed when QA has passed."""
        from core.iterate import run_iterate
        from tests.conftest import FakeProvider

        # Setup: docs card for PR #42
        docs_card = {
            'id': 'docs-card-1',
            'title': 'Documentation: Issue #42',
            'assignee': 'documentation-daedalus',
            'status': 'blocked',
            'reason': 'docs posted: PR #42',
            'latest_summary': 'docs posted: PR #42',
            'body': 'Issue #42\nPR #42',
            'idempotency_key': 'docs-42'
        }

        mock_list_blocked.return_value = [docs_card]
        mock_show_card.return_value = docs_card
        mock_complete.return_value = True

        # QA HAS passed
        mock_qa_passed.return_value = True

        provider = FakeProvider()
        provider._ci = 'green'
        provider._open_prs = {42}

        result = run_iterate(
            'test-board',
            'test/repo',
            resolved={'execution': {'auto_merge': True}},
            provider=provider
        )

        # Should have called merge_pr
        assert len(provider.merged) > 0, "Auto-merge should be called when QA has passed"

    @patch('core.iterate._qa_passed_for_issue')
    @patch('core.iterate.kanban.list_blocked')
    @patch('core.iterate.kanban.show_card')
    @patch('core.iterate.kanban.complete')
    def test_auto_merge_disabled_does_not_merge_even_when_qa_passed(
        self, mock_complete, mock_show_card, mock_list_blocked, mock_qa_passed
    ):
        """When auto_merge=False, PR must NOT be merged even if QA has passed."""
        from core.iterate import run_iterate
        from tests.conftest import FakeProvider

        docs_card = {
            'id': 'docs-card-1',
            'title': 'Documentation: Issue #42',
            'assignee': 'documentation-daedalus',
            'status': 'blocked',
            'latest_summary': 'docs posted: PR #42',
            'body': 'Issue #42\nPR #42',
        }

        mock_list_blocked.return_value = [docs_card]
        mock_show_card.return_value = docs_card
        mock_complete.return_value = True
        mock_qa_passed.return_value = True  # QA passed — but auto_merge is off

        provider = FakeProvider()
        provider._ci = 'green'
        provider._open_prs = {42}

        run_iterate(
            'test-board',
            'test/repo',
            resolved={'execution': {'auto_merge': False}},
            provider=provider,
        )

        assert len(provider.merged) == 0, (
            "merge_pr must NOT be called when auto_merge=False, even if QA passed"
        )

    @patch('core.iterate._qa_passed_for_issue')
    @patch('core.iterate.kanban.list_blocked')
    @patch('core.iterate.kanban.show_card')
    @patch('core.iterate.kanban.complete')
    def test_auto_merge_absent_defaults_to_disabled(
        self, mock_complete, mock_show_card, mock_list_blocked, mock_qa_passed
    ):
        """When auto_merge key is absent from config, PR must NOT be merged."""
        from core.iterate import run_iterate
        from tests.conftest import FakeProvider

        docs_card = {
            'id': 'docs-card-2',
            'title': 'Documentation: Issue #7',
            'assignee': 'documentation-daedalus',
            'status': 'blocked',
            'latest_summary': 'docs posted: PR #7',
            'body': 'Issue #7\nPR #7',
        }

        mock_list_blocked.return_value = [docs_card]
        mock_show_card.return_value = docs_card
        mock_complete.return_value = True
        mock_qa_passed.return_value = True

        provider = FakeProvider()
        provider._ci = 'green'
        provider._open_prs = {7}

        # No auto_merge key at all — should default to False
        run_iterate('test-board', 'test/repo', resolved={}, provider=provider)

        assert len(provider.merged) == 0, (
            "merge_pr must NOT be called when auto_merge key is absent (defaults to disabled)"
        )
