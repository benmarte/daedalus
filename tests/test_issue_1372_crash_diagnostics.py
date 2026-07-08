"""Tests for gate-agent crash diagnostics + bounded convergence (issue #1372).

Covers the acceptance criteria:
  (AC1) a crashed agent's redacted failure cause (worker-log tail) is attached
        to the card and surfaced in the returned action (→ history.jsonl),
  (AC2) each crash action carries its pipeline role + trigger class so the
        dispatcher can emit per-stage crash counts into history telemetry,
  (AC3) a simulated inner-agent death converges within the configured attempt
        cap and escalates exactly once past it (no infinite loop),
  (AC4) no behaviour change when agents do not crash (no log read, no comment).

Plus the shared secret-redaction helper (#1372) that scrubs the captured tail.

The reconciler's kanban collaborator is an in-memory FakeKanban that also
implements ``worker_log_tail`` (the new #1372 capture surface); ``core.kanban
._hk`` is additionally stubbed board-wide by conftest per #1209 so no test can
reach a real board.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from conftest import _load_dispatch  # noqa: E402

from core import crash_retry, dispatch_state  # noqa: E402
from core.providers.http import redact_secrets  # noqa: E402

disp = _load_dispatch()

T0 = 1_750_000_000.0
MIN = 60.0
HOUR = 3600.0

CRASH = "coding-agent-failed: CODING_AGENT_DIED — see stderr above"
# A worker-log tail that embeds a credential the redactor must scrub.
LOG_TAIL = (
    "Traceback (most recent call last):\n"
    "  File 'agent.py', line 42, in run\n"
    "    raise ConnectionError('ollama APIConnectionError')\n"
    "cloning https://x:ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789@github.com/o/r\n"
    "ConnectionError: ollama APIConnectionError while spawning\n"
)


class FakeKanban:
    """In-memory stand-in exposing the surfaces reconcile() + #1372 use."""

    def __init__(self, cards: List[Dict[str, Any]], log_tail: str = ""):
        self.cards = {c["id"]: dict(c) for c in cards}
        self.unblocked: List[tuple] = []
        self.comments: Dict[str, List[str]] = {}
        self.log_tail = log_tail
        self.log_calls: List[str] = []

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

    def worker_log_tail(self, slug: str, tid: str, *, tail_bytes: int = 6000) -> str:
        self.log_calls.append(tid)
        return self.log_tail


@pytest.fixture()
def fake(monkeypatch):
    def _make(cards: List[Dict[str, Any]], log_tail: str = "") -> FakeKanban:
        fk = FakeKanban(cards, log_tail=log_tail)
        monkeypatch.setattr(crash_retry, "kanban", fk)
        return fk

    return _make


def _card(
    tid="t_1",
    status="blocked",
    summary=CRASH,
    title="reviewer: #42 review PR",
    assignee="reviewer-daedalus",
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


# ── redaction helper (#1372) ─────────────────────────────────────────────────


def test_redact_secrets_scrubs_env_token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_supersecretvalue1234567890")
    out = redact_secrets("auth failed with token ghp_supersecretvalue1234567890 in url")
    assert "ghp_supersecretvalue1234567890" not in out
    assert "<REDACTED>" in out


@pytest.mark.parametrize(
    "secret",
    [
        "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
        "github_pat_11ABCDEFG0123456789_abcdefghijklmnop",
        "glpat-abcdefghijklmnop1234",
        "xoxb-1234567890-abcdefghijk",
        "sk-ant-abcdefghijklmnop1234567890",
        "AKIAIOSFODNN7EXAMPLE",
    ],
)
def test_redact_secrets_scrubs_token_shapes(secret):
    out = redact_secrets(f"leaked {secret} here")
    assert secret not in out
    assert "<REDACTED>" in out


def test_redact_secrets_scrubs_url_credentials():
    out = redact_secrets("cloning https://user:tok_deadbeef@github.com/o/r")
    assert "tok_deadbeef" not in out
    assert "https://<REDACTED>@github.com/o/r" in out


def test_redact_secrets_passes_clean_text_unchanged():
    clean = "ConnectionError: ollama APIConnectionError while spawning"
    assert redact_secrets(clean) == clean


def test_redact_secrets_handles_empty():
    assert redact_secrets("") == ""


# ── AC1: failure cause captured on the card + in the action ──────────────────


def test_retry_attaches_redacted_diagnostics_to_card(fake, tmp_path):
    fk = fake([_card()], log_tail=LOG_TAIL)
    actions = crash_retry.reconcile("board", str(tmp_path), {}, now=T0)
    assert [a["action"] for a in actions] == ["retried"]
    # The worker log was read for the crashed card.
    assert fk.log_calls == ["t_1"]
    # A diagnostics comment landed on the card, credential scrubbed.
    stamped = "\n".join(fk.comments.get("t_1", []))
    assert crash_retry.DIAG_MARKER in stamped
    assert "APIConnectionError" in stamped
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" not in stamped
    assert "<REDACTED>" in stamped
    # The returned action carries the redacted tail (→ history.jsonl).
    diag = actions[0]["diagnostics"]
    assert "APIConnectionError" in diag and "<REDACTED>" in diag
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" not in diag


def test_retry_without_worker_log_posts_no_diagnostics_comment(fake, tmp_path):
    # No worker log available → fall back to block evidence; no noisy comment
    # (the unblock reason already carries the evidence).
    fk = fake([_card()], log_tail="")
    actions = crash_retry.reconcile("board", str(tmp_path), {}, now=T0)
    assert [a["action"] for a in actions] == ["retried"]
    assert fk.comments.get("t_1", []) == []
    # Diagnostics still records the failure cause from the evidence.
    assert "CODING_AGENT_DIED" in actions[0]["diagnostics"]


# ── AC2: per-stage role + class on every crash action ────────────────────────


def test_action_carries_role_and_class(fake, tmp_path):
    fk = fake([_card(assignee="security-analyst-daedalus", summary=CRASH)], log_tail="")
    actions = crash_retry.reconcile("board", str(tmp_path), {}, now=T0)
    assert actions[0]["role"] == "security-analyst"
    assert actions[0]["class"] == "crash"


def test_role_falls_back_to_title(fake, tmp_path):
    fk = fake([_card(assignee="", title="accessibility: #42 audit", summary=CRASH)])
    actions = crash_retry.reconcile("board", str(tmp_path), {}, now=T0)
    assert actions[0]["role"] == "accessibility"


# ── AC3: bounded convergence + escalation past the cap ───────────────────────


def test_inner_agent_death_converges_and_escalates_once(fake, tmp_path):
    """A perpetually-crashing gate agent: each tick re-dispatches, the crash
    recurs, and after ``max_crash_retries`` the reconciler escalates ONCE and
    then stays terminal — a bounded loop, not an infinite one."""
    wd = str(tmp_path)
    cap = 3
    cfg = {"max_crash_retries": cap, "crash_retry_backoff_minutes": [0]}
    fk = fake([_card(summary=CRASH)], log_tail=LOG_TAIL)

    outcomes: List[str] = []
    now = T0
    # Simulate many ticks; the agent dies every time (card stays blocked).
    for _ in range(cap + 5):
        acts = crash_retry.reconcile("board", wd, cfg, now=now)
        for a in acts:
            outcomes.append(a["action"])
        # Inner agent died again → card is blocked once more for the next tick.
        fk.cards["t_1"]["status"] = "blocked"
        now += MIN  # advance past the [0]-minute backoff step

    # Bounded: exactly `cap` retries then exactly one escalation, nothing after.
    assert outcomes == ["retried"] * cap + ["escalated"]
    # Terminal: further ticks do nothing (no re-dispatch, no re-notify).
    for dt in (MIN, HOUR, 10 * HOUR):
        assert crash_retry.reconcile("board", wd, cfg, now=now + dt) == []
    entry = dispatch_state.get_crash_retry(wd, "t_1")
    assert entry["escalated"] is True


def test_escalation_action_and_card_carry_diagnostics(fake, tmp_path):
    wd = str(tmp_path)
    fk = fake([_card(summary=CRASH)], log_tail=LOG_TAIL)
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
    esc = actions[0]
    assert esc["role"] == "reviewer" and esc["class"] == "crash"
    assert "APIConnectionError" in esc["diagnostics"]
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" not in esc["diagnostics"]
    stamped = "\n".join(fk.comments["t_1"])
    assert crash_retry.ESCALATED_MARKER in stamped
    assert "Diagnostics (redacted tail)" in stamped
    assert "<REDACTED>" in stamped
    # Single escalation comment — no duplicate on later ticks.
    assert crash_retry.reconcile("board", wd, {}, now=T0 + HOUR) == []
    assert len(fk.comments["t_1"]) == 1


# ── AC2: per-stage crash telemetry lands in the dispatch history record ──────


def test_summarize_crash_telemetry_counts_per_role():
    actions = [
        {"action": "retried", "task_id": "t_1", "issue": 42, "role": "reviewer",
         "class": "crash", "attempt": 1, "max_attempts": 5, "diagnostics": "boom"},
        {"action": "escalated", "task_id": "t_2", "issue": 43, "role": "reviewer",
         "class": "api_connection_error", "attempt": 5, "max_attempts": 5,
         "diagnostics": "APIConnectionError"},
        {"action": "retried", "task_id": "t_3", "issue": 44,
         "role": "security-analyst", "class": "crash", "attempt": 1,
         "max_attempts": 5, "diagnostics": "died"},
    ]
    tel = disp._summarize_crash_telemetry(actions)
    assert tel["crash_counts_by_role"] == {"reviewer": 2, "security-analyst": 1}
    diags = tel["crash_diagnostics"]
    assert [d["task_id"] for d in diags] == ["t_1", "t_2", "t_3"]
    assert diags[1]["class"] == "api_connection_error"
    assert diags[1]["action"] == "escalated"
    assert diags[0]["diagnostics"] == "boom"


def test_summarize_crash_telemetry_empty_is_empty():
    tel = disp._summarize_crash_telemetry([])
    assert tel == {"crash_counts_by_role": {}, "crash_diagnostics": []}


def test_summarize_crash_telemetry_defaults_missing_role():
    tel = disp._summarize_crash_telemetry([{"action": "retried", "task_id": "t_x"}])
    assert tel["crash_counts_by_role"] == {"unknown": 1}
    assert tel["crash_diagnostics"][0]["class"] == "crash"


# ── AC4: no behaviour change when agents do not crash ────────────────────────


def test_non_crash_block_never_captures_diagnostics(fake, tmp_path):
    fk = fake([_card(summary="review-required: PR #42 — fix/issue-42")], log_tail=LOG_TAIL)
    actions = crash_retry.reconcile("board", str(tmp_path), {}, now=T0)
    assert actions == []
    assert fk.unblocked == []
    assert fk.comments == {}
    # The worker log is NEVER read for a non-crash card.
    assert fk.log_calls == []


def test_dry_run_captures_nothing(fake, tmp_path):
    fk = fake([_card(summary=CRASH)], log_tail=LOG_TAIL)
    actions = crash_retry.reconcile("board", str(tmp_path), {}, now=T0, dry_run=True)
    assert actions == []
    assert fk.comments == {}
    assert fk.log_calls == []


if __name__ == "__main__":  # pragma: no cover — dual-mode standalone runner
    sys.exit(pytest.main([__file__, "-q"]))
