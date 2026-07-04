"""Issue #1008 — Dispatcher behavior when downstream tasks reach terminal states.

Integration tests verifying the dispatcher correctly handles scenarios where
downstream tasks (developer/reviewer/QA) are in terminal states (done/cancelled/archived).
The dispatcher should NOT be blocked from creating new work when all downstream tasks are finished.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import conftest
from conftest import PIPELINE_ROLES, FakeKanban, _load_dispatch, kanban_as

SLUG = "proj1008"


def check(name: str, cond: bool) -> None:
    conftest.check(name, cond)
    if not cond and os.environ.get("PYTEST_CURRENT_TEST"):
        raise AssertionError(name)


def test_dispatcher_allows_new_dispatch_when_downstream_terminal():
    """Dispatcher should allow new downstream work when all existing tasks are terminal."""
    disp = _load_dispatch()
    fk = FakeKanban()

    issue_n = 10081

    # Seed downstream tasks all in terminal states
    fk.seed(
        assignee=PIPELINE_ROLES["developer"],
        title=f"#{issue_n} Developer: stale",
        status="done",
        summary="earlier attempt completed",
    )
    fk.seed(
        assignee=PIPELINE_ROLES["reviewer"],
        title=f"#{issue_n} Reviewer: stale",
        status="completed",
        summary="reviewed earlier PR",
    )
    fk.seed(
        assignee=PIPELINE_ROLES["developer"],
        title=f"#{issue_n} Developer: cancelled",
        status="cancelled",
        summary="",
    )

    with kanban_as(disp.kanban, fk):
        # _has_downstream_tasks should return False (all terminal)
        # This means dispatcher is NOT blocked and can create new work
        result = disp._has_downstream_tasks(
            SLUG, issue_n,
            validator_profile=PIPELINE_ROLES["validator"],
            pm_profile=PIPELINE_ROLES["pm"],
            planner_profile="planner-daedalus",
        )
        check(
            "_has_downstream_tasks returns False when all downstream are terminal (dispatcher unblocked)",
            result is False,
        )

        # Now add an active task - dispatcher should see it
        fk.seed(
            assignee=PIPELINE_ROLES["developer"],
            title=f"#{issue_n} Developer: new attempt",
            status="todo",
            summary="",
        )

        result = disp._has_downstream_tasks(
            SLUG, issue_n,
            validator_profile=PIPELINE_ROLES["validator"],
            pm_profile=PIPELINE_ROLES["pm"],
            planner_profile="planner-daedalus",
        )
        check(
            "_has_downstream_tasks returns True when an active task exists (dispatcher sees it)",
            result is True,
        )


def test_dispatcher_respects_active_downstream_tasks():
    """Dispatcher should NOT create duplicates when active downstream tasks exist."""
    disp = _load_dispatch()
    fk = FakeKanban()

    issue_n = 10082

    # Active downstream task
    fk.seed(
        assignee=PIPELINE_ROLES["developer"],
        title=f"#{issue_n} Developer: running",
        status="running",
        summary="",
    )

    with kanban_as(disp.kanban, fk):
        # _has_downstream_tasks should return True (active task exists)
        result = disp._has_downstream_tasks(
            SLUG, issue_n,
            validator_profile=PIPELINE_ROLES["validator"],
            pm_profile=PIPELINE_ROLES["pm"],
            planner_profile="planner-daedalus",
        )
    check(
        "_has_downstream_tasks returns True when active downstream exists (no duplicate creation)",
        result is True,
    )


def test_dispatcher_mixed_terminal_and_active():
    """Dispatcher correctly handles mix of terminal and active tasks."""
    disp = _load_dispatch()
    fk = FakeKanban()

    issue_n = 10083

    # Mix of terminal and active
    fk.seed(
        assignee=PIPELINE_ROLES["developer"],
        title=f"#{issue_n} Developer: old",
        status="done",
        summary="",
    )
    fk.seed(
        assignee=PIPELINE_ROLES["developer"],
        title=f"#{issue_n} Developer: active",
        status="running",
        summary="",
    )
    fk.seed(
        assignee=PIPELINE_ROLES["reviewer"],
        title=f"#{issue_n} Reviewer: archived",
        status="archived",
        summary="",
    )

    with kanban_as(disp.kanban, fk):
        result = disp._has_downstream_tasks(
            SLUG, issue_n,
            validator_profile=PIPELINE_ROLES["validator"],
            pm_profile=PIPELINE_ROLES["pm"],
            planner_profile="planner-daedalus",
        )
    check(
        "_has_downstream_tasks returns True with mixed terminal/active (active task detected)",
        result is True,
    )


if __name__ == "__main__":
    test_dispatcher_allows_new_dispatch_when_downstream_terminal()
    test_dispatcher_respects_active_downstream_tasks()
    test_dispatcher_mixed_terminal_and_active()
