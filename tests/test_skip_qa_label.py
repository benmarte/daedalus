"""Tests for the `skip-qa` label bypass in the auto-merge monitor.

Issue #1003: PRs with the `skip-qa` label should bypass the qa-passed signal
requirement and proceed directly to auto-merge after the docs card completes.
PRs without the label still require a qa-passed signal before auto-merge.

Two layers of bypass:
  1. classify_blocked — QA card with skip_qa=True returns ADVANCE immediately.
  2. run_iterate auto-merge gate — skip-qa label on PR skips _qa_passed_for_issue check.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import conftest  # noqa: E402,F401
from conftest import FakeProvider, check  # noqa: E402,F401
from core import iterate  # noqa: E402
from core import kanban  # noqa: E402


# ── classify_blocked: QA card with skip_qa bypass ─────────────────────────────


def test_qa_card_skip_qa_bypass_returns_advance():
    """skip_qa=True → ADVANCE even when handoff has no qa-passed signal."""
    result = iterate.classify_blocked(
        "qa-daedalus",
        "running: QA still in progress",
        ci_green=False,
        skip_qa=True,
    )
    check("skip_qa bypass → advance", result == iterate.ADVANCE)


def test_qa_card_skip_qa_bypass_with_empty_handoff():
    """skip_qa=True with empty handoff → ADVANCE (bypass wins)."""
    result = iterate.classify_blocked(
        "qa-daedalus",
        "",
        ci_green=False,
        skip_qa=True,
    )
    check("skip_qa empty handoff → advance", result == iterate.ADVANCE)


def test_qa_card_skip_qa_bypass_with_qa_failed():
    """skip_qa=True even overrides qa-failed signal (label is the stronger signal)."""
    result = iterate.classify_blocked(
        "qa-daedalus",
        "qa-failed: tests broken",
        ci_green=False,
        skip_qa=True,
    )
    check("skip_qa overrides qa-failed → advance", result == iterate.ADVANCE)


def test_qa_card_skip_qa_bypass_with_pending():
    """skip_qa=True even overrides pending (no signal yet) → ADVANCE."""
    result = iterate.classify_blocked(
        "qa-daedalus",
        "some unrelated text",
        ci_green=False,
        skip_qa=True,
    )
    check("skip_qa overrides pending → advance", result == iterate.ADVANCE)


def test_qa_card_no_skip_qa_no_signal_returns_pending_ci():
    """skip_qa=False, no qa-passed signal, no qa-failed → PENDING_CI."""
    result = iterate.classify_blocked(
        "qa-daedalus",
        "running: QA in progress",
        ci_green=False,
        skip_qa=False,
    )
    check("no skip_qa + no signal → pending_ci", result == iterate.PENDING_CI)


def test_qa_card_no_skip_qa_qa_failed_returns_dev_fix_ci():
    """skip_qa=False, qa-failed signal → DEV_FIX_CI."""
    result = iterate.classify_blocked(
        "qa-daedalus",
        "qa-failed: tests broken",
        ci_green=False,
        skip_qa=False,
    )
    check("no skip_qa + qa-failed → dev_fix_ci", result == iterate.DEV_FIX_CI)


def test_qa_card_no_skip_qa_qa_passed_returns_advance():
    """skip_qa=False, qa-passed signal → ADVANCE (baseline, no regression)."""
    result = iterate.classify_blocked(
        "qa-daedalus",
        "qa-passed: all checks green",
        ci_green=False,
        skip_qa=False,
    )
    check("no skip_qa + qa-passed → advance", result == iterate.ADVANCE)


# ── run_iterate auto-merge gate: skip-qa label allows merge ─────────────────


def _make_docs_card(pr: int, issue: int) -> dict:
    """Build a minimal blocked docs card ready for merge."""
    return {
        "id": f"t_docs_{pr}",
        "title": f"Documentation: Issue #{issue}",
        "assignee": "documentation-daedalus",
        "status": "blocked",
        "reason": f"docs posted: PR #{pr}",
        "latest_summary": f"docs posted: PR #{pr}",
        "body": f"Issue benmarte/daedalus#{issue}\nPR #{pr}",
        "idempotency_key": f"docs-{issue}",
    }


@mock.patch("core.iterate._security_passed_for_issue", return_value=True)
@mock.patch("core.iterate._reviewer_passed_for_issue", return_value=True)
@mock.patch("core.iterate._qa_passed_for_issue")
@mock.patch("core.iterate.kanban.list_blocked")
@mock.patch("core.iterate.kanban.show_card")
@mock.patch("core.iterate.kanban.complete", return_value=True)
def test_auto_merge_allowed_with_skip_qa_label_no_qa_passed(
    mock_complete, mock_show_card, mock_list_blocked, mock_qa_passed,
    mock_reviewer_passed, mock_security_passed
):
    """skip-qa label: auto-merge proceeds EVEN WHEN _qa_passed_for_issue returns False."""
    provider = FakeProvider()
    provider._ci = "green"
    provider._open_prs = {42}
    provider.labels[42] = ["skip-qa"]

    docs_card = _make_docs_card(42, 77)
    mock_list_blocked.return_value = [docs_card]
    mock_show_card.return_value = docs_card

    mock_qa_passed.return_value = False  # QA has NOT passed

    counts, advance_prs, _, _qa_f, *_ = iterate.run_iterate(
        "test-board",
        "benmarte/daedalus",
        resolved={"execution": {"auto_merge": True}},
        provider=provider,
    )

    assert len(provider.merged) > 0, "Auto-merge SHOULD be called when skip-qa label present"
    assert any(pr == 42 for pr, _ in provider.merged), "PR #42 should be merged"


@mock.patch("core.iterate._qa_passed_for_issue")
@mock.patch("core.iterate.kanban.list_blocked")
@mock.patch("core.iterate.kanban.show_card")
@mock.patch("core.iterate.kanban.complete", return_value=True)
def test_auto_merge_blocked_without_skip_qa_label_when_qa_not_passed(
    mock_complete, mock_show_card, mock_list_blocked, mock_qa_passed
):
    """Without skip-qa label: auto-merge is BLOCKED when QA has not passed."""
    provider = FakeProvider()
    provider._ci = "green"
    provider._open_prs = {42}
    # No skip-qa label

    docs_card = _make_docs_card(42, 77)
    mock_list_blocked.return_value = [docs_card]
    mock_show_card.return_value = docs_card

    mock_qa_passed.return_value = False  # QA has NOT passed

    counts, advance_prs, _, _qa_f, *_ = iterate.run_iterate(
        "test-board",
        "benmarte/daedalus",
        resolved={"execution": {"auto_merge": True}},
        provider=provider,
    )

    assert len(provider.merged) == 0, "Auto-merge should NOT be called when QA failed and no skip-qa"


@mock.patch("core.iterate._security_passed_for_issue", return_value=True)
@mock.patch("core.iterate._reviewer_passed_for_issue", return_value=True)
@mock.patch("core.iterate._qa_passed_for_issue")
@mock.patch("core.iterate.kanban.list_blocked")
@mock.patch("core.iterate.kanban.show_card")
@mock.patch("core.iterate.kanban.complete", return_value=True)
def test_auto_merge_allowed_with_qa_passed_no_skip_qa_label(
    mock_complete, mock_show_card, mock_list_blocked, mock_qa_passed,
    mock_reviewer_passed, mock_security_passed
):
    """Without skip-qa label but QA passed: auto-merge proceeds (baseline, no regression)."""
    provider = FakeProvider()
    provider._ci = "green"
    provider._open_prs = {42}
    # No skip-qa label

    docs_card = _make_docs_card(42, 77)
    mock_list_blocked.return_value = [docs_card]
    mock_show_card.return_value = docs_card

    mock_qa_passed.return_value = True  # QA HAS passed

    counts, advance_prs, _, _qa_f, *_ = iterate.run_iterate(
        "test-board",
        "benmarte/daedalus",
        resolved={"execution": {"auto_merge": True}},
        provider=provider,
    )

    assert len(provider.merged) > 0, "Auto-merge should be called when QA passed"


# ── run_iterate: skip_qa detection via provider.has_label ─────────────────────


def test_run_iterate_detects_skip_qa_label_via_provider():
    """run_iterate calls provider.has_label(pr, 'skip-qa') to detect the label."""
    provider = FakeProvider()
    provider._ci = "green"
    provider._open_prs = {42}
    provider.labels[42] = ["skip-qa"]

    docs_card = _make_docs_card(42, 77)

    with mock.patch("core.iterate.kanban.list_blocked", return_value=[docs_card]), \
         mock.patch("core.iterate.kanban.show_card", return_value=docs_card), \
         mock.patch("core.iterate.kanban.complete", return_value=True), \
         mock.patch("core.iterate._qa_passed_for_issue", return_value=False), \
         mock.patch("core.iterate._reviewer_passed_for_issue", return_value=True), \
         mock.patch("core.iterate._security_passed_for_issue", return_value=True):

        iterate.run_iterate(
            "test-board",
            "benmarte/daedalus",
            resolved={"execution": {"auto_merge": True}},
            provider=provider,
        )

    # skip-qa label should have prevented merge blocking, so merge_pr was called
    assert any(pr == 42 for pr, _ in provider.merged), "PR #42 should be merged with skip-qa"


def test_run_iterate_no_skip_qa_label_qa_gate_enforced():
    """When skip-qa label is absent and QA not passed, merge is blocked."""
    provider = FakeProvider()
    provider._ci = "green"
    provider._open_prs = {42}
    # No labels — no skip-qa

    docs_card = _make_docs_card(42, 77)

    with mock.patch("core.iterate.kanban.list_blocked", return_value=[docs_card]), \
         mock.patch("core.iterate.kanban.show_card", return_value=docs_card), \
         mock.patch("core.iterate.kanban.complete", return_value=True), \
         mock.patch("core.iterate._qa_passed_for_issue", return_value=False), \
         mock.patch("core.iterate._reviewer_passed_for_issue", return_value=True), \
         mock.patch("core.iterate._security_passed_for_issue", return_value=True):

        iterate.run_iterate(
            "test-board",
            "benmarte/daedalus",
            resolved={"execution": {"auto_merge": True}},
            provider=provider,
        )

    assert len(provider.merged) == 0, "Merge should be blocked without skip-qa and QA not passed"


# ── classify_blocked: skip_qa on non-QA cards has no effect ───────────────────


def test_skip_qa_does_not_affect_developer_card():
    """skip_qa on a developer card should not alter its normal behaviour."""
    result = iterate.classify_blocked(
        "developer-daedalus",
        "review-required: PR #42 shipped",
        ci_green=True,
        pr_is_open=True,
        skip_qa=True,
    )
    check("skip_qa on dev card does not alter advance", result == iterate.ADVANCE)


def test_skip_qa_does_not_affect_reviewer_card():
    """skip_qa on a reviewer card should not alter its normal behaviour."""
    result = iterate.classify_blocked(
        "reviewer-daedalus",
        "review-required: CHANGES REQUESTED",
        ci_green=True,
        skip_qa=True,
    )
    check("skip_qa on reviewer card → pm_route (unaffected)", result == iterate.PM_ROUTE)


# ── Standalone __main__ ───────────────────────────────────────────────────────


if __name__ == "__main__":
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    print(f"Running {len(tests)} skip-qa tests...")
    for t in tests:
        try:
            t()
        except AssertionError as e:
            check(t.__name__, False)
            print(f"    ERROR: {e}")
    print(f"\n{'='*60}")
    print(f"  PASSED: {conftest._passed}")
    print(f"  FAILED: {conftest._failed}")
    sys.exit(1 if conftest._failed else 0)
