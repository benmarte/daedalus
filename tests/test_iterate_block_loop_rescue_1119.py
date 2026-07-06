"""Tests for the block-loop rescue scan (issue #1119).

When ``kanban.complete()`` fails transiently on a gate card blocked with a
passing verdict (``qa-passed:`` / ``review-approved:`` / ``security-approved:``),
the Hermes framework's loop detection fires ``block_loop_detected`` and
auto-resolves by re-promoting the task back to running. The card leaves the
blocked column, so the main ``run_iterate`` blocked scan never sees it and the
whole gate re-runs forever.

The rescue scan finds active gate-profile tasks whose most recent
``block_loop_detected`` event carries a passing verdict and completes them
instead of letting the framework re-promote.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import conftest  # noqa: E402
from conftest import check  # noqa: E402
from core import iterate  # noqa: E402
from core import kanban  # noqa: E402


def _qa_task(tid="t_qa", status="running", assignee="qa-daedalus"):
    return {"id": tid, "status": status, "assignee": assignee,
            "title": "QA #1115: verify dispatcher scan fix"}


def _detail(tid="t_qa", assignee="qa-daedalus", reason="qa-passed: PR #1117 verified",
            events=None, latest_summary=""):
    if events is None:
        events = [
            {"kind": "blocked", "payload": {"reason": reason}},
            {"kind": "block_loop_detected",
             "payload": {"reason": reason, "kind": "needs_input", "recurrences": 7, "limit": 2}},
            {"kind": "specified", "payload": None},
            {"kind": "promoted", "payload": None},
        ]
    return {
        "task": {"id": tid, "assignee": assignee, "status": "running",
                 "body": "QA verify for issue benmarte/daedalus#1115"},
        "latest_summary": latest_summary or reason,
        "events": events,
        "runs": [],
        "comments": [],
    }


# ── _latest_block_loop_reason ────────────────────────────────────────────────


def test_latest_block_loop_reason_dict_payload():
    d = _detail(reason="qa-passed: PR #1117 verified")
    check("dict payload reason extracted",
          iterate._latest_block_loop_reason(d) == "qa-passed: PR #1117 verified")


def test_latest_block_loop_reason_string_payload():
    d = _detail(events=[
        {"kind": "block_loop_detected",
         "payload": '{"reason": "qa-passed: PR #9 ok", "kind": "needs_input"}'},
    ])
    check("string payload reason extracted",
          iterate._latest_block_loop_reason(d) == "qa-passed: PR #9 ok")


def test_latest_block_loop_reason_none_without_event():
    d = _detail(events=[{"kind": "blocked", "payload": {"reason": "qa-passed: PR #9"}}])
    check("no block_loop event → None",
          iterate._latest_block_loop_reason(d) is None)


def test_latest_block_loop_reason_most_recent_wins():
    d = _detail(events=[
        {"kind": "block_loop_detected", "payload": {"reason": "qa-passed: PR #9 ok"}},
        {"kind": "promoted", "payload": None},
        {"kind": "block_loop_detected", "payload": {"reason": "qa-failed: tests broke"}},
    ])
    check("latest block_loop event wins",
          iterate._latest_block_loop_reason(d) == "qa-failed: tests broke")


# ── run_iterate rescue integration ───────────────────────────────────────────


def test_run_iterate_rescues_qa_block_loop():
    """Acceptance #2: QA card with qa-passed summary in block_loop_detected
    state is completed (not re-promoted) on the next dispatch tick — even when
    the blocked column is empty (the framework already re-promoted it)."""
    with mock.patch.object(kanban, "list_blocked", return_value=[]), \
         mock.patch.object(kanban, "list_tasks", return_value=[_qa_task()]), \
         mock.patch.object(kanban, "show_card", return_value=_detail()), \
         mock.patch.object(kanban, "complete", return_value=True) as mk_complete, \
         mock.patch.object(iterate, "_create_downstream_review_tasks") as mk_down:
        counts, prs, *_ = iterate.run_iterate("slug", "O/R")
    check("qa rescue → complete called for t_qa",
          any(c.args[:2] == ("slug", "t_qa") for c in mk_complete.call_args_list))
    check("qa rescue → counted as advance", counts[iterate.ADVANCE] == 1)
    check("qa rescue → PR 1117 in advance_prs", prs == [1117])
    check("qa rescue → downstream tasks for issue 1115",
          mk_down.call_count == 1 and mk_down.call_args.args[1] == 1115)


def test_run_iterate_rescues_reviewer_and_security():
    tasks = [
        _qa_task(tid="t_rev", assignee="reviewer-daedalus"),
        _qa_task(tid="t_sec", assignee="security-analyst-daedalus"),
    ]
    details = {
        "t_rev": _detail(tid="t_rev", assignee="reviewer-daedalus",
                         reason="review-approved: PR #12 LGTM"),
        "t_sec": _detail(tid="t_sec", assignee="security-analyst-daedalus",
                         reason="security-approved: PR #12 no findings"),
    }
    with mock.patch.object(kanban, "list_blocked", return_value=[]), \
         mock.patch.object(kanban, "list_tasks", return_value=tasks), \
         mock.patch.object(kanban, "show_card", side_effect=lambda s, tid: details[tid]), \
         mock.patch.object(kanban, "complete", return_value=True) as mk_complete:
        counts, prs, *_ = iterate.run_iterate("slug", "O/R")
    completed = {c.args[1] for c in mk_complete.call_args_list}
    check("reviewer + security rescued", completed == {"t_rev", "t_sec"})
    check("counted as approve_advance", counts[iterate.APPROVE_ADVANCE] == 2)
    check("gate rescues don't feed advance_prs", prs == [])


def test_run_iterate_rescue_skips_non_pass_reason():
    """A block loop whose reason is not a passing verdict is left alone —
    that's a genuine needs-input loop, not a lost terminal state."""
    d = _detail(reason="needs GITHUB_TOKEN to run the suite")
    with mock.patch.object(kanban, "list_blocked", return_value=[]), \
         mock.patch.object(kanban, "list_tasks", return_value=[_qa_task()]), \
         mock.patch.object(kanban, "show_card", return_value=d), \
         mock.patch.object(kanban, "complete", return_value=True) as mk_complete:
        counts, *_ = iterate.run_iterate("slug", "O/R")
    check("non-pass loop reason → not completed", mk_complete.call_count == 0)
    check("non-pass loop reason → no advance", counts[iterate.ADVANCE] == 0)


def test_run_iterate_rescue_skips_without_block_loop_event():
    """An active QA card that never hit a block loop is not touched — the
    agent may still be running; the blocked-card path owns the normal flow."""
    d = _detail(events=[{"kind": "claimed", "payload": None}],
                latest_summary="qa-passed: PR #7 verified")
    with mock.patch.object(kanban, "list_blocked", return_value=[]), \
         mock.patch.object(kanban, "list_tasks", return_value=[_qa_task()]), \
         mock.patch.object(kanban, "show_card", return_value=d), \
         mock.patch.object(kanban, "complete", return_value=True) as mk_complete:
        iterate.run_iterate("slug", "O/R")
    check("no block_loop event → not completed", mk_complete.call_count == 0)


def test_run_iterate_rescue_skips_terminal_and_blocked_cards():
    """Done/archived tasks are skipped; a card still in the blocked column is
    owned by the main blocked scan (no double handling)."""
    blocked_card = {"id": "t_qa", "assignee": "qa-daedalus",
                    "runs": [{"reason": "qa-passed: PR #1117 verified"}],
                    "body": "QA verify for issue benmarte/daedalus#1115"}
    tasks = [
        _qa_task(tid="t_qa", status="blocked"),
        _qa_task(tid="t_done", status="done"),
        _qa_task(tid="t_arch", status="archived"),
    ]
    with mock.patch.object(kanban, "list_blocked", return_value=[blocked_card]), \
         mock.patch.object(kanban, "list_tasks", return_value=tasks), \
         mock.patch.object(kanban, "show_card", return_value=_detail()), \
         mock.patch.object(kanban, "complete", return_value=True) as mk_complete, \
         mock.patch.object(iterate, "_create_downstream_review_tasks"):
        counts, *_ = iterate.run_iterate("slug", "O/R")
    completed = [c.args[1] for c in mk_complete.call_args_list]
    check("blocked card completed exactly once (main scan only)",
          completed.count("t_qa") == 1)
    check("terminal tasks untouched",
          "t_done" not in completed and "t_arch" not in completed)
    check("single advance counted", counts[iterate.ADVANCE] == 1)


def test_run_iterate_rescue_complete_failure_retries_next_tick():
    """If complete() still fails, the rescue counts nothing — the card stays
    active and the next tick retries (graceful degradation, no crash)."""
    with mock.patch.object(kanban, "list_blocked", return_value=[]), \
         mock.patch.object(kanban, "list_tasks", return_value=[_qa_task()]), \
         mock.patch.object(kanban, "show_card", return_value=_detail()), \
         mock.patch.object(kanban, "complete", return_value=False):
        counts, prs, *_ = iterate.run_iterate("slug", "O/R")
    check("failed complete → no advance count", counts[iterate.ADVANCE] == 0)
    check("failed complete → no advance PRs", prs == [])


def test_run_iterate_rescue_dry_run():
    with mock.patch.object(kanban, "list_blocked", return_value=[]), \
         mock.patch.object(kanban, "list_tasks", return_value=[_qa_task()]), \
         mock.patch.object(kanban, "show_card", return_value=_detail()), \
         mock.patch.object(kanban, "complete", return_value=True) as mk_complete:
        counts, *_ = iterate.run_iterate("slug", "O/R", dry_run=True)
    check("dry-run → complete not called", mk_complete.call_count == 0)
    check("dry-run → advance still counted", counts[iterate.ADVANCE] == 1)


def test_run_iterate_rescue_ignores_non_gate_assignees():
    tasks = [_qa_task(tid="t_dev", assignee="developer-daedalus")]
    with mock.patch.object(kanban, "list_blocked", return_value=[]), \
         mock.patch.object(kanban, "list_tasks", return_value=tasks), \
         mock.patch.object(kanban, "show_card", return_value=_detail(tid="t_dev")) as mk_show, \
         mock.patch.object(kanban, "complete", return_value=True) as mk_complete:
        iterate.run_iterate("slug", "O/R")
    check("non-gate assignee → no show_card lookup", mk_show.call_count == 0)
    check("non-gate assignee → not completed", mk_complete.call_count == 0)


if __name__ == "__main__":
    print("block-loop rescue tests (issue #1119)")
    print("-" * 60)
    for fn in (
        test_latest_block_loop_reason_dict_payload,
        test_latest_block_loop_reason_string_payload,
        test_latest_block_loop_reason_none_without_event,
        test_latest_block_loop_reason_most_recent_wins,
        test_run_iterate_rescues_qa_block_loop,
        test_run_iterate_rescues_reviewer_and_security,
        test_run_iterate_rescue_skips_non_pass_reason,
        test_run_iterate_rescue_skips_without_block_loop_event,
        test_run_iterate_rescue_skips_terminal_and_blocked_cards,
        test_run_iterate_rescue_complete_failure_retries_next_tick,
        test_run_iterate_rescue_dry_run,
        test_run_iterate_rescue_ignores_non_gate_assignees,
    ):
        fn()
    print("-" * 60)
    print(f"Results: {conftest._passed} passed, {conftest._failed} failed")
    sys.exit(1 if conftest._failed else 0)
