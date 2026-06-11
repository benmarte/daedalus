"""Table-driven tests for CI-aware auto-advance routing and self-healing loop.

Tests core.iterate: classify_blocked, action executors, and the main loop.
Follows the same pattern as test_daedalus.py: plain Python with a check() helper.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

# Make the package root importable (config/, core/).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import iterate  # noqa: E402
from core import kanban  # noqa: E402
class _FakeProvider:
    """Stands in for a core.providers.VCSProvider in run_iterate calls."""

    name = "github"

    def pr_ci_green(self, pr_number):
        return False

    def find_pr_for_branch(self, branch):
        return None


gp = _FakeProvider()  # patched per-test via mock.patch.object

_passed = 0
_failed = 0


def check(name, cond):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {name}")
    else:
        _failed += 1
        print(f"  FAIL  {name}")


def _load_dispatch():
    """Load the daedalus_dispatch module (for _human_summary tests)."""
    import importlib.util
    p = Path(__file__).resolve().parent.parent / "scripts" / "daedalus_dispatch.py"
    spec = importlib.util.spec_from_file_location("disp", str(p))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── classify_blocked: pure function ──────────────────────────────────────────


def test_classify_blocked_dev_green():
    """Developer + review-required with PR + CI green → advance."""
    result = iterate.classify_blocked(
        "developer",
        "review-required: PR #42 shipped, all tests pass",
        ci_green=True,
    )
    check("dev green CI → advance", result == iterate.ADVANCE)


def test_classify_blocked_dev_red():
    """Developer + review-required with PR + CI red → dev_fix_ci."""
    result = iterate.classify_blocked(
        "developer",
        "review-required: PR #42 — CI failing",
        ci_green=False,
    )
    check("dev red CI → dev_fix_ci", result == iterate.DEV_FIX_CI)


def test_classify_blocked_dev_escalate():
    """Developer + CI red + fix_attempts >= max → escalate."""
    result = iterate.classify_blocked(
        "developer",
        "review-required: PR #42 — CI failing",
        ci_green=False,
        fix_attempts=3,
    )
    check("dev over max → escalate", result == iterate.ESCALATE)

    result2 = iterate.classify_blocked(
        "developer",
        "review-required: PR #42",
        ci_green=True,
        fix_attempts=5,
    )
    check("dev over max (green CI) → escalate too", result2 == iterate.ESCALATE)


def test_classify_blocked_dev_no_pr():
    """Developer + no PR in handoff → empty string (no action)."""
    result = iterate.classify_blocked(
        "developer",
        "some other block reason",
        ci_green=True,
    )
    check("dev no PR → no action", result == "")


def test_classify_blocked_reviewer_changes():
    """Reviewer + changes requested → pm_route."""
    result = iterate.classify_blocked(
        "reviewer",
        "review-required: CHANGES REQUESTED — SQL injection in api/search.py",
        ci_green=True,
    )
    check("reviewer changes requested → pm_route", result == iterate.PM_ROUTE)


def test_classify_blocked_reviewer_approved():
    """Reviewer + approved → approve_advance."""
    result = iterate.classify_blocked(
        "reviewer",
        "review-required: APPROVED. PR #42 looks good.",
        ci_green=True,
    )
    check("reviewer approved → approve_advance", result == iterate.APPROVE_ADVANCE)


def test_classify_blocked_reviewer_escalate():
    """Reviewer + changes requested + fix_attempts >= max → escalate."""
    result = iterate.classify_blocked(
        "reviewer",
        "review-required: CHANGES REQUESTED",
        ci_green=True,
        fix_attempts=3,
    )
    check("reviewer over max → escalate", result == iterate.ESCALATE)


def test_classify_blocked_security_approved():
    """Security-analyst + approved → approve_advance."""
    result = iterate.classify_blocked(
        "security-analyst",
        "review-required: No findings. Approved for merge.",
        ci_green=True,
    )
    check("security approved → approve_advance", result == iterate.APPROVE_ADVANCE)


def test_classify_blocked_security_findings():
    """Security-analyst + blocking findings → pm_route."""
    result = iterate.classify_blocked(
        "security-analyst",
        "review-required: BLOCKING FINDINGS — hardcoded secret in config.py",
        ci_green=True,
    )
    check("security findings → pm_route", result == iterate.PM_ROUTE)


def test_classify_blocked_unknown_assignee():
    """Unknown assignee → no action."""
    result = iterate.classify_blocked(
        "documentation",
        "review-required: APPROVED",
        ci_green=True,
    )
    check("unknown assignee → no action", result == "")


def test_classify_blocked_empty_handoff():
    """Empty handoff → no action."""
    result = iterate.classify_blocked("developer", "", ci_green=True)
    check("empty handoff → no action", result == "")


def test_classify_blocked_variant_approval():
    """Various approval phrasings all → approve_advance."""
    for text in (
        "review: LGTM, PR #42.",
        "review-required: sign-off given.",
        "looks good to me, approved.",
        "no findings, :+1:",
        "approved. merge when ready.",
    ):
        result = iterate.classify_blocked("reviewer", text, ci_green=True)
        check(f"approval phrase '{text[:40]}' → approve_advance", result == iterate.APPROVE_ADVANCE)


def test_classify_blocked_variant_changes():
    """Various change-request phrasings all → pm_route."""
    for text in (
        "review: CHANGES REQUESTED — fix the null check.",
        "blocking findings in auth module: needs fixes.",
        "request changes: missing error handling.",
        "changes required before approval.",
    ):
        result = iterate.classify_blocked("reviewer", text, ci_green=True)
        check(f"changes phrase '{text[:40]}' → pm_route", result == iterate.PM_ROUTE)


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


def test_execute_dev_fix_ci():
    """_execute_dev_fix_ci creates a fix task with idempotency key."""
    with mock.patch.object(kanban, "create_task", return_value="t_fix") as mk_create:
        with mock.patch.object(kanban, "comment", return_value=True) as mk_comment:
            card = {
                "id": "t_dev",
                "runs": [{"metadata": {"fix_attempts": 0}}],
                "workspace": "dir:/tmp",
            }
            ok = iterate._execute_dev_fix_ci(
                "slug", card, "O/R", "review-required: PR #55 CI failing",
            )
    check("dev_fix_ci returns True", ok is True)
    mk_create.assert_called_once()
    # Check idempotency key includes attempt number
    call_args = mk_create.call_args[1]
    check("idempotency key has attempt 1",
          "attempt-1" in call_args["idempotency_key"])
    check("assignee is developer", call_args["assignee"] == "developer")


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
                    router_profile="project-manager",
                )
    check("pm_route returns True", ok is True)
    mk_create.assert_called_once()
    # Verify PM card is created with goal=True
    pos_args, call_kwargs = mk_create.call_args
    check("pm card assignee is project-manager", call_kwargs["assignee"] == "project-manager")
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
    check("fallback assignee is developer", call_kwargs["assignee"] == "developer")
    check("fallback does NOT use goal", call_kwargs.get("goal") != True)


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
    """_execute_dev_fix_ci escalates when fix_attempts >= MAX."""
    card = {
        "id": "t_dev",
        "runs": [{"metadata": {"fix_attempts": 3}}],
    }
    with mock.patch.object(kanban, "comment", return_value=True) as mk_comment:
        ok = iterate._execute_dev_fix_ci(
            "slug", card, "O/R", "review-required: PR #42 CI failing",
        )
    check("dev_fix_ci escalates when over cap", ok is True)
    mk_comment.assert_called_once()
    # Make sure no create was called
    assert "escalate" in mk_comment.call_args[0][2].lower()


# ── run_iterate (main loop) ─────────────────────────────────────────────────


def test_run_iterate_empty():
    """run_iterate with no blocked cards returns zero counts."""
    with mock.patch.object(kanban, "list_blocked", return_value=[]):
        counts, prs = iterate.run_iterate("slug", "O/R", provider=gp)
    check("empty board → all zeros", all(v == 0 for v in counts.values()))
    check("empty board → no prs", prs == [])


def test_run_iterate_dev_advance():
    """Blocked dev card with green CI → advance."""
    cards = [{
        "id": "t_dev",
        "assignee": "developer",
        "runs": [{"reason": "review-required: PR #42 shipped"}],
    }]
    with mock.patch.object(kanban, "list_blocked", return_value=cards):
        with mock.patch.object(kanban, "complete", return_value=True):
            with mock.patch.object(gp, "pr_ci_green", return_value=True):
                counts, prs = iterate.run_iterate("slug", "O/R", provider=gp)
    check("dev green CI → advance count 1", counts[iterate.ADVANCE] == 1)
    check("no other actions", sum(v for v in counts.values() if v > 0) == 1)
    check("advance PR is 42", prs == [42])


def test_run_iterate_dev_fix_ci():
    """Blocked dev card with red CI → dev_fix_ci."""
    cards = [{
        "id": "t_dev",
        "assignee": "developer",
        "runs": [{"reason": "review-required: PR #42 CI red"}, {"metadata": {"fix_attempts": 0}}],
        "workspace": "dir:/w",
    }]
    with mock.patch.object(kanban, "list_blocked", return_value=cards):
        with mock.patch.object(kanban, "create_task", return_value="t_fix"):
            with mock.patch.object(kanban, "comment", return_value=True):
                with mock.patch.object(gp, "pr_ci_green", return_value=False):
                    counts, _ = iterate.run_iterate("slug", "O/R", provider=gp)
    check("dev red CI → dev_fix_ci count 1", counts[iterate.DEV_FIX_CI] == 1)


def test_run_iterate_reviewer_changes():
    """Blocked reviewer card with changes requested → pm_route."""
    cards = [{
        "id": "t_rev",
        "assignee": "reviewer",
        "runs": [{"reason": "review-required: CHANGES REQUESTED — fix auth"}, {"metadata": {"fix_attempts": 0}}],
        "workspace": "dir:/w",
    }]
    with mock.patch.object(kanban, "list_blocked", return_value=cards):
        with mock.patch.object(kanban, "create_task", return_value="t_pm"):
            with mock.patch.object(kanban, "comment", return_value=True):
                with mock.patch.object(kanban, "block_task", return_value=True):
                    with mock.patch.object(gp, "pr_ci_green", return_value=True):
                        counts, _ = iterate.run_iterate("slug", "O/R", provider=gp)
    check("reviewer changes → pm_route count 1", counts[iterate.PM_ROUTE] == 1)


def test_run_iterate_reviewer_approved():
    """Blocked reviewer card with approved → approve_advance."""
    cards = [{
        "id": "t_rev",
        "assignee": "reviewer",
        "runs": [{"reason": "review-required: APPROVED"}],
    }]
    with mock.patch.object(kanban, "list_blocked", return_value=cards):
        with mock.patch.object(kanban, "complete", return_value=True):
            with mock.patch.object(gp, "pr_ci_green", return_value=True):
                counts, _ = iterate.run_iterate("slug", "O/R", provider=gp)
    check("reviewer approved → approve_advance count 1", counts[iterate.APPROVE_ADVANCE] == 1)


def test_run_iterate_escalate():
    """Blocked card with fix_attempts >= MAX → escalate."""
    cards = [{
        "id": "t_dev",
        "assignee": "developer",
        "runs": [
            {"reason": "review-required: PR #42 CI red",
             "metadata": {"fix_attempts": 3}},  # at cap
        ],
    }]
    with mock.patch.object(kanban, "list_blocked", return_value=cards):
        with mock.patch.object(kanban, "comment", return_value=True):
            with mock.patch.object(gp, "pr_ci_green", return_value=False):
                counts, _ = iterate.run_iterate("slug", "O/R", provider=gp)
    check("over cap → escalate count 1", counts[iterate.ESCALATE] == 1)


def test_run_iterate_mixed():
    """Multiple blocked cards produce mixed action counts."""
    cards = [
        {
            "id": "t_dev_green",
            "assignee": "developer",
            "runs": [{"reason": "review-required: PR #1"}],
        },
        {
            "id": "t_rev_approved",
            "assignee": "reviewer",
            "runs": [{"reason": "review-required: APPROVED PR #1"}],
        },
        {
            "id": "t_unknown",
            "assignee": "documentation",
            "runs": [{"reason": "some block"}],
        },
    ]
    with mock.patch.object(kanban, "list_blocked", return_value=cards):
        with mock.patch.object(kanban, "complete", return_value=True):
            with mock.patch.object(gp, "pr_ci_green", return_value=True):
                counts, _ = iterate.run_iterate("slug", "O/R", provider=gp)
    check("mixed: advance count 1", counts[iterate.ADVANCE] == 1)
    check("mixed: approve_advance count 1", counts[iterate.APPROVE_ADVANCE] == 1)
    check("mixed: no other actions", counts[iterate.DEV_FIX_CI] == 0
          and counts[iterate.PM_ROUTE] == 0
          and counts[iterate.ESCALATE] == 0)


def test_run_iterate_dry_run():
    """dry_run=True does not call mutating kanban methods."""
    cards = [{
        "id": "t_dev",
        "assignee": "developer",
        "runs": [{"reason": "review-required: PR #1"}],
    }]
    with mock.patch.object(kanban, "list_blocked", return_value=cards):
        with mock.patch.object(kanban, "complete") as mk_complete:
            with mock.patch.object(gp, "pr_ci_green", return_value=True):
                counts, _ = iterate.run_iterate("slug", "O/R", provider=gp, dry_run=True)
    check("dry_run: advance counted", counts[iterate.ADVANCE] == 1)
    check("dry_run: complete NOT called", mk_complete.call_count == 0)


# ── PR→CI cache ──────────────────────────────────────────────────────────────


def test_run_iterate_ci_cache():
    """Two cards referencing the same PR → only one provider pr_ci_green call."""
    cards = [
        {
            "id": "t_a",
            "assignee": "developer",
            "runs": [{"reason": "review-required: PR #42"}],
        },
        {
            "id": "t_b",
            "assignee": "reviewer",
            "runs": [{"reason": "APPROVED PR #42"}],
        },
    ]
    with mock.patch.object(kanban, "list_blocked", return_value=cards):
        with mock.patch.object(kanban, "complete", return_value=True):
            with mock.patch.object(gp, "pr_ci_green", return_value=True) as mk_ci:
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
            "routed_actions": {"dev_fix_ci": 1, "escalate": 2},
            "reconciled": [("5", "In review")],
        }
    }
    msg = disp._human_summary(summaries)
    check("summary mentions PR #42", "#42" in msg)
    check("summary mentions #99", "#99" in msg)
    check("summary does NOT contain count tuples", "count" not in msg)
    check("summary has ci-fix count", "ci-fix:1" in msg)
    check("summary has escalate count", "escalate:2" in msg)
    check("summary has dispatched issues", "#1" in msg and "#2" in msg)
    check("summary has closed issues", "✅" in msg and "#3" in msg)
    check("summary has reconciled status", "5→In review" in msg)


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
            "routed_actions": {"pm_route": 2, "dev_fix_ci": 1},
        }
    }
    msg = disp._human_summary(summaries)
    check("summary has pm-route count", "pm-route:2" in msg)
    check("summary has ci-fix count", "ci-fix:1" in msg)
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
        kanban.create_task("slug", "title", assignee="developer", goal=True)
    args = mk.call_args[0][0]
    check("--goal in create_task args", "--goal" in args)


def test_create_task_passes_goal_max_turns():
    """kanban.create_task passes --goal-max-turns when goal_max_turns is set."""
    with mock.patch("core.kanban._hk", return_value=(0, "t_test", "")) as mk:
        kanban.create_task("slug", "title", assignee="developer", goal=True, goal_max_turns=10)
    args = mk.call_args[0][0]
    check("--goal in args", "--goal" in args)
    check("--goal-max-turns 10 in args", "--goal-max-turns" in args and "10" in args)


def test_create_task_no_goal_by_default():
    """kanban.create_task NOT passes --goal when goal=False (default)."""
    with mock.patch("core.kanban._hk", return_value=(0, "t_test", "")) as mk:
        kanban.create_task("slug", "title", assignee="developer")
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
        "assignee": "developer",
    }]
    # show_card returns full detail with latest_summary
    full_card = {
        "latest_summary": "review-required: PR #42 shipped, CI green",
    }
    with mock.patch.object(kanban, "list_blocked", return_value=minimal_cards):
        with mock.patch.object(kanban, "show_card", return_value=full_card) as mk_show:
            with mock.patch.object(kanban, "complete", return_value=True):
                with mock.patch.object(gp, "pr_ci_green", return_value=True):
                    counts, prs = iterate.run_iterate("slug", "O/R", provider=gp)
    check("show_card was called for the blocked card", mk_show.call_count == 1)
    check("show_card called with slug and tid", mk_show.call_args == mock.call("slug", "t_dev"))
    check("dev green CI → advance count 1", counts[iterate.ADVANCE] == 1)
    check("advance PR is 42", prs == [42])


def test_run_iterate_show_card_fallback_skip_on_failure():
    """show_card returning None → card skipped (graceful degradation)."""
    minimal_cards = [{
        "id": "t_dev",
        "assignee": "developer",
    }]
    with mock.patch.object(kanban, "list_blocked", return_value=minimal_cards):
        with mock.patch.object(kanban, "show_card", return_value=None):
            with mock.patch.object(gp, "pr_ci_green", return_value=True):
                counts, _ = iterate.run_iterate("slug", "O/R", provider=gp)
    check("show_card None → no actions", all(v == 0 for v in counts.values()))


def test_run_iterate_show_card_no_latest_summary():
    """show_card returns dict without latest_summary → card skipped."""
    minimal_cards = [{
        "id": "t_dev",
        "assignee": "developer",
    }]
    with mock.patch.object(kanban, "list_blocked", return_value=minimal_cards):
        with mock.patch.object(kanban, "show_card", return_value={"id": "t_dev"}):
            with mock.patch.object(gp, "pr_ci_green", return_value=True):
                counts, _ = iterate.run_iterate("slug", "O/R", provider=gp)
    check("no latest_summary → no actions", all(v == 0 for v in counts.values()))


# ── router_profile config ────────────────────────────────────────────────────


def test_run_iterate_respects_router_profile_config():
    """run_iterate passes router_profile from resolved config to executor."""
    cards = [{
        "id": "t_rev",
        "assignee": "reviewer",
        "runs": [{"reason": "review-required: CHANGES REQUESTED — fix X"}, {"metadata": {"fix_attempts": 0}}],
        "workspace": "dir:/w",
    }]
    with mock.patch.object(kanban, "list_blocked", return_value=cards):
        with mock.patch.object(kanban, "create_task", return_value="t_pm") as mk_create:
            with mock.patch.object(kanban, "comment", return_value=True):
                with mock.patch.object(kanban, "block_task", return_value=True):
                    with mock.patch.object(gp, "pr_ci_green", return_value=True):
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
        "assignee": "reviewer",
        "runs": [{"reason": "review-required: CHANGES REQUESTED — fix X"}, {"metadata": {"fix_attempts": 0}}],
        "workspace": "dir:/w",
    }]
    with mock.patch.object(kanban, "list_blocked", return_value=cards):
        with mock.patch.object(kanban, "create_task", return_value="t_pm") as mk_create:
            with mock.patch.object(kanban, "comment", return_value=True):
                with mock.patch.object(kanban, "block_task", return_value=True):
                    with mock.patch.object(gp, "pr_ci_green", return_value=True):
                        iterate.run_iterate("slug", "O/R", provider=gp, resolved={})
    call_kwargs = mk_create.call_args[1]
    check("default router_profile → assignee is project-manager", call_kwargs["assignee"] == "project-manager")


# ── branch→PR fallback ────────────────────────────────────────────────────────


def test_classify_blocked_pr_number_fallback():
    """classify_blocked uses pr_number kwarg when handoff has no PR."""
    # handoff has no PR, but pr_number=42 provided
    result = iterate.classify_blocked(
        "developer",
        "review-required: all tests pass, CI green",
        ci_green=True,
        pr_number=42,
    )
    check("classify pr_number fallback → advance", result == iterate.ADVANCE)

    # handoff WITH PR still wins (handoff takes priority)
    result2 = iterate.classify_blocked(
        "developer",
        "review-required: PR #99 shipped",
        ci_green=True,
        pr_number=42,  # should be ignored; handoff's #99 wins
    )
    check("handoff PR wins over pr_number fallback", result2 == iterate.ADVANCE)


def test_run_iterate_branch_pr_fallback():
    """run_iterate advances a dev card whose handoff has no PR but branch resolves to one."""
    cards = [{
        "id": "t_dev",
        "assignee": "developer",
        "branch_name": "feat/my-feature",
        "runs": [{"reason": "review-required: CI green, shipped"}],
    }]
    with mock.patch.object(kanban, "list_blocked", return_value=cards):
        with mock.patch.object(kanban, "complete", return_value=True):
            with mock.patch.object(gp, "find_pr_for_branch", return_value=42) as mk_branch:
                with mock.patch.object(gp, "pr_ci_green", return_value=True):
                    counts, prs = iterate.run_iterate("slug", "O/R", provider=gp)
    check("branch PR fallback → advance count 1", counts[iterate.ADVANCE] == 1)
    check("branch PR fallback → PR is 42", prs == [42])
    mk_branch.assert_called_once_with("feat/my-feature")


def test_run_iterate_branch_pr_fallback_no_match():
    """branch→PR lookup returns None → card skipped (graceful)."""
    cards = [{
        "id": "t_dev",
        "assignee": "developer",
        "branch_name": "feat/nonexistent",
        "runs": [{"reason": "review-required: CI green"}],
    }]
    with mock.patch.object(kanban, "list_blocked", return_value=cards):
        with mock.patch.object(gp, "find_pr_for_branch", return_value=None):
            with mock.patch.object(gp, "pr_ci_green") as mk_ci:
                counts, _ = iterate.run_iterate("slug", "O/R", provider=gp)
    check("branch no match → no actions", all(v == 0 for v in counts.values()))
    mk_ci.assert_not_called()


def test_run_iterate_handoff_pr_still_works():
    """Existing handoff-PR path still works alongside the new branch fallback."""
    cards = [{
        "id": "t_dev",
        "assignee": "developer",
        "branch_name": "feat/other",
        "runs": [{"reason": "review-required: PR #55 shipped"}],
    }]
    with mock.patch.object(kanban, "list_blocked", return_value=cards):
        with mock.patch.object(kanban, "complete", return_value=True):
            with mock.patch.object(gp, "find_pr_for_branch") as mk_branch:
                with mock.patch.object(gp, "pr_ci_green", return_value=True) as mk_ci:
                    counts, prs = iterate.run_iterate("slug", "O/R", provider=gp)
    check("handoff PR still works → advance count 1", counts[iterate.ADVANCE] == 1)
    check("handoff PR still works → PR is 55", prs == [55])
    # open_pr_for_branch should NOT be called since handoff has a PR
    mk_branch.assert_not_called()
    mk_ci.assert_called_once_with(55)


def test_run_iterate_branch_pr_fallback_ci_red():
    """branch PR + CI red → dev_fix_ci still created."""
    cards = [{
        "id": "t_dev",
        "assignee": "developer",
        "branch_name": "feat/broken",
        "runs": [{"reason": "review-required: CI failing"}, {"metadata": {"fix_attempts": 0}}],
        "workspace": "dir:/w",
    }]
    with mock.patch.object(kanban, "list_blocked", return_value=cards):
        with mock.patch.object(kanban, "create_task", return_value="t_fix"):
            with mock.patch.object(kanban, "comment", return_value=True):
                with mock.patch.object(gp, "find_pr_for_branch", return_value=99):
                    with mock.patch.object(gp, "pr_ci_green", return_value=False):
                        counts, _ = iterate.run_iterate("slug", "O/R", provider=gp)
    check("branch PR ci red → dev_fix_ci count 1", counts[iterate.DEV_FIX_CI] == 1)




if __name__ == "__main__":
    print("Iterate (CI-aware auto-advance) tests")
    print("-" * 60)
    for fn in (
        test_classify_blocked_dev_green,
        test_classify_blocked_dev_red,
        test_classify_blocked_dev_escalate,
        test_classify_blocked_dev_no_pr,
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
        test_execute_dev_fix_ci,
        test_execute_pm_route,
        test_execute_pm_route_empty_profile_fallback,
        test_execute_approve_advance,
        test_execute_escalate,
        test_execute_dev_fix_escalate_when_over_cap,
        test_run_iterate_empty,
        test_run_iterate_dev_advance,
        test_run_iterate_dev_fix_ci,
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
        test_run_iterate_respects_router_profile_config,
        test_run_iterate_default_router_profile,
        test_classify_blocked_pr_number_fallback,
        test_run_iterate_branch_pr_fallback,
        test_run_iterate_branch_pr_fallback_no_match,
        test_run_iterate_handoff_pr_still_works,
        test_run_iterate_branch_pr_fallback_ci_red,
    ):
        fn()
    print("-" * 60)
    print(f"Results: {_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)
