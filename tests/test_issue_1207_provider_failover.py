"""Tests for the configurable provider fallback chain (issue #1207).

Covers the spec's acceptance criteria:
  (1) execution.coding_agents / model.providers chains parsed + validated;
      legacy single-value keys keep working as one-element chains,
  (2) coding-agent session-limit crash → the SAME card is re-dispatched on the
      next configured coding agent (apply callback + card comment),
  (3) brain APIConnectionError → next brain provider applied via callback,
  (4) max_attempts_per_provider honored per provider; whole chain exhausted →
      hard-block + escalation carrying per-provider history,
  (5) a capped provider cools down globally and is skipped by other cards until
      the window expires; primary is preferred again on recovery,
  (6) every failover posts a card comment + is logged (no silent switches),
  (7) deterministic failures (no crash marker) never rotate providers; one
      re-dispatch per card per tick,
plus dispatch_state cooldown bookkeeping and the kanban body read/write
helpers used by the delegation-block rewrite.

Timelines are simulated with NON-ALIGNED ticks through the full escalation
bound (PR #1211 lesson): every reconcile pass runs at an off-boundary time and
in-backoff/in-cooldown ticks are asserted to produce no action — no pre-seeded
counters.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import crash_retry, dispatch_state, provider_failover  # noqa: E402

T0 = 1_750_000_000.0  # deterministic "now"
MIN = 60.0

AGENT_DEFAULTS = {
    "claude-code": "claude -p",
    "codex": "codex exec --full-auto",
    "opencode": "opencode run",
}

SESSION_LIMIT = "coding-agent-failed: You've hit your session limit · resets 6:30pm"
API_CONN = "APIConnectionError: ollama.com unreachable"


# ── chain resolution (AC 1) ───────────────────────────────────────────────────


def test_coding_agent_chain_from_list():
    execution = {
        "coding_agents": [
            {"name": "claude-code", "cmd": "CLAUDE_CONFIG_DIR=$HOME/.c claude -p"},
            {"name": "codex"},
        ]
    }
    chain = provider_failover.resolve_coding_agent_chain(execution, AGENT_DEFAULTS)
    assert chain == [
        {"name": "claude-code", "cmd": "CLAUDE_CONFIG_DIR=$HOME/.c claude -p"},
        {"name": "codex", "cmd": "codex exec --full-auto"},  # cmd defaulted
    ]


def test_coding_agent_chain_drops_invalid_and_duplicate_entries():
    execution = {
        "coding_agents": [
            {"name": "not-a-real-agent"},
            "just-a-string",
            {"name": "codex", "cmd": "codex exec --full-auto"},
            {"name": "codex", "cmd": "different"},
        ]
    }
    chain = provider_failover.resolve_coding_agent_chain(execution, AGENT_DEFAULTS)
    assert chain == [{"name": "codex", "cmd": "codex exec --full-auto"}]


def test_coding_agent_chain_legacy_single_value_backcompat():
    execution = {"coding_agent": "claude-code", "coding_agent_cmd": "claude -p -x"}
    chain = provider_failover.resolve_coding_agent_chain(execution, AGENT_DEFAULTS)
    assert chain == [{"name": "claude-code", "cmd": "claude -p -x"}]
    # nothing configured at all → hermes one-element chain (dispatcher default)
    assert provider_failover.resolve_coding_agent_chain({}, AGENT_DEFAULTS) == [
        {"name": "hermes", "cmd": ""}
    ]


def test_model_provider_chain_from_list_and_fallback():
    model_cfg = {
        "providers": [
            {"provider": "ollama-cloud", "default": "glm-5.2"},
            {"provider": "anthropic", "default": "claude-opus-4-8"},
            {"provider": "ollama-cloud", "default": "dupe"},
            {"default": "no-provider"},
        ]
    }
    chain = provider_failover.resolve_model_provider_chain(model_cfg)
    assert chain == [
        {"provider": "ollama-cloud", "default": "glm-5.2"},
        {"provider": "anthropic", "default": "claude-opus-4-8"},
    ]
    # legacy fallback: one-element chain from the active global model config
    active = {"model": "glm-5.2", "provider": "ollama-cloud"}
    assert provider_failover.resolve_model_provider_chain({}, active) == [
        {"provider": "ollama-cloud", "default": "glm-5.2"}
    ]
    assert provider_failover.resolve_model_provider_chain({}, {}) == []


def test_failover_config_defaults_and_precedence():
    cfg = provider_failover.resolve_failover_config({})
    assert cfg["max_attempts_per_provider"] == 2
    assert cfg["cooldown_minutes"] == 30
    assert cfg["reset_to_primary"] is True
    assert set(cfg["triggers"]) == set(provider_failover.TRIGGER_CLASSES)
    # model.failover applies; execution.failover wins per-key
    cfg = provider_failover.resolve_failover_config(
        {"failover": {"max_attempts_per_provider": 3}},
        {"failover": {"max_attempts_per_provider": 9, "cooldown_minutes": 10}},
    )
    assert cfg["max_attempts_per_provider"] == 3
    assert cfg["cooldown_minutes"] == 10
    # invalid values fall back; unknown triggers filtered out
    cfg = provider_failover.resolve_failover_config(
        {
            "failover": {
                "max_attempts_per_provider": "bogus",
                "cooldown_minutes": -5,
                "triggers": ["session_limit", "not-a-trigger"],
            }
        }
    )
    assert cfg["max_attempts_per_provider"] == 2
    assert cfg["cooldown_minutes"] == 30
    assert cfg["triggers"] == ["session_limit"]


def test_validate_failover():
    ok = {
        "execution": {"coding_agents": [{"name": "codex"}]},
        "model": {"providers": [{"provider": "anthropic"}]},
    }
    assert provider_failover.validate_failover(ok) == []
    assert provider_failover.validate_failover({}) == []  # all-absent is valid
    bad = {
        "execution": {
            "coding_agents": "codex",
            "failover": {"triggers": ["bogus"]},
        },
        "model": {"providers": [{"default": "x"}]},
    }
    errors = provider_failover.validate_failover(bad)
    assert len(errors) == 3
    assert any("coding_agents must be a list" in e for e in errors)
    assert any("model.providers entry" in e for e in errors)
    assert any("unknown trigger" in e for e in errors)


# ── layer attribution + selection ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "evidence,layer",
    [
        (SESSION_LIMIT, "coding_agent"),
        ("CODING_AGENT_DIED: agent exited", "coding_agent"),
        ("coding_agent_timeout: exceeded 3600s", "coding_agent"),
        ("429 rate limit from provider", "coding_agent"),
        (API_CONN, "brain"),
        ("worker gave up (crash breaker)", "brain"),
    ],
)
def test_layer_for_evidence(evidence, layer):
    assert provider_failover.layer_for_evidence(evidence) == layer


CHAIN = [{"name": "a"}, {"name": "b"}, {"name": "c"}]
FCFG = {"max_attempts_per_provider": 2, "reset_to_primary": True}


def test_select_provider_prefers_primary_when_eligible():
    sel = provider_failover.select_provider(CHAIN, {}, set(), FCFG, current_index=1)
    assert (sel["action"], sel["index"]) == ("use", 0)


def test_select_provider_skips_capped_and_cooling():
    sel = provider_failover.select_provider(
        CHAIN, {"a": 2}, {"b"}, FCFG, current_index=0
    )
    assert (sel["action"], sel["index"]) == ("use", 2)


def test_select_provider_wait_when_all_candidates_cooling():
    sel = provider_failover.select_provider(CHAIN, {"a": 2}, {"b", "c"}, FCFG)
    assert sel["action"] == "wait"


def test_select_provider_exhausted_when_every_provider_capped():
    sel = provider_failover.select_provider(CHAIN, {"a": 2, "b": 2, "c": 3}, set(), FCFG)
    assert sel["action"] == "exhausted"


def test_select_provider_sticky_without_reset_to_primary():
    cfg = {"max_attempts_per_provider": 2, "reset_to_primary": False}
    sel = provider_failover.select_provider(CHAIN, {}, set(), cfg, current_index=1)
    assert (sel["action"], sel["index"]) == ("use", 1)
    # current capped → next in chain order, wrapping when needed
    sel = provider_failover.select_provider(
        CHAIN, {"c": 2}, set(), cfg, current_index=2
    )
    assert (sel["action"], sel["index"]) == ("use", 0)


# ── dispatch_state cooldown bookkeeping ───────────────────────────────────────


def test_provider_cooldown_roundtrip(tmp_path):
    wd = str(tmp_path)
    assert dispatch_state.get_provider_cooldowns(wd) == {}
    dispatch_state.set_provider_cooldown(wd, "coding_agent:claude-code", T0 + 100)
    assert dispatch_state.get_provider_cooldowns(wd) == {
        "coding_agent:claude-code": T0 + 100
    }
    dispatch_state.clear_provider_cooldown(wd, "coding_agent:claude-code")
    assert dispatch_state.get_provider_cooldowns(wd) == {}
    assert dispatch_state.get_brain_active_index(wd) == 0
    dispatch_state.set_brain_active_index(wd, 1)
    assert dispatch_state.get_brain_active_index(wd) == 1


# ── kanban body helpers (delegation-block rewrite substrate) ──────────────────


def test_kanban_body_roundtrip(tmp_path, monkeypatch):
    import sqlite3

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from core import kanban

    db_dir = tmp_path / "kanban" / "boards" / "proj"
    db_dir.mkdir(parents=True)
    conn = sqlite3.connect(db_dir / "kanban.db")
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, body TEXT)")
    conn.execute("INSERT INTO tasks VALUES ('t_1', 'old body')")
    conn.commit()
    conn.close()
    assert kanban.get_body("proj", "t_1") == "old body"
    assert kanban.edit_body("proj", "t_1", "new body") is True
    assert kanban.get_body("proj", "t_1") == "new body"
    assert kanban.edit_body("proj", "t_missing", "x") is False
    assert kanban.get_body("no-board", "t_1") is None


# ── reconcile failover integration ────────────────────────────────────────────


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

    # test helper: simulate the re-dispatched worker crashing again
    def crash(self, tid: str, summary: str) -> None:
        self.cards[tid]["status"] = "blocked"
        self.cards[tid]["summary"] = summary


@pytest.fixture()
def fake(monkeypatch):
    def _make(cards: List[Dict[str, Any]]) -> FakeKanban:
        fk = FakeKanban(cards)
        monkeypatch.setattr(crash_retry, "kanban", fk)
        return fk

    return _make


def _card(tid="t_1", status="blocked", summary=SESSION_LIMIT, title="#42 Developer: fix thing"):
    return {
        "id": tid,
        "status": status,
        "summary": summary,
        "title": title,
        "assignee": "developer-daedalus",
    }


def _ctx(applied: List[tuple], cap=2, cooldown_min=30, brain=None, coding=None):
    """Failover context with recording apply callbacks."""

    def _apply(layer):
        def fn(card, entry):
            applied.append((layer, provider_failover.entry_name(entry)))
            return True

        return fn

    return {
        "cfg": {
            "max_attempts_per_provider": cap,
            "cooldown_minutes": cooldown_min,
            "reset_to_primary": True,
            "triggers": list(provider_failover.TRIGGER_CLASSES),
        },
        "chains": {
            "coding_agent": coding
            if coding is not None
            else [
                {"name": "claude-code", "cmd": "claude -p"},
                {"name": "codex", "cmd": "codex exec --full-auto"},
            ],
            "brain": brain
            if brain is not None
            else [
                {"provider": "ollama-cloud", "default": "glm-5.2"},
                {"provider": "anthropic", "default": "claude-opus-4-8"},
            ],
        },
        "apply": {"coding_agent": _apply("coding_agent"), "brain": _apply("brain")},
        "current": {"coding_agent": "claude-code", "brain": "ollama-cloud"},
    }


def test_session_limit_fails_over_to_next_coding_agent_full_timeline(
    fake, tmp_path
):
    """AC 2/4/5/6 end-to-end: non-aligned ticks through the whole escalation
    bound — retry on primary, cap → cooldown + failover to codex, cap again →
    chain exhausted → hard-block with per-provider history."""
    wd = str(tmp_path)
    fk = fake([_card()])
    applied: List[tuple] = []
    ctx = _ctx(applied)
    execution: Dict[str, Any] = {}

    # tick 1 (t+1min, non-aligned): first retry stays on the primary
    acts = crash_retry.reconcile("p", wd, execution, now=T0 + 1 * MIN, failover=ctx)
    assert [a["action"] for a in acts] == ["retried"]
    assert acts[0]["provider"] == "claude-code"
    assert applied == []  # no switch yet → apply not called
    assert len(fk.unblocked) == 1

    # worker crashes again on the primary
    fk.crash("t_1", SESSION_LIMIT)

    # tick 2 (t+3min): inside the 15-min backoff → nothing happens
    assert crash_retry.reconcile("p", wd, execution, now=T0 + 3 * MIN, failover=ctx) == []
    assert len(fk.unblocked) == 1

    # tick 3 (t+17min): primary spent its 2 attempts → cooldown + failover
    acts = crash_retry.reconcile("p", wd, execution, now=T0 + 17 * MIN, failover=ctx)
    assert [a["action"] for a in acts] == ["retried"]
    assert acts[0]["provider"] == "codex"
    assert applied == [("coding_agent", "codex")]
    assert any("failover" in c and "claude-code" in c and "codex" in c
               for c in fk.comments["t_1"])
    cooldowns = dispatch_state.get_provider_cooldowns(wd)
    assert cooldowns["coding_agent:claude-code"] == pytest.approx(
        T0 + 17 * MIN + 30 * MIN
    )

    # crash on codex; tick 4 inside 30-min backoff → no action
    fk.crash("t_1", SESSION_LIMIT)
    assert (
        crash_retry.reconcile("p", wd, execution, now=T0 + 29 * MIN, failover=ctx)
        == []
    )

    # tick 5 (t+49min): codex retried once more (primary capped for the episode
    # even though its cooldown expired at t+47min — per-episode bound holds)
    acts = crash_retry.reconcile("p", wd, execution, now=T0 + 49 * MIN, failover=ctx)
    assert acts[0]["provider"] == "codex"
    assert applied == [("coding_agent", "codex")]  # still just one switch

    # crash again; tick 6 (t+111min, past the 60-min backoff step): every
    # provider capped → chain exhausted → escalate with per-provider history
    fk.crash("t_1", SESSION_LIMIT)
    acts = crash_retry.reconcile("p", wd, execution, now=T0 + 111 * MIN, failover=ctx)
    assert [a["action"] for a in acts] == ["escalated"]
    assert "claude-code: 2 attempt(s)" in acts[0]["provider_history"]
    assert "codex: 2 attempt(s)" in acts[0]["provider_history"]
    assert "claude-code → codex" in acts[0]["provider_history"]
    assert fk.cards["t_1"]["summary"].startswith(crash_retry.EXHAUSTED_PREFIX)
    diag = "\n".join(fk.comments["t_1"])
    assert "Per-provider history" in diag

    # escalation is terminal until a human unblocks
    assert (
        crash_retry.reconcile("p", wd, execution, now=T0 + 200 * MIN, failover=ctx)
        == []
    )
    assert len(fk.unblocked) == 3  # exactly one dispatch per allowed retry


def test_brain_api_connection_error_fails_over_to_next_provider(fake, tmp_path):
    """AC 3: brain-layer trigger applies the next model provider via callback."""
    wd = str(tmp_path)
    fk = fake([_card(summary=API_CONN, title="#7 Validator: check thing")])
    applied: List[tuple] = []
    ctx = _ctx(applied, cap=1)  # fail over on the first retry

    acts = crash_retry.reconcile("p", wd, {}, now=T0 + 1 * MIN, failover=ctx)
    assert [a["action"] for a in acts] == ["retried"]
    assert acts[0]["provider"] == "anthropic"
    assert applied == [("brain", "anthropic")]
    assert dispatch_state.get_provider_cooldowns(wd).get("brain:ollama-cloud")
    assert any("ollama-cloud" in c and "anthropic" in c for c in fk.comments["t_1"])
    entry = dispatch_state.get_crash_retry(wd, "t_1")
    assert entry["provider"]["layer"] == "brain"
    assert entry["provider"]["name"] == "anthropic"
    assert entry["class"] == "api_connection_error"


def test_cooling_provider_is_skipped_by_new_episodes(fake, tmp_path):
    """AC 5: a globally cooling primary is skipped by a fresh card; after the
    window expires the primary is preferred again (reset_to_primary)."""
    wd = str(tmp_path)
    dispatch_state.set_provider_cooldown(
        wd, "coding_agent:claude-code", T0 + 30 * MIN
    )
    fk = fake([_card(tid="t_9")])
    applied: List[tuple] = []
    ctx = _ctx(applied)

    acts = crash_retry.reconcile("p", wd, {}, now=T0 + 1 * MIN, failover=ctx)
    assert acts[0]["provider"] == "codex"
    assert applied == [("coding_agent", "codex")]

    # card recovers; cooldown expires; a NEW episode prefers the primary again
    fk.cards["t_9"]["status"] = "done"
    crash_retry.reconcile("p", wd, {}, now=T0 + 40 * MIN, failover=ctx)  # cleanup
    fk.crash("t_9", SESSION_LIMIT)
    acts = crash_retry.reconcile("p", wd, {}, now=T0 + 61 * MIN, failover=ctx)
    assert acts[0]["provider"] == "claude-code"
    assert applied == [("coding_agent", "codex")]  # no new switch needed


def test_all_candidates_cooling_waits_instead_of_dispatching(fake, tmp_path):
    wd = str(tmp_path)
    dispatch_state.set_provider_cooldown(wd, "coding_agent:codex", T0 + 60 * MIN)
    dispatch_state.set_provider_cooldown(
        wd, "coding_agent:claude-code", T0 + 60 * MIN
    )
    fk = fake([_card()])
    acts = crash_retry.reconcile("p", wd, {}, now=T0 + 1 * MIN, failover=_ctx([]))
    assert acts == []
    assert fk.unblocked == []
    # cooldowns expired → dispatch resumes
    acts = crash_retry.reconcile("p", wd, {}, now=T0 + 61 * MIN, failover=_ctx([]))
    assert [a["action"] for a in acts] == ["retried"]


def test_apply_failure_defers_the_switch_to_the_next_tick(fake, tmp_path):
    wd = str(tmp_path)
    fk = fake([_card()])
    calls: List[str] = []

    ctx = _ctx([], cap=1)

    def failing_apply(card, entry):
        calls.append(provider_failover.entry_name(entry))
        return len(calls) > 1  # first attempt fails, second succeeds

    ctx["apply"]["coding_agent"] = failing_apply
    assert crash_retry.reconcile("p", wd, {}, now=T0 + 1 * MIN, failover=ctx) == []
    assert fk.unblocked == []  # switch not applied → card stays blocked
    acts = crash_retry.reconcile("p", wd, {}, now=T0 + 2 * MIN, failover=ctx)
    assert [a["action"] for a in acts] == ["retried"]
    assert acts[0]["provider"] == "codex"
    assert calls == ["codex", "codex"]


def test_single_element_chain_reproduces_1205_behavior(fake, tmp_path):
    """Back-compat: a one-element chain (legacy config) never rotates and the
    flat #1205 caps govern escalation."""
    wd = str(tmp_path)
    fake([_card()])
    applied: List[tuple] = []
    ctx = _ctx(applied, coding=[{"name": "claude-code", "cmd": "claude -p"}])
    acts = crash_retry.reconcile("p", wd, {}, now=T0 + 1 * MIN, failover=ctx)
    assert [a["action"] for a in acts] == ["retried"]
    assert acts[0]["provider"] == ""  # no chain in play
    assert applied == []
    assert acts[0]["max_attempts"] == 5  # crash_retry default, not cap×chain


def test_disabled_trigger_never_rotates(fake, tmp_path):
    wd = str(tmp_path)
    fake([_card()])  # session-limit evidence
    applied: List[tuple] = []
    ctx = _ctx(applied)
    ctx["cfg"]["triggers"] = ["api_connection_error"]
    acts = crash_retry.reconcile("p", wd, {}, now=T0 + 1 * MIN, failover=ctx)
    assert [a["action"] for a in acts] == ["retried"]  # plain #1205 retry
    assert acts[0]["provider"] == ""
    assert applied == []


def test_non_crash_block_untouched_even_with_failover(fake, tmp_path):
    fk = fake([_card(summary="review-required: PR #12 — fix/issue-42")])
    acts = crash_retry.reconcile(
        "p", str(tmp_path), {}, now=T0 + 1 * MIN, failover=_ctx([])
    )
    assert acts == []
    assert fk.unblocked == []


def test_concurrent_tick_never_double_dispatches(fake, tmp_path):
    """AC 7: the attempt is persisted state-first, so a second reconcile at the
    same instant sees it spent and stays in backoff."""
    wd = str(tmp_path)
    fk = fake([_card()])
    ctx = _ctx([])
    crash_retry.reconcile("p", wd, {}, now=T0 + 1 * MIN, failover=ctx)
    fk.crash("t_1", SESSION_LIMIT)
    assert crash_retry.reconcile("p", wd, {}, now=T0 + 1 * MIN, failover=ctx) == []
    assert len(fk.unblocked) == 1


# ── dispatcher wiring (delegation rewrite, context, brain reset) ──────────────

from conftest import _load_dispatch  # noqa: E402

disp = _load_dispatch()

ROLE_BODY = (
    "You are the DEVELOPER for issue org/repo#42: fix thing\n"
    "Work in the existing git repo.\n"
)


def _delegated_body(agent="claude-code", cmd="claude -p"):
    return disp._prepend_delegation(
        ROLE_BODY, agent, cmd, role="developer", issue_number=42, base_branch="dev"
    )


def test_rewrite_delegation_block_swaps_agents():
    body = _delegated_body()
    assert "CLAUDE CODE" in body
    new_block = disp._build_delegation_instructions(
        "codex", "codex exec --full-auto", role="developer",
        issue_number=42, base_branch="dev",
    )
    out = disp._rewrite_delegation_block(body, new_block)
    assert out is not None
    assert "CODEX" in out and "CLAUDE CODE" not in out
    assert "claude -p" not in out and "codex exec --full-auto" in out
    assert out.count("You are the DEVELOPER") == 1
    # /tmp file scoping is preserved (role prefix + issue number)
    assert "/tmp/dev-42-task.txt" in out


def test_rewrite_delegation_block_inserts_and_strips():
    # no existing block → new block is prepended
    out = disp._rewrite_delegation_block(ROLE_BODY, "\n⚠️  AGENT DELEGATION — USE X:")
    assert out.startswith("\n⚠️  AGENT DELEGATION — USE X:")
    assert out.endswith(ROLE_BODY)
    # empty block (fallback to hermes) strips delegation entirely
    out = disp._rewrite_delegation_block(_delegated_body(), "")
    assert out == ROLE_BODY
    # unrecognized body shape → refuse
    assert disp._rewrite_delegation_block("no role marker here", "x") is None


def test_apply_coding_agent_failover_rewrites_card_body(monkeypatch):
    store = {"body": _delegated_body()}
    monkeypatch.setattr(disp.kanban, "get_body", lambda s, t: store["body"])

    def _edit(s, t, b):
        store["body"] = b
        return True

    monkeypatch.setattr(disp.kanban, "edit_body", _edit)
    monkeypatch.setattr(
        disp, "_resolve_active_model_provider",
        lambda: {"model": None, "provider": None},
    )
    card = {"id": "t_1", "title": "#42 Developer: fix thing",
            "assignee": "developer-daedalus"}
    ok = disp._apply_coding_agent_failover(
        "p", card, {"name": "codex", "cmd": "codex exec --full-auto"}, {}, "dev"
    )
    assert ok is True
    assert "CODEX" in store["body"] and "CLAUDE CODE" not in store["body"]


def test_build_failover_context_and_brain_apply(monkeypatch, tmp_path):
    wd = str(tmp_path)
    resync_calls = []
    monkeypatch.setattr(
        disp, "_resync_profiles_to_model",
        lambda workdir, model, provider, old: resync_calls.append(
            (model, provider, dict(old or {}))
        ) or 3,
    )
    monkeypatch.setattr(
        disp, "_resolve_active_model_provider",
        lambda: {"model": "glm-5.2", "provider": "ollama-cloud"},
    )
    execution = {
        "coding_agents": [
            {"name": "claude-code", "cmd": "claude -p"},
            {"name": "codex"},
        ],
    }
    resolved = {
        "model": {
            "providers": [
                {"provider": "ollama-cloud", "default": "glm-5.2"},
                {"provider": "anthropic", "default": "claude-opus-4-8"},
            ],
            "failover": {"max_attempts_per_provider": 1},
        },
        "vcs": {"target_branch": "dev"},
    }
    ctx = disp._build_failover_context("p", resolved, execution, wd)
    assert [e["name"] for e in ctx["chains"]["coding_agent"]] == [
        "claude-code", "codex",
    ]
    assert ctx["chains"]["coding_agent"][1]["cmd"]  # cmd defaulted
    assert ctx["cfg"]["max_attempts_per_provider"] == 1
    assert ctx["current"] == {"coding_agent": "claude-code", "brain": "ollama-cloud"}

    # brain apply resyncs profiles to the fallback and records the index;
    # old_values reflect the previously ACTIVE entry's model
    fallback = ctx["chains"]["brain"][1]
    assert ctx["apply"]["brain"]({}, fallback) is True
    assert resync_calls == [
        ("claude-opus-4-8", "anthropic", {"model_default": "glm-5.2", "coding_agent": ""})
    ]
    assert dispatch_state.get_brain_active_index(wd) == 1
    # a context built AFTER the switch reports the fallback as current
    ctx2 = disp._build_failover_context("p", resolved, execution, wd)
    assert ctx2["current"]["brain"] == "anthropic"


def test_maybe_reset_brain_to_primary(monkeypatch, tmp_path):
    wd = str(tmp_path)
    resync_calls = []
    monkeypatch.setattr(
        disp, "_resync_profiles_to_model",
        lambda workdir, model, provider, old: resync_calls.append(provider) or 1,
    )
    monkeypatch.setattr(
        disp, "_resolve_active_model_provider",
        lambda: {"model": "glm-5.2", "provider": "ollama-cloud"},
    )
    resolved = {
        "model": {
            "providers": [
                {"provider": "ollama-cloud", "default": "glm-5.2"},
                {"provider": "anthropic", "default": "claude-opus-4-8"},
            ]
        }
    }
    ctx = disp._build_failover_context("p", resolved, {}, wd)
    dispatch_state.set_brain_active_index(wd, 1)

    # primary still cooling → stay on the fallback
    import time as _time

    dispatch_state.set_provider_cooldown(
        wd, "brain:ollama-cloud", _time.time() + 600
    )
    disp._maybe_reset_brain_to_primary(wd, ctx, dry_run=False)
    assert resync_calls == []
    assert dispatch_state.get_brain_active_index(wd) == 1

    # cooldown expired → resync back to the primary + clear the cooldown
    dispatch_state.set_provider_cooldown(
        wd, "brain:ollama-cloud", _time.time() - 1
    )
    disp._maybe_reset_brain_to_primary(wd, ctx, dry_run=False)
    assert resync_calls == ["ollama-cloud"]
    assert dispatch_state.get_brain_active_index(wd) == 0
    assert dispatch_state.get_provider_cooldowns(wd) == {}


def test_integration_limit_then_fallback_success(fake, monkeypatch, tmp_path):
    """AC 2 integration: session-limit crash → delegation block rewritten to
    codex via the real dispatcher context → card re-dispatched → completes."""
    wd = str(tmp_path)
    store = {"body": _delegated_body()}
    monkeypatch.setattr(disp.kanban, "get_body", lambda s, t: store["body"])

    def _edit(s, t, b):
        store["body"] = b
        return True

    monkeypatch.setattr(disp.kanban, "edit_body", _edit)
    monkeypatch.setattr(
        disp, "_resolve_active_model_provider",
        lambda: {"model": None, "provider": None},
    )
    execution = {
        "coding_agents": [
            {"name": "claude-code", "cmd": "claude -p"},
            {"name": "codex"},
        ],
        "failover": {"max_attempts_per_provider": 1},
    }
    ctx = disp._build_failover_context(
        "p", {"vcs": {"target_branch": "dev"}}, execution, wd
    )
    fk = fake([_card()])

    acts = crash_retry.reconcile("p", wd, execution, now=T0 + 1 * MIN, failover=ctx)
    assert [a["action"] for a in acts] == ["retried"]
    assert acts[0]["provider"] == "codex"
    assert "CODEX" in store["body"] and "CLAUDE CODE" not in store["body"]
    assert fk.cards["t_1"]["status"] == "ready"
    assert any("failover" in c.lower() for c in fk.comments["t_1"])

    # fallback succeeds → card completes → episode + bookkeeping cleared
    fk.cards["t_1"]["status"] = "done"
    crash_retry.reconcile("p", wd, execution, now=T0 + 5 * MIN, failover=ctx)
    assert dispatch_state.get_crash_retry(wd, "t_1") is None
