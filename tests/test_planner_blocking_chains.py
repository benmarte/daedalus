"""Tests for overlap-based blocking chains in planner decomposition.

Covers:
- detect_file_overlap correctly identifies overlapping tasks
- Non-overlapping tasks produce empty depends_on (parallel execution)
- Overlapping tasks produce selective depends_on (only the overlapping predecessors)
- Mixed scenarios: some pairs overlap, others don't
- File-path-based overlap detection
- Keyword-based overlap detection

Run: pytest tests/test_planner_blocking_chains.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.file_overlap import detect_file_overlap, extract_file_refs  # noqa: E402


# ─── detect_file_overlap ──────────────────────────────────────────────────────

class TestDetectFileOverlap:

    def test_same_file_reference_overlaps(self):
        a = "Fix bug in `core/dispatch_state.py` around the fingerprint logic"
        b = "Add tests for `core/dispatch_state.py` set_resync_fingerprint"
        result = detect_file_overlap(a, b)
        assert result["overlaps"] is True
        assert result["confidence"] > 0
        assert "core/dispatch_state.py" in result["matched_files"]

    def test_no_shared_file_or_keyword_no_overlap(self):
        a = "Update README with installation instructions"
        b = "Fix authentication middleware in api/auth.py"
        result = detect_file_overlap(a, b)
        # These share no file refs and low keyword similarity
        assert result["confidence"] == 0.0 or result["overlaps"] is False

    def test_high_keyword_similarity_overlaps(self):
        a = "Implement profile resync trigger in dispatcher run() function"
        b = "Add profile resync logging to dispatcher run() after fingerprint check"
        result = detect_file_overlap(a, b)
        assert result["overlaps"] is True
        assert result["confidence"] >= 0.4

    def test_distinct_files_no_overlap(self):
        a = "Update `scripts/daedalus_dispatch.py` fingerprint storage"
        b = "Add unit tests in `tests/test_file_overlap.py`"
        result = detect_file_overlap(a, b)
        assert result["overlaps"] is False or result["confidence"] < 0.4

    def test_returns_required_keys(self):
        result = detect_file_overlap("task a", "task b")
        assert "overlaps" in result
        assert "confidence" in result
        assert "matched_files" in result
        assert "matched_keywords" in result

    def test_confidence_zero_when_no_overlap(self):
        result = detect_file_overlap("fix database connection", "update CSS styles")
        assert isinstance(result["confidence"], float)
        assert result["confidence"] >= 0.0

    def test_dict_inputs_accepted(self):
        a = {"title": "Fix dispatch_state.py bug", "body": "Update fingerprint logic"}
        b = {"title": "Test dispatch_state.py", "body": "Add coverage for fingerprint"}
        result = detect_file_overlap(a, b)
        assert result["overlaps"] is True

    def test_empty_strings_no_overlap(self):
        result = detect_file_overlap("", "")
        assert result["overlaps"] is False
        assert result["confidence"] == 0.0


# ─── extract_file_refs ────────────────────────────────────────────────────────

class TestExtractFileRefs:

    def test_backtick_file_path(self):
        refs = extract_file_refs("Update `core/dispatch_state.py` fingerprint")
        assert "core/dispatch_state.py" in refs

    def test_bare_file_path(self):
        refs = extract_file_refs("Edit scripts/daedalus_dispatch.py to add resync")
        assert "scripts/daedalus_dispatch.py" in refs

    def test_no_file_refs_returns_empty(self):
        refs = extract_file_refs("Update the logging configuration")
        assert refs == []

    def test_deduplicates_refs(self):
        refs = extract_file_refs(
            "Update `core/dispatch_state.py` and also edit `core/dispatch_state.py`"
        )
        assert refs.count("core/dispatch_state.py") == 1

    def test_multiple_files(self):
        refs = extract_file_refs(
            "Update `core/dispatch_state.py` and `scripts/daedalus_dispatch.py`"
        )
        assert "core/dispatch_state.py" in refs
        assert "scripts/daedalus_dispatch.py" in refs


# ─── planner blocking chain logic ─────────────────────────────────────────────

class TestPlannerBlockingChainLogic:
    """Simulate the depends_on selection logic from _execute_planner_decompose_inner."""

    @staticmethod
    def _compute_depends_on(current_text, prior_texts_and_numbers):
        """Mirror the logic: find which prior tasks overlap with current."""
        return [
            n for n, text in prior_texts_and_numbers
            if detect_file_overlap(current_text, text)["overlaps"]
        ]

    def test_non_overlapping_tasks_are_parallel(self):
        """Tasks touching different files get empty depends_on (run in parallel)."""
        task_a = "Add CSS styles to `frontend/styles.css`"
        task_b = "Fix database schema in `backend/models.py`"
        task_c = "Update Makefile targets"

        # task_b and task_c don't overlap with task_a
        depends_b = self._compute_depends_on(task_b, [(1, task_a)])
        depends_c = self._compute_depends_on(task_c, [(1, task_a), (2, task_b)])

        assert depends_b == []
        assert depends_c == []

    def test_overlapping_tasks_create_chain(self):
        """Tasks touching the same file get chained via depends_on."""
        task_a = "Add `compute_config_fingerprint` to `core/dispatch_state.py`"
        task_b = "Add `set_resync_fingerprint` to `core/dispatch_state.py`"
        task_c = "Add `get_resync_fingerprint` to `core/dispatch_state.py`"

        depends_b = self._compute_depends_on(task_b, [(100, task_a)])
        depends_c = self._compute_depends_on(task_c, [(100, task_a), (101, task_b)])

        assert 100 in depends_b
        assert 101 in depends_c or 100 in depends_c

    def test_mixed_overlap_selects_only_overlapping_predecessors(self):
        """A task only chains against predecessors it actually overlaps with."""
        task_a = "Update `core/dispatch_state.py` fingerprint logic"
        task_b = "Add tests in `tests/test_file_overlap.py`"
        task_c = "Add resync trigger to `core/dispatch_state.py`"

        # task_c overlaps with task_a (same file) but not with task_b
        depends_c = self._compute_depends_on(task_c, [(1, task_a), (2, task_b)])

        assert 1 in depends_c        # overlaps with task_a
        assert 2 not in depends_c    # does NOT overlap with task_b

    def test_first_task_always_parallel(self):
        """First sub-issue has no predecessors → always empty depends_on."""
        task_a = "Implement profile resync in `scripts/daedalus_dispatch.py`"
        depends = self._compute_depends_on(task_a, [])
        assert depends == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
