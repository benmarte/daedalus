"""Tests for epic-issue detection (issues #138 and #149).

Heuristic tests use ``is_epic`` from ``core.providers.base`` directly (Phase 1,
#138). Planner-body and dispatcher-routing tests use the dispatch module (#149).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.providers.base import is_epic, _EPIC_CHECKLIST_MIN, _EPIC_BODY_SIZE_MIN
from conftest import _load_dispatch  # noqa: E402

disp = _load_dispatch()


def _make_issue(body: str = "", labels=None, title: str = "Test", number: int = 1) -> dict:
    return {
        "number": number,
        "title": title,
        "body": body,
        "labels": labels if labels is not None else [],
        "url": "https://example.com/issues/1",
    }


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
    def test_subtask_label_excludes_non_epic(self):
        """Issues labelled 'subtask' must never be classified as epics.

        Sub-issues created by Phase 3 decomposition are never epics themselves;
        without this guard, a large sub-issue body or inherited labels could
        trigger the heuristic and cause infinite decomposition loops.
        """
        # Subtask + epic-like label → still excluded
        assert is_epic(_make_issue(labels=[{"name": "subtask"}])) is False
        assert is_epic(_make_issue(labels=[{"name": "subtask"}, {"name": "epic"}])) is False
        assert is_epic(_make_issue(labels=[{"name": "SUBTASK"}])) is False
        assert is_epic(_make_issue(labels=["subtask"])) is False
        # Subtask + huge body → still excluded (body-size heuristic must not fire)
        assert is_epic(_make_issue(body="x" * 5000, labels=[{"name": "subtask"}])) is False
        # Subtask + checklist → still excluded
        body = "\n".join(f"- [ ] task {i}" for i in range(10))
        assert is_epic(_make_issue(body=body, labels=[{"name": "subtask"}])) is False

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


# ── _planner_body output (dispatch-specific, #149) ─────────────────────────


class TestPlannerBody:
    def test_contains_issue_number(self):
        issue = _make_issue(number=100, title="Big task", body="- [ ] t\n" * 5)
        body = disp._planner_body("org/repo", issue, "/work", "main", "github")
        assert "#100" in body

    def test_contains_title(self):
        issue = _make_issue(number=200, title="Big Task Title", body="- [ ] t\n" * 5)
        body = disp._planner_body("org/repo", issue, "/work", "main", "github")
        assert "Big Task Title" in body

    def test_mentions_planning_complete(self):
        issue = _make_issue(number=300, body="- [ ] t\n" * 5)
        body = disp._planner_body("org/repo", issue, "/work", "main", "github")
        assert "PLANNING COMPLETE" in body

    def test_lists_detection_reasons(self):
        issue = _make_issue(number=400, body="- [ ] t\n" * 5, labels=[{"name": "epic"}])
        body = disp._planner_body("org/repo", issue, "/work", "main", "github")
        assert "checklist" in body.lower()
        assert "epic" in body.lower()

    def test_contains_repo_info(self):
        issue = _make_issue(body="- [ ] t\n" * 5)
        body = disp._planner_body("owner/repo", issue, "/path/to/work", "dev", "github")
        assert "owner/repo" in body
        assert "/path/to/work" in body
        assert "dev" in body
        assert "github" in body

    def test_original_body_excerpt(self):
        original = "This is the original issue description for testing purposes."
        issue = _make_issue(body=original, labels=[{"name": "epic"}])
        body = disp._planner_body("org/repo", issue, "/work", "main", "github")
        assert original in body

    def test_truncates_large_body(self):
        long_body = "X" * 2000
        issue = _make_issue(body=long_body)
        body = disp._planner_body("org/repo", issue, "/work", "main", "github")
        assert "X" * 1000 in body
        assert len(body) < len(long_body)


# ── Dispatcher routing (integration, #149) ──────────────────────────────────


class TestDispatcherRouting:
    def test_planner_profile_registered(self):
        assert "planner" in disp._DEFAULT_PROFILES
        assert disp._DEFAULT_PROFILES["planner"] == "planner-daedalus"

    def test_planner_in_role_tmp_prefix(self):
        assert "planner" in disp._ROLE_TMP_PREFIX
        assert disp._ROLE_TMP_PREFIX["planner"] == "planner"
