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


def _card(
    tid="t_1",
    status="blocked",
    summary="",
    title="developer: #42 fix thing",
    assignee="developer-daedalus",
    **extra,
):
    return {
        "id": tid,
        "status": status,
        "summary": summary,
        "title": title,
        "assignee": assignee,
        **extra,
    }


CRASH = "coding-agent-failed: CODING_AGENT_DIED — see stderr above"


# ── classification ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "evidence,expected",
    [
        # #1207: classify returns the trigger CLASS (all truthy — every one of
        # these remains crash-class for the reconciler's is-None checks).
        (CRASH, "crash"),
        ("coding_agent_timeout: exceeded 3600s", "timeout"),
        ("run 2403 crashed (pid not alive)", "crash"),
        ("ollama APIConnectionError while spawning", "api_connection_error"),
        ("permission-error: cannot write /tmp", "crash"),
        ("worker exited with code 137", "crash"),
        ("claude code session limit not yet reset", "session_limit"),
        ("usage limit reached", "session_limit"),
        ("quota exceeded for project", "quota_exceeded"),
        ("429 rate limit from provider", "quota_exceeded"),
        ("crash-retries-exhausted: whatever", "crash"),
        ("review-required: PR #12 — fix/issue-42", None),
        ("qa-failed: 3 tests failing", None),
        ("qa-deferred: waiting on sub-issue PRs", None),
        ("ESCALATE: security finding", None),
        # #1207 review fix: pipeline-owned prefixes must return None even when
        # the text contains generic quota/rate-limit markers that would
        # otherwise classify as quota_exceeded.
        ("qa-failed: rate limit tests failing", None),
        ("review-required: quota exceeded in tests", None),
        ("escalate: rate limit issue", None),
        ("qa-deferred: waiting on sub-issue PRs", None),
        ("awaiting-fix: t_abc123", None),
        ("awaiting-pr — no PR yet", None),
        ("", None),
        (None, None),
        # #1211: non-crash prefixes suppress crash classification even when a
        # crash marker (e.g. "usage limit", "session limit") appears later in
        # the evidence text — the block is owned by iterate/PM/QA, not the
        # crash-retry reconciler.
        ("review-required: PR #123 — usage limit exceeded", None),
        ("review-changes-requested: usage limit on provider X", None),
        ("qa-failed: tests failed, session limit hit", None),
        ("qa-fix: usage limit exceeded in test run", None),
        ("escalate: usage limit on provider", None),
        ("pm-route: session limit hit during dispatch", None),
        ("awaiting-fix: t_abc123 usage limit", None),
        ("awaiting-pr: PR #99 — usage limit", None),
        ("pending-pr: session limit exceeded", None),
        ("a11y-skipped: usage limit on vision provider", None),
        ("spec: PM completed despite usage limit", None),
        # Genuine crash prefixes are NOT exempt — crash markers inside a
        # real crash message still classify as crash-class (truthy, not None).
        # Note: "usage limit" in the text matches session_limit (checked first
        # in _TRIGGER_MARKERS) — the point is it's NOT None (not exempt).
        ("coding-agent-failed: agent died, usage limit", "session_limit"),
        ("agent crash: process exited with code 1", "crash"),
        ("coding_agent_died: session limit reached", "session_limit"),
        # Bare quota errors (no non-crash prefix) are still crash-class.
        ("usage limit exceeded", "session_limit"),
        ("session limit hit", "session_limit"),
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
    assert crash_retry.resolve_config({})["crash_retry_backoff_minutes"] == [
        0,
        15,
        30,
        60,
        120,
    ]


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
    entry = {
        "first_crash_ts": T0,
        "attempts": 2,
        "last_attempt_ts": T0,
        "escalated": False,
        "class": "crash",
    }
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
    fk = fake(
        [
            _card(
                summary="",
                events=[
                    {"type": "blocked"},
                    {"type": "unblocked"},
                    {"type": "gave_up", "error": "pid not alive (run 2484)"},
                ],
            )
        ]
    )
    actions = crash_retry.reconcile("board", str(tmp_path), {}, now=T0)
    assert [a["action"] for a in actions] == ["retried"]
    assert "pid not alive" in fk.unblocked[0][1]


def test_blocked_event_after_gave_up_is_not_crash_class(fake, tmp_path):
    """A worker/human block AFTER the breaker episode owns the card — the most
    recent lifecycle event is ``blocked``, so the reconciler must not touch it."""
    fk = fake(
        [
            _card(
                summary="",
                events=[
                    {"type": "gave_up", "error": "pid not alive"},
                    {"type": "unblocked"},
                    {"type": "blocked", "reason": "human hold"},
                ],
            )
        ]
    )
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
    crash_retry.reconcile(
        "board", wd, {}, now=T0
    )  # attempt 1 (schedule[0]=0 → immediate)
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
        wd,
        "t_1",
        {
            "first_crash_ts": T0 - 5 * HOUR,
            "attempts": 4,
            "last_attempt_ts": T0 - 3 * HOUR,
            "escalated": False,
            "class": "crash",
        },
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
        wd,
        "t_1",
        {
            "first_crash_ts": T0 - HOUR,
            "attempts": 5,
            "last_attempt_ts": T0 - 20 * MIN,
            "escalated": False,
            "class": "crash",
        },
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
        wd,
        "t_1",
        {
            "first_crash_ts": T0 - 7 * HOUR,
            "attempts": 2,
            "last_attempt_ts": T0 - 90 * MIN,
            "escalated": False,
            "class": "crash",
        },
    )
    actions = crash_retry.reconcile("board", wd, {}, now=T0)
    assert [a["action"] for a in actions] == ["escalated"]
    assert fk.unblocked == []


def test_escalation_marker_on_card_survives_state_loss(fake, tmp_path):
    wd = str(tmp_path)
    fk = fake([_card(summary=CRASH)])
    fk.comments["t_1"] = [crash_retry.ESCALATED_MARKER + "\nolder escalation"]
    dispatch_state.set_crash_retry(
        wd,
        "t_1",
        {
            "first_crash_ts": T0 - HOUR,
            "attempts": 5,
            "last_attempt_ts": T0 - 20 * MIN,
            "escalated": False,
            "class": "crash",
        },
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
        wd,
        "t_1",
        {
            "first_crash_ts": T0,
            "attempts": 5,
            "last_attempt_ts": T0,
            "escalated": True,
            "class": "crash",
        },
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


# ── dispatcher integration ────────────────────────────────────────────────────

from unittest import mock  # noqa: E402

from conftest import _load_dispatch  # noqa: E402

disp = _load_dispatch()


def _blocked_card(tid, assignee, summary, title):
    return {
        "id": tid,
        "assignee": assignee,
        "summary": summary,
        "last_summary": summary,
        "title": title,
        "status": "blocked",
    }


def test_team_blockers_skip_crash_class_summary():
    """Crash-class blocks get a REAL re-dispatch from the reconciler — the
    advisory-only PM consultation must not be created for them."""
    card = _blocked_card(
        "t_c1",
        "developer-daedalus",
        "coding-agent-failed: CODING_AGENT_DIED — see stderr above",
        "#75 Developer: fix bug",
    )
    issue = {"number": 75, "title": "fix bug", "body": ""}
    with (
        mock.patch.object(disp.kanban, "list_blocked", return_value=[card]),
        mock.patch.object(
            disp.kanban, "get_latest_summary", return_value=card["summary"]
        ),
        mock.patch.object(disp.kanban, "list_tasks", return_value=[]),
        mock.patch.object(disp.kanban, "create_task") as mk_create,
    ):
        triggered = disp._check_team_blockers(
            "slug",
            "org/repo",
            {75: issue},
            "/w",
            "dev",
            "github",
        )
    assert mk_create.call_count == 0 and triggered == []


def test_team_blockers_skip_breaker_gave_up_event_card():
    """Breaker-blocked card (empty summary, gave_up event) is crash-class."""
    card = _blocked_card("t_c2", "developer-daedalus", "", "#76 Developer: fix bug")
    shown = {
        **card,
        "events": [{"type": "gave_up", "error": "pid not alive"}],
        "comments": [],
    }
    issue = {"number": 76, "title": "fix bug", "body": ""}
    with (
        mock.patch.object(disp.kanban, "list_blocked", return_value=[card]),
        mock.patch.object(disp.kanban, "get_latest_summary", return_value=""),
        mock.patch.object(disp.kanban, "show_card", return_value=shown),
        mock.patch.object(disp.kanban, "list_tasks", return_value=[]),
        mock.patch.object(disp.kanban, "create_task") as mk_create,
    ):
        triggered = disp._check_team_blockers(
            "slug",
            "org/repo",
            {76: issue},
            "/w",
            "dev",
            "github",
        )
    assert mk_create.call_count == 0 and triggered == []


def test_team_blockers_still_consult_for_genuine_blocker():
    card = _blocked_card(
        "t_c3",
        "developer-daedalus",
        "cannot determine VCS provider credentials",
        "#77 Developer: fix auth bug",
    )
    issue = {"number": 77, "title": "fix auth bug", "body": ""}
    with (
        mock.patch.object(disp.kanban, "list_blocked", return_value=[card]),
        mock.patch.object(
            disp.kanban, "get_latest_summary", return_value=card["summary"]
        ),
        mock.patch.object(
            disp.kanban,
            "show_card",
            return_value={**card, "events": [], "comments": []},
        ),
        mock.patch.object(disp.kanban, "list_tasks", return_value=[]),
        mock.patch.object(
            disp.kanban, "create_task", return_value="t_consult"
        ) as mk_create,
    ):
        triggered = disp._check_team_blockers(
            "slug",
            "org/repo",
            {77: issue},
            "/w",
            "dev",
            "github",
        )
    assert mk_create.call_count == 1 and triggered == [77]


def test_enforce_validator_blocks_skips_crash_class():
    """An infrastructure crash is not a validator verdict — no board 'Blocked'
    enforcement, no downstream cancellation."""
    card = _blocked_card(
        "t_v1",
        "validator-daedalus",
        "coding-agent-failed: CODING_AGENT_TIMEOUT — see stderr above",
        "#78 Validator: confirm bug",
    )
    provider = mock.Mock()
    provider.board_configured.return_value = True
    with (
        mock.patch.object(disp.kanban, "list_blocked", return_value=[card]),
        mock.patch.object(disp.kanban, "close_non_blocked_issue_tasks") as mk_close,
    ):
        enforced = disp._enforce_validator_blocks("slug", provider, {78})
    assert enforced == []
    provider.board_set_status.assert_not_called()
    mk_close.assert_not_called()


def test_crash_notification_falls_back_to_retry_cap_targets():
    action = {
        "action": "escalated",
        "task_id": "t_1",
        "issue": 42,
        "attempt": 5,
        "max_attempts": 5,
        "elapsed_minutes": 90.0,
        "summary": "pid not alive",
        "title": "#42",
        "assignee": "developer-daedalus",
    }

    def _targets(resolved, event):
        return {"retry-cap-exhausted": ["slack:C123"]}.get(event, [])

    with (
        mock.patch.object(disp, "_notify_targets", side_effect=_targets),
        mock.patch.object(disp, "_hermes_send", return_value=(True, None)) as mk_send,
        mock.patch.object(disp, "send_webhook_notification"),
    ):
        disp._send_crash_retries_exhausted_notification(
            action=action, resolved={}, dry_run=False
        )
    assert mk_send.call_count == 1
    target, body = mk_send.call_args[0]
    assert target == "slack:C123"
    assert "Crash Retries Exhausted" in body and "#42" in body
    assert "pid not alive" in body and "hermes kanban unblock t_1" in body


def test_crash_notification_dry_run_sends_nothing():
    action = {
        "action": "escalated",
        "task_id": "t_1",
        "issue": 42,
        "attempt": 5,
        "max_attempts": 5,
        "elapsed_minutes": 90.0,
        "summary": "x",
        "title": "#42",
        "assignee": "developer-daedalus",
    }
    with (
        mock.patch.object(disp, "_notify_targets", return_value=["slack:C123"]),
        mock.patch.object(disp, "_hermes_send") as mk_send,
        mock.patch.object(disp, "send_webhook_notification") as mk_hook,
    ):
        disp._send_crash_retries_exhausted_notification(
            action=action, resolved={}, dry_run=True
        )
    mk_send.assert_not_called()
    mk_hook.assert_not_called()


def test_notify_events_include_crash_retries_exhausted():
    assert "crash-retries-exhausted" in disp.NOTIFY_EVENTS


# ── incident reproduction (card t_34adae1f, 2026-07-02) ──────────────────────


def test_incident_two_fast_crashes_auto_recover_without_manual_unblock(fake, tmp_path):
    """2 crashes within a minute exhaust hermes-core's failure_limit=2 and the
    breaker parks the card (gave_up). The next dispatch tick must auto-unblock
    it — the 46-minute manual-unblock strand from the incident must not recur."""
    wd = str(tmp_path)
    fk = fake(
        [
            _card(
                tid="t_34adae1f",
                status="gave_up",
                summary="run 2484 crashed (pid not alive)",
                title="developer: #1198 fix dispatch",
            )
        ]
    )
    # Tick 1 (cron or on_session_end): auto re-dispatch, no human involved.
    actions = crash_retry.reconcile("board", wd, {}, now=T0)
    assert [a["action"] for a in actions] == ["retried"]
    assert fk.unblocked and fk.unblocked[0][0] == "t_34adae1f"
    # The session limit has reset by the next crash-free run → card completes.
    fk.cards["t_34adae1f"]["status"] = "done"
    crash_retry.reconcile("board", wd, {}, now=T0 + 20 * MIN)
    assert dispatch_state.all_crash_retry(wd) == {}
