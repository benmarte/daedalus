"""Tests for the time-bounded crash-retry reconciler (issue #1205).

Covers the spec's acceptance criteria:
  (a) transient crash → retried on the next tick,
  (b) crash counter resets after the cooldown window,
  (c) retries exhausted → escalate once with diagnostics, no infinite loop,
  (d) success mid-retry → episode cleared (proceeds normally),
  (e) two ticks → at most one re-dispatch (no duplicate concurrent workers),
plus non-crash blocks never auto-unblocked, config-knob resolution, and the
incident reproduction (2 fast crashes → gave_up → auto-recovery, no manual
unblock).

The reconciler's kanban collaborator is replaced by an in-memory FakeKanban
(``core.kanban._hk`` is additionally stubbed board-wide by conftest per the
#1209 isolation pattern, so no test can ever reach a real board).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import crash_retry, dispatch_state  # noqa: E402

T0 = 1_750_000_000.0  # deterministic "now"
MIN = 60.0
HOUR = 3600.0


class FakeKanban:
    """Minimal in-memory stand-in for the core.kanban helpers reconcile uses."""

    def __init__(self, cards: List[Dict[str, Any]]):
        self.cards = {c["id"]: dict(c) for c in cards}
        self.unblocked: List[tuple] = []
        self.comments: Dict[str, List[str]] = {}

    def list_tasks(self, slug: str, status: str = "") -> List[Dict[str, Any]]:
        return [dict(c) for c in self.cards.values()]

    def get_latest_summary(self, slug: str, tid: str) -> str:
        return str(self.cards.get(tid, {}).get("summary") or "")

    def unblock_task(self, slug: str, tid: str, reason: str = "") -> bool:
        self.unblocked.append((tid, reason))
        self.cards[tid]["status"] = "ready"
        return True

    def edit_summary(self, slug: str, tid: str, summary: str) -> bool:
        self.cards[tid]["summary"] = summary
        return True

    def comment(self, slug: str, tid: str, body: str) -> bool:
        self.comments.setdefault(tid, []).append(body)
        return True

    def show_card(self, slug: str, tid: str):
        c = self.cards.get(tid)
        if not c:
            return None
        return {**c, "comments": [{"body": b} for b in self.comments.get(tid, [])]}


@pytest.fixture()
def fake(monkeypatch):
    def _make(cards: List[Dict[str, Any]]) -> FakeKanban:
        fk = FakeKanban(cards)
        monkeypatch.setattr(crash_retry, "kanban", fk)
        return fk

    return _make


def _card(tid="t_1", status="blocked", summary="", title="developer: #42 fix thing",
          assignee="developer-daedalus", **extra):
    return {"id": tid, "status": status, "summary": summary,
            "title": title, "assignee": assignee, **extra}


CRASH = "coding-agent-failed: CODING_AGENT_DIED — see stderr above"


# ── classification ────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "evidence,expected",
    [
        (CRASH, "crash"),
        ("coding_agent_timeout: exceeded 3600s", "crash"),
        ("run 2403 crashed (pid not alive)", "crash"),
        ("ollama APIConnectionError while spawning", "crash"),
        ("permission-error: cannot write /tmp", "crash"),
        ("worker exited with code 137", "crash"),
        ("claude code session limit not yet reset", "crash"),
        ("crash-retries-exhausted: whatever", "crash"),
        ("review-required: PR #12 — fix/issue-42", None),
        ("qa-failed: 3 tests failing", None),
        ("qa-deferred: waiting on sub-issue PRs", None),
        ("ESCALATE: security finding", None),
        ("", None),
        (None, None),
    ],
)
def test_classify(evidence, expected):
    assert crash_retry.classify(evidence) == expected


# ── config resolver ───────────────────────────────────────────────────────────

def test_resolve_config_defaults():
    cfg = crash_retry.resolve_config({})
    assert cfg["crash_retry_enabled"] is True
    assert cfg["max_crash_retries"] == 5
    assert cfg["crash_retry_backoff_minutes"] == [0, 15, 30, 60, 120]
    assert cfg["crash_retry_cooldown_minutes"] == 120
    assert cfg["crash_retry_window_hours"] == 6
    # copies — mutating the result must not corrupt the defaults
    cfg["crash_retry_backoff_minutes"].append(999)
    assert crash_retry.resolve_config({})["crash_retry_backoff_minutes"] == [0, 15, 30, 60, 120]


def test_resolve_config_overrides_and_fallbacks():
    cfg = crash_retry.resolve_config(
        {
            "crash_retry_enabled": False,
            "max_crash_retries": 7,
            "crash_retry_backoff_minutes": [0, 5, 10],
            "crash_retry_cooldown_minutes": "nope",  # invalid → default
            "crash_retry_window_hours": -3,  # non-positive → default
        }
    )
    assert cfg["crash_retry_enabled"] is False
    assert cfg["max_crash_retries"] == 7
    assert cfg["crash_retry_backoff_minutes"] == [0, 5, 10]
    assert cfg["crash_retry_cooldown_minutes"] == 120
    assert cfg["crash_retry_window_hours"] == 6


@pytest.mark.parametrize("bad", [[], "15,30", [5, -1], ["x"], None])
def test_resolve_config_bad_backoff_falls_back(bad):
    cfg = crash_retry.resolve_config({"crash_retry_backoff_minutes": bad})
    assert cfg["crash_retry_backoff_minutes"] == [0, 15, 30, 60, 120]


def test_disabled_reconciler_is_noop(fake, tmp_path):
    fk = fake([_card(status="gave_up", summary=CRASH)])
    actions = crash_retry.reconcile(
        "board", str(tmp_path), {"crash_retry_enabled": False}, now=T0
    )
    assert actions == [] and fk.unblocked == []


# ── state persistence ─────────────────────────────────────────────────────────

def test_state_roundtrip(tmp_path):
    wd = str(tmp_path)
    assert dispatch_state.get_crash_retry(wd, "t_x") is None
    entry = {"first_crash_ts": T0, "attempts": 2, "last_attempt_ts": T0,
             "escalated": False, "class": "crash"}
    dispatch_state.set_crash_retry(wd, "t_x", entry)
    assert dispatch_state.get_crash_retry(wd, "t_x") == entry
    assert dispatch_state.all_crash_retry(wd) == {"t_x": entry}
    dispatch_state.clear_crash_retry(wd, "t_x")
    assert dispatch_state.get_crash_retry(wd, "t_x") is None
    dispatch_state.clear_crash_retry(wd, "t_x")  # idempotent


def test_state_tolerates_malformed_table(tmp_path):
    wd = str(tmp_path)
    dispatch_state._save(wd, {"crash_retry": "garbage"})
    assert dispatch_state.get_crash_retry(wd, "t_x") is None
    assert dispatch_state.all_crash_retry(wd) == {}
    dispatch_state.set_crash_retry(wd, "t_x", {"attempts": 1})
    assert dispatch_state.get_crash_retry(wd, "t_x") == {"attempts": 1}


# ── (a) crash → retried on next tick ─────────────────────────────────────────

def test_crash_blocked_card_retried_next_tick(fake, tmp_path):
    fk = fake([_card(summary=CRASH)])
    actions = crash_retry.reconcile("board", str(tmp_path), {}, now=T0)
    assert [a["action"] for a in actions] == ["retried"]
    assert actions[0]["attempt"] == 1 and actions[0]["issue"] == 42
    tid, reason = fk.unblocked[0]
    assert tid == "t_1" and "attempt 1/5" in reason
    entry = dispatch_state.get_crash_retry(str(tmp_path), "t_1")
    assert entry["attempts"] == 1 and entry["escalated"] is False


def test_gave_up_card_retried_even_without_crash_summary(fake, tmp_path):
    fk = fake([_card(status="gave_up", summary="")])
    actions = crash_retry.reconcile("board", str(tmp_path), {}, now=T0)
    assert [a["action"] for a in actions] == ["retried"]
    assert len(fk.unblocked) == 1


def test_blocked_card_classified_via_last_failure_error(fake, tmp_path):
    fk = fake([_card(summary="", last_failure_error="spawn failure: pid not alive")])
    actions = crash_retry.reconcile("board", str(tmp_path), {}, now=T0)
    assert [a["action"] for a in actions] == ["retried"]
    assert len(fk.unblocked) == 1


def test_breaker_blocked_card_detected_via_gave_up_event(fake, tmp_path):
    """Primary incident case: the hermes-core breaker blocks the card with an
    EMPTY summary — the crash only exists as a ``gave_up`` task event."""
    fk = fake([_card(summary="",
                     events=[{"type": "blocked"}, {"type": "unblocked"},
                             {"type": "gave_up", "error": "pid not alive (run 2484)"}])])
    actions = crash_retry.reconcile("board", str(tmp_path), {}, now=T0)
    assert [a["action"] for a in actions] == ["retried"]
    assert "pid not alive" in fk.unblocked[0][1]


def test_blocked_event_after_gave_up_is_not_crash_class(fake, tmp_path):
    """A worker/human block AFTER the breaker episode owns the card — the most
    recent lifecycle event is ``blocked``, so the reconciler must not touch it."""
    fk = fake([_card(summary="",
                     events=[{"type": "gave_up", "error": "pid not alive"},
                             {"type": "unblocked"},
                             {"type": "blocked", "reason": "human hold"}])])
    actions = crash_retry.reconcile("board", str(tmp_path), {}, now=T0)
    assert actions == [] and fk.unblocked == []


def test_blocked_card_without_events_or_summary_untouched(fake, tmp_path):
    fk = fake([_card(summary="")])
    actions = crash_retry.reconcile("board", str(tmp_path), {}, now=T0)
    assert actions == [] and fk.unblocked == []


def test_non_crash_blocks_never_auto_unblocked(fake, tmp_path):
    fk = fake(
        [
            _card(tid="t_r", summary="review-required: PR #7 — fix/issue-42"),
            _card(tid="t_q", summary="qa-failed: lint broken"),
            _card(tid="t_d", summary="qa-deferred: waiting on sub-issue PRs"),
            _card(tid="t_e", summary="ESCALATE: secrets in diff"),
            _card(tid="t_h", summary="human: on hold until design review"),
            _card(tid="t_run", status="running", summary=CRASH),
        ]
    )
    actions = crash_retry.reconcile("board", str(tmp_path), {}, now=T0)
    assert actions == [] and fk.unblocked == []


def test_backoff_schedule_gates_next_retry(fake, tmp_path):
    wd = str(tmp_path)
    fk = fake([_card(summary=CRASH)])
    crash_retry.reconcile("board", wd, {}, now=T0)  # attempt 1 (schedule[0]=0 → immediate)
    fk.cards["t_1"]["status"] = "blocked"  # crashed again right away
    actions = crash_retry.reconcile("board", wd, {}, now=T0 + 5 * MIN)
    assert actions == [] and len(fk.unblocked) == 1  # inside the 15-min step
    actions = crash_retry.reconcile("board", wd, {}, now=T0 + 16 * MIN)
    assert [a["action"] for a in actions] == ["retried"]
    assert actions[0]["attempt"] == 2
    assert len(fk.unblocked) == 2


# ── (b) cooldown resets the counter ───────────────────────────────────────────

def test_cooldown_resets_episode(fake, tmp_path):
    wd = str(tmp_path)
    fake([_card(summary=CRASH)])
    dispatch_state.set_crash_retry(
        wd, "t_1",
        {"first_crash_ts": T0 - 5 * HOUR, "attempts": 4,
         "last_attempt_ts": T0 - 3 * HOUR, "escalated": False, "class": "crash"},
    )
    actions = crash_retry.reconcile("board", wd, {}, now=T0)
    assert [a["action"] for a in actions] == ["retried"]
    assert actions[0]["attempt"] == 1  # fresh episode, not 5
    entry = dispatch_state.get_crash_retry(wd, "t_1")
    assert entry["attempts"] == 1 and entry["first_crash_ts"] == T0


# ── (c) exhaustion → escalate once with diagnostics, no loop ─────────────────

def test_attempt_cap_escalates_once_with_diagnostics(fake, tmp_path):
    wd = str(tmp_path)
    fk = fake([_card(summary=CRASH)])
    dispatch_state.set_crash_retry(
        wd, "t_1",
        {"first_crash_ts": T0 - HOUR, "attempts": 5,
         "last_attempt_ts": T0 - 20 * MIN, "escalated": False, "class": "crash"},
    )
    actions = crash_retry.reconcile("board", wd, {}, now=T0)
    assert [a["action"] for a in actions] == ["escalated"]
    assert fk.unblocked == []
    assert dispatch_state.get_crash_retry(wd, "t_1")["escalated"] is True
    # Card hard-blocked with the exhausted reason + marker-deduped diagnostics.
    assert fk.cards["t_1"]["summary"].startswith("crash-retries-exhausted:")
    stamped = "\n".join(fk.comments["t_1"])
    assert crash_retry.ESCALATED_MARKER in stamped
    assert "5/5" in stamped and "CODING_AGENT_DIED" in stamped
    # Subsequent ticks — including past the cooldown — stay terminal:
    # no unblock, no duplicate comment, no re-notification.
    for dt in (MIN, HOUR, 10 * HOUR):
        assert crash_retry.reconcile("board", wd, {}, now=T0 + dt) == []
    assert fk.unblocked == [] and len(fk.comments["t_1"]) == 1


def test_wall_clock_window_escalates(fake, tmp_path):
    wd = str(tmp_path)
    fk = fake([_card(summary=CRASH)])
    dispatch_state.set_crash_retry(
        wd, "t_1",
        {"first_crash_ts": T0 - 7 * HOUR, "attempts": 2,
         "last_attempt_ts": T0 - 90 * MIN, "escalated": False, "class": "crash"},
    )
    actions = crash_retry.reconcile("board", wd, {}, now=T0)
    assert [a["action"] for a in actions] == ["escalated"]
    assert fk.unblocked == []


def test_escalation_marker_on_card_survives_state_loss(fake, tmp_path):
    wd = str(tmp_path)
    fk = fake([_card(summary=CRASH)])
    fk.comments["t_1"] = [crash_retry.ESCALATED_MARKER + "\nolder escalation"]
    dispatch_state.set_crash_retry(
        wd, "t_1",
        {"first_crash_ts": T0 - HOUR, "attempts": 5,
         "last_attempt_ts": T0 - 20 * MIN, "escalated": False, "class": "crash"},
    )
    actions = crash_retry.reconcile("board", wd, {}, now=T0)
    assert actions == []  # dedup — no double notification
    assert len(fk.comments["t_1"]) == 1
    assert dispatch_state.get_crash_retry(wd, "t_1")["escalated"] is True


# ── (d) success mid-retry clears the episode ──────────────────────────────────

def test_recovered_card_clears_state(fake, tmp_path):
    wd = str(tmp_path)
    fk = fake([_card(summary=CRASH)])
    crash_retry.reconcile("board", wd, {}, now=T0)
    assert dispatch_state.get_crash_retry(wd, "t_1") is not None
    fk.cards["t_1"]["status"] = "done"  # worker succeeded this time
    crash_retry.reconcile("board", wd, {}, now=T0 + 10 * MIN)
    assert dispatch_state.get_crash_retry(wd, "t_1") is None


def test_manual_unblock_of_escalated_card_resets_counter(fake, tmp_path):
    wd = str(tmp_path)
    fk = fake([_card(summary=CRASH)])
    dispatch_state.set_crash_retry(
        wd, "t_1",
        {"first_crash_ts": T0, "attempts": 5, "last_attempt_ts": T0,
         "escalated": True, "class": "crash"},
    )
    fk.cards["t_1"]["status"] = "ready"  # human ran `hermes kanban unblock`
    crash_retry.reconcile("board", wd, {}, now=T0 + MIN)
    assert dispatch_state.get_crash_retry(wd, "t_1") is None


# ── (e) idempotency / no duplicate workers ────────────────────────────────────

def test_two_ticks_same_instant_single_redispatch(fake, tmp_path):
    wd = str(tmp_path)
    fk = fake([_card(summary=CRASH)])
    crash_retry.reconcile("board", wd, {}, now=T0)
    # Second tick at the SAME instant (lock-loser rerun / racing cron): the
    # attempt was persisted before the unblock, so backoff holds the card.
    fk.cards["t_1"]["status"] = "blocked"
    crash_retry.reconcile("board", wd, {}, now=T0)
    assert len(fk.unblocked) == 1


def test_dry_run_makes_no_mutations(fake, tmp_path):
    wd = str(tmp_path)
    fk = fake([_card(summary=CRASH)])
    actions = crash_retry.reconcile("board", wd, {}, now=T0, dry_run=True)
    assert actions == []
    assert fk.unblocked == [] and fk.comments == {}
    assert dispatch_state.all_crash_retry(wd) == {}


def test_unblock_failure_backs_off_not_loops(fake, tmp_path, monkeypatch):
    wd = str(tmp_path)
    fk = fake([_card(summary=CRASH)])
    monkeypatch.setattr(fk, "unblock_task", lambda *a, **k: False)
    actions = crash_retry.reconcile("board", wd, {}, now=T0)
    assert actions == []
    # Attempt was still recorded → next immediate tick stays in backoff.
    assert dispatch_state.get_crash_retry(wd, "t_1")["attempts"] == 1


def test_kanban_error_on_one_card_does_not_break_the_tick(fake, tmp_path, monkeypatch):
    wd = str(tmp_path)
    fk = fake([_card(tid="t_bad", summary=CRASH), _card(tid="t_ok", summary=CRASH)])
    real_unblock = fk.unblock_task

    def _unblock(slug, tid, reason=""):
        if tid == "t_bad":
            raise RuntimeError("kanban exploded")
        return real_unblock(slug, tid, reason)

    monkeypatch.setattr(fk, "unblock_task", _unblock)
    actions = crash_retry.reconcile("board", wd, {}, now=T0)
    assert [a["task_id"] for a in actions] == ["t_ok"]


# ── incident reproduction (card t_34adae1f, 2026-07-02) ──────────────────────

def test_incident_two_fast_crashes_auto_recover_without_manual_unblock(fake, tmp_path):
    """2 crashes within a minute exhaust hermes-core's failure_limit=2 and the
    breaker parks the card (gave_up). The next dispatch tick must auto-unblock
    it — the 46-minute manual-unblock strand from the incident must not recur."""
    wd = str(tmp_path)
    fk = fake([_card(tid="t_34adae1f", status="gave_up",
                     summary="run 2484 crashed (pid not alive)",
                     title="developer: #1198 fix dispatch")])
    # Tick 1 (cron or on_session_end): auto re-dispatch, no human involved.
    actions = crash_retry.reconcile("board", wd, {}, now=T0)
    assert [a["action"] for a in actions] == ["retried"]
    assert fk.unblocked and fk.unblocked[0][0] == "t_34adae1f"
    # The session limit has reset by the next crash-free run → card completes.
    fk.cards["t_34adae1f"]["status"] = "done"
    crash_retry.reconcile("board", wd, {}, now=T0 + 20 * MIN)
    assert dispatch_state.all_crash_retry(wd) == {}
