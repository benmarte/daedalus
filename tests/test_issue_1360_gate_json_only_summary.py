"""Regression tests for issue #1360.

The auto-merge gate (``_role_gate_passed`` / ``_qa_passed_for_issue`` /
``_reviewer_passed_for_issue`` / ``_security_passed_for_issue``) used a raw
``latest_summary.startswith(signal)`` scan. Under the #1170 dual-write protocol
an agent may write a JSON-only summary (a fenced ``daedalus_outcome`` block with
no leading free-text prefix). After ``lstrip()`` such a summary begins with
```` ```json ```` — matching no approval token — so a genuinely passing gate
card blocked auto-merge indefinitely.

The fix makes gate clearance structured-aware: the parsed ``daedalus_outcome``
verdict is authoritative, with the legacy ``startswith`` scan as a fallback for
prefix-only summaries.
"""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.iterate import (  # noqa: E402
    _qa_passed_for_issue,
    _reviewer_passed_for_issue,
    _security_passed_for_issue,
)


def _json_summary(role: str, verdict: str, *, issue: int = 42, pr: int = 43) -> str:
    """A JSON-only structured-outcome summary (no free-text prefix line)."""
    return (
        "```json\n"
        '{"daedalus_outcome": 1, "role": "%s", "verdict": "%s",\n'
        ' "refs": {"issue": %d, "pr": %d},\n'
        ' "evidence": {"owasp": "top10 checked", "findings": "none"}, "note": ""}\n'
        "```" % (role, verdict, issue, pr)
    )


def _card(role: str, summary: str) -> dict:
    return {
        "id": f"{role}-card-1",
        "title": f"#42 {role}: Issue",
        "assignee": f"{role}-daedalus",
        "status": "blocked",
        "latest_summary": summary,
    }


class TestJsonOnlySummaryClearsGate:
    """AC: a JSON-only passing verdict clears QA, reviewer, and security gates."""

    @patch("core.iterate.kanban.show_card")
    @patch("core.iterate.kanban.list_tasks")
    def test_security_json_only_approved_passes(self, mock_list, mock_show):
        summary = _json_summary("security", "approved")
        mock_list.return_value = [_card("security", summary)]
        mock_show.return_value = {"id": "security-card-1", "latest_summary": summary}

        assert _security_passed_for_issue("test-board", 42) is True

    @patch("core.iterate.kanban.show_card")
    @patch("core.iterate.kanban.list_tasks")
    def test_qa_json_only_passed_passes(self, mock_list, mock_show):
        summary = _json_summary("qa", "passed")
        mock_list.return_value = [_card("qa", summary)]
        mock_show.return_value = {"id": "qa-card-1", "latest_summary": summary}

        assert _qa_passed_for_issue("test-board", 42) is True

    @patch("core.iterate.kanban.show_card")
    @patch("core.iterate.kanban.list_tasks")
    def test_reviewer_json_only_approved_passes(self, mock_list, mock_show):
        summary = _json_summary("reviewer", "approved")
        mock_list.return_value = [_card("reviewer", summary)]
        mock_show.return_value = {"id": "reviewer-card-1", "latest_summary": summary}

        assert _reviewer_passed_for_issue("test-board", 42) is True


class TestJsonOnlyFailVerdictStillBlocks:
    """AC: a failing/changes_requested JSON verdict must NOT clear the gate."""

    @patch("core.iterate.kanban.show_card")
    @patch("core.iterate.kanban.list_tasks")
    def test_security_json_only_changes_requested_blocks(self, mock_list, mock_show):
        summary = _json_summary("security", "changes_requested")
        mock_list.return_value = [_card("security", summary)]
        mock_show.return_value = {"id": "security-card-1", "latest_summary": summary}

        assert _security_passed_for_issue("test-board", 42) is False

    @patch("core.iterate.kanban.show_card")
    @patch("core.iterate.kanban.list_tasks")
    def test_qa_json_only_failed_blocks(self, mock_list, mock_show):
        summary = _json_summary("qa", "failed")
        mock_list.return_value = [_card("qa", summary)]
        mock_show.return_value = {"id": "qa-card-1", "latest_summary": summary}

        assert _qa_passed_for_issue("test-board", 42) is False


class TestLegacyPrefixNoRegression:
    """AC: legacy prefix-only summaries still pass (no regression)."""

    @patch("core.iterate.kanban.show_card")
    @patch("core.iterate.kanban.list_tasks")
    def test_security_legacy_prefix_passes(self, mock_list, mock_show):
        summary = "security: cleared — OWASP top10 checked, findings: none"
        mock_list.return_value = [_card("security", summary)]
        mock_show.return_value = {"id": "security-card-1", "latest_summary": summary}

        assert _security_passed_for_issue("test-board", 42) is True

    @patch("core.iterate.kanban.show_card")
    @patch("core.iterate.kanban.list_tasks")
    def test_qa_legacy_prefix_passes(self, mock_list, mock_show):
        summary = "qa-passed: PR #43"
        mock_list.return_value = [_card("qa", summary)]
        mock_show.return_value = {"id": "qa-card-1", "latest_summary": summary}

        assert _qa_passed_for_issue("test-board", 42) is True

    @patch("core.iterate.kanban.show_card")
    @patch("core.iterate.kanban.list_tasks")
    def test_reviewer_mid_string_false_positive_still_blocks(self, mock_list, mock_show):
        # #1125 F1 guard: an approval token mid-string must not clear the gate.
        summary = "changes-requested: approved workaround"
        mock_list.return_value = [_card("reviewer", summary)]
        mock_show.return_value = {"id": "reviewer-card-1", "latest_summary": summary}

        assert _reviewer_passed_for_issue("test-board", 42) is False
