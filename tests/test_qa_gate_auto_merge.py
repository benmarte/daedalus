"""Tests for QA pass gate before auto-merge (fix/qa-passed-signal).

Verifies that the iterate loop blocks auto-merge of a PR when the QA card
for that issue has not produced a 'qa-passed' signal.
"""
import pytest
from unittest.mock import patch, MagicMock

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.conftest import kanban_as  # noqa: E402


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
                'title': '#42 QA: Issue',
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
                'title': '#42 QA: Issue',
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
                'title': '#42 QA: Issue',
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
                'title': '#42 QA: Issue',
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
                'title': '#99 QA: Issue',
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
                'title': '#42 QA: Issue',
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
            'title': '#42 Docs: Issue',
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

    @patch('core.iterate._security_passed_for_issue')
    @patch('core.iterate._reviewer_passed_for_issue')
    @patch('core.iterate._qa_passed_for_issue')
    @patch('core.iterate.kanban.list_blocked')
    @patch('core.iterate.kanban.show_card')
    @patch('core.iterate.kanban.complete')
    def test_auto_merge_allowed_when_qa_passed(
        self, mock_complete, mock_show_card, mock_list_blocked, mock_qa_passed,
        mock_reviewer_passed, mock_security_passed
    ):
        """Auto-merge SHOULD proceed when QA has passed."""
        from core.iterate import run_iterate
        from tests.conftest import FakeProvider

        # Setup: docs card for PR #42
        docs_card = {
            'id': 'docs-card-1',
            'title': '#42 Docs: Issue',
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

        # QA, reviewer, and security HAVE passed
        mock_qa_passed.return_value = True
        mock_reviewer_passed.return_value = True
        mock_security_passed.return_value = True

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
            'title': '#42 Docs: Issue',
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
            'title': '#7 Docs: Issue',
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

    @patch('core.iterate._qa_passed_for_issue')
    @patch('core.iterate.kanban.list_blocked')
    @patch('core.iterate.kanban.show_card')
    @patch('core.iterate.kanban.complete')
    def test_auto_merge_blocked_when_ci_not_green(
        self, mock_complete, mock_show_card, mock_list_blocked, mock_qa_passed
    ):
        """Auto-merge must NOT proceed when CI is not green (per epic #1074).

        CI gating moved from ADVANCE-time to merge-time. Even if QA has passed
        and auto_merge is enabled, the merge must be blocked when CI is red
        or pending.
        """
        from core.iterate import run_iterate
        from tests.conftest import FakeProvider

        docs_card = {
            'id': 'docs-card-ci-red',
            'title': '#55 Docs: Issue',
            'assignee': 'documentation-daedalus',
            'status': 'blocked',
            'latest_summary': 'docs posted: PR #55',
            'body': 'Issue #55\nPR #55',
        }

        mock_list_blocked.return_value = [docs_card]
        mock_show_card.return_value = docs_card
        mock_complete.return_value = True
        mock_qa_passed.return_value = True  # QA passed

        provider = FakeProvider()
        provider._ci = 'red'  # CI is red
        provider._open_prs = {55}

        run_iterate(
            'test-board',
            'test/repo',
            resolved={'execution': {'auto_merge': True}},
            provider=provider,
        )

        # Should NOT have called merge_pr — CI is red
        assert len(provider.merged) == 0, (
            "Auto-merge should NOT be called when CI is red (CI gated at merge-time per epic #1074)"
        )

    @patch('core.iterate._security_passed_for_issue')
    @patch('core.iterate._reviewer_passed_for_issue')
    @patch('core.iterate._qa_passed_for_issue')
    @patch('core.iterate.kanban.list_blocked')
    @patch('core.iterate.kanban.show_card')
    @patch('core.iterate.kanban.complete')
    def test_auto_merge_allowed_when_ci_green(
        self, mock_complete, mock_show_card, mock_list_blocked, mock_qa_passed,
        mock_reviewer_passed, mock_security_passed
    ):
        """Auto-merge SHOULD proceed when CI is green and QA has passed (no regression)."""
        from core.iterate import run_iterate
        from tests.conftest import FakeProvider

        docs_card = {
            'id': 'docs-card-ci-green',
            'title': '#56 Docs: Issue',
            'assignee': 'documentation-daedalus',
            'status': 'blocked',
            'latest_summary': 'docs posted: PR #56',
            'body': 'Issue #56\nPR #56',
        }

        mock_list_blocked.return_value = [docs_card]
        mock_show_card.return_value = docs_card
        mock_complete.return_value = True
        mock_qa_passed.return_value = True  # QA passed
        mock_reviewer_passed.return_value = True
        mock_security_passed.return_value = True

        provider = FakeProvider()
        provider._ci = 'green'  # CI is green
        provider._open_prs = {56}

        run_iterate(
            'test-board',
            'test/repo',
            resolved={'execution': {'auto_merge': True}},
            provider=provider,
        )

        # Should have called merge_pr — CI is green and QA passed
        assert len(provider.merged) > 0, (
            "Auto-merge should be called when CI is green and QA has passed"
        )

    @patch('core.iterate._security_passed_for_issue')
    @patch('core.iterate._reviewer_passed_for_issue')
    @patch('core.iterate._qa_passed_for_issue')
    @patch('core.iterate.kanban.list_blocked')
    @patch('core.iterate.kanban.show_card')
    @patch('core.iterate.kanban.complete')
    def test_auto_merge_deferred_then_merges_when_ci_passes(
        self, mock_complete, mock_show_card, mock_list_blocked, mock_qa_passed,
        mock_reviewer_passed, mock_security_passed
    ):
        """CI eventually passes after docs completes → next cron tick triggers merge (#1085).

        Per epic #1074: CI is checked at merge-time only. When the docs card
        completes but CI is still pending, the merge is deferred (continue).
        On the next cron tick, when CI has turned green, the merge proceeds.
        """
        from core.iterate import run_iterate
        from tests.conftest import FakeProvider

        docs_card = {
            'id': 'docs-card-ci-deferred',
            'title': '#77 Docs: Issue',
            'assignee': 'documentation-daedalus',
            'status': 'blocked',
            'latest_summary': 'docs posted: PR #77',
            'body': 'Issue #77\nPR #77',
        }

        mock_list_blocked.return_value = [docs_card]
        mock_show_card.return_value = docs_card
        mock_complete.return_value = True
        mock_qa_passed.return_value = True
        mock_reviewer_passed.return_value = True
        mock_security_passed.return_value = True

        # Tick 1: CI is pending — merge should be deferred
        provider_pending = FakeProvider()
        provider_pending._ci = 'pending'
        provider_pending._open_prs = {77}

        run_iterate(
            'test-board',
            'test/repo',
            resolved={'execution': {'auto_merge': True}},
            provider=provider_pending,
        )
        assert len(provider_pending.merged) == 0, (
            "Auto-merge should NOT be called when CI is pending (deferred to next tick)"
        )

        # Tick 2: CI is now green — merge should proceed
        provider_green = FakeProvider()
        provider_green._ci = 'green'
        provider_green._open_prs = {77}

        run_iterate(
            'test-board',
            'test/repo',
            resolved={'execution': {'auto_merge': True}},
            provider=provider_green,
        )
        assert len(provider_green.merged) > 0, (
            "Auto-merge should be called on the next cron tick once CI turns green"
        )

    @patch('core.iterate._qa_passed_for_issue')
    @patch('core.iterate.kanban.list_blocked')
    @patch('core.iterate.kanban.show_card')
    @patch('core.iterate.kanban.complete')
    def test_auto_merge_blocked_when_ci_pending(
        self, mock_complete, mock_show_card, mock_list_blocked, mock_qa_passed
    ):
        """Auto-merge must NOT proceed when CI is pending (distinct from red).

        Per epic #1074 / issue #1085: CI pending means the merge is deferred
        (not failed). The PR is left open and the next cron tick will retry
        when CI resolves.
        """
        from core.iterate import run_iterate
        from tests.conftest import FakeProvider

        docs_card = {
            'id': 'docs-card-ci-pending',
            'title': '#88 Docs: Issue',
            'assignee': 'documentation-daedalus',
            'status': 'blocked',
            'latest_summary': 'docs posted: PR #88',
            'body': 'Issue #88\nPR #88',
        }

        mock_list_blocked.return_value = [docs_card]
        mock_show_card.return_value = docs_card
        mock_complete.return_value = True
        mock_qa_passed.return_value = True

        provider = FakeProvider()
        provider._ci = 'pending'
        provider._open_prs = {88}

        run_iterate(
            'test-board',
            'test/repo',
            resolved={'execution': {'auto_merge': True}},
            provider=provider,
        )

        assert len(provider.merged) == 0, (
            "Auto-merge should NOT be called when CI is pending (deferred to next tick)"
        )


class TestAutoMergeCIGateIntegration:
    """End-to-end integration tests for CI gate at merge-time (issue #1085).

    Uses the FakeKanban in-memory board to verify the full flow without
    mocking the kanban module — the card state transitions are real.
    """

    def test_ci_gate_end_to_end_pending_then_green(self):
        """Full pipeline: docs card with CI pending defers merge, then merges when CI passes.

        Tick 1: docs card is blocked, CI is pending → card stays blocked (no merge).
        Tick 2: same docs card still blocked, CI now green → card completes and merge fires.
        """
        from core import iterate
        from tests.conftest import FakeKanban, FakeProvider

        fk = FakeKanban()
        issue_n = 9100
        pr_n = 9100

        docs_tid = fk.seed(
            assignee="documentation-daedalus",
            title=f"#{issue_n} Docs: Issue",
            status="blocked",
            reason=f"docs posted: PR #{pr_n}",
            body=f"Issue #{issue_n}\nPR #{pr_n}",
        )

        fk.seed(
            assignee="qa-daedalus",
            title=f"#{issue_n} QA: Issue",
            status="blocked",
            reason=f"qa-passed: PR #{pr_n}",
            summary=f"qa-passed: PR #{pr_n}",
            body=f"Issue #{issue_n}\nPR #{pr_n}",
            idempotency_key=f"qa-{issue_n}",
        )

        fk.seed(
            assignee="reviewer-daedalus",
            title=f"#{issue_n} Reviewer: Issue",
            status="blocked",
            reason=f"review-approved: PR #{pr_n}",
            summary=f"review-approved: PR #{pr_n}",
            body=f"Issue #{issue_n}\nPR #{pr_n}",
            idempotency_key=f"reviewer-{issue_n}",
        )

        fk.seed(
            assignee="security-analyst-daedalus",
            title=f"#{issue_n} Security: Issue",
            status="blocked",
            reason=f"security-approved: PR #{pr_n}",
            summary=f"security-approved: PR #{pr_n}",
            body=f"Issue #{issue_n}\nPR #{pr_n}",
            idempotency_key=f"security-{issue_n}",
        )

        resolved = {
            'execution': {'auto_merge': True, 'merge_method': 'squash'},
        }

        with kanban_as(iterate.kanban, fk):
            # Tick 1: CI is pending — card should stay blocked, no merge
            provider_pending = FakeProvider(ci_status="pending", open_prs={pr_n})
            iterate.run_iterate(
                'test-board', 'test/repo',
                resolved=resolved, provider=provider_pending,
            )
            assert len(provider_pending.merged) == 0, (
                "Tick 1: auto-merge should NOT fire when CI is pending"
            )
            assert fk.tasks[docs_tid].get("status") == "blocked", (
                "Tick 1: docs card should stay blocked when CI is pending (not completed)"
            )

            # Tick 2: CI is now green — card completes and merge fires
            provider_green = FakeProvider(ci_status="green", open_prs={pr_n})
            iterate.run_iterate(
                'test-board', 'test/repo',
                resolved=resolved, provider=provider_green,
            )
            assert len(provider_green.merged) > 0, (
                "Tick 2: auto-merge should fire once CI turns green"
            )
            assert provider_green.merged[0][0] == pr_n, (
                f"Tick 2: should merge PR #{pr_n}, got #{provider_green.merged[0][0]}"
            )

    def test_ci_gate_end_to_end_red_ci_blocks_merge(self):
        """Full pipeline: docs card with CI red → card stays blocked, no merge."""
        from core import iterate
        from tests.conftest import FakeKanban, FakeProvider

        fk = FakeKanban()
        issue_n = 9101
        pr_n = 9101

        docs_tid = fk.seed(
            assignee="documentation-daedalus",
            title=f"#{issue_n} Docs: Issue",
            status="blocked",
            reason=f"docs posted: PR #{pr_n}",
            body=f"Issue #{issue_n}\nPR #{pr_n}",
        )

        fk.seed(
            assignee="qa-daedalus",
            title=f"#{issue_n} QA: Issue",
            status="blocked",
            reason=f"qa-passed: PR #{pr_n}",
            summary=f"qa-passed: PR #{pr_n}",
            body=f"Issue #{issue_n}\nPR #{pr_n}",
            idempotency_key=f"qa-{issue_n}",
        )

        resolved = {
            'execution': {'auto_merge': True, 'merge_method': 'squash'},
        }

        provider_red = FakeProvider(ci_status="red", open_prs={pr_n})
        with kanban_as(iterate.kanban, fk):
            iterate.run_iterate(
                'test-board', 'test/repo',
                resolved=resolved, provider=provider_red,
            )
            assert len(provider_red.merged) == 0, (
                "Auto-merge should NOT fire when CI is red"
            )
            assert fk.tasks[docs_tid].get("status") == "blocked", (
                "Docs card should stay blocked when CI is red (not completed)"
            )

    def test_ci_gate_end_to_end_green_ci_merges(self):
        """Full pipeline: docs card with CI green → card completes and merge fires."""
        from core import iterate
        from tests.conftest import FakeKanban, FakeProvider

        fk = FakeKanban()
        issue_n = 9102
        pr_n = 9102

        fk.seed(
            assignee="documentation-daedalus",
            title=f"#{issue_n} Docs: Issue",
            status="blocked",
            reason=f"docs posted: PR #{pr_n}",
            body=f"Issue #{issue_n}\nPR #{pr_n}",
        )

        fk.seed(
            assignee="qa-daedalus",
            title=f"#{issue_n} QA: Issue",
            status="blocked",
            reason=f"qa-passed: PR #{pr_n}",
            summary=f"qa-passed: PR #{pr_n}",
            body=f"Issue #{issue_n}\nPR #{pr_n}",
            idempotency_key=f"qa-{issue_n}",
        )

        fk.seed(
            assignee="reviewer-daedalus",
            title=f"#{issue_n} Reviewer: Issue",
            status="blocked",
            reason=f"review-approved: PR #{pr_n}",
            summary=f"review-approved: PR #{pr_n}",
            body=f"Issue #{issue_n}\nPR #{pr_n}",
            idempotency_key=f"reviewer-{issue_n}",
        )

        fk.seed(
            assignee="security-analyst-daedalus",
            title=f"#{issue_n} Security: Issue",
            status="blocked",
            reason=f"security-approved: PR #{pr_n}",
            summary=f"security-approved: PR #{pr_n}",
            body=f"Issue #{issue_n}\nPR #{pr_n}",
            idempotency_key=f"security-{issue_n}",
        )

        resolved = {
            'execution': {'auto_merge': True, 'merge_method': 'squash'},
        }

        provider_green = FakeProvider(ci_status="green", open_prs={pr_n})
        with kanban_as(iterate.kanban, fk):
            iterate.run_iterate(
                'test-board', 'test/repo',
                resolved=resolved, provider=provider_green,
            )
            assert len(provider_green.merged) > 0, (
                "Auto-merge should fire when CI is green and QA has passed"
            )
            assert provider_green.merged[0][0] == pr_n, (
                f"Should merge PR #{pr_n}, got #{provider_green.merged[0][0]}"
            )

    def test_ci_gate_no_ci_support_treated_as_green(self):
        """Provider without CI status support → UNKNOWN treated as green (no block).

        Repos without CI should not be permanently blocked by the merge gate.
        """
        from core import iterate
        from tests.conftest import FakeKanban, FakeProvider

        fk = FakeKanban()
        issue_n = 9103
        pr_n = 9103

        fk.seed(
            assignee="documentation-daedalus",
            title=f"#{issue_n} Docs: Issue",
            status="blocked",
            reason=f"docs posted: PR #{pr_n}",
            body=f"Issue #{issue_n}\nPR #{pr_n}",
        )

        fk.seed(
            assignee="qa-daedalus",
            title=f"#{issue_n} QA: Issue",
            status="blocked",
            reason=f"qa-passed: PR #{pr_n}",
            summary=f"qa-passed: PR #{pr_n}",
            body=f"Issue #{issue_n}\nPR #{pr_n}",
            idempotency_key=f"qa-{issue_n}",
        )

        fk.seed(
            assignee="reviewer-daedalus",
            title=f"#{issue_n} Reviewer: Issue",
            status="blocked",
            reason=f"review-approved: PR #{pr_n}",
            summary=f"review-approved: PR #{pr_n}",
            body=f"Issue #{issue_n}\nPR #{pr_n}",
            idempotency_key=f"reviewer-{issue_n}",
        )

        fk.seed(
            assignee="security-analyst-daedalus",
            title=f"#{issue_n} Security: Issue",
            status="blocked",
            reason=f"security-approved: PR #{pr_n}",
            summary=f"security-approved: PR #{pr_n}",
            body=f"Issue #{issue_n}\nPR #{pr_n}",
            idempotency_key=f"security-{issue_n}",
        )

        resolved = {
            'execution': {'auto_merge': True, 'merge_method': 'squash'},
        }

        provider = FakeProvider(
            ci_status="unknown",
            open_prs={pr_n},
            supports_ci_status=False,
        )
        with kanban_as(iterate.kanban, fk):
            iterate.run_iterate(
                'test-board', 'test/repo',
                resolved=resolved, provider=provider,
            )
            assert len(provider.merged) > 0, (
                "Auto-merge should fire when provider has no CI support (UNKNOWN → green)"
            )


class TestCronTickMergeTrigger:
    """Tests for the cron-tick merge trigger when CI passes after docs completion.

    Issue #1085 / epic #1074: When the docs card completes but CI is not yet
    green, the merge is deferred. On subsequent cron ticks, when CI has passed
    and all gates (QA, reviewer, security) are satisfied, the merge fires.

    These tests cover the remaining acceptance criteria not already covered by
    TestAutoMergeCIGateIntegration:
    - Already-merged PR is skipped (idempotency)
    - Reviewer and security gates are checked before merge
    - CI-passed-before-docs does not use the deferred merge path
    - Idempotency: re-running cron does not double-merge
    - Audit logging for the deferred-to-merged transition
    """

    def test_already_merged_pr_skipped(self):
        """Idempotency: if the PR is already merged, cron skips without error.

        When a human or previous cron tick already merged the PR, the next
        cron tick must detect this and skip the merge call — no error,
        no double-merge attempt.
        """
        from core import iterate
        from tests.conftest import FakeKanban, FakeProvider

        fk = FakeKanban()
        issue_n = 9200
        pr_n = 9200

        fk.seed(
            assignee="documentation-daedalus",
            title=f"#{issue_n} Docs: Issue",
            status="blocked",
            reason=f"docs posted: PR #{pr_n}",
            body=f"Issue #{issue_n}\nPR #{pr_n}",
        )

        fk.seed(
            assignee="qa-daedalus",
            title=f"#{issue_n} QA: Issue",
            status="blocked",
            reason=f"qa-passed: PR #{pr_n}",
            summary=f"qa-passed: PR #{pr_n}",
            body=f"Issue #{issue_n}\nPR #{pr_n}",
            idempotency_key=f"qa-{issue_n}",
        )

        fk.seed(
            assignee="reviewer-daedalus",
            title=f"#{issue_n} Reviewer: Issue",
            status="blocked",
            reason=f"review-approved: PR #{pr_n}",
            summary=f"review-approved: PR #{pr_n}",
            body=f"Issue #{issue_n}\nPR #{pr_n}",
            idempotency_key=f"reviewer-{issue_n}",
        )

        fk.seed(
            assignee="security-analyst-daedalus",
            title=f"#{issue_n} Security: Issue",
            status="blocked",
            reason=f"security-approved: PR #{pr_n}",
            summary=f"security-approved: PR #{pr_n}",
            body=f"Issue #{issue_n}\nPR #{pr_n}",
            idempotency_key=f"security-{issue_n}",
        )

        resolved = {
            'execution': {'auto_merge': True, 'merge_method': 'squash'},
        }

        # PR is already merged — provider reports is_pr_merged=True
        provider = FakeProvider(
            ci_status="green",
            open_prs=set(),      # PR is not open (it's merged)
            merged_prs={pr_n},   # PR is merged
        )
        with kanban_as(iterate.kanban, fk):
            iterate.run_iterate(
                'test-board', 'test/repo',
                resolved=resolved, provider=provider,
            )
            assert len(provider.merged) == 0, (
                "Auto-merge must NOT be called when PR is already merged (idempotency)"
            )

    def test_reviewer_gate_blocks_merge(self):
        """Merge must NOT fire when reviewer has not approved the PR.

        Per issue #1085: all gates (QA, reviewer, security) must pass before
        merge. If the reviewer card has not posted an approval signal, the
        merge is blocked.
        """
        from core import iterate
        from tests.conftest import FakeKanban, FakeProvider

        fk = FakeKanban()
        issue_n = 9201
        pr_n = 9201

        fk.seed(
            assignee="documentation-daedalus",
            title=f"#{issue_n} Docs: Issue",
            status="blocked",
            reason=f"docs posted: PR #{pr_n}",
            body=f"Issue #{issue_n}\nPR #{pr_n}",
        )

        # QA passed
        fk.seed(
            assignee="qa-daedalus",
            title=f"#{issue_n} QA: Issue",
            status="blocked",
            reason=f"qa-passed: PR #{pr_n}",
            summary=f"qa-passed: PR #{pr_n}",
            body=f"Issue #{issue_n}\nPR #{pr_n}",
            idempotency_key=f"qa-{issue_n}",
        )

        # Reviewer has NOT approved — blocked with changes-requested
        fk.seed(
            assignee="reviewer-daedalus",
            title=f"#{issue_n} Reviewer: Issue",
            status="blocked",
            reason=f"review-changes-requested: PR #{pr_n}",
            summary=f"review-changes-requested: PR #{pr_n}",
            body=f"Issue #{issue_n}\nPR #{pr_n}",
            idempotency_key=f"reviewer-{issue_n}",
        )

        # Security passed
        fk.seed(
            assignee="security-analyst-daedalus",
            title=f"#{issue_n} Security: Issue",
            status="blocked",
            reason=f"security-approved: PR #{pr_n}",
            summary=f"security-approved: PR #{pr_n}",
            body=f"Issue #{issue_n}\nPR #{pr_n}",
            idempotency_key=f"security-{issue_n}",
        )

        resolved = {
            'execution': {'auto_merge': True, 'merge_method': 'squash'},
        }

        provider = FakeProvider(ci_status="green", open_prs={pr_n})
        with kanban_as(iterate.kanban, fk):
            iterate.run_iterate(
                'test-board', 'test/repo',
                resolved=resolved, provider=provider,
            )
            assert len(provider.merged) == 0, (
                "Auto-merge must NOT fire when reviewer has not approved"
            )

    def test_security_gate_blocks_merge(self):
        """Merge must NOT fire when security has not approved the PR."""
        from core import iterate
        from tests.conftest import FakeKanban, FakeProvider

        fk = FakeKanban()
        issue_n = 9202
        pr_n = 9202

        fk.seed(
            assignee="documentation-daedalus",
            title=f"#{issue_n} Docs: Issue",
            status="blocked",
            reason=f"docs posted: PR #{pr_n}",
            body=f"Issue #{issue_n}\nPR #{pr_n}",
        )

        # QA passed
        fk.seed(
            assignee="qa-daedalus",
            title=f"#{issue_n} QA: Issue",
            status="blocked",
            reason=f"qa-passed: PR #{pr_n}",
            summary=f"qa-passed: PR #{pr_n}",
            body=f"Issue #{issue_n}\nPR #{pr_n}",
            idempotency_key=f"qa-{issue_n}",
        )

        # Reviewer approved
        fk.seed(
            assignee="reviewer-daedalus",
            title=f"#{issue_n} Reviewer: Issue",
            status="blocked",
            reason=f"review-approved: PR #{pr_n}",
            summary=f"review-approved: PR #{pr_n}",
            body=f"Issue #{issue_n}\nPR #{pr_n}",
            idempotency_key=f"reviewer-{issue_n}",
        )

        # Security has NOT approved — still pending
        fk.seed(
            assignee="security-analyst-daedalus",
            title=f"#{issue_n} Security: Issue",
            status="blocked",
            reason=f"security-pending: PR #{pr_n}",
            summary=f"security-pending: PR #{pr_n}",
            body=f"Issue #{issue_n}\nPR #{pr_n}",
            idempotency_key=f"security-{issue_n}",
        )

        resolved = {
            'execution': {'auto_merge': True, 'merge_method': 'squash'},
        }

        provider = FakeProvider(ci_status="green", open_prs={pr_n})
        with kanban_as(iterate.kanban, fk):
            iterate.run_iterate(
                'test-board', 'test/repo',
                resolved=resolved, provider=provider,
            )
            assert len(provider.merged) == 0, (
                "Auto-merge must NOT fire when security has not approved"
            )

    def test_all_gates_passed_merges(self):
        """Merge fires when QA, reviewer, AND security have all passed."""
        from core import iterate
        from tests.conftest import FakeKanban, FakeProvider

        fk = FakeKanban()
        issue_n = 9203
        pr_n = 9203

        fk.seed(
            assignee="documentation-daedalus",
            title=f"#{issue_n} Docs: Issue",
            status="blocked",
            reason=f"docs posted: PR #{pr_n}",
            body=f"Issue #{issue_n}\nPR #{pr_n}",
        )

        # QA passed
        fk.seed(
            assignee="qa-daedalus",
            title=f"#{issue_n} QA: Issue",
            status="blocked",
            reason=f"qa-passed: PR #{pr_n}",
            summary=f"qa-passed: PR #{pr_n}",
            body=f"Issue #{issue_n}\nPR #{pr_n}",
            idempotency_key=f"qa-{issue_n}",
        )

        # Reviewer approved
        fk.seed(
            assignee="reviewer-daedalus",
            title=f"#{issue_n} Reviewer: Issue",
            status="blocked",
            reason=f"review-approved: PR #{pr_n}",
            summary=f"review-approved: PR #{pr_n}",
            body=f"Issue #{issue_n}\nPR #{pr_n}",
            idempotency_key=f"reviewer-{issue_n}",
        )

        # Security approved
        fk.seed(
            assignee="security-analyst-daedalus",
            title=f"#{issue_n} Security: Issue",
            status="blocked",
            reason=f"security-approved: PR #{pr_n}",
            summary=f"security-approved: PR #{pr_n}",
            body=f"Issue #{issue_n}\nPR #{pr_n}",
            idempotency_key=f"security-{issue_n}",
        )

        resolved = {
            'execution': {'auto_merge': True, 'merge_method': 'squash'},
        }

        provider = FakeProvider(ci_status="green", open_prs={pr_n})
        with kanban_as(iterate.kanban, fk):
            iterate.run_iterate(
                'test-board', 'test/repo',
                resolved=resolved, provider=provider,
            )
            assert len(provider.merged) > 0, (
                "Auto-merge should fire when QA, reviewer, and security have all passed"
            )
            assert provider.merged[0][0] == pr_n

    def test_ci_passed_before_docs_no_deferred_path(self):
        """CI green before docs completes → normal merge path, no deferral.

        When CI is already green when the docs card completes, the merge fires
        immediately on the same tick. This is NOT the deferred path — it's the
        normal happy path. The test verifies the merge happens on tick 1
        without needing a second tick.
        """
        from core import iterate
        from tests.conftest import FakeKanban, FakeProvider

        fk = FakeKanban()
        issue_n = 9204
        pr_n = 9204

        docs_tid = fk.seed(
            assignee="documentation-daedalus",
            title=f"#{issue_n} Docs: Issue",
            status="blocked",
            reason=f"docs posted: PR #{pr_n}",
            body=f"Issue #{issue_n}\nPR #{pr_n}",
        )

        fk.seed(
            assignee="qa-daedalus",
            title=f"#{issue_n} QA: Issue",
            status="blocked",
            reason=f"qa-passed: PR #{pr_n}",
            summary=f"qa-passed: PR #{pr_n}",
            body=f"Issue #{issue_n}\nPR #{pr_n}",
            idempotency_key=f"qa-{issue_n}",
        )

        fk.seed(
            assignee="reviewer-daedalus",
            title=f"#{issue_n} Reviewer: Issue",
            status="blocked",
            reason=f"review-approved: PR #{pr_n}",
            summary=f"review-approved: PR #{pr_n}",
            body=f"Issue #{issue_n}\nPR #{pr_n}",
            idempotency_key=f"reviewer-{issue_n}",
        )

        fk.seed(
            assignee="security-analyst-daedalus",
            title=f"#{issue_n} Security: Issue",
            status="blocked",
            reason=f"security-approved: PR #{pr_n}",
            summary=f"security-approved: PR #{pr_n}",
            body=f"Issue #{issue_n}\nPR #{pr_n}",
            idempotency_key=f"security-{issue_n}",
        )

        resolved = {
            'execution': {'auto_merge': True, 'merge_method': 'squash'},
        }

        # CI is already green — merge should fire on this tick (no deferral)
        provider = FakeProvider(ci_status="green", open_prs={pr_n})
        with kanban_as(iterate.kanban, fk):
            iterate.run_iterate(
                'test-board', 'test/repo',
                resolved=resolved, provider=provider,
            )
            assert len(provider.merged) > 0, (
                "Merge should fire immediately when CI is already green (no deferral)"
            )
            # Card should be completed (not left blocked for deferred merge)
            assert fk.tasks[docs_tid].get("status") == "done", (
                "Docs card should be completed when CI is green (not deferred)"
            )

    def test_idempotent_no_double_merge(self):
        """Re-running cron after merge does not double-merge or error.

        After a successful merge, the docs card is completed and disappears
        from list_blocked. A second cron tick finds no blocked docs card and
        does not attempt to merge again.
        """
        from core import iterate
        from tests.conftest import FakeKanban, FakeProvider

        fk = FakeKanban()
        issue_n = 9205
        pr_n = 9205

        docs_tid = fk.seed(
            assignee="documentation-daedalus",
            title=f"#{issue_n} Docs: Issue",
            status="blocked",
            reason=f"docs posted: PR #{pr_n}",
            body=f"Issue #{issue_n}\nPR #{pr_n}",
        )

        fk.seed(
            assignee="qa-daedalus",
            title=f"#{issue_n} QA: Issue",
            status="blocked",
            reason=f"qa-passed: PR #{pr_n}",
            summary=f"qa-passed: PR #{pr_n}",
            body=f"Issue #{issue_n}\nPR #{pr_n}",
            idempotency_key=f"qa-{issue_n}",
        )

        fk.seed(
            assignee="reviewer-daedalus",
            title=f"#{issue_n} Reviewer: Issue",
            status="blocked",
            reason=f"review-approved: PR #{pr_n}",
            summary=f"review-approved: PR #{pr_n}",
            body=f"Issue #{issue_n}\nPR #{pr_n}",
            idempotency_key=f"reviewer-{issue_n}",
        )

        fk.seed(
            assignee="security-analyst-daedalus",
            title=f"#{issue_n} Security: Issue",
            status="blocked",
            reason=f"security-approved: PR #{pr_n}",
            summary=f"security-approved: PR #{pr_n}",
            body=f"Issue #{issue_n}\nPR #{pr_n}",
            idempotency_key=f"security-{issue_n}",
        )

        resolved = {
            'execution': {'auto_merge': True, 'merge_method': 'squash'},
        }

        with kanban_as(iterate.kanban, fk):
            # Tick 1: CI green → merge fires, docs card completed
            provider1 = FakeProvider(ci_status="green", open_prs={pr_n})
            iterate.run_iterate(
                'test-board', 'test/repo',
                resolved=resolved, provider=provider1,
            )
            assert len(provider1.merged) == 1, "Tick 1: merge should fire exactly once"
            assert fk.tasks[docs_tid].get("status") == "done"

            # Tick 2: docs card is done, not blocked → no merge attempt
            provider2 = FakeProvider(ci_status="green", open_prs={pr_n})
            iterate.run_iterate(
                'test-board', 'test/repo',
                resolved=resolved, provider=provider2,
            )
            assert len(provider2.merged) == 0, (
                "Tick 2: must NOT re-merge — docs card is already done"
            )

    def test_deferred_merge_audit_logging(self):
        """Deferred merge path logs the transition for auditability.

        When CI passes after docs completion and the merge fires on a subsequent
        tick, the log should record:
        1. The deferral (CI not green, card stays blocked)
        2. The eventual merge (CI now green, merge triggered)
        """
        import logging
        from core import iterate
        from tests.conftest import FakeKanban, FakeProvider

        fk = FakeKanban()

        # Capture log messages
        log_messages = []

        class _ListHandler(logging.Handler):
            def emit(self, record):
                log_messages.append(self.format(record))

        handler = _ListHandler()
        handler.setLevel(logging.DEBUG)
        logger = iterate.logger
        old_level = logger.level
        logger.setLevel(logging.DEBUG)  # Ensure INFO messages are captured
        logger.addHandler(handler)

        try:
            issue_n = 9206
            pr_n = 9206

            fk.seed(
                assignee="documentation-daedalus",
                title=f"#{issue_n} Docs: Issue",
                status="blocked",
                reason=f"docs posted: PR #{pr_n}",
                body=f"Issue #{issue_n}\nPR #{pr_n}",
            )

            fk.seed(
                assignee="qa-daedalus",
                title=f"#{issue_n} QA: Issue",
                status="blocked",
                reason=f"qa-passed: PR #{pr_n}",
                summary=f"qa-passed: PR #{pr_n}",
                body=f"Issue #{issue_n}\nPR #{pr_n}",
                idempotency_key=f"qa-{issue_n}",
            )

            fk.seed(
                assignee="reviewer-daedalus",
                title=f"#{issue_n} Reviewer: Issue",
                status="blocked",
                reason=f"review-approved: PR #{pr_n}",
                summary=f"review-approved: PR #{pr_n}",
                body=f"Issue #{issue_n}\nPR #{pr_n}",
                idempotency_key=f"reviewer-{issue_n}",
            )

            fk.seed(
                assignee="security-analyst-daedalus",
                title=f"#{issue_n} Security: Issue",
                status="blocked",
                reason=f"security-approved: PR #{pr_n}",
                summary=f"security-approved: PR #{pr_n}",
                body=f"Issue #{issue_n}\nPR #{pr_n}",
                idempotency_key=f"security-{issue_n}",
            )

            resolved = {
                'execution': {'auto_merge': True, 'merge_method': 'squash'},
            }

            with kanban_as(iterate.kanban, fk):
                # Tick 1: CI pending → deferred (should log deferral)
                provider_pending = FakeProvider(ci_status="pending", open_prs={pr_n})
                iterate.run_iterate(
                    'test-board', 'test/repo',
                    resolved=resolved, provider=provider_pending,
                )

                # Tick 2: CI green → merge fires (should log merge)
                provider_green = FakeProvider(ci_status="green", open_prs={pr_n})
                iterate.run_iterate(
                    'test-board', 'test/repo',
                    resolved=resolved, provider=provider_green,
                )

            all_logs = " ".join(log_messages)

            # Verify deferral was logged
            assert "deferring" in all_logs.lower(), (
                "Log should record the deferral (CI not green, card stays blocked)"
            )
            # Verify merge was logged
            assert "auto-merged" in all_logs.lower(), (
                "Log should record the merge after CI passes"
            )
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)


class TestCronTickMergeTriggerIntegration:
    """End-to-end integration test for the full cron-tick-to-merge flow.

    Simulates the complete lifecycle: docs completes with CI pending →
    deferred → CI turns green on next tick → all gates checked → merge fires.
    """

    def test_full_deferred_merge_lifecycle(self):
        """Full lifecycle: CI pending at docs → deferred → CI green → all gates pass → merge.

        Tick 1: docs card blocked, CI pending → deferred (no merge, card stays blocked)
        Tick 2: CI now green, QA/reviewer/security all passed → merge fires
        Tick 3: docs card completed, no blocked card → no merge attempt (idempotent)
        """
        from core import iterate
        from tests.conftest import FakeKanban, FakeProvider

        fk = FakeKanban()
        issue_n = 9300
        pr_n = 9300

        docs_tid = fk.seed(
            assignee="documentation-daedalus",
            title=f"#{issue_n} Docs: Issue",
            status="blocked",
            reason=f"docs posted: PR #{pr_n}",
            body=f"Issue #{issue_n}\nPR #{pr_n}",
        )

        fk.seed(
            assignee="qa-daedalus",
            title=f"#{issue_n} QA: Issue",
            status="blocked",
            reason=f"qa-passed: PR #{pr_n}",
            summary=f"qa-passed: PR #{pr_n}",
            body=f"Issue #{issue_n}\nPR #{pr_n}",
            idempotency_key=f"qa-{issue_n}",
        )

        fk.seed(
            assignee="reviewer-daedalus",
            title=f"#{issue_n} Reviewer: Issue",
            status="blocked",
            reason=f"review-approved: PR #{pr_n}",
            summary=f"review-approved: PR #{pr_n}",
            body=f"Issue #{issue_n}\nPR #{pr_n}",
            idempotency_key=f"reviewer-{issue_n}",
        )

        fk.seed(
            assignee="security-analyst-daedalus",
            title=f"#{issue_n} Security: Issue",
            status="blocked",
            reason=f"security-approved: PR #{pr_n}",
            summary=f"security-approved: PR #{pr_n}",
            body=f"Issue #{issue_n}\nPR #{pr_n}",
            idempotency_key=f"security-{issue_n}",
        )

        resolved = {
            'execution': {'auto_merge': True, 'merge_method': 'squash'},
        }

        with kanban_as(iterate.kanban, fk):
            # Tick 1: CI pending → deferred
            provider1 = FakeProvider(ci_status="pending", open_prs={pr_n})
            iterate.run_iterate(
                'test-board', 'test/repo',
                resolved=resolved, provider=provider1,
            )
            assert len(provider1.merged) == 0, "Tick 1: no merge when CI pending"
            assert fk.tasks[docs_tid].get("status") == "blocked", (
                "Tick 1: docs card stays blocked when CI pending"
            )

            # Tick 2: CI green, all gates passed → merge fires
            provider2 = FakeProvider(ci_status="green", open_prs={pr_n})
            iterate.run_iterate(
                'test-board', 'test/repo',
                resolved=resolved, provider=provider2,
            )
            assert len(provider2.merged) == 1, (
                "Tick 2: merge should fire when CI green and all gates passed"
            )
            assert provider2.merged[0][0] == pr_n
            assert fk.tasks[docs_tid].get("status") == "done", (
                "Tick 2: docs card completed after merge"
            )

            # Tick 3: docs card done, not blocked → no merge (idempotent)
            provider3 = FakeProvider(ci_status="green", open_prs={pr_n})
            iterate.run_iterate(
                'test-board', 'test/repo',
                resolved=resolved, provider=provider3,
            )
            assert len(provider3.merged) == 0, (
                "Tick 3: no re-merge — docs card already done (idempotent)"
            )
