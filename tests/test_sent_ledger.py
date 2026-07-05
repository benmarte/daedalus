"""Unit tests for the crash-safe sent-ledger (#1275).

Covers:
  AC1 — ledger helpers in core.dispatch_state
  AC2 — deliver_event pending→finalize protocol in core.thread_delivery
  AC3 — crash-simulation + block-notification stale-pending resolution
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import conftest  # noqa: E402
from conftest import check, FakeKanban  # noqa: E402,F401

from core import dispatch_state, thread_delivery  # noqa: E402
from core.dispatch.dedup import (  # noqa: E402
    _has_notified_block,
    _mark_notified_block,
    _block_ledger_key,
    record_pending_block_notification,
)


# ── AC1: ledger helpers ────────────────────────────────────────────────────────


def test_ledger_record_pending_then_finalize(tmp_path):
    wd = str(tmp_path)
    dispatch_state.ledger_record_pending(wd, "evt-1", "slack:C1")
    check("pending entry present", dispatch_state.ledger_is_pending(wd, "evt-1"))
    dispatch_state.ledger_finalize(wd, "evt-1")
    check("finalized after ledger_finalize", dispatch_state.ledger_is_finalized(wd, "evt-1"))
    check("no longer pending after finalize", not dispatch_state.ledger_is_pending(wd, "evt-1"))


def test_ledger_is_finalized_after_finalize(tmp_path):
    wd = str(tmp_path)
    dispatch_state.ledger_record_pending(wd, "evt-2", "t1")
    dispatch_state.ledger_finalize(wd, "evt-2", note="done")
    entry = dispatch_state.ledger_get(wd, "evt-2")
    check("status=sent", entry is not None and entry.get("status") == "sent")
    check("note preserved", entry is not None and entry.get("note") == "done")


def test_ledger_is_pending_before_finalize(tmp_path):
    wd = str(tmp_path)
    dispatch_state.ledger_record_pending(wd, "evt-3", "slug-x")
    check("pending before finalize", dispatch_state.ledger_is_pending(wd, "evt-3"))
    check("not finalized before finalize", not dispatch_state.ledger_is_finalized(wd, "evt-3"))


def test_ledger_finalize_from_nothing(tmp_path):
    """ledger_finalize without a prior pending call is idempotent (no crash)."""
    wd = str(tmp_path)
    dispatch_state.ledger_finalize(wd, "new-evt")
    check("finalized from scratch", dispatch_state.ledger_is_finalized(wd, "new-evt"))


def test_ledger_corrupt_entry_returns_false(tmp_path):
    """A corrupt (non-dict) entry is treated as not-finalized."""
    import json
    import os
    wd = str(tmp_path)
    state_dir = Path(wd) / ".hermes"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "daedalus_dispatch_state.json"
    state_path.write_text(json.dumps({"sent_ledger": {"bad-evt": "not-a-dict"}}))
    check("corrupt entry → not finalized", not dispatch_state.ledger_is_finalized(wd, "bad-evt"))
    check("corrupt entry → not pending", not dispatch_state.ledger_is_pending(wd, "bad-evt"))
    check("ledger_get returns None for corrupt", dispatch_state.ledger_get(wd, "bad-evt") is None)


def test_ledger_empty_workdir_is_noop():
    """Empty workdir should not raise and should return safe defaults."""
    dispatch_state.ledger_record_pending("", "evt", "t")  # no crash
    dispatch_state.ledger_finalize("", "evt")  # no crash
    check("empty workdir → not finalized", not dispatch_state.ledger_is_finalized("", "evt"))
    check("empty workdir → not pending", not dispatch_state.ledger_is_pending("", "evt"))
    check("empty workdir → get returns None", dispatch_state.ledger_get("", "evt") is None)
    check("empty workdir → all_pending empty", dispatch_state.ledger_all_pending("") == [])


def test_ledger_all_pending_returns_pending_only(tmp_path):
    wd = str(tmp_path)
    dispatch_state.ledger_record_pending(wd, "p1", "t1")
    dispatch_state.ledger_record_pending(wd, "p2", "t2")
    dispatch_state.ledger_record_pending(wd, "s1", "t3")
    dispatch_state.ledger_finalize(wd, "s1")
    pending = dispatch_state.ledger_all_pending(wd)
    keys = [k for k, _ in pending]
    check("two pending entries", len(pending) == 2)
    check("p1 in pending", "p1" in keys)
    check("p2 in pending", "p2" in keys)
    check("s1 NOT in pending (finalized)", "s1" not in keys)


def test_record_dispatch_preserves_sent_ledger(tmp_path):
    """record_dispatch must not clobber the sent_ledger top-level key."""
    wd = str(tmp_path)
    dispatch_state.ledger_record_pending(wd, "preserve-me", "t1")
    dispatch_state.ledger_finalize(wd, "preserve-me")
    dispatch_state.record_dispatch(wd, 42)
    check("ledger survives record_dispatch", dispatch_state.ledger_is_finalized(wd, "preserve-me"))


# ── AC2: thread_delivery pending→finalize protocol ────────────────────────────


class _PendingCaptureSend:
    """Send callable that asserts the ledger is pending BEFORE returning."""

    def __init__(self, wd, event_key, *, anchor="ts-root"):
        self.wd = wd
        self.event_key = event_key
        self.anchor = anchor
        self.calls = []
        self.was_pending_on_call = []

    def __call__(self, target, body, thread_id):
        self.calls.append((target, body, thread_id))
        self.was_pending_on_call.append(
            dispatch_state.ledger_is_pending(self.wd, self.event_key)
        )
        return (True, self.anchor if thread_id is None else None)


def test_deliver_event_records_pending_before_send(tmp_path):
    wd = str(tmp_path)
    send = _PendingCaptureSend(wd, "root")
    thread_delivery.deliver_event(wd, 1, "slack:C1", "hello", "root", send=send)
    check("send called once", len(send.calls) == 1)
    check("ledger was pending DURING send", send.was_pending_on_call[0])


def test_deliver_event_finalizes_after_send(tmp_path):
    wd = str(tmp_path)

    def simple_send(target, body, thread_id):
        return (True, "ts-1" if thread_id is None else None)

    thread_delivery.deliver_event(wd, 1, "slack:C1", "hello", "root", send=simple_send)
    check("ledger finalized after send", dispatch_state.ledger_is_finalized(wd, "root"))
    check("no longer pending", not dispatch_state.ledger_is_pending(wd, "root"))


def test_deliver_event_skips_when_finalized(tmp_path):
    wd = str(tmp_path)
    dispatch_state.ledger_finalize(wd, "evt-skip")
    calls = []

    def send(target, body, thread_id):
        calls.append(1)
        return (True, None)

    res = thread_delivery.deliver_event(wd, 1, "slack:C1", "body", "evt-skip", send=send)
    check("returns skipped when ledger finalized", res == "skipped")
    check("send not called", len(calls) == 0)


def test_deliver_event_backfills_ledger_from_thread_events(tmp_path):
    wd = str(tmp_path)
    # Pre-mark via old thread_events mechanism
    dispatch_state.mark_thread_event(wd, 1, "slack:C1", "evt-old")
    calls = []

    def send(target, body, thread_id):
        calls.append(1)
        return (True, None)

    res = thread_delivery.deliver_event(wd, 1, "slack:C1", "body", "evt-old", send=send)
    check("returns skipped (compat hit)", res == "skipped")
    check("send not called", len(calls) == 0)
    check("ledger backfilled to finalized", dispatch_state.ledger_is_finalized(wd, "evt-old"))


def test_deliver_event_resends_stale_pending(tmp_path):
    """Stale pending entry → send IS called (at-most-once-extra re-send)."""
    wd = str(tmp_path)
    dispatch_state.ledger_record_pending(wd, "stale-evt", "slack:C1")
    calls = []

    def send(target, body, thread_id):
        calls.append(1)
        return (True, "ts-new" if thread_id is None else None)

    res = thread_delivery.deliver_event(wd, 1, "slack:C1", "body", "stale-evt", send=send)
    check("send called for stale pending", len(calls) == 1)
    check("result is sent", res == "sent")
    check("finalized after stale-pending re-send", dispatch_state.ledger_is_finalized(wd, "stale-evt"))


# ── AC3: crash simulation ──────────────────────────────────────────────────────


def test_crash_between_send_and_mark_thread_event_does_not_double_finalize(tmp_path):
    """Simulate crash: send OK but mark_thread_event raises → pending remains.

    Next call with send OK → delivers once more → finalized.
    Total sends == 2 (at-most-once-extra), final state == finalized.
    """
    wd = str(tmp_path)
    send_count = [0]

    def send(target, body, thread_id):
        send_count[0] += 1
        return (True, "ts-1" if thread_id is None else None)

    original_mark = dispatch_state.mark_thread_event

    def crashing_mark(*args, **kwargs):
        raise RuntimeError("simulated crash")

    # Tick 1: send succeeds, mark_thread_event crashes
    with mock.patch.object(dispatch_state, "mark_thread_event", side_effect=crashing_mark):
        try:
            thread_delivery.deliver_event(wd, 1, "slack:C1", "body", "crash-evt", send=send)
        except RuntimeError:
            pass

    check("pending after crash (ledger NOT finalized)", dispatch_state.ledger_is_pending(wd, "crash-evt"))
    check("send called once in tick 1", send_count[0] == 1)

    # Tick 2: normal path — stale pending → re-sends once
    res = thread_delivery.deliver_event(wd, 1, "slack:C1", "body", "crash-evt", send=send)
    check("tick 2 returns sent", res == "sent")
    check("total sends == 2 (at-most-once-extra)", send_count[0] == 2)
    check("finalized after recovery tick", dispatch_state.ledger_is_finalized(wd, "crash-evt"))


def test_block_notification_stale_pending_when_card_missing_no_refire(tmp_path):
    """Ledger shows pending, card lookup returns no cards → _has_notified_block returns True (no re-fire)."""
    wd = str(tmp_path)
    slug = "test-board"
    issue_number = 42
    role = "pm"

    # Seed ledger as pending
    dispatch_state.ledger_record_pending(wd, _block_ledger_key(issue_number, role), target=slug)

    board = FakeKanban()

    with mock.patch("core.kanban._hk", lambda args, timeout=60: (1, "", "stub")):
        with mock.patch("core.kanban.list_tasks", return_value=[]):
            result = _has_notified_block(slug, issue_number, role=role, workdir=wd)

    check("returns True (no re-fire, card missing)", result is True)
    check("ledger finalized with card-missing note",
          dispatch_state.ledger_is_finalized(wd, _block_ledger_key(issue_number, role)))
    entry = dispatch_state.ledger_get(wd, _block_ledger_key(issue_number, role))
    check("note says card-missing-assumed-sent",
          entry is not None and "card-missing" in entry.get("note", ""))


def test_block_notification_stale_pending_with_card_marker_found(tmp_path):
    """Ledger shows pending, card has the marker comment → _has_notified_block returns True."""
    wd = str(tmp_path)
    slug = "test-board"
    issue_number = 77
    role = "validator"

    dispatch_state.ledger_record_pending(wd, _block_ledger_key(issue_number, role), target=slug)

    marker_comment = f"<!-- daedalus:retry-cap-notified:{role} -->"
    task = {
        "id": "t1",
        "title": f"Fix issue #{issue_number}",
        "assignee": "validator-daedalus",
        "status": "done",
        "comments": [{"body": marker_comment}],
    }

    with mock.patch("core.kanban._hk", lambda args, timeout=60: (1, "", "stub")):
        with mock.patch("core.kanban.list_tasks", return_value=[task]):
            with mock.patch("core.kanban.show_card", return_value=task):
                result = _has_notified_block(slug, issue_number, role=role, workdir=wd)

    check("returns True (marker found)", result is True)
    check("ledger finalized",
          dispatch_state.ledger_is_finalized(wd, _block_ledger_key(issue_number, role)))
    entry = dispatch_state.ledger_get(wd, _block_ledger_key(issue_number, role))
    check("note says verified-from-card",
          entry is not None and "verified-from-card" in entry.get("note", ""))


def test_mark_notified_block_writes_ledger(tmp_path):
    """_mark_notified_block with workdir → ledger_is_finalized returns True."""
    wd = str(tmp_path)
    slug = "board"
    issue_number = 55
    role = "pm"

    with mock.patch("core.kanban._hk", lambda args, timeout=60: (1, "", "stub")):
        with mock.patch("core.kanban.list_tasks", return_value=[]):
            with mock.patch("core.kanban.comment", return_value=False):
                _mark_notified_block(
                    slug, issue_number, role=role, workdir=wd, fallback_task_id=""
                )

    check("ledger finalized after _mark_notified_block",
          dispatch_state.ledger_is_finalized(wd, _block_ledger_key(issue_number, role)))


def test_has_notified_block_returns_true_when_ledger_finalized(tmp_path):
    """No card, no state, but ledger finalized → _has_notified_block returns True (fixes #1167 class)."""
    wd = str(tmp_path)
    slug = "board"
    issue_number = 99
    role = "developer"

    dispatch_state.ledger_finalize(wd, _block_ledger_key(issue_number, role))

    with mock.patch("core.kanban._hk", lambda args, timeout=60: (1, "", "stub")):
        with mock.patch("core.kanban.list_tasks", return_value=[]):
            result = _has_notified_block(slug, issue_number, role=role, workdir=wd)

    check("ledger-only hit returns True (no card scan needed)", result is True)


# ── standalone runner ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import inspect

    print("sent-ledger tests (#1275)")
    print("-" * 60)
    for _name, _fn in sorted(
        (n, f) for n, f in globals().items()
        if n.startswith("test_") and callable(f)
    ):
        if "tmp_path" in inspect.signature(_fn).parameters:
            with tempfile.TemporaryDirectory() as d:
                _fn(Path(d))
        else:
            _fn()
    print("-" * 60)
    print(f"Results: {conftest._passed} passed, {conftest._failed} failed")
    import sys as _sys
    _sys.exit(1 if conftest._failed else 0)
