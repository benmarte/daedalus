#!/usr/bin/env python3
"""Integration regression tests: manual issue → Ready → dispatch (issue #930).

Epic #915 added auto-advance of sub-issues to Ready after planner
decomposition. The existing ``test_manual_ready_regression.py`` suite guards
the *promotion* layer (``core.tier_promotion`` + a board-simulating stub), but
nothing drove the **real dispatcher** ``daedalus_dispatch.run()`` against an
issue a human moved to Ready by hand.

That dispatch gate (``daedalus_dispatch.py``) is exactly where a regression
from the auto-advance feature would surface: the loop reads board state
(``board_numbers_with_statuses``) and is agnostic to *how* an issue reached
Ready — manual board move and tier auto-promotion converge at the same gate.
These tests pin that invariant end-to-end:

  1. A manually-Ready standalone (non-epic, no-deps) issue IS dispatched.
  2. A manually-Ready issue with an OPEN blocker is held back — manual Ready
     does NOT bypass dependency-aware gating (#139).
  3. A manually-Ready issue whose blockers are all closed IS dispatched.
  4. An issue NOT in Ready (control) is never dispatched — proves the gate,
     not the harness, is what lets #1–#3 through.

Each test runs the live ``run()`` loop over a board-configured provider double,
mirroring the harness in ``test_daedalus.py::test_priority_sort_*``.

Run: python3 tests/test_manual_ready_dispatch_integration.py
"""

from __future__ import annotations

import re
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

import pytest

# Make the package root importable (config/, core/, scripts/) and the tests dir.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import FakeProvider, _load_dispatch, check  # noqa: E402,F401
from core.providers.base import IssueSummary  # noqa: E402


# ── Board-configured provider double ──────────────────────────────────────────


class _BoardProvider(FakeProvider):
    """Provider double exercising the dispatcher's Ready-gating path.

    Models a GitHub-Projects board: ``ready`` is the set of issue numbers a
    human (or auto-advance) moved to Ready, ``open_blockers`` maps an issue to
    its still-open dependency numbers. Standalone vs. sub-issue is irrelevant to
    the dispatch gate — it reads board status only — so both are expressed
    purely as membership in ``ready``.
    """

    def __init__(self, *, ready, issues, open_blockers=None, prs=None):
        super().__init__()
        self._ready = set(ready)
        self._issue_list = list(issues)
        self._open_blockers = dict(open_blockers or {})
        self._prs = dict(prs or {})

    def board_configured(self):
        return True

    def status_name(self, key):
        return {
            "ready": "Ready",
            "in_progress": "In progress",
            "in_review": "In review",
            "done": "Done",
        }.get(key, key)

    def board_numbers_with_statuses(self, names):
        # Ready candidates feed the gate; the post-loop Done-sync query passes
        # ["Done"], for which nothing is Done in these fixtures.
        if "Done" in names:
            return set()
        return set(self._ready)

    def board_set_status(self, n, status):
        return True

    def list_issues(self, state="open", labels=None, limit=50):
        return list(self._issue_list)

    def blockers(self, issue_number):
        return list(self._open_blockers.get(issue_number, []))

    def pr_state_for_issue(self, n):
        return self._prs.get(n)


# ── Harness ────────────────────────────────────────────────────────────────────


def _run_dispatch(provider, *, max_dispatch=5):
    """Drive the real ``run()`` loop and return the dispatched issue numbers.

    Patches the kanban surface to in-memory no-ops and captures the issue
    number from each created card's ``#N``-prefixed title (every dispatch path
    — validator and planner — titles its first card ``#N <issue title>``).
    """
    disp = _load_dispatch()
    dispatched: list[int] = []

    def fake_create(
        slug,
        title,
        body="",
        *,
        assignee="",
        idempotency_key="",
        workspace="",
        max_retries=None,
        skills=None,
        goal=False,
        goal_max_turns=None,
        parents=None,
    ):
        m = re.search(r"#(\d+)", title)
        if m:
            dispatched.append(int(m.group(1)))
        return "t_x"

    # ``disp.kanban`` is the shared ``core.kanban`` module, so patch via
    # ``mock.patch.object`` context managers — they auto-restore on exit and keep
    # this harness from leaking stubs into sibling suites (e.g. test_daedalus.py).
    patches = {
        "ensure_board": lambda s: None,
        "list_blocked": lambda s: [],
        "list_issue_numbers": lambda s: set(),
        "list_tasks": lambda *a, **k: [],
        "dispatch": lambda s, max_spawns=5: True,
        "create_triage": lambda *a, **k: "t_triage",
        "decompose": lambda *a, **k: True,
        "create_task": fake_create,
    }
    with ExitStack() as stack:
        for attr, stub in patches.items():
            stack.enter_context(mock.patch.object(disp.kanban, attr, stub))
        disp.run(
            {
                "repo": "O/R",
                "workdir": "/tmp",
                "name": "x",
                "issues": {"filters": {}},
                "execution": {},
                "tracking": {"github_project_number": 1},
            },
            provider=provider,
            max_dispatch=max_dispatch,
        )

    return dispatched


# ── Tests ──────────────────────────────────────────────────────────────────────


def test_manual_ready_standalone_issue_is_dispatched():
    """A standalone issue a human moved to Ready IS dispatched.

    Regression guard: auto-advance must not filter out manually-Ready issues
    that never went through tier promotion (test plan Scenario 1).
    """
    provider = _BoardProvider(
        ready={42},
        issues=[IssueSummary(number=42, title="standalone manual ready", labels=[])],
    )
    dispatched = _run_dispatch(provider)
    check("manually-Ready standalone issue is dispatched", dispatched == [42])


def test_manual_ready_issue_with_open_blocker_is_held():
    """A manually-Ready issue with an OPEN blocker is NOT dispatched.

    Manual Ready must not bypass dependency-aware gating (#139): the gate
    re-checks blockers every tick regardless of how the issue reached Ready
    (test plan Scenario 3).
    """
    provider = _BoardProvider(
        ready={50},
        issues=[IssueSummary(number=50, title="manual ready, dep open", labels=[])],
        open_blockers={50: [49]},
    )
    dispatched = _run_dispatch(provider)
    check("manually-Ready issue with open blocker is held back", dispatched == [])


def test_manual_ready_issue_with_closed_blockers_is_dispatched():
    """A manually-Ready issue whose blockers are all closed IS dispatched.

    Empty ``blockers()`` (deps resolved) means the dependency gate is clear, so
    the manually-Ready issue proceeds — same outcome as an auto-promoted one.
    """
    provider = _BoardProvider(
        ready={60},
        issues=[IssueSummary(number=60, title="manual ready, deps closed", labels=[])],
        open_blockers={60: []},
    )
    dispatched = _run_dispatch(provider)
    check("manually-Ready issue with closed blockers is dispatched", dispatched == [60])


def test_manual_ready_issue_with_existing_pr_is_not_redispatched():
    """A manually-Ready issue that already has an open PR is NOT re-dispatched.

    The Ready gate skips issues with existing PR work to avoid duplicate
    workers — manual Ready does not override that guard.
    """
    provider = _BoardProvider(
        ready={70},
        issues=[IssueSummary(number=70, title="manual ready, has PR", labels=[])],
        prs={70: "open"},
    )
    dispatched = _run_dispatch(provider)
    check("manually-Ready issue with an open PR is not re-dispatched", dispatched == [])


def test_issue_not_in_ready_is_never_dispatched():
    """Control: an issue NOT moved to Ready is never dispatched.

    Proves the Ready gate (not the harness) is what admits the cases above —
    a Backlog issue with no blockers and no PR still does not dispatch.
    """
    provider = _BoardProvider(
        ready=set(),
        issues=[IssueSummary(number=80, title="still in backlog", labels=[])],
    )
    dispatched = _run_dispatch(provider)
    check("issue not in Ready is never dispatched", dispatched == [])


def test_manual_ready_mixed_batch_dispatches_only_unblocked():
    """A mixed batch: only manually-Ready, unblocked, PR-free issues dispatch.

    End-to-end of the gate across a realistic board — one ready+clear, one
    ready+blocked, one backlog — asserting exactly the clear one goes through.
    """
    provider = _BoardProvider(
        ready={91, 92},  # 93 is backlog
        issues=[
            IssueSummary(number=91, title="ready and clear", labels=[]),
            IssueSummary(number=92, title="ready but blocked", labels=[]),
            IssueSummary(number=93, title="backlog", labels=[]),
        ],
        open_blockers={92: [99]},
    )
    dispatched = _run_dispatch(provider)
    check("only the ready, unblocked, PR-free issue dispatches", dispatched == [91])


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
