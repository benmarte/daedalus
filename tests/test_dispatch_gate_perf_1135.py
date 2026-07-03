"""Regression test for issue #1135 — bounded list_tasks calls in the merge gate.

``_qa_passed_for_issue`` / ``_reviewer_passed_for_issue`` /
``_security_passed_for_issue`` each resolve their gate card via
``_role_cards_for_issue``, which runs ``kanban.list_tasks`` for the active board
and (when the done gate card has already archived) again for the archived list.
``sweep_deferred_merges`` calls all three gates for *every* done documentation
card each tick, so with N pipeline-complete PRs it used to burn up to ``2 + 6N``
``list_tasks`` subprocesses per tick.

The fix fetches the active and archived board lists once at the top of the sweep
and threads them through the gate checks (same pattern the dashboard's
``_kanban_summary`` uses), so the call count is bounded (2) regardless of how
many PRs are being swept.

Run under pytest, or standalone: ``python tests/test_dispatch_gate_perf_1135.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import iterate  # noqa: E402

GREEN = iterate.CIStatus.GREEN

_RESOLVED = {"execution": {"auto_merge": True, "merge_method": "squash"}}


def _provider():
    """Provider double: every branch resolves to a green, un-merged, open PR."""
    p = mock.MagicMock()
    p.supports_ci_status = True
    # Deterministic branch fix/issue-<n> → PR number == issue number.
    p.find_pr_for_branch.side_effect = lambda branch: int(branch.rsplit("-", 1)[-1])
    p.get_pr_ci_status.return_value = GREEN
    p.is_pr_merged.return_value = False
    p.is_issue_open.return_value = True
    p.has_label.return_value = False
    p.merge_pr.return_value = True
    return p


def _board(issue_numbers):
    """Active docs cards + archived (already-completed) gate cards for each issue.

    Done gate cards archive fast, so the realistic shape is: the docs card is
    still active while the QA/reviewer/security verdicts live in the archived
    list. ``sweep_deferred_merges`` must consult both — once, not per PR.
    """
    active = [
        {"id": f"docs_{n}", "assignee": "documentation-daedalus",
         "title": f"#{n} Docs: document the fix", "status": "done"}
        for n in issue_numbers
    ]
    archived = []
    for n in issue_numbers:
        archived += [
            {"id": f"qa_{n}", "assignee": "qa-daedalus",
             "title": f"#{n} QA: verify PR", "status": "done"},
            {"id": f"rev_{n}", "assignee": "reviewer-daedalus",
             "title": f"#{n} Review PR", "status": "done"},
            {"id": f"sec_{n}", "assignee": "security-analyst-daedalus",
             "title": f"#{n} Security review", "status": "done"},
        ]
    return active, archived


def _summary_for(task_id):
    if task_id.startswith("qa_"):
        return {"latest_summary": "qa-passed"}
    if task_id.startswith("rev_"):
        return {"latest_summary": "approved"}
    if task_id.startswith("sec_"):
        return {"latest_summary": "security: cleared"}
    return {}


def _run_sweep(issue_numbers):
    """Drive sweep_deferred_merges, returning (merged_prs, list_tasks_call_count)."""
    active, archived = _board(issue_numbers)
    calls = {"n": 0}

    def fake_list_tasks(slug, status=""):
        calls["n"] += 1
        return list(archived) if status == "archived" else list(active)

    p = _provider()
    with mock.patch.object(iterate.kanban, "list_tasks", side_effect=fake_list_tasks), \
            mock.patch.object(iterate.kanban, "show_card",
                              side_effect=lambda slug, tid: _summary_for(tid)):
        merged = iterate.sweep_deferred_merges("slug", "owner/repo", p, _RESOLVED)
    return merged, calls["n"]


def test_sweep_gate_checks_reuse_one_board_snapshot():
    """5 pipeline-complete PRs still trigger exactly 2 list_tasks calls (active + archived)."""
    merged, list_tasks_calls = _run_sweep([101, 102, 103, 104, 105])
    assert sorted(merged) == [101, 102, 103, 104, 105], merged
    assert list_tasks_calls == 2, (
        f"list_tasks must be called once for active + once for archived regardless "
        f"of PR count, not per gate check per PR: {list_tasks_calls}"
    )


def test_sweep_list_tasks_count_is_independent_of_pr_count():
    """The call count is the same for 1 PR and 8 PRs — it does not scale with N."""
    _, one = _run_sweep([200])
    _, many = _run_sweep([300, 301, 302, 303, 304, 305, 306, 307])
    assert one == many == 2, (one, many)


def test_gate_check_uses_threaded_lists_without_fetching():
    """When active/archived lists are threaded in, gate helpers never call list_tasks."""
    _, archived = _board([42])
    with mock.patch.object(iterate.kanban, "list_tasks",
                           side_effect=AssertionError("list_tasks must not be called")), \
            mock.patch.object(iterate.kanban, "show_card",
                              side_effect=lambda slug, tid: _summary_for(tid)):
        assert iterate._qa_passed_for_issue(
            "slug", 42, active_tasks=[], archived_tasks=archived) is True
        assert iterate._reviewer_passed_for_issue(
            "slug", 42, active_tasks=[], archived_tasks=archived) is True
        assert iterate._security_passed_for_issue(
            "slug", 42, active_tasks=[], archived_tasks=archived) is True


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if failures else 0)
