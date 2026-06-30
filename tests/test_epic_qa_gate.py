"""Tests for epic QA dispatch gating (issue #1098).

Covers:
  - _extract_sub_issue_numbers: parse sub-issue refs from epic body
  - _check_epic_qa_ready: non-epic (gate off), epic+no-PR (skip),
    epic+one-PR (dispatch), epic with no sub-issues (fail open),
    issue=None (fail open)
  - _gate_epic_qa_tasks: defers ready QA cards for epics with no PRs
  - _maybe_undefer_epic_qa_tasks: unblocks deferred QA cards when PR appears
  - classify_blocked: qa-deferred signal routes to PENDING_SIGNAL (not QA_FIX)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import _load_dispatch, check  # noqa: E402,F401

disp = _load_dispatch()

from conftest import FakeKanban  # noqa: E402


# ── _extract_sub_issue_numbers ──────────────────────────────────────────────


def test_extract_sub_issue_numbers_basic():
    """Sub-issue checklist refs are extracted from an epic body."""
    body = """## Sub-issues
- [ ] #1082
- [ ] #1083
- [x] [#1084](https://github.com/benmarte/daedalus/issues/1084)
- [ ] #1085
"""
    nums = disp._extract_sub_issue_numbers(body)
    check("extracts 4 sub-issue numbers", nums == [1082, 1083, 1084, 1085])


def test_extract_sub_issue_numbers_empty():
    """Empty body returns empty list."""
    check("empty body", disp._extract_sub_issue_numbers("") == [])
    check("none body", disp._extract_sub_issue_numbers(None) == [])


def test_extract_sub_issue_numbers_dedup():
    """Duplicate sub-issue refs are deduplicated."""
    body = """- [ ] #100
- [ ] #100
- [x] #101
"""
    nums = disp._extract_sub_issue_numbers(body)
    check("deduped", nums == [100, 101])


def test_extract_sub_issue_numbers_no_checklist():
    """Body without checklist items returns empty."""
    body = "This is a regular issue body with #100 reference inline."
    nums = disp._extract_sub_issue_numbers(body)
    check("no checklist -> empty", nums == [])


# ── _check_epic_qa_ready ────────────────────────────────────────────────────


def test_check_epic_qa_ready_non_epic():
    """Non-epic issue always returns True (gate is no-op)."""
    fk = FakeKanban()
    issue = {"number": 100, "title": "Bug fix", "body": "Small bug.", "labels": []}
    result = disp._check_epic_qa_ready("slug", 100, issue, fk)
    check("non-epic returns True", result is True)


def test_check_epic_qa_ready_epic_no_sub_issue_pr():
    """Epic with sub-issues but no developer review-required -> False."""
    fk = FakeKanban()
    # Epic issue body with sub-issue checklist
    epic_body = """## Sub-issues
- [ ] #200
- [ ] #201
- [ ] #202
- [ ] #203
"""
    epic_issue = {
        "number": 100,
        "title": "Epic: refactor everything",
        "body": epic_body,
        "labels": [{"name": "epic"}],
    }
    # Sub-issue developer cards exist but no review-required signal
    fk.seed(
        assignee="developer-daedalus",
        title="#200 Developer: sub-task A",
        status="running",
        summary="",
    )
    fk.seed(
        assignee="developer-daedalus",
        title="#201 Developer: sub-task B",
        status="running",
        summary="",
    )
    result = disp._check_epic_qa_ready("slug", 100, epic_issue, fk)
    check("epic no PR -> False", result is False)


def test_check_epic_qa_ready_epic_with_sub_issue_pr():
    """Epic with at least one sub-issue developer having review-required -> True."""
    fk = FakeKanban()
    epic_body = """## Sub-issues
- [ ] #200
- [ ] #201
- [ ] #202
- [ ] #203
"""
    epic_issue = {
        "number": 100,
        "title": "Epic: refactor everything",
        "body": epic_body,
        "labels": [{"name": "epic"}],
    }
    # Sub-issue 200 developer has a PR
    fk.seed(
        assignee="developer-daedalus",
        title="#200 Developer: sub-task A",
        status="blocked",
        summary="review-required: PR #42 — fix/issue-200-slug",
    )
    # Sub-issue 201 developer still running
    fk.seed(
        assignee="developer-daedalus",
        title="#201 Developer: sub-task B",
        status="running",
        summary="",
    )
    result = disp._check_epic_qa_ready("slug", 100, epic_issue, fk)
    check("epic with PR -> True", result is True)


def test_check_epic_qa_ready_issue_none():
    """None issue returns True (fail open)."""
    fk = FakeKanban()
    result = disp._check_epic_qa_ready("slug", 100, None, fk)
    check("None issue -> True (fail open)", result is True)


def test_check_epic_qa_ready_epic_no_sub_issues_in_body():
    """Epic with no parseable sub-issue numbers returns True (fail open)."""
    fk = FakeKanban()
    # Epic label but body has no sub-issue checklist
    epic_issue = {
        "number": 100,
        "title": "Epic with no sub-issues listed",
        "body": "Some long body with decomposition language but no checklist refs.",
        "labels": [{"name": "epic"}],
    }
    result = disp._check_epic_qa_ready("slug", 100, epic_issue, fk)
    check("epic no sub-issues in body -> True (fail open)", result is True)


def test_check_epic_qa_ready_kanban_error_fails_open():
    """Kanban list_tasks raising -> fail open (True)."""
    class BrokenKanban:
        def list_tasks(self, slug, status=None):
            raise RuntimeError("kanban error")
    epic_body = """- [ ] #200
- [ ] #201
- [ ] #202
- [ ] #203
"""
    epic_issue = {
        "number": 100,
        "title": "Epic",
        "body": epic_body,
        "labels": [{"name": "epic"}],
    }
    result = disp._check_epic_qa_ready("slug", 100, epic_issue, BrokenKanban())
    check("kanban error -> True (fail open)", result is True)


def test_check_epic_qa_ready_review_required_without_pr_number():
    """review-required without 'PR #' should not count as having a PR."""
    fk = FakeKanban()
    epic_body = """- [ ] #200
- [ ] #201
- [ ] #202
- [ ] #203
"""
    epic_issue = {
        "number": 100,
        "title": "Epic",
        "body": epic_body,
        "labels": [{"name": "epic"}],
    }
    fk.seed(
        assignee="developer-daedalus",
        title="#200 Developer: sub-task A",
        status="running",
        summary="review-required: branch pushed but no PR yet",
    )
    result = disp._check_epic_qa_ready("slug", 100, epic_issue, fk)
    check("review-required without PR # -> False", result is False)


# ── _gate_epic_qa_tasks ─────────────────────────────────────────────────────


def test_gate_epic_qa_tasks_defers_qa_for_epic_no_pr():
    """QA card for epic with no sub-issue PR is blocked (deferred)."""
    fk = FakeKanban()
    epic_body = """- [ ] #200
- [ ] #201
- [ ] #202
- [ ] #203
"""
    epic_issue = {
        "number": 100,
        "title": "Epic",
        "body": epic_body,
        "labels": [{"name": "epic"}],
    }
    issues_map = {100: epic_issue}

    # QA card ready to dispatch
    qa_tid = fk.seed(
        assignee="qa-daedalus",
        title="#100 QA: Epic",
        status="ready",
    )
    # Developer card for sub-issue 200 (running, no PR)
    fk.seed(
        assignee="developer-daedalus",
        title="#200 Developer: sub-task A",
        status="running",
    )

    deferred = disp._gate_epic_qa_tasks("slug", issues_map, fk)
    check("1 QA card deferred", deferred == 1)
    check("QA card is blocked", fk.tasks[qa_tid]["status"] == "blocked")
    check(
        "QA card reason has qa-deferred",
        "qa-deferred" in fk.tasks[qa_tid].get("latest_summary", "").lower(),
    )


def test_gate_epic_qa_tasks_does_not_defer_when_pr_exists():
    """QA card for epic with sub-issue PR is NOT blocked."""
    fk = FakeKanban()
    epic_body = """- [ ] #200
- [ ] #201
- [ ] #202
- [ ] #203
"""
    epic_issue = {
        "number": 100,
        "title": "Epic",
        "body": epic_body,
        "labels": [{"name": "epic"}],
    }
    issues_map = {100: epic_issue}

    qa_tid = fk.seed(
        assignee="qa-daedalus",
        title="#100 QA: Epic",
        status="ready",
    )
    # Sub-issue developer with PR
    fk.seed(
        assignee="developer-daedalus",
        title="#200 Developer: sub-task A",
        status="blocked",
        summary="review-required: PR #42 — fix/issue-200-slug",
    )

    deferred = disp._gate_epic_qa_tasks("slug", issues_map, fk)
    check("0 QA cards deferred (PR exists)", deferred == 0)
    check("QA card not blocked", fk.tasks[qa_tid]["status"] == "ready")


def test_gate_epic_qa_tasks_non_epic_qa_not_deferred():
    """Non-epic QA card is not deferred."""
    fk = FakeKanban()
    issue = {"number": 50, "title": "Bug", "body": "Small bug.", "labels": []}
    issues_map = {50: issue}

    qa_tid = fk.seed(
        assignee="qa-daedalus",
        title="#50 QA: Bug fix",
        status="ready",
    )
    deferred = disp._gate_epic_qa_tasks("slug", issues_map, fk)
    check("0 deferred for non-epic", deferred == 0)
    check("QA card not blocked", fk.tasks[qa_tid]["status"] == "ready")


def test_gate_epic_qa_tasks_already_deferred_not_re_blocked():
    """A QA card already blocked with qa-deferred is not re-blocked."""
    fk = FakeKanban()
    epic_body = """- [ ] #200
- [ ] #201
- [ ] #202
- [ ] #203
"""
    epic_issue = {
        "number": 100,
        "title": "Epic",
        "body": epic_body,
        "labels": [{"name": "epic"}],
    }
    issues_map = {100: epic_issue}

    # QA card already deferred
    fk.seed(
        assignee="qa-daedalus",
        title="#100 QA: Epic",
        status="blocked",
        summary="qa-deferred: no sub-issue PRs open yet for epic #100",
    )
    deferred = disp._gate_epic_qa_tasks("slug", issues_map, fk)
    check("0 re-deferred (already deferred)", deferred == 0)


def test_gate_epic_qa_tasks_skips_done_qa():
    """Done QA cards are not deferred."""
    fk = FakeKanban()
    epic_body = """- [ ] #200
- [ ] #201
- [ ] #202
- [ ] #203
"""
    epic_issue = {
        "number": 100,
        "title": "Epic",
        "body": epic_body,
        "labels": [{"name": "epic"}],
    }
    issues_map = {100: epic_issue}

    fk.seed(
        assignee="qa-daedalus",
        title="#100 QA: Epic",
        status="done",
    )
    deferred = disp._gate_epic_qa_tasks("slug", issues_map, fk)
    check("done QA not deferred", deferred == 0)


def test_gate_epic_qa_tasks_issue_not_in_map():
    """QA card whose issue is not in issues_map is skipped (fail open)."""
    fk = FakeKanban()
    issues_map = {}  # empty — issue not in window

    qa_tid = fk.seed(
        assignee="qa-daedalus",
        title="#100 QA: Epic",
        status="ready",
    )
    deferred = disp._gate_epic_qa_tasks("slug", issues_map, fk)
    check("issue not in map -> not deferred", deferred == 0)
    check("QA card not blocked", fk.tasks[qa_tid]["status"] == "ready")


# ── _maybe_undefer_epic_qa_tasks ─────────────────────────────────────────────


def test_undefer_epic_qa_when_pr_appears():
    """Deferred QA card is unblocked when a sub-issue PR appears."""
    fk = FakeKanban()
    epic_body = """- [ ] #200
- [ ] #201
- [ ] #202
- [ ] #203
"""
    epic_issue = {
        "number": 100,
        "title": "Epic",
        "body": epic_body,
        "labels": [{"name": "epic"}],
    }
    issues_map = {100: epic_issue}

    # QA card deferred
    qa_tid = fk.seed(
        assignee="qa-daedalus",
        title="#100 QA: Epic",
        status="blocked",
        summary="qa-deferred: no sub-issue PRs open yet for epic #100",
    )
    # Sub-issue developer now has a PR
    fk.seed(
        assignee="developer-daedalus",
        title="#200 Developer: sub-task A",
        status="blocked",
        summary="review-required: PR #99 — fix/issue-200-slug",
    )

    unblocked = disp._maybe_undefer_epic_qa_tasks("slug", issues_map, fk)
    check("1 QA card unblocked", unblocked == 1)
    check("QA card is running again", fk.tasks[qa_tid]["status"] == "running")


def test_undefer_epic_qa_stays_blocked_when_no_pr():
    """Deferred QA card stays blocked when no sub-issue PR exists."""
    fk = FakeKanban()
    epic_body = """- [ ] #200
- [ ] #201
- [ ] #202
- [ ] #203
"""
    epic_issue = {
        "number": 100,
        "title": "Epic",
        "body": epic_body,
        "labels": [{"name": "epic"}],
    }
    issues_map = {100: epic_issue}

    qa_tid = fk.seed(
        assignee="qa-daedalus",
        title="#100 QA: Epic",
        status="blocked",
        summary="qa-deferred: no sub-issue PRs open yet for epic #100",
    )
    # No developer PR
    fk.seed(
        assignee="developer-daedalus",
        title="#200 Developer: sub-task A",
        status="running",
        summary="",
    )

    unblocked = disp._maybe_undefer_epic_qa_tasks("slug", issues_map, fk)
    check("0 unblocked (no PR yet)", unblocked == 0)
    check("QA card still blocked", fk.tasks[qa_tid]["status"] == "blocked")


def test_undefer_does_not_touch_non_deferred_qa():
    """Blocked QA card without qa-deferred sentinel is not touched."""
    fk = FakeKanban()
    epic_body = """- [ ] #200
- [ ] #201
- [ ] #202
- [ ] #203
"""
    epic_issue = {
        "number": 100,
        "title": "Epic",
        "body": epic_body,
        "labels": [{"name": "epic"}],
    }
    issues_map = {100: epic_issue}

    qa_tid = fk.seed(
        assignee="qa-daedalus",
        title="#100 QA: Epic",
        status="blocked",
        summary="qa-failed: tests broken",
    )

    unblocked = disp._maybe_undefer_epic_qa_tasks("slug", issues_map, fk)
    check("0 unblocked (not qa-deferred)", unblocked == 0)
    check("QA card still blocked", fk.tasks[qa_tid]["status"] == "blocked")


# ── classify_blocked qa-deferred signal ─────────────────────────────────────


def test_classify_blocked_qa_deferred_routes_to_pending_signal():
    """classify_blocked routes qa-deferred to PENDING_SIGNAL, not QA_FIX."""
    from core import iterate

    result = iterate.classify_blocked(
        "qa-daedalus",
        "qa-deferred: no sub-issue PRs found for epic #100",
        ci_green=True,
    )
    check(
        "qa-deferred -> PENDING_SIGNAL (not QA_FIX)",
        result == iterate.PENDING_SIGNAL,
    )


def test_classify_blocked_qa_failed_still_routes_to_qa_fix():
    """qa-failed still routes to QA_FIX (unchanged behavior)."""
    from core import iterate

    result = iterate.classify_blocked(
        "qa-daedalus",
        "qa-failed: tests are broken",
        ci_green=True,
    )
    check("qa-failed -> QA_FIX", result == iterate.QA_FIX)


def test_classify_blocked_qa_passed_still_routes_to_advance():
    """qa-passed still routes to ADVANCE (unchanged behavior)."""
    from core import iterate

    result = iterate.classify_blocked(
        "qa-daedalus",
        "qa-passed: PR #42 — suite green",
        ci_green=True,
    )
    check("qa-passed -> ADVANCE", result == iterate.ADVANCE)


# ── Integration: gate + undefer cycle ────────────────────────────────────────


def test_gate_then_undefer_full_cycle():
    """Full cycle: defer -> PR appears -> undefer."""
    fk = FakeKanban()
    epic_body = """## Sub-issues
- [ ] #200
- [ ] #201
- [ ] #202
- [ ] #203
"""
    epic_issue = {
        "number": 100,
        "title": "Epic: big refactor",
        "body": epic_body,
        "labels": [{"name": "epic"}],
    }
    issues_map = {100: epic_issue}

    # Step 1: QA card ready, no dev PRs -> should be deferred
    qa_tid = fk.seed(
        assignee="qa-daedalus",
        title="#100 QA: big refactor",
        status="ready",
    )
    fk.seed(
        assignee="developer-daedalus",
        title="#200 Developer: sub-task A",
        status="running",
        summary="",
    )

    deferred = disp._gate_epic_qa_tasks("slug", issues_map, fk)
    check("step 1: deferred", deferred == 1)
    check("step 1: QA blocked", fk.tasks[qa_tid]["status"] == "blocked")

    # Step 2: Developer opens PR -> undefer should unblock QA
    fk.seed(
        assignee="developer-daedalus",
        title="#200 Developer: sub-task A",
        status="blocked",
        summary="review-required: PR #55 — fix/issue-200-slug",
        tid="t_dev200",
    )
    # The old running card is still there — we need to update it or add a new one.
    # In the real pipeline, the same card transitions from running to blocked.
    # Simulate: update the existing dev card's summary.
    fk.tasks["t2"]["status"] = "blocked"
    fk.tasks["t2"]["latest_summary"] = "review-required: PR #55 — fix/issue-200-slug"

    unblocked = disp._maybe_undefer_epic_qa_tasks("slug", issues_map, fk)
    check("step 2: unblocked", unblocked == 1)
    check("step 2: QA running", fk.tasks[qa_tid]["status"] == "running")

    # Step 3: Gate re-evaluates — PR exists, QA should NOT be re-deferred
    deferred = disp._gate_epic_qa_tasks("slug", issues_map, fk)
    check("step 3: not re-deferred", deferred == 0)
    check("step 3: QA still running", fk.tasks[qa_tid]["status"] == "running")


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))