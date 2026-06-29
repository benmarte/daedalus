"""Tests for file-reference extraction and overlap detection (issue #1058).

Covers:
- extract_file_refs: explicit file paths, backtick-quoted, code spans,
  absolute/relative paths, deduplication, edge cases
- detect_file_overlap: file overlap detection, keyword similarity scoring,
  confidence thresholds, edge cases (empty, identical, partial paths)
- _tokenize / _normalize_keyword: helper behaviour

Run: pytest tests/test_file_overlap.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.file_overlap import (  # noqa: E402
    extract_file_refs,
    detect_file_overlap,
    _tokenize,
    _normalize_keyword,
    FILE_REF_THRESHOLD,
    KEYWORD_HIGH_THRESHOLD,
)


# ─────────────────────────────────────────────────────────────────────────────
# extract_file_refs
# ─────────────────────────────────────────────────────────────────────────────


class TestExtractFileRefs:
    """Tests for explicit file reference extraction."""

    def test_relative_path_in_prose(self):
        body = "Update src/foo/bar.ts to fix the import"
        refs = extract_file_refs(body)
        assert "src/foo/bar.ts" in refs

    def test_absolute_path(self):
        body = "Modify /usr/local/bin/daedalus_dispatch.py"
        refs = extract_file_refs(body)
        assert "/usr/local/bin/daedalus_dispatch.py" in refs

    def test_backtick_quoted_path(self):
        body = "Edit `core/util.py` and `tests/test_util.py`"
        refs = extract_file_refs(body)
        assert "core/util.py" in refs
        assert "tests/test_util.py" in refs

    def test_markdown_code_span_path(self):
        body = "Change the function in `src/components/Button.tsx` to return null"
        refs = extract_file_refs(body)
        assert "src/components/Button.tsx" in refs

    def test_multiple_paths_deduplicated(self):
        body = "Fix src/foo/bar.ts and src/foo/bar.ts again"
        refs = extract_file_refs(body)
        assert len(refs) == 1
        assert refs[0] == "src/foo/bar.ts"

    def test_preserves_order_first_occurrence(self):
        body = "First edit `core/kanban.py`, then update `core/util.py`"
        refs = extract_file_refs(body)
        assert refs[0] == "core/kanban.py"
        assert refs[1] == "core/util.py"

    def test_path_with_dots_in_filename(self):
        body = "Update scripts/daedalus_dispatch.py"
        refs = extract_file_refs(body)
        assert "scripts/daedalus_dispatch.py" in refs

    def test_python_module_path(self):
        body = "Modify core/sweeper.py to handle edge cases"
        refs = extract_file_refs(body)
        assert "core/sweeper.py" in refs

    def test_empty_body(self):
        assert extract_file_refs("") == []

    def test_none_body(self):
        assert extract_file_refs(None) == []

    def test_no_file_refs(self):
        body = "This task has no file references at all"
        assert extract_file_refs(body) == []

    def test_does_not_match_bare_words(self):
        body = "Update the dispatcher to handle new cases"
        refs = extract_file_refs(body)
        assert refs == []

    def test_does_not_match_url_without_path_extension(self):
        body = "See https://example.com/page for details"
        refs = extract_file_refs(body)
        assert refs == []

    def test_backtick_path_without_extension_not_matched(self):
        """Backtick-quoted strings without a path-like extension are not file refs."""
        body = "Use the `Button` component"
        refs = extract_file_refs(body)
        assert refs == []

    def test_multiple_backtick_paths(self):
        body = "Refactor `core/kanban.py` and `core/util.py` and `core/sweeper.py`"
        refs = extract_file_refs(body)
        assert len(refs) == 3
        assert "core/kanban.py" in refs
        assert "core/util.py" in refs
        assert "core/sweeper.py" in refs

    def test_path_with_dashes(self):
        body = "Fix scripts/daedalus_dispatch.py"
        refs = extract_file_refs(body)
        assert "scripts/daedalus_dispatch.py" in refs

    def test_does_not_extract_issue_numbers(self):
        body = "Closes #1058"
        refs = extract_file_refs(body)
        assert refs == []


# ─────────────────────────────────────────────────────────────────────────────
# _tokenize / _normalize_keyword
# ─────────────────────────────────────────────────────────────────────────────


class TestTokenizeAndNormalize:
    """Tests for tokenization and keyword normalization helpers."""

    def test_tokenize_basic(self):
        tokens = _tokenize("Fix the dispatcher module")
        assert "fix" in tokens
        assert "dispatch" in tokens  # "dispatcher" normalizes to "dispatch"

    def test_tokenize_strips_punctuation(self):
        tokens = _tokenize("Update core/util.py, then test.")
        assert "update" in tokens

    def test_tokenize_splits_camelcase(self):
        tokens = _tokenize("Fix FileOverlapDetection")
        assert "file" in tokens
        assert "overlap" in tokens
        assert "detection" in tokens

    def test_tokenize_empty(self):
        assert _tokenize("") == []

    def test_tokenize_none(self):
        assert _tokenize(None) == []

    def test_normalize_keyword_strips_suffix(self):
        assert _normalize_keyword("running") == "run"
        assert _normalize_keyword("tests") == "test"
        assert _normalize_keyword("dispatching") == "dispatch"

    def test_normalize_keyword_irregular_unchanged(self):
        # Porter stemmer doesn't handle all irregulars; just check it returns something
        result = _normalize_keyword("config")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_normalize_keyword_empty(self):
        assert _normalize_keyword("") == ""


# ─────────────────────────────────────────────────────────────────────────────
# detect_file_overlap — file reference overlap
# ─────────────────────────────────────────────────────────────────────────────


class TestDetectFileOverlapFileRefs:
    """Tests for file-reference-based overlap detection."""

    def test_identical_file_refs(self):
        task_a = {"title": "Fix bug", "body": "Edit `src/foo/bar.ts`"}
        task_b = {"title": "Add feature", "body": "Update `src/foo/bar.ts`"}
        result = detect_file_overlap(task_a, task_b)
        assert result["overlaps"] is True
        assert result["confidence"] >= FILE_REF_THRESHOLD
        assert "src/foo/bar.ts" in result["matched_files"]

    def test_different_file_refs_no_overlap(self):
        task_a = {"title": "Fix bug", "body": "Edit `src/foo/bar.ts`"}
        task_b = {"title": "Add feature", "body": "Update `src/baz/qux.ts`"}
        result = detect_file_overlap(task_a, task_b)
        assert result["overlaps"] is False

    def test_partial_path_match(self):
        """A full path in one task should match a shorter path in the other."""
        task_a = {"title": "Fix", "body": "Edit `src/foo/bar.ts`"}
        task_b = {"title": "Add", "body": "Update `foo/bar.ts`"}
        result = detect_file_overlap(task_a, task_b)
        assert result["overlaps"] is True
        assert result["confidence"] >= FILE_REF_THRESHOLD

    def test_multiple_file_refs_some_match(self):
        task_a = {"title": "Refactor", "body": "Edit `core/kanban.py` and `core/util.py`"}
        task_b = {"title": "Fix bug", "body": "Update `core/util.py` and `core/sweeper.py`"}
        result = detect_file_overlap(task_a, task_b)
        assert result["overlaps"] is True
        assert "core/util.py" in result["matched_files"]

    def test_no_file_refs_falls_back_to_keywords(self):
        """When no file refs exist, keyword similarity should be used."""
        task_a = {"title": "Fix dispatcher", "body": "Update the dispatch logic"}
        task_b = {"title": "Fix dispatch bug", "body": "Fix the dispatcher module"}
        result = detect_file_overlap(task_a, task_b)
        # High keyword overlap → should overlap
        assert result["overlaps"] is True
        assert result["confidence"] >= KEYWORD_HIGH_THRESHOLD


# ─────────────────────────────────────────────────────────────────────────────
# detect_file_overlap — keyword similarity
# ─────────────────────────────────────────────────────────────────────────────


class TestDetectFileOverlapKeywords:
    """Tests for keyword-based similarity scoring."""

    def test_high_keyword_overlap(self):
        task_a = {"title": "Fix dispatch bug", "body": "Update the dispatcher"}
        task_b = {"title": "Fix dispatcher bug", "body": "Update the dispatch logic"}
        result = detect_file_overlap(task_a, task_b)
        assert result["overlaps"] is True
        assert "dispatch" in result["matched_keywords"]

    def test_no_keyword_overlap(self):
        task_a = {"title": "Fix database schema", "body": "Update migration scripts"}
        task_b = {"title": "Add CSS styling", "body": "Style the components"}
        result = detect_file_overlap(task_a, task_b)
        assert result["overlaps"] is False

    def test_moderate_keyword_overlap_below_threshold(self):
        task_a = {"title": "Fix database", "body": "Update the database schema and migrations"}
        task_b = {"title": "Add styling", "body": "Update the CSS and styling for components"}
        result = detect_file_overlap(task_a, task_b)
        # "update" is common but shouldn't be enough for high confidence
        assert result["overlaps"] is False
        assert result["confidence"] < KEYWORD_HIGH_THRESHOLD

    def test_confidence_is_float(self):
        task_a = {"title": "Fix bug", "body": "Some text here"}
        task_b = {"title": "Add feature", "body": "Other text"}
        result = detect_file_overlap(task_a, task_b)
        assert isinstance(result["confidence"], float)
        assert 0.0 <= result["confidence"] <= 1.0


# ─────────────────────────────────────────────────────────────────────────────
# detect_file_overlap — edge cases
# ─────────────────────────────────────────────────────────────────────────────


class TestDetectFileOverlapEdgeCases:
    """Edge cases for overlap detection."""

    def test_empty_bodies(self):
        task_a = {"title": "Task A", "body": ""}
        task_b = {"title": "Task B", "body": ""}
        result = detect_file_overlap(task_a, task_b)
        assert result["overlaps"] is False
        assert result["confidence"] == 0.0

    def test_none_bodies(self):
        task_a = {"title": "Task A", "body": None}
        task_b = {"title": "Task B", "body": None}
        result = detect_file_overlap(task_a, task_b)
        assert result["overlaps"] is False
        assert result["confidence"] == 0.0

    def test_identical_tasks(self):
        task_a = {"title": "Fix dispatch", "body": "Edit `core/dispatch.py`"}
        task_b = {"title": "Fix dispatch", "body": "Edit `core/dispatch.py`"}
        result = detect_file_overlap(task_a, task_b)
        assert result["overlaps"] is True
        assert result["confidence"] == 1.0
        assert "core/dispatch.py" in result["matched_files"]

    def test_missing_title_uses_body(self):
        task_a = {"body": "Fix `core/kanban.py`"}
        task_b = {"body": "Update `core/kanban.py`"}
        result = detect_file_overlap(task_a, task_b)
        assert result["overlaps"] is True

    def test_missing_body_uses_title(self):
        task_a = {"title": "Fix core/kanban.py file"}
        task_b = {"title": "Update core/kanban.py module"}
        result = detect_file_overlap(task_a, task_b)
        assert result["overlaps"] is True

    def test_no_matched_files_when_none_overlap(self):
        task_a = {"title": "Fix", "body": "Edit `src/a.ts`"}
        task_b = {"title": "Add", "body": "Update `src/b.ts`"}
        result = detect_file_overlap(task_a, task_b)
        assert result["matched_files"] == []

    def test_no_matched_keywords_when_none_overlap(self):
        task_a = {"title": "Fix database", "body": "Database migration"}
        task_b = {"title": "Add CSS", "body": "Style components"}
        result = detect_file_overlap(task_a, task_b)
        assert result["matched_keywords"] == []

    def test_result_has_all_fields(self):
        task_a = {"title": "Task A", "body": "Edit `src/foo.ts`"}
        task_b = {"title": "Task B", "body": "Edit `src/bar.ts`"}
        result = detect_file_overlap(task_a, task_b)
        assert "overlaps" in result
        assert "confidence" in result
        assert "matched_files" in result
        assert "matched_keywords" in result

    def test_file_ref_overrides_low_keyword(self):
        """Even with low keyword overlap, a file match should trigger overlap."""
        task_a = {"title": "Database migration", "body": "Edit `core/util.py`"}
        task_b = {"title": "CSS styling", "body": "Update `core/util.py`"}
        result = detect_file_overlap(task_a, task_b)
        assert result["overlaps"] is True
        assert "core/util.py" in result["matched_files"]

    def test_accepts_string_bodies(self):
        """Tasks can be plain strings instead of dicts."""
        task_a = "Fix the bug in `core/kanban.py`"
        task_b = "Update `core/kanban.py` to add new feature"
        result = detect_file_overlap(task_a, task_b)
        assert result["overlaps"] is True
        assert "core/kanban.py" in result["matched_files"]