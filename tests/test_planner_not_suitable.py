"""Test for NOT SUITABLE FOR DECOMPOSITION signal handling in dispatcher.

When planner completes with a summary indicating the issue is NOT suitable for
decomposition, the dispatcher must:
1. Transition the issue out of 'In Progress' state
2. Post an explanatory comment on the issue
3. Close the issue (or mark as done/blocked)
4. Log the state transition
"""
from unittest import mock

from scripts.daedalus_dispatch import (
    _check_planner_not_suitable,
    kanban,
)


class TestPlannerNotSuitable:
    """Test _check_planner_not_suitable handler."""

    def test_not_suitable_signal_transitions_issue_state(self):
        """Planner 'NOT SUITABLE' summary must close the issue and post comment."""
        slug = "test-board"
        workdir = "/test/workdir"
        profiles = {"planner": "planner-test"}
        
        # Mock a planner task that completed with NOT SUITABLE signal
        mock_task = {
            "id": "t_planner_1",
            "assignee": "planner-test",
            "title": "#920 Epic: Large feature request",
            "summary": "NOT SUITABLE FOR DECOMPOSITION: issue is already small enough to implement directly",
            "status": "done",
        }
        
        with mock.patch.object(kanban, "list_tasks", return_value=[mock_task]), \
             mock.patch.object(kanban, "show_card", return_value=mock_task), \
             mock.patch("scripts.daedalus_dispatch.logger") as mock_logger:
            
            # Mock provider
            mock_provider = mock.MagicMock()
            mock_provider.board_configured.return_value = True
            mock_provider.board_set_status.return_value = True
            mock_provider.post_issue_comment.return_value = True
            mock_provider.close_issue.return_value = True
            mock_provider.status_name.return_value = "done"
            
            # Call the handler
            triggered = _check_planner_not_suitable(
                slug, workdir, profiles=profiles, provider=mock_provider,
            )
            
            # Verify it detected and handled the signal
            assert 920 in triggered, "Should return list of issue numbers that were handled"
            
            # Verify state transitions
            mock_provider.board_set_status.assert_called_once_with(920, mock_provider.status_name("done"))
            mock_provider.close_issue.assert_called_once_with(920)
            mock_provider.post_issue_comment.assert_called_once()
            
            # Verify the comment explains the state change
            comment_body = mock_provider.post_issue_comment.call_args[0][1]
            assert "NOT SUITABLE" in comment_body.upper() or "not suitable" in comment_body.lower()
            assert "decomposition" in comment_body.lower() or "decompos" in comment_body.lower()
            
            # Verify logging
            mock_logger.info.assert_any_call(
                "dispatch: planner NOT SUITABLE #%s — closing issue (not suitable for decomposition)",
                920,
            )

    def test_not_suitable_signal_with_blocked_state(self):
        """Planner can signal 'NOT SUITABLE' due to blocking dependency → mark as Blocked."""
        slug = "test-board"
        workdir = "/test/workdir"
        profiles = {"planner": "planner-test"}
        
        # Mock a planner task with NOT SUITABLE due to blocker
        mock_task = {
            "id": "t_planner_2",
            "assignee": "planner-test",
            "title": "#921 Epic: Feature with dependencies",
            "summary": "NOT SUITABLE: has blocking dependency on other issues",
            "status": "done",
        }
        
        with mock.patch.object(kanban, "list_tasks", return_value=[mock_task]), \
             mock.patch.object(kanban, "show_card", return_value=mock_task), \
             mock.patch("scripts.daedalus_dispatch.logger"):
            
            mock_provider = mock.MagicMock()
            mock_provider.board_configured.return_value = True
            mock_provider.board_set_status.return_value = True
            mock_provider.post_issue_comment.return_value = True
            mock_provider.close_issue.return_value = True
            mock_provider.status_name.side_effect = lambda key: {"done": "Done", "blocked": "Blocked"}.get(key, key)
            
            triggered = _check_planner_not_suitable(
                slug, workdir, profiles=profiles, provider=mock_provider,
            )
            
            assert 921 in triggered
            
            # With blocking dependencies, should set board status to Blocked (not close)
            mock_provider.board_set_status.assert_called_once_with(921, "Blocked")
            
            # Should NOT close the issue (it's blocked, not done)
            mock_provider.close_issue.assert_not_called()
            
            # Should post comment explaining the block
            mock_provider.post_issue_comment.assert_called_once()
            comment_body = mock_provider.post_issue_comment.call_args[0][1]
            assert "block" in comment_body.lower() or "depend" in comment_body.lower()

    def test_planning_complete_not_treated_as_not_suitable(self):
        """PLANNING COMPLETE should NOT trigger the NOT SUITABLE handler."""
        slug = "test-board"
        workdir = "/test/workdir"
        profiles = {"planner": "planner-test"}
        
        # Mock a normal PLANNING COMPLETE task
        mock_task = {
            "id": "t_planner_3",
            "assignee": "planner-test",
            "title": "#922 Epic: Ready for decomposition",
            "summary": "PLANNING COMPLETE: ready for decomposition",
            "status": "done",
        }
        
        with mock.patch.object(kanban, "list_tasks", return_value=[mock_task]), \
             mock.patch.object(kanban, "show_card", return_value=mock_task):
            
            mock_provider = mock.MagicMock()
            
            triggered = _check_planner_not_suitable(
                slug, workdir, profiles=profiles, provider=mock_provider,
            )
            
            # Should NOT handle this - it's a normal PLANNING COMPLETE
            assert len(triggered) == 0
            
            # Should NOT have closed or posted comments
            mock_provider.close_issue.assert_not_called()
            mock_provider.post_issue_comment.assert_not_called()
            mock_provider.board_set_status.assert_not_called()

    def test_not_suitable_dry_run_does_not_mutate(self):
        """Dry-run mode should log but not call provider methods."""
        slug = "test-board"
        workdir = "/test/workdir"
        profiles = {"planner": "planner-test"}
        
        mock_task = {
            "id": "t_planner_4",
            "assignee": "planner-test",
            "title": "#923 Epic: Not suitable example",
            "summary": "NOT SUITABLE FOR DECOMPOSITION: already small",
            "status": "done",
        }
        
        with mock.patch.object(kanban, "list_tasks", return_value=[mock_task]), \
             mock.patch.object(kanban, "show_card", return_value=mock_task), \
             mock.patch("scripts.daedalus_dispatch.logger") as mock_logger:
            
            mock_provider = mock.MagicMock()
            
            triggered = _check_planner_not_suitable(
                slug, workdir, profiles=profiles, provider=mock_provider,
                dry_run=True,
            )
            
            # Should return the issue number even in dry-run
            assert 923 in triggered
            
            # Should NOT have mutated anything
            mock_provider.close_issue.assert_not_called()
            mock_provider.post_issue_comment.assert_not_called()
            mock_provider.board_set_status.assert_not_called()
            
            # Should have logged what would happen
            mock_logger.info.assert_any_call(
                "[dry-run] planner NOT SUITABLE #%s — would close issue + post comment",
                923,
            )
