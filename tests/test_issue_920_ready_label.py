"""Regression tests for issue #920: auto-advance of independent sub-issues to Ready status.

Issue #920 Scope:
- Sub-issues with NO `depends_on` metadata must receive the `status:ready` label automatically
- Sub-issues WITH `depends_on` metadata must NOT receive the `status:ready` label
- This verifies the dependency-aware conditional labeling implemented in _execute_planner_decompose_inner

References:
- Epic #915: feat: auto-advance sub-issues to Ready after planner decomposition
- PR #937: Initial implementation (merged)
- Issue #919: Dependency tracking foundation (depends_on parsing)
"""
from __future__ import annotations

import contextlib
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from core.iterate import _execute_planner_decompose
from core.providers.base import VCSProvider, parse_depends_on
from core import iterate


def _make_provider(
    *,
    sub_issue_numbers: list[int],
    epic_body: str,
    epic_number: int = 100,
    board_configured: bool = True,
):
    """Build a mock VCS provider for planner decomposition tests."""
    provider = MagicMock(spec=VCSProvider)
    
    # Parent epic issue
    epic_issue = MagicMock(
        number=epic_number,
        title=f"Epic #{epic_number}",
        body=epic_body,
        labels=[],
    )
    epic_issue.as_dict.return_value = {
        "number": epic_number,
        "title": epic_issue.title,
        "body": epic_body,
        "labels": [],
    }
    provider.get_issue.return_value = epic_issue
    provider.get_issue_comments.return_value = []
    
    # Sub-issue creation returns the provided numbers in sequence
    provider.create_issue.side_effect = sub_issue_numbers
    
    # Label and board operations
    provider.add_label.return_value = True
    provider.board_set_status.return_value = True
    provider.board_configured.return_value = board_configured
    
    # Comment posting
    provider.post_issue_comment.return_value = True
    
    return provider


def _make_card(issue_number: int = 100) -> dict:
    """Create a minimal kanban card for testing."""
    return {
        "id": f"t_test_{issue_number}",
        "title": f"Test Card #{issue_number}",
        "body": f"See issue #{issue_number}",
        "assignee": "planner-daedalus",
    }


@contextlib.contextmanager
def _mock_kanban():
    """Patch kanban operations at method level to avoid side effects."""
    import unittest.mock as mock
    from core import kanban as _core_kanban
    with (
        mock.patch.object(_core_kanban, "complete", return_value=True),
        mock.patch.object(_core_kanban, "create_triage", return_value="t_triage"),
        mock.patch.object(_core_kanban, "decompose", return_value=True),
        mock.patch.object(_core_kanban, "list_tasks", return_value=[]),
    ):
        yield


class TestIssue920ReadyLabelAutoAdvance:
    """Issue #920: Verify status:ready label is applied based on depends_on metadata."""
    
    def test_ready_label_applied_when_no_depends_on(self, tmp_path):
        """Sub-issue with NO depends_on metadata must receive Ready label."""
        epic_body = "## Scope\n- [ ] Task A\n- [ ] Task B"
        provider = _make_provider(
            sub_issue_numbers=[101, 102],
            epic_body=epic_body,
        )
        with _mock_kanban():
            result = _execute_planner_decompose(
                slug="test",
                card=_make_card(100),
                repo="test/repo",
                handoff_text="PLANNING COMPLETE",
                workdir=str(tmp_path),
                provider=provider,
                dry_run=False,
            )

        assert result is True, "Decomposition should succeed"

        # Find all Ready label calls
        ready_calls = [
            call for call in provider.add_label.call_args_list
            if call[0][1] == "Ready"
        ]

        # First sub-issue (tier-0, no dependencies) should get Ready label
        assert len(ready_calls) >= 1, "At least one sub-issue should receive Ready label"
        assert ready_calls[0][0][0] == 101, "First sub-issue (#101) should get Ready label"
    
    def test_ready_label_not_applied_when_depends_on_present(self, tmp_path):
        """Sub-issue WITH depends_on metadata must NOT receive Ready label initially."""
        # Epic body that will generate sub-issues with dependencies
        # The planner generates sequential dependencies: sub-issue N depends on all previous
        epic_body = "## Scope\n- [ ] Task A\n- [ ] Task B\n- [ ] Task C"
        provider = _make_provider(
            sub_issue_numbers=[201, 202, 203],
            epic_body=epic_body,
        )
        with _mock_kanban():
            result = _execute_planner_decompose(
                slug="test",
                card=_make_card(100),
                repo="test/repo",
                handoff_text="PLANNING COMPLETE",
                workdir=str(tmp_path),
                provider=provider,
                dry_run=False,
            )

        assert result is True, "Decomposition should succeed"

        # Verify that sub-issues with dependencies do NOT get Ready label
        # Only tier-0 (first) sub-issue should get it
        ready_calls = [
            call for call in provider.add_label.call_args_list
            if call[0][1] == "Ready"
        ]
        
        # Should only have Ready label for tier-0 sub-issue
        assert len(ready_calls) == 1, f"Only tier-0 should get Ready, got {len(ready_calls)} calls"
        assert ready_calls[0][0][0] == 201, "Only first sub-issue (#201) should get Ready label"
        
        # Verify other sub-issues did NOT get Ready label
        ready_issue_numbers = {call[0][0] for call in ready_calls}
        assert 202 not in ready_issue_numbers, "Sub-issue #202 (has deps) should NOT get Ready label"
        assert 203 not in ready_issue_numbers, "Sub-issue #203 (has deps) should NOT get Ready label"
    
    def test_parse_depends_on_detects_dependencies(self):
        """Verify parse_depends_on correctly identifies dependency metadata."""
        # No dependencies
        body_no_deps = "## Scope\n- [ ] Task A\n- [ ] Task B"
        assert parse_depends_on(body_no_deps) == [], "Should return empty list when no depends_on"
        
        # With dependencies
        body_with_deps = "## Scope\n- [ ] Task A\n\nDepends on: #101, #102"
        deps = parse_depends_on(body_with_deps)
        assert deps == [101, 102], f"Should parse dependencies, got {deps}"
        
        # Empty depends_on line
        body_empty_deps = "## Scope\n- [ ] Task A\n\nDepends on:"
        assert parse_depends_on(body_empty_deps) == [], "Empty depends_on should return []"
    
    def test_single_subissue_always_gets_ready_label(self, tmp_path):
        """A single sub-issue (tier-0) must always receive Ready label."""
        epic_body = "## Scope\n- [ ] Only Task"
        provider = _make_provider(
            sub_issue_numbers=[301],
            epic_body=epic_body,
        )
        with _mock_kanban():
            result = _execute_planner_decompose(
                slug="test",
                card=_make_card(100),
                repo="test/repo",
                handoff_text="PLANNING COMPLETE",
                workdir=str(tmp_path),
                provider=provider,
                dry_run=False,
            )

        assert result is True

        # Single sub-issue should get Ready label
        ready_calls = [
            call for call in provider.add_label.call_args_list
            if call[0][1] == "Ready"
        ]
        assert len(ready_calls) == 1, "Single sub-issue should get Ready label"
        assert ready_calls[0][0][0] == 301
    
    def test_board_status_matches_ready_label(self, tmp_path):
        """Board status should match Ready label for tier-0 sub-issues."""
        epic_body = "## Scope\n- [ ] Task A\n- [ ] Task B"
        provider = _make_provider(
            sub_issue_numbers=[401, 402],
            epic_body=epic_body,
            board_configured=True,
        )
        with _mock_kanban():
            result = _execute_planner_decompose(
                slug="test",
                card=_make_card(100),
                repo="test/repo",
                handoff_text="PLANNING COMPLETE",
                workdir=str(tmp_path),
                provider=provider,
                dry_run=False,
            )

        assert result is True

        # Verify Ready label and board status are aligned
        ready_label_calls = {
            call[0][0] for call in provider.add_label.call_args_list
            if call[0][1] == "Ready"
        }
        ready_board_calls = {
            call[0][0] for call in provider.board_set_status.call_args_list
            if call[0][1] == "Ready"
        }
        
        # Tier-0 sub-issue should have both Ready label and Ready board status
        assert 401 in ready_label_calls, "Tier-0 sub-issue should get Ready label"
        assert 401 in ready_board_calls, "Tier-0 sub-issue should get Ready board status"
        
        # Tier-1 sub-issue should NOT have Ready label or status
        assert 402 not in ready_label_calls, "Tier-1 sub-issue should NOT get Ready label"
        assert 402 not in ready_board_calls, "Tier-1 sub-issue should NOT get Ready board status"
    
    def test_board_not_configured_ready_label_still_applied(self, tmp_path):
        """Ready label should be applied even when board is not configured."""
        epic_body = "## Scope\n- [ ] Task A"
        provider = _make_provider(
            sub_issue_numbers=[501],
            epic_body=epic_body,
            board_configured=False,
        )
        with _mock_kanban():
            result = _execute_planner_decompose(
                slug="test",
                card=_make_card(100),
                repo="test/repo",
                handoff_text="PLANNING COMPLETE",
                workdir=str(tmp_path),
                provider=provider,
                dry_run=False,
            )

        assert result is True

        # Ready label should still be applied
        ready_calls = [
            call for call in provider.add_label.call_args_list
            if call[0][1] == "Ready"
        ]
        assert len(ready_calls) == 1, "Ready label should be applied regardless of board config"
        assert ready_calls[0][0][0] == 501
        
        # Board operations should not be called
        assert provider.board_set_status.call_count == 0, "No board calls when board not configured"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
