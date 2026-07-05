"""Tests for #1274 — workdir threading to activate protocol Fork 3.

Verifies that ``workdir`` and ``prefix_fallback`` are correctly threaded into:
  - _enforce_validator_blocks (stages.py) → _has_notified_block / _mark_notified_block
  - _arbitrate_validator_outcome (stages.py) → _has_notified_block / _mark_notified_block
  - _is_consult_resolved (stages.py, called by _check_team_blockers in dispatcher)

Fork 3 behaviour under test
---------------------------
  prefix_fallback=False + workdir set + state pre-populated:
    _has_notified_block / _is_consult_resolved return True from dispatch_state ONLY —
    the kanban comment scan (kanban.list_tasks inside dedup) is NOT called.

  prefix_fallback=True (default) + workdir set, no state record:
    Behaviour is byte-identical to before threading — comment scan still runs.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import FakeKanban, FakeProvider, check, kanban_as  # noqa: E402,F401

import core.kanban as kanban  # noqa: E402
import core.dispatch_state as ds  # noqa: E402
from core.dispatch import stages  # noqa: E402

SLUG = "proj"
VALIDATOR = "validator-daedalus"
ISSUE_N = 55


# ── helpers ───────────────────────────────────────────────────────────────────


def _seed_blocked_validator(fk: FakeKanban, issue_n: int = ISSUE_N) -> str:
    """Add a blocked validator card for *issue_n* so _enforce_validator_blocks fires."""
    return fk.seed(
        assignee=VALIDATOR,
        title=f"validate issue #{issue_n}",
        status="blocked",
        summary="blocked: cannot proceed",
        idempotency_key=f"validator-{issue_n}",
    )


def _seed_escalation_validator(fk: FakeKanban, issue_n: int = ISSUE_N) -> str:
    """Add a done validator card with SECURITY_THREAT verdict for the arbiter."""
    return fk.seed(
        assignee=VALIDATOR,
        title=f"validate issue #{issue_n}",
        status="done",
        summary="SECURITY_THREAT: malicious input detected",
        idempotency_key=f"validator-{issue_n}",
    )


# ── _enforce_validator_blocks ─────────────────────────────────────────────────


def test_enforce_fork3_state_authoritative_no_comment_scan(tmp_path, monkeypatch):
    """Fork 3: with workdir + prefix_fallback=False + state pre-populated,
    kanban.list_tasks (comment scan in _has_notified_block) is NOT called.
    The escalation-notified state record is sufficient — the card is deduped
    via state and NOT included in 'enforced'.
    """
    wd = str(tmp_path)
    # Pre-populate state: escalation already notified for ISSUE_N.
    ds.mark_escalation_notified(wd, ISSUE_N)

    fk = FakeKanban()
    _seed_blocked_validator(fk)
    prov = FakeProvider(board_configured=True)

    list_tasks_calls: list[int] = []
    _orig_list_tasks = kanban.list_tasks

    def _tracking_list_tasks(slug: str, status: str = "") -> list:
        list_tasks_calls.append(1)
        return _orig_list_tasks(slug, status)

    with kanban_as(kanban, fk):
        # Patch list_tasks AFTER kanban_as so we track calls in dedup's comment scan.
        monkeypatch.setattr("core.dispatch.dedup.kanban.list_tasks", _tracking_list_tasks)
        enforced = stages._enforce_validator_blocks(
            SLUG, prov, {ISSUE_N},
            validator_profile=VALIDATOR,
            workdir=wd,
            prefix_fallback=False,
        )

    check("fork3: already-notified-by-state → not in enforced", enforced == [])
    check("fork3: comment scan skipped (no kanban.list_tasks call)", list_tasks_calls == [])


def test_enforce_default_flag_byte_inert(tmp_path, monkeypatch):
    """prefix_fallback=True (default): comment scan still runs even when workdir is set.
    No state record → comment scan determines the answer (list_tasks IS called).
    """
    wd = str(tmp_path)
    # No state record — scan must run.

    fk = FakeKanban()
    _seed_blocked_validator(fk)
    prov = FakeProvider(board_configured=True)

    list_tasks_calls: list[int] = []
    _orig_list_tasks = kanban.list_tasks

    def _tracking_list_tasks(slug: str, status: str = "") -> list:
        list_tasks_calls.append(1)
        return _orig_list_tasks(slug, status)

    with kanban_as(kanban, fk):
        monkeypatch.setattr("core.dispatch.dedup.kanban.list_tasks", _tracking_list_tasks)
        enforced = stages._enforce_validator_blocks(
            SLUG, prov, {ISSUE_N},
            validator_profile=VALIDATOR,
            workdir=wd,
            prefix_fallback=True,  # default — comment scan runs
        )

    check("flag-on: card appears in enforced (first notification)", enforced == [ISSUE_N])
    check("flag-on: comment scan ran (list_tasks called)", list_tasks_calls != [])


def test_enforce_no_workdir_unchanged(tmp_path, monkeypatch):
    """Without workdir, behaviour is completely unchanged regardless of prefix_fallback."""
    fk = FakeKanban()
    _seed_blocked_validator(fk)
    prov = FakeProvider(board_configured=True)

    with kanban_as(kanban, fk):
        enforced = stages._enforce_validator_blocks(
            SLUG, prov, {ISSUE_N},
            validator_profile=VALIDATOR,
            # no workdir, no prefix_fallback
        )

    check("no workdir: card in enforced (original path)", enforced == [ISSUE_N])


# ── _arbitrate_validator_outcome ──────────────────────────────────────────────


def test_arbiter_fork3_state_authoritative_no_comment_scan(tmp_path, monkeypatch):
    """Fork 3: arbiter deduplication reads from state; comment scan skipped."""
    wd = str(tmp_path)
    ds.mark_escalation_notified(wd, ISSUE_N)

    fk = FakeKanban()
    _seed_escalation_validator(fk)
    prov = FakeProvider(board_configured=True)

    list_tasks_calls: list[int] = []
    _orig_list_tasks = kanban.list_tasks

    def _tracking_list_tasks(slug: str, status: str = "") -> list:
        list_tasks_calls.append(1)
        return _orig_list_tasks(slug, status)

    with kanban_as(kanban, fk):
        monkeypatch.setattr("core.dispatch.dedup.kanban.list_tasks", _tracking_list_tasks)
        enforced = stages._arbitrate_validator_outcome(
            SLUG, prov, {ISSUE_N},
            validator_profile=VALIDATOR,
            workdir=wd,
            prefix_fallback=False,
        )

    check("arbiter fork3: state-notified → not in enforced", enforced == [])
    check("arbiter fork3: comment scan skipped", list_tasks_calls == [])


def test_arbiter_default_flag_byte_inert(tmp_path, monkeypatch):
    """Arbiter: default prefix_fallback=True — comment scan still runs."""
    wd = str(tmp_path)

    fk = FakeKanban()
    _seed_escalation_validator(fk)
    prov = FakeProvider(board_configured=True)

    list_tasks_calls: list[int] = []
    _orig_list_tasks = kanban.list_tasks

    def _tracking_list_tasks(slug: str, status: str = "") -> list:
        list_tasks_calls.append(1)
        return _orig_list_tasks(slug, status)

    with kanban_as(kanban, fk):
        monkeypatch.setattr("core.dispatch.dedup.kanban.list_tasks", _tracking_list_tasks)
        enforced = stages._arbitrate_validator_outcome(
            SLUG, prov, {ISSUE_N},
            validator_profile=VALIDATOR,
            workdir=wd,
            prefix_fallback=True,
        )

    check("arbiter flag-on: first notification fires", enforced == [ISSUE_N])
    check("arbiter flag-on: comment scan ran", list_tasks_calls != [])


# ── _is_consult_resolved with prefix_fallback ─────────────────────────────────


def test_is_consult_resolved_fork3_state_authoritative(tmp_path, monkeypatch):
    """Fork 3: _is_consult_resolved returns True from state, skips show_card."""
    wd = str(tmp_path)
    ds.mark_consult_resolved_for_card(wd, "card-99", ISSUE_N)

    show_card_calls: list[int] = []

    def _no_show_card(*a, **kw) -> dict | None:
        show_card_calls.append(1)
        return None

    monkeypatch.setattr("core.dispatch.stages.kanban.show_card", _no_show_card)

    result = stages._is_consult_resolved(
        SLUG, "card-99", ISSUE_N, workdir=wd, prefix_fallback=False
    )

    check("fork3 consult: returns True from state", result is True)
    check("fork3 consult: show_card not called", show_card_calls == [])


def test_is_consult_resolved_default_flag_runs_scan(tmp_path, monkeypatch):
    """prefix_fallback=True (default): show_card is called even with workdir set."""
    wd = str(tmp_path)
    # No state record — must fall through to comment scan.

    show_card_calls: list[int] = []

    def _empty_show_card(slug: str, card_id: str) -> dict:
        show_card_calls.append(1)
        return {"comments": []}

    monkeypatch.setattr("core.dispatch.stages.kanban.show_card", _empty_show_card)
    monkeypatch.setattr("core.dispatch.stages.kanban.list_tasks", lambda *a, **kw: [])

    result = stages._is_consult_resolved(
        SLUG, "card-99", ISSUE_N, workdir=wd, prefix_fallback=True
    )

    check("flag-on consult: returns False (no marker)", result is False)
    check("flag-on consult: show_card called (scan ran)", show_card_calls != [])


def test_is_consult_resolved_no_workdir_unchanged(monkeypatch):
    """Without workdir, prefix_fallback has no effect — scan always runs."""
    show_card_calls: list[int] = []

    def _empty_show_card(slug: str, card_id: str) -> dict:
        show_card_calls.append(1)
        return {"comments": []}

    monkeypatch.setattr("core.dispatch.stages.kanban.show_card", _empty_show_card)

    result = stages._is_consult_resolved(SLUG, "card-99", ISSUE_N)

    check("no workdir: scan runs regardless of flag", show_card_calls != [])
    check("no workdir: returns False (no marker)", result is False)
