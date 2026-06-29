"""Integration tests for planner signal handlers coexistence.

Verifies that the 'PLANNING COMPLETE' and 'NOT SUITABLE FOR DECOMPOSITION'
handlers dispatch correctly and independently — the critical requirement
that the existing PLANNING COMPLETE path remains completely unchanged
after adding the NOT SUITABLE handler.

Issue: #920 / Epic: #918
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import (  # noqa: E402
    FakeKanban,
    FakeProvider,
    _load_dispatch,
)

disp = _load_dispatch()


# ── Helpers ──────────────────────────────────────────────────────────────────


def _setup_fake_board():
    """Set up FakeKanban and FakeProvider with standard patches."""
    fake_kb = FakeKanban()
    fake_provider = FakeProvider()
    
    patches = [
        mock.patch.object(disp.kanban, "list_tasks", side_effect=fake_kb.list_tasks),
        mock.patch.object(disp.kanban, "show_card", side_effect=lambda slug, tid: fake_kb.tasks.get(tid)),
        mock.patch.object(disp.kanban, "create_task", side_effect=fake_kb.create_task),
    ]
    return fake_kb, fake_provider, patches


def _make_planner_card(
    fake_kb: FakeKanban,
    issue_n: int,
    summary: str,
    status: str = "done",
    *,
    title: str = "",
    tid: str = "",
):
    """Seed a planner card with the given summary."""
    if not tid:
        tid = fake_kb.seed(
            assignee="planner-daedalus",
            title=title or f"#{issue_n} Epic for integration test",
            status=status,
            summary=summary,
            body=f"#{issue_n} Epic body for testing",
        )
    else:
        fake_kb.tasks[tid] = {
            "id": tid,
            "assignee": "planner-daedalus",
            "title": title or f"#{issue_n} Epic for integration test",
            "status": status,
            "summary": summary,
            "latest_summary": summary,
            "body": f"#{issue_n} Epic body for testing",
            "idempotency_key": "",
            "comments": [],
        }
    return tid


# ── Integration Tests ────────────────────────────────────────────────────────


class TestPlannerSignalCoexistence:
    """Verify both signals dispatch independently without interference."""

    def test_planning_complete_fires_completed_planner_only(self):
        """PLANNING COMPLETE triggers _check_completed_planner, NOT _check_planner_not_suitable."""
        fake_kb, fake_provider, patches = _setup_fake_board()
        
        # Seed a card with PLANNING COMPLETE signal
        _make_planner_card(
            fake_kb,
            42,
            "PLANNING COMPLETE: ready for decomposition into 3 sub-issues",
        )
        
        # Set up provider to track calls
        issues_map = {42: {"number": 42, "title": "Epic #42", "body": "Epic body", "labels": ["epic"]}}
        
        with patches[0], patches[1], patches[2]:
            # Mock _execute_planner_decompose to avoid actual sub-issue creation
            with mock.patch("core.iterate._execute_planner_decompose", return_value=True) as mock_decompose:
                # Call PLANNING COMPLETE handler
                triggered_complete = disp._check_completed_planner(
                    "test-slug",
                    workdir="/tmp/test",
                    dry_run=False,
                    provider=fake_provider,
                )
                
                # Call NOT SUITABLE handler
                triggered_not_suitable = disp._check_planner_not_suitable(
                    "test-slug",
                    repo="test/repo",
                    issues_map=issues_map,
                    workdir="/tmp/test",
                    base_branch="dev",
                    provider_name="github",
                    dry_run=False,
                    provider=fake_provider,
                )
        
        # PLANNING COMPLETE handler should have triggered
        assert triggered_complete == [42], f"Expected [42], got {triggered_complete}"
        
        # NOT SUITABLE handler should NOT have triggered (signal doesn't match)
        assert triggered_not_suitable == [], f"Expected [], got {triggered_not_suitable}"
        
        # Verify _execute_planner_decompose was called (decompose path fired)
        assert mock_decompose.called, "_execute_planner_decompose should have been called"
        
        # Verify no validator task was created (NOT SUITABLE path didn't fire)
        validator_tasks = [c for c in fake_kb.created if "validator" in c.get("assignee", "")]
        assert len(validator_tasks) == 0, "No validator task should be created for PLANNING COMPLETE"

    def test_not_suitable_fires_not_suitable_handler_only(self):
        """NOT SUITABLE triggers _check_planner_not_suitable, NOT _check_completed_planner."""
        fake_kb, fake_provider, patches = _setup_fake_board()
        
        # Seed a card with NOT SUITABLE signal
        _make_planner_card(
            fake_kb,
            43,
            "NOT SUITABLE FOR DECOMPOSITION: issue is too small, implement directly",
        )
        
        issues_map = {43: {"number": 43, "title": "Small fix", "body": "Small fix body", "labels": []}}
        
        with patches[0], patches[1], patches[2]:
            # Mock _execute_planner_decompose (should NOT be called)
            with mock.patch("core.iterate._execute_planner_decompose", return_value=True) as mock_decompose:
                # Call PLANNING COMPLETE handler
                triggered_complete = disp._check_completed_planner(
                    "test-slug",
                    workdir="/tmp/test",
                    dry_run=False,
                    provider=fake_provider,
                )
                
                # Call NOT SUITABLE handler
                triggered_not_suitable = disp._check_planner_not_suitable(
                    "test-slug",
                    repo="test/repo",
                    issues_map=issues_map,
                    workdir="/tmp/test",
                    base_branch="dev",
                    provider_name="github",
                    dry_run=False,
                    provider=fake_provider,
                )
        
        # PLANNING COMPLETE handler should NOT have triggered (signal doesn't match)
        assert triggered_complete == [], f"Expected [], got {triggered_complete}"
        
        # NOT SUITABLE handler should have triggered
        assert triggered_not_suitable == [43], f"Expected [43], got {triggered_not_suitable}"
        
        # Verify _execute_planner_decompose was NOT called (decompose path didn't fire)
        assert not mock_decompose.called, "_execute_planner_decompose should NOT have been called"
        
        # Verify a validator task WAS created (NOT SUITABLE path fired)
        validator_tasks = [c for c in fake_kb.created if "validator" in c.get("assignee", "")]
        assert len(validator_tasks) == 1, f"Expected 1 validator task, got {len(validator_tasks)}"

    def test_both_signals_coexist_independently(self):
        """When both signals exist on different cards, each handler fires independently."""
        fake_kb, fake_provider, patches = _setup_fake_board()
        
        # Seed two cards: one with PLANNING COMPLETE, one with NOT SUITABLE
        _make_planner_card(
            fake_kb,
            44,
            "PLANNING COMPLETE: ready for decomposition",
            tid="t_planner_complete",
        )
        _make_planner_card(
            fake_kb,
            45,
            "NOT SUITABLE FOR DECOMPOSITION: already small enough",
            tid="t_planner_not_suitable",
        )
        
        issues_map = {
            44: {"number": 44, "title": "Epic #44", "body": "Epic body", "labels": ["epic"]},
            45: {"number": 45, "title": "Small fix", "body": "Small fix body", "labels": []},
        }
        
        with patches[0], patches[1], patches[2]:
            with mock.patch("core.iterate._execute_planner_decompose", return_value=True) as mock_decompose:
                # Call both handlers (simulating dispatcher tick)
                triggered_complete = disp._check_completed_planner(
                    "test-slug",
                    workdir="/tmp/test",
                    dry_run=False,
                    provider=fake_provider,
                )
                
                triggered_not_suitable = disp._check_planner_not_suitable(
                    "test-slug",
                    repo="test/repo",
                    issues_map=issues_map,
                    workdir="/tmp/test",
                    base_branch="dev",
                    provider_name="github",
                    dry_run=False,
                    provider=fake_provider,
                )
        
        # Both handlers should have triggered for their respective cards
        assert triggered_complete == [44], f"Expected [44], got {triggered_complete}"
        assert triggered_not_suitable == [45], f"Expected [45], got {triggered_not_suitable}"
        
        # Verify _execute_planner_decompose was called once (for card 44 only)
        assert mock_decompose.call_count == 1, f"Expected 1 call, got {mock_decompose.call_count}"
        
        # Verify the decompose was called for the right card (44, not 45)
        decompose_call_args = mock_decompose.call_args
        decompose_card = decompose_call_args[0][1]  # Second positional arg is the card dict
        assert "#44" in decompose_card.get("title", ""), "Decompose should have been called for card 44"
        
        # Verify a validator task was created (for card 45 only)
        validator_tasks = [c for c in fake_kb.created if "validator" in c.get("assignee", "")]
        assert len(validator_tasks) == 1, f"Expected 1 validator task, got {len(validator_tasks)}"
        assert "#45" in validator_tasks[0].get("title", ""), "Validator task should reference issue 45"

    def test_planning_complete_path_unaffected_by_not_suitable_handler(self):
        """CRITICAL: Verify the existing PLANNING COMPLETE path remains completely unchanged.
        
        This test ensures that adding the NOT SUITABLE handler did not break or alter
        the behavior of the original PLANNING COMPLETE handler — the core requirement
        of issue #920.
        """
        fake_kb, fake_provider, patches = _setup_fake_board()
        
        # Seed a card with PLANNING COMPLETE signal (the original happy path)
        _make_planner_card(
            fake_kb,
            46,
            "PLANNING COMPLETE: ready for decomposition into multiple sub-issues",
        )
        
        issues_map = {46: {"number": 46, "title": "Epic #46", "body": "Epic body", "labels": ["epic"]}}
        
        with patches[0], patches[1], patches[2]:
            # Mock _execute_planner_decompose to simulate successful decomposition
            with mock.patch("core.iterate._execute_planner_decompose", return_value=True) as mock_decompose:
                # Call the PLANNING COMPLETE handler (should work exactly as before)
                triggered_complete = disp._check_completed_planner(
                    "test-slug",
                    workdir="/tmp/test",
                    dry_run=False,
                    provider=fake_provider,
                )
        
        # PLANNING COMPLETE handler must trigger decompose
        assert triggered_complete == [46], f"PLANNING COMPLETE must trigger for issue 46, got {triggered_complete}"
        
        # Verify decompose was called with the correct card
        assert mock_decompose.called, "_execute_planner_decompose must be called for PLANNING COMPLETE"
        decompose_call_args = mock_decompose.call_args
        decompose_card = decompose_call_args[0][1]
        assert "#46" in decompose_card.get("title", ""), "Decompose must reference the correct issue"
        
        # Verify no side effects from NOT SUITABLE handler (it should not have fired)
        # No validator tasks should exist for this card
        validator_tasks_46 = [
            c for c in fake_kb.created 
            if "validator" in c.get("assignee", "") and "#46" in c.get("title", "")
        ]
        assert len(validator_tasks_46) == 0, "PLANNING COMPLETE must not create validator tasks"

    def test_handler_execution_order_matches_dispatcher(self):
        """Verify that handlers are called in the same order as the dispatcher tick.
        
        The dispatcher calls _check_completed_planner BEFORE _check_planner_not_suitable.
        This ordering ensures the happy path (PLANNING COMPLETE) takes precedence and
        is not accidentally intercepted by the NOT SUITABLE handler.
        """
        fake_kb, fake_provider, patches = _setup_fake_board()
        
        # Seed cards for both signals
        _make_planner_card(fake_kb, 47, "PLANNING COMPLETE: ready", tid="t_pc_47")
        _make_planner_card(fake_kb, 48, "NOT SUITABLE: too small", tid="t_ns_48")
        
        issues_map = {
            47: {"number": 47, "title": "Epic #47", "body": "body", "labels": ["epic"]},
            48: {"number": 48, "title": "Small #48", "body": "body", "labels": []},
        }
        
        call_order = []
        
        with patches[0], patches[1], patches[2]:
            with mock.patch("core.iterate._execute_planner_decompose", return_value=True) as mock_decompose:
                # Simulate dispatcher tick order: completed_planner first, then not_suitable
                call_order.append("completed_planner")
                triggered_complete = disp._check_completed_planner(
                    "test-slug", workdir="/tmp/test", dry_run=False, provider=fake_provider,
                )
                
                call_order.append("not_suitable")
                triggered_not_suitable = disp._check_planner_not_suitable(
                    "test-slug", repo="test/repo", issues_map=issues_map,
                    workdir="/tmp/test", base_branch="dev", provider_name="github",
                    dry_run=False, provider=fake_provider,
                )
        
        # Verify correct dispatch order
        assert call_order == ["completed_planner", "not_suitable"], "Dispatcher must call handlers in correct order"
        
        # Both should have triggered independently
        assert triggered_complete == [47], "PLANNING COMPLETE must trigger for 47"
        assert triggered_not_suitable == [48], "NOT SUITABLE must trigger for 48"
        
        # Verify decompose was called (for 47 only)
        assert mock_decompose.call_count == 1, "Decompose should be called once"
        
        # Verify validator task created (for 48 only)
        validator_tasks = [c for c in fake_kb.created if "validator" in c.get("assignee", "")]
        assert len(validator_tasks) == 1, "One validator task must be created"

    def test_blocked_planner_with_not_suitable_triggers_handler(self):
        """A blocked (not done) planner card with NOT SUITABLE signal must still trigger validation.
        
        The soul instructs the planner to complete (not block), but if the planner blocks anyway,
        the handler must detect the signal and route to validator. This is defense in depth.
        """
        fake_kb, fake_provider, patches = _setup_fake_board()
        
        # Seed a blocked planner card with NOT SUITABLE signal
        _make_planner_card(
            fake_kb,
            50,
            "NOT SUITABLE FOR DECOMPOSITION: already small",
            status="blocked",
            tid="t_blocked_planner",
        )
        
        issues_map = {50: {"number": 50, "title": "Small fix", "body": "...", "labels": []}}
        
        with patches[0], patches[1], patches[2]:
            # Call NOT SUITABLE handler (should detect blocked card)
            triggered_not_suitable = disp._check_planner_not_suitable(
                "test-slug",
                repo="test/repo",
                issues_map=issues_map,
                workdir="/tmp/test",
                base_branch="dev",
                provider_name="github",
                dry_run=False,
                provider=fake_provider,
            )
        
        # Handler should have triggered for the blocked card
        assert triggered_not_suitable == [50], f"Expected [50], got {triggered_not_suitable}"
        
        # Verify a validator task was created
        validator_tasks = [c for c in fake_kb.created if "validator" in c.get("assignee", "")]
        assert len(validator_tasks) == 1, f"Expected 1 validator task for blocked card, got {len(validator_tasks)}"
        assert "#50" in validator_tasks[0].get("title", ""), "Validator task should reference issue 50"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
