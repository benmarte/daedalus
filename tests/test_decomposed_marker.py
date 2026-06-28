"""Tests for the idempotency marker detection logic for epic decomposition.

Verifies that the new ``<!-- daedalus:decomposed[:timestamp] -->`` marker:
  1. is detected in the parent issue body OR in comments
  2. handles whitespace variations (spaces after <!-- and before -->)
  3. handles optional Unix-timestamp suffix (``:1234567890``)
  4. correctly prevents re-running decomposition on already-decomposed epics
     (integration test: zero sub-issues created on second pass)

Unit tests target ``core.iterate.has_decomposed_marker``; integration tests
target ``core.iterate._execute_planner_decompose``.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.iterate import (  # noqa: E402
    _execute_planner_decompose,
    has_decomposed_marker,
)


# ── has_decomposed_marker: unit tests ────────────────────────────────────────

def test_exact_marker_detected():
    assert has_decomposed_marker("<!-- daedalus:decomposed -->") is True


def test_marker_with_timestamp_detected():
    assert has_decomposed_marker("<!-- daedalus:decomposed:1720000000 -->") is True


def test_marker_with_extra_internal_whitespace_detected():
    assert has_decomposed_marker("<!--  daedalus:decomposed  -->") is True
    assert has_decomposed_marker("<!--   daedalus:decomposed:12345   -->") is True


def test_marker_with_no_whitespace_detected():
    assert has_decomposed_marker("<!--daedalus:decomposed-->") is True
    assert has_decomposed_marker("<!--daedalus:decomposed:9-->") is True


def test_marker_case_insensitive():
    assert has_decomposed_marker("<!-- DAEDALUS:DECOMPOSED -->") is True
    assert has_decomposed_marker("<!-- Daedalus:Decomposed:123 -->") is True


def test_marker_embedded_in_body_detected():
    body = (
        "## Epic scope\n"
        "Some description of work.\n\n"
        "<!-- daedalus:decomposed:1720000000 -->\n\n"
        "Leftover notes.\n"
    )
    assert has_decomposed_marker(body) is True


def test_marker_embedded_in_comment_detected():
    body = "daedalus:decomposed\nDaedalus decomposed epic #42."
    # No HTML comment wrapper — should NOT match.
    assert has_decomposed_marker(body) is False


def test_partial_marker_without_closing_not_detected():
    # Unclosed comment — should not match (regex requires closing -->).
    assert has_decomposed_marker("<!-- daedalus:decomposed") is False


def test_similar_but_different_prefix_not_detected():
    assert has_decomposed_marker("<!-- daedalus:sub-issues:[10] -->") is False
    assert has_decomposed_marker("<!-- decomposed -->") is False
    assert has_decomposed_marker("<!-- other:decomposed -->") is False


def test_empty_and_blank_inputs():
    assert has_decomposed_marker("") is False
    assert has_decomposed_marker(None) is False
    assert has_decomposed_marker("   ") is False


def test_marker_in_multiline_with_surrounding_text():
    text = (
        "top\n"
        "middle <!-- daedalus:decomposed:1720000000 --> text after\n"
        "bottom\n"
    )
    assert has_decomposed_marker(text) is True


def test_marker_with_non_numeric_suffix_not_detected():
    # The regex expects an optional decimal timestamp — a non-digit suffix must
    # not match as the "decomposed" bare form (regex requires the closing
    # --> immediately after the colon-or-no-colon segment).
    assert has_decomposed_marker("<!-- daedalus:decomposed:abc -->") is False


# ── Integration: _execute_planner_decompose idempotency ──────────────────────

def _make_card(issue_n: int = 1, body: str = "") -> dict:
    body_with_ref = body if f"#{issue_n}" in body else f"Issue #{issue_n}\n{body}"
    return {"id": "t_test", "title": f"#{issue_n} Epic",
            "body": body_with_ref, "assignee": "planner-daedalus"}


def _make_issue_obj(number: int = 1, title: str = "Epic", body: str = "",
                    labels=None):
    class _Obj:
        def as_dict(self_):
            return {"number": number, "title": title, "body": body,
                    "labels": labels or [], "url": f"https://github.com/x/y/issues/{number}"}
    return _Obj()


def _make_provider(*, issue_obj=None, comments=None):
    prov = mock.MagicMock()
    prov.get_issue.return_value = issue_obj
    prov.get_issue_comments.return_value = comments or []
    prov.create_issue.side_effect = lambda *a, **k: None  # should never fire
    prov.post_issue_comment.return_value = True
    return prov


def test_integ_body_marker_skips_all_sub_issue_creation():
    """When the parent issue BODY carries the new decomposed marker, the
    dispatcher must NOT create any sub-issues AND must short-circuit the
    comment scan."""
    body = "<!-- daedalus:decomposed:1720000000 -->\nOriginal epic body"
    issue = _make_issue_obj(number=5, body=body)
    prov = _make_provider(issue_obj=issue)

    with mock.patch.object(iterate_module(), "kanban") as mk_kanban:
        mk_kanban.complete.return_value = True
        ok = _execute_planner_decompose(
            "slug", _make_card(issue_n=5), "o/r", "PLANNING COMPLETE",
            provider=prov,
        )

    assert ok is True
    prov.create_issue.assert_not_called()
    prov.post_issue_comment.assert_not_called()
    prov.get_issue_comments.assert_not_called()  # short-circuit path
    mk_kanban.complete.assert_called_once()


def iterate_module():
    from core import iterate as _it
    return _it


def test_integ_comment_marker_skips_sub_issue_creation():
    """When the marker appears as a posted comment (legacy or new), no
    sub-issues are created on a re-run of the dispatcher."""
    issue = _make_issue_obj(number=7, body="plain epic body")
    comments = [{"body": "<!-- daedalus:decomposed:1720000000 -->\nDone"}]
    prov = _make_provider(issue_obj=issue, comments=comments)

    with mock.patch.object(iterate_module(), "kanban") as mk_kanban:
        mk_kanban.complete.return_value = True
        ok = _execute_planner_decompose(
            "slug", _make_card(issue_n=7), "o/r", "PLANNING COMPLETE",
            provider=prov,
        )

    assert ok is True
    prov.create_issue.assert_not_called()
    prov.post_issue_comment.assert_not_called()
    mk_kanban.complete.assert_called_once()


def test_integ_legacy_sub_issues_marker_still_skips():
    """Legacy <!-- daedalus:sub-issues:[...] --> marker in comments must
    still be honored (backward compatibility)."""
    issue = _make_issue_obj(number=9, body="plain body")
    comments = [{"body": "<!-- daedalus:sub-issues:[20,21,22] -->\n3 issues"}]
    prov = _make_provider(issue_obj=issue, comments=comments)

    with mock.patch.object(iterate_module(), "kanban") as mk_kanban:
        mk_kanban.complete.return_value = True
        ok = _execute_planner_decompose(
            "slug", _make_card(issue_n=9), "o/r", "PLANNING COMPLETE",
            provider=prov,
        )

    assert ok is True
    prov.create_issue.assert_not_called()


def test_integ_no_marker_triggers_full_decompose():
    """Sanity check: without any marker, decomposition MUST run so we don't
    accidentally skip a fresh epic."""
    issue = _make_issue_obj(number=11, body="- [ ] first\n- [ ] second")
    prov = mock.MagicMock()
    prov.get_issue.return_value = issue
    prov.get_issue_comments.return_value = []
    created = iter([50, 51])
    prov.create_issue.side_effect = lambda *a, **k: next(created, None)
    prov.post_issue_comment.return_value = True
    prov.add_label.return_value = True

    with mock.patch.object(iterate_module(), "kanban") as mk_kanban:
        ok = _execute_planner_decompose(
            "slug", _make_card(issue_n=11), "o/r", "PLANNING COMPLETE",
            provider=prov,
        )

    assert ok is True
    assert prov.create_issue.call_count == 2
    prov.post_issue_comment.assert_called_once()  # marker posted for next run


def test_integ_rerun_after_marker_posted_creates_zero_sub_issues():
    """End-to-end integration: first run posts the marker, second run
    creates zero sub-issues (idempotent)."""
    issue = _make_issue_obj(number=13, body="- [ ] first\n- [ ] second")
    prov = mock.MagicMock()
    prov.get_issue.return_value = issue
    prov.get_issue_comments.return_value = []
    created = iter([60, 61])
    prov.create_issue.side_effect = lambda *a, **k: next(created, None)
    prov.post_issue_comment.return_value = True
    prov.add_label.return_value = True

    # ── Pass 1: fresh epic, marker not yet posted ──
    with mock.patch.object(iterate_module(), "kanban") as mk_kanban:
        ok1 = _execute_planner_decompose(
            "slug", _make_card(issue_n=13), "o/r", "PLANNING COMPLETE",
            provider=prov,
        )

    assert ok1 is True
    assert prov.create_issue.call_count == 2

    # Capture the marker that was posted so we can simulate it being present.
    marker_call_args = prov.post_issue_comment.call_args
    marker_body = marker_call_args[0][1] if marker_call_args else ""
    assert "<!-- daedalus:sub-issues:" in marker_body or "daedalus" in marker_body

    # ── Pass 2: marker now present as a comment — must short-circuit ──
    prov.get_issue_comments.return_value = [{"body": marker_body}]
    prov.create_issue.reset_mock()
    prov.post_issue_comment.reset_mock()

    with mock.patch.object(iterate_module(), "kanban") as mk_kanban:
        mk_kanban.complete.return_value = True
        ok2 = _execute_planner_decompose(
            "slug", _make_card(issue_n=13), "o/r", "PLANNING COMPLETE",
            provider=prov,
        )

    assert ok2 is True
    prov.create_issue.assert_not_called()     # zero sub-issues created
    prov.post_issue_comment.assert_not_called()  # no duplicate marker
    mk_kanban.complete.assert_called_once()
