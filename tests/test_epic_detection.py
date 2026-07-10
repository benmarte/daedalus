"""Tests for epic-issue detection (issues #138 and #149).

Heuristic tests use ``is_epic`` from ``core.providers.base`` directly (Phase 1,
#138). Planner-body and dispatcher-routing tests use the dispatch module (#149).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.providers.base import (
    is_epic,
    _EPIC_CHECKLIST_MIN,
    _EPIC_BODY_SIZE_MIN,
    _deliverable_checklist_items,
    _has_sub_issue_checklist,
    _is_single_ac_bug,
)
from conftest import _load_dispatch  # noqa: E402

disp = _load_dispatch()


def _make_issue(
    body: str = "", labels=None, title: str = "Test", number: int = 1
) -> dict:
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
        assert (
            is_epic(_make_issue(labels=[{"name": "subtask"}, {"name": "epic"}]))
            is False
        )
        assert is_epic(_make_issue(labels=[{"name": "SUBTASK"}])) is False
        assert is_epic(_make_issue(labels=["subtask"])) is False
        # Subtask + huge body → still excluded (body-size heuristic must not fire)
        assert (
            is_epic(_make_issue(body="x" * 5000, labels=[{"name": "subtask"}])) is False
        )
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
        assert (
            is_epic(_make_issue(labels=[{"name": "bug"}, {"name": "priority"}]))
            is False
        )

    def test_empty_labels(self):
        assert is_epic(_make_issue(labels=[])) is False

    def test_none_labels(self):
        assert is_epic({"body": "x", "labels": None}) is False

    def test_string_labels(self):
        assert is_epic(_make_issue(labels=["epic"])) is True
        assert is_epic(_make_issue(labels=[{"name": "epic"}])) is True


# ── Heuristic 3: body size + semantic signal ────────────────────────────────


class TestBodySizeHeuristic:
    def test_at_threshold_with_decomp_language_is_epic(self):
        # Body size alone is insufficient; needs a semantic decomposition signal.
        body = "x" * _EPIC_BODY_SIZE_MIN + " decompose into multiple phases"
        assert is_epic(_make_issue(body=body)) is True

    def test_above_threshold_with_decomp_language_is_epic(self):
        body = "x" * (_EPIC_BODY_SIZE_MIN + 1000) + " Phase 1 of 3"
        assert is_epic(_make_issue(body=body)) is True

    def test_size_alone_does_not_trigger(self):
        # Large body with no decomp language and no sub-issue refs → not epic (#1100).
        assert is_epic(_make_issue(body="x" * _EPIC_BODY_SIZE_MIN)) is False

    def test_below_threshold_not_epic(self):
        assert is_epic(_make_issue(body="x" * (_EPIC_BODY_SIZE_MIN - 1))) is False

    def test_very_small_body(self):
        assert is_epic(_make_issue(body="fix the bug")) is False

    def test_sub_issue_checklist_with_large_body_is_epic(self):
        body = (
            "x" * _EPIC_BODY_SIZE_MIN
            + "\n- [ ] #101 implement auth\n- [ ] #102 add tests"
        )
        assert is_epic(_make_issue(body=body)) is True


# ── Input shape flexibility ─────────────────────────────────────────────────


class TestInputShapes:
    def test_object_with_body_and_labels_attr(self):
        class FakeIssue:
            body = "- [ ] a\n- [ ] b\n- [ ] c\n- [ ] d"
            labels = []

        assert is_epic(FakeIssue()) is True

    def test_dict_shape_with_decomp_language(self):
        # Large body classified as epic only when decomp language is present.
        body = "x" * 3000 + " decompose into phases"
        assert is_epic({"body": body, "labels": []}) is True

    def test_dict_shape_large_body_no_signal_not_epic(self):
        # Body size alone no longer sufficient (#1100).
        assert is_epic({"body": "x" * 3000, "labels": []}) is False

    def test_none_input(self):
        assert is_epic(None) is False

    def test_missing_body_key(self):
        assert is_epic({"labels": []}) is False

    def test_missing_labels_key_with_decomp_language(self):
        body = "y" * 3000 + " decompose into phases"
        assert is_epic({"body": body}) is True

    def test_missing_labels_key_large_body_no_signal_not_epic(self):
        # Body size alone no longer sufficient (#1100).
        assert is_epic({"body": "y" * 3000}) is False

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

    def test_size_with_decomp_language_triggers(self):
        # Body size + decomposition language → epic.
        body = "z" * _EPIC_BODY_SIZE_MIN + " decompose into phases"
        assert is_epic(_make_issue(body=body)) is True

    def test_size_alone_does_not_trigger(self):
        # Body size alone is no longer sufficient (#1100).
        assert is_epic(_make_issue(body="z" * _EPIC_BODY_SIZE_MIN)) is False

    def test_no_triggers_not_epic(self):
        assert (
            is_epic(_make_issue(body="small issue", labels=[{"name": "bug"}])) is False
        )


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
        # Assert the truncation itself — a total-length check breaks whenever
        # the planner template legitimately grows (#1241 inline-execution guard).
        assert "X" * 1001 not in body


# ── Semantic signals: AC exclusion + decomp language (#1100) ────────────────


_LONG_BUG_BODY = """\
## Problem

The dispatcher's epic-detection heuristic uses issue body length as a proxy for
epic size. Bug reports with detailed acceptance criteria, observed/expected sections,
or reproduction steps exceed the length threshold and get routed to the planner
instead of the validator.

## Observed

Issue #1099 was classified as an epic despite being a targeted bug fix.

## Expected

A well-scoped bug fix with clear acceptance criteria should always route to the
validator, regardless of body length.

## Acceptance Criteria

- [ ] Epic detection uses semantic signals in addition to body length
- [ ] Issues with a single AC block and no sub-issue checklist are NOT epics
- [ ] Existing epic decomposition behavior is unchanged
- [ ] Unit tests cover the new cases
"""

_SHORT_EPIC_BODY = """\
## Overview

We need to split our auth system into three independent phases.

Phase 1: Migrate to OAuth2
Phase 2: Add MFA support
Phase 3: Session management overhaul
"""

_SUB_ISSUE_BODY = """\
## Tasks

- [ ] #101 Implement OAuth2 login
- [ ] #102 Add MFA support
- [ ] #103 Session management
"""


class TestSemanticSignals:
    """Tests for semantic epic detection (#1100) — AC exclusion and decomp language."""

    # -- AC exclusion: well-scoped bug reports are never epics --

    def test_long_body_bug_with_ac_routes_to_validator(self):
        """A detailed bug report with ## Acceptance Criteria is NOT an epic."""
        assert is_epic(_make_issue(body=_LONG_BUG_BODY)) is False

    def test_single_ac_block_no_sub_issues_not_epic(self):
        body = (
            "Some description.\n\n"
            "## Acceptance Criteria\n\n"
            "- [ ] Feature works correctly\n"
            "- [ ] Tests pass\n"
            "- [ ] Docs updated\n"
            "- [ ] No regressions\n"
        )
        assert is_epic(_make_issue(body=body)) is False

    def test_ac_bug_not_epic_even_when_body_large(self):
        """Body size must not override the AC-exclusion signal (#1100)."""
        body = (
            "x" * 3000
            + "\n## Acceptance Criteria\n- [ ] one\n- [ ] two\n- [ ] three\n- [ ] four\n"
        )
        assert is_epic(_make_issue(body=body)) is False

    def test_epic_label_overrides_ac_exclusion(self):
        """Explicit 'epic' label beats the AC-exclusion heuristic."""
        body = "## Acceptance Criteria\n- [ ] one\n- [ ] two\n- [ ] three\n- [ ] four\n"
        assert is_epic(_make_issue(body=body, labels=[{"name": "epic"}])) is True

    # -- Sub-issue checklist → epic --

    def test_sub_issue_checklist_triggers_epic(self):
        """A checklist of GitHub issue refs is a true decomposition signal."""
        assert is_epic(_make_issue(body=_SUB_ISSUE_BODY)) is True

    def test_sub_issue_checklist_with_ac_section_is_still_epic(self):
        """Sub-issue refs in checklist override AC-exclusion; it IS an epic."""
        body = (
            "## Acceptance Criteria\n\n"
            "- [ ] #50 Implement feature A\n"
            "- [ ] #51 Implement feature B\n"
        )
        assert is_epic(_make_issue(body=body)) is True

    def test_linked_issue_checklist_triggers_epic(self):
        body = "- [ ] [Fix auth](#101)\n- [ ] [Add tests](#102)\n"
        assert is_epic(_make_issue(body=body)) is True

    # -- Decomposition language → epic (with large body) --

    def test_short_epic_body_with_phase_language_is_epic(self):
        """Explicit 'Phase 1/2/3' language is a semantic epic signal (#1100)."""
        assert is_epic(_make_issue(body=_SHORT_EPIC_BODY)) is False  # body < threshold
        # but with body size + phase language it IS epic:
        large = _SHORT_EPIC_BODY + "x" * _EPIC_BODY_SIZE_MIN
        assert is_epic(_make_issue(body=large)) is True

    def test_decompose_into_language_triggers_epic(self):
        body = "x" * _EPIC_BODY_SIZE_MIN + "\nWe should decompose into three services."
        assert is_epic(_make_issue(body=body)) is True

    def test_split_into_language_triggers_epic(self):
        body = "x" * _EPIC_BODY_SIZE_MIN + "\nWe plan to split into microservices."
        assert is_epic(_make_issue(body=body)) is True

    # -- Helper functions --

    def test_has_sub_issue_checklist_true(self):
        assert _has_sub_issue_checklist("- [ ] #42 Do something") is True
        assert _has_sub_issue_checklist("- [ ] [Do something](#42)") is True

    def test_has_sub_issue_checklist_false(self):
        assert _has_sub_issue_checklist("- [ ] plain task") is False
        assert _has_sub_issue_checklist("- [ ] fix the bug") is False

    def test_is_single_ac_bug_true(self):
        body = "## Acceptance Criteria\n- [ ] task 1\n- [ ] task 2\n"
        assert _is_single_ac_bug(body) is True

    def test_is_single_ac_bug_false_when_sub_issue_present(self):
        body = "## Acceptance Criteria\n- [ ] #10 task\n- [ ] #11 task\n"
        assert _is_single_ac_bug(body) is False

    def test_is_single_ac_bug_false_when_no_ac_section(self):
        assert _is_single_ac_bug("- [ ] plain checklist item\n" * 6) is False


# ── AC-vs-deliverable checklist attribution (issue #1402) ────────────────────


class TestAcSectionExclusion:
    def test_ac_bullets_excluded_from_density(self):
        """Checklist items under an Acceptance Criteria heading do not make an epic."""
        body = (
            "## Summary\nOne unit of work.\n\n"
            "## Acceptance Criteria\n"
            + "\n".join(f"- [ ] behaviour {i}" for i in range(_EPIC_CHECKLIST_MIN + 2))
        )
        assert is_epic(_make_issue(body=body)) is False

    def test_test_plan_bullets_excluded_from_density(self):
        body = "## Test Plan\n" + "\n".join(
            f"- [ ] check {i}" for i in range(_EPIC_CHECKLIST_MIN + 1)
        )
        assert is_epic(_make_issue(body=body)) is False

    def test_deliverable_bullets_still_make_epic(self):
        body = "## Tasks\n" + "\n".join(
            f"- [ ] Build subsystem {i}" for i in range(_EPIC_CHECKLIST_MIN)
        )
        assert is_epic(_make_issue(body=body)) is True

    def test_deliverable_items_skips_ac_section(self):
        body = (
            "## Tasks\n- [ ] Build API\n- [ ] Build UI\n\n"
            "## Acceptance Criteria\n- [ ] works end to end\n"
        )
        assert _deliverable_checklist_items(body) == ["Build API", "Build UI"]

    def test_deliverable_items_bold_heading_ac(self):
        body = "**Acceptance Criteria**\n- [ ] does the thing\n"
        assert _deliverable_checklist_items(body) == []


# ── Dispatcher routing (integration, #149) ──────────────────────────────────


class TestDispatcherRouting:
    def test_planner_profile_registered(self):
        assert "planner" in disp._DEFAULT_PROFILES
        assert disp._DEFAULT_PROFILES["planner"] == "planner-daedalus"

    def test_planner_in_role_tmp_prefix(self):
        assert "planner" in disp._ROLE_TMP_PREFIX
        assert disp._ROLE_TMP_PREFIX["planner"] == "planner"
