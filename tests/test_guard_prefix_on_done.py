"""Tests for the F5 mechanical prefix guard (_guard_prefix_on_done, #1125).

Guards that a done card with an unexpected summary (no canonical role prefix)
is archived and replaced with a blocked coding-agent-failed card.

Roles covered: qa-daedalus, reviewer-daedalus, security-analyst-daedalus,
               accessibility-daedalus, documentation-daedalus.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from conftest import FakeKanban, _load_dispatch  # noqa: E402


# ── helpers ───────────────────────────────────────────────────────────────────


def _seed_done(fk: FakeKanban, assignee: str, issue_n: int, summary: str) -> str:
    """Seed a done card for the given assignee and issue number."""
    return fk.seed(
        assignee=assignee,
        title=f"#{issue_n} something",
        status="done",
        summary=summary,
    )


def _run_guard(disp, fk: FakeKanban, **kwargs) -> int:
    """Wire FakeKanban onto disp.kanban and run the guard."""
    with mock.patch.object(disp.kanban, "list_tasks", fk.list_tasks), \
         mock.patch.object(disp.kanban, "show_card", fk.show_card), \
         mock.patch.object(disp.kanban, "archive_task", fk.archive_task), \
         mock.patch.object(disp.kanban, "create_task", fk.create_task), \
         mock.patch.object(disp.kanban, "block_task", fk.block_task):
        return disp._guard_prefix_on_done("board", **kwargs)


# ── happy path: well-formed done cards are left alone ─────────────────────────


@pytest.mark.parametrize("role,summary", [
    ("qa-daedalus",               "qa-passed: PR #42 verified"),
    ("qa-daedalus",               "qa-failed: 3 tests broken"),
    ("qa-daedalus",               "qa-deferred: no sub-issue PRs"),
    ("reviewer-daedalus",         "review-approved: PR #42"),
    ("reviewer-daedalus",         "review-changes-requested: fix null deref"),
    ("security-analyst-daedalus", "security-approved: PR #42"),
    ("security-analyst-daedalus", "security-changes-requested: CVE found"),
    ("security-analyst-daedalus", "security: cleared — no vulnerabilities"),
    ("accessibility-daedalus",    "approved: WCAG 2.1 AA"),
    ("accessibility-daedalus",    "a11y-approved: PR #42"),
    ("accessibility-daedalus",    "accessibility-na: PR #42"),
    ("accessibility-daedalus",    "a11y-skipped: no UI changes"),
    ("accessibility-daedalus",    "changes requested: add aria labels"),
    ("documentation-daedalus",    "docs posted: PR #42 — added readme"),
])
def test_guard_does_not_fire_for_well_formed_done_card(role, summary):
    """Well-formed done cards with recognised prefix are left intact."""
    disp = _load_dispatch()
    fk = FakeKanban()
    _seed_done(fk, role, 42, summary)

    count = _run_guard(disp, fk)

    assert count == 0
    assert len(fk.archived) == 0
    assert len(fk.created) == 0
    assert len(fk.blocked_calls) == 0


# ── anomalous done card triggers archive + recreate ───────────────────────────


@pytest.mark.parametrize("role,bad_summary", [
    ("qa-daedalus",               "all tests pass — checked out"),    # missing qa-passed: prefix
    ("qa-daedalus",               ""),                                 # empty summary
    ("reviewer-daedalus",         "reviewed: approved"),              # legacy template form
    ("reviewer-daedalus",         "lgtm everything looks great"),     # not a canonical prefix
    ("security-analyst-daedalus", "no vulnerabilities found"),        # not a canonical prefix
    ("accessibility-daedalus",    "the PR is approved for a11y"),     # 'approved' mid-string
    ("documentation-daedalus",    "docs: posted completion report"),  # old template form (colon vs space)
    ("documentation-daedalus",    "posted: docs for PR #42"),         # 'docs posted' not at start
])
def test_guard_fires_for_unexpected_done_summary(role, bad_summary):
    """Done card with unexpected summary triggers archive + coding-agent-failed block."""
    disp = _load_dispatch()
    fk = FakeKanban()
    task_id = _seed_done(fk, role, 42, bad_summary)

    count = _run_guard(disp, fk)

    assert count == 1
    # Original card archived.
    assert task_id in fk.archived
    # New card created with correct role assignee.
    assert len(fk.created) == 1
    new_card = fk.created[0]
    assert new_card["assignee"] == role
    # New card blocked with coding-agent-failed: prefix.
    assert len(fk.blocked_calls) == 1
    _new_id, reason = fk.blocked_calls[0]
    assert reason.startswith("coding-agent-failed: unexpected completion summary:"), (
        f"Expected 'coding-agent-failed:' prefix, got: {reason!r}"
    )


# ── idempotency: same card not re-processed next tick ─────────────────────────


def test_guard_idempotent_after_archive():
    """Once a done card is archived it leaves the done list; guard must not re-fire."""
    disp = _load_dispatch()
    fk = FakeKanban()
    _seed_done(fk, "qa-daedalus", 42, "unexpected summary without prefix")

    with mock.patch.object(disp.kanban, "list_tasks", fk.list_tasks), \
         mock.patch.object(disp.kanban, "show_card", fk.show_card), \
         mock.patch.object(disp.kanban, "archive_task", fk.archive_task), \
         mock.patch.object(disp.kanban, "create_task", fk.create_task), \
         mock.patch.object(disp.kanban, "block_task", fk.block_task):
        # First tick — guard fires, card gets archived (status → "archived").
        count1 = disp._guard_prefix_on_done("board")
        # Second tick — card now has status "archived", not "done" in FakeKanban,
        # so list_tasks(status="done") does not return it.
        count2 = disp._guard_prefix_on_done("board")

    assert count1 == 1, "First tick should trigger the guard"
    assert count2 == 0, "Second tick: archived card gone from done list, guard must not re-fire"


# ── idempotency: idempotency_key prevents duplicate blocked card ───────────────


def test_guard_idempotency_key_prevents_duplicate():
    """FakeKanban.create_task is idempotent on key: second call returns existing id."""
    disp = _load_dispatch()
    fk = FakeKanban()
    _seed_done(fk, "qa-daedalus", 99, "bad summary no prefix")

    # Two sequential calls: FakeKanban.archive_task moves card to "archived"
    # so the second guard run sees no done cards → count2 == 0.
    count1 = _run_guard(disp, fk)
    count2 = _run_guard(disp, fk)

    assert count1 + count2 == 1, (
        "Guard must trigger exactly once total across two ticks; "
        f"got count1={count1}, count2={count2}"
    )
    # Exactly one replacement card.
    assert len(fk.created) == 1


# ── dry-run mode: no mutations ────────────────────────────────────────────────


def test_guard_dry_run_does_not_mutate():
    """In dry-run mode the guard reports intent but does NOT archive or create cards."""
    disp = _load_dispatch()
    fk = FakeKanban()
    _seed_done(fk, "documentation-daedalus", 7, "wrong prefix: this is not docs posted")

    count = _run_guard(disp, fk, dry_run=True)

    assert count == 1, "dry-run still increments the trigger count"
    assert len(fk.archived) == 0, "dry-run must not archive"
    assert len(fk.created) == 0, "dry-run must not create cards"
    assert len(fk.blocked_calls) == 0, "dry-run must not block"


# ── roles NOT guarded (validator, pm, developer, planner) are skipped ─────────


@pytest.mark.parametrize("role", [
    "validator-daedalus",
    "project-manager-daedalus",
    "developer-daedalus",
    "planner-daedalus",
])
def test_guard_does_not_fire_for_unguarded_roles(role):
    """Validator/PM/developer/planner have their own _check_completed_* handlers."""
    disp = _load_dispatch()
    fk = FakeKanban()
    # Even with an obviously wrong summary, these roles are not in _DONE_GUARD_PREFIXES.
    _seed_done(fk, role, 42, "completely wrong summary no prefix at all")

    count = _run_guard(disp, fk)

    assert count == 0
    assert len(fk.archived) == 0
    assert len(fk.created) == 0


# ── card with no issue number in title is skipped ─────────────────────────────


def test_guard_skips_card_with_no_issue_number():
    """Done card without '#N' in title is silently skipped."""
    disp = _load_dispatch()
    fk = FakeKanban()
    # Seed a card with no issue number in title.
    fk.seed(
        assignee="qa-daedalus",
        title="some qa task with no issue number",
        status="done",
        summary="missing qa prefix",
    )

    count = _run_guard(disp, fk)

    assert count == 0
    assert len(fk.archived) == 0


# ── F1 false-positive: mid-string 'approved' is not the approve signal ────────


def test_f5_guard_catches_legacy_reviewer_format():
    """'reviewed: approved' (old template) is NOT a canonical prefix → guard fires."""
    disp = _load_dispatch()
    fk = FakeKanban()
    task_id = _seed_done(fk, "reviewer-daedalus", 55, "reviewed: approved — LGTM")

    count = _run_guard(disp, fk)

    assert count == 1
    assert task_id in fk.archived
    assert len(fk.blocked_calls) == 1
    _, reason = fk.blocked_calls[0]
    assert reason.startswith("coding-agent-failed:"), reason


def test_f5_guard_allows_canonical_reviewer_prefix():
    """'review-approved: PR #55' IS canonical → guard does NOT fire."""
    disp = _load_dispatch()
    fk = FakeKanban()
    _seed_done(fk, "reviewer-daedalus", 55, "review-approved: PR #55")

    count = _run_guard(disp, fk)

    assert count == 0
    assert len(fk.archived) == 0
