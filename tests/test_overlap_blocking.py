"""Unit tests for file-overlap detection and blocking-edge creation (#1058, #1059).

These tests cover the pure functions that detect when two sub-issue contexts
share file paths and build deterministic blocking chains (task N+1 blocked by
task N) so that overlapping tasks are serialized.

Part of epic #1050: planner should create blocking chains for tasks that touch
the same file(s).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.iterate import (  # noqa: E402
    EpicContext,
    detect_file_overlap,
    build_blocking_edges,
    _compute_sub_issue_dependencies,
)


# ── detect_file_overlap ──────────────────────────────────────────────────────


class TestDetectFileOverlap:
    """Tests for the pure overlap-detection function (#1058)."""

    def test_no_overlap_when_contexts_have_disjoint_files(self):
        """Two contexts touching different files → no overlap."""
        ctx_a = EpicContext(scope="A", file_paths=["src/auth/login.py"])
        ctx_b = EpicContext(scope="B", file_paths=["src/api/users.py"])
        result = detect_file_overlap([ctx_a, ctx_b])
        assert result == {}

    def test_single_overlap_pair(self):
        """Two contexts touching the same file → one overlap group."""
        ctx_a = EpicContext(scope="A", file_paths=["src/dispatch.py"])
        ctx_b = EpicContext(scope="B", file_paths=["src/dispatch.py"])
        result = detect_file_overlap([ctx_a, ctx_b])
        assert "src/dispatch.py" in result
        assert set(result["src/dispatch.py"]) == {0, 1}

    def test_multi_task_chain_on_same_file(self):
        """Three contexts all touching the same file → one group with all indices."""
        ctx_a = EpicContext(scope="A", file_paths=["core/iterate.py"])
        ctx_b = EpicContext(scope="B", file_paths=["core/iterate.py"])
        ctx_c = EpicContext(scope="C", file_paths=["core/iterate.py"])
        result = detect_file_overlap([ctx_a, ctx_b, ctx_c])
        assert "core/iterate.py" in result
        assert set(result["core/iterate.py"]) == {0, 1, 2}

    def test_multiple_files_multiple_groups(self):
        """Contexts touching different overlapping files produce separate groups."""
        ctx_a = EpicContext(scope="A", file_paths=["file1.py", "file2.py"])
        ctx_b = EpicContext(scope="B", file_paths=["file1.py"])
        ctx_c = EpicContext(scope="C", file_paths=["file2.py"])
        result = detect_file_overlap([ctx_a, ctx_b, ctx_c])
        assert set(result["file1.py"]) == {0, 1}
        assert set(result["file2.py"]) == {0, 2}

    def test_empty_contexts(self):
        """No contexts → no overlap."""
        assert detect_file_overlap([]) == {}

    def test_contexts_with_no_file_paths(self):
        """Contexts without file_paths → no overlap."""
        ctx_a = EpicContext(scope="A")
        ctx_b = EpicContext(scope="B")
        assert detect_file_overlap([ctx_a, ctx_b]) == {}

    def test_single_context_never_overlaps(self):
        """A single context can never overlap with another."""
        ctx = EpicContext(scope="A", file_paths=["file.py"])
        assert detect_file_overlap([ctx]) == {}


# ── build_blocking_edges ─────────────────────────────────────────────────────


class TestBuildBlockingEdges:
    """Tests for the blocking-edge builder (#1059)."""

    def test_single_overlap_pair_creates_one_edge(self):
        """Two tasks overlapping → task 1 blocked by task 0."""
        overlap = {"file.py": {0, 1}}
        edges = build_blocking_edges(overlap, total_tasks=2)
        assert edges == {1: [0]}

    def test_multi_task_chain_creates_chain(self):
        """Three tasks overlapping same file → chain: 1←0, 2←1."""
        overlap = {"file.py": {0, 1, 2}}
        edges = build_blocking_edges(overlap, total_tasks=3)
        assert edges == {1: [0], 2: [1]}

    def test_no_overlap_creates_no_edges(self):
        """No overlap → no edges."""
        edges = build_blocking_edges({}, total_tasks=3)
        assert edges == {}

    def test_no_duplicate_edges_when_already_present(self):
        """If an edge already exists, it is not duplicated."""
        overlap = {"file.py": {0, 1}}
        existing = {1: [0]}
        edges = build_blocking_edges(overlap, total_tasks=2, existing=existing)
        assert edges == {1: [0]}

    def test_no_circular_dependencies(self):
        """Blocking edges never create cycles."""
        overlap = {"file.py": {0, 1, 2}}
        edges = build_blocking_edges(overlap, total_tasks=3)
        # Verify no cycles: each task only depends on lower-numbered tasks
        for task, deps in edges.items():
            for dep in deps:
                assert dep < task, f"Circular dependency: task {task} depends on {dep}"

    def test_multiple_overlapping_files_merge_groups(self):
        """When task 0+1 share file_a and task 1+2 share file_b, the chain is 1←0, 2←1."""
        overlap = {"file_a.py": {0, 1}, "file_b.py": {1, 2}}
        edges = build_blocking_edges(overlap, total_tasks=3)
        # Task 1 blocked by 0 (file_a overlap)
        # Task 2 blocked by 1 (file_b overlap) — not by 0 (no file overlap between 0 and 2)
        assert 0 in edges.get(1, [])
        assert 1 in edges.get(2, [])
        assert 0 not in edges.get(2, [])

    def test_existing_edges_preserved(self):
        """Pre-existing edges (e.g. from sequential tier ordering) are preserved."""
        existing = {2: [0]}  # task 2 already depends on task 0
        overlap = {"file.py": {1, 2}}
        edges = build_blocking_edges(overlap, total_tasks=3, existing=existing)
        assert 0 in edges.get(2, [])  # preserved
        assert 1 in edges.get(2, [])  # added from overlap


# ── _compute_sub_issue_dependencies (integration of overlap + edges) ────────


class TestComputeSubIssueDependencies:
    """Tests for the combined function that computes depends_on per sub-issue."""

    def test_first_sub_issue_never_has_dependencies(self):
        """The first sub-issue in creation order always has empty depends_on."""
        contexts = [EpicContext(scope="A", file_paths=["file.py"])]
        deps = _compute_sub_issue_dependencies(contexts, index=0, created_numbers=[])
        assert deps == []

    def test_overlap_creates_dependency_on_previous(self):
        """When task N+1 overlaps task N, N+1 depends on N."""
        contexts = [
            EpicContext(scope="A", file_paths=["file.py"]),
            EpicContext(scope="B", file_paths=["file.py"]),
        ]
        # Simulate task 0 already created as issue #100
        deps = _compute_sub_issue_dependencies(contexts, index=1, created_numbers=[100])
        assert deps == [100]

    def test_no_overlap_creates_no_dependency(self):
        """When task N+1 does NOT overlap task N, no dependency is created."""
        contexts = [
            EpicContext(scope="A", file_paths=["file_a.py"]),
            EpicContext(scope="B", file_paths=["file_b.py"]),
        ]
        deps = _compute_sub_issue_dependencies(contexts, index=1, created_numbers=[100])
        assert deps == []

    def test_chain_of_three_overlapping(self):
        """Three tasks all touching the same file → chain 100←101←102."""
        contexts = [
            EpicContext(scope="A", file_paths=["file.py"]),
            EpicContext(scope="B", file_paths=["file.py"]),
            EpicContext(scope="C", file_paths=["file.py"]),
        ]
        deps_1 = _compute_sub_issue_dependencies(contexts, index=1, created_numbers=[100])
        assert deps_1 == [100]
        deps_2 = _compute_sub_issue_dependencies(contexts, index=2, created_numbers=[100, 101])
        assert deps_2 == [101]

    def test_partial_overlap_only_links_overlapping_pair(self):
        """Tasks 0 and 2 overlap, task 1 does not — task 2 depends on 0, task 1 is free."""
        contexts = [
            EpicContext(scope="A", file_paths=["shared.py"]),
            EpicContext(scope="B", file_paths=["other.py"]),
            EpicContext(scope="C", file_paths=["shared.py"]),
        ]
        deps_1 = _compute_sub_issue_dependencies(contexts, index=1, created_numbers=[100])
        assert deps_1 == []
        deps_2 = _compute_sub_issue_dependencies(contexts, index=2, created_numbers=[100, 101])
        assert deps_2 == [100]

    def test_no_duplicate_when_edge_already_exists(self):
        """If a dependency already exists, it is not duplicated."""
        contexts = [
            EpicContext(scope="A", file_paths=["file.py"]),
            EpicContext(scope="B", file_paths=["file.py"]),
        ]
        # Simulate task 0 already created and already in depends_on
        deps = _compute_sub_issue_dependencies(
            contexts, index=1, created_numbers=[100], existing_deps=[100]
        )
        assert deps == [100]  # no duplicate

    def test_empty_contexts_no_dependencies(self):
        """Empty contexts → no dependencies."""
        deps = _compute_sub_issue_dependencies([], index=0, created_numbers=[])
        assert deps == []

    def test_context_without_file_paths_sequential_fallback(self):
        """Contexts without file_paths → no overlap info → sequential fallback (all prior deps)."""
        contexts = [
            EpicContext(scope="A"),
            EpicContext(scope="B"),
        ]
        deps = _compute_sub_issue_dependencies(contexts, index=1, created_numbers=[100])
        assert deps == [100]  # sequential fallback when file_paths unknown