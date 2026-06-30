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
    _CHECKLIST_LINE_RE,
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


# ── Heuristic 3 (removed): body size alone no longer classifies as epic ───────
# Issue #1100: body length is not a reliable signal — detailed bug reports with
# long acceptance criteria sections were misclassified. See TestSemanticEpicDetection.


class TestBodySizeHeuristic:
    def test_at_threshold_not_epic_without_semantic_signals(self):
        # Body at the old threshold with no semantic signals → NOT epic (#1100)
        assert is_epic(_make_issue(body="x" * _EPIC_BODY_SIZE_MIN)) is False

    def test_above_threshold_not_epic_without_semantic_signals(self):
        # Body above the old threshold with no semantic signals → NOT epic (#1100)
        assert is_epic(_make_issue(body="x" * (_EPIC_BODY_SIZE_MIN + 1000))) is False

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
        # Large body alone no longer classifies as epic (#1100)
        assert is_epic({"body": "x" * 3000, "labels": []}) is False

    def test_none_input(self):
        assert is_epic(None) is False

    def test_missing_body_key(self):
        assert is_epic({"labels": []}) is False

    def test_missing_labels_key(self):
        # Large body alone no longer classifies as epic (#1100)
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

    def test_only_size_no_longer_triggers(self):
        # Body size alone is no longer a trigger (#1100)
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
        assert len(body) < len(long_body)


# ── Dispatcher routing (integration, #149) ──────────────────────────────────


class TestDispatcherRouting:
    def test_planner_profile_registered(self):
        assert "planner" in disp._DEFAULT_PROFILES
        assert disp._DEFAULT_PROFILES["planner"] == "planner-daedalus"

    def test_planner_in_role_tmp_prefix(self):
        assert "planner" in disp._ROLE_TMP_PREFIX
        assert disp._ROLE_TMP_PREFIX["planner"] == "planner"


# ── Semantic signal detection (issue #1100) ──────────────────────────────────


class TestSemanticEpicDetection:
    """Body length alone must NOT classify an issue as epic (issue #1100).

    Semantic signals — sub-issue checklist references, decomposition language —
    are required. Detailed bug reports with long acceptance criteria sections
    should route to the validator, not the planner.
    """

    def test_long_body_bug_report_not_epic(self):
        """Long-body bug report with a single AC block is NOT classified as epic."""
        body = (
            "## Bug Report: epic-detection misclassifies detailed bug reports\n\n"
            "### Problem\n"
            "The dispatcher misclassifies large-body bug reports as epics. "
            "This is a detailed description of the bug with full context about what goes wrong, "
            "when it happens, and what the observed vs expected behavior is. "
            "The root cause is that the heuristic uses raw body length as a proxy for epic size. "
            "Bug reports with detailed acceptance criteria, observed/expected sections, "
            "or reproduction steps exceed the length threshold and get routed to the planner "
            "instead of the validator, adding unnecessary latency and burning retry caps.\n\n"
            "### Root Cause Analysis\n"
            "The `is_epic` function in `core/providers/base.py` has three heuristics. "
            "Heuristic 3 fires when `len(body) >= 2000`. A well-written bug report with "
            "detailed reproduction steps, environment notes, observed vs expected sections, "
            "and a full acceptance criteria block routinely exceeds this threshold. "
            "The body length is a weak proxy: it correlates with writing quality, not with "
            "the complexity of the work. An epic should require multiple independent deliverables "
            "that different agents might tackle in parallel, not just a long description.\n\n"
            "### Observed Behavior\n"
            "Issues with bodies exceeding 2000 characters route to the planner for "
            "unnecessary decomposition, even when the issue is a single well-scoped bug fix "
            "with a clear acceptance criteria block and a single PR.\n\n"
            "### Expected Behavior\n"
            "Issues with long bodies but no sub-issue checklists and no multi-phase "
            "language should route to the validator, not the planner. "
            "Body length alone is not a sufficient signal for epic classification.\n\n"
            "### Reproduction Steps\n"
            "1. Create a GitHub issue with a detailed bug report exceeding 2000 characters\n"
            "2. Ensure the body contains only a single '## Acceptance Criteria' section\n"
            "3. Do not include checklist items referencing issue numbers (- [ ] #N)\n"
            "4. Do not include decomposition language\n"
            "5. Trigger the dispatcher and observe it routes to the planner\n\n"
            "### Environment\n"
            "- Daedalus dispatcher version: current dev branch\n"
            "- Affected module: core/providers/base.py, function is_epic\n"
            "- Related: daedalus-epic-detection-hazard memory entry\n\n"
            "## Acceptance Criteria\n"
            "- [ ] Epic detection uses semantic signals instead of body length\n"
            "- [ ] Long-body bug reports route to the validator\n"
            "- [ ] Tests cover this scenario\n"
        )
        assert len(body) > _EPIC_BODY_SIZE_MIN, (
            f"body must exceed {_EPIC_BODY_SIZE_MIN} chars for this test"
        )
        assert is_epic(_make_issue(body=body)) is False

    def test_short_body_with_decomposition_language_is_epic(self):
        """Short body containing Phase 1/2/3 decomposition language IS an epic."""
        body = (
            "## Auth Overhaul Epic\n\n"
            "This epic tracks the authentication system overhaul.\n\n"
            "Phase 1: OAuth integration and provider setup\n"
            "Phase 2: Session management redesign\n"
            "Phase 3: Security audit and hardening\n"
        )
        assert len(body) < _EPIC_BODY_SIZE_MIN, (
            f"body must be under {_EPIC_BODY_SIZE_MIN} chars for this test"
        )
        assert is_epic(_make_issue(body=body)) is True

    def test_sub_issue_checklist_is_epic(self):
        """Body with sub-issue checklist references (- [ ] #N) IS an epic."""
        body = (
            "This epic tracks the following sub-issues:\n\n"
            "- [ ] #201 Implement OAuth\n"
            "- [ ] #202 Update session handling\n"
        )
        # Fewer than _EPIC_CHECKLIST_MIN items so checklist density alone does not fire
        assert len(_CHECKLIST_LINE_RE.findall(body)) < _EPIC_CHECKLIST_MIN
        assert is_epic(_make_issue(body=body)) is True

    def test_decompose_into_language_is_epic(self):
        """Body with 'decompose into' language IS classified as an epic."""
        body = "We should decompose into three independent sub-issues for this work."
        assert is_epic(_make_issue(body=body)) is True

    def test_long_body_with_sub_issue_checklist_is_epic(self):
        """Long body that also has sub-issue checklist references remains an epic."""
        body = "background context " * 120 + "\n- [ ] #201 sub-task one\n"
        assert len(body) > _EPIC_BODY_SIZE_MIN
        assert is_epic(_make_issue(body=body)) is True
