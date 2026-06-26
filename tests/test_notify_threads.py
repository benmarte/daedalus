"""Tests for per-issue notification threading (issue #121).

Covers three layers:
  * ``core.dispatch_state`` thread-anchor + dedup primitives;
  * ``core.notify_templates.render_issue_thread_root``;
  * the dispatcher's ``_hermes_send`` / ``_deliver_to_issue_thread`` fan-out,
    with ``hermes send`` stubbed via a fake ``subprocess.run``.

These are plain pytest functions (no standalone ``__main__`` runner) so the
canonical CI runner (``pytest tests/``) is the single source of truth.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import dispatch_state  # noqa: E402
from core import notify_templates  # noqa: E402
from conftest import _load_dispatch  # noqa: E402

disp = _load_dispatch()


# ── dispatch_state thread primitives ──────────────────────────────────────────


def test_anchor_roundtrip(tmp_path):
    wd = str(tmp_path)
    assert dispatch_state.get_thread_anchor(wd, 121, "slack:C1") is None
    dispatch_state.set_thread_anchor(wd, 121, "slack:C1", "1700.0001")
    assert dispatch_state.get_thread_anchor(wd, 121, "slack:C1") == "1700.0001"
    # Independent per target and per issue.
    assert dispatch_state.get_thread_anchor(wd, 121, "discord:9") is None
    assert dispatch_state.get_thread_anchor(wd, 122, "slack:C1") is None


def test_anchor_persisted_keyed_by_issue(tmp_path):
    """AC: thread ids stored in dispatch state keyed by issue number."""
    wd = str(tmp_path)
    dispatch_state.set_thread_anchor(wd, 121, "slack:C1", "ts1")
    dispatch_state.set_thread_anchor(wd, 121, "discord:9", "mid1")
    raw = json.loads((tmp_path / ".hermes" / "daedalus_dispatch_state.json").read_text())
    assert raw["threads"]["121"]["anchors"] == {"slack:C1": "ts1", "discord:9": "mid1"}


def test_set_anchor_ignores_falsy(tmp_path):
    wd = str(tmp_path)
    dispatch_state.set_thread_anchor(wd, 121, "slack:C1", "")
    assert dispatch_state.get_thread_anchor(wd, 121, "slack:C1") is None


def test_event_dedup(tmp_path):
    wd = str(tmp_path)
    assert dispatch_state.thread_event_seen(wd, 121, "slack:C1", "dispatched") is False
    dispatch_state.mark_thread_event(wd, 121, "slack:C1", "dispatched")
    assert dispatch_state.thread_event_seen(wd, 121, "slack:C1", "dispatched") is True
    # Distinct event / target / issue are still unseen.
    assert dispatch_state.thread_event_seen(wd, 121, "slack:C1", "doc-report:5") is False
    assert dispatch_state.thread_event_seen(wd, 121, "discord:9", "dispatched") is False
    # Marking twice is idempotent.
    dispatch_state.mark_thread_event(wd, 121, "slack:C1", "dispatched")
    events = json.loads(
        (tmp_path / ".hermes" / "daedalus_dispatch_state.json").read_text()
    )["threads"]["121"]["events"]["slack:C1"]
    assert events == ["dispatched"]


def test_clear_threads(tmp_path):
    wd = str(tmp_path)
    dispatch_state.set_thread_anchor(wd, 121, "slack:C1", "ts1")
    dispatch_state.mark_thread_event(wd, 121, "slack:C1", "dispatched")
    dispatch_state.clear_threads(wd, 121)
    assert dispatch_state.get_thread_anchor(wd, 121, "slack:C1") is None
    assert dispatch_state.thread_event_seen(wd, 121, "slack:C1", "dispatched") is False


def test_malformed_state_is_tolerated(tmp_path):
    wd = str(tmp_path)
    p = tmp_path / ".hermes" / "daedalus_dispatch_state.json"
    p.parent.mkdir(parents=True)
    p.write_text('{"threads": {"121": {"anchors": "not-a-dict"}}}')
    # Should not raise — malformed anchor reads as "no anchor".
    assert dispatch_state.get_thread_anchor(wd, 121, "slack:C1") is None


# ── template ──────────────────────────────────────────────────────────────────


def test_render_issue_thread_root():
    msg = notify_templates.render_issue_thread_root(
        "acme", 121, "Add Slack threads",
        issue_url="https://example/issues/121", repo="acme/widgets",
    )
    assert "#121: Add Slack threads" in msg
    assert "https://example/issues/121" in msg
    assert "acme/widgets" in msg


# ── _hermes_send ───────────────────────────────────────────────────────────────


class _FakeRun:
    """Records hermes-send invocations and returns canned JSON results."""

    def __init__(self, results):
        # results: list of (returncode, stdout) consumed in order, or a callable
        self.results = results
        self.calls = []

    def __call__(self, argv, capture_output=True, text=True, timeout=None):
        self.calls.append(argv)
        if callable(self.results):
            rc, out = self.results(argv)
        else:
            rc, out = self.results[len(self.calls) - 1]
        return SimpleNamespace(returncode=rc, stdout=out, stderr="")


def _target_of(argv):
    return argv[argv.index("-t") + 1]


def test_hermes_send_returns_message_id(monkeypatch):
    fake = _FakeRun([(0, json.dumps({"success": True, "message_id": "ts-123"}))])
    monkeypatch.setattr(disp.subprocess, "run", fake)
    assert disp._hermes_send("slack:C1", "hello") == "ts-123"
    assert _target_of(fake.calls[0]) == "slack:C1"
    assert "--json" in fake.calls[0]


def test_hermes_send_threads_append_anchor(monkeypatch):
    fake = _FakeRun([(0, json.dumps({"success": True, "message_id": "ts-456"}))])
    monkeypatch.setattr(disp.subprocess, "run", fake)
    disp._hermes_send("slack:C1", "reply", thread_id="ts-123")
    assert _target_of(fake.calls[0]) == "slack:C1:ts-123"


def test_hermes_send_failure_returns_none(monkeypatch):
    fake = _FakeRun([(1, "")])
    monkeypatch.setattr(disp.subprocess, "run", fake)
    assert disp._hermes_send("slack:C1", "hello") is None


def test_hermes_send_error_payload_returns_none(monkeypatch):
    fake = _FakeRun([(0, json.dumps({"error": "channel_not_found"}))])
    monkeypatch.setattr(disp.subprocess, "run", fake)
    assert disp._hermes_send("slack:C1", "hello") is None


def test_send_via_hermes_bool_wrapper(monkeypatch):
    fake = _FakeRun([(0, json.dumps({"success": True})), (1, "")])
    monkeypatch.setattr(disp.subprocess, "run", fake)
    assert disp._send_via_hermes("slack:C1", "ok") is True   # success, no id → ""
    assert disp._send_via_hermes("slack:C1", "fail") is False


# ── _deliver_to_issue_thread ────────────────────────────────────────────────────


def test_thread_opens_root_then_replies(tmp_path, monkeypatch):
    """First event opens a root; the second replies in the stored anchor."""
    wd = str(tmp_path)

    def results(argv):
        return (0, json.dumps({"success": True, "message_id": "ROOT"}))

    fake = _FakeRun(results)
    monkeypatch.setattr(disp.subprocess, "run", fake)

    n = disp._deliver_to_issue_thread(wd, 121, "dispatched", ["slack:C1"], "root msg")
    assert n == 1
    assert _target_of(fake.calls[0]) == "slack:C1"  # root, no thread suffix
    assert dispatch_state.get_thread_anchor(wd, 121, "slack:C1") == "ROOT"

    disp._deliver_to_issue_thread(wd, 121, "doc-report:5", ["slack:C1"], "reply msg")
    assert _target_of(fake.calls[1]) == "slack:C1:ROOT"  # replied in thread


def test_thread_dedup_suppresses_repeat(tmp_path, monkeypatch):
    """AC: the same event is not posted twice across consecutive ticks."""
    wd = str(tmp_path)
    fake = _FakeRun(lambda a: (0, json.dumps({"success": True, "message_id": "ROOT"})))
    monkeypatch.setattr(disp.subprocess, "run", fake)

    disp._deliver_to_issue_thread(wd, 121, "dispatched", ["slack:C1"], "msg")
    before = len(fake.calls)
    n = disp._deliver_to_issue_thread(wd, 121, "dispatched", ["slack:C1"], "msg")
    assert n == 0
    assert len(fake.calls) == before  # no second send


def test_thread_fallback_on_invalid_anchor(tmp_path, monkeypatch):
    """AC: missing/deleted anchor falls back to a new root and re-stores it."""
    wd = str(tmp_path)
    dispatch_state.set_thread_anchor(wd, 121, "slack:C1", "STALE")

    def results(argv):
        # Threaded reply (target has :STALE) fails; bare root succeeds.
        if argv[argv.index("-t") + 1].endswith(":STALE"):
            return (1, "")
        return (0, json.dumps({"success": True, "message_id": "FRESH"}))

    fake = _FakeRun(results)
    monkeypatch.setattr(disp.subprocess, "run", fake)

    n = disp._deliver_to_issue_thread(wd, 121, "update", ["slack:C1"], "msg")
    assert n == 1
    assert _target_of(fake.calls[0]) == "slack:C1:STALE"  # tried the stale anchor
    assert _target_of(fake.calls[1]) == "slack:C1"        # then a fresh root
    assert dispatch_state.get_thread_anchor(wd, 121, "slack:C1") == "FRESH"


def test_thread_failure_left_unmarked_for_retry(tmp_path, monkeypatch):
    """A total send failure must not mark the event seen (so it retries)."""
    wd = str(tmp_path)
    fake = _FakeRun(lambda a: (1, ""))
    monkeypatch.setattr(disp.subprocess, "run", fake)

    n = disp._deliver_to_issue_thread(wd, 121, "dispatched", ["slack:C1"], "msg")
    assert n == 0
    assert dispatch_state.thread_event_seen(wd, 121, "slack:C1", "dispatched") is False


def test_thread_fans_out_to_all_platforms(tmp_path, monkeypatch):
    """AC: works across every configured platform (Slack, Discord, …)."""
    wd = str(tmp_path)

    def results(argv):
        t = argv[argv.index("-t") + 1]
        mid = "TS" if t.startswith("slack") else "MID"
        return (0, json.dumps({"success": True, "message_id": mid}))

    fake = _FakeRun(results)
    monkeypatch.setattr(disp.subprocess, "run", fake)

    n = disp._deliver_to_issue_thread(
        wd, 121, "dispatched", ["slack:C1", "discord:9"], "msg",
    )
    assert n == 2
    assert dispatch_state.get_thread_anchor(wd, 121, "slack:C1") == "TS"
    assert dispatch_state.get_thread_anchor(wd, 121, "discord:9") == "MID"


def test_thread_dry_run_sends_nothing(tmp_path, monkeypatch):
    wd = str(tmp_path)
    fake = _FakeRun(lambda a: (0, "{}"))
    monkeypatch.setattr(disp.subprocess, "run", fake)
    n = disp._deliver_to_issue_thread(
        wd, 121, "dispatched", ["slack:C1"], "msg", dry_run=True,
    )
    assert n == 1
    assert fake.calls == []
    assert dispatch_state.get_thread_anchor(wd, 121, "slack:C1") is None
