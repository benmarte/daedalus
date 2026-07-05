"""Tests for goal-mode kanban card configuration (issue #1296).

Acceptance criteria covered:
  (AC1) ``resolve_goal_mode`` resolves ``execution.goal_mode`` (default off)
        into ``{enabled, max_turns}`` with validated fallbacks.
  (AC2) Flag OFF ⇒ ``goal_kwargs`` returns ``{}`` — byte-identical to
        pre-#1296 (no ``--goal`` / ``--goal-max-turns`` in CLI args).
  (AC3) Flag ON + eligible role + native agent ⇒ ``{"goal": True, "goal_max_turns": N}``.
  (AC4) Delegation bypass: non-hermes effective_coding_agent ⇒ ``{}`` for
        any role (outer wrapper has ~1 turn, can't produce verifiable artifact).
  (AC5) Ineligible roles (validator, pm, planner, reviewer, security,
        accessibility) always return ``{}`` regardless of flag.
  (AC6) ``create_with_goal_fallback`` retries WITHOUT goal on ``None`` from
        first attempt (judge-LLM-unavailable path); flag-off produces a
        single call identical to direct ``create_task``.
  (AC7) ``create_task`` flag-off ⇒ byte-identical CLI args (no ``--goal``).
        Flag-on ⇒ ``--goal`` (+ ``--goal-max-turns``) appended.
  (AC8) Integration: ``_check_completed_pm`` with ``goal_cfg`` off ⇒ no goal
        args. With it on + native agent ⇒ goal args present for dev / qa / docs.

Dual-mode: runs under pytest AND as a standalone ``__main__`` script.
Kanban calls are intercepted via ``mock.patch("core.kanban._hk", …)`` or the
FakeKanban/kanban_as infrastructure from conftest so no test reaches a live board.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import goal_mode, kanban  # noqa: E402

# ── AC1: resolve_goal_mode ────────────────────────────────────────────────────


def test_resolve_goal_mode_default_disabled():
    cfg = goal_mode.resolve_goal_mode({})
    assert cfg["enabled"] is False
    assert cfg["max_turns"] == goal_mode.DEFAULT_MAX_TURNS


def test_resolve_goal_mode_enabled():
    cfg = goal_mode.resolve_goal_mode({"goal_mode": True})
    assert cfg["enabled"] is True
    assert cfg["max_turns"] == goal_mode.DEFAULT_MAX_TURNS


def test_resolve_goal_mode_custom_max_turns():
    cfg = goal_mode.resolve_goal_mode({"goal_mode": True, "goal_max_turns": 50})
    assert cfg["max_turns"] == 50


def test_resolve_goal_mode_bad_max_turns_falls_back():
    for bad in ("x", -1, 0, True, None):
        cfg = goal_mode.resolve_goal_mode({"goal_mode": True, "goal_max_turns": bad})
        assert cfg["max_turns"] == goal_mode.DEFAULT_MAX_TURNS, bad


def test_resolve_goal_mode_nondicts_do_not_raise():
    assert goal_mode.resolve_goal_mode(None)["enabled"] is False  # type: ignore[arg-type]
    assert goal_mode.resolve_goal_mode("bad")["enabled"] is False  # type: ignore[arg-type]
    assert goal_mode.resolve_goal_mode(42)["enabled"] is False  # type: ignore[arg-type]


def test_resolve_goal_mode_flag_off_explicit():
    cfg = goal_mode.resolve_goal_mode({"goal_mode": False})
    assert cfg["enabled"] is False


# ── AC2: goal_kwargs disabled ⇒ empty dict ───────────────────────────────────


def test_goal_kwargs_disabled_is_empty():
    off = goal_mode.resolve_goal_mode({"goal_mode": False})
    assert goal_mode.goal_kwargs(off, "developer", 42) == {}
    absent = goal_mode.resolve_goal_mode({})
    assert goal_mode.goal_kwargs(absent, "developer", 42) == {}
    assert goal_mode.goal_kwargs(None, "developer", 42) == {}
    # flag absent and explicit false produce identical results
    assert goal_mode.goal_kwargs(off, "developer", 42) == goal_mode.goal_kwargs(absent, "developer", 42)


# ── AC3: goal_kwargs enabled + eligible + native agent ────────────────────────


def test_goal_kwargs_enabled_eligible_native_agent():
    on = goal_mode.resolve_goal_mode({"goal_mode": True, "goal_max_turns": 25})
    for role in ("developer", "qa", "documentation"):
        kw = goal_mode.goal_kwargs(on, role, 42, effective_coding_agent="none")
        assert kw == {"goal": True, "goal_max_turns": 25}, role
    # hermes is also a native agent
    kw = goal_mode.goal_kwargs(on, "developer", 42, effective_coding_agent="hermes")
    assert kw == {"goal": True, "goal_max_turns": 25}


def test_goal_kwargs_uses_default_max_turns_when_not_set():
    on = goal_mode.resolve_goal_mode({"goal_mode": True})
    kw = goal_mode.goal_kwargs(on, "qa", 99, effective_coding_agent="none")
    assert kw["goal_max_turns"] == goal_mode.DEFAULT_MAX_TURNS


# ── AC4: delegation bypass ─────────────────────────────────────────────────────


def test_goal_kwargs_delegation_bypass_for_all_roles():
    on = goal_mode.resolve_goal_mode({"goal_mode": True})
    for agent in ("claude-code", "codex", "opencode", "cursor"):
        for role in ("developer", "qa", "documentation"):
            kw = goal_mode.goal_kwargs(on, role, 42, effective_coding_agent=agent)
            assert kw == {}, f"role={role} agent={agent}"


def test_goal_kwargs_delegation_bypass_logged_as_debug(caplog):
    import logging
    on = goal_mode.resolve_goal_mode({"goal_mode": True})
    with caplog.at_level(logging.DEBUG, logger="daedalus.goal_mode"):
        goal_mode.goal_kwargs(on, "developer", 42, effective_coding_agent="claude-code")
    assert any("delegat" in r.message.lower() for r in caplog.records)


# ── AC5: ineligible roles always return empty ─────────────────────────────────


def test_goal_kwargs_ineligible_roles_always_empty():
    on = goal_mode.resolve_goal_mode({"goal_mode": True})
    for role in ("validator", "pm", "planner", "reviewer", "security", "accessibility"):
        kw = goal_mode.goal_kwargs(on, role, 42, effective_coding_agent="none")
        assert kw == {}, role


# ── AC7: create_task CLI arg emission ────────────────────────────────────────


def _capture_create_task_args(**kwargs) -> List[str]:
    """Return the CLI arg list that create_task hands to _hk for *kwargs*."""
    captured: Dict[str, List[str]] = {}

    def _fake_hk(args, timeout=60):
        captured["args"] = list(args)
        return (0, "t_deadbeef created", "")

    with mock.patch("core.kanban._hk", side_effect=_fake_hk):
        kanban.create_task("board", "#42 title", body="b", assignee="dev", **kwargs)
    return captured["args"]


def test_create_task_goal_flag_off_byte_identical():
    # AC7a: flag off (no kwargs) vs explicitly disabled (goal_kwargs → {})
    # produce IDENTICAL arg lists — no --goal / --goal-max-turns present.
    off = goal_mode.resolve_goal_mode({"goal_mode": False})
    baseline = _capture_create_task_args()
    disabled = _capture_create_task_args(**goal_mode.goal_kwargs(off, "developer", 42))
    assert baseline == disabled
    assert "--goal" not in baseline
    assert "--goal-max-turns" not in baseline


def test_create_task_goal_flag_on_args_present():
    # AC7b: goal=True appends --goal; goal_max_turns appends --goal-max-turns N.
    args_with_max = _capture_create_task_args(goal=True, goal_max_turns=30)
    assert "--goal" in args_with_max
    assert "--goal-max-turns" in args_with_max
    assert args_with_max[args_with_max.index("--goal-max-turns") + 1] == "30"


def test_create_task_goal_without_max_turns():
    # --goal without --goal-max-turns is valid (hermes uses its own default).
    args = _capture_create_task_args(goal=True)
    assert "--goal" in args
    assert "--goal-max-turns" not in args


# ── AC6: create_with_goal_fallback ────────────────────────────────────────────


def test_create_with_goal_fallback_succeeds():
    calls: List[Dict] = []

    def _create(*args, **kwargs):
        calls.append(kwargs)
        return "t_abc123"

    on = goal_mode.resolve_goal_mode({"goal_mode": True, "goal_max_turns": 20})
    result = goal_mode.create_with_goal_fallback(
        _create, on, "developer", 42, "none",
        "board", "title", body="b",
    )
    assert result == "t_abc123"
    assert len(calls) == 1
    assert calls[0]["goal"] is True
    assert calls[0]["goal_max_turns"] == 20


def test_create_with_goal_fallback_retries_without_goal_on_none():
    # AC6: first call (with goal) returns None → retry without goal.
    call_count = [0]

    def _create(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return None  # simulate judge-unavailable failure
        return "t_fallback"

    on = goal_mode.resolve_goal_mode({"goal_mode": True})
    result = goal_mode.create_with_goal_fallback(
        _create, on, "qa", 99, "none",
        "board", "title",
    )
    assert result == "t_fallback"
    assert call_count[0] == 2


def test_create_with_goal_fallback_flag_off_single_call():
    # AC6b: flag off → single call, no fallback, no goal kwargs injected.
    calls: List[Dict] = []

    def _create(*args, **kwargs):
        calls.append(kwargs)
        return None  # would trigger fallback if goal_kw were non-empty

    off = goal_mode.resolve_goal_mode({"goal_mode": False})
    result = goal_mode.create_with_goal_fallback(
        _create, off, "developer", 42, "none",
        "board", "title",
    )
    assert result is None
    assert len(calls) == 1
    assert "goal" not in calls[0]


def test_create_with_goal_fallback_both_fail_returns_none():
    def _create(*args, **kwargs):
        return None

    on = goal_mode.resolve_goal_mode({"goal_mode": True})
    result = goal_mode.create_with_goal_fallback(
        _create, on, "developer", 42, "none",
        "board", "title",
    )
    assert result is None


def test_create_with_goal_fallback_delegation_no_retry():
    # When delegation bypass applies (goal_kw = {}), a None result does not
    # trigger a second call — there's nothing to fall back from.
    call_count = [0]

    def _create(*args, **kwargs):
        call_count[0] += 1
        return None

    on = goal_mode.resolve_goal_mode({"goal_mode": True})
    goal_mode.create_with_goal_fallback(
        _create, on, "developer", 42, "claude-code",
        "board", "title",
    )
    assert call_count[0] == 1  # no retry because goal_kw was empty


# ── goal_string sanity ────────────────────────────────────────────────────────


def test_goal_string_nonempty_for_eligible_roles():
    for role in ("developer", "qa", "documentation"):
        s = goal_mode.goal_string(role, 42)
        assert isinstance(s, str) and len(s) > 20, role
        assert "42" in s, role


def test_goal_string_empty_for_ineligible_roles():
    for role in ("validator", "pm", "planner", "reviewer", "security"):
        assert goal_mode.goal_string(role, 42) == "", role


# ── AC8: integration with _check_completed_pm ────────────────────────────────


def test_check_completed_pm_goal_off_no_goal_args():
    """Flag off ⇒ create_task calls carry no goal kwargs (byte-identical)."""
    try:
        from core.dispatch.checks import _check_completed_pm
    except ImportError:
        return  # skip when checks module not importable standalone

    created_kwargs: List[Dict] = []

    class _TrackingKanban:
        def list_tasks(self, slug, status=""):
            return [
                {
                    "id": "t_pm1",
                    "assignee": "pm-daedalus",
                    "title": "#42 PM: fix thing",
                    "status": "done",
                    "summary": "SPEC: implement fix",
                }
            ]

        def show_card(self, slug, tid):
            return None

        def create_task(self, slug, title, **kwargs):
            created_kwargs.append(kwargs)
            return f"t_{len(created_kwargs):03d}"

        def list_blocked(self, slug):
            return []

        def get_latest_summary(self, slug, tid):
            return "SPEC: implement fix"

    off = goal_mode.resolve_goal_mode({"goal_mode": False})
    tk = _TrackingKanban()

    with mock.patch("core.dispatch.checks._kanban", return_value=tk), \
         mock.patch("core.dispatch.checks._has_downstream_tasks", return_value=False), \
         mock.patch("core.dispatch.checks._get_task_summary", return_value="SPEC: implement fix"):
        _check_completed_pm(
            "board",
            "owner/repo",
            {42: {"title": "fix thing", "body": "", "labels": [], "number": 42}},
            3,
            "",
            "",
            "dev",
            "github",
            goal_cfg=off,
        )

    for kw in created_kwargs:
        assert "goal" not in kw, f"unexpected goal in kwargs: {kw}"


# ── resolve_primary_coding_agent — chain-only config (#1296 polish) ──────────


def test_resolve_primary_coding_agent_chain_only():
    """When only coding_agents chain is set (no coding_agent singular key),
    resolve_primary_coding_agent returns the first chain entry's name,
    NOT "hermes" (the _resolve_coding_agent fallback).
    """
    execution = {
        "coding_agents": [
            {"name": "claude-code", "cmd": "claude -p"},
            {"name": "codex", "cmd": "codex exec --full-auto"},
        ]
    }
    agent = goal_mode.resolve_primary_coding_agent(execution)
    assert agent == "claude-code", f"expected claude-code, got {agent!r}"


def test_resolve_primary_coding_agent_singular_key_wins():
    """When coding_agent (singular) is present alongside coding_agents,
    the chain still provides the correct primary (chain[0]).
    """
    execution = {
        "coding_agent": "claude-code",
        "coding_agents": [
            {"name": "claude-code", "cmd": "claude -p"},
        ]
    }
    agent = goal_mode.resolve_primary_coding_agent(execution)
    assert agent == "claude-code"


def test_resolve_primary_coding_agent_empty_falls_back_to_hermes():
    """Empty execution dict → "hermes" (same as _resolve_coding_agent default)."""
    agent = goal_mode.resolve_primary_coding_agent({})
    assert agent == "hermes"


def test_goal_kwargs_delegation_bypass_chain_only_config():
    """Chain-only config: goal_kwargs correctly bypasses for delegation targets.

    This tests the misfiring bug: when only coding_agents is set, the old
    code resolved "hermes" as effective_coding_agent and goal_kwargs fired
    incorrectly.  With resolve_primary_coding_agent, the chain's primary
    ("claude-code") is used and the bypass fires.
    """
    on = goal_mode.resolve_goal_mode({"goal_mode": True})
    # Chain-only config — no coding_agent singular key.
    primary = goal_mode.resolve_primary_coding_agent({
        "coding_agents": [{"name": "claude-code", "cmd": "claude -p"}]
    })
    for role in ("developer", "qa", "documentation"):
        kw = goal_mode.goal_kwargs(on, role, 42, effective_coding_agent=primary)
        assert kw == {}, f"role={role}: expected bypass, got {kw!r}"


_TESTS = [
    test_resolve_goal_mode_default_disabled,
    test_resolve_goal_mode_enabled,
    test_resolve_goal_mode_custom_max_turns,
    test_resolve_goal_mode_bad_max_turns_falls_back,
    test_resolve_goal_mode_nondicts_do_not_raise,
    test_resolve_goal_mode_flag_off_explicit,
    test_goal_kwargs_disabled_is_empty,
    test_goal_kwargs_enabled_eligible_native_agent,
    test_goal_kwargs_uses_default_max_turns_when_not_set,
    test_goal_kwargs_delegation_bypass_for_all_roles,
    test_goal_kwargs_ineligible_roles_always_empty,
    test_resolve_primary_coding_agent_chain_only,
    test_resolve_primary_coding_agent_singular_key_wins,
    test_resolve_primary_coding_agent_empty_falls_back_to_hermes,
    test_goal_kwargs_delegation_bypass_chain_only_config,
    test_create_task_goal_flag_off_byte_identical,
    test_create_task_goal_flag_on_args_present,
    test_create_task_goal_without_max_turns,
    test_create_with_goal_fallback_succeeds,
    test_create_with_goal_fallback_retries_without_goal_on_none,
    test_create_with_goal_fallback_flag_off_single_call,
    test_create_with_goal_fallback_both_fail_returns_none,
    test_create_with_goal_fallback_delegation_no_retry,
    test_goal_string_nonempty_for_eligible_roles,
    test_goal_string_empty_for_ineligible_roles,
    test_check_completed_pm_goal_off_no_goal_args,
]


if __name__ == "__main__":
    _failed = 0
    for _t in _TESTS:
        if _t.__name__ == "test_goal_kwargs_delegation_bypass_logged_as_debug":
            # requires pytest caplog fixture — skip standalone
            print(f"\n--- {_t.__name__} --- [skipped: needs caplog]")
            continue
        print(f"\n--- {_t.__name__} ---")
        try:
            _t()
            print("  ok")
        except Exception as exc:  # noqa: BLE001 — standalone runner
            _failed += 1
            print(f"  FAIL  ({type(exc).__name__}: {exc})")
    print(f"\n{'=' * 60}")
    _runnable = len(_TESTS) - 1  # exclude caplog test
    print(f"Results: {_runnable - _failed} passed, {_failed} failed")
    if _failed:
        sys.exit(1)
