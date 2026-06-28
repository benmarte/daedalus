"""Unit tests for sub-issue visibility on the project board after planner decomposition.

Epic #915: After the planner decomposes an epic, each sub-issue must be enrolled
onto the configured project board. The GitHub provider's ``board_set_status``
auto-enrolls via ``_board_add_item`` when the issue isn't already on the board,
so calling it from ``_execute_planner_decompose_inner`` is the integration point.

Tests verify:
1. ``board_set_status`` is invoked for every created sub-issue (board configured).
2. Tier-0 sub-issues (no dependencies) get "Ready" status; dependent sub-issues get "Todo".
3. When ``board_configured`` is False, no board calls are made.
4. A failing ``board_set_status`` does not abort the decomposition — other sub-issues still get processed.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import _load_dispatch  # noqa: E402
from core import iterate  # noqa: E402
from core.iterate import _execute_planner_decompose  # noqa: E402


def _make_provider(*, board_configured=True, created_numbers=None, board_status_ret=None):
    """Build a minimal mock VCS provider.

    *board_status_ret* may be a bool, or a list of bools/Exceptions consumed in
    order via ``side_effect`` for per-call failure injection.
    """
    prov = mock.MagicMock()
    prov.board_configured.return_value = board_configured

    # Parent issue
    parent = mock.MagicMock()
    parent.as_dict.return_value = {
        "title": "Epic: multi-step overhaul",
        "body": (
            "- [ ] step A\n"
            "- [ ] step B\n"
            "- [ ] step C\n"
        ),
        "labels": ["epic"],
    }
    prov.get_issue.return_value = parent
    prov.get_issue_comments.return_value = []
    prov.add_label.return_value = True
    prov.post_issue_comment.return_value = True

    # Sub-issue numbers
    _created = iter(created_numbers or [101, 102, 103])
    prov.create_issue.side_effect = lambda *a, **kw: next(_created, None)

    # board_set_status behaviour: list/side_effect for per-call control, else uniform return.
    if isinstance(board_status_ret, list):
        prov.board_set_status.side_effect = board_status_ret
    else:
        prov.board_set_status.return_value = board_status_ret

    # Fake sub-issue returned by get_issue for triage card creation
    def fake_get_issue(n):
        if n == parent:
            return parent
        sub = mock.MagicMock()
        sub.as_dict.return_value = {
            "title": f"sub-issue #{n}",
            "body": f"Depends on: none (#{n})\n## Scope\nstep {n}\n",
        }
        return sub

    prov.get_issue.side_effect = fake_get_issue
    return prov


def _make_card(tid="t_subvis_001", issue_number=50):
    return {"id": tid, "body": f"See issue #{issue_number}", "status": "running"}


def _patch_kanban():
    """Patch kanban.create_triage / decompose / complete as no-ops."""
    return mock.patch.multiple(
        iterate,
        kanban=mock.MagicMock(
            create_triage=mock.MagicMock(return_value="t_triage_xxx"),
            decompose=mock.MagicMock(return_value=True),
            complete=mock.MagicMock(return_value=True),
        ),
    )


class TestSubIssueBoardEnrollment:
    """Sub-issues must be added to the project board right after creation."""

    def test_board_set_status_called_for_each_sub_issue(self, tmp_path):
        prov = _make_provider(board_configured=True, created_numbers=[201, 202, 203])
        with _patch_kanban():
            ok = _execute_planner_decompose(
                "slug", _make_card(issue_number=50), "org/repo",
                "PLANNING COMPLETE: done",
                workdir=str(tmp_path), dry_run=False, provider=prov,
            )

        assert ok is True
        assert prov.create_issue.call_count == 3
        # board_set_status called once per sub-issue with correct tier-based status.
        assert prov.board_set_status.call_count == 3
        calls = prov.board_set_status.call_args_list
        assert [c.args[0] for c in calls] == [201, 202, 203]
        # First sub-issue (no deps) → "Ready"; subsequent (have deps) → "Todo"
        assert calls[0].args[1] == "Ready"
        assert calls[1].args[1] == "Todo"
        assert calls[2].args[1] == "Todo"

    def test_no_board_call_when_board_not_configured(self, tmp_path):
        prov = _make_provider(board_configured=False, created_numbers=[301])
        with _patch_kanban():
            ok = _execute_planner_decompose(
                "slug", _make_card(issue_number=60), "org/repo",
                "PLANNING COMPLETE: done",
                workdir=str(tmp_path), dry_run=False, provider=prov,
            )

        assert ok is True
        prov.board_set_status.assert_not_called()

    def test_board_set_status_failure_does_not_abort_decomposition(self, tmp_path):
        """If enrolling sub-issue #1 fails, sub-issues #2 and #3 must still be processed.

        board_set_status returning False (or raising) is non-fatal per sub-issue.
        """
        prov = _make_provider(
            board_configured=True,
            created_numbers=[401, 402, 403],
            board_status_ret=[False, True, True],  # first fails, others succeed
        )
        with _patch_kanban():
            ok = _execute_planner_decompose(
                "slug", _make_card(issue_number=70), "org/repo",
                "PLANNING COMPLETE: done",
                workdir=str(tmp_path), dry_run=False, provider=prov,
            )

        assert ok is True
        # All three sub-issues were attempted, regardless of the first failure
        assert prov.board_set_status.call_count == 3
        # All three sub-issues were still created
        assert prov.create_issue.call_count == 3
