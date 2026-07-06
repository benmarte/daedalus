"""Issue #891: backward compat for old decomposed marker + concurrent lock.

Verifies that:
  1. has_decomposed_marker() detects BOTH old format (<!-- daedalus:sub-issues:[...] -->)
     AND new format (<!-- daedalus:decomposed[:timestamp] -->)
  2. Concurrent decompose operations are prevented via locking
"""
import json
import time
from pathlib import Path
from unittest import mock

from core.iterate import has_decomposed_marker, _execute_planner_decompose
from core import kanban as _core_kanban
from tests.conftest import kanban_as


def iterate_module():
    from core import iterate
    return iterate


# ── Old marker format detection ──────────────────────────────────────────────

class TestOldMarkerFormatDetection:
    """Old format: <!-- daedalus:sub-issues:[N,...] -->"""

    def test_old_format_basic(self):
        assert has_decomposed_marker("<!-- daedalus:sub-issues:[10] -->") is True

    def test_old_format_multiple_issues(self):
        assert has_decomposed_marker("<!-- daedalus:sub-issues:[20,21,22] -->") is True

    def test_old_format_in_comment(self):
        body = "Some text\n<!-- daedalus:sub-issues:[1,2,3] -->\nMore text"
        assert has_decomposed_marker(body) is True

    def test_old_format_case_insensitive(self):
        assert has_decomposed_marker("<!-- DAEDALUS:SUB-ISSUES:[5] -->") is True
        assert has_decomposed_marker("<!-- Daedalus:Sub-Issues:[5] -->") is True

    def test_old_format_whitespace_tolerance(self):
        assert has_decomposed_marker("<!--daedalus:sub-issues:[1]-->") is True
        assert has_decomposed_marker("<!--  daedalus:sub-issues:[1]  -->") is True

    def test_old_format_empty_array(self):
        assert has_decomposed_marker("<!-- daedalus:sub-issues:[] -->") is True


# ── New marker format detection ──────────────────────────────────────────────

class TestNewMarkerFormatDetection:
    """New format: <!-- daedalus:decomposed[:timestamp] -->"""

    def test_new_format_no_timestamp(self):
        assert has_decomposed_marker("<!-- daedalus:decomposed -->") is True

    def test_new_format_with_timestamp(self):
        assert has_decomposed_marker("<!-- daedalus:decomposed:1720000000 -->") is True

    def test_new_format_case_insensitive(self):
        assert has_decomposed_marker("<!-- DAEDALUS:DECOMPOSED -->") is True


# ── Negative cases ───────────────────────────────────────────────────────────

class TestNegativeCases:
    """Should NOT match unrelated markers."""

    def test_empty_input(self):
        assert has_decomposed_marker("") is False
        assert has_decomposed_marker(None) is False

    def test_no_marker(self):
        assert has_decomposed_marker("just regular text") is False

    def test_similar_but_wrong_format(self):
        assert has_decomposed_marker("<!-- decomposed -->") is False
        assert has_decomposed_marker("<!-- other:sub-issues:[1] -->") is False


# ── Integration: old marker prevents re-decompose ────────────────────────────

def _make_card(issue_n=1):
    return {
        "id": "t_lock_test", "title": "Epic",
        "body": f"Issue #{issue_n}", "assignee": "planner-daedalus",
    }


def _make_issue_obj(number=1, body=""):
    class _Obj:
        def as_dict(self_):
            return {
                "number": number, "title": "Epic", "body": body,
                "labels": [], "url": f"https://github.com/x/y/issues/{number}",
            }
    return _Obj()


def _make_provider(*, issue_obj=None, comments=None):
    prov = mock.MagicMock()
    prov.get_issue.return_value = issue_obj
    prov.get_issue_comments.return_value = comments or []
    prov.create_issue.side_effect = lambda *a, **k: None
    prov.post_issue_comment.return_value = True
    return prov


def test_old_marker_in_body_skips_decompose():
    """Old format in issue body must skip decompose (backward compat)."""
    body = "<!-- daedalus:sub-issues:[50,51] -->\nOriginal epic body"
    issue = _make_issue_obj(number=5, body=body)
    prov = _make_provider(issue_obj=issue)

    mk_kanban = mock.MagicMock()
    mk_kanban.complete.return_value = True
    with kanban_as(_core_kanban, mk_kanban):
        ok = _execute_planner_decompose(
            "slug", _make_card(5), "o/r", "PLANNING COMPLETE",
            provider=prov,
        )

    assert ok is True
    prov.create_issue.assert_not_called()
    prov.post_issue_comment.assert_not_called()


def test_old_marker_in_comment_skips_decompose():
    """Old format in comment must skip decompose (backward compat)."""
    issue = _make_issue_obj(number=7, body="plain epic body")
    comments = [{"body": "<!-- daedalus:sub-issues:[20,21,22] -->\n3 issues"}]
    prov = _make_provider(issue_obj=issue, comments=comments)

    mk_kanban = mock.MagicMock()
    mk_kanban.complete.return_value = True
    with kanban_as(_core_kanban, mk_kanban):
        ok = _execute_planner_decompose(
            "slug", _make_card(7), "o/r", "PLANNING COMPLETE",
            provider=prov,
        )

    assert ok is True
    prov.create_issue.assert_not_called()


# ── Concurrent decompose lock ────────────────────────────────────────────────

def test_concurrent_decompose_lock_prevents_second_decompose(tmp_path):
    """Two concurrent ticks: the second must see lock and skip."""
    workdir = Path(tmp_path)
    lock_file = workdir / ".hermes" / "decompose-lock.json"

    # Simulate lock acquired by another tick 2 seconds ago (= still active)
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    lock_data = {
        "pid": 99999,
        "issue_n": 42,
        "acquired_at": int(time.time()) - 2,
    }
    lock_file.write_text(json.dumps(lock_data))

    issue = _make_issue_obj(number=42, body="plain body")
    prov = _make_provider(issue_obj=issue)

    mk_kanban = mock.MagicMock()
    mk_kanban.complete.return_value = True
    with kanban_as(_core_kanban, mk_kanban):
        ok = _execute_planner_decompose(
            "slug", _make_card(42), "o/r", "PLANNING COMPLETE",
            provider=prov, workdir=str(workdir),
        )

    assert ok is True
    prov.create_issue.assert_not_called()  # skipped due to lock
    prov.post_issue_comment.assert_not_called()


def test_stale_lock_is_ignored(tmp_path):
    """A lock older than 60 seconds is stale and should be ignored."""
    workdir = Path(tmp_path)
    lock_file = workdir / ".hermes" / "decompose-lock.json"
    lock_file.parent.mkdir(parents=True, exist_ok=True)

    # Stale lock: acquired 120 seconds ago
    lock_data = {
        "pid": 99999,
        "issue_n": 43,
        "acquired_at": int(time.time()) - 120,
    }
    lock_file.write_text(json.dumps(lock_data))

    issue = _make_issue_obj(number=43, body="- [ ] first\n- [ ] second")
    prov = mock.MagicMock()
    prov.get_issue.return_value = issue
    prov.get_issue_comments.return_value = []
    created = iter([60, 61])
    prov.create_issue.side_effect = lambda *a, **k: next(created, None)
    prov.post_issue_comment.return_value = True
    prov.add_label.return_value = True

    mk_kanban = mock.MagicMock()
    mk_kanban.complete.return_value = True
    with kanban_as(_core_kanban, mk_kanban):
        ok = _execute_planner_decompose(
            "slug", _make_card(43), "o/r", "PLANNING COMPLETE",
            provider=prov, workdir=str(workdir),
        )

    assert ok is True
    # Should proceed since lock was stale
    assert prov.create_issue.call_count == 2
