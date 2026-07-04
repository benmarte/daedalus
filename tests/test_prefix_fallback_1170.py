"""Tests for protocol.prefix_fallback flag — Phase 3 of #1170.

Covers three fork points parametrised over both flag values:

  Fork 1 — classify_blocked:
    flag=True  → JSON-less completion falls through to legacy prefix routing
                 (existing suite already covers full prefix behaviour)
    flag=False → JSON-less completion → PENDING_SIGNAL (hold for next tick)
    flag=False + valid JSON → routes normally (not PENDING_SIGNAL)

  Fork 2 — _guard_prefix_on_done:
    flag=True  → prefix line satisfies the guard (existing behaviour)
    flag=False → prefix alone fails; valid JSON outcome record required
    flag=False + valid JSON → guard skips (well-formed completion)

  Fork 3 — marker comment-scans (_has_notified_block, _is_consult_resolved):
    flag=True  → comment-scan runs (state-hit skips scan, state-miss falls
                 through to scan — existing Phase-2 behaviour)
    flag=False → comment-scan skipped when workdir supplied; show_card never
                 called (FakeKanban call-count assertion)

  Integration — run_iterate with protocol.prefix_fallback: false:
    JSON-less blocked cards get PENDING_SIGNAL, not PM_ROUTE/advance/etc.
    JSON cards route normally.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import check  # noqa: E402,F401

import core.dispatch_state as ds
from core.iterate.classify import (
    PENDING_SIGNAL, ADVANCE, PM_ROUTE, APPROVE_ADVANCE, classify_blocked,
)
from core.iterate.outcomes import OutcomeRecord, SCHEMA_VERSION


# ── helpers ───────────────────────────────────────────────────────────────────


def _json_handoff(role: str, verdict: str, *, pr_ref: int = 5, issue_ref: int = 42) -> str:
    """Build a handoff string that embeds a valid JSON OutcomeRecord block."""
    block = {
        "schema_version": SCHEMA_VERSION,
        "role": role,
        "verdict": verdict,
        "issue_ref": issue_ref,
        "pr_ref": pr_ref,
        "evidence": {},
        "note": "",
    }
    prefix = f"review-required: PR #{pr_ref}"
    return f"{prefix}\n```json\n{json.dumps(block)}\n```"


def _prefix_only_handoff(prefix: str) -> str:
    """Build a handoff string with only a legacy prefix, no JSON."""
    return prefix


# ── Fork 1: classify_blocked ──────────────────────────────────────────────────


def test_classify_prefix_fallback_true_prefix_only_routes_normally():
    """flag=True (default): prefix-only handoff falls through to prefix routing."""
    # reviewer card with "review-approved:" prefix but no JSON → prefix routing → APPROVE_ADVANCE
    result = classify_blocked(
        "reviewer-daedalus",
        "review-approved: PR #5 looks good",
        ci_green=True,
        prefix_fallback=True,
    )
    check("prefix routing active when flag=True", result == APPROVE_ADVANCE)


def test_classify_prefix_fallback_false_no_json_returns_pending_signal():
    """flag=False: a card with NO valid JSON outcome → PENDING_SIGNAL (hold)."""
    # developer card with only prefix text, no JSON block
    result = classify_blocked(
        "developer-daedalus",
        "review-required: PR #5",
        ci_green=True,
        pr_number=5,
        prefix_fallback=False,
    )
    check("PENDING_SIGNAL when flag=False and no JSON", result == PENDING_SIGNAL)


def test_classify_prefix_fallback_false_no_json_qa_returns_pending_signal():
    """flag=False: QA card with only qa-passed prefix → PENDING_SIGNAL."""
    result = classify_blocked(
        "qa-daedalus",
        "qa-passed: PR #5 all tests green",
        ci_green=True,
        prefix_fallback=False,
    )
    check("PENDING_SIGNAL for QA when flag=False and no JSON", result == PENDING_SIGNAL)


def test_classify_prefix_fallback_false_no_json_docs_returns_pending_signal():
    """flag=False: docs card with docs-posted prefix only → PENDING_SIGNAL."""
    result = classify_blocked(
        "documentation-daedalus",
        "docs posted: PR #5 updated",
        ci_green=True,
        prefix_fallback=False,
    )
    check("PENDING_SIGNAL for docs when flag=False and no JSON", result == PENDING_SIGNAL)


def test_classify_prefix_fallback_false_with_valid_json_routes_normally():
    """flag=False + valid JSON → routes by JSON table (not PENDING_SIGNAL)."""
    handoff = _json_handoff("developer", "pr_opened", pr_ref=5, issue_ref=42)
    result = classify_blocked(
        "developer-daedalus",
        handoff,
        ci_green=True,
        pr_number=5,
        prefix_fallback=False,
    )
    check("valid JSON routes normally when flag=False", result == ADVANCE)


def test_classify_prefix_fallback_false_with_valid_json_qa_routes():
    """flag=False + valid JSON for qa/passed → ADVANCE (not PENDING_SIGNAL)."""
    handoff = _json_handoff("qa", "passed", pr_ref=5, issue_ref=42)
    result = classify_blocked(
        "qa-daedalus",
        handoff,
        ci_green=True,
        prefix_fallback=False,
    )
    check("qa/passed JSON routes to ADVANCE when flag=False", result == ADVANCE)


def test_classify_prefix_fallback_false_with_valid_json_docs_routes():
    """flag=False + valid JSON for docs/posted → APPROVE_ADVANCE."""
    handoff = _json_handoff("docs", "posted", pr_ref=5, issue_ref=42)
    result = classify_blocked(
        "documentation-daedalus",
        handoff,
        ci_green=True,
        prefix_fallback=False,
    )
    check("docs/posted JSON routes to APPROVE_ADVANCE when flag=False",
          result == APPROVE_ADVANCE)


def test_classify_prefix_fallback_true_and_false_produce_same_result_for_valid_json():
    """Both flag values produce identical routing when valid JSON is present."""
    handoff = _json_handoff("developer", "pr_opened", pr_ref=7, issue_ref=99)
    result_true = classify_blocked(
        "developer-daedalus", handoff, ci_green=True, pr_number=7, prefix_fallback=True,
    )
    result_false = classify_blocked(
        "developer-daedalus", handoff, ci_green=True, pr_number=7, prefix_fallback=False,
    )
    check("same result for valid JSON regardless of flag", result_true == result_false)


def test_classify_skip_qa_bypass_unaffected_by_flag():
    """skip_qa overrides routing regardless of prefix_fallback."""
    # QA card with no JSON, skip_qa=True → always ADVANCE
    for flag in (True, False):
        result = classify_blocked(
            "qa-daedalus",
            "some unrecognized text without json",
            ci_green=True,
            skip_qa=True,
            prefix_fallback=flag,
        )
        check(f"skip_qa=True always ADVANCE (flag={flag})", result == ADVANCE)


def test_classify_source_collector_records_prefix_when_flag_false_no_json():
    """_source_collector records 'prefix' when flag=False and no JSON present."""
    collector: list[str] = []
    classify_blocked(
        "developer-daedalus",
        "review-required: PR #5",
        ci_green=True,
        pr_number=5,
        prefix_fallback=False,
        _source_collector=collector,
    )
    check("source_collector records prefix when no JSON", collector == ["prefix"])


# ── Fork 2: _guard_prefix_on_done ────────────────────────────────────────────


def _make_fake_kanban_for_guard(
    *,
    assignee: str = "qa-daedalus",
    summary: str,
) -> MagicMock:
    """Build a minimal FakeKanban mock for _guard_prefix_on_done tests."""
    mk = MagicMock()
    task = {
        "id": "t-guard-1",
        "title": "fix issue #10",
        "assignee": assignee,
        "status": "done",
        "summary": summary,
    }
    mk.list_tasks.return_value = [task]
    mk.show_card.return_value = {"latest_summary": summary, "comments": []}
    mk.archive_task.return_value = True
    mk.create_task.return_value = "guard-blocked-id"
    mk.block_task.return_value = True
    return mk


def test_guard_prefix_fallback_true_prefix_satisfies():
    """flag=True (default): prefix-bearing summary passes the guard."""
    from core.dispatch.checks import _guard_prefix_on_done

    with patch("core.dispatch.checks._kanban") as mk_fn:
        mk = _make_fake_kanban_for_guard(
            assignee="qa-daedalus",
            summary="qa-passed: all tests green",
        )
        mk_fn.return_value = mk

        count = _guard_prefix_on_done("slug", prefix_fallback=True)
        check("prefix satisfies guard when flag=True", count == 0)


def test_guard_prefix_fallback_false_prefix_only_fails():
    """flag=False: a prefix-only summary (no JSON) fails the guard."""
    from core.dispatch.checks import _guard_prefix_on_done

    with patch("core.dispatch.checks._kanban") as mk_fn:
        mk = _make_fake_kanban_for_guard(
            assignee="qa-daedalus",
            summary="qa-passed: all tests green",  # prefix only — no JSON block
        )
        mk_fn.return_value = mk
        # provider with healthy issue (so closed-issue skip doesn't fire)
        provider = MagicMock()
        provider.get_issue.return_value = {"number": 10}

        count = _guard_prefix_on_done("slug", prefix_fallback=False, provider=provider)
        check("prefix-only summary fails guard when flag=False", count == 1)


def test_guard_prefix_fallback_false_valid_json_passes():
    """flag=False: summary with valid JSON OutcomeRecord passes the guard."""
    from core.dispatch.checks import _guard_prefix_on_done

    good_summary = _json_handoff("qa", "passed", pr_ref=5, issue_ref=10)

    with patch("core.dispatch.checks._kanban") as mk_fn:
        mk = _make_fake_kanban_for_guard(
            assignee="qa-daedalus",
            summary=good_summary,
        )
        mk_fn.return_value = mk

        count = _guard_prefix_on_done("slug", prefix_fallback=False)
        check("valid JSON satisfies guard when flag=False", count == 0)


def test_guard_prefix_fallback_false_wrong_role_json_fails():
    """flag=False: JSON with wrong role (reviewer for qa card) fails the guard."""
    from core.dispatch.checks import _guard_prefix_on_done

    # qa card but JSON claims reviewer role → role mismatch → not well-formed
    wrong_role_summary = _json_handoff("reviewer", "approved", pr_ref=5, issue_ref=10)

    with patch("core.dispatch.checks._kanban") as mk_fn:
        mk = _make_fake_kanban_for_guard(
            assignee="qa-daedalus",
            summary=wrong_role_summary,
        )
        mk_fn.return_value = mk
        provider = MagicMock()
        provider.get_issue.return_value = {"number": 10}

        count = _guard_prefix_on_done("slug", prefix_fallback=False, provider=provider)
        check("role-mismatch JSON fails guard when flag=False", count == 1)


def test_guard_prefix_fallback_true_and_false_identical_for_valid_prefix_and_json():
    """Both flag values pass for a well-formed summary with both prefix AND JSON."""
    from core.dispatch.checks import _guard_prefix_on_done

    # Dual-write: prefix + JSON — well-formed under both flag values.
    dual_summary = _json_handoff("qa", "passed", pr_ref=5, issue_ref=10)

    for flag in (True, False):
        with patch("core.dispatch.checks._kanban") as mk_fn:
            mk = _make_fake_kanban_for_guard(
                assignee="qa-daedalus",
                summary=dual_summary,
            )
            mk_fn.return_value = mk

            count = _guard_prefix_on_done("slug", prefix_fallback=flag)
            check(f"dual-write passes guard for flag={flag}", count == 0)


# ── Fork 3: marker comment-scans ─────────────────────────────────────────────


def test_has_notified_block_flag_false_skips_comment_scan(tmp_path, monkeypatch):
    """flag=False + workdir: comment-scan (list_tasks/show_card) is NOT called."""
    from core.dispatch import dedup as _dedup

    wd = str(tmp_path)
    # State does NOT have the marker set — so if comment-scan were active it
    # would fall through to list_tasks.  With flag=False it must return False
    # without calling list_tasks at all.

    list_tasks_calls: list[bool] = []

    monkeypatch.setattr(
        "core.dispatch.dedup.kanban.list_tasks",
        lambda *a, **kw: list_tasks_calls.append(True) or [],
    )

    result = _dedup._has_notified_block("slug", 200, workdir=wd, prefix_fallback=False)
    check("returns False (state miss)", result is False)
    check("list_tasks NOT called when flag=False", list_tasks_calls == [])


def test_has_notified_block_flag_true_falls_through_to_comment_scan(tmp_path, monkeypatch):
    """flag=True + workdir + state-miss: falls through to comment-scan (list_tasks called)."""
    from core.dispatch import dedup as _dedup

    wd = str(tmp_path)
    # State empty → state-miss → comment-scan with flag=True
    list_tasks_calls: list[bool] = []

    monkeypatch.setattr(
        "core.dispatch.dedup.kanban.list_tasks",
        lambda *a, **kw: list_tasks_calls.append(True) or [],
    )

    result = _dedup._has_notified_block("slug", 201, workdir=wd, prefix_fallback=True)
    check("returns False (no marker)", result is False)
    check("list_tasks CALLED when flag=True (comment-scan runs)", list_tasks_calls == [True])


def test_has_notified_block_flag_false_state_hit_returns_true_no_scan(tmp_path, monkeypatch):
    """flag=False + workdir + state set: returns True without scanning comments."""
    from core.dispatch import dedup as _dedup

    wd = str(tmp_path)
    ds.mark_escalation_notified(wd, 202)  # write to state

    list_tasks_calls: list[bool] = []
    monkeypatch.setattr(
        "core.dispatch.dedup.kanban.list_tasks",
        lambda *a, **kw: list_tasks_calls.append(True) or [],
    )

    result = _dedup._has_notified_block("slug", 202, workdir=wd, prefix_fallback=False)
    check("state hit returns True", result is True)
    check("list_tasks not called on state hit", list_tasks_calls == [])


def test_is_consult_resolved_flag_false_skips_show_card(tmp_path, monkeypatch):
    """flag=False + workdir: show_card (comment-scan) NOT called on state-miss."""
    from core.dispatch import stages as _stages

    wd = str(tmp_path)
    # State does not have the marker set.
    show_card_calls: list[bool] = []

    monkeypatch.setattr(
        "core.dispatch.stages.kanban.show_card",
        lambda *a, **kw: show_card_calls.append(True) or None,
    )

    result = _stages._is_consult_resolved("slug", "card-300", 300, workdir=wd, prefix_fallback=False)
    check("returns False (state miss)", result is False)
    check("show_card NOT called when flag=False", show_card_calls == [])


def test_is_consult_resolved_flag_true_calls_show_card_on_state_miss(tmp_path, monkeypatch):
    """flag=True + workdir + state-miss: show_card is called (comment-scan active)."""
    from core.dispatch import stages as _stages

    wd = str(tmp_path)
    show_card_calls: list[bool] = []

    monkeypatch.setattr(
        "core.dispatch.stages.kanban.show_card",
        lambda *a, **kw: show_card_calls.append(True) or {"comments": []},
    )

    result = _stages._is_consult_resolved("slug", "card-301", 301, workdir=wd, prefix_fallback=True)
    check("returns False (no marker)", result is False)
    check("show_card CALLED when flag=True (comment-scan runs)", show_card_calls == [True])


def test_is_consult_resolved_flag_false_state_hit_returns_true_no_scan(tmp_path, monkeypatch):
    """flag=False + workdir + state set: returns True without scanning card comments."""
    from core.dispatch import stages as _stages

    wd = str(tmp_path)
    ds.mark_consult_resolved_for_card(wd, "card-302", 302)

    show_card_calls: list[bool] = []
    monkeypatch.setattr(
        "core.dispatch.stages.kanban.show_card",
        lambda *a, **kw: show_card_calls.append(True) or None,
    )

    result = _stages._is_consult_resolved("slug", "card-302", 302, workdir=wd, prefix_fallback=False)
    check("state hit returns True", result is True)
    check("show_card not called on state hit", show_card_calls == [])


# ── Integration: run_iterate with protocol.prefix_fallback=false ──────────────


def test_run_iterate_flag_false_prefix_only_card_gets_pending_signal(tmp_path):
    """run_iterate with prefix_fallback=false: prefix-only blocked card → PENDING_SIGNAL."""
    from core.iterate import run_iterate

    wd = str(tmp_path)
    resolved = {
        "workdir": wd,
        "protocol": {"prefix_fallback": False},
    }
    # Developer card: only a legacy prefix, no JSON block
    blocked_card = {
        "id": "t-pf-dev",
        "title": "fix issue #400",
        "assignee": "developer-daedalus",
        "block_reason": "review-required: PR #5",
    }
    pending_signal_cards: list = []

    with patch("core.iterate.kanban") as mk:
        mk.list_blocked.return_value = [blocked_card]
        mk.list_tasks.return_value = []
        mk.comment.return_value = True

        counts, _, pending_signal_cards, _, _ = run_iterate(
            "slug", "org/repo", resolved=resolved
        )

    check("prefix-only card lands in pending_signal_cards when flag=False",
          len(pending_signal_cards) == 1)
    check("pending_signal_cards contains the developer card",
          pending_signal_cards[0]["tid"] == "t-pf-dev")
    check("PENDING_SIGNAL counter incremented", counts.get("pending_signal", 0) == 1)


def test_run_iterate_flag_false_valid_json_card_routes_normally(tmp_path):
    """run_iterate with prefix_fallback=false: JSON-carrying card routes normally."""
    from core.iterate import run_iterate

    wd = str(tmp_path)
    resolved = {
        "workdir": wd,
        "protocol": {"prefix_fallback": False},
    }
    # Developer card with valid JSON → should ADVANCE (not pending_signal)
    handoff = _json_handoff("developer", "pr_opened", pr_ref=7, issue_ref=400)
    blocked_card = {
        "id": "t-pf-json",
        "title": "fix issue #400",
        "assignee": "developer-daedalus",
        "block_reason": handoff,
    }

    provider = MagicMock()
    provider.get_pr_ci_status.return_value = "green"
    provider.supports_ci_status = True
    provider.is_pr_open.return_value = True
    provider.is_pr_merged.return_value = False
    provider.has_label.return_value = False
    provider.find_pr_for_branch.return_value = None

    advance_calls: list = []

    with patch("core.iterate.kanban") as mk:
        mk.list_blocked.return_value = [blocked_card]
        mk.list_tasks.return_value = []
        mk.complete.side_effect = lambda s, t, **kw: advance_calls.append(t) or True
        mk.create_task.return_value = "next-id"

        counts, advance_prs, pending_signal_cards, _, _ = run_iterate(
            "slug", "org/repo", resolved=resolved, provider=provider
        )

    check("JSON card not in pending_signal_cards", len(pending_signal_cards) == 0)
    check("advance ran for JSON card (kanban.complete called)",
          len(advance_calls) > 0)


def test_run_iterate_flag_true_default_prefix_only_routes_normally(tmp_path):
    """run_iterate with flag=True (default): prefix-only card routes via legacy prefix path."""
    from core.iterate import run_iterate

    wd = str(tmp_path)
    # Default: prefix_fallback=True (omit protocol key entirely)
    resolved = {"workdir": wd}

    blocked_card = {
        "id": "t-pf-default",
        "title": "fix issue #401",
        "assignee": "qa-daedalus",
        "block_reason": "qa-passed: all tests green",
    }

    advance_calls: list = []

    provider = MagicMock()
    provider.get_pr_ci_status.return_value = "green"
    provider.supports_ci_status = True
    provider.has_label.return_value = False
    provider.find_pr_for_branch.return_value = None

    with patch("core.iterate.kanban") as mk:
        mk.list_blocked.return_value = [blocked_card]
        mk.list_tasks.return_value = []
        mk.complete.side_effect = lambda s, t, **kw: advance_calls.append(t) or True
        mk.create_task.return_value = "next-id"

        counts, _, pending_signal_cards, _, _ = run_iterate(
            "slug", "org/repo", resolved=resolved, provider=provider
        )

    check("prefix-only card NOT in pending_signal_cards with default flag",
          len(pending_signal_cards) == 0)
    check("qa-passed routed to ADVANCE with default flag", len(advance_calls) > 0)
