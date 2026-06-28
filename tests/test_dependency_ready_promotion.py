"""Comprehensive tests for dependency ordering and ready promotion.

Covers four scenarios as specified in issue #139:
(a) Creating N sub-issues results in only dependency-free sub-issues entering Ready state
(b) Merging a leaf sub-issue auto-promotes next-tier sub-issues to Ready
(c) Correct promotion through multi-tier dependency chains
(d) Circular dependency detection and warning behavior
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List, Optional
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.iterate import _sub_issue_body  # noqa: E402
from core.providers.base import IssueSummary, VCSProvider, parse_depends_on  # noqa: E402
from core import tier_promotion  # noqa: E402


# ── Stub Provider ─────────────────────────────────────────────────────────────

class _StubProvider(VCSProvider):
    """Minimal concrete provider for unit testing dependency ordering."""
    name = "stub-dep-order"

    def __init__(self):
        self._bodies: Dict[int, str] = {}
        self._labels: Dict[int, List[str]] = {}
        self._states: Dict[int, Optional[str]] = {}
        self._blockers: Dict[int, Optional[List[int]]] = {}
        self.label_calls: List[tuple] = []
        self.comment_calls: List[tuple] = []

    def list_issues(self, state="open", labels=None, limit=50):
        return []

    def close_issue(self, issue_number):
        self._states[issue_number] = "closed"
        return True

    def list_prs(self, state="all", limit=50):
        return []

    def get_issue(self, issue_number):
        if issue_number not in self._states:
            return None
        labels = self._labels.get(issue_number, [])
        return IssueSummary(
            number=issue_number,
            body=self._bodies.get(issue_number, ""),
            labels=labels,
        )

    def get_issue_state(self, issue_number):
        return self._states.get(issue_number)

    def has_label(self, issue_number: int, label_name: str) -> bool:
        return label_name.lower() in [lbl.lower() for lbl in self._labels.get(issue_number, [])]

    def add_label(self, issue_number: int, label_name: str) -> bool:
        self._labels.setdefault(issue_number, []).append(label_name)
        self.label_calls.append((issue_number, label_name))
        return True

    def post_issue_comment(self, issue_number, body):
        self.comment_calls.append((issue_number, body))
        return True

    def blockers(self, issue_number: int) -> List[int]:
        if issue_number in self._blockers and self._blockers[issue_number] is not None:
            out = self._blockers[issue_number]
            return list(out) if out else []
        body = self._bodies.get(issue_number, "")
        refs = parse_depends_on(body)
        return [n for n in refs if self._states.get(n) == "open"]

    def sub_issues_of(self, epic_number: int) -> List[int]:
        import re
        pattern = re.compile(
            rf"(?m)(?:part\s+of\s+epic\s+#|epic\s*:\s*#){epic_number}(?::|\b)",
            re.IGNORECASE
        )
        results: List[int] = []
        for issue_number, body in self._bodies.items():
            if pattern.search(body):
                state = self._states.get(issue_number)
                if state in ("open", "closed"):
                    results.append(issue_number)
        return results

    def set_issue(self, issue_number: int, body: str, state: str = "open",
                  labels: Optional[List[str]] = None):
        self._bodies[issue_number] = body
        self._states[issue_number] = state
        if labels is not None:
            self._labels[issue_number] = list(labels)

    def set_blockers(self, issue_number: int, blockers: Optional[List[int]]):
        self._blockers[issue_number] = blockers


# ═══════════════════════════════════════════════════════════════════════════════
# Scenario (a): Creating N sub-issues results in only dependency-free
# sub-issues entering Ready state
# ═══════════════════════════════════════════════════════════════════════════════

def test_creation_only_dependency_free_sub_issues_get_ready_label():
    """When creating N sub-issues with sequential dependencies, only the first
    (dependency-free) sub-issue receives the Ready label at creation time."""
    parent_n = 100
    parent_title = "Test Epic"
    
    # Simulate creating 4 sub-issues sequentially as in iterate.py:1253-1277
    created_numbers: List[int] = []
    ready_numbers: List[int] = []
    created_count = 0
    
    # Sub-issue 1: no dependencies (tier 0)
    scope1 = "First task"
    body1 = _sub_issue_body(parent_n, parent_title, scope1, list(created_numbers))
    created_count += 1
    sub_n_1 = 101
    created_numbers.append(sub_n_1)
    
    # Check if this sub-issue has any dependencies
    dependencies1 = parse_depends_on(body1)
    if not dependencies1:
        ready_numbers.append(sub_n_1)
    
    # Sub-issue 2: depends on #101 (tier 1)
    scope2 = "Second task"
    body2 = _sub_issue_body(parent_n, parent_title, scope2, list(created_numbers))
    created_count += 1
    sub_n_2 = 102
    created_numbers.append(sub_n_2)
    
    dependencies2 = parse_depends_on(body2)
    if not dependencies2:
        ready_numbers.append(sub_n_2)
    
    # Sub-issue 3: depends on #101, #102 (tier 2)
    scope3 = "Third task"
    body3 = _sub_issue_body(parent_n, parent_title, scope3, list(created_numbers))
    created_count += 1
    sub_n_3 = 103
    created_numbers.append(sub_n_3)
    
    dependencies3 = parse_depends_on(body3)
    if not dependencies3:
        ready_numbers.append(sub_n_3)
    
    # Sub-issue 4: depends on #101, #102, #103 (tier 3)
    scope4 = "Fourth task"
    body4 = _sub_issue_body(parent_n, parent_title, scope4, list(created_numbers))
    created_count += 1
    sub_n_4 = 104
    created_numbers.append(sub_n_4)
    
    dependencies4 = parse_depends_on(body4)
    if not dependencies4:
        ready_numbers.append(sub_n_4)
    
    # Verify: created 4 sub-issues
    assert created_count == 4
    assert len(created_numbers) == 4
    
    # Verify: only the first sub-issue (no dependencies) is Ready
    assert ready_numbers == [101], f"Expected only #101 to be Ready, got {ready_numbers}"
    
    # Verify: dependency structure is correct
    assert dependencies1 == []
    assert dependencies2 == [101]
    assert dependencies3 == [101, 102]
    assert dependencies4 == [101, 102, 103]


def test_creation_sequential_dependencies_prevent_ready_label():
    """Subsequent sub-issues with dependencies do NOT receive the Ready label."""
    parent_n = 200
    parent_title = "Sequential Tasks"
    
    created_numbers: List[int] = []
    ready_numbers: List[int] = []
    
    # Create 3 sub-issues sequentially (each depends on previous)
    for i in range(3):
        scope = f"Task {i+1}"
        body = _sub_issue_body(parent_n, parent_title, scope, list(created_numbers))
        sub_n = 201 + i
        created_numbers.append(sub_n)
        
        dependencies = parse_depends_on(body)
        if not dependencies:
            ready_numbers.append(sub_n)
    
    # Only the first sub-issue (no dependencies) should be Ready
    assert len(created_numbers) == 3
    assert ready_numbers == [201], f"Expected only #201 to be Ready, got {ready_numbers}"
    
    # Verify dependency structure built up correctly
    for i in range(1, 3):
        sub_n = 201 + i
        # Reconstruct the body to verify dependencies
        body = _sub_issue_body(parent_n, parent_title, f"Task {i+1}", created_numbers[:i])
        deps = parse_depends_on(body)
        assert deps == created_numbers[:i]


def test_creation_no_dependencies_parsed_correctly():
    """Verify parse_depends_on returns empty list for dependency-free bodies."""
    body = _sub_issue_body(100, "Epic", "Leaf task", depends_on=[])
    deps = parse_depends_on(body)
    assert deps == []
    
    # The body should contain the depends_on line with empty value
    lines = body.splitlines()
    dep_lines = [l for l in lines if l.startswith("depends_on:")]
    assert len(dep_lines) == 1
    after_colon = dep_lines[0].split(":", 1)[1].strip()
    assert after_colon == ""


def test_creation_dependency_free_issue_gets_ready_in_iteration():
    """Test the actual logic from iterate.py lines 1266-1275."""
    # Simulate the logic
    sub_body = _sub_issue_body(100, "Epic", "First task", depends_on=[])
    dependencies = parse_depends_on(sub_body)
    
    should_get_ready = not dependencies
    assert should_get_ready, "Dependency-free sub-issue should get Ready label"
    
    # Now with dependencies
    sub_body_with_deps = _sub_issue_body(100, "Epic", "Second task", depends_on=[101])
    dependencies_with_deps = parse_depends_on(sub_body_with_deps)
    
    should_get_ready_2 = not dependencies_with_deps
    assert not should_get_ready_2, "Sub-issue with dependencies should NOT get Ready label"


# ═══════════════════════════════════════════════════════════════════════════════
# Scenario (b): Merging a leaf sub-issue auto-promotes next-tier sub-issues to Ready
# ═══════════════════════════════════════════════════════════════════════════════

def test_merge_leaf_promotes_immediate_dependents():
    """When a tier-0 sub-issue merges (closes), its tier-1 dependents are promoted."""
    provider = _StubProvider()
    provider.set_issue(100, "Epic parent")
    provider.set_issue(101, "Epic: #100")  # tier 0, just merged
    provider.set_issue(102, "Epic: #100\nDepends on: #101")  # tier 1
    provider.set_issue(103, "Epic: #100\nDepends on: #101")  # tier 1
    
    provider._states[101] = "closed"  # just merged
    provider.set_blockers(102, [])  # 101 closed → no blockers
    provider.set_blockers(103, [])  # 101 closed → no blockers
    
    result = tier_promotion.promote_waiting_tiers(provider, just_closed=[101])
    
    assert 102 in result.promoted
    assert 103 in result.promoted
    assert (102, "Ready") in provider.label_calls
    assert (103, "Ready") in provider.label_calls
    assert result.errors == []


def test_merge_leaf_with_multiple_dependents_all_promoted():
    """When a leaf merges and multiple sub-issues depend on it, all are promoted."""
    provider = _StubProvider()
    provider.set_issue(100, "Epic")
    provider.set_issue(101, "Epic: #100")  # tier 0
    for n in [102, 103, 104, 105]:
        provider.set_issue(n, f"Epic: #100\nDepends on: #101")
    
    provider._states[101] = "closed"
    for n in [102, 103, 104, 105]:
        provider.set_blockers(n, [])
    
    result = tier_promotion.promote_waiting_tiers(provider, just_closed=[101])
    
    assert set(result.promoted) == {102, 103, 104, 105}
    for n in [102, 103, 104, 105]:
        assert (n, "Ready") in provider.label_calls


def test_partial_merge_no_promotion_when_blocker_remains():
    """When only some dependencies close, the dependent sub-issue waits."""
    provider = _StubProvider()
    provider.set_issue(100, "Epic")
    provider.set_issue(101, "Epic: #100")  # tier 0, merged
    provider.set_issue(102, "Epic: #100")  # tier 0, still open
    provider.set_issue(103, "Epic: #100\nDepends on: #101, #102")  # tier 1
    
    provider._states[101] = "closed"
    provider._states[102] = "open"  # still open
    provider.set_blockers(103, [102])  # 102 blocks 103
    
    result = tier_promotion.promote_waiting_tiers(provider, just_closed=[101])
    
    # 103 not promoted because 102 still blocks it
    assert 103 not in result.promoted
    assert (103, "Ready") not in provider.label_calls


def test_all_dependencies_closed_promotes():
    """When all dependencies of a sub-issue close, it's promoted."""
    provider = _StubProvider()
    provider.set_issue(100, "Epic")
    provider.set_issue(101, "Epic: #100")  # tier 0
    provider.set_issue(102, "Epic: #100")  # tier 0
    provider.set_issue(103, "Epic: #100\nDepends on: #101, #102")  # tier 1
    
    provider._states[101] = "closed"
    provider._states[102] = "closed"
    provider.set_blockers(103, [])  # both deps closed
    
    result = tier_promotion.promote_waiting_tiers(provider, just_closed=[101, 102])
    
    assert 103 in result.promoted
    assert (103, "Ready") in provider.label_calls


# ═══════════════════════════════════════════════════════════════════════════════
# Scenario (c): Correct promotion through multi-tier dependency chains
# ═══════════════════════════════════════════════════════════════════════════════

def test_multi_tier_chain_promotion():
    """Tier-by-tier promotion through a chain: tier 0 → 1 → 2 → 3."""
    provider = _StubProvider()
    provider.set_issue(100, "Epic")
    provider.set_issue(101, "Epic: #100")  # tier 0
    provider.set_issue(102, "Epic: #100\nDepends on: #101")  # tier 1
    provider.set_issue(103, "Epic: #100\nDepends on: #102")  # tier 2
    provider.set_issue(104, "Epic: #100\nDepends on: #103")  # tier 3
    
    # Tick 1: tier 0 merges → tier 1 promoted
    provider._states[101] = "closed"
    provider.set_blockers(102, [])
    provider.set_blockers(103, [102])
    provider.set_blockers(104, [103])
    
    result1 = tier_promotion.promote_waiting_tiers(provider, just_closed=[101])
    assert 102 in result1.promoted
    assert 103 not in result1.promoted
    assert 104 not in result1.promoted
    
    # Tick 2: tier 1 merges → tier 2 promoted
    provider._states[102] = "closed"
    provider.set_blockers(103, [])
    
    result2 = tier_promotion.promote_waiting_tiers(provider, just_closed=[102])
    assert 103 in result2.promoted
    assert 104 not in result2.promoted
    
    # Tick 3: tier 2 merges → tier 3 promoted
    provider._states[103] = "closed"
    provider.set_blockers(104, [])
    
    result3 = tier_promotion.promote_waiting_tiers(provider, just_closed=[103])
    assert 104 in result3.promoted


def test_multi_tier_fan_out_promotion():
    """Diamond dependency: tier 0 → two tier 1s → tier 2 depends on both."""
    provider = _StubProvider()
    provider.set_issue(100, "Epic")
    provider.set_issue(101, "Epic: #100")  # tier 0
    provider.set_issue(102, "Epic: #100\nDepends on: #101")  # tier 1
    provider.set_issue(103, "Epic: #100\nDepends on: #101")  # tier 1
    provider.set_issue(104, "Epic: #100\nDepends on: #102, #103")  # tier 2
    
    # Tick 1: tier 0 merges → both tier 1s promoted
    provider._states[101] = "closed"
    provider.set_blockers(102, [])
    provider.set_blockers(103, [])
    provider.set_blockers(104, [102, 103])  # both tier 1s still open
    
    result1 = tier_promotion.promote_waiting_tiers(provider, just_closed=[101])
    assert 102 in result1.promoted
    assert 103 in result1.promoted
    assert 104 not in result1.promoted
    
    # Tick 2: only one tier 1 merges → tier 2 still blocked
    provider._states[102] = "closed"
    provider.set_blockers(104, [103])  # 103 still open
    
    result2 = tier_promotion.promote_waiting_tiers(provider, just_closed=[102])
    assert 104 not in result2.promoted  # still blocked by 103
    
    # Tick 3: second tier 1 merges → tier 2 promoted
    provider._states[103] = "closed"
    provider.set_blockers(104, [])
    
    result3 = tier_promotion.promote_waiting_tiers(provider, just_closed=[103])
    assert 104 in result3.promoted


def test_long_chain_correct_tier_assignment():
    """Verify tier computation for a long chain: 1→2→3→4→5."""
    dep_map = {1: [], 2: [1], 3: [2], 4: [3], 5: [4]}
    tiers = tier_promotion.compute_tiers(dep_map)
    
    assert tiers == {1: 0, 2: 1, 3: 2, 4: 3, 5: 4}


def test_sequential_ordering_one_tier_per_tick():
    """When two PRs merge between ticks, only the lowest tier is promoted per tick."""
    provider = _StubProvider()
    provider.set_issue(100, "Epic")
    provider.set_issue(101, "Epic: #100")  # tier 0
    provider.set_issue(102, "Epic: #100\nDepends on: #101")  # tier 1
    provider.set_issue(103, "Epic: #100\nDepends on: #101")  # tier 1
    provider.set_issue(104, "Epic: #100\nDepends on: #102")  # tier 2
    
    # Two PRs merged between ticks: #101 (tier 0) and #103 (tier 1)
    provider._states[101] = "closed"
    provider._states[103] = "closed"
    provider.set_blockers(102, [])  # 101 closed
    provider.set_blockers(104, [])  # 102 closed (but 102 still open)
    
    result = tier_promotion.promote_waiting_tiers(provider, just_closed=[101, 103])
    
    # Only tier 1 (#102) should be promoted this tick, not tier 2 (#104)
    assert 102 in result.promoted
    assert 104 not in result.promoted


# ═══════════════════════════════════════════════════════════════════════════════
# Scenario (d): Circular dependency detection and warning behavior
# ═══════════════════════════════════════════════════════════════════════════════

def test_simple_cycle_detected():
    """Two-node cycle: A→B→A is detected and no promotion occurs."""
    dep_map = {1: [2], 2: [1]}
    cycles = tier_promotion.detect_cycles(dep_map)
    
    assert len(cycles) >= 1
    cycle_nodes = set()
    for cyc in cycles:
        cycle_nodes.update(cyc)
    assert {1, 2}.issubset(cycle_nodes)
    
    # Tiers exclude cyclic nodes
    tiers = tier_promotion.compute_tiers(dep_map)
    assert 1 not in tiers
    assert 2 not in tiers


def test_three_node_cycle_detected():
    """Three-node cycle: A→B→C→A is detected."""
    dep_map = {1: [2], 2: [3], 3: [1]}
    cycles = tier_promotion.detect_cycles(dep_map)
    
    assert len(cycles) >= 1
    cycle_nodes = set()
    for cyc in cycles:
        cycle_nodes.update(cyc)
    assert {1, 2, 3}.issubset(cycle_nodes)
    
    # All nodes in cycle excluded from tiers
    tiers = tier_promotion.compute_tiers(dep_map)
    assert 1 not in tiers
    assert 2 not in tiers
    assert 3 not in tiers


def test_partial_cycle_only_cyclic_nodes_excluded():
    """When only some nodes are in a cycle, non-cyclic nodes get tiers."""
    dep_map = {1: [], 2: [3], 3: [2], 4: [1]}
    # 2↔3 form a cycle; 1 and 4 are acyclic
    tiers = tier_promotion.compute_tiers(dep_map)
    
    assert 1 in tiers and tiers[1] == 0
    assert 4 in tiers and tiers[4] == 1
    assert 2 not in tiers
    assert 3 not in tiers


def test_cycle_in_promotion_no_issues_promoted():
    """When all sub-issues are in a cycle, no promotion occurs."""
    provider = _StubProvider()
    provider.set_issue(100, "Epic")
    provider.set_issue(101, "Epic: #100\nDepends on: #103")
    provider.set_issue(102, "Epic: #100\nDepends on: #101")
    provider.set_issue(103, "Epic: #100\nDepends on: #102")
    
    provider.set_blockers(101, [103])
    provider.set_blockers(102, [101])
    provider.set_blockers(103, [102])
    provider._states[101] = "closed"
    
    result = tier_promotion.promote_waiting_tiers(provider, just_closed=[101])
    
    assert result.promoted == []
    assert len(result.cycles) > 0


def test_cycle_mixed_with_acyclic():
    """When some nodes are cyclic and others aren't, only acyclic ones are promoted."""
    provider = _StubProvider()
    provider.set_issue(100, "Epic")
    provider.set_issue(101, "Epic: #100")  # tier 0, acyclic
    provider.set_issue(102, "Epic: #100\nDepends on: #104")  # cycle
    provider.set_issue(103, "Epic: #100\nDepends on: #105")  # cycle
    provider.set_issue(104, "Epic: #100\nDepends on: #103")  # cycle
    provider.set_issue(105, "Epic: #100\nDepends on: #102")  # cycle
    provider.set_issue(106, "Epic: #100\nDepends on: #101")  # tier 1, acyclic
    
    provider._states[101] = "closed"
    provider.set_blockers(102, [104])  # cycle
    provider.set_blockers(103, [105])  # cycle
    provider.set_blockers(104, [103])  # cycle
    provider.set_blockers(105, [102])  # cycle
    provider.set_blockers(106, [])  # 101 closed → unblocked
    
    result = tier_promotion.promote_waiting_tiers(provider, just_closed=[101])
    
    # Only #106 (acyclic, tier 1) is promoted
    assert 106 in result.promoted
    # Cyclic nodes not promoted
    assert 102 not in result.promoted
    assert 103 not in result.promoted
    assert 104 not in result.promoted
    assert 105 not in result.promoted
    # Cycles detected
    assert len(result.cycles) > 0


def test_self_dependency_detected_as_cycle():
    """A node depending on itself is a cycle and excluded from tiers."""
    dep_map = {1: [1], 2: [1]}
    # 1 depends on itself (self-loop = cycle), 2 depends on 1
    
    # Self-dependency is a cycle of length 1
    cycles = tier_promotion.detect_cycles(dep_map)
    cycle_nodes = set()
    for cyc in cycles:
        cycle_nodes.update(cyc)
    assert 1 in cycle_nodes, "Self-dependency should be detected as a cycle"
    
    # Node 1 is excluded from tiers (it's in a cycle)
    tiers = tier_promotion.compute_tiers(dep_map)
    assert 1 not in tiers, "Self-dependent node should be excluded from tiers"
    # Node 2's dependency on cyclic node 1 is filtered out (doesn't block),
    # so node 2 becomes tier 0 (no valid dependencies)
    assert 2 in tiers, "Non-cyclic node should still get a tier"
    assert tiers[2] == 0, "Node depending only on cyclic node becomes tier 0"


# ═══════════════════════════════════════════════════════════════════════════════
# Edge cases and integration
# ═══════════════════════════════════════════════════════════════════════════════

def test_external_dependencies_dont_block():
    """Dependencies on issues outside the epic are treated as satisfied."""
    dep_map = {1: [999], 2: [1]}  # 999 is external
    tiers = tier_promotion.compute_tiers(dep_map)
    # 999 not in dep_map → treated as external/satisfied
    assert tiers[1] == 0
    assert tiers[2] == 1


def test_empty_dep_map():
    """Empty dependency map returns empty tiers."""
    tiers = tier_promotion.compute_tiers({})
    assert tiers == {}


def test_no_promotion_when_nothing_closed():
    """When just_closed is empty, no promotion occurs."""
    provider = _StubProvider()
    provider.set_issue(100, "Epic")
    provider.set_issue(101, "Epic: #100")
    provider.set_issue(102, "Epic: #100\nDepends on: #101")
    provider.set_blockers(102, [101])
    
    result = tier_promotion.promote_waiting_tiers(provider, just_closed=[])
    assert result.promoted == []


def test_idempotent_promotion():
    """Issues already labeled Ready are not re-promoted."""
    provider = _StubProvider()
    provider.set_issue(100, "Epic")
    provider.set_issue(101, "Epic: #100")
    provider.set_issue(102, "Epic: #100\nDepends on: #101", labels=["Ready"])
    
    provider._states[101] = "closed"
    provider.set_blockers(102, [])
    
    result = tier_promotion.promote_waiting_tiers(provider, just_closed=[101])
    
    # 102 already has Ready label → not re-promoted
    assert 102 not in result.promoted


def test_complex_dependency_graph_tiers():
    """Complex graph: multiple paths, verify longest-path tier assignment."""
    # Graph:
    #   1 (tier 0)
    #   ↓
    #   2 (tier 1)
    #   ↓
    #   3 (tier 2)
    #   ↓
    #   4 (tier 3)
    # Also: 1 → 5 → 4 (5 is tier 1, but 4 is tier 3 due to longer path through 2→3)
    dep_map = {1: [], 2: [1], 3: [2], 4: [3, 5], 5: [1]}
    tiers = tier_promotion.compute_tiers(dep_map)
    
    assert tiers[1] == 0
    assert tiers[2] == 1
    assert tiers[3] == 2
    assert tiers[5] == 1
    # 4 depends on 3 (tier 2) and 5 (tier 1), so tier = max(2, 1) + 1 = 3
    assert tiers[4] == 3


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
