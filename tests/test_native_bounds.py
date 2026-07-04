"""Tests for native per-task retry/runtime bounds (issue #1289).

Covers the spec's acceptance criteria:
  (AC1) ``create_task`` accepts ``max_runtime`` → appends ``--max-runtime``.
  (AC2) ``resolve_bounds`` resolves ``execution.native_bounds`` (default off)
        into per-role ``{max_retries, max_runtime}`` with validated fallbacks.
  (AC3) Flag OFF ⇒ byte-identical CLI args (no ``--max-retries`` /
        ``--max-runtime`` — flag absent and explicit ``false`` produce the
        same arg list).
  (AC4) Flag ON ⇒ dispatcher-created cards carry both args per role policy.
  (AC5) ``--max-retries`` default is explicitly 2 (= kanban.failure_limit),
        distinct from ``MAX_FIX_ATTEMPTS = 3``.
  (AC6) crash_retry de-dup: with native bounds ON the reconciler skips
        ``timeout``-class cards (native ``--max-runtime`` requeue owns them);
        other crash classes (session_limit, …) are unaffected.

Dual-mode per repo convention: runs under pytest AND as a standalone
``__main__`` script. Every kanban touch is either an explicit
``mock.patch("core.kanban._hk", …)`` or a patched ``crash_retry.kanban``, so no
test can ever reach a real board (also stubbed board-wide by conftest under
pytest, #1209).
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import crash_retry, kanban, native_bounds  # noqa: E402

T0 = 1_750_000_000.0  # deterministic "now"


# ── AC2 / AC5: resolve_bounds ────────────────────────────────────────────────


def test_resolve_bounds_default_disabled():
    b = native_bounds.resolve_bounds({})
    assert b["enabled"] is False
    # A fully-populated map is still returned so call sites never KeyError.
    assert set(b["by_role"]) >= {
        "validator",
        "developer",
        "qa",
        "reviewer",
        "security",
        "documentation",
    }


def test_resolve_bounds_missing_execution_is_disabled():
    # Non-dict / None execution must not raise and must be disabled.
    assert native_bounds.resolve_bounds(None)["enabled"] is False  # type: ignore[arg-type]
    assert native_bounds.resolve_bounds("nope")["enabled"] is False  # type: ignore[arg-type]


def test_resolve_bounds_max_retries_default_is_2():
    # AC5: explicitly 2 (= kanban.failure_limit), distinct from MAX_FIX_ATTEMPTS=3.
    b = native_bounds.resolve_bounds({"native_bounds": True})
    assert native_bounds.DEFAULT_MAX_RETRIES == 2
    for role, rb in b["by_role"].items():
        assert rb["max_retries"] == 2, role


def test_resolve_bounds_developer_gets_generous_runtime():
    b = native_bounds.resolve_bounds({"native_bounds": True})
    assert b["by_role"]["developer"]["max_runtime"] == "1h"
    assert b["by_role"]["validator"]["max_runtime"] == native_bounds.DEFAULT_MAX_RUNTIME


def test_resolve_bounds_overrides_and_validation():
    b = native_bounds.resolve_bounds(
        {
            "native_bounds": True,
            "native_max_retries": 4,
            "native_max_runtime": "45m",
            "native_bounds_by_role": {
                "qa": {"max_retries": 1, "max_runtime": "10m"},
                # bad values fall back to the resolved globals
                "reviewer": {"max_retries": 0, "max_runtime": "  "},
            },
        }
    )
    assert b["enabled"] is True
    assert b["default_max_retries"] == 4
    assert b["default_max_runtime"] == "45m"
    assert b["by_role"]["qa"] == {"max_retries": 1, "max_runtime": "10m"}
    # invalid override → global defaults
    assert b["by_role"]["reviewer"] == {"max_retries": 4, "max_runtime": "45m"}


def test_resolve_bounds_bad_globals_fall_back():
    b = native_bounds.resolve_bounds(
        {
            "native_bounds": True,
            "native_max_retries": "abc",  # non-int
            "native_max_runtime": "",  # empty
            "native_bounds_by_role": "not-a-dict",  # ignored
        }
    )
    assert b["default_max_retries"] == native_bounds.DEFAULT_MAX_RETRIES
    assert b["default_max_runtime"] == native_bounds.DEFAULT_MAX_RUNTIME


def test_resolve_bounds_rejects_bool_and_nonpositive_retries():
    b = native_bounds.resolve_bounds(
        {"native_bounds": True, "native_max_retries": True}
    )
    assert b["default_max_retries"] == native_bounds.DEFAULT_MAX_RETRIES
    b2 = native_bounds.resolve_bounds(
        {"native_bounds": True, "native_max_retries": -3}
    )
    assert b2["default_max_retries"] == native_bounds.DEFAULT_MAX_RETRIES


# ── AC3: bounds_kwargs off ⇒ empty (byte-identical) ──────────────────────────


def test_bounds_kwargs_disabled_is_empty():
    assert native_bounds.bounds_kwargs(None, "developer") == {}
    off = native_bounds.resolve_bounds({"native_bounds": False})
    assert native_bounds.bounds_kwargs(off, "developer") == {}
    # flag absent resolves identically to explicit false
    absent = native_bounds.resolve_bounds({})
    assert native_bounds.bounds_kwargs(absent, "developer") == {}
    assert native_bounds.bounds_kwargs(off, "developer") == native_bounds.bounds_kwargs(
        absent, "developer"
    )


def test_bounds_kwargs_enabled_per_role():
    on = native_bounds.resolve_bounds({"native_bounds": True})
    assert native_bounds.bounds_kwargs(on, "developer") == {
        "max_retries": 2,
        "max_runtime": "1h",
    }
    assert native_bounds.bounds_kwargs(on, "validator") == {
        "max_retries": 2,
        "max_runtime": "30m",
    }
    # unknown role → global defaults, never a KeyError
    assert native_bounds.bounds_kwargs(on, "mystery") == {
        "max_retries": 2,
        "max_runtime": "30m",
    }


# ── AC1 / AC3 / AC4: create_task arg emission ────────────────────────────────


def _capture_create_task_args(**kwargs) -> List[str]:
    """Return the CLI arg list create_task hands to _hk for *kwargs*."""
    captured: Dict[str, List[str]] = {}

    def _fake_hk(args, timeout=60):
        captured["args"] = list(args)
        return (0, "t_deadbeef created", "")

    with mock.patch("core.kanban._hk", side_effect=_fake_hk):
        kanban.create_task("board", "#42 title", body="b", assignee="dev", **kwargs)
    return captured["args"]


def test_create_task_flag_off_byte_identical():
    # AC3: flag absent (no kwargs) vs explicitly disabled (bounds_kwargs → {})
    # produce IDENTICAL arg lists, and neither carries the native flags.
    off = native_bounds.resolve_bounds({"native_bounds": False})
    baseline = _capture_create_task_args()
    disabled = _capture_create_task_args(**native_bounds.bounds_kwargs(off, "developer"))
    assert baseline == disabled
    assert "--max-retries" not in baseline
    assert "--max-runtime" not in baseline


def test_create_task_max_runtime_emitted():
    # AC1: passing max_runtime appends --max-runtime <value>.
    args = _capture_create_task_args(max_runtime="30m")
    assert "--max-runtime" in args
    assert args[args.index("--max-runtime") + 1] == "30m"
    # and it is omitted when None
    assert "--max-runtime" not in _capture_create_task_args(max_runtime=None)


def test_create_task_flag_on_args_present_per_role():
    # AC4: flag on ⇒ both bounds present with the configured per-role values.
    on = native_bounds.resolve_bounds({"native_bounds": True})
    dev = _capture_create_task_args(**native_bounds.bounds_kwargs(on, "developer"))
    assert dev[dev.index("--max-retries") + 1] == "2"
    assert dev[dev.index("--max-runtime") + 1] == "1h"

    val = _capture_create_task_args(**native_bounds.bounds_kwargs(on, "validator"))
    assert val[val.index("--max-runtime") + 1] == "30m"


# ── AC6: crash_retry timeout-class de-dup ────────────────────────────────────


class _FakeKanban:
    """Minimal in-memory stand-in for the kanban helpers reconcile uses."""

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
        if c is None:
            return None
        return {**c, "comments": [{"body": b} for b in self.comments.get(tid, [])]}


def _card(tid: str, summary: str) -> Dict[str, Any]:
    return {
        "id": tid,
        "status": "blocked",
        "summary": summary,
        "title": "developer: #42 fix thing",
        "assignee": "developer-daedalus",
    }


def _reconcile(cards: List[Dict[str, Any]], *, native: bool):
    fk = _FakeKanban(cards)
    with tempfile.TemporaryDirectory() as wd, mock.patch.object(
        crash_retry, "kanban", fk
    ):
        actions = crash_retry.reconcile(
            "board", wd, {}, now=T0, native_bounds=native
        )
    return fk, actions


TIMEOUT_EVIDENCE = "coding-agent-failed: CODING_AGENT_TIMEOUT — exceeded 3600s"
SESSION_EVIDENCE = "claude code session limit reached, not yet reset"


def test_crash_retry_skips_timeout_when_native_bounds_on():
    # AC6: native bounds ON ⇒ a timeout-class card is left to native requeue.
    assert crash_retry.classify(TIMEOUT_EVIDENCE) == "timeout"
    fk, actions = _reconcile([_card("t_to", TIMEOUT_EVIDENCE)], native=True)
    assert actions == []
    assert fk.unblocked == []


def test_crash_retry_handles_timeout_when_native_bounds_off():
    # Flag off ⇒ crash_retry owns the timeout card as before (byte-identical).
    fk, actions = _reconcile([_card("t_to", TIMEOUT_EVIDENCE)], native=False)
    assert [a["action"] for a in actions] == ["retried"]
    assert fk.unblocked and fk.unblocked[0][0] == "t_to"


def test_crash_retry_non_timeout_unaffected_by_native_bounds():
    # AC6: session_limit (and other non-timeout classes) still flow through
    # crash_retry regardless of the native-bounds flag.
    assert crash_retry.classify(SESSION_EVIDENCE) == "session_limit"
    for native in (True, False):
        fk, actions = _reconcile([_card("t_sl", SESSION_EVIDENCE)], native=native)
        assert [a["action"] for a in actions] == ["retried"], native
        assert fk.unblocked and fk.unblocked[0][0] == "t_sl"


_TESTS = [
    test_resolve_bounds_default_disabled,
    test_resolve_bounds_missing_execution_is_disabled,
    test_resolve_bounds_max_retries_default_is_2,
    test_resolve_bounds_developer_gets_generous_runtime,
    test_resolve_bounds_overrides_and_validation,
    test_resolve_bounds_bad_globals_fall_back,
    test_resolve_bounds_rejects_bool_and_nonpositive_retries,
    test_bounds_kwargs_disabled_is_empty,
    test_bounds_kwargs_enabled_per_role,
    test_create_task_flag_off_byte_identical,
    test_create_task_max_runtime_emitted,
    test_create_task_flag_on_args_present_per_role,
    test_crash_retry_skips_timeout_when_native_bounds_on,
    test_crash_retry_handles_timeout_when_native_bounds_off,
    test_crash_retry_non_timeout_unaffected_by_native_bounds,
]


if __name__ == "__main__":
    _failed = 0
    for _t in _TESTS:
        print(f"\n--- {_t.__name__} ---")
        try:
            _t()
            print("  ok")
        except Exception as exc:  # noqa: BLE001 — standalone runner
            _failed += 1
            print(f"  FAIL  ({type(exc).__name__}: {exc})")
    print(f"\n{'=' * 60}")
    print(f"Results: {len(_TESTS) - _failed} passed, {_failed} failed")
    if _failed:
        sys.exit(1)
