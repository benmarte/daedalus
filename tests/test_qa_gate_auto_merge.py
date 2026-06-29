"""Tests for QA gate logic in auto-merge monitor (issue #998).

Verifies that the auto-merge monitor evaluates the 'qa-passed' signal before
proceeding. If QA has failed or the signal is missing, the monitor aborts the
auto-merge sequence.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from core.iterate import _qa_passed_for_issue


class TestQAGateAutoMerge(unittest.TestCase):
    """Test QA gate logic in auto-merge monitor."""

    @patch('core.iterate.kanban.list_tasks')
    @patch('core.iterate.kanban.show_card')
    def test_qa_passed_for_issue_positive(self, mock_show_card, mock_list_tasks):
        """Test that _qa_passed_for_issue returns True when QA has passed."""
        # Setup: QA card exists and has 'qa-passed' in summary
        mock_list_tasks.return_value = [
            {'id': 'qa-123', 'idempotency_key': 'qa-42'}
        ]
        mock_show_card.return_value = {
            'latest_summary': 'All tests executed. qa-passed signal confirmed.'
        }

        result = _qa_passed_for_issue('test-board', 42)

        self.assertTrue(result, "Should return True when QA has passed")
        mock_list_tasks.assert_called_once_with('test-board')
        mock_show_card.assert_called_once_with('test-board', 'qa-123')

    @patch('core.iterate.kanban.list_tasks')
    @patch('core.iterate.kanban.show_card')
    def test_qa_passed_for_issue_negative_failed(self, mock_show_card, mock_list_tasks):
        """Test that _qa_passed_for_issue returns False when QA has failed."""
        # Setup: QA card exists but has 'qa-failed' in summary
        mock_list_tasks.return_value = [
            {'id': 'qa-123', 'idempotency_key': 'qa-42'}
        ]
        mock_show_card.return_value = {
            'latest_summary': 'QA process completed. qa-failed: tests broken.'
        }

        result = _qa_passed_for_issue('test-board', 42)

        self.assertFalse(result, "Should return False when QA has failed")

    @patch('core.iterate.kanban.list_tasks')
    @patch('core.iterate.kanban.show_card')
    def test_qa_passed_for_issue_negative_missing(self, mock_show_card, mock_list_tasks):
        """Test that _qa_passed_for_issue returns False when QA signal is missing."""
        # Setup: QA card exists but summary has no qa-passed signal
        mock_list_tasks.return_value = [
            {'id': 'qa-123', 'idempotency_key': 'qa-42'}
        ]
        mock_show_card.return_value = {
            'latest_summary': 'QA process still running.'
        }

        result = _qa_passed_for_issue('test-board', 42)

        self.assertFalse(result, "Should return False when QA signal is missing")

    @patch('core.iterate.kanban.list_tasks')
    @patch('core.iterate.kanban.show_card')
    def test_qa_passed_for_issue_no_qa_card(self, mock_show_card, mock_list_tasks):
        """Test that _qa_passed_for_issue returns False when no QA card exists."""
        # Setup: No QA card on the board
        mock_list_tasks.return_value = []

        result = _qa_passed_for_issue('test-board', 42)

        self.assertFalse(result, "Should return False when no QA card exists")
        mock_show_card.assert_not_called()

    @patch('core.iterate.kanban.list_tasks')
    @patch('core.iterate.kanban.show_card')
    def test_qa_passed_for_issue_wrong_issue(self, mock_show_card, mock_list_tasks):
        """Test that _qa_passed_for_issue returns False when QA card is for different issue."""
        # Setup: QA card exists but for a different issue
        mock_list_tasks.return_value = [
            {'id': 'qa-123', 'idempotency_key': 'qa-99'}
        ]

        result = _qa_passed_for_issue('test-board', 42)

        self.assertFalse(result, "Should return False when QA card is for different issue")
        mock_show_card.assert_not_called()

    @patch('core.iterate.kanban.list_tasks')
    @patch('core.iterate.kanban.show_card')
    def test_qa_passed_for_issue_card_details_unavailable(self, mock_show_card, mock_list_tasks):
        """Test that _qa_passed_for_issue returns False when card details cannot be fetched."""
        # Setup: QA card exists but show_card returns None
        mock_list_tasks.return_value = [
            {'id': 'qa-123', 'idempotency_key': 'qa-42'}
        ]
        mock_show_card.return_value = None

        result = _qa_passed_for_issue('test-board', 42)

        self.assertFalse(result, "Should return False when card details are unavailable")

    @patch('core.iterate.kanban.list_tasks')
    def test_qa_passed_for_issue_no_issue_number(self, mock_list_tasks):
        """Test that _qa_passed_for_issue returns False when issue number is None."""
        result = _qa_passed_for_issue('test-board', None)

        self.assertFalse(result, "Should return False when issue number is None")
        mock_list_tasks.assert_not_called()

    @patch('core.iterate.kanban.list_tasks')
    @patch('core.iterate.kanban.show_card')
    def test_qa_passed_for_issue_empty_summary(self, mock_show_card, mock_list_tasks):
        """Test that _qa_passed_for_issue returns False when summary is empty."""
        # Setup: QA card exists but summary is empty
        mock_list_tasks.return_value = [
            {'id': 'qa-123', 'idempotency_key': 'qa-42'}
        ]
        mock_show_card.return_value = {'latest_summary': ''}

        result = _qa_passed_for_issue('test-board', 42)

        self.assertFalse(result, "Should return False when summary is empty")

    @patch('core.iterate.kanban.list_tasks')
    @patch('core.iterate.kanban.show_card')
    def test_qa_passed_for_issue_case_insensitive(self, mock_show_card, mock_list_tasks):
        """Test that _qa_passed_for_issue is case-insensitive."""
        # Setup: QA card has 'QA-PASSED' in uppercase
        mock_list_tasks.return_value = [
            {'id': 'qa-123', 'idempotency_key': 'qa-42'}
        ]
        mock_show_card.return_value = {
            'latest_summary': 'QA-PASSED: All checks successful.'
        }

        result = _qa_passed_for_issue('test-board', 42)

        self.assertTrue(result, "Should be case-insensitive")


if __name__ == '__main__':
    unittest.main()
