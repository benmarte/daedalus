"""Tests for core.dispatch_state per-issue pipeline state (#1170 Phase 2).

Covers:
  - retry_cap_notified: mark / is / idempotent / per-role isolation
  - escalation_notified: mark / is / idempotent
  - consult_resolved_for_card: mark / is / per-card isolation
  - stage_history: append / multi-append / read-back
  - Atomicity: concurrent-style reads/writes do not corrupt state
  - Dual-read backfill in _has_notified_block (dedup.py integration)
  - Dual-write in _mark_notified_block (dedup.py integration)
  - Dual-read backfill in _is_consult_resolved (stages.py integration)
  - Dual-write in _stamp_resolved_consultations (stages.py integration)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import check  # noqa: E402,F401

import core.dispatch_state as ds


# ── retry_cap_notified ────────────────────────────────────────────────────────


def test_retry_cap_not_set_initially(tmp_path):
    wd = str(tmp_path)
    check("not set initially", not ds.is_retry_cap_notified(wd, 1, "developer"))


def test_retry_cap_mark_and_check(tmp_path):
    wd = str(tmp_path)
    ds.mark_retry_cap_notified(wd, 1, "developer")
    check("set after mark", ds.is_retry_cap_notified(wd, 1, "developer"))


def test_retry_cap_per_role_isolation(tmp_path):
    wd = str(tmp_path)
    ds.mark_retry_cap_notified(wd, 2, "developer")
    check("developer set", ds.is_retry_cap_notified(wd, 2, "developer"))
    check("qa not set", not ds.is_retry_cap_notified(wd, 2, "qa"))
    check("reviewer not set", not ds.is_retry_cap_notified(wd, 2, "reviewer"))


def test_retry_cap_per_issue_isolation(tmp_path):
    wd = str(tmp_path)
    ds.mark_retry_cap_notified(wd, 10, "developer")
    check("issue 10 set", ds.is_retry_cap_notified(wd, 10, "developer"))
    check("issue 11 not set", not ds.is_retry_cap_notified(wd, 11, "developer"))


def test_retry_cap_idempotent(tmp_path):
    wd = str(tmp_path)
    ds.mark_retry_cap_notified(wd, 3, "developer")
    ds.mark_retry_cap_notified(wd, 3, "developer")  # second call must not raise
    check("still set after double mark", ds.is_retry_cap_notified(wd, 3, "developer"))


def test_retry_cap_persists_across_loads(tmp_path):
    wd = str(tmp_path)
    ds.mark_retry_cap_notified(wd, 4, "qa")
    # Reload state from disk by constructing a fresh call
    check("persists across load", ds.is_retry_cap_notified(wd, 4, "qa"))


# ── escalation_notified ───────────────────────────────────────────────────────


def test_escalation_not_set_initially(tmp_path):
    wd = str(tmp_path)
    check("not set initially", not ds.is_escalation_notified(wd, 5))


def test_escalation_mark_and_check(tmp_path):
    wd = str(tmp_path)
    ds.mark_escalation_notified(wd, 5)
    check("set after mark", ds.is_escalation_notified(wd, 5))


def test_escalation_per_issue_isolation(tmp_path):
    wd = str(tmp_path)
    ds.mark_escalation_notified(wd, 20)
    check("issue 20 set", ds.is_escalation_notified(wd, 20))
    check("issue 21 not set", not ds.is_escalation_notified(wd, 21))


def test_escalation_idempotent(tmp_path):
    wd = str(tmp_path)
    ds.mark_escalation_notified(wd, 6)
    ds.mark_escalation_notified(wd, 6)
    check("idempotent", ds.is_escalation_notified(wd, 6))


def test_escalation_and_retry_cap_coexist(tmp_path):
    wd = str(tmp_path)
    ds.mark_escalation_notified(wd, 7)
    ds.mark_retry_cap_notified(wd, 7, "developer")
    check("escalation set", ds.is_escalation_notified(wd, 7))
    check("retry cap set", ds.is_retry_cap_notified(wd, 7, "developer"))


# ── consult_resolved_for_card ─────────────────────────────────────────────────


def test_consult_resolved_not_set_initially(tmp_path):
    wd = str(tmp_path)
    check("not set initially", not ds.is_consult_resolved_for_card(wd, "card-1", 30))


def test_consult_resolved_mark_and_check(tmp_path):
    wd = str(tmp_path)
    ds.mark_consult_resolved_for_card(wd, "card-1", 30)
    check("set after mark", ds.is_consult_resolved_for_card(wd, "card-1", 30))


def test_consult_resolved_per_card_isolation(tmp_path):
    wd = str(tmp_path)
    ds.mark_consult_resolved_for_card(wd, "card-1", 31)
    check("card-1 set", ds.is_consult_resolved_for_card(wd, "card-1", 31))
    check("card-2 not set", not ds.is_consult_resolved_for_card(wd, "card-2", 31))


def test_consult_resolved_per_issue_isolation(tmp_path):
    wd = str(tmp_path)
    ds.mark_consult_resolved_for_card(wd, "card-x", 40)
    check("issue 40 set", ds.is_consult_resolved_for_card(wd, "card-x", 40))
    check("issue 41 not set", not ds.is_consult_resolved_for_card(wd, "card-x", 41))


def test_consult_resolved_idempotent(tmp_path):
    wd = str(tmp_path)
    ds.mark_consult_resolved_for_card(wd, "card-1", 30)
    ds.mark_consult_resolved_for_card(wd, "card-1", 30)
    check("still set after double mark", ds.is_consult_resolved_for_card(wd, "card-1", 30))


# ── stage_history ─────────────────────────────────────────────────────────────


def test_stage_history_empty_initially(tmp_path):
    wd = str(tmp_path)
    state = ds.get_issue_pipeline_state(wd, 50)
    check("stage_history empty initially", state.get("stage_history", []) == [])


def test_stage_history_append_and_read(tmp_path):
    wd = str(tmp_path)
    rec = {"role": "developer", "verdict": "pr_opened", "outcome_source": "json"}
    ds.append_stage_history(wd, 50, rec)
    state = ds.get_issue_pipeline_state(wd, 50)
    history = state.get("stage_history", [])
    check("one entry in history", len(history) == 1)
    check("entry matches", history[0]["role"] == "developer")


def test_stage_history_multi_append(tmp_path):
    wd = str(tmp_path)
    ds.append_stage_history(wd, 51, {"role": "developer", "verdict": "pr_opened"})
    ds.append_stage_history(wd, 51, {"role": "qa", "verdict": "passed"})
    ds.append_stage_history(wd, 51, {"role": "docs", "verdict": "posted"})
    state = ds.get_issue_pipeline_state(wd, 51)
    check("three entries", len(state["stage_history"]) == 3)
    check("order preserved", [e["role"] for e in state["stage_history"]] ==
          ["developer", "qa", "docs"])


def test_stage_history_per_issue_isolation(tmp_path):
    wd = str(tmp_path)
    ds.append_stage_history(wd, 60, {"role": "qa", "verdict": "passed"})
    state_61 = ds.get_issue_pipeline_state(wd, 61)
    check("issue 61 history empty", state_61.get("stage_history", []) == [])


def test_get_issue_pipeline_state_returns_copy(tmp_path):
    wd = str(tmp_path)
    ds.mark_retry_cap_notified(wd, 70, "developer")
    state = ds.get_issue_pipeline_state(wd, 70)
    # Mutating the returned dict must not corrupt the stored state.
    state["retry_cap_notified"] = {}
    check("mutating return value does not affect stored state",
          ds.is_retry_cap_notified(wd, 70, "developer"))


# ── Atomicity / multi-write simulation ────────────────────────────────────────


def test_multiple_writes_do_not_corrupt_state(tmp_path):
    """Simulate what happens across several dispatcher ticks."""
    wd = str(tmp_path)
    # Tick 1: retry cap for developer
    ds.mark_retry_cap_notified(wd, 80, "developer")
    # Tick 2: escalation
    ds.mark_escalation_notified(wd, 80)
    # Tick 3: consult resolved
    ds.mark_consult_resolved_for_card(wd, "card-80", 80)
    # Tick 4: stage history
    ds.append_stage_history(wd, 80, {"role": "developer", "verdict": "pr_opened"})
    # Tick 5: another role's retry cap
    ds.mark_retry_cap_notified(wd, 80, "qa")

    check("developer retry cap still set", ds.is_retry_cap_notified(wd, 80, "developer"))
    check("escalation still set", ds.is_escalation_notified(wd, 80))
    check("consult still set", ds.is_consult_resolved_for_card(wd, "card-80", 80))
    state = ds.get_issue_pipeline_state(wd, 80)
    check("history has one entry", len(state["stage_history"]) == 1)
    check("qa retry cap set", ds.is_retry_cap_notified(wd, 80, "qa"))


# ── Dedup integration: dual-read backfill via _has_notified_block ──────────────


def test_dedup_state_read_returns_true_without_kanban(tmp_path, monkeypatch):
    """_has_notified_block returns True from state without scanning kanban comments."""
    from core.dispatch import dedup as _dedup

    wd = str(tmp_path)
    ds.mark_escalation_notified(wd, 90)

    # If state is consulted first, list_tasks must NOT be called.
    list_tasks_called: list[bool] = []

    def _no_kanban(*a, **kw):  # type: ignore[no-untyped-def]
        list_tasks_called.append(True)
        return []

    monkeypatch.setattr("core.dispatch.dedup.kanban.list_tasks", _no_kanban)

    result = _dedup._has_notified_block("slug", 90, workdir=wd)
    check("state-first returns True", result is True)
    check("list_tasks not called (state hit)", list_tasks_called == [])


def test_dedup_state_read_role_returns_true_without_kanban(tmp_path, monkeypatch):
    """Role-scoped _has_notified_block reads state first."""
    from core.dispatch import dedup as _dedup

    wd = str(tmp_path)
    ds.mark_retry_cap_notified(wd, 91, "developer")

    list_tasks_called: list[bool] = []

    def _no_kanban(*a, **kw):  # type: ignore[no-untyped-def]
        list_tasks_called.append(True)
        return []

    monkeypatch.setattr("core.dispatch.dedup.kanban.list_tasks", _no_kanban)

    result = _dedup._has_notified_block("slug", 91, role="developer", workdir=wd)
    check("state-first returns True for role", result is True)
    check("list_tasks not called", list_tasks_called == [])


def test_dedup_mark_writes_to_state(tmp_path, monkeypatch):
    """_mark_notified_block writes to dispatch_state in addition to kanban comment."""
    from core.dispatch import dedup as _dedup

    wd = str(tmp_path)

    # Make kanban.list_tasks return one "validator" card for issue #92.
    fake_card = {
        "id": "vcard-92",
        "title": "validate issue #92",
        "assignee": "validator-daedalus",
    }
    monkeypatch.setattr("core.dispatch.dedup.kanban.list_tasks", lambda *a, **kw: [fake_card])
    monkeypatch.setattr("core.dispatch.dedup.kanban.comment", lambda *a, **kw: True)

    _dedup._mark_notified_block("slug", 92, workdir=wd)

    check("escalation written to state", ds.is_escalation_notified(wd, 92))


def test_dedup_mark_role_writes_to_state(tmp_path, monkeypatch):
    """_mark_notified_block with role writes retry-cap state."""
    from core.dispatch import dedup as _dedup

    wd = str(tmp_path)

    fake_card = {
        "id": "vcard-93",
        "title": "validate issue #93",
        "assignee": "validator-daedalus",
    }
    monkeypatch.setattr("core.dispatch.dedup.kanban.list_tasks", lambda *a, **kw: [fake_card])
    monkeypatch.setattr("core.dispatch.dedup.kanban.comment", lambda *a, **kw: True)

    _dedup._mark_notified_block("slug", 93, role="developer", workdir=wd)

    check("retry cap written to state", ds.is_retry_cap_notified(wd, 93, "developer"))


# ── Stages integration: _is_consult_resolved dual-read backfill ───────────────


def test_stages_consult_state_read_skips_kanban(tmp_path, monkeypatch):
    """_is_consult_resolved checks state first; kanban not consulted on state hit."""
    from core.dispatch import stages as _stages

    wd = str(tmp_path)
    ds.mark_consult_resolved_for_card(wd, "blocked-card-1", 100)

    show_card_called: list[bool] = []

    def _no_kanban(*a, **kw):  # type: ignore[no-untyped-def]
        show_card_called.append(True)
        return []

    monkeypatch.setattr("core.dispatch.stages.kanban.list_tasks", _no_kanban)

    result = _stages._is_consult_resolved("slug", "blocked-card-1", 100, workdir=wd)
    check("state-first returns True", result is True)
    check("list_tasks not called (state hit)", show_card_called == [])


# ── _mark_notified_block: first-match-only semantics (break behaviour) ────────


def test_mark_notified_block_stamps_only_first_validator_card(tmp_path, monkeypatch):
    """_mark_notified_block stamps exactly ONE validator card even when multiple match.

    The ``break`` after the first match is intentional: only the first validator
    card for the issue is stamped.  A second (re-run) validator card for the same
    issue is NOT stamped by this call — it will be deduped via the dual-read
    state path instead.  This test pins that behaviour so future refactors don't
    silently revert to stamping all cards.
    """
    from core.dispatch import dedup as _dedup

    wd = str(tmp_path)

    # Two validator cards for issue #110 — both match #110 in title.
    v1 = {"id": "v1-card", "title": "validate issue #110", "assignee": "validator-daedalus"}
    v2 = {"id": "v2-card", "title": "validate issue #110 re-run", "assignee": "validator-daedalus"}

    comments_posted: dict[str, list[str]] = {}

    def _fake_comment(slug: str, tid: str, body: str) -> bool:
        comments_posted.setdefault(tid, []).append(body)
        return True

    monkeypatch.setattr("core.dispatch.dedup.kanban.list_tasks", lambda *a, **kw: [v1, v2])
    monkeypatch.setattr("core.dispatch.dedup.kanban.comment", _fake_comment)

    result = _dedup._mark_notified_block("slug", 110, workdir=wd)

    check("mark returned True", result is True)
    check("exactly one card was stamped", len(comments_posted) == 1)
    check("the FIRST matching card was stamped", "v1-card" in comments_posted)
    check("the second card was NOT stamped", "v2-card" not in comments_posted)


def test_mark_notified_block_fallback_after_no_validator(tmp_path, monkeypatch):
    """When no validator card matches, fallback_task_id is stamped instead."""
    from core.dispatch import dedup as _dedup

    wd = str(tmp_path)

    comments_posted: dict[str, list[str]] = {}

    def _fake_comment(slug: str, tid: str, body: str) -> bool:
        comments_posted.setdefault(tid, []).append(body)
        return True

    # No validator cards on the board.
    monkeypatch.setattr("core.dispatch.dedup.kanban.list_tasks", lambda *a, **kw: [])
    monkeypatch.setattr("core.dispatch.dedup.kanban.comment", _fake_comment)

    result = _dedup._mark_notified_block("slug", 111, fallback_task_id="fallback-card", workdir=wd)

    check("mark returned True via fallback", result is True)
    check("fallback card was stamped", "fallback-card" in comments_posted)
    check("no validator card stamped (none found)", len(comments_posted) == 1)
