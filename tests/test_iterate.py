"""Table-driven tests for CI-aware auto-advance routing and self-healing loop.

Tests core.iterate: classify_blocked, action executors, and the main loop.
Follows the same pattern as test_daedalus.py: plain Python with a check() helper.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

# Make the package root importable (config/, core/) and the tests dir (conftest).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import conftest  # noqa: E402
from conftest import FakeProvider, _load_dispatch, check  # noqa: E402,F401
from core import iterate  # noqa: E402
from core import kanban  # noqa: E402

# Canonical FakeProvider lives in conftest; default ci defaults to "green" there,
# so request "unknown" to preserve this suite's historical default.
gp = FakeProvider(ci_status="unknown")  # patched per-test via mock.patch.object


# ── classify_blocked: pure function ──────────────────────────────────────────


def test_classify_blocked_dev_green():
    """Developer + review-required with PR + CI green → advance."""
    result = iterate.classify_blocked(
        "developer-daedalus",
        "review-required: PR #42 shipped, all tests pass",
        ci_green=True,
    )
    check("dev green CI → advance", result == iterate.ADVANCE)


def test_classify_blocked_dev_red():
    """Developer + review-required with PR + QA failure → ADVANCE (CI no longer gates ADVANCE, per epic #1074)."""
    result = iterate.classify_blocked(
        "developer-daedalus",
        "review-required: PR #42 — CI failing",
        ci_green=False,
    )
    check("dev QA-reported failures → advance (CI gated at merge-time)", result == iterate.ADVANCE)


def test_classify_blocked_dev_advance_all_ci_states():
    """Developer + review-required + PR → ADVANCE regardless of CI state (per epic #1074).

    CI gating moved from ADVANCE-time to merge-time only. This test verifies
    all three CI states (green, pending, red/unknown) produce ADVANCE.
    """
    from core.providers.base import CIStatus

    # CI green → ADVANCE
    result_green = iterate.classify_blocked(
        "developer-daedalus",
        "review-required: PR #42 shipped",
        ci_green=True,
        raw_ci=CIStatus.GREEN,
    )
    check("dev CI green → advance", result_green == iterate.ADVANCE)

    # CI pending → ADVANCE (previously PENDING_SIGNAL)
    result_pending = iterate.classify_blocked(
        "developer-daedalus",
        "review-required: PR #42 waiting on CI",
        ci_green=False,
        raw_ci=CIStatus.PENDING,
    )
    check("dev CI pending → advance", result_pending == iterate.ADVANCE)

    # QA failure → ADVANCE (previously QA_FIX)
    result_red = iterate.classify_blocked(
        "developer-daedalus",
        "review-required: PR #42 CI failing",
        ci_green=False,
        raw_ci=CIStatus.RED,
    )
    check("dev QA failure → advance", result_red == iterate.ADVANCE)

    # CI unknown → ADVANCE (previously QA_FIX)
    result_unknown = iterate.classify_blocked(
        "developer-daedalus",
        "review-required: PR #42",
        ci_green=False,
        raw_ci=CIStatus.UNKNOWN,
    )
    check("dev CI unknown → advance", result_unknown == iterate.ADVANCE)


def test_classify_blocked_dev_escalate():
    """Developer + QA failure + fix_attempts >= max → escalate."""
    result = iterate.classify_blocked(
        "developer-daedalus",
        "review-required: PR #42 — CI failing",
        ci_green=False,
        fix_attempts=3,
    )
    check("dev over max → escalate", result == iterate.ESCALATE)

    result2 = iterate.classify_blocked(
        "developer-daedalus",
        "review-required: PR #42",
        ci_green=True,
        fix_attempts=5,
    )
    check("dev over max (green CI) → escalate too", result2 == iterate.ESCALATE)


def test_classify_blocked_honors_custom_max_fix_attempts():
    """The escalation cap is configurable via the max_fix_attempts param (#1125 F3)."""
    # With a raised cap, fix_attempts=3 (the old hardcoded limit) no longer escalates.
    not_yet = iterate.classify_blocked(
        "developer-daedalus",
        "review-required: PR #42",
        ci_green=True,
        fix_attempts=3,
        max_fix_attempts=5,
    )
    check("raised cap → 3 attempts still advances", not_yet == iterate.ADVANCE)

    # At the raised cap it escalates.
    at_cap = iterate.classify_blocked(
        "developer-daedalus",
        "review-required: PR #42",
        ci_green=True,
        fix_attempts=5,
        max_fix_attempts=5,
    )
    check("raised cap → at cap escalates", at_cap == iterate.ESCALATE)

    # A lowered cap escalates earlier than the default of 3.
    reviewer_low = iterate.classify_blocked(
        "reviewer-daedalus",
        "changes requested: fix findings",
        ci_green=True,
        fix_attempts=1,
        max_fix_attempts=1,
    )
    check("lowered cap → reviewer escalates at 1", reviewer_low == iterate.ESCALATE)

    # Default (unspecified) preserves the historical cap of 3.
    default_cap = iterate.classify_blocked(
        "developer-daedalus",
        "review-required: PR #42",
        ci_green=True,
        fix_attempts=3,
    )
    check("default cap unchanged at 3", default_cap == iterate.ESCALATE)


def test_classify_blocked_dev_no_pr():
    """Developer + no PR in handoff → pm_route (was: silent drop, now routes to PM)."""
    result = iterate.classify_blocked(
        "developer-daedalus",
        "some other block reason",
        ci_green=True,
    )
    check("dev no PR → pm_route", result == iterate.PM_ROUTE)


def test_classify_blocked_dev_pr_not_open_holds():
    """#953: review-required + green CI but provider says PR is NOT open → pending_pr.

    The handoff's 'PR #N' is just a string the agent typed; if the provider
    affirmatively reports no open PR, the dev card must NOT advance (which would
    release the QA child against a phantom PR / mid-edit shared tree).
    """
    result = iterate.classify_blocked(
        "developer-daedalus",
        "review-required: PR #42 shipped, all tests pass",
        ci_green=True,
        pr_is_open=False,
    )
    check("dev green CI but PR not open → pending_pr", result == iterate.PENDING_PR)


def test_classify_blocked_dev_pr_open_advances():
    """#953: verified-open PR still advances on green CI (no regression)."""
    result = iterate.classify_blocked(
        "developer-daedalus",
        "review-required: PR #42 shipped",
        ci_green=True,
        pr_is_open=True,
    )
    check("dev green CI + PR open → advance", result == iterate.ADVANCE)


def test_classify_blocked_dev_pr_unverified_advances():
    """#953: pr_is_open=None (unverified) preserves prior advance behaviour."""
    result = iterate.classify_blocked(
        "developer-daedalus",
        "review-required: PR #42 shipped",
        ci_green=True,
        pr_is_open=None,
    )
    check("dev green CI + PR unverified → advance", result == iterate.ADVANCE)


def test_classify_blocked_dev_pr_merged_reconciles():
    """#957: review-required dev card whose PR was merged → reconcile_merged.

    A human merged the PR directly (bypassing the pipeline). The work landed,
    so the card reconciles instead of looping forever in PENDING_PR.
    """
    result = iterate.classify_blocked(
        "developer-daedalus",
        "review-required: PR #954 shipped",
        ci_green=False,
        pr_is_open=False,
        pr_is_merged=True,
    )
    check("dev PR merged → reconcile_merged", result == iterate.RECONCILE_MERGED)


def test_classify_blocked_dev_pr_merged_precedes_not_open():
    """#957: a merged PR is also 'not open', but merged wins → reconcile_merged.

    Guards the ordering: the merged check must run before the #953 not-open gate
    so a landed PR reconciles rather than being held in PENDING_PR.
    """
    result = iterate.classify_blocked(
        "developer-daedalus",
        "review-required: PR #954 shipped",
        ci_green=True,
        pr_is_open=False,
        pr_is_merged=True,
    )
    check("merged precedes not-open gate", result == iterate.RECONCILE_MERGED)


def test_classify_blocked_dev_pr_not_merged_still_holds():
    """#957: not open and not merged (phantom PR) → still PENDING_PR (no regression)."""
    result = iterate.classify_blocked(
        "developer-daedalus",
        "review-required: PR #954 shipped",
        ci_green=True,
        pr_is_open=False,
        pr_is_merged=False,
    )
    check("not-merged phantom PR → pending_pr", result == iterate.PENDING_PR)


def test_classify_blocked_reviewer_changes():
    """Reviewer + changes requested → pm_route."""
    # Canonical prefix per #1125 F1: must start with "review-changes-requested:".
    result = iterate.classify_blocked(
        "reviewer-daedalus",
        "review-changes-requested: SQL injection in api/search.py",
        ci_green=True,
    )
    check("reviewer changes requested → pm_route", result == iterate.PM_ROUTE)


def test_classify_blocked_reviewer_approved():
    """Reviewer + approved → approve_advance."""
    # Canonical prefix per #1125 F1: must start with "review-approved:".
    result = iterate.classify_blocked(
        "reviewer-daedalus",
        "review-approved: PR #42",
        ci_green=True,
    )
    check("reviewer approved → approve_advance", result == iterate.APPROVE_ADVANCE)


def test_classify_blocked_reviewer_escalate():
    """Reviewer + changes requested + fix_attempts >= max → escalate."""
    # Canonical prefix per #1125 F1: must start with "review-changes-requested:".
    result = iterate.classify_blocked(
        "reviewer-daedalus",
        "review-changes-requested: fix null deref",
        ci_green=True,
        fix_attempts=3,
    )
    check("reviewer over max → escalate", result == iterate.ESCALATE)


def test_classify_blocked_security_approved():
    """Security-analyst + approved → approve_advance."""
    # Canonical prefix per #1125 F1: must start with "security-approved:".
    result = iterate.classify_blocked(
        "security-analyst-daedalus",
        "security-approved: PR #42 — no findings",
        ci_green=True,
    )
    check("security approved → approve_advance", result == iterate.APPROVE_ADVANCE)


def test_classify_blocked_security_findings():
    """Security-analyst + blocking findings → pm_route."""
    # Canonical prefix per #1125 F1: must start with "security-changes-requested:".
    result = iterate.classify_blocked(
        "security-analyst-daedalus",
        "security-changes-requested: hardcoded secret in config.py",
        ci_green=True,
    )
    check("security findings → pm_route", result == iterate.PM_ROUTE)


def test_classify_blocked_unknown_assignee():
    """Unknown assignee → no action."""
    result = iterate.classify_blocked(
        "documentation-unknown",
        "review-required: APPROVED",
        ci_green=True,
    )
    check("unknown assignee → no action", result == "")


def test_classify_blocked_empty_handoff():
    """Empty handoff → pm_route for developer (no PR → routes to PM)."""
    result = iterate.classify_blocked("developer-daedalus", "", ci_green=True)
    check("empty handoff dev → pm_route", result == iterate.PM_ROUTE)


def test_classify_blocked_variant_approval():
    """Various approval phrasings all → approve_advance."""
    for text in (
        "review: LGTM, PR #42.",
        "review-required: sign-off given.",
        "looks good to me, approved.",
        "no findings, :+1:",
        "approved. merge when ready.",
    ):
        result = iterate.classify_blocked("reviewer-daedalus", text, ci_green=True)
        check(f"approval phrase '{text[:40]}' → approve_advance", result == iterate.APPROVE_ADVANCE)


def test_classify_blocked_variant_changes():
    """Various change-request phrasings all → pm_route."""
    for text in (
        "review: CHANGES REQUESTED — fix the null check.",
        "blocking findings in auth module: needs fixes.",
        "request changes: missing error handling.",
        "changes required before approval.",
    ):
        result = iterate.classify_blocked("reviewer-daedalus", text, ci_green=True)
        check(f"changes phrase '{text[:40]}' → pm_route", result == iterate.PM_ROUTE)


def test_classify_blocked_crash_markers():
    """Crash/infrastructure markers → no action (empty string), not pm_route.

    When the coding agent dies at startup or crashes, PM routing cannot fix it.
    classify_blocked must return '' so the human is notified, not an infinite
    pm_route loop that keeps spawning PM cards.
    """
    crash_texts = (
        "coding-agent-failed: command exited with non-zero status",
        "permission-error: API key invalid",
        "agent died: coding_agent_died unexpectedly",
        "agent crash: coding_agent_timeout after 300s",
        "subprocess exited with code 137",
        "agent crash during initialization",
    )
    for text in crash_texts:
        result = iterate.classify_blocked("developer-daedalus", text, ci_green=False)
        check(
            f"crash marker '{text[:50]}' → no-op (not pm_route)",
            result == "",
        )


# ── _parse_handoff ───────────────────────────────────────────────────────────


def test_parse_handoff_pr():
    """_parse_handoff extracts PR number."""
    h = iterate._parse_handoff("review-required: PR #42 shipped")
    check("parse PR number", h["pr_number"] == 42)
    check("is review-required", h["is_review_required"] is True)

    h2 = iterate._parse_handoff("some random text")
    check("no PR in text", h2["pr_number"] is None)
    check("not review-required", h2["is_review_required"] is False)


def test_parse_handoff_signals():
    """_parse_handoff detects changes-requested and approved."""
    h = iterate._parse_handoff("CHANGES REQUESTED — fix X")
    check("changes requested detected", h["is_changes_requested"] is True)
    check("not approved", h["is_approved"] is False)

    h2 = iterate._parse_handoff("APPROVED — LGTM")
    check("approved detected", h2["is_approved"] is True)
    check("not changes", h2["is_changes_requested"] is False)


def test_parse_handoff_pass_substring_no_false_positive():
    """#956: 'pass' substring in arbitrary text must NOT trigger is_approved.

    Previously, 'pass' was in approve_signals as a raw substring match, so text like
    'changes-requested: tests pass but login flow needs fix' incorrectly set
    is_approved=True, causing the dispatcher to fire APPROVE_ADVANCE when the
    reviewer actually requested changes.
    """
    # Exact reproduction from bug report
    h = iterate._parse_handoff("changes-requested: tests pass but login flow needs fix")
    check("changes-requested still detected", h["is_changes_requested"] is True)
    check("'tests pass' does NOT fire is_approved", h["is_approved"] is False)

    # Another false-positive case: 'password' contains 'pass'
    h2 = iterate._parse_handoff("the password field is missing validation")
    check("'password' does NOT fire is_approved", h2["is_approved"] is False)

    # 'passing' also should not trigger
    h3 = iterate._parse_handoff("tests passing but logic needs review")
    check("'passing' does NOT fire is_approved", h3["is_approved"] is False)

    # Changes-requested + 'tests pass' — must NOT approve when reviewer requested changes
    h4 = iterate._parse_handoff("review-changes-requested: tests pass but logic broken")
    check("request changes not confused with approve", h4["is_changes_requested"] is True)
    check("approved NOT set when changes requested", h4["is_approved"] is False)


def test_parse_handoff_prefixed_approval_signals():
    """#956: Role-prefixed approval signals are detected (backward compat).

    Note: "a11y-passed" was removed from approve_signals (#1258) — no SOUL emits
    it. The accessibility gate uses its own startswith check in _a11y_executor and
    never routes through _parse_handoff's approve_signals list.
    """
    prefixes = [
        "qa-passed: regression suite green",
        "security-approved: no vulnerabilities found",
        "security-passed: review complete",
    ]
    for text in prefixes:
        h = iterate._parse_handoff(text)
        check(f"prefixed approval detected: {text[:30]}", h["is_approved"] is True)
        check("not changes-requested", h["is_changes_requested"] is False)


def test_parse_handoff_existing_approvals_still_detected():
    """Existing unambiguous approval signals MUST still work (backward compat)."""
    approvals = [
        "APPROVED — LGTM",
        "approved: all tests green",
        "changes: sign-off complete",
        "signoff received",
        "looks good to me",
        "no findings from security review",
        ":+1: ready to merge",
    ]
    for text in approvals:
        h = iterate._parse_handoff(text)
        check(f"existing approval detected: {text[:30]}", h["is_approved"] is True)


def test_parse_handoff_pass_not_in_signals_list():
    """#956: 'pass' is not a standalone signal in approve_signals list."""
    import inspect
    import re
    src = inspect.getsource(iterate._parse_handoff)
    m = re.search(r"approve_signals\s*=\s*\[(.*?)\]", src, re.DOTALL)
    assert m is not None, "Could not locate approve_signals in _parse_handoff source"
    signals_block = m.group(1)
    # 'pass' must NOT appear as a standalone quoted entry
    check(
        "'pass' removed from approve_signals",
        '"pass"' not in signals_block and "'pass'" not in signals_block,
    )


# ── _count_fix_attempts ─────────────────────────────────────────────────────


def test_count_fix_attempts():
    """_count_fix_attempts reads from persistent file, board tasks, and legacy runs metadata."""
    # Legacy: runs metadata only (backward compat for tests)
    card = {
        "id": "t_123",
        "runs": [
            {"metadata": {"fix_attempts": 0}},
            {"metadata": {"fix_attempts": 2}},
        ],
    }
    check("counts max across runs", iterate._count_fix_attempts(card) == 2)

    card2 = {"id": "t_empty", "runs": []}
    check("empty runs → 0", iterate._count_fix_attempts(card2) == 0)

    card3 = {"id": "t_nokey", "runs": [{"metadata": {}}]}
    check("no fix_attempts key → 0", iterate._count_fix_attempts(card3) == 0)


def test_count_fix_attempts_board_count():
    """_count_fix_attempts counts fix cards by idempotency key on the board."""
    tasks = [
        {"idempotency_key": "fix-ci-t_parent-attempt-1"},
        {"idempotency_key": "fix-ci-t_parent-attempt-2"},
        {"idempotency_key": "unrelated-key"},
    ]
    card = {"id": "t_parent", "runs": []}
    with mock.patch.object(kanban, "list_tasks", return_value=tasks):
        count = iterate._count_fix_attempts(card, slug="slug", workdir="/tmp")
    check("board-count: 2 fix cards for t_parent", count == 2)
    # Legacy metadata can't override a higher board count
    card2 = {"id": "t_parent", "runs": [{"metadata": {"fix_attempts": 1}}]}
    with mock.patch.object(kanban, "list_tasks", return_value=tasks):
        count2 = iterate._count_fix_attempts(card2, slug="slug", workdir="/tmp")
    check("board-count beats lower metadata", count2 == 2)


def test_fix_attempts_persistence():
    """_increment_fix_attempts and _read_fix_attempts round-trip."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        card = {"id": "t_test"}
        c1 = iterate._increment_fix_attempts(card, tmp)
        check("first increment → 1", c1 == 1)
        c2 = iterate._increment_fix_attempts(card, tmp)
        check("second increment → 2", c2 == 2)
        data = iterate._read_fix_attempts(tmp)
        check("persisted count is 2", data.get("t_test") == 2)


def test_count_fix_attempts_pm_route_key():
    """_count_fix_attempts counts pm-route idempotency keys."""

    tasks = [
        {"idempotency_key": "pm-route-t_parent-attempt-1"},
        {"idempotency_key": "pm-route-t_parent-attempt-2"},
        {"idempotency_key": "fix-ci-t_parent-attempt-1"},
        {"idempotency_key": "fix-review-t_parent-attempt-1"},
        {"idempotency_key": "unrelated-key"},
    ]
    card = {"id": "t_parent", "runs": []}
    with mock.patch.object(kanban, "list_tasks", return_value=tasks):
        count = iterate._count_fix_attempts(card, slug="slug", workdir="/tmp")
    # 2 pm-route + 1 fix-ci + 1 fix-review = 4 total fix cards
    check("pm-route keys counted alongside fix-ci/fix-review", count == 4)


# ── _handoff_from_card ─────────────────────────────────────────────────────


def test_handoff_from_card():
    """_handoff_from_card extracts reason from runs or card."""
    card = {"runs": [{"reason": "review-required: PR #7"}]}
    check("from run reason", iterate._handoff_from_card(card) == "review-required: PR #7")

    card2 = {"runs": [{"reason": ""}, {"reason": "actual reason"}], "reason": "fallback"}
    check("picks first non-empty run reason",
          iterate._handoff_from_card(card2) == "actual reason")

    card3 = {"runs": [], "reason": "card-level only"}
    check("fallback to card reason",
          iterate._handoff_from_card(card3) == "card-level only")


# ── action executors ─────────────────────────────────────────────────────────


def test_execute_advance():
    """_execute_advance calls kanban.complete."""
    with mock.patch.object(kanban, "complete", return_value=True) as mk:
        card = {"id": "t_abc"}
        ok = iterate._execute_advance("slug", card, "O/R", "review-required: PR #42")
    check("advance returns True", ok is True)
    mk.assert_called_once_with("slug", "t_abc")


def test_execute_reconcile_merged():
    """#957: _execute_reconcile_merged closes all pipeline cards for the issue."""
    with mock.patch.object(
        kanban, "close_issue_tasks", return_value=["t_dev", "t_qa", "t_rev"]
    ) as mk:
        card = {"id": "t_dev", "body": "Issue benmarte/daedalus#957: fix bug"}
        ok = iterate._execute_reconcile_merged(
            "slug", card, "O/R", "review-required: PR #954 merged",
        )
    check("reconcile_merged returns True", ok is True)
    pos, kw = mk.call_args
    check("close_issue_tasks called for issue 957", pos[0] == "slug" and pos[1] == 957)
    check("summary mentions merged PR", "954" in kw["summary"] and "merged" in kw["summary"])


def test_execute_reconcile_merged_no_issue_number():
    """#957: with no issue number on the card, fall back to completing the card."""
    with mock.patch.object(kanban, "complete", return_value=True) as mk:
        card = {"id": "t_dev"}  # no body → no issue number
        ok = iterate._execute_reconcile_merged(
            "slug", card, "O/R", "review-required: PR #954 merged",
        )
    check("reconcile fallback returns True", ok is True)
    mk.assert_called_once()
    check("fallback completes the card", mk.call_args[0][1] == "t_dev")


def test_execute_reconcile_merged_dry_run():
    """#957: dry_run logs intent without mutating the board."""
    with mock.patch.object(kanban, "close_issue_tasks") as mk_close:
        with mock.patch.object(kanban, "complete") as mk_complete:
            card = {"id": "t_dev", "body": "#957"}
            ok = iterate._execute_reconcile_merged(
                "slug", card, "O/R", "review-required: PR #954 merged", dry_run=True,
            )
    check("dry_run returns True", ok is True)
    check("dry_run does not close tasks", mk_close.call_count == 0)
    check("dry_run does not complete", mk_complete.call_count == 0)


def test_execute_qa_fix():
    """_execute_qa_fix creates a fix task with idempotency key."""
    with mock.patch.object(kanban, "create_task", return_value="t_fix") as mk_create:
        with mock.patch.object(kanban, "comment", return_value=True):
            card = {
                "id": "t_dev",
                "runs": [{"metadata": {"fix_attempts": 0}}],
                "workspace": "dir:/tmp",
            }
            ok = iterate._execute_qa_fix(
                "slug", card, "O/R", "review-required: PR #55 CI failing",
            )
    check("qa_fix returns True", ok is True)
    mk_create.assert_called_once()
    # Check idempotency key includes attempt number
    call_args = mk_create.call_args[1]
    check("idempotency key has attempt 1",
          "attempt-1" in call_args["idempotency_key"])
    check("assignee is developer", call_args["assignee"] == "developer-daedalus")


def test_execute_pm_route():
    """_execute_pm_route creates a PM routing card with goal-mode and findings."""
    with mock.patch.object(kanban, "create_task", return_value="t_pm") as mk_create:
        with mock.patch.object(kanban, "comment", return_value=True):
            with mock.patch.object(kanban, "block_task", return_value=True) as mk_block:
                card = {
                    "id": "t_reviewer",
                    "runs": [{"metadata": {"fix_attempts": 0}}],
                    "workspace": "dir:/w",
                }
                ok = iterate._execute_pm_route(
                    "slug", card, "O/R",
                    "review-required: CHANGES REQUESTED — fix X",
                    router_profile="project-manager-daedalus",
                )
    check("pm_route returns True", ok is True)
    mk_create.assert_called_once()
    # Verify PM card is created with goal=True
    pos_args, call_kwargs = mk_create.call_args
    check("pm card assignee is project-manager", call_kwargs["assignee"] == "project-manager-daedalus")
    check("pm card has goal=True", call_kwargs["goal"] is True)
    check("pm body has findings", "fix X" in call_kwargs["body"])
    check("pm title mentions PM-ROUTE", "PM-ROUTE" in pos_args[1])  # title is positional arg 1
    # Verify reviewer card was blocked as awaiting-fix
    block_call = mk_block.call_args
    check("reviewer marked awaiting-fix",
          "awaiting-fix" in block_call[0][2])


def test_execute_pm_route_empty_profile_fallback():
    """_execute_pm_route with empty router_profile falls back to legacy direct-dev."""
    with mock.patch.object(kanban, "create_task", return_value="t_fix") as mk_create:
        with mock.patch.object(kanban, "comment", return_value=True):
            with mock.patch.object(kanban, "block_task", return_value=True):
                card = {
                    "id": "t_reviewer",
                    "runs": [{"metadata": {"fix_attempts": 0}}],
                    "workspace": "dir:/w",
                }
                ok = iterate._execute_pm_route(
                    "slug", card, "O/R",
                    "review-required: CHANGES REQUESTED — fix X",
                    router_profile="",  # empty → fallback
                )
    check("pm_route empty profile → fallback returns True", ok is True)
    call_kwargs = mk_create.call_args[1]
    check("fallback assignee is developer", call_kwargs["assignee"] == "developer-daedalus")
    check("fallback does NOT use goal", not call_kwargs.get("goal"))


def test_execute_approve_advance():
    """_execute_approve_advance calls kanban.complete."""
    with mock.patch.object(kanban, "complete", return_value=True) as mk:
        card = {"id": "t_rev"}
        ok = iterate._execute_approve_advance("slug", card, "O/R", "APPROVED")
    check("approve_advance returns True", ok is True)
    mk.assert_called_once_with("slug", "t_rev")


def test_execute_escalate():
    """_execute_escalate comments and logs but does NOT complete the card."""
    with mock.patch.object(kanban, "comment", return_value=True) as mk_comment:
        with mock.patch.object(kanban, "complete") as mk_complete:
            card = {"id": "t_stuck"}
            ok = iterate._execute_escalate(
                "slug", card, "O/R", "review-required: PR #42",
            )
    check("escalate returns True", ok is True)
    mk_comment.assert_called_once()
    mk_complete.assert_not_called()


def test_execute_dev_fix_escalate_when_over_cap():
    """_execute_qa_fix escalates when fix_attempts >= MAX."""
    card = {
        "id": "t_dev",
        "runs": [{"metadata": {"fix_attempts": 3}}],
    }
    with mock.patch.object(kanban, "comment", return_value=True) as mk_comment:
        ok = iterate._execute_qa_fix(
            "slug", card, "O/R", "review-required: PR #42 CI failing",
        )
    check("qa_fix escalates when over cap", ok is True)
    mk_comment.assert_called_once()
    # Make sure no create was called
    assert "escalate" in mk_comment.call_args[0][2].lower()


def test_check_and_maybe_escalate_below_threshold():
    """_check_and_maybe_escalate returns the incremented attempt count when under cap."""
    card = {"id": "t_dev", "runs": [{"metadata": {"fix_attempts": 1}}]}
    with mock.patch.object(kanban, "comment") as mk_comment:
        res = iterate._check_and_maybe_escalate(
            "slug", card, "O/R", "review-required: PR #42",
        )
    assert res == 2, f"expected incremented attempt count 2, got {res!r}"
    assert not isinstance(res, bool)
    mk_comment.assert_not_called()


def test_check_and_maybe_escalate_over_threshold():
    """_check_and_maybe_escalate delegates to _execute_escalate when over cap."""
    card = {"id": "t_dev", "runs": [{"metadata": {"fix_attempts": iterate.MAX_FIX_ATTEMPTS}}]}
    with mock.patch.object(kanban, "comment", return_value=True) as mk_comment:
        res = iterate._check_and_maybe_escalate(
            "slug", card, "O/R", "review-required: PR #42",
        )
    assert res is True, f"expected escalate result True, got {res!r}"
    mk_comment.assert_called_once()
    assert "escalate" in mk_comment.call_args[0][2].lower()


# ── run_iterate (main loop) ─────────────────────────────────────────────────


def test_run_iterate_empty():
    """run_iterate with no blocked cards returns zero counts."""
    with mock.patch.object(kanban, "list_blocked", return_value=[]):
        counts, prs, _pending, _qa_failed, *_ = iterate.run_iterate("slug", "O/R", provider=gp)
    check("empty board → all zeros", all(v == 0 for v in counts.values()))
    check("empty board → no prs", prs == [])


def test_run_iterate_dev_advance():
    """Blocked dev card with green CI → advance."""
    cards = [{
        "id": "t_dev",
        "assignee": "developer-daedalus",
        "runs": [{"reason": "review-required: PR #42 shipped"}],
    }]
    with mock.patch.object(kanban, "list_blocked", return_value=cards):
        with mock.patch.object(kanban, "complete", return_value=True):
            with mock.patch.object(gp, "get_pr_ci_status", return_value="green"):
                counts, prs, _pending, _qa_failed, *_ = iterate.run_iterate("slug", "O/R", provider=gp)
    check("dev green CI → advance count 1", counts[iterate.ADVANCE] == 1)
    check("no other actions", sum(v for v in counts.values() if v > 0) == 1)
    check("advance PR is 42", prs == [42])


def test_run_iterate_dev_no_open_pr_holds_qa():
    """#953: dev card with green CI but no real open PR → held (PENDING_PR), QA not released."""
    cards = [{
        "id": "t_dev",
        "assignee": "developer-daedalus",
        "runs": [{"reason": "review-required: PR #42 shipped"}],
    }]
    # Provider reports PR #42 is NOT among the open PRs.
    prov = FakeProvider(ci_status="green", open_prs=set())
    with mock.patch.object(kanban, "list_blocked", return_value=cards):
        with mock.patch.object(iterate, "_execute_pending_pr", return_value=True) as mpend:
            counts, prs, _pending, _qa_failed, *_ = iterate.run_iterate("slug", "O/R", provider=prov)
    check("no advance when PR not open", counts[iterate.ADVANCE] == 0)
    check("held as pending_pr", counts[iterate.PENDING_PR] == 1)
    check("pending-pr executor invoked", mpend.call_count == 1)
    check("no PR advanced", prs == [])


def test_run_iterate_dev_pr_merged_reconciles():
    """#957: dev card whose PR was merged outside the pipeline → reconcile_merged.

    The PR is not open (merged), so the #953 gate alone would loop it in
    PENDING_PR forever. With is_pr_merged=True the dispatcher reconciles:
    close_issue_tasks completes the dev card and its sibling pipeline cards.
    """
    cards = [{
        "id": "t_dev",
        "assignee": "developer-daedalus",
        "body": "Issue benmarte/daedalus#957: fix orphaned cards",
        "runs": [{"reason": "review-required: PR #954 shipped"}],
    }]
    # PR #954 is merged (not open).
    prov = FakeProvider(ci_status="green", open_prs=set(), merged_prs={954})
    with mock.patch.object(kanban, "list_blocked", return_value=cards):
        with mock.patch.object(
            kanban, "close_issue_tasks", return_value=["t_dev", "t_qa"]
        ) as mk_close:
            counts, prs, _pending, _qa_failed, *_ = iterate.run_iterate("slug", "O/R", provider=prov)
    check("reconcile_merged count 1", counts[iterate.RECONCILE_MERGED] == 1)
    check("not held as pending_pr", counts[iterate.PENDING_PR] == 0)
    check("close_issue_tasks invoked for issue 957", mk_close.call_args[0][1] == 957)


def test_run_iterate_qa_fix():
    """Blocked dev card with QA-reported failures → ADVANCE (CI no longer gates ADVANCE, per epic #1074).

    The old behavior was QA_FIX (create fix card for QA-reported failures). Now the card
    advances immediately and CI is enforced at merge-time only.
    """
    cards = [{
        "id": "t_dev",
        "assignee": "developer-daedalus",
        "runs": [{"reason": "review-required: PR #42 QA failure"}, {"metadata": {"fix_attempts": 0}}],
        "workspace": "dir:/w",
    }]
    with mock.patch.object(kanban, "list_blocked", return_value=cards):
        with mock.patch.object(kanban, "complete", return_value=True):
            with mock.patch.object(gp, "get_pr_ci_status", return_value="red"):
                counts, *_ = iterate.run_iterate("slug", "O/R", provider=gp)
    check("dev QA-reported failures → advance (CI gated at merge-time)", counts[iterate.ADVANCE] == 1)
    check("dev QA-reported failures → no qa_fix", counts[iterate.QA_FIX] == 0)


def test_run_iterate_reviewer_changes():
    """Blocked reviewer card with changes requested → pm_route."""
    cards = [{
        "id": "t_rev",
        "assignee": "reviewer-daedalus",
        # Canonical prefix per #1125 F1: "review-changes-requested:" triggers is_changes_requested.
        "runs": [{"reason": "review-changes-requested: fix auth"}, {"metadata": {"fix_attempts": 0}}],
        "workspace": "dir:/w",
    }]
    with mock.patch.object(kanban, "list_blocked", return_value=cards):
        with mock.patch.object(kanban, "create_task", return_value="t_pm"):
            with mock.patch.object(kanban, "comment", return_value=True):
                with mock.patch.object(kanban, "block_task", return_value=True):
                    with mock.patch.object(gp, "get_pr_ci_status", return_value="green"):
                        counts, *_ = iterate.run_iterate("slug", "O/R", provider=gp)
    check("reviewer changes → pm_route count 1", counts[iterate.PM_ROUTE] == 1)


def test_run_iterate_reviewer_approved():
    """Blocked reviewer card with approved → approve_advance."""
    cards = [{
        "id": "t_rev",
        "assignee": "reviewer-daedalus",
        # Canonical prefix per #1125 F1: "review-approved:" triggers is_approved.
        "runs": [{"reason": "review-approved: PR #42"}],
    }]
    with mock.patch.object(kanban, "list_blocked", return_value=cards):
        with mock.patch.object(kanban, "complete", return_value=True):
            with mock.patch.object(gp, "get_pr_ci_status", return_value="green"):
                counts, *_ = iterate.run_iterate("slug", "O/R", provider=gp)
    check("reviewer approved → approve_advance count 1", counts[iterate.APPROVE_ADVANCE] == 1)


def test_run_iterate_escalate():
    """Blocked card with fix_attempts >= MAX → escalate."""
    cards = [{
        "id": "t_dev",
        "assignee": "developer-daedalus",
        "runs": [
            {"reason": "review-required: PR #42 QA failure",
             "metadata": {"fix_attempts": 3}},  # at cap
        ],
    }]
    with mock.patch.object(kanban, "list_blocked", return_value=cards):
        with mock.patch.object(kanban, "comment", return_value=True):
            with mock.patch.object(gp, "get_pr_ci_status", return_value="red"):
                counts, *_ = iterate.run_iterate("slug", "O/R", provider=gp)
    check("over cap → escalate count 1", counts[iterate.ESCALATE] == 1)


def test_run_iterate_mixed():
    """Multiple blocked cards produce mixed action counts."""
    cards = [
        {
            "id": "t_dev_green",
            "assignee": "developer-daedalus",
            "runs": [{"reason": "review-required: PR #1"}],
        },
        {
            "id": "t_rev_approved",
            "assignee": "reviewer-daedalus",
            "runs": [{"reason": "review-required: APPROVED PR #1"}],
        },
        {
            "id": "t_unknown",
            "assignee": "documentation-daedalus",
            "runs": [{"reason": "some block"}],
        },
    ]
    with mock.patch.object(kanban, "list_blocked", return_value=cards):
        with mock.patch.object(kanban, "complete", return_value=True):
            with mock.patch.object(gp, "get_pr_ci_status", return_value="green"):
                counts, *_ = iterate.run_iterate("slug", "O/R", provider=gp)
    check("mixed: advance count 1", counts[iterate.ADVANCE] == 1)
    check("mixed: approve_advance count 1", counts[iterate.APPROVE_ADVANCE] == 1)
    check("mixed: no other actions", counts[iterate.QA_FIX] == 0
          and counts[iterate.PM_ROUTE] == 0
          and counts[iterate.ESCALATE] == 0)


def test_run_iterate_dry_run():
    """dry_run=True does not call mutating kanban methods."""
    cards = [{
        "id": "t_dev",
        "assignee": "developer-daedalus",
        "runs": [{"reason": "review-required: PR #1"}],
    }]
    with mock.patch.object(kanban, "list_blocked", return_value=cards):
        with mock.patch.object(kanban, "complete") as mk_complete:
            with mock.patch.object(gp, "get_pr_ci_status", return_value="green"):
                counts, *_ = iterate.run_iterate("slug", "O/R", provider=gp, dry_run=True)
    check("dry_run: advance counted", counts[iterate.ADVANCE] == 1)
    check("dry_run: complete NOT called", mk_complete.call_count == 0)


# ── PR→CI cache ──────────────────────────────────────────────────────────────


def test_run_iterate_ci_cache():
    """Two cards referencing the same PR → only one provider pr_ci_green call."""
    cards = [
        {
            "id": "t_a",
            "assignee": "developer-daedalus",
            "runs": [{"reason": "review-required: PR #42"}],
        },
        {
            "id": "t_b",
            "assignee": "reviewer-daedalus",
            "runs": [{"reason": "APPROVED PR #42"}],
        },
    ]
    with mock.patch.object(kanban, "list_blocked", return_value=cards):
        with mock.patch.object(kanban, "complete", return_value=True):
            with mock.patch.object(gp, "get_pr_ci_status", return_value="green") as mk_ci:
                iterate.run_iterate("slug", "O/R", provider=gp)
    check("CI cache: only 1 gh call for same PR", mk_ci.call_count == 1)


# ── Fix 3: reviewer re-engage after dev fix completion ─────────────────────


def test_execute_advance_unblocks_reviewer():
    """_execute_advance unblocks cards blocked with 'awaiting-fix: {tid}'."""
    fix_card = {"id": "t_fix"}
    blocker_card = {
        "id": "t_blocker",
        "runs": [{"reason": "awaiting-fix: t_fix"}],
    }
    with mock.patch.object(kanban, "complete", return_value=True) as mk_complete:
        with mock.patch.object(kanban, "list_blocked", return_value=[blocker_card]) as mk_list:
            with mock.patch.object(kanban, "unblock_task", return_value=True) as mk_unblock:
                ok = iterate._execute_advance(
                    "slug", fix_card, "O/R",
                    "review-required: CI green",
                )
    check("advance returns True", ok is True)
    mk_complete.assert_called_once_with("slug", "t_fix")
    check("list_blocked was called", mk_list.call_count == 1)
    # unblock_task should have been called for t_blocker
    mk_unblock.assert_called_once()
    call_slug, call_tid = mk_unblock.call_args[0][:2]
    check("unblocked t_blocker", call_tid == "t_blocker")


def test_execute_advance_ignores_other_blocks():
    """_execute_advance only unblocks cards with 'awaiting-fix' matching tid."""
    fix_card = {"id": "t_fix"}
    blocker_card = {
        "id": "t_other",
        "runs": [{"reason": "blocked for something else"}],
    }
    with mock.patch.object(kanban, "complete", return_value=True):
        with mock.patch.object(kanban, "list_blocked", return_value=[blocker_card]):
            with mock.patch.object(kanban, "unblock_task") as mk_unblock:
                iterate._execute_advance(
                    "slug", fix_card, "O/R",
                    "review-required: CI green",
                )
    mk_unblock.assert_not_called()


# ── Fix 1: _human_summary ────────────────────────────────────────────────────


def test_human_summary_format():
    """_human_summary renders PR numbers and routed actions correctly."""
    disp = _load_dispatch()

    summaries = {
        "my-repo": {
            "mode": "kanban",
            "created": [1, 2],
            "completed": [3],
            "advance_prs": [42, 99],
            "routed_actions": {"qa_fix": 1, "escalate": 2},
            "reconciled": [("5", "In review")],
        }
    }
    msg = disp._human_summary(summaries)
    check("summary mentions PR #42", "#42" in msg)
    check("summary mentions #99", "#99" in msg)
    check("summary does NOT contain count tuples", "count" not in msg)
    check("summary has ci-fix count", "QA fixes: 1x" in msg)
    check("summary has escalate count", "Escalations: 2x" in msg)
    check("summary has dispatched issues", "#1" in msg and "#2" in msg)
    check("summary has closed issues", "✅" in msg and "#3" in msg)
    check("summary has reconciled status", "#5 → In review" in msg)


def test_human_summary_no_routed_actions():
    """_human_summary omits routed actions section when none exist."""
    disp = _load_dispatch()

    summaries = {
        "my-repo": {
            "mode": "kanban",
            "advance_prs": [7],
            "routed_actions": {},
        }
    }
    msg = disp._human_summary(summaries)
    check("no routed section → no 🔧", "🔧" not in msg)
    check("advance PR #7 is shown", "#7" in msg)


def test_human_summary_empty():
    """_human_summary returns empty string when nothing happened."""
    disp = _load_dispatch()
    msg = disp._human_summary({"r": {"mode": "kanban"}})
    check("empty summary returns ''", msg == "")


def test_human_summary_pm_route():
    """_human_summary renders pm_route actions correctly."""
    disp = _load_dispatch()

    summaries = {
        "my-repo": {
            "mode": "kanban",
            "advance_prs": [7],
            "routed_actions": {"pm_route": 2, "qa_fix": 1},
        }
    }
    msg = disp._human_summary(summaries)
    check("summary has pm-route count", "PM routes: 2x" in msg)
    check("summary has ci-fix count", "QA fixes: 1x" in msg)
    check("old review-fix NOT present", "review-fix" not in msg)


# ── diagnostics() ────────────────────────────────────────────────────────────


def test_diagnostics_parses_json():
    """kanban.diagnostics() returns parsed list of dicts."""
    sample = [
        {"task_id": "t_abc", "severity": "warning", "message": "stale", "status": "blocked"},
        {"task_id": "t_def", "severity": "critical", "message": "deadlock", "status": "blocked"},
    ]
    with mock.patch("core.kanban._hk", return_value=(0, json.dumps(sample), "")):
        result = kanban.diagnostics("slug")
    check("diagnostics returns 2 items", len(result) == 2)
    check("first item is t_abc", result[0]["task_id"] == "t_abc")
    check("severity is warning", result[0]["severity"] == "warning")


def test_diagnostics_nonzero_returns_empty():
    """kanban.diagnostics() returns [] on non-zero exit."""
    with mock.patch("core.kanban._hk", return_value=(1, "", "command not found")):
        result = kanban.diagnostics("slug")
    check("diagnostics non-zero → []", result == [])


def test_diagnostics_non_json_returns_empty():
    """kanban.diagnostics() returns [] on malformed JSON."""
    with mock.patch("core.kanban._hk", return_value=(0, "not json", "")):
        result = kanban.diagnostics("slug")
    check("diagnostics bad json → []", result == [])


# ── goal-mode in create_task / create_triage ─────────────────────────────────


def test_create_task_passes_goal():
    """kanban.create_task passes --goal when goal=True."""
    with mock.patch("core.kanban._hk", return_value=(0, "t_test", "")) as mk:
        kanban.create_task("slug", "title", assignee="developer-daedalus", goal=True)
    args = mk.call_args[0][0]
    check("--goal in create_task args", "--goal" in args)


def test_create_task_passes_goal_max_turns():
    """kanban.create_task passes --goal-max-turns when goal_max_turns is set."""
    with mock.patch("core.kanban._hk", return_value=(0, "t_test", "")) as mk:
        kanban.create_task("slug", "title", assignee="developer-daedalus", goal=True, goal_max_turns=10)
    args = mk.call_args[0][0]
    check("--goal in args", "--goal" in args)
    check("--goal-max-turns 10 in args", "--goal-max-turns" in args and "10" in args)


def test_create_task_no_goal_by_default():
    """kanban.create_task NOT passes --goal when goal=False (default)."""
    with mock.patch("core.kanban._hk", return_value=(0, "t_test", "")) as mk:
        kanban.create_task("slug", "title", assignee="developer-daedalus")
    args = mk.call_args[0][0]
    check("--goal NOT in create_task args", "--goal" not in args)


# ── Fix: run_iterate handoff-source bug ──────────────────────────────────────


def test_run_iterate_falls_back_to_show_card_for_handoff():
    """run_iterate uses show_card to get handoff when list_blocked dicts lack it.

    list_blocked returns minimal dicts (no runs/no reason). Without the fallback,
    _handoff_from_card returns '' -> classify_blocked returns '' -> loop no-ops.
    With the fallback, show_card provides latest_summary -> classify works.
    """
    # Minimal list_blocked result (no runs, no reason)
    minimal_cards = [{
        "id": "t_dev",
        "assignee": "developer-daedalus",
    }]
    # show_card returns full detail with latest_summary
    full_card = {
        "latest_summary": "review-required: PR #42 shipped, CI green",
    }
    with mock.patch.object(kanban, "list_blocked", return_value=minimal_cards):
        with mock.patch.object(kanban, "show_card", return_value=full_card) as mk_show:
            with mock.patch.object(kanban, "complete", return_value=True):
                with mock.patch.object(gp, "get_pr_ci_status", return_value="green"):
                    counts, prs, _pending, _qa_failed, *_ = iterate.run_iterate("slug", "O/R", provider=gp)
    check("show_card was called for the blocked card", mk_show.call_count == 1)
    check("show_card called with slug and tid", mk_show.call_args == mock.call("slug", "t_dev"))
    check("dev green CI → advance count 1", counts[iterate.ADVANCE] == 1)
    check("advance PR is 42", prs == [42])


def test_run_iterate_show_card_fallback_skip_on_failure():
    """show_card returning None → card skipped (graceful degradation)."""
    minimal_cards = [{
        "id": "t_dev",
        "assignee": "developer-daedalus",
    }]
    with mock.patch.object(kanban, "list_blocked", return_value=minimal_cards):
        with mock.patch.object(kanban, "show_card", return_value=None):
            with mock.patch.object(gp, "get_pr_ci_status", return_value="green"):
                counts, *_ = iterate.run_iterate("slug", "O/R", provider=gp)
    check("show_card None → no actions", all(v == 0 for v in counts.values()))


def test_run_iterate_show_card_no_latest_summary():
    """show_card returns dict without latest_summary → card skipped."""
    minimal_cards = [{
        "id": "t_dev",
        "assignee": "developer-daedalus",
    }]
    with mock.patch.object(kanban, "list_blocked", return_value=minimal_cards):
        with mock.patch.object(kanban, "show_card", return_value={"id": "t_dev"}):
            with mock.patch.object(gp, "get_pr_ci_status", return_value="green"):
                counts, *_ = iterate.run_iterate("slug", "O/R", provider=gp)
    check("no latest_summary → no actions", all(v == 0 for v in counts.values()))


# ── router_profile config ────────────────────────────────────────────────────


def test_run_iterate_respects_router_profile_config():
    """run_iterate passes router_profile from resolved config to executor."""
    cards = [{
        "id": "t_rev",
        "assignee": "reviewer-daedalus",
        # Canonical prefix per #1125 F1: "review-changes-requested:" triggers is_changes_requested.
        "runs": [{"reason": "review-changes-requested: fix X"}, {"metadata": {"fix_attempts": 0}}],
        "workspace": "dir:/w",
    }]
    with mock.patch.object(kanban, "list_blocked", return_value=cards):
        with mock.patch.object(kanban, "create_task", return_value="t_pm") as mk_create:
            with mock.patch.object(kanban, "comment", return_value=True):
                with mock.patch.object(kanban, "block_task", return_value=True):
                    with mock.patch.object(gp, "get_pr_ci_status", return_value="green"):
                        iterate.run_iterate(
                            "slug", "O/R", provider=gp,
                            resolved={"router_profile": "custom-pm"},
                        )
    call_kwargs = mk_create.call_args[1]
    check("custom router_profile → assignee is custom-pm", call_kwargs["assignee"] == "custom-pm")


def test_run_iterate_default_router_profile():
    """run_iterate defaults router_profile to 'project-manager' when not in config."""
    cards = [{
        "id": "t_rev",
        "assignee": "reviewer-daedalus",
        # Canonical prefix per #1125 F1: "review-changes-requested:" triggers is_changes_requested.
        "runs": [{"reason": "review-changes-requested: fix X"}, {"metadata": {"fix_attempts": 0}}],
        "workspace": "dir:/w",
    }]
    with mock.patch.object(kanban, "list_blocked", return_value=cards):
        with mock.patch.object(kanban, "create_task", return_value="t_pm") as mk_create:
            with mock.patch.object(kanban, "comment", return_value=True):
                with mock.patch.object(kanban, "block_task", return_value=True):
                    with mock.patch.object(gp, "get_pr_ci_status", return_value="green"):
                        iterate.run_iterate("slug", "O/R", provider=gp, resolved={})
    call_kwargs = mk_create.call_args[1]
    check("default router_profile → assignee is project-manager", call_kwargs["assignee"] == "project-manager-daedalus")


# ── branch→PR fallback ────────────────────────────────────────────────────────


def test_classify_blocked_pr_number_fallback():
    """classify_blocked uses pr_number kwarg when handoff has no PR."""
    # handoff has no PR, but pr_number=42 provided
    result = iterate.classify_blocked(
        "developer-daedalus",
        "review-required: all tests pass, CI green",
        ci_green=True,
        pr_number=42,
    )
    check("classify pr_number fallback → advance", result == iterate.ADVANCE)

    # handoff WITH PR still wins (handoff takes priority)
    result2 = iterate.classify_blocked(
        "developer-daedalus",
        "review-required: PR #99 shipped",
        ci_green=True,
        pr_number=42,  # should be ignored; handoff's #99 wins
    )
    check("handoff PR wins over pr_number fallback", result2 == iterate.ADVANCE)


def test_run_iterate_branch_pr_fallback():
    """run_iterate advances a dev card whose handoff has no PR but branch resolves to one."""
    cards = [{
        "id": "t_dev",
        "assignee": "developer-daedalus",
        "branch_name": "feat/my-feature",
        "runs": [{"reason": "review-required: CI green, shipped"}],
    }]
    with mock.patch.object(kanban, "list_blocked", return_value=cards):
        with mock.patch.object(kanban, "complete", return_value=True):
            with mock.patch.object(gp, "find_pr_for_branch", return_value=42) as mk_branch:
                with mock.patch.object(gp, "get_pr_ci_status", return_value="green"):
                    counts, prs, _pending, _qa_failed, *_ = iterate.run_iterate("slug", "O/R", provider=gp)
    check("branch PR fallback → advance count 1", counts[iterate.ADVANCE] == 1)
    check("branch PR fallback → PR is 42", prs == [42])
    mk_branch.assert_called_once_with("feat/my-feature")


def test_run_iterate_branch_pr_fallback_no_match():
    """branch→PR lookup returns None → card skipped (graceful)."""
    cards = [{
        "id": "t_dev",
        "assignee": "developer-daedalus",
        "branch_name": "feat/nonexistent",
        "runs": [{"reason": "review-required: CI green"}],
    }]
    with mock.patch.object(kanban, "list_blocked", return_value=cards):
        with mock.patch.object(gp, "find_pr_for_branch", return_value=None):
            with mock.patch.object(gp, "get_pr_ci_status") as mk_ci:
                counts, *_ = iterate.run_iterate("slug", "O/R", provider=gp)
    check("branch no match → no actions", all(v == 0 for v in counts.values()))
    mk_ci.assert_not_called()


def test_run_iterate_handoff_pr_still_works():
    """Existing handoff-PR path still works alongside the new branch fallback."""
    cards = [{
        "id": "t_dev",
        "assignee": "developer-daedalus",
        "branch_name": "feat/other",
        "runs": [{"reason": "review-required: PR #55 shipped"}],
    }]
    with mock.patch.object(kanban, "list_blocked", return_value=cards):
        with mock.patch.object(kanban, "complete", return_value=True):
            with mock.patch.object(gp, "find_pr_for_branch") as mk_branch:
                with mock.patch.object(gp, "get_pr_ci_status", return_value="green") as mk_ci:
                    counts, prs, _pending, _qa_failed, *_ = iterate.run_iterate("slug", "O/R", provider=gp)
    check("handoff PR still works → advance count 1", counts[iterate.ADVANCE] == 1)
    check("handoff PR still works → PR is 55", prs == [55])
    # open_pr_for_branch should NOT be called since handoff has a PR
    mk_branch.assert_not_called()
    mk_ci.assert_called_once_with(55)


def test_run_iterate_branch_pr_fallback_ci_red():
    """branch PR + QA failure → ADVANCE (CI no longer gates ADVANCE, per epic #1074)."""
    cards = [{
        "id": "t_dev",
        "assignee": "developer-daedalus",
        "branch_name": "feat/broken",
        "runs": [{"reason": "review-required: CI failing"}, {"metadata": {"fix_attempts": 0}}],
        "workspace": "dir:/w",
    }]
    with mock.patch.object(kanban, "list_blocked", return_value=cards):
        with mock.patch.object(kanban, "complete", return_value=True):
            with mock.patch.object(gp, "find_pr_for_branch", return_value=99):
                with mock.patch.object(gp, "get_pr_ci_status", return_value="red"):
                    with mock.patch.object(gp, "is_pr_open", return_value=True):
                        counts, *_ = iterate.run_iterate("slug", "O/R", provider=gp)
    check("branch PR ci red → advance count 1", counts[iterate.ADVANCE] == 1)




# ── pipeline reliability routing (issue #19) ──────────────────────────────


def test_classify_blocked_planner_returns_pm_route():
    """Planner blocked → pm_route (PM consultation)."""
    result = iterate.classify_blocked(
        "planner-daedalus",
        "BLOCKED: missing acceptance criteria",
        ci_green=True,
    )
    check("planner blocked → pm_route", result == iterate.PM_ROUTE)


def test_classify_blocked_documentation_docs_posted_returns_approve_advance():
    """Documentation blocked with 'docs posted' → approve_advance (terminal complete)."""
    # Canonical prefix per #1125 F1: must start with "docs posted:".
    result = iterate.classify_blocked(
        "documentation-daedalus",
        "docs posted: PR #34 — README updated",
        ci_green=True,
    )
    check("docs posted → approve_advance", result == iterate.APPROVE_ADVANCE)


def test_classify_blocked_documentation_unknown_returns_pm_route():
    """Documentation blocked with unknown signal → pm_route (PM consultation)."""
    result = iterate.classify_blocked(
        "documentation-daedalus",
        "BLOCKED: unclear scope",
        ci_green=True,
    )
    check("docs unknown → pm_route", result == iterate.PM_ROUTE)


def test_classify_blocked_pm_returns_escalate():
    """PM blocked → escalate (human gate — PM can't consult itself)."""
    result = iterate.classify_blocked(
        "project-manager-daedalus",
        "BLOCKED: conflicting requirements",
        ci_green=True,
    )
    check("PM blocked → escalate", result == iterate.ESCALATE)


# ── _extract_issue_number_from_card ─────────────────────────────────────────


def test_extract_issue_number_repo_qualified():
    """Extracts issue number from org/repo#N pattern."""
    card = {"body": "Implement issue benmarte/daedalus#21 in the repo."}
    check("repo-qualified #21", iterate._extract_issue_number_from_card(card) == 21)


def test_extract_issue_number_bare_hash():
    """Falls back to bare #N when no repo-qualified pattern."""
    card = {"body": "Fix for #42 is ready."}
    check("bare #42", iterate._extract_issue_number_from_card(card) == 42)


def test_extract_issue_number_none():
    """Returns None when no issue number found."""
    card = {"body": "Some task without an issue reference."}
    check("no issue number → None", iterate._extract_issue_number_from_card(card) is None)

    card2 = {"body": ""}
    check("empty body → None", iterate._extract_issue_number_from_card(card2) is None)


def test_extract_issue_number_prefers_repo_qualified():
    """Prefers org/repo#N over a bare #N that appears first."""
    card = {"body": "PR #10 was opened. Implements benmarte/daedalus#21."}
    check("prefers repo-qualified #21 over bare #10",
          iterate._extract_issue_number_from_card(card) == 21)


# ── _create_downstream_review_tasks ─────────────────────────────────────────


def test_create_downstream_happy_path():
    """Creates qa/reviewer/security/accessibility/docs tasks with correct keys and assignees."""
    card = {
        "id": "t_dev",
        "body": "Implement benmarte/daedalus#19",
        "workspace": "dir:/work",
    }
    with mock.patch.object(kanban, "list_tasks", return_value=[]):
        with mock.patch.object(kanban, "create_task", side_effect=["t_qa", "t_rev", "t_sec", "t_acc", "t_doc"]) as mk_create:
            with mock.patch.object(kanban, "comment", return_value=True) as mk_comment:
                created = iterate._create_downstream_review_tasks("slug", 19, card, pr_number=22)
    check("created 5 tasks", len(created) == 5)
    check("returns correct ids", created == ["t_qa", "t_rev", "t_sec", "t_acc", "t_doc"])
    check("create_task called 5 times", mk_create.call_count == 5)

    # Verify body text reflects parallel CI (per epic #1074 — no longer says "CI is green")
    bodies = [call.kwargs.get("body", "") for call in mk_create.call_args_list]
    check("body mentions 'CI may still be running'",
          all("CI may still be running" in b for b in bodies))
    check("body does NOT say 'CI is green'",
          all("CI is green" not in b for b in bodies))

    # Verify assignees
    assignees = [call.kwargs["assignee"] for call in mk_create.call_args_list]
    check("qa assignee", assignees[0] == "qa-daedalus")
    check("reviewer assignee", assignees[1] == "reviewer-daedalus")
    check("security assignee", assignees[2] == "security-analyst-daedalus")
    check("accessibility assignee", assignees[3] == "accessibility-daedalus")
    check("docs assignee", assignees[4] == "documentation-daedalus")

    # Verify idempotency keys
    keys = [call.kwargs["idempotency_key"] for call in mk_create.call_args_list]
    check("qa key", keys[0] == "qa-19")
    check("reviewer key", keys[1] == "reviewer-19")
    check("security key", keys[2] == "security-19")
    check("accessibility key", keys[3] == "accessibility-19")
    check("docs key", keys[4] == "docs-19")

    # Verify workspace propagated
    workspaces = [call.kwargs["workspace"] for call in mk_create.call_args_list]
    check("workspace propagated", all(w == "dir:/work" for w in workspaces))

    # Verify the QA gate parent chain (#955): only QA hangs off the dev card;
    # every other role is gated behind QA so it cannot unblock before QA runs.
    parents = [call.kwargs["parents"] for call in mk_create.call_args_list]
    check("qa parented to dev card", parents[0] == ["t_dev"])
    check("reviewer parented to qa", parents[1] == ["t_qa"])
    check("security parented to qa", parents[2] == ["t_qa"])
    check("accessibility parented to qa", parents[3] == ["t_qa"])
    check("docs parented to reviewer/security/accessibility (not dev)",
          parents[4] == ["t_rev", "t_sec", "t_acc"])
    check("no review role parented to dev card except qa",
          all("t_dev" not in (p or []) for p in parents[1:]))

    # Verify comment posted
    mk_comment.assert_called_once()


def test_create_downstream_qa_gate_recovered_parent():
    """#955 When QA already exists, a new reviewer is parented to the recovered
    QA id — not the developer card — so the QA gate is preserved on re-runs."""
    card = {"id": "t_dev", "body": "benmarte/daedalus#19", "workspace": "dir:/w"}
    # QA already on the board (any status) with a known id; reviewer is new.
    existing = [{"idempotency_key": "qa-19", "id": "t_qa_existing", "status": "running"}]
    with mock.patch.object(kanban, "list_tasks", return_value=existing):
        with mock.patch.object(kanban, "create_task", side_effect=["t_rev", "t_sec", "t_acc", "t_doc"]) as mk_create:
            with mock.patch.object(kanban, "comment", return_value=True):
                iterate._create_downstream_review_tasks("slug", 19, card)
    by_key = {call.kwargs["idempotency_key"]: call.kwargs["parents"] for call in mk_create.call_args_list}
    check("qa skipped (already exists)", "qa-19" not in by_key)
    check("reviewer parented to recovered qa id", by_key["reviewer-19"] == ["t_qa_existing"])
    check("security parented to recovered qa id", by_key["security-19"] == ["t_qa_existing"])
    check("accessibility parented to recovered qa id", by_key["accessibility-19"] == ["t_qa_existing"])
    check("no recovered role parented to dev card",
          all("t_dev" not in (p or []) for p in by_key.values()))


def test_create_downstream_idempotency_guard():
    """Skips creation for tasks whose idempotency keys already exist."""
    card = {"id": "t_dev", "body": "benmarte/daedalus#19", "workspace": "dir:/w"}
    existing = [
        {"idempotency_key": "qa-19"},
        {"idempotency_key": "reviewer-19"},
        {"idempotency_key": "security-19"},
        {"idempotency_key": "accessibility-19"},
        {"idempotency_key": "docs-19"},
    ]
    with mock.patch.object(kanban, "list_tasks", return_value=existing):
        with mock.patch.object(kanban, "create_task") as mk_create:
            with mock.patch.object(kanban, "comment", return_value=True):
                created = iterate._create_downstream_review_tasks("slug", 19, card, pr_number=22)
    check("all exist → nothing created", created == [])
    mk_create.assert_not_called()


def test_create_downstream_partial_idempotency():
    """Creates only the tasks whose keys don't yet exist."""
    card = {"id": "t_dev", "body": "benmarte/daedalus#19", "workspace": "dir:/w"}
    # qa and reviewer already exist; security, accessibility, docs need creating
    existing = [{"idempotency_key": "qa-19"}, {"idempotency_key": "reviewer-19"}]
    with mock.patch.object(kanban, "list_tasks", return_value=existing):
        with mock.patch.object(kanban, "create_task", side_effect=["t_sec", "t_acc", "t_doc"]) as mk_create:
            with mock.patch.object(kanban, "comment", return_value=True):
                created = iterate._create_downstream_review_tasks("slug", 19, card, pr_number=22)
    check("partial: 3 created (security + accessibility + docs)", len(created) == 3)
    check("create_task called 3 times", mk_create.call_count == 3)
    # Verify the right keys were used
    keys = [call.kwargs["idempotency_key"] for call in mk_create.call_args_list]
    check("security key created", keys[0] == "security-19")
    check("accessibility key created", keys[1] == "accessibility-19")
    check("docs key created", keys[2] == "docs-19")


def test_create_downstream_dedup_respects_status():
    """#936 Dedup must detect tasks regardless of their status.

    Idempotency scan must match on idempotency_key alone — a task in
    ``running``/``ready``/``done``/``blocked`` with the same key must still
    prevent creation. Regression guard for #936.
    """
    card = {"id": "t_dev", "body": "benmarte/daedalus#19", "workspace": "dir:/w"}

    # security exists in "running" status — must be detected and skipped
    existing = [{"idempotency_key": "security-19", "status": "running"}]
    with mock.patch.object(kanban, "list_tasks", return_value=existing):
        with mock.patch.object(kanban, "create_task", side_effect=["t_qa", "t_rev", "t_acc", "t_doc"]) as mk_create:
            with mock.patch.object(kanban, "comment", return_value=True):
                iterate._create_downstream_review_tasks("slug", 19, card)

    created_keys = [call.kwargs["idempotency_key"] for call in mk_create.call_args_list]
    check("security (running) skipped", "security-19" not in created_keys)
    check("4 tasks created (qa, reviewer, accessibility, docs)", len(created_keys) == 4)
    check("qa key created", "qa-19" in created_keys)
    check("reviewer key created", "reviewer-19" in created_keys)
    check("accessibility key created", "accessibility-19" in created_keys)
    check("docs key created", "docs-19" in created_keys)


def test_create_downstream_dedup_multiple_statuses():
    """#936 Dedup must work across several status values at once.

    qa / reviewer / security each exist in a different status — all must be
    deduplicated; only accessibility and docs should be created.
    """
    card = {"id": "t_dev", "body": "benmarte/daedalus#19", "workspace": "dir:/w"}

    existing = [
        {"idempotency_key": "qa-19", "status": "ready"},
        {"idempotency_key": "reviewer-19", "status": "running"},
        {"idempotency_key": "security-19", "status": "blocked"},
    ]
    with mock.patch.object(kanban, "list_tasks", return_value=existing):
        with mock.patch.object(kanban, "create_task", side_effect=["t_acc", "t_doc"]) as mk_create:
            with mock.patch.object(kanban, "comment", return_value=True):
                iterate._create_downstream_review_tasks("slug", 19, card)

    created_keys = [call.kwargs["idempotency_key"] for call in mk_create.call_args_list]
    check("qa (ready) skipped", "qa-19" not in created_keys)
    check("reviewer (running) skipped", "reviewer-19" not in created_keys)
    check("security (blocked) skipped", "security-19" not in created_keys)
    check("2 tasks created (accessibility, docs)", len(created_keys) == 2)
    check("accessibility key created", "accessibility-19" in created_keys)
    check("docs key created", "docs-19" in created_keys)


def test_create_downstream_dedup_done_status():
    """#936 A task in ``done`` status must still be deduplicated.

    Regression: a terminal reviewer card from a previous run must prevent a
    fresh reviewer creation on re-dispatch.
    """
    card = {"id": "t_dev", "body": "benmarte/daedalus#19", "workspace": "dir:/w"}
    existing = [{"idempotency_key": "reviewer-19", "status": "done"}]
    with mock.patch.object(kanban, "list_tasks", return_value=existing):
        with mock.patch.object(kanban, "create_task", side_effect=["t_qa", "t_sec", "t_acc", "t_doc"]) as mk_create:
            with mock.patch.object(kanban, "comment", return_value=True):
                iterate._create_downstream_review_tasks("slug", 19, card)

    created_keys = [call.kwargs["idempotency_key"] for call in mk_create.call_args_list]
    check("reviewer (done) skipped", "reviewer-19" not in created_keys)
    check("4 other tasks still created", len(created_keys) == 4)


def test_create_downstream_dry_run():
    """dry_run=True logs but does not create or comment."""
    card = {"id": "t_dev", "body": "benmarte/daedalus#19", "workspace": "dir:/w"}
    with mock.patch.object(kanban, "list_tasks", return_value=[]):
        with mock.patch.object(kanban, "create_task") as mk_create:
            with mock.patch.object(kanban, "comment") as mk_comment:
                created = iterate._create_downstream_review_tasks(
                    "slug", 19, card, pr_number=22, dry_run=True,
                )
    check("dry_run: nothing created", created == [])
    mk_create.assert_not_called()
    mk_comment.assert_not_called()


def test_create_downstream_delegation_wrapped_when_coding_agent():
    """#1344: downstream review cards carry the delegation marker when an external
    coding agent is configured — so ``direct_dispatch`` dispatches them instead of
    skipping (``_DELEGATION_MARKER not in body``). The accessibility card is the
    only review role created solely by this path, so it was the sole one to stall.
    """
    from core.dispatch.bodies import _DELEGATION_MARKER
    disp = _load_dispatch()  # noqa: F841 — _disp() finds this frame in the stack
    card = {"id": "t_dev", "body": "benmarte/daedalus#19", "workspace": "dir:/w"}
    with mock.patch.object(kanban, "list_tasks", return_value=[]):
        with mock.patch.object(
            kanban, "create_task",
            side_effect=["t_qa", "t_rev", "t_sec", "t_acc", "t_doc"],
        ) as mk_create:
            with mock.patch.object(kanban, "comment", return_value=True):
                iterate._create_downstream_review_tasks(
                    "slug", 19, card, pr_number=22,
                    coding_agent="claude-code", coding_agent_cmd="claude -p",
                )
    bodies = {
        call.kwargs["idempotency_key"]: call.kwargs["body"]
        for call in mk_create.call_args_list
    }
    check("accessibility body carries the delegation marker",
          _DELEGATION_MARKER in bodies["accessibility-19"])
    check("every downstream review body carries the delegation marker",
          all(_DELEGATION_MARKER in b for b in bodies.values()))
    # The generic base body must survive the wrap (delegation is additive).
    check("base body preserved under the delegation wrap",
          all("CI may still be running" in b for b in bodies.values()))


def test_create_downstream_no_delegation_without_coding_agent():
    """Default path is byte-identical: no external coding agent → plain body, no
    delegation marker. Guards the non-delegate flow from behaviour drift (#1344)."""
    from core.dispatch.bodies import _DELEGATION_MARKER
    card = {"id": "t_dev", "body": "benmarte/daedalus#19", "workspace": "dir:/w"}
    with mock.patch.object(kanban, "list_tasks", return_value=[]):
        with mock.patch.object(
            kanban, "create_task",
            side_effect=["t_qa", "t_rev", "t_sec", "t_acc", "t_doc"],
        ) as mk_create:
            with mock.patch.object(kanban, "comment", return_value=True):
                iterate._create_downstream_review_tasks("slug", 19, card, pr_number=22)
    bodies = [call.kwargs["body"] for call in mk_create.call_args_list]
    check("no delegation marker when no coding agent is configured",
          all(_DELEGATION_MARKER not in b for b in bodies))
    check("hermes agent also yields a plain body",
          iterate._wrap_downstream_delegation(
              "body-x", "accessibility", 19, "hermes", "") == "body-x")


def test_execute_advance_triggers_downstream():
    """_execute_advance calls _create_downstream_review_tasks after completing."""
    card = {
        "id": "t_dev",
        "body": "Implement benmarte/daedalus#42",
        "workspace": "dir:/work",
    }
    with mock.patch.object(kanban, "complete", return_value=True):
        with mock.patch.object(kanban, "list_blocked", return_value=[]):
            with mock.patch.object(iterate, "_create_downstream_review_tasks", return_value=["t1"]) as mk_downstream:
                ok = iterate._execute_advance("slug", card, "O/R", "review-required: PR #44")
    check("advance returns True", ok is True)
    mk_downstream.assert_called_once()
    call_args = mk_downstream.call_args
    check("issue number passed", call_args[0][1] == 42)
    check("pr_number passed", call_args[1]["pr_number"] == 44)


def test_execute_advance_skip_downstream_no_issue_number():
    """_execute_advance skips downstream creation when issue number can't be parsed."""
    card = {
        "id": "t_dev",
        "body": "Some task with no issue reference",
        "workspace": "dir:/work",
    }
    with mock.patch.object(kanban, "complete", return_value=True):
        with mock.patch.object(kanban, "list_blocked", return_value=[]):
            with mock.patch.object(iterate, "_create_downstream_review_tasks") as mk_downstream:
                ok = iterate._execute_advance("slug", card, "O/R", "review-required: PR #44")
    check("advance returns True even without issue number", ok is True)
    mk_downstream.assert_not_called()


def test_downstream_task_body_references_pr():
    """Downstream task body includes PR number when available."""
    card = {"id": "t_dev", "body": "benmarte/daedalus#19", "workspace": "dir:/w"}
    with mock.patch.object(kanban, "list_tasks", return_value=[]):
        with mock.patch.object(kanban, "create_task", return_value="t_rev") as mk_create:
            with mock.patch.object(kanban, "comment", return_value=True):
                iterate._create_downstream_review_tasks("slug", 19, card, pr_number=42)
    body = mk_create.call_args[1]["body"]
    check("body references PR #42", "PR #42" in body)
    check("body references issue #19", "issue #19" in body)
    check("body references developer card", "t_dev" in body)


def test_create_downstream_idempotency_open_status():
    """Dedup works when task exists in open (todo/ready) status, not just done."""
    card = {"id": "t_dev", "body": "benmarte/daedalus#19", "workspace": "dir:/w"}
    # All 5 downstream task keys exist, each in a different non-terminal status.
    # dedup must skip creation regardless of status.
    existing = [
        {"idempotency_key": "qa-19", "status": "todo"},
        {"idempotency_key": "reviewer-19", "status": "ready"},
        {"idempotency_key": "security-19", "status": "running"},
        {"idempotency_key": "accessibility-19", "status": "blocked"},
        {"idempotency_key": "docs-19", "status": "todo"},
    ]
    with mock.patch.object(kanban, "list_tasks", return_value=existing):
        with mock.patch.object(kanban, "create_task", return_value="t_unexpected") as mk_create:
            with mock.patch.object(kanban, "comment", return_value=True):
                created = iterate._create_downstream_review_tasks("slug", 19, card, pr_number=22)
    check("open/ready/running/blocked tasks → all skipped", created == [])
    mk_create.assert_not_called()


def test_create_downstream_idempotency_in_progress_status():
    """Dedup works when task exists in in-progress (running) status."""
    card = {"id": "t_dev", "body": "benmarte/daedalus#19", "workspace": "dir:/w"}
    # Task exists with idempotency key and is actively running
    existing = [
        {"idempotency_key": "docs-19", "status": "running", "id": "t_doc"},
    ]
    with mock.patch.object(kanban, "list_tasks", return_value=existing):
        with mock.patch.object(kanban, "create_task", return_value="t_other") as mk_create:
            with mock.patch.object(kanban, "comment", return_value=True):
                created = iterate._create_downstream_review_tasks("slug", 19, card, pr_number=22)
    check("running task → docs key skipped", len(created) == 4)
    check("4 tasks created (qa, reviewer, security, accessibility)", mk_create.call_count == 4)
    # Verify docs was NOT in the created keys
    created_keys = [call.kwargs["idempotency_key"] for call in mk_create.call_args_list]
    check("docs key not created", "docs-19" not in created_keys)
    check("qa key created", "qa-19" in created_keys)


def test_create_downstream_idempotency_mixed_statuses():
    """Dedup works across all statuses: todo, ready, running, blocked, done."""
    card = {"id": "t_dev", "body": "benmarte/daedalus#19", "workspace": "dir:/w"}
    # Each existing task has a different status
    existing = [
        {"idempotency_key": "qa-19", "status": "done", "id": "t_qa"},
        {"idempotency_key": "reviewer-19", "status": "todo", "id": "t_rev"},
        {"idempotency_key": "security-19", "status": "running", "id": "t_sec"},
        {"idempotency_key": "accessibility-19", "status": "blocked", "id": "t_acc"},
        # docs has no matching key, so it should be created
    ]
    with mock.patch.object(kanban, "list_tasks", return_value=existing):
        with mock.patch.object(kanban, "create_task", return_value="t_doc") as mk_create:
            with mock.patch.object(kanban, "comment", return_value=True):
                created = iterate._create_downstream_review_tasks("slug", 19, card, pr_number=22)
    check("only docs created (4 others exist across all statuses)", len(created) == 1)
    check("docs key created", mk_create.call_args[1]["idempotency_key"] == "docs-19")


def test_create_downstream_independent_task_types():
    """Each task type is checked independently — existence of one doesn't affect others."""
    card = {"id": "t_dev", "body": "benmarte/daedalus#19", "workspace": "dir:/w"}
    # Only reviewer-19 exists; qa, security, accessibility, docs should be created
    existing = [{"idempotency_key": "reviewer-19", "status": "done", "id": "t_rev"}]
    with mock.patch.object(kanban, "list_tasks", return_value=existing):
        with mock.patch.object(kanban, "create_task", side_effect=["t_qa", "t_sec", "t_acc", "t_doc"]) as mk_create:
            with mock.patch.object(kanban, "comment", return_value=True):
                created = iterate._create_downstream_review_tasks("slug", 19, card, pr_number=22)
    check("4 tasks created (reviewer skipped)", len(created) == 4)
    check("reviewer key not in created keys",
          all(call.kwargs["idempotency_key"] != "reviewer-19" for call in mk_create.call_args_list))
    created_keys = [call.kwargs["idempotency_key"] for call in mk_create.call_args_list]
    check("qa key created", "qa-19" in created_keys)
    check("security key created", "security-19" in created_keys)
    check("accessibility key created", "accessibility-19" in created_keys)
    check("docs key created", "docs-19" in created_keys)


def test_create_downstream_no_preexisting_creates_all():
    """When no tasks exist, all 5 downstream tasks are created."""
    card = {"id": "t_dev", "body": "benmarte/daedalus#21", "workspace": "dir:/w"}
    with mock.patch.object(kanban, "list_tasks", return_value=[]):
        with mock.patch.object(kanban, "create_task", side_effect=["t_qa", "t_rev", "t_sec", "t_acc", "t_doc"]) as mk_create:
            with mock.patch.object(kanban, "comment", return_value=True):
                created = iterate._create_downstream_review_tasks("slug", 21, card, pr_number=25)
    check("no preexisting → all 5 created", len(created) == 5)
    check("create_task called 5 times", mk_create.call_count == 5)


# ── Issue #24: robust CI polling ──────────────────────────────────────────────


class _NoCIProvider:
    """Provider that does NOT support CI status (supports_ci_status=False)."""
    name = "generic"
    supports_ci_status = False

    def has_label(self, pr_number, label_name):
        return False

    def get_pr_ci_status(self, pr_number):
        return "unknown"

    def find_pr_for_branch(self, branch):
        return None


class _PendingProvider:
    """Provider that supports CI but returns PENDING initially."""
    name = "github"
    supports_ci_status = True

    def has_label(self, pr_number, label_name):
        return False

    def __init__(self, ci_status="pending"):
        self._status = ci_status

    def get_pr_ci_status(self, pr_number):
        return self._status

    def find_pr_for_branch(self, branch):
        return None


def test_run_iterate_no_ci_provider_advances_immediately():
    """Provider without CI support + UNKNOWN status → treated as green → advance."""
    no_ci = _NoCIProvider()
    cards = [{
        "id": "t_dev",
        "assignee": "developer-daedalus",
        "runs": [{"reason": "review-required: PR #42 shipped"}],
    }]
    with mock.patch.object(kanban, "list_blocked", return_value=cards):
        with mock.patch.object(kanban, "complete", return_value=True):
            counts, prs, pending, _qa_f, *_ = iterate.run_iterate("slug", "O/R", provider=no_ci)
    check("no-CI provider → advance count 1", counts[iterate.ADVANCE] == 1)
    check("no-CI provider → advance PR 42", prs == [42])
    check("no-CI provider → no pending", pending == [])


def test_run_iterate_pending_signal_returns_pending_cards():
    """CI PENDING → ADVANCE (CI no longer gates ADVANCE, per epic #1074). Card is completed, no pending cards."""
    pp = _PendingProvider("pending")
    cards = [{
        "id": "t_dev",
        "assignee": "developer-daedalus",
        "runs": [{"reason": "review-required: PR #42 shipped"}],
    }]
    with mock.patch.object(kanban, "list_blocked", return_value=cards):
        with mock.patch.object(kanban, "complete", return_value=True):
            counts, prs, pending, _qa_f, *_ = iterate.run_iterate("slug", "O/R", provider=pp)
    check("pending CI → advance count 1", counts[iterate.ADVANCE] == 1)
    check("pending CI → prs", prs == [42])
    check("pending CI → no pending cards", pending == [])


def test_run_iterate_green_ci_no_pending_cards():
    """CI green → advance, pending_signal_cards is empty."""
    gp_green = _PendingProvider("green")
    cards = [{
        "id": "t_dev",
        "assignee": "developer-daedalus",
        "runs": [{"reason": "review-required: PR #42 shipped"}],
    }]
    with mock.patch.object(kanban, "list_blocked", return_value=cards):
        with mock.patch.object(kanban, "complete", return_value=True):
            counts, prs, pending, _qa_f, *_ = iterate.run_iterate("slug", "O/R", provider=gp_green)
    check("green CI → advance count 1", counts[iterate.ADVANCE] == 1)
    check("green CI → prs", prs == [42])
    check("green CI → no pending", pending == [])


def test_run_iterate_red_ci_no_pending_cards():
    """QA failure → ADVANCE (CI no longer gates ADVANCE, per epic #1074). No pending cards."""
    gp_red = _PendingProvider("red")
    cards = [{
        "id": "t_dev",
        "assignee": "developer-daedalus",
        "runs": [{"reason": "review-required: PR #42 QA failure"}, {"metadata": {"fix_attempts": 0}}],
        "workspace": "dir:/w",
    }]
    with mock.patch.object(kanban, "list_blocked", return_value=cards):
        with mock.patch.object(kanban, "complete", return_value=True):
            counts, prs, pending, _qa_f, *_ = iterate.run_iterate("slug", "O/R", provider=gp_red)
    check("QA-reported failures → advance count 1", counts[iterate.ADVANCE] == 1)
    check("QA-reported failures → no pending", pending == [])


def test_run_iterate_pending_signal_multiple_cards():
    """Multiple cards with PENDING CI → all ADVANCE (CI no longer gates ADVANCE, per epic #1074)."""
    pp = _PendingProvider("pending")
    cards = [
        {
            "id": "t_a",
            "assignee": "developer-daedalus",
            "runs": [{"reason": "review-required: PR #1 shipped"}],
        },
        {
            "id": "t_b",
            "assignee": "developer-daedalus",
            "runs": [{"reason": "review-required: PR #2 shipped"}],
        },
    ]
    with mock.patch.object(kanban, "list_blocked", return_value=cards):
        with mock.patch.object(kanban, "complete", return_value=True):
            counts, prs, pending, _qa_f, *_ = iterate.run_iterate("slug", "O/R", provider=pp)
    check("multi pending → advance count 2", counts[iterate.ADVANCE] == 2)
    check("multi pending → prs captured", set(prs) == {1, 2})
    check("multi pending → no pending cards", pending == [])


# ── Issue #30: PENDING_SIGNAL classification ──────────────────────────────────────


def test_classify_blocked_pending_signal_returns_advance():
    """Developer + review-required + PR + CI PENDING → ADVANCE (CI no longer gates ADVANCE, per epic #1074)."""
    from core.providers.base import CIStatus
    result = iterate.classify_blocked(
        "developer-daedalus",
        "review-required: PR #42 shipped, waiting on CI",
        ci_green=False,
        pr_number=42,
        raw_ci=CIStatus.PENDING,
    )
    check("dev pending CI → advance (CI gated at merge-time)", result == iterate.ADVANCE)


def test_classify_blocked_red_ci_returns_advance():
    """Developer + review-required + PR + CI RED → ADVANCE (CI no longer gates ADVANCE, per epic #1074)."""
    from core.providers.base import CIStatus
    result = iterate.classify_blocked(
        "developer-daedalus",
        "review-required: PR #42 CI failing",
        ci_green=False,
        pr_number=42,
        raw_ci=CIStatus.RED,
    )
    check("dev QA-reported failures (explicit) → advance (CI gated at merge-time)", result == iterate.ADVANCE)


def test_classify_blocked_unknown_ci_returns_advance():
    """Developer + review-required + PR + CI UNKNOWN → ADVANCE (CI no longer gates ADVANCE, per epic #1074)."""
    from core.providers.base import CIStatus
    result = iterate.classify_blocked(
        "developer-daedalus",
        "review-required: PR #42",
        ci_green=False,
        pr_number=42,
        raw_ci=CIStatus.UNKNOWN,
    )
    check("dev unknown CI → advance (CI gated at merge-time)", result == iterate.ADVANCE)


def test_classify_blocked_default_raw_ci_backward_compat():
    """classify_blocked() with no raw_ci (default None) → ADVANCE for ci_green=False (per epic #1074)."""
    # CI no longer gates ADVANCE for developer cards — backward compat callers get ADVANCE
    result = iterate.classify_blocked(
        "developer-daedalus",
        "review-required: PR #42 CI failing",
        ci_green=False,
    )
    check("default raw_ci → advance (CI gated at merge-time)", result == iterate.ADVANCE)


def test_run_iterate_pending_signal_classified_correctly():
    """run_iterate: PENDING CI → ADVANCE (CI no longer gates ADVANCE, per epic #1074). Card is completed."""
    pp = _PendingProvider("pending")
    cards = [{
        "id": "t_dev",
        "assignee": "developer-daedalus",
        "runs": [{"reason": "review-required: PR #42 shipped"}],
    }]
    with mock.patch.object(kanban, "list_blocked", return_value=cards):
        with mock.patch.object(kanban, "complete", return_value=True):
            counts, prs, pending, _qa_f, *_ = iterate.run_iterate("slug", "O/R", provider=pp)
    check("pending CI → ADVANCE count 1", counts[iterate.ADVANCE] == 1)
    check("pending CI → QA_FIX count 0", counts[iterate.QA_FIX] == 0)
    check("pending CI → no pending cards", len(pending) == 0)


# ── qa-daedalus classify_blocked paths ──────────────────────────────────────


def test_classify_blocked_qa_passed():
    """QA card with qa-passed handoff + CI green → ADVANCE."""
    # Canonical prefix per #1125 F1: must start with "qa-passed:".
    result = iterate.classify_blocked(
        "qa-daedalus",
        "qa-passed: PR #5 — all tests pass, coverage adequate",
        ci_green=True,
    )
    check("qa qa-passed + green CI → advance", result == iterate.ADVANCE)


def test_classify_blocked_qa_failed():
    """QA card with qa-failed handoff → QA_FIX."""
    result = iterate.classify_blocked(
        "qa-daedalus",
        "review-required: qa-failed: PR #5 — lint failure in src/foo.py",
        ci_green=True,
    )
    check("qa qa-failed → qa_fix", result == iterate.QA_FIX)


def test_classify_blocked_qa_pending_signal():
    """QA card without explicit qa-passed/qa-failed signal → PENDING_SIGNAL fallback."""
    result = iterate.classify_blocked(
        "qa-daedalus",
        "review-required: qa-checking: PR #5",
        ci_green=True,
    )
    check("qa unspecified signal → pending_signal", result == iterate.PENDING_SIGNAL)


def test_classify_blocked_qa_failed_ci_red():
    """QA card with qa-failed + QA failure → still QA_FIX (CI doesn't gate QA)."""
    result = iterate.classify_blocked(
        "qa-daedalus",
        "review-required: qa-failed: PR #5 — test failures",
        ci_green=False,
    )
    check("qa qa-failed + QA-reported failures → qa_fix", result == iterate.QA_FIX)


# ── accessibility-daedalus classify_blocked paths ────────────────────────────


def test_classify_blocked_accessibility_approved():
    """Accessibility card with 'approved' in handoff → ADVANCE."""
    result = iterate.classify_blocked(
        "accessibility-daedalus",
        "review-required: approved: PR #5",
        ci_green=True,
    )
    check("accessibility approved → advance", result == iterate.ADVANCE)


def test_classify_blocked_accessibility_na():
    """Accessibility card with accessibility-na (no frontend changes) → ADVANCE."""
    result = iterate.classify_blocked(
        "accessibility-daedalus",
        "review-required: accessibility-na: PR #5 — no frontend files changed",
        ci_green=True,
    )
    check("accessibility-na → advance", result == iterate.ADVANCE)


def test_classify_blocked_accessibility_skipped():
    """Accessibility card with a11y-skipped (no UI changes) → ADVANCE."""
    result = iterate.classify_blocked(
        "accessibility-daedalus",
        "a11y-skipped: no UI changes in PR #7",
        ci_green=False,
    )
    check("a11y-skipped → advance", result == iterate.ADVANCE)


def test_classify_blocked_accessibility_changes_requested():
    """Accessibility card with 'changes requested' → PM_ROUTE."""
    result = iterate.classify_blocked(
        "accessibility-daedalus",
        "review-required: changes requested: PR #5 — missing alt text on hero image",
        ci_green=True,
    )
    check("accessibility changes requested → pm_route", result == iterate.PM_ROUTE)


def test_classify_blocked_accessibility_pending():
    """Accessibility card without a clear outcome → PENDING_SIGNAL."""
    result = iterate.classify_blocked(
        "accessibility-daedalus",
        "review-required: PR #5 audit in progress",
        ci_green=True,
    )
    check("accessibility unspecified signal → pending_signal", result == iterate.PENDING_SIGNAL)


def test_run_iterate_qa_passed_advances():
    """run_iterate: qa-daedalus with qa-passed → counts[ADVANCE] == 1."""
    from core.providers.base import CIStatus

    class _GreenProvider:
        name = "github"
        def get_pr_ci_status(self, pr_number):
            return CIStatus.GREEN
        def find_pr_for_branch(self, branch):
            return None
        def has_label(self, pr_number, label_name):
            return False

    cards = [{
        "id": "t_qa",
        "assignee": "qa-daedalus",
        # Canonical prefix per #1125 F1: must start with "qa-passed:".
        "runs": [{"reason": "qa-passed: PR #7 — all good"}],
        "workspace": "dir:/w",
    }]
    with mock.patch.object(kanban, "list_blocked", return_value=cards):
        with mock.patch.object(kanban, "complete", return_value=True):
            with mock.patch.object(kanban, "show_card", return_value=None):
                with mock.patch.object(kanban, "list_tasks", return_value=[]):
                    with mock.patch.object(kanban, "create_task", return_value="t_x"):
                        counts, prs, pending, _qa_f, *_ = iterate.run_iterate(
                            "slug", "O/R", provider=_GreenProvider(),
                        )
    check("run_iterate qa-passed → ADVANCE 1", counts[iterate.ADVANCE] == 1)
    check("run_iterate qa-passed → PR #7 in advance_prs", 7 in prs)
    check("run_iterate qa-passed → no pending", pending == [])


def test_run_iterate_accessibility_approved_advances():
    """run_iterate: accessibility-daedalus with approved handoff → ADVANCE."""
    from core.providers.base import CIStatus

    class _GreenProvider2:
        name = "github"
        def get_pr_ci_status(self, pr_number):
            return CIStatus.GREEN
        def find_pr_for_branch(self, branch):
            return None
        def has_label(self, pr_number, label_name):
            return False

    cards = [{
        "id": "t_a11y",
        "assignee": "accessibility-daedalus",
        "runs": [{"reason": "review-required: approved: PR #9"}],
        "workspace": "dir:/w",
    }]
    with mock.patch.object(kanban, "list_blocked", return_value=cards):
        with mock.patch.object(kanban, "complete", return_value=True):
            with mock.patch.object(kanban, "show_card", return_value=None):
                counts, prs, pending, _qa_f, *_ = iterate.run_iterate(
                    "slug", "O/R", provider=_GreenProvider2(),
                )
    check("run_iterate accessibility-approved → ADVANCE 1", counts[iterate.ADVANCE] == 1)
    check("run_iterate accessibility-approved → PR #9 in advance_prs", 9 in prs)


def test_run_iterate_accessibility_changes_requested_routes_to_pm():
    """run_iterate: accessibility-daedalus with changes requested → PM_ROUTE."""
    from core.providers.base import CIStatus

    class _GreenProvider3:
        name = "github"
        def get_pr_ci_status(self, pr_number):
            return CIStatus.GREEN
        def find_pr_for_branch(self, branch):
            return None
        def has_label(self, pr_number, label_name):
            return False

    cards = [{
        "id": "t_a11y2",
        "assignee": "accessibility-daedalus",
        "runs": [{"reason": "review-required: changes requested: PR #10 — missing alt text"}],
        "workspace": "dir:/w",
    }]
    with mock.patch.object(kanban, "list_blocked", return_value=cards):
        with mock.patch.object(kanban, "create_task", return_value="t_pm"):
            with mock.patch.object(kanban, "comment", return_value=True):
                with mock.patch.object(kanban, "block_task", return_value=True):
                    with mock.patch.object(kanban, "show_card", return_value=None):
                        counts, prs, pending, _qa_f, *_ = iterate.run_iterate(
                            "slug", "O/R", provider=_GreenProvider3(),
                        )
    check("run_iterate accessibility-changes → PM_ROUTE 1", counts[iterate.PM_ROUTE] == 1)


def test_run_iterate_qa_failed_creates_fix_card():
    """run_iterate: qa-daedalus with qa-failed → QA_FIX."""
    from core.providers.base import CIStatus

    class _GreenProvider4:
        name = "github"
        def get_pr_ci_status(self, pr_number):
            return CIStatus.GREEN
        def find_pr_for_branch(self, branch):
            return None
        def has_label(self, pr_number, label_name):
            return False

    cards = [{
        "id": "t_qa_fail",
        "assignee": "qa-daedalus",
        "runs": [{"reason": "review-required: qa-failed: PR #11 — test failure"}, {"metadata": {"fix_attempts": 0}}],
        "workspace": "dir:/w",
    }]
    with mock.patch.object(kanban, "list_blocked", return_value=cards):
        with mock.patch.object(kanban, "create_task", return_value="t_fix"):
            with mock.patch.object(kanban, "comment", return_value=True):
                with mock.patch.object(kanban, "show_card", return_value=None):
                    with mock.patch.object(kanban, "list_tasks", return_value=[]):
                        counts, prs, pending, _qa_f, *_ = iterate.run_iterate(
                            "slug", "O/R", provider=_GreenProvider4(),
                        )
    check("run_iterate qa-failed → QA_FIX 1", counts[iterate.QA_FIX] == 1)
    check("run_iterate qa-failed → no pending", pending == [])


def test_run_iterate_red_ci_classified_correctly():
    """run_iterate: RED CI → ADVANCE (CI no longer gates ADVANCE, per epic #1074)."""
    gp_red = _PendingProvider("red")
    cards = [{
        "id": "t_dev",
        "assignee": "developer-daedalus",
        "runs": [{"reason": "review-required: PR #42 QA failure"}, {"metadata": {"fix_attempts": 0}}],
        "workspace": "dir:/w",
    }]
    with mock.patch.object(kanban, "list_blocked", return_value=cards):
        with mock.patch.object(kanban, "complete", return_value=True):
            counts, prs, pending, _qa_f, *_ = iterate.run_iterate("slug", "O/R", provider=gp_red)
    check("QA-reported failures → ADVANCE count 1", counts[iterate.ADVANCE] == 1)
    check("QA-reported failures → QA_FIX count 0", counts[iterate.QA_FIX] == 0)
    check("QA-reported failures → no pending cards", pending == [])


# ── Issue #1405: QA "no PR" grace-poll ──────────────────────────────────────


def test_is_qa_no_pr_block_matches_no_pr_variant():
    """_is_qa_no_pr_block: only the 'no PR' variant of qa-failed matches (#1405)."""
    check("no-PR failure matches",
          iterate._is_qa_no_pr_block("qa-failed: no PR — developer work incomplete"))
    check("case-insensitive", iterate._is_qa_no_pr_block("QA-FAILED: No PR"))
    check("leading whitespace tolerated",
          iterate._is_qa_no_pr_block("  qa-failed: no pr yet"))
    check("real test failure does NOT match",
          not iterate._is_qa_no_pr_block("qa-failed: test_foo broke"))
    check("qa-passed does NOT match", not iterate._is_qa_no_pr_block("qa-passed: PR #7"))
    check("empty does NOT match", not iterate._is_qa_no_pr_block(""))
    check("no-pr text without qa-failed prefix does NOT match",
          not iterate._is_qa_no_pr_block("review-required: no PR yet"))


def test_grace_qa_no_pr_holds_when_no_pr():
    """_grace_qa_no_pr: no PR on the branch → 'holding', counter bumped, no unblock (#1405)."""
    import tempfile
    wd = tempfile.mkdtemp()
    fk = conftest.FakeKanban()
    prov = FakeProvider()  # no branch_prs → find_pr_for_issue returns None
    card = {"id": "t_qa", "body": "Issue O/R#77"}
    with mock.patch.object(iterate, "kanban", fk):
        result = iterate._grace_qa_no_pr(
            "slug", card, 77, prov, workdir=wd, max_grace_ticks=3,
        )
    check("no PR → holding", result == "holding")
    check("no unblock while holding", fk.unblocked_calls == [])
    check("grace counter bumped", iterate._read_qa_no_pr_grace(wd).get("t_qa") == 1)


def test_grace_qa_no_pr_adopts_late_pr():
    """_grace_qa_no_pr: a PR that appears on the branch → 'adopted', QA unblocked (#1405)."""
    import tempfile
    wd = tempfile.mkdtemp()
    fk = conftest.FakeKanban()
    prov = FakeProvider(branch_prs={"fix/issue-77": 99})
    fk.seed(assignee="qa-daedalus", title="QA #77", status="blocked", tid="t_qa")
    card = {"id": "t_qa", "body": "Issue O/R#77"}
    with mock.patch.object(iterate, "kanban", fk):
        result = iterate._grace_qa_no_pr(
            "slug", card, 77, prov, workdir=wd, max_grace_ticks=3,
        )
    check("PR appeared → adopted", result == "adopted")
    check("QA card unblocked", [c[0] for c in fk.unblocked_calls] == ["t_qa"])
    check("unblock reason names the PR", "PR #99" in fk.unblocked_calls[0][1])


def test_grace_qa_no_pr_adopts_suffixed_branch():
    """_grace_qa_no_pr: adopts a descriptive-suffix branch fix/issue-<n>-<slug> (#1405)."""
    import tempfile
    wd = tempfile.mkdtemp()
    fk = conftest.FakeKanban()
    prov = FakeProvider(branch_prs={"fix/issue-77-late-pr": 123})
    fk.seed(assignee="qa-daedalus", title="QA #77", status="blocked", tid="t_qa")
    card = {"id": "t_qa", "body": "Issue O/R#77"}
    with mock.patch.object(iterate, "kanban", fk):
        result = iterate._grace_qa_no_pr(
            "slug", card, 77, prov, workdir=wd, max_grace_ticks=3,
        )
    check("suffixed branch PR adopted", result == "adopted")
    check("QA card unblocked for suffixed branch", fk.unblocked_calls[0][0] == "t_qa")


def test_grace_qa_no_pr_exhausts_after_window():
    """_grace_qa_no_pr: after max_grace_ticks holds, returns 'exhausted' (#1405)."""
    import tempfile
    wd = tempfile.mkdtemp()
    fk = conftest.FakeKanban()
    prov = FakeProvider()  # never finds a PR
    card = {"id": "t_qa", "body": "Issue O/R#77"}
    with mock.patch.object(iterate, "kanban", fk):
        r1 = iterate._grace_qa_no_pr("slug", card, 77, prov, workdir=wd, max_grace_ticks=2)
        r2 = iterate._grace_qa_no_pr("slug", card, 77, prov, workdir=wd, max_grace_ticks=2)
        r3 = iterate._grace_qa_no_pr("slug", card, 77, prov, workdir=wd, max_grace_ticks=2)
    check("first tick holds", r1 == "holding")
    check("second tick holds", r2 == "holding")
    check("window exhausted on third tick", r3 == "exhausted")


def test_grace_qa_no_pr_dry_run_no_side_effects():
    """_grace_qa_no_pr: dry_run does not bump the counter or unblock (#1405)."""
    import tempfile
    wd = tempfile.mkdtemp()
    fk = conftest.FakeKanban()
    prov = FakeProvider(branch_prs={"fix/issue-77": 99})
    fk.seed(assignee="qa-daedalus", title="QA #77", status="blocked", tid="t_qa")
    card = {"id": "t_qa", "body": "Issue O/R#77"}
    with mock.patch.object(iterate, "kanban", fk):
        result = iterate._grace_qa_no_pr(
            "slug", card, 77, prov, workdir=wd, max_grace_ticks=3, dry_run=True,
        )
    check("dry-run still reports adopted", result == "adopted")
    check("dry-run does not unblock", fk.unblocked_calls == [])
    check("dry-run does not bump counter", iterate._read_qa_no_pr_grace(wd) == {})


def test_run_iterate_qa_no_pr_holds_and_skips_qa_fix():
    """run_iterate: QA 'no PR' block with no PR yet → held (grace), NOT QA_FIX (#1405)."""
    import tempfile
    wd = tempfile.mkdtemp()
    cards = [{
        "id": "t_qa",
        "assignee": "qa-daedalus",
        "body": "Issue O/R#77",
        "runs": [{"reason": "qa-failed: no PR — developer work incomplete"}],
    }]
    prov = FakeProvider(ci_status="unknown")  # no branch_prs → no PR found
    with mock.patch.object(kanban, "list_blocked", return_value=cards):
        with mock.patch.object(kanban, "list_tasks", return_value=[]):
            with mock.patch.object(kanban, "unblock_task", return_value=True) as munblock:
                with mock.patch.object(kanban, "create_task", return_value="t_fix") as mcreate:
                    counts, *_ = iterate.run_iterate(
                        "slug", "O/R", provider=prov,
                        resolved={"workdir": wd, "pipeline": {"qa_no_pr_grace_ticks": 3}},
                    )
    check("held by grace-poll", counts["qa_no_pr_grace"] == 1)
    check("no QA_FIX while holding", counts[iterate.QA_FIX] == 0)
    check("no unblock while holding", munblock.call_count == 0)
    check("no fix card created while holding", mcreate.call_count == 0)


def test_run_iterate_qa_no_pr_adopts_late_pr():
    """run_iterate: QA 'no PR' block but a PR appeared → adopt (unblock QA), NOT QA_FIX (#1405)."""
    import tempfile
    wd = tempfile.mkdtemp()
    cards = [{
        "id": "t_qa",
        "assignee": "qa-daedalus",
        "body": "Issue O/R#77",
        "runs": [{"reason": "qa-failed: no PR — developer work incomplete"}],
    }]
    prov = FakeProvider(ci_status="unknown", branch_prs={"fix/issue-77": 99})
    with mock.patch.object(kanban, "list_blocked", return_value=cards):
        with mock.patch.object(kanban, "list_tasks", return_value=[]):
            with mock.patch.object(kanban, "unblock_task", return_value=True) as munblock:
                counts, *_ = iterate.run_iterate(
                    "slug", "O/R", provider=prov,
                    resolved={"workdir": wd, "pipeline": {"qa_no_pr_grace_ticks": 3}},
                )
    check("adopted by grace-poll", counts["qa_no_pr_grace"] == 1)
    check("no QA_FIX on adopt", counts[iterate.QA_FIX] == 0)
    check("QA card unblocked to re-run", munblock.call_count == 1)


def test_run_iterate_qa_real_failure_unaffected_by_grace():
    """run_iterate: a genuine qa-failed (test failure) is untouched by the grace-poll (#1405)."""
    import tempfile
    wd = tempfile.mkdtemp()
    cards = [{
        "id": "t_qa",
        "assignee": "qa-daedalus",
        "body": "Issue O/R#77",
        "runs": [{"reason": "qa-failed: test_foo broke on PR #11"}],
        "workspace": "dir:/w",
    }]
    prov = FakeProvider(ci_status="unknown")
    with mock.patch.object(kanban, "list_blocked", return_value=cards):
        with mock.patch.object(kanban, "list_tasks", return_value=[]):
            with mock.patch.object(kanban, "show_card", return_value=None):
                with mock.patch.object(kanban, "create_task", return_value="t_fix"):
                    with mock.patch.object(kanban, "comment", return_value=True):
                        with mock.patch.object(kanban, "unblock_task", return_value=True) as munblock:
                            counts, *_ = iterate.run_iterate(
                                "slug", "O/R", provider=prov,
                                resolved={"workdir": wd, "pipeline": {"qa_no_pr_grace_ticks": 3}},
                            )
    check("grace not triggered for real failure", counts["qa_no_pr_grace"] == 0)
    check("real failure routes to QA_FIX", counts[iterate.QA_FIX] == 1)
    check("real failure does not unblock", munblock.call_count == 0)


def test_run_iterate_qa_no_pr_grace_disabled():
    """run_iterate: qa_no_pr_grace_ticks=0 disables the grace-poll — falls straight through (#1405)."""
    import tempfile
    wd = tempfile.mkdtemp()
    cards = [{
        "id": "t_qa",
        "assignee": "qa-daedalus",
        "body": "Issue O/R#77",
        "runs": [{"reason": "qa-failed: no PR — developer work incomplete"}],
    }]
    prov = FakeProvider(ci_status="unknown", branch_prs={"fix/issue-77": 99})
    with mock.patch.object(kanban, "list_blocked", return_value=cards):
        with mock.patch.object(kanban, "list_tasks", return_value=[]):
            with mock.patch.object(kanban, "unblock_task", return_value=True) as munblock:
                counts, *_ = iterate.run_iterate(
                    "slug", "O/R", provider=prov,
                    resolved={"workdir": wd, "pipeline": {"qa_no_pr_grace_ticks": 0}},
                )
    check("grace disabled → not counted", counts["qa_no_pr_grace"] == 0)
    check("grace disabled → no adopt/unblock", munblock.call_count == 0)


# ── Issue #35: escalation dedup tests ───────────────────────────────────────


def test_escalation_dedup_same_issue():
    """Two cards for same issue at max attempts → only one escalates, second is silently completed."""
    cards = [
        {
            "id": "t_dev1",
            "assignee": "developer-daedalus",
            "body": "benmarte/daedalus#35",
            "runs": [{"reason": "review-required: PR #50",
                      "metadata": {"fix_attempts": 3}}],
        },
        {
            "id": "t_rev1",
            "assignee": "reviewer-daedalus",
            "body": "benmarte/daedalus#35",
            "runs": [{"reason": "review-required: changes requested",
                      "metadata": {"fix_attempts": 3}}],
        },
    ]
    with mock.patch.object(kanban, "list_blocked", return_value=cards):
        with mock.patch.object(kanban, "comment", return_value=True) as mk_comment:
            with mock.patch.object(kanban, "complete", return_value=True) as mk_complete:
                # show_card returns no stamp, so _is_card_already_escalated returns False
                with mock.patch.object(kanban, "show_card", return_value={"id": "", "comments": []}):
                    with mock.patch.object(gp, "get_pr_ci_status", return_value="red"):
                        counts, *_ = iterate.run_iterate("slug", "O/R", provider=gp)

    # First card escalates, second is silently completed
    check("dedup: first card escalates", counts[iterate.ESCALATE] == 1)
    # The second card should be completed with 'skipped: escalated by' summary
    complete_calls = mk_complete.call_args_list
    # Expect exactly one complete call for the duplicate card (not the first)
    check("dedup: complete called for duplicate",
          len(complete_calls) == 1)
    if complete_calls:
        # complete takes (slug, tid, summary=...)
        call_kwargs = complete_calls[0][1] if len(complete_calls[0]) > 1 else {}
        call_args = complete_calls[0][0]
        summary = call_kwargs.get("summary", "")
        if len(call_args) > 2 and not summary:
            summary = call_args[2]
        check("dedup: skip summary references first_tid",
              "skipped: escalated by" in summary and "t_dev1" in summary)
    # Only one ESCALATE comment posted (the first one)
    escalate_comments = [c for c in mk_comment.call_args_list
                         if "ESCALATE" in str(c)]
    check("dedup: only one ESCALATE comment posted", len(escalate_comments) == 1)


def test_escalation_stamp_prevents_rerun():
    """Card with 'escalated: issue #N' stamp in comments is skipped on subsequent tick."""
    cards = [{
        "id": "t_dev",
        "assignee": "developer-daedalus",
        "body": "benmarte/daedalus#35",
        "runs": [{"reason": "review-required: PR #50",
                  "metadata": {"fix_attempts": 3}}],
    }]
    # show_card returns a stamp in comments (simulating a previous tick)
    stamped_card = {
        "id": "t_dev",
        "comments": [{"body": "escalated: issue #35"}],
        "latest_summary": "",
    }
    with mock.patch.object(kanban, "list_blocked", return_value=cards):
        with mock.patch.object(kanban, "show_card", return_value=stamped_card):
            with mock.patch.object(kanban, "comment", return_value=True) as mk_comment:
                with mock.patch.object(gp, "get_pr_ci_status", return_value="red"):
                    counts, *_ = iterate.run_iterate("slug", "O/R", provider=gp)

    check("stamp: no escalation counted", counts[iterate.ESCALATE] == 0)
    mk_comment.assert_not_called()


def test_escalation_dedup_dry_run():
    """Dry-run mode: first card logs 'would escalate', second logs 'would skip'."""
    cards = [
        {
            "id": "t_dev1",
            "assignee": "developer-daedalus",
            "body": "benmarte/daedalus#35",
            "runs": [{"reason": "review-required: PR #50",
                      "metadata": {"fix_attempts": 3}}],
        },
        {
            "id": "t_rev1",
            "assignee": "reviewer-daedalus",
            "body": "benmarte/daedalus#35",
            "runs": [{"reason": "review-required: changes requested",
                      "metadata": {"fix_attempts": 3}}],
        },
    ]
    with mock.patch.object(kanban, "list_blocked", return_value=cards):
        with mock.patch.object(kanban, "comment", return_value=True) as mk_comment:
            with mock.patch.object(kanban, "complete", return_value=True) as mk_complete:
                with mock.patch.object(kanban, "show_card", return_value={"id": "", "comments": []}):
                    with mock.patch.object(gp, "get_pr_ci_status", return_value="red"):
                        counts, *_ = iterate.run_iterate("slug", "O/R", provider=gp, dry_run=True)

    # In dry-run, no side effects
    mk_comment.assert_not_called()
    mk_complete.assert_not_called()
    # First card still counted as ESCALATE in counts (dry-run path still increments)
    check("dry-run: first ESCALATE counted", counts[iterate.ESCALATE] == 1)


def test_escalation_different_issues_independent():
    """Cards for different issues escalate independently (no dedup)."""
    cards = [
        {
            "id": "t_dev1",
            "assignee": "developer-daedalus",
            "body": "benmarte/daedalus#35",
            "runs": [{"reason": "review-required: PR #50",
                      "metadata": {"fix_attempts": 3}}],
        },
        {
            "id": "t_dev2",
            "assignee": "developer-daedalus",
            "body": "benmarte/daedalus#36",
            "runs": [{"reason": "review-required: PR #51",
                      "metadata": {"fix_attempts": 3}}],
        },
    ]
    with mock.patch.object(kanban, "list_blocked", return_value=cards):
        with mock.patch.object(kanban, "comment", return_value=True):
            with mock.patch.object(kanban, "show_card", return_value={"id": "", "comments": []}):
                with mock.patch.object(gp, "get_pr_ci_status", return_value="red"):
                    counts, *_ = iterate.run_iterate("slug", "O/R", provider=gp)

    # Both issues are independent, both should escalate
    check("independent: both escalate", counts[iterate.ESCALATE] == 2)


def test_execute_escalate_stamps_card():
    """_execute_escalate posts both ESCALATE comment and escalation stamp."""
    with mock.patch.object(kanban, "comment", return_value=True) as mk_comment:
        card = {"id": "t_stuck", "body": "benmarte/daedalus#35"}
        ok = iterate._execute_escalate("slug", card, "O/R", "review-required: PR #42")
    check("escalate returns True", ok is True)
    # Two comments: the ESCALATE msg + the stamp
    check("stamp: two comments posted", mk_comment.call_count == 2)
    # Second comment should be the stamp
    stamp_call = mk_comment.call_args_list[1]
    check("stamp comment body", "escalated: issue #35" in stamp_call[0][2])


def test_execute_escalate_no_stamp_without_issue_number():
    """_execute_escalate skips stamp when card has no extractable issue number."""
    with mock.patch.object(kanban, "comment", return_value=True) as mk_comment:
        card = {"id": "t_stuck", "body": "task with no issue reference"}
        ok = iterate._execute_escalate("slug", card, "O/R", "review-required: PR #42")
    check("escalate returns True", ok is True)
    # Only one comment (the ESCALATE msg), no stamp
    check("no stamp without issue number", mk_comment.call_count == 1)


def test_extract_issue_number_from_card_basic():
    """_extract_issue_number_from_card resolves issue from body."""
    card = {"body": "Implement issue benmarte/daedalus#35 in the repo."}
    n = iterate._extract_issue_number_from_card(card)
    check("extracts issue #35 from repo-qualified ref", n == 35)


def test_extract_issue_number_from_card_none():
    """_extract_issue_number_from_card returns None when no body."""
    card = {"body": ""}
    check("empty body → None", iterate._extract_issue_number_from_card(card) is None)


# ── _execute_pm_route awaiting-pr guard (issue #87) ──────────────────────────


def test_execute_pm_route_skips_awaiting_pr():
    """_execute_pm_route must return False and create nothing when block is awaiting-pr."""
    with mock.patch.object(kanban, "create_task") as mk_create:
        card = {"id": "t_dev", "runs": [], "workspace": "dir:/w"}
        ok = iterate._execute_pm_route(
            "slug", card, "org/repo",
            "review-required: awaiting-pr — Claude Code spawned, PR pending",
            router_profile="project-manager-daedalus",
        )
    check("awaiting-pr → pm_route returns False", ok is False)
    check("awaiting-pr → no task created", mk_create.call_count == 0)


def test_execute_pm_route_skips_awaiting_pr_case_insensitive():
    """awaiting-pr guard is case-insensitive."""
    with mock.patch.object(kanban, "create_task") as mk_create:
        card = {"id": "t_dev", "runs": [], "workspace": "dir:/w"}
        ok = iterate._execute_pm_route(
            "slug", card, "org/repo",
            "review-required: AWAITING-PR — Claude Code spawned",
            router_profile="project-manager-daedalus",
        )
    check("AWAITING-PR uppercase skipped", ok is False)
    check("no task created for uppercase variant", mk_create.call_count == 0)


def test_execute_pm_route_proceeds_for_changes_requested():
    """pm_route still fires when handoff is changes-requested (not awaiting-pr)."""
    with mock.patch.object(kanban, "create_task", return_value="t_pm") as mk_create:
        with mock.patch.object(kanban, "comment", return_value=True):
            with mock.patch.object(kanban, "block_task", return_value=True):
                card = {"id": "t_rev", "runs": [], "workspace": "dir:/w"}
                ok = iterate._execute_pm_route(
                    "slug", card, "org/repo",
                    "review-required: PR #91 — CHANGES REQUESTED: improve test coverage",
                    router_profile="project-manager-daedalus",
                )
    check("changes-requested still creates PM card", ok is True)
    check("PM card was created", mk_create.call_count == 1)


if __name__ == "__main__":
    print("Iterate (CI-aware auto-advance) tests")
    print("-" * 60)
    for fn in (
        test_classify_blocked_dev_green,
        test_classify_blocked_dev_red,
        test_classify_blocked_dev_advance_all_ci_states,
        test_classify_blocked_dev_escalate,
        test_classify_blocked_honors_custom_max_fix_attempts,
        test_classify_blocked_dev_no_pr,
        test_classify_blocked_dev_pr_merged_reconciles,
        test_classify_blocked_dev_pr_merged_precedes_not_open,
        test_classify_blocked_dev_pr_not_merged_still_holds,
        test_execute_reconcile_merged,
        test_execute_reconcile_merged_no_issue_number,
        test_execute_reconcile_merged_dry_run,
        test_run_iterate_dev_pr_merged_reconciles,
        test_classify_blocked_reviewer_changes,
        test_classify_blocked_reviewer_approved,
        test_classify_blocked_reviewer_escalate,
        test_classify_blocked_security_approved,
        test_classify_blocked_security_findings,
        test_classify_blocked_unknown_assignee,
        test_classify_blocked_empty_handoff,
        test_classify_blocked_variant_approval,
        test_classify_blocked_variant_changes,
        test_parse_handoff_pr,
        test_parse_handoff_signals,
        test_count_fix_attempts,
        test_count_fix_attempts_board_count,
        test_fix_attempts_persistence,
        test_count_fix_attempts_pm_route_key,
        test_handoff_from_card,
        test_execute_advance,
        test_execute_qa_fix,
        test_execute_pm_route,
        test_execute_pm_route_empty_profile_fallback,
        test_execute_approve_advance,
        test_execute_escalate,
        test_execute_dev_fix_escalate_when_over_cap,
        test_check_and_maybe_escalate_below_threshold,
        test_check_and_maybe_escalate_over_threshold,
        test_run_iterate_empty,
        test_run_iterate_dev_advance,
        test_run_iterate_qa_fix,
        test_run_iterate_reviewer_changes,
        test_run_iterate_reviewer_approved,
        test_run_iterate_escalate,
        test_run_iterate_mixed,
        test_run_iterate_dry_run,
        test_run_iterate_ci_cache,
        test_execute_advance_unblocks_reviewer,
        test_execute_advance_ignores_other_blocks,
        test_human_summary_format,
        test_human_summary_no_routed_actions,
        test_human_summary_empty,
        test_human_summary_pm_route,
        test_diagnostics_parses_json,
        test_diagnostics_nonzero_returns_empty,
        test_diagnostics_non_json_returns_empty,
        test_create_task_passes_goal,
        test_create_task_passes_goal_max_turns,
        test_create_task_no_goal_by_default,
        test_run_iterate_falls_back_to_show_card_for_handoff,
        test_run_iterate_show_card_fallback_skip_on_failure,
        test_run_iterate_show_card_no_latest_summary,
        test_create_downstream_happy_path,
        test_create_downstream_qa_gate_recovered_parent,
        test_create_downstream_idempotency_guard,
        test_create_downstream_partial_idempotency,
        test_create_downstream_dedup_respects_status,
        test_create_downstream_dedup_multiple_statuses,
        test_create_downstream_dedup_done_status,
        test_create_downstream_dry_run,
        test_execute_advance_triggers_downstream,
        test_execute_advance_skip_downstream_no_issue_number,
        test_downstream_task_body_references_pr,
        test_classify_blocked_planner_returns_pm_route,
        test_classify_blocked_documentation_docs_posted_returns_approve_advance,
        test_classify_blocked_documentation_unknown_returns_pm_route,
        test_classify_blocked_pm_returns_escalate,
        test_extract_issue_number_repo_qualified,
        test_extract_issue_number_bare_hash,
        test_extract_issue_number_none,
        test_extract_issue_number_prefers_repo_qualified,
        test_run_iterate_no_ci_provider_advances_immediately,
        test_run_iterate_pending_signal_returns_pending_cards,
        test_run_iterate_green_ci_no_pending_cards,
        test_run_iterate_red_ci_no_pending_cards,
        test_run_iterate_pending_signal_multiple_cards,
        test_classify_blocked_pending_signal_returns_advance,
        test_classify_blocked_red_ci_returns_advance,
        test_classify_blocked_unknown_ci_returns_advance,
        test_classify_blocked_default_raw_ci_backward_compat,
        test_run_iterate_pending_signal_classified_correctly,
        test_run_iterate_red_ci_classified_correctly,
        test_classify_blocked_qa_passed,
        test_classify_blocked_qa_failed,
        test_classify_blocked_qa_pending_signal,
        test_classify_blocked_qa_failed_ci_red,
        test_classify_blocked_accessibility_approved,
        test_classify_blocked_accessibility_na,
        test_classify_blocked_accessibility_changes_requested,
        test_classify_blocked_accessibility_pending,
        test_run_iterate_qa_passed_advances,
        test_run_iterate_accessibility_approved_advances,
        test_run_iterate_accessibility_changes_requested_routes_to_pm,
        test_run_iterate_qa_failed_creates_fix_card,
        test_is_qa_no_pr_block_matches_no_pr_variant,
        test_grace_qa_no_pr_holds_when_no_pr,
        test_grace_qa_no_pr_adopts_late_pr,
        test_grace_qa_no_pr_adopts_suffixed_branch,
        test_grace_qa_no_pr_exhausts_after_window,
        test_grace_qa_no_pr_dry_run_no_side_effects,
        test_run_iterate_qa_no_pr_holds_and_skips_qa_fix,
        test_run_iterate_qa_no_pr_adopts_late_pr,
        test_run_iterate_qa_real_failure_unaffected_by_grace,
        test_run_iterate_qa_no_pr_grace_disabled,
        test_escalation_dedup_same_issue,
        test_escalation_stamp_prevents_rerun,
        test_escalation_dedup_dry_run,
        test_escalation_different_issues_independent,
        test_execute_escalate_stamps_card,
        test_execute_escalate_no_stamp_without_issue_number,
    ):
        fn()
    print()
    print("_execute_pm_route awaiting-pr guard tests (issue #87)")
    print("-" * 60)
    for fn in (
        test_execute_pm_route_skips_awaiting_pr,
        test_execute_pm_route_skips_awaiting_pr_case_insensitive,
        test_execute_pm_route_proceeds_for_changes_requested,
    ):
        fn()
    print("-" * 60)
    print(f"Results: {conftest._passed} passed, {conftest._failed} failed")
    sys.exit(1 if conftest._failed else 0)
