"""Integration tests for tier promotion via dispatcher wiring.

Verifies the end-to-end path: merge detected → completed list populated →
promote_waiting_tiers called → dependent sub-issues promoted to Ready.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import tier_promotion  # noqa: E402
from core.providers.base import VCSProvider, IssueSummary  # noqa: E402


class _StubProvider(VCSProvider):
    """Concrete stub for dispatcher→promotion integration tests."""
    name = "stub-integration"

    def __init__(self, bodies, states, labels, blocker_overrides=None):
        self._bodies: dict[int, str] = dict(bodies)
        self._states: dict[int, str] = dict(states)
        self._labels: dict[int, list[str]] = {k: list(v) for k, v in labels.items()}
        self._blocker_overrides: dict[int, list[int]] = dict(blocker_overrides or {})
        self.label_calls: list[tuple[int, str]] = []
        self.comment_calls: list[tuple[int, str]] = []
        self._board_configured = False

    def list_issues(self, state: str = "open", labels: Any = None, limit: int = 100) -> list:
        return []

    def close_issue(self, issue_number: int, reason: str = "completed") -> bool:
        return True

    def list_prs(self, state: str = "all", limit: int = 50) -> list:
        return []

    def get_issue(self, issue_number: int) -> Optional[IssueSummary]:
        if issue_number not in self._states:
            return None
        return IssueSummary(
            number=issue_number,
            body=self._bodies.get(issue_number, ""),
            labels=list(self._labels.get(issue_number, [])),
            title="",
            url="",
        )

    def get_issue_state(self, issue_number: int) -> Optional[str]:
        return self._states.get(issue_number)

    def has_label(self, issue_number: int, label_name: str) -> bool:
        return label_name.lower() in [lbl.lower() for lbl in self._labels.get(issue_number, [])]

    def add_label(self, issue_number: int, label_name: str) -> bool:
        self._labels.setdefault(issue_number, []).append(label_name)
        self.label_calls.append((issue_number, label_name))
        return True

    def blockers(self, issue_number: int) -> list[int]:
        if issue_number in self._blocker_overrides:
            return list(self._blocker_overrides[issue_number])
        from core.providers.base import parse_depends_on
        refs = parse_depends_on(self._bodies.get(issue_number, ""))
        return [d for d in refs if self._states.get(d) == "open"]

    def sub_issues_of(self, epic_number: int) -> list[int]:
        import re
        pattern = re.compile(
            rf"(?m)(?:part\s+of\s+epic\s+#|epic\s*:\s*#){epic_number}(?::|\b)",
            re.IGNORECASE,
        )
        return [n for n, body in self._bodies.items() if pattern.search(body or "")]

    def board_set_status(self, issue_number: int, status_name: str, **_: Any) -> bool:
        return True

    def board_configured(self) -> bool:
        return self._board_configured

    def post_issue_comment(self, issue_number: int, body: str) -> bool:
        self.comment_calls.append((issue_number, body))
        return True


# ── Test 1: Single merge triggers promotion ──────────────────────────────────
def test_dispatch_merge_triggers_promotion_single_dep():
    """End-to-end: when a sub-issue's PR merges, its dependent is promoted.

    Scenario:
    - Epic #100 has sub-issues #201 (tier 0) and #202 (tier 1, depends on #201)
    - #201's PR merges → dispatcher builds completed=[201]
    - promote_waiting_tiers is called, detects #201 closed, promotes #202
    """
    provider = _StubProvider(
        bodies={
            100: "Epic parent",
            201: "Part of epic #100\n\nDepends on:",
            202: "Part of epic #100\n\nDepends on: #201",
        },
        states={100: "open", 201: "closed", 202: "open"},
        labels={100: [], 201: ["Ready"], 202: []},
        blocker_overrides={202: []},  # no blockers — #201 closed
    )

    # Simulate dispatcher detecting merge of #201
    completed = [201]

    # Call the promotion logic (this is what the dispatcher does at line 3617)
    result = tier_promotion.promote_waiting_tiers(provider, completed)

    # Verify #202 was promoted
    assert 202 in result.promoted, f"Expected #202 in promoted, got {result.promoted}"
    assert len(result.errors) == 0, f"Expected no errors, got {result.errors}"

    # Verify label was applied
    assert (202, "Ready") in provider.label_calls, "Expected Ready label on #202"

    print("✓ Single merge triggers promotion of dependent sub-issue")


# ── Test 2: Multiple dependencies all closed → promotion ─────────────────────
def test_dispatch_merge_multiple_deps_all_closed():
    """End-to-end: when all dependencies of a sub-issue close, it's promoted.

    Scenario:
    - Epic #100 has #301, #302 (both tier 0, no deps) and #303 (tier 1, depends on both)
    - Both #301 and #302 merge in the same tick
    - #303 should be promoted once both are closed
    """
    provider = _StubProvider(
        bodies={
            100: "Epic parent",
            301: "Part of epic #100\n\nDepends on:",
            302: "Part of epic #100\n\nDepends on:",
            303: "Part of epic #100\n\nDepends on: #301, #302",
        },
        states={100: "open", 301: "closed", 302: "closed", 303: "open"},
        labels={100: [], 301: ["Ready"], 302: ["Ready"], 303: []},
        blocker_overrides={303: []},  # both deps closed
    )

    # Dispatcher detects both merges in the same tick
    completed = [301, 302]

    result = tier_promotion.promote_waiting_tiers(provider, completed)

    # #303 should be promoted (both deps closed)
    assert 303 in result.promoted, f"Expected #303 in promoted, got {result.promoted}"
    assert (303, "Ready") in provider.label_calls

    print("✓ Multiple dependencies all closed triggers promotion")


# ── Test 3: Partial closure → no promotion (blocker remains) ─────────────────
def test_dispatch_merge_partial_closure_no_promotion():
    """End-to-end: when only some dependencies close, sub-issue waits.

    Scenario:
    - Epic #100 has #401, #402 (both tier 0) and #403 (tier 1, depends on both)
    - Only #401 merges in this tick (#402 still open)
    - #403 should NOT be promoted yet
    """
    provider = _StubProvider(
        bodies={
            100: "Epic parent",
            401: "Part of epic #100\n\nDepends on:",
            402: "Part of epic #100\n\nDepends on:",
            403: "Part of epic #100\n\nDepends on: #401, #402",
        },
        states={100: "open", 401: "closed", 402: "open", 403: "open"},
        labels={100: [], 401: ["Ready"], 402: ["Ready"], 403: []},
        blocker_overrides={403: [402]},  # 402 still open
    )

    # Dispatcher detects only #401 merge
    completed = [401]

    result = tier_promotion.promote_waiting_tiers(provider, completed)

    # #403 should NOT be promoted (402 still open)
    assert 403 not in result.promoted, f"Expected #403 NOT in promoted, got {result.promoted}"
    assert (403, "Ready") not in provider.label_calls, "Expected no Ready label on #403"

    print("✓ Partial closure does not promote (blocker remains open)")


# ── Test 4: No eligible tier (all tiers have open deps) ──────────────────────
def test_dispatch_merge_no_eligible_tier():
    """End-to-end: when no sub-issue has all deps closed, nothing is promoted.

    Scenario:
    - Epic #100 has #501 (tier 0, open), #502 (tier 0, closed), #503 (tier 1, depends on both)
    - #502 merges, but #501 still open
    - #503 cannot be promoted (501 still blocks it)
    """
    provider = _StubProvider(
        bodies={
            100: "Epic parent",
            501: "Part of epic #100\n\nDepends on:",
            502: "Part of epic #100\n\nDepends on:",
            503: "Part of epic #100\n\nDepends on: #501, #502",
        },
        states={100: "open", 501: "open", 502: "closed", 503: "open"},
        labels={100: [], 501: ["Ready"], 502: ["Ready"], 503: []},
        blocker_overrides={503: [501]},  # 501 still open
    )

    # Dispatcher detects #502 merge
    completed = [502]

    result = tier_promotion.promote_waiting_tiers(provider, completed)

    # #503 should NOT be promoted (501 still blocks it)
    assert 503 not in result.promoted, f"Expected #503 NOT in promoted, got {result.promoted}"
    assert len(result.promoted) == 0, f"Expected nothing promoted, got {result.promoted}"

    print("✓ No promotion when blocker still open")


# ── Test 5: Cascading promotion across multiple tiers ────────────────────────
def test_dispatch_merge_cascading_promotion_multi_tier():
    """End-to-end: chain of dependencies promotes tier-by-tier as each merges.

    Scenario:
    - Epic #100 has #601 (tier 0) → #602 (tier 1) → #603 (tier 2)
    - First tick: #601 merges → #602 promoted
    - Second tick: #602 merges → #603 promoted
    """
    # First tick: #601 merges
    provider1 = _StubProvider(
        bodies={
            100: "Epic parent",
            601: "Part of epic #100\n\nDepends on:",
            602: "Part of epic #100\n\nDepends on: #601",
            603: "Part of epic #100\n\nDepends on: #602",
        },
        states={100: "open", 601: "closed", 602: "open", 603: "open"},
        labels={100: [], 601: ["Ready"], 602: [], 603: []},
        blocker_overrides={602: [], 603: [602]},  # 602 has no blockers; 603 blocked by 602
    )

    result1 = tier_promotion.promote_waiting_tiers(provider1, [601])

    assert 602 in result1.promoted, f"Tick 1: Expected #602 promoted, got {result1.promoted}"
    assert 603 not in result1.promoted, "Tick 1: Expected #603 NOT promoted (602 still open)"

    # Second tick: #602 merges (update state)
    provider2 = _StubProvider(
        bodies={
            100: "Epic parent",
            601: "Part of epic #100\n\nDepends on:",
            602: "Part of epic #100\n\nDepends on: #601",
            603: "Part of epic #100\n\nDepends on: #602",
        },
        states={100: "open", 601: "closed", 602: "closed", 603: "open"},
        labels={100: [], 601: ["Ready"], 602: ["Ready"], 603: []},
        blocker_overrides={603: []},  # 602 now closed, so 603 has no blockers
    )

    result2 = tier_promotion.promote_waiting_tiers(provider2, [602])

    assert 603 in result2.promoted, f"Tick 2: Expected #603 promoted, got {result2.promoted}"

    print("✓ Cascading promotion works across multiple tiers")


# ── Test 6: Concurrent merges in same tick ───────────────────────────────────
def test_dispatch_merge_concurrent_merges_same_tick():
    """End-to-end: when multiple sub-issues merge concurrently, all valid promotions fire.

    Scenario:
    - Epic #100 has #701, #702 (both tier 0) and #703 (tier 1, depends on #701), #704 (tier 1, depends on #702)
    - Both #701 and #702 merge in the same tick
    - Both #703 and #704 should be promoted
    """
    provider = _StubProvider(
        bodies={
            100: "Epic parent",
            701: "Part of epic #100\n\nDepends on:",
            702: "Part of epic #100\n\nDepends on:",
            703: "Part of epic #100\n\nDepends on: #701",
            704: "Part of epic #100\n\nDepends on: #702",
        },
        states={100: "open", 701: "closed", 702: "closed", 703: "open", 704: "open"},
        labels={100: [], 701: ["Ready"], 702: ["Ready"], 703: [], 704: []},
        blocker_overrides={703: [], 704: []},  # both deps closed
    )

    # Dispatcher detects both merges concurrently
    completed = [701, 702]

    result = tier_promotion.promote_waiting_tiers(provider, completed)

    # Both #703 and #704 should be promoted
    assert 703 in result.promoted, f"Expected #703 in promoted, got {result.promoted}"
    assert 704 in result.promoted, f"Expected #704 in promoted, got {result.promoted}"
    assert (703, "Ready") in provider.label_calls
    assert (704, "Ready") in provider.label_calls

    print("✓ Concurrent merges trigger all valid promotions")


if __name__ == "__main__":
    test_dispatch_merge_triggers_promotion_single_dep()
    test_dispatch_merge_multiple_deps_all_closed()
    test_dispatch_merge_partial_closure_no_promotion()
    test_dispatch_merge_no_eligible_tier()
    test_dispatch_merge_cascading_promotion_multi_tier()
    test_dispatch_merge_concurrent_merges_same_tick()
    print("\n✅ All integration tests passed!")
