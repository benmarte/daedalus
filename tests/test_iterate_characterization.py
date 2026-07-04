"""Characterization ("golden") tests for classify_blocked + executors (#1150).

These lock in the CURRENT observable behavior of ``core.iterate`` so the
upcoming module split (#1154) is provably behavior-preserving. Every
``classify_blocked`` routing branch has a row in the parametrized table below,
and each executor has at least a happy-path and an escalation/negative-path
test.

Style notes (deliberately matching the repo's established patterns):
  * Pure ``classify_blocked`` cases are table-driven via ``pytest.mark.parametrize``.
  * Executor side-effects are exercised by patching ``core.kanban`` methods with
    ``mock.patch.object`` — the same doubles / patch targets used by
    ``tests/test_iterate.py``.
  * These are CHARACTERIZATION tests: they assert what the code does TODAY, not
    what it "should" do. Where behavior is surprising, a comment flags it — the
    assertion still captures the actual current outcome.

Unlike parts of ``test_iterate.py`` these use plain ``assert`` (not the
non-raising ``conftest.check`` helper) so a regression genuinely fails under the
canonical ``pytest`` CI runner.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

# Make the package root importable (config/, core/) and the tests dir (conftest).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import iterate  # noqa: E402
from core import kanban  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# classify_blocked — one row per routing branch (iterate.py ~L182-355)
#
# Columns: card_assignee, handoff_text, kwargs, expected_outcome, note
# ─────────────────────────────────────────────────────────────────────────────

_CLASSIFY_CASES = [
    # ── planner-daedalus ──
    (
        "planner-daedalus",
        "PLANNING COMPLETE — 5 sub-issues identified",
        {},
        iterate.PLANNER_DECOMPOSE,
        "planner emits PLANNING COMPLETE (case-insensitive) → decompose",
    ),
    (
        "planner-daedalus",
        "planning complete: ready to fan out",
        {},
        iterate.PLANNER_DECOMPOSE,
        "planner marker is upper()-matched so lowercase still decomposes",
    ),
    (
        "planner-daedalus",
        "some unexpected planner output",
        {},
        iterate.PM_ROUTE,
        "planner without the marker → PM route",
    ),
    # ── documentation-daedalus (terminal stage) ──
    (
        "documentation-daedalus",
        "docs posted: added README section",
        {},
        iterate.APPROVE_ADVANCE,
        "docs posted → approve/complete (terminal)",
    ),
    (
        "documentation-daedalus",
        "documentation complete: see wiki",
        {},
        iterate.PM_ROUTE,
        "SURPRISE: 'documentation complete' is NOT the pass signal; only "
        "'docs posted' approves — anything else routes to PM (wasted round-trip)",
    ),
    # ── project-manager-daedalus (human gate) ──
    (
        "project-manager-daedalus",
        "awaiting-fix: t_dev123",
        {},
        "",
        "PM waiting on a dev fix is not a real escalation → no-op ''",
    ),
    (
        "project-manager-daedalus",
        "cannot decide owner, stuck",
        {},
        iterate.ESCALATE,
        "PM blocked (not awaiting-fix) → escalate (PM can't consult itself)",
    ),
    # ── developer-daedalus ──
    (
        "developer-daedalus",
        "review-required: PR #42 shipped",
        {"fix_attempts": iterate.MAX_FIX_ATTEMPTS},
        iterate.ESCALATE,
        "dev at/over MAX_FIX_ATTEMPTS → escalate (checked before everything else)",
    ),
    (
        "developer-daedalus",
        "review-required: PR #42 merged by a human",
        {"pr_is_merged": True},
        iterate.RECONCILE_MERGED,
        "#957: review-required + PR + merged-outside-pipeline → reconcile_merged",
    ),
    (
        "developer-daedalus",
        "review-required: PR #42",
        {"pr_is_open": False},
        iterate.PENDING_PR,
        "#953: review-required + PR but provider says NOT open → hold in pending_pr",
    ),
    (
        "developer-daedalus",
        "review-required: PR #42 all green",
        {},
        iterate.ADVANCE,
        "review-required + open PR → advance (CI no longer gates, epic #1074)",
    ),
    (
        "developer-daedalus",
        "review-required: PR #42 CI failing",
        {"ci_green": False},
        iterate.ADVANCE,
        "SURPRISE-per-history: red CI still ADVANCEs (CI gated at merge-time only)",
    ),
    (
        "developer-daedalus",
        "review-required: awaiting-pr, still opening",
        {},
        iterate.PENDING_PR,
        "review-required + awaiting-pr (no PR number yet) → pending_pr",
    ),
    (
        "developer-daedalus",
        "coding-agent-failed: gateway crashed at startup",
        {},
        "",
        "infrastructure crash marker → no-op '' (human must fix env + unblock)",
    ),
    (
        "developer-daedalus",
        "coding_agent_timeout after 3600s",
        {},
        "",
        "another crash marker → no-op ''",
    ),
    (
        "developer-daedalus",
        "some other block reason without a PR",
        {},
        iterate.PM_ROUTE,
        "dev, not review-required, no crash marker → PM route",
    ),
    # ── reviewer-daedalus / security-analyst-daedalus (share a branch) ──
    (
        "reviewer-daedalus",
        "changes requested: fix the null deref",
        {"fix_attempts": iterate.MAX_FIX_ATTEMPTS},
        iterate.ESCALATE,
        "reviewer over MAX_FIX_ATTEMPTS → escalate (checked first)",
    ),
    (
        "reviewer-daedalus",
        "awaiting-fix: t_fix99",
        {},
        "",
        "reviewer already has a fix in flight → no-op '' (avoids duplicate PM-ROUTE)",
    ),
    (
        "reviewer-daedalus",
        "review-changes-requested: needs fixes",
        {},
        iterate.PM_ROUTE,
        "reviewer flagged changes → PM route",
    ),
    (
        "reviewer-daedalus",
        "review-approved: LGTM, no findings",
        {},
        iterate.APPROVE_ADVANCE,
        "reviewer approved → approve_advance",
    ),
    (
        "security-analyst-daedalus",
        "security: cleared — no vulnerabilities",
        {},
        iterate.APPROVE_ADVANCE,
        "#1185: security 'cleared' is a documented approval signal → approve_advance",
    ),
    (
        "security-analyst-daedalus",
        "still analyzing, no verdict yet",
        {},
        "",
        "reviewer/security with no recognizable signal → no-op '' (pending)",
    ),
    # ── qa-daedalus ──
    (
        "qa-daedalus",
        "anything at all",
        {"skip_qa": True},
        iterate.ADVANCE,
        "skip-qa label bypasses the signal requirement → advance",
    ),
    (
        "qa-daedalus",
        "qa-passed: all tests green",
        {},
        iterate.ADVANCE,
        "qa-passed → advance",
    ),
    (
        "qa-daedalus",
        "qa-failed: 3 tests broken",
        {},
        iterate.QA_FIX,
        "qa-failed → qa_fix",
    ),
    (
        "qa-daedalus",
        "still running the suite",
        {},
        iterate.PENDING_SIGNAL,
        "qa with no qa-passed/qa-failed signal → pending_signal",
    ),
    # ── accessibility-daedalus ──
    (
        "accessibility-daedalus",
        "approved — WCAG 2.1 AA compliant",
        {},
        iterate.ADVANCE,
        "a11y approved → advance",
    ),
    (
        "accessibility-daedalus",
        "accessibility-na: no UI changes",
        {},
        iterate.ADVANCE,
        "a11y not-applicable → advance",
    ),
    (
        "accessibility-daedalus",
        "a11y-skipped: backend-only PR",
        {},
        iterate.ADVANCE,
        "a11y skipped → advance",
    ),
    (
        "accessibility-daedalus",
        "changes requested: add aria labels",
        {},
        iterate.PM_ROUTE,
        "a11y changes requested → PM route",
    ),
    (
        "accessibility-daedalus",
        "audit in progress",
        {},
        iterate.PENDING_SIGNAL,
        "a11y with no recognizable signal → pending_signal",
    ),
    # ── validator-daedalus ──
    (
        "validator-daedalus",
        "awaiting-pr (used dev protocol by mistake)",
        {},
        iterate.ESCALATE,
        "a BLOCKED validator is always an error → escalate (validators only complete)",
    ),
    # ── unknown / empty assignee ──
    (
        "some-random-role",
        "whatever",
        {},
        "",
        "unknown assignee → no-op ''",
    ),
    (
        "",
        "review-required: PR #42",
        {},
        "",
        "empty assignee → no-op ''",
    ),
]


@pytest.mark.parametrize(
    "assignee,handoff,kwargs,expected,note",
    _CLASSIFY_CASES,
    ids=[f"{c[0] or 'empty'}:{c[3] or 'noop'}" for c in _CLASSIFY_CASES],
)
def test_classify_blocked_characterization(assignee, handoff, kwargs, expected, note):
    """Lock in the routing outcome for every classify_blocked branch."""
    # ci_green defaults True unless a case overrides it (dev branch ignores it
    # for ADVANCE per epic #1074, but other historical params still accept it).
    call_kwargs = {"ci_green": True}
    call_kwargs.update(kwargs)
    result = iterate.classify_blocked(assignee, handoff, **call_kwargs)
    assert result == expected, f"{note}\n  got {result!r}, expected {expected!r}"


def test_classify_all_documented_outcomes_are_reachable():
    """Guard: the table exercises every documented classify_blocked outcome."""
    produced = {c[3] for c in _CLASSIFY_CASES}
    documented = {
        iterate.ADVANCE,
        iterate.QA_FIX,
        iterate.PENDING_SIGNAL,
        iterate.PENDING_PR,
        iterate.PM_ROUTE,
        iterate.APPROVE_ADVANCE,
        iterate.ESCALATE,
        iterate.PLANNER_DECOMPOSE,
        iterate.RECONCILE_MERGED,
        "",  # explicit no-op
    }
    missing = documented - produced
    assert not missing, f"classify_blocked outcomes not covered by table: {missing}"


# ─────────────────────────────────────────────────────────────────────────────
# Executors — happy path + escalation/negative path
# ─────────────────────────────────────────────────────────────────────────────


# ---- _execute_advance ----

def test_execute_advance_happy_completes_unblocks_and_creates_downstream():
    """Happy path: completes the card, re-engages awaiting-fix cards, fans out."""
    blocked_sibling = {"id": "t_rev", "reason": "awaiting-fix: t_dev", "status": "blocked"}
    with mock.patch.object(kanban, "complete", return_value=True) as mk_complete, \
         mock.patch.object(kanban, "list_blocked", return_value=[blocked_sibling]), \
         mock.patch.object(kanban, "unblock_task", return_value=True) as mk_unblock, \
         mock.patch.object(iterate, "_create_downstream_review_tasks",
                           return_value=["t_qa"]) as mk_down:
        card = {"id": "t_dev", "body": "Issue benmarte/daedalus#42"}
        ok = iterate._execute_advance("slug", card, "O/R", "review-required: PR #42")
    assert ok is True
    mk_complete.assert_called_once_with("slug", "t_dev")
    # The awaiting-fix sibling is re-engaged.
    assert mk_unblock.call_args[0][1] == "t_rev"
    # Downstream review tasks are created for the parsed issue number.
    assert mk_down.call_args[0][1] == 42


def test_execute_advance_no_id_returns_false():
    """Negative path: a card with no id short-circuits to False (no completion)."""
    with mock.patch.object(kanban, "complete") as mk_complete:
        ok = iterate._execute_advance("slug", {}, "O/R", "review-required: PR #42")
    assert ok is False
    mk_complete.assert_not_called()


def test_execute_advance_dry_run_no_mutation():
    """dry_run reports intent without touching the board."""
    with mock.patch.object(kanban, "complete") as mk_complete:
        card = {"id": "t_dev", "body": "#42"}
        ok = iterate._execute_advance("slug", card, "O/R",
                                      "review-required: PR #42", dry_run=True)
    assert ok is True
    mk_complete.assert_not_called()


# ---- _execute_reconcile_merged ----

def test_execute_reconcile_merged_closes_issue_pipeline():
    """Happy path: with an issue number, close every pipeline card for it (#957)."""
    with mock.patch.object(kanban, "close_issue_tasks",
                           return_value=["t_dev", "t_qa"]) as mk_close:
        card = {"id": "t_dev", "body": "Issue benmarte/daedalus#957"}
        ok = iterate._execute_reconcile_merged(
            "slug", card, "O/R", "review-required: PR #954 merged")
    assert ok is True
    pos, kw = mk_close.call_args
    assert pos[1] == 957
    assert "954" in kw["summary"] and "merged" in kw["summary"]


def test_execute_reconcile_merged_no_issue_number_falls_back_to_complete():
    """Negative path: no issue number → just complete this card so it can't loop."""
    with mock.patch.object(kanban, "complete", return_value=True) as mk_complete:
        card = {"id": "t_dev"}  # no body → no issue number
        ok = iterate._execute_reconcile_merged(
            "slug", card, "O/R", "review-required: PR #954 merged")
    assert ok is True
    assert mk_complete.call_args[0][1] == "t_dev"


# ---- _execute_qa_fix ----

def test_execute_qa_fix_happy_creates_dev_fix_card():
    """Happy path: creates a developer fix card keyed by attempt number."""
    with mock.patch.object(kanban, "create_task", return_value="t_fix") as mk_create, \
         mock.patch.object(kanban, "comment", return_value=True):
        card = {"id": "t_qa", "runs": [{"metadata": {"fix_attempts": 0}}], "workspace": "dir:/w"}
        ok = iterate._execute_qa_fix("slug", card, "O/R",
                                     "qa-failed: PR #55 tests broken")
    assert ok is True
    kw = mk_create.call_args[1]
    assert kw["assignee"] == "developer-daedalus"
    assert "attempt-1" in kw["idempotency_key"]


def test_execute_qa_fix_no_pr_returns_false():
    """Negative path: no PR number in handoff → False, no card created."""
    with mock.patch.object(kanban, "create_task") as mk_create:
        card = {"id": "t_qa", "runs": [{"metadata": {"fix_attempts": 0}}]}
        ok = iterate._execute_qa_fix("slug", card, "O/R", "qa-failed: tests broke")
    assert ok is False
    mk_create.assert_not_called()


def test_execute_qa_fix_over_cap_escalates():
    """Escalation path: fix_attempts already at MAX → escalate (comment, no fix card)."""
    with mock.patch.object(kanban, "comment", return_value=True) as mk_comment, \
         mock.patch.object(kanban, "create_task") as mk_create:
        card = {"id": "t_qa", "runs": [{"metadata": {"fix_attempts": iterate.MAX_FIX_ATTEMPTS}}]}
        ok = iterate._execute_qa_fix("slug", card, "O/R",
                                     "qa-failed: PR #55 still broken")
    assert ok is True
    assert "escalate" in mk_comment.call_args[0][2].lower()
    mk_create.assert_not_called()


# ---- _execute_pm_route ----

def test_execute_pm_route_happy_creates_goal_card_and_blocks_reviewer():
    """Happy path: goal-mode PM card, findings carried, reviewer marked awaiting-fix."""
    with mock.patch.object(kanban, "create_task", return_value="t_pm") as mk_create, \
         mock.patch.object(kanban, "comment", return_value=True), \
         mock.patch.object(kanban, "block_task", return_value=True) as mk_block, \
         mock.patch.object(kanban, "show_card",
                           return_value={"task": {"status": "running"}}):
        card = {"id": "t_rev", "runs": [{"metadata": {"fix_attempts": 0}}], "workspace": "dir:/w"}
        ok = iterate._execute_pm_route(
            "slug", card, "O/R", "changes requested — fix the leak",
            router_profile="project-manager-daedalus")
    assert ok is True
    kw = mk_create.call_args[1]
    assert kw["assignee"] == "project-manager-daedalus"
    assert kw["goal"] is True
    assert "fix the leak" in kw["body"]
    assert "awaiting-fix" in mk_block.call_args[0][2]


def test_execute_pm_route_already_done_skips_reblock():
    """Idempotency: if the PM card already resolved (done), skip re-block/increment."""
    with mock.patch.object(kanban, "create_task", return_value="t_pm"), \
         mock.patch.object(kanban, "comment", return_value=True), \
         mock.patch.object(kanban, "block_task") as mk_block, \
         mock.patch.object(kanban, "show_card",
                           return_value={"task": {"status": "done"}}):
        card = {"id": "t_rev", "runs": [{"metadata": {"fix_attempts": 0}}]}
        ok = iterate._execute_pm_route(
            "slug", card, "O/R", "changes requested",
            router_profile="project-manager-daedalus")
    assert ok is True
    mk_block.assert_not_called()


def test_execute_pm_route_empty_profile_falls_back_to_legacy_dev():
    """Fallback path: empty router_profile → direct developer fix card, no goal-mode."""
    with mock.patch.object(kanban, "create_task", return_value="t_fix") as mk_create, \
         mock.patch.object(kanban, "comment", return_value=True), \
         mock.patch.object(kanban, "block_task", return_value=True):
        card = {"id": "t_rev", "runs": [{"metadata": {"fix_attempts": 0}}], "workspace": "dir:/w"}
        ok = iterate._execute_pm_route(
            "slug", card, "O/R", "changes requested — fix X", router_profile="")
    assert ok is True
    kw = mk_create.call_args[1]
    assert kw["assignee"] == "developer-daedalus"
    assert not kw.get("goal")


def test_execute_pm_route_skips_awaiting_pr():
    """Guard (#87): awaiting-pr is owned by pending_pr, so PM route is a no-op False."""
    with mock.patch.object(kanban, "create_task") as mk_create:
        card = {"id": "t_dev", "runs": [{"metadata": {"fix_attempts": 0}}]}
        ok = iterate._execute_pm_route(
            "slug", card, "O/R", "review-required: awaiting-pr")
    assert ok is False
    mk_create.assert_not_called()


def test_execute_pm_route_over_cap_escalates():
    """Escalation path: over MAX_FIX_ATTEMPTS → escalate, no PM card."""
    with mock.patch.object(kanban, "comment", return_value=True) as mk_comment, \
         mock.patch.object(kanban, "create_task") as mk_create:
        card = {"id": "t_rev",
                "runs": [{"metadata": {"fix_attempts": iterate.MAX_FIX_ATTEMPTS}}]}
        ok = iterate._execute_pm_route(
            "slug", card, "O/R", "changes requested",
            router_profile="project-manager-daedalus")
    assert ok is True
    assert "escalate" in mk_comment.call_args[0][2].lower()
    mk_create.assert_not_called()


# ---- _execute_legacy_dev_fix_review ----

def test_execute_legacy_dev_fix_review_happy():
    """Happy path: creates a developer fix card and blocks the review card."""
    with mock.patch.object(kanban, "create_task", return_value="t_fix") as mk_create, \
         mock.patch.object(kanban, "comment", return_value=True), \
         mock.patch.object(kanban, "block_task", return_value=True) as mk_block:
        card = {"id": "t_rev", "runs": [{"metadata": {"fix_attempts": 0}}], "workspace": "dir:/w"}
        ok = iterate._execute_legacy_dev_fix_review(
            "slug", card, "O/R", "changes requested — fix Y")
    assert ok is True
    kw = mk_create.call_args[1]
    assert kw["assignee"] == "developer-daedalus"
    assert "fix-review-" in kw["idempotency_key"]
    assert "awaiting-fix" in mk_block.call_args[0][2]


def test_execute_legacy_dev_fix_review_over_cap_escalates():
    """Escalation path: over cap → escalate, no fix card."""
    with mock.patch.object(kanban, "comment", return_value=True) as mk_comment, \
         mock.patch.object(kanban, "create_task") as mk_create:
        card = {"id": "t_rev",
                "runs": [{"metadata": {"fix_attempts": iterate.MAX_FIX_ATTEMPTS}}]}
        ok = iterate._execute_legacy_dev_fix_review(
            "slug", card, "O/R", "changes requested")
    assert ok is True
    assert "escalate" in mk_comment.call_args[0][2].lower()
    mk_create.assert_not_called()


# ---- _execute_approve_advance ----

def test_execute_approve_advance_completes():
    """Happy path: completes the approved review card."""
    with mock.patch.object(kanban, "complete", return_value=True) as mk:
        ok = iterate._execute_approve_advance("slug", {"id": "t_rev"}, "O/R", "APPROVED")
    assert ok is True
    mk.assert_called_once_with("slug", "t_rev")


def test_execute_approve_advance_complete_fails_returns_false():
    """Negative path: kanban.complete failure propagates as False."""
    with mock.patch.object(kanban, "complete", return_value=False):
        ok = iterate._execute_approve_advance("slug", {"id": "t_rev"}, "O/R", "APPROVED")
    assert ok is False


# ---- _execute_pending_pr ----

def test_execute_pending_pr_found_updates_block_reason():
    """Happy path: a matching open PR is found → unblock + re-block with PR #N."""
    # FakeProvider has no list_prs; the executor only needs .list_prs to return
    # objects with a .number, so a plain Mock provider is the minimal stand-in.
    found = mock.Mock(number=77)
    provider = mock.Mock()
    provider.list_prs.return_value = [found]
    with mock.patch.object(iterate, "issue_linked_to_pr", return_value=True), \
         mock.patch.object(kanban, "unblock_task", return_value=True) as mk_unblock, \
         mock.patch.object(kanban, "block_task", return_value=True) as mk_block:
        card = {"id": "t_dev", "body": "Issue benmarte/daedalus#42"}
        ok = iterate._execute_pending_pr("slug", card, "O/R",
                                         "review-required: awaiting-pr", provider=provider)
    assert ok is True
    mk_unblock.assert_called_once()
    assert "PR #77" in mk_block.call_args[0][2]


def test_execute_pending_pr_no_provider_returns_false():
    """Negative path: no provider → nothing to search, returns False."""
    card = {"id": "t_dev", "body": "#42"}
    ok = iterate._execute_pending_pr("slug", card, "O/R",
                                     "review-required: awaiting-pr", provider=None)
    assert ok is False


def test_execute_pending_pr_no_matching_pr_returns_false():
    """Negative path: provider has no PR linked to the issue → False (stays blocked)."""
    provider = mock.Mock()
    provider.list_prs.return_value = []
    with mock.patch.object(kanban, "block_task") as mk_block:
        card = {"id": "t_dev", "body": "Issue benmarte/daedalus#42"}
        ok = iterate._execute_pending_pr("slug", card, "O/R",
                                         "review-required: awaiting-pr", provider=provider)
    assert ok is False
    mk_block.assert_not_called()


# ---- _execute_escalate ----

def test_execute_escalate_comments_and_stamps_without_completing():
    """Happy path: escalate posts the warning + a stamp comment, never completes."""
    with mock.patch.object(kanban, "comment", return_value=True) as mk_comment, \
         mock.patch.object(kanban, "complete") as mk_complete:
        card = {"id": "t_stuck", "body": "Issue benmarte/daedalus#42"}
        ok = iterate._execute_escalate("slug", card, "O/R", "review-required: PR #42")
    assert ok is True
    mk_complete.assert_not_called()
    # Two comments: the escalation warning and the cross-tick 'escalated: issue #N' stamp.
    bodies = [c.args[2] for c in mk_comment.call_args_list]
    assert any("ESCALATE" in b for b in bodies)
    assert any("escalated: issue #42" in b for b in bodies)


def test_execute_escalate_no_id_returns_false():
    """Negative path: no card id → False, nothing posted."""
    with mock.patch.object(kanban, "comment") as mk_comment:
        ok = iterate._execute_escalate("slug", {}, "O/R", "review-required: PR #42")
    assert ok is False
    mk_comment.assert_not_called()


# ---- _check_and_maybe_escalate (shared guard used by the fix executors) ----

def test_check_and_maybe_escalate_under_cap_returns_incremented_int():
    """Under cap → returns the incremented attempt count as an int (not bool)."""
    card = {"id": "t_dev", "runs": [{"metadata": {"fix_attempts": 1}}]}
    with mock.patch.object(kanban, "comment") as mk_comment:
        res = iterate._check_and_maybe_escalate("slug", card, "O/R", "review-required: PR #42")
    assert res == 2 and not isinstance(res, bool)
    mk_comment.assert_not_called()


def test_check_and_maybe_escalate_over_cap_delegates_to_escalate():
    """Over cap → returns the bool result of _execute_escalate."""
    card = {"id": "t_dev", "runs": [{"metadata": {"fix_attempts": iterate.MAX_FIX_ATTEMPTS}}]}
    with mock.patch.object(kanban, "comment", return_value=True) as mk_comment:
        res = iterate._check_and_maybe_escalate("slug", card, "O/R", "review-required: PR #42")
    assert res is True
    assert "escalate" in mk_comment.call_args[0][2].lower()


# ─────────────────────────────────────────────────────────────────────────────
# F1 (#1125) — startswith prefix enforcement prevents mid-string false positives
# ─────────────────────────────────────────────────────────────────────────────


class TestF1PrefixEnforcement:
    """Verify that gate matching uses startswith so mid-string signals are rejected."""

    # ── _parse_handoff approve_signals ──────────────────────────────────────

    def test_parse_handoff_midstring_approved_rejected(self):
        """'changes-requested: approved workaround' must NOT set is_approved=True.

        The classic false-positive: the word 'approved' appears mid-string after a
        changes-requested prefix.  With old substring matching it fires; with
        startswith it does not.
        """
        result = iterate._parse_handoff("changes-requested: approved workaround")
        assert result["is_approved"] is False, (
            "mid-string 'approved' must not fire with startswith matching"
        )
        # But is_changes_requested should be True (prefix matches).
        assert result["is_changes_requested"] is True

    def test_parse_handoff_review_approved_prefix_accepted(self):
        """'review-approved: PR #42' MUST set is_approved=True."""
        result = iterate._parse_handoff("review-approved: PR #42")
        assert result["is_approved"] is True

    def test_parse_handoff_security_cleared_prefix_accepted(self):
        """'security: cleared — no vulns' MUST set is_approved=True."""
        result = iterate._parse_handoff("security: cleared — no vulns")
        assert result["is_approved"] is True

    def test_parse_handoff_review_changes_requested_prefix(self):
        """'review-changes-requested: fix null deref' MUST set is_changes_requested=True."""
        result = iterate._parse_handoff("review-changes-requested: fix null deref")
        assert result["is_changes_requested"] is True

    def test_parse_handoff_security_changes_requested_prefix(self):
        """'security-changes-requested: CVE found' MUST set is_changes_requested=True."""
        result = iterate._parse_handoff("security-changes-requested: CVE found")
        assert result["is_changes_requested"] is True

    def test_parse_handoff_midstring_changes_requested_in_approved_summary_rejected(self):
        """'approved: no changes requested' must NOT set is_changes_requested=True."""
        result = iterate._parse_handoff("approved: no changes requested")
        assert result["is_changes_requested"] is False, (
            "mid-string 'changes requested' must not fire with startswith matching"
        )
        assert result["is_approved"] is True

    # ── classify_blocked — QA branch ────────────────────────────────────────

    def test_qa_midstring_qa_passed_not_at_start_pending(self):
        """QA summary 'not-qa-passed: contains qa-passed' must NOT advance."""
        result = iterate.classify_blocked("qa-daedalus", "not-qa-passed: something", ci_green=True)
        assert result == iterate.PENDING_SIGNAL, (
            "qa-passed mid-string must not trigger ADVANCE with startswith"
        )

    def test_qa_midstring_qa_failed_not_at_start_pending(self):
        """QA summary 'comment: qa-failed somewhere' must NOT trigger QA_FIX."""
        result = iterate.classify_blocked("qa-daedalus", "comment: qa-failed was seen", ci_green=True)
        assert result == iterate.PENDING_SIGNAL

    # ── classify_blocked — accessibility branch ──────────────────────────────

    def test_a11y_midstring_approved_not_at_start_pending(self):
        """'changes-requested: approved workaround' must NOT advance (was ADVANCE with substring)."""
        result = iterate.classify_blocked(
            "accessibility-daedalus",
            "changes-requested: approved workaround",
            ci_green=True,
        )
        assert result == iterate.PENDING_SIGNAL, (
            "mid-string 'approved' after 'changes-requested:' must not fire ADVANCE"
        )

    def test_a11y_changes_requested_at_start_routes_to_pm(self):
        """'changes requested: add aria labels' (at start) MUST route to PM."""
        result = iterate.classify_blocked(
            "accessibility-daedalus", "changes requested: add aria labels", ci_green=True
        )
        assert result == iterate.PM_ROUTE

    def test_a11y_approved_at_start_advances(self):
        """'approved: WCAG 2.1 AA compliant' (at start) MUST advance."""
        result = iterate.classify_blocked(
            "accessibility-daedalus", "approved: WCAG 2.1 AA compliant", ci_green=True
        )
        assert result == iterate.ADVANCE

    def test_a11y_a11y_approved_advances(self):
        """'a11y-approved: PR #N' (legacy SOUL form — starts with a11y-approved) MUST advance."""
        result = iterate.classify_blocked(
            "accessibility-daedalus", "a11y-approved: PR #55", ci_green=True
        )
        assert result == iterate.ADVANCE

    # ── classify_blocked — planner branch ───────────────────────────────────

    def test_planner_planning_complete_at_start_decomposes(self):
        """'PLANNING COMPLETE: ready' (at start) MUST trigger PLANNER_DECOMPOSE."""
        result = iterate.classify_blocked(
            "planner-daedalus", "PLANNING COMPLETE: ready to fan out", ci_green=True
        )
        assert result == iterate.PLANNER_DECOMPOSE

    def test_planner_planning_complete_mid_string_routes_to_pm(self):
        """'note: PLANNING COMPLETE appears here' (mid-string) MUST NOT decompose."""
        result = iterate.classify_blocked(
            "planner-daedalus",
            "note: I mentioned PLANNING COMPLETE as an example",
            ci_green=True,
        )
        assert result == iterate.PM_ROUTE

    # ── classify_blocked — docs branch ──────────────────────────────────────

    def test_docs_posted_at_start_approves(self):
        """'docs posted: issue #N PR #M' (at start) MUST approve."""
        result = iterate.classify_blocked(
            "documentation-daedalus", "docs posted: issue #5 PR #22 — added readme", ci_green=True
        )
        assert result == iterate.APPROVE_ADVANCE

    def test_docs_posted_mid_string_routes_to_pm(self):
        """'summary: docs posted is the terminal signal' (mid-string) MUST NOT approve."""
        result = iterate.classify_blocked(
            "documentation-daedalus",
            "summary: docs posted is the terminal signal",
            ci_green=True,
        )
        assert result == iterate.PM_ROUTE

    # ── _role_gate_passed — startswith prevents mid-string false positive ────

    def test_role_gate_passed_midstring_approved_rejected(self):
        """_role_gate_passed with ['approved'] must not match 'changes-requested: approved wk'."""
        card_data = {
            "id": "t_rev",
            "latest_summary": "changes-requested: approved workaround",
            "status": "done",
        }
        with mock.patch.object(kanban, "list_tasks", return_value=[
            {"id": "t_rev", "title": "#42", "assignee": "reviewer-daedalus"},
        ]), mock.patch.object(kanban, "show_card", return_value={"latest_summary": "changes-requested: approved workaround"}):
            passed = iterate._role_gate_passed("slug", 42, "reviewer", ["approved", "review-approved"])
        assert passed is False, (
            "mid-string 'approved' in 'changes-requested: approved workaround' "
            "must not trigger gate with startswith"
        )

    def test_role_gate_passed_canonical_prefix_accepted(self):
        """_role_gate_passed with 'review-approved' must match 'review-approved: LGTM'."""
        with mock.patch.object(kanban, "list_tasks", return_value=[
            {"id": "t_rev", "title": "#42", "assignee": "reviewer-daedalus"},
        ]), mock.patch.object(kanban, "show_card", return_value={"latest_summary": "review-approved: LGTM, no findings"}):
            passed = iterate._role_gate_passed("slug", 42, "reviewer", ["approved", "review-approved"])
        assert passed is True
