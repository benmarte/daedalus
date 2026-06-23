"""Regression tests for all-role Claude Code delegation (issue #48).

The Daedalus pipeline delegates real work to a coding agent (Claude Code) by
emitting a literal trigger marker at the top of every role task body.  Each
pipeline role's SOUL.md carries a "delegation gate" that tells the local LLM:
*when the task body contains this exact marker, spawn the coding agent and relay
its output instead of doing the work yourself.*

The gate only fires if the string the soul checks for is byte-for-byte identical
to the string the dispatcher emits.  The marker is ``⚠️  AGENT DELEGATION`` —
note the TWO spaces after the warning emoji.  If either side silently drifts to a
single space (or the marker text changes on one side only), the substring check
fails and every role stops delegating without any error — the exact failure mode
issue #48 exists to guard against.

These tests assert that contract holds for all six delegating roles, and they
deliberately DERIVE the expected marker from the dispatcher source rather than
hard-coding it, so the trigger string and its check can never drift apart.

Runs both as a plain script (``python3 tests/test_soul_delegation.py``) and
under pytest.  Assertions are real ``assert`` statements so failures surface
under both runners.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SOULS = _ROOT / "config" / "souls"
_DISPATCHER = _ROOT / "scripts" / "daedalus_dispatch.py"

# The six pipeline roles that produce artifacts and must delegate to the coding
# agent.  ``validator`` (triage-only, never spawns a coding agent) and
# ``accessibility`` (not part of the active delegating pipeline) intentionally
# carry no gate, so they are excluded here.
DELEGATING_ROLES = (
    "developer",
    "documentation",
    "project-manager",
    "qa",
    "reviewer",
    "security-analyst",
)

# Substring from the warning emoji through the word DELEGATION, e.g.
# "⚠️  AGENT DELEGATION".  Whitespace is captured verbatim so a one-space vs
# two-space difference is a mismatch, not a normalisation.
_MARKER_RE = re.compile(r"⚠️[ ]*[A-Z ]*?DELEGATION")


def _soul_path(role: str) -> Path:
    return _SOULS / f"{role}-daedalus.md"


def _dispatcher_marker() -> str:
    """The exact delegation trigger string the dispatcher writes into bodies."""
    src = _DISPATCHER.read_text(encoding="utf-8")
    markers = set(_MARKER_RE.findall(src))
    assert markers, (
        "dispatcher emits no delegation trigger — expected a "
        "'⚠️  AGENT DELEGATION' marker in scripts/daedalus_dispatch.py"
    )
    assert len(markers) == 1, (
        f"dispatcher emits inconsistent delegation markers: {sorted(markers)} — "
        "all role task bodies must use one identical trigger string"
    )
    return markers.pop()


def test_dispatcher_emits_single_consistent_trigger() -> None:
    """The dispatcher emits exactly one delegation marker, with two spaces."""
    marker = _dispatcher_marker()
    # Two spaces after the emoji is the load-bearing detail; assert it explicitly.
    assert marker.startswith("⚠️  "), (
        f"delegation marker {marker!r} must have two spaces after the emoji; "
        "a single space silently breaks every soul's trigger check"
    )
    assert marker.endswith("DELEGATION")


def test_all_delegating_souls_exist() -> None:
    for role in DELEGATING_ROLES:
        assert _soul_path(role).is_file(), f"missing SOUL.md for role {role!r}"


def test_all_delegating_souls_check_for_dispatcher_marker() -> None:
    """Every delegating soul checks for the EXACT string the dispatcher emits.

    This is the core issue-#48 invariant: trigger (emitted) == check (in soul).
    """
    marker = _dispatcher_marker()
    # The check is meaningful only against the two-space marker: every soul also
    # contains a single-space header ("# ⚠️ AGENT DELEGATION — READ FIRST"), so a
    # one-space marker would spuriously match that header. Require two spaces here
    # so this test stays sound even if the dispatcher side drifts in isolation.
    assert marker.startswith("⚠️  "), (
        f"derived trigger {marker!r} is not the two-space marker; the soul check "
        "line must match the exact string the dispatcher emits"
    )
    for role in DELEGATING_ROLES:
        text = _soul_path(role).read_text(encoding="utf-8")
        assert marker in text, (
            f"{role}-daedalus.md does not check for the dispatcher's trigger "
            f"{marker!r}; delegation would never fire for this role"
        )


def test_gate_is_read_first() -> None:
    """The delegation gate must precede the role's other instructions."""
    for role in DELEGATING_ROLES:
        text = _soul_path(role).read_text(encoding="utf-8")
        gate = text.find("AGENT DELEGATION")
        assert gate != -1, f"{role}-daedalus.md has no AGENT DELEGATION gate"
        # The gate header sits within the first few lines (after at most a short
        # persona line), well before bulk sections like "# Communication".
        gate_line = text[:gate].count("\n")
        assert gate_line <= 4, (
            f"{role}-daedalus.md gate appears at line {gate_line + 1}; it must be "
            "READ FIRST, near the very top of the file"
        )
        for section in ("# Communication", "# Code Standards", "# Problem Solving"):
            pos = text.find(section)
            if pos != -1:
                assert pos > gate, (
                    f"{role}-daedalus.md places {section!r} before the delegation "
                    "gate; the gate must come first"
                )


def test_gate_defines_spawn_and_completion_steps() -> None:
    """Each gate spawns the agent and runs the pipeline-advance completion step."""
    for role in DELEGATING_ROLES:
        text = _soul_path(role).read_text(encoding="utf-8")
        assert "terminal(" in text, (
            f"{role}-daedalus.md gate must spawn the coding agent via terminal()"
        )
        assert "daedalus-cron.sh" in text, (
            f"{role}-daedalus.md gate must run daedalus-cron.sh to advance the "
            "pipeline after delegation completes"
        )


_TESTS = (
    test_dispatcher_emits_single_consistent_trigger,
    test_all_delegating_souls_exist,
    test_all_delegating_souls_check_for_dispatcher_marker,
    test_gate_is_read_first,
    test_gate_defines_spawn_and_completion_steps,
)


if __name__ == "__main__":
    print("Soul Delegation tests")
    print("-" * 60)
    failed = 0
    for fn in _TESTS:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {fn.__name__}: {exc}")
    print("-" * 60)
    print(f"Results: {len(_TESTS) - failed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
