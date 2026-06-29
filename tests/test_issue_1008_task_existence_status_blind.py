"""Issue #1008 — Universal status-blind task-existence queries.

Verifies that all task-existence queries in daedalus_dispatch ignore tasks in
terminal states (done, complete, completed, cancelled, canceled, archived), as
required by the universal status-blind principle from epic #1008.

The three functions audited here:
  * _has_downstream_tasks       — used by PM triage card dedup
  * _has_active_pm_consultation — pre-flight guard for PM consultation creation
  * _count_active_issue_tasks   — orphan-cleanup safety gate

If any of these returned True/positive count for a terminal-only set, the
dispatcher would incorrectly block re-dispatch or skip orphan cleanup.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import conftest  # noqa: E402
from conftest import (  # noqa: E402
    PIPELINE_ROLES,
    FakeKanban,
    _load_dispatch,
)


SLUG = "proj1008_exist"
TERMINAL_STATES = ("done", "complete", "completed", "cancelled", "canceled", "archived")
ACTIVE_STATES = ("todo", "ready", "running", "blocked", "scheduled")


@pytest.fixture
def disp():
    return _load_dispatch()


# ── _has_downstream_tasks ────────────────────────────────────────────────────


class TestHasDownstreamTasksStatusBlind:
    """Terminal-only issues should be treated as having no downstream work."""

    def test_ignores_each_terminal_state(self, disp):
        for terminal in TERMINAL_STATES:
            fk = FakeKanban()
            disp.kanban = fk
            fk.seed(
                assignee=PIPELINE_ROLES["developer"],
                title=f"#100801 Developer: stale",
                status=terminal,
                summary="prior attempt",
            )
            assert disp._has_downstream_tasks(
                SLUG, 100801,
                validator_profile=PIPELINE_ROLES["validator"],
                pm_profile=PIPELINE_ROLES["pm"],
                planner_profile="planner-daedalus",
            ) is False, f"should ignore downstream task in terminal state {terminal!r}"

    def test_active_downstream_detected(self, disp):
        for active in ACTIVE_STATES:
            fk = FakeKanban()
            disp.kanban = fk
            fk.seed(
                assignee=PIPELINE_ROLES["developer"],
                title=f"#100802 Developer: active",
                status=active,
                summary="",
            )
            assert disp._has_downstream_tasks(
                SLUG, 100802,
                validator_profile=PIPELINE_ROLES["validator"],
                pm_profile=PIPELINE_ROLES["pm"],
                planner_profile="planner-daedalus",
            ) is True, f"should see downstream task in active state {active!r}"

    def test_mix_terminal_and_active(self, disp):
        fk = FakeKanban()
        disp.kanban = fk
        # All terminals
        for i, ts in enumerate(TERMINAL_STATES):
            fk.seed(
                assignee=PIPELINE_ROLES["developer"],
                title=f"#100803 Developer: stale-{i}",
                status=ts,
                summary="",
            )
        # One active
        fk.seed(
            assignee=PIPELINE_ROLES["reviewer"],
            title=f"#100803 Reviewer: still alive",
            status="running",
            summary="",
        )
        assert disp._has_downstream_tasks(
            SLUG, 100803,
            validator_profile=PIPELINE_ROLES["validator"],
            pm_profile=PIPELINE_ROLES["pm"],
            planner_profile="planner-daedalus",
        ) is True

    def test_empty_returns_false(self, disp):
        fk = FakeKanban()
        disp.kanban = fk
        assert disp._has_downstream_tasks(
            SLUG, 100804,
            validator_profile=PIPELINE_ROLES["validator"],
            pm_profile=PIPELINE_ROLES["pm"],
            planner_profile="planner-daedalus",
        ) is False


# ── _has_active_pm_consultation ─────────────────────────────────────────────


class TestHasActivePmConsultationStatusBlind:
    """Terminal consultations must not block new ones (idempotency key is primary guard)."""

    def test_ignores_each_terminal_state(self, disp):
        for terminal in TERMINAL_STATES:
            fk = FakeKanban()
            disp.kanban = fk
            fk.seed(
                assignee=PIPELINE_ROLES["pm"],
                title=f"Consult: #{100810} Some question",
                status=terminal,
                summary="prior consultation finished",
            )
            assert disp._has_active_pm_consultation(
                SLUG, 100810, pm_profile=PIPELINE_ROLES["pm"],
            ) is False, f"should ignore consultation in terminal state {terminal!r}"

    def test_detects_each_active_state(self, disp):
        for active in ACTIVE_STATES:
            fk = FakeKanban()
            disp.kanban = fk
            fk.seed(
                assignee=PIPELINE_ROLES["pm"],
                title=f"Consult: #{100811} Open question",
                status=active,
                summary="",
            )
            assert disp._has_active_pm_consultation(
                SLUG, 100811, pm_profile=PIPELINE_ROLES["pm"],
            ) is True, f"should detect consultation in active state {active!r}"

    def test_mix_terminal_and_active(self, disp):
        fk = FakeKanban()
        disp.kanban = fk
        for i, ts in enumerate(TERMINAL_STATES):
            fk.seed(
                assignee=PIPELINE_ROLES["pm"],
                title=f"Consult: #{100812} Stale consult {i}",
                status=ts,
                summary="",
            )
        fk.seed(
            assignee=PIPELINE_ROLES["pm"],
            title=f"Consult: #{100812} Live consult",
            status="running",
            summary="",
        )
        assert disp._has_active_pm_consultation(
            SLUG, 100812, pm_profile=PIPELINE_ROLES["pm"],
        ) is True

    def test_empty_returns_false(self, disp):
        fk = FakeKanban()
        disp.kanban = fk
        assert disp._has_active_pm_consultation(
            SLUG, 100813, pm_profile=PIPELINE_ROLES["pm"],
        ) is False

    def test_non_consultation_pm_tasks_ignored(self, disp):
        fk = FakeKanban()
        disp.kanban = fk
        fk.seed(
            assignee=PIPELINE_ROLES["pm"],
            title=f"#{100814} Regular PM spec task",
            status="running",
            summary="",
        )
        assert disp._has_active_pm_consultation(
            SLUG, 100814, pm_profile=PIPELINE_ROLES["pm"],
        ) is False

    def test_other_assignee_consults_ignored(self, disp):
        fk = FakeKanban()
        disp.kanban = fk
        fk.seed(
            assignee="some-other-agent",
            title=f"Consult: #{100815} someone else's consult",
            status="running",
            summary="",
        )
        assert disp._has_active_pm_consultation(
            SLUG, 100815, pm_profile=PIPELINE_ROLES["pm"],
        ) is False


# ── _count_active_issue_tasks ────────────────────────────────────────────────


class TestCountActiveIssueTasksStatusBlind:
    """Terminal tasks must not count toward the orphan cleanup safety gate."""

    def test_ignores_each_terminal_state(self, disp):
        for terminal in TERMINAL_STATES:
            fk = FakeKanban()
            disp.kanban = fk
            fk.seed(
                assignee=PIPELINE_ROLES["developer"],
                title=f"#{100820} Developer: done task",
                status=terminal,
                summary="",
            )
            count = disp._count_active_issue_tasks(SLUG, 100820)
            assert count == 0, f"terminal state {terminal!r} should not count active"

    def test_counts_each_active_state(self, disp):
        for active in ACTIVE_STATES:
            fk = FakeKanban()
            disp.kanban = fk
            fk.seed(
                assignee=PIPELINE_ROLES["developer"],
                title=f"#{100821} Developer: active task",
                status=active,
                summary="",
            )
            count = disp._count_active_issue_tasks(SLUG, 100821)
            assert count == 1, f"active state {active!r} should count"

    def test_mixed_terminal_and_active(self, disp):
        fk = FakeKanban()
        disp.kanban = fk
        # 5 terminals
        for i, ts in enumerate(TERMINAL_STATES):
            fk.seed(
                assignee=PIPELINE_ROLES["developer"],
                title=f"#{100822} Developer: terminal-{i}",
                status=ts,
                summary="",
            )
        # 2 active
        fk.seed(
            assignee=PIPELINE_ROLES["reviewer"],
            title=f"#{100822} Reviewer: active-1",
            status="running",
            summary="",
        )
        fk.seed(
            assignee=PIPELINE_ROLES["qa"],
            title=f"#{100822} QA: active-2",
            status="ready",
            summary="",
        )
        count = disp._count_active_issue_tasks(SLUG, 100822)
        assert count == 2, f"expected 2 active; got {count}"

    def test_empty_board_returns_zero(self, disp):
        fk = FakeKanban()
        disp.kanban = fk
        assert disp._count_active_issue_tasks(SLUG, 100823) == 0

    def test_issue_number_isolation(self, disp):
        fk = FakeKanban()
        disp.kanban = fk
        fk.seed(
            assignee=PIPELINE_ROLES["developer"],
            title=f"#999 Developer: unrelated active",
            status="running",
            summary="",
        )
        assert disp._count_active_issue_tasks(SLUG, 100824) == 0
