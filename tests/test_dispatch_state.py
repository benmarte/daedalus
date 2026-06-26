"""Unit tests for core.dispatch_state thread-anchor / event helpers (issue #121)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import conftest  # noqa: E402
from conftest import check  # noqa: E402,F401

from core import dispatch_state  # noqa: E402


# ── thread anchors ────────────────────────────────────────────────────────────


def test_set_and_get_thread_anchor(tmp_path):
    wd = str(tmp_path)
    dispatch_state.set_thread_anchor(wd, 121, "slack:C1", "1700.001")
    check("anchor round-trips", dispatch_state.get_thread_anchor(wd, 121, "slack:C1") == "1700.001")
    check("unknown target → None", dispatch_state.get_thread_anchor(wd, 121, "discord:#x") is None)
    check("unknown issue → None", dispatch_state.get_thread_anchor(wd, 999, "slack:C1") is None)


def test_anchors_are_per_target(tmp_path):
    wd = str(tmp_path)
    dispatch_state.set_thread_anchor(wd, 5, "slack:C1", "ts-a")
    dispatch_state.set_thread_anchor(wd, 5, "discord:#ops", "msg-b")
    anchors = dispatch_state.get_thread_anchors(wd, 5)
    check("both targets stored", anchors == {"slack:C1": "ts-a", "discord:#ops": "msg-b"})


def test_anchor_coerced_to_str(tmp_path):
    wd = str(tmp_path)
    dispatch_state.set_thread_anchor(wd, 5, "slack:C1", 12345)
    check("numeric anchor stored as str", dispatch_state.get_thread_anchor(wd, 5, "slack:C1") == "12345")


# ── event dedup ───────────────────────────────────────────────────────────────


def test_mark_and_has_thread_event(tmp_path):
    wd = str(tmp_path)
    check("event absent initially", not dispatch_state.has_thread_event(wd, 7, "slack:C1", "root"))
    dispatch_state.mark_thread_event(wd, 7, "slack:C1", "root")
    check("event present after mark", dispatch_state.has_thread_event(wd, 7, "slack:C1", "root"))


def test_events_are_per_target(tmp_path):
    wd = str(tmp_path)
    dispatch_state.mark_thread_event(wd, 7, "slack:C1", "comment:issue:1")
    check("marked target sees it", dispatch_state.has_thread_event(wd, 7, "slack:C1", "comment:issue:1"))
    check("other target does not", not dispatch_state.has_thread_event(wd, 7, "discord:#ops", "comment:issue:1"))


def test_mark_event_is_idempotent(tmp_path):
    wd = str(tmp_path)
    dispatch_state.mark_thread_event(wd, 7, "slack:C1", "root")
    dispatch_state.mark_thread_event(wd, 7, "slack:C1", "root")
    entry = dispatch_state._load(wd)["issues"]["7"]
    check("no duplicate event key stored", entry["thread_events"]["slack:C1"] == ["root"])


# ── record_dispatch preserves thread state ──────────────────────────────────────


def test_record_dispatch_preserves_threads(tmp_path):
    wd = str(tmp_path)
    dispatch_state.set_thread_anchor(wd, 121, "slack:C1", "ts-1")
    dispatch_state.mark_thread_event(wd, 121, "slack:C1", "root")
    dispatch_state.record_dispatch(wd, 121)  # must NOT wipe threads/events
    check("anchor survived record_dispatch", dispatch_state.get_thread_anchor(wd, 121, "slack:C1") == "ts-1")
    check("event survived record_dispatch", dispatch_state.has_thread_event(wd, 121, "slack:C1", "root"))
    check("dispatched_at recorded", dispatch_state.get_dispatch_age_hours(wd, 121) is not None)


def test_clear_dispatch_drops_threads(tmp_path):
    wd = str(tmp_path)
    dispatch_state.set_thread_anchor(wd, 121, "slack:C1", "ts-1")
    dispatch_state.record_dispatch(wd, 121)
    dispatch_state.clear_dispatch(wd, 121)
    check("anchor gone after clear", dispatch_state.get_thread_anchor(wd, 121, "slack:C1") is None)


if __name__ == "__main__":
    import tempfile

    print("dispatch_state thread-anchor tests (issue #121)")
    print("-" * 60)
    for _name, _fn in sorted(
        (n, f) for n, f in globals().items()
        if n.startswith("test_") and callable(f)
    ):
        with tempfile.TemporaryDirectory() as d:
            _fn(Path(d))
    print("-" * 60)
    print(f"Results: {conftest._passed} passed, {conftest._failed} failed")
    sys.exit(1 if conftest._failed else 0)
