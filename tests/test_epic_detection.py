"""Tests for epic detection heuristic (issue #138 Phase 1).

``is_epic`` lives in ``core.providers.base`` so it sits next to the issue data
models it inspects. These tests exercise the three heuristics independently,
edge cases (None, missing keys, mixed input shapes), and the OR-combination
contract.

Phase 1 is detection only — no dispatcher changes are tested here.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.providers.base import is_epic, _EPIC_CHECKLIST_MIN, _EPIC_BODY_SIZE_MIN


def _make_issue(body: str = "", labels=None) -> dict:
    return {"body": body, "labels": labels if labels is not None else []}


# ── Heuristic 1: checklist density ──────────────────────────────────────────


class TestChecklistHeuristic:
    def test_exactly_min_dash_items_is_epic(self):
        body = "\n".join("- [ ] task " + str(i) for i in range(_EPIC_CHECKLIST_MIN))
        assert is_epic(_make_issue(body=body)) is True

    def test_below_min_items_not_epic(self):
        body = "\n".join("- [ ] task " + str(i) for i in range(_EPIC_CHECKLIST_MIN - 1))
        assert is_epic(_make_issue(body=body)) is False

    def test_asterisk_markers_count(self):
        body = "* [ ] one\n* [ ] two\n* [ ] three\n* [ ] four"
        assert is_epic(_make_issue(body=body)) is True

    def test_plus_markers_count(self):
        body = "+ [ ] one\n+ [ ] two\n+ [ ] three\n+ [ ] four"
        assert is_epic(_make_issue(body=body)) is True

    def test_completed_items_still_count(self):
        body = "- [x] done\n- [X] also done\n- [ ] todo\n- [ ] todo more"
        assert is_epic(_make_issue(body=body)) is True

    def test_mixed_markers_count(self):
        body = "- [ ] dash\n* [x] star\n+ [X] plus\n- [ ] more"
        assert is_epic(_make_issue(body=body)) is True

    def test_indented_items_count(self):
        body = "    - [ ] nested\n    - [ ] nested\n    - [ ] nested\n    - [ ] nested"
        assert is_epic(_make_issue(body=body)) is True

    def test_plain_bullets_do_not_count(self):
        body = "- item\n- item\n- item\n- item\n- item\n- item"
        assert is_epic(_make_issue(body=body)) is False

    def test_numbered_list_does_not_count(self):
        body = "1. step\n2. step\n3. step\n4. step\n5. step"
        assert is_epic(_make_issue(body=body)) is False

    def test_empty_text_in_brackets(self):
        body = "- [] not a checkbox\n- [] not a checkbox\n- [] not a checkbox\n- [] not a checkbox"
        assert is_epic(_make_issue(body=body)) is False


# ── Heuristic 2: epic label ─────────────────────────────────────────────────


class TestEpicLabelHeuristic:
    def test_exact_epic_label(self):
        assert is_epic(_make_issue(labels=[{"name": "epic"}])) is True

    def test_case_insensitive_lower(self):
        assert is_epic(_make_issue(labels=[{"name": "epic"}])) is True

    def test_case_insensitive_upper(self):
        assert is_epic(_make_issue(labels=[{"name": "EPIC"}])) is True

    def test_case_insensitive_mixed(self):
        assert is_epic(_make_issue(labels=[{"name": "Epic"}])) is True

    def test_partial_match_not_epic(self):
        assert is_epic(_make_issue(labels=[{"name": "epic-like"}])) is False
        assert is_epic(_make_issue(labels=[{"name": "myth-epic"}])) is False

    def test_unrelated_labels(self):
        assert is_epic(_make_issue(labels=[{"name": "bug"}, {"name": "priority"}])) is False

    def test_empty_labels(self):
        assert is_epic(_make_issue(labels=[])) is False

    def test_none_labels(self):
        assert is_epic({"body": "x", "labels": None}) is False

    def test_string_labels(self):
        # Provider dicts use {"name": ...} but guard for plain strings
        assert is_epic(_make_issue(labels=["epic"])) is True
        assert is_epic(_make_issue(labels=[{"name": "epic"}])) is True


# ── Heuristic 3: body size ──────────────────────────────────────────────────


class TestBodySizeHeuristic:
    def test_at_threshold_is_epic(self):
        assert is_epic(_make_issue(body="x" * _EPIC_BODY_SIZE_MIN)) is True

    def test_above_threshold_is_epic(self):
        assert is_epic(_make_issue(body="x" * (_EPIC_BODY_SIZE_MIN + 1000))) is True

    def test_below_threshold_not_epic(self):
        assert is_epic(_make_issue(body="x" * (_EPIC_BODY_SIZE_MIN - 1))) is False

    def test_very_small_body(self):
        assert is_epic(_make_issue(body="fix the bug")) is False


# ── Input shape flexibility ─────────────────────────────────────────────────


class TestInputShapes:
    def test_object_with_body_and_labels_attr(self):
        class FakeIssue:
            body = "- [ ] a\n- [ ] b\n- [ ] c\n- [ ] d"
            labels = []
        assert is_epic(FakeIssue()) is True

    def test_dict_shape(self):
        assert is_epic({"body": "x" * 3000, "labels": []}) is True

    def test_none_input(self):
        assert is_epic(None) is False

    def test_missing_body_key(self):
        assert is_epic({"labels": []}) is False

    def test_missing_labels_key(self):
        # Body large enough to trigger
        assert is_epic({"body": "y" * 3000}) is True

    def test_none_body(self):
        assert is_epic({"body": None, "labels": []}) is False

    def test_empty_body_and_labels(self):
        assert is_epic({"body": "", "labels": []}) is False


# ── Or-combination ──────────────────────────────────────────────────────────


class TestOrCombination:
    def test_only_checklist_triggers(self):
        body = "\n".join("- [ ] x" for _ in range(_EPIC_CHECKLIST_MIN))
        assert is_epic(_make_issue(body=body)) is True

    def test_only_label_triggers(self):
        assert is_epic(_make_issue(labels=[{"name": "epic"}])) is True

    def test_only_size_triggers(self):
        assert is_epic(_make_issue(body="z" * _EPIC_BODY_SIZE_MIN)) is True

    def test_no_triggers_not_epic(self):
        assert is_epic(_make_issue(body="small issue", labels=[{"name": "bug"}])) is False
