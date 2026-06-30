"""Unit tests for close_issue_tasks word-boundary matching and body fallback.

Covers:
  - #957 does NOT match #9571 or #9570 (word-boundary regex, issue #957)
  - Body/handoff fallback: cards without #N in title are matched via body
  - Idempotency: re-running does not complete already-terminal cards
"""

import sys
import re
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import kanban
from conftest import check


# ── Word-boundary matching ────────────────────────────────────────────────────


def test_close_issue_tasks_word_boundary_no_false_positive():
    """#957 must NOT match #9571 or #9570 — word-boundary regex guards this."""
    pattern = re.compile(r"(?<!\d)#957(?!\d)")
    # These should match exactly #957 (not #9571, #9570, etc.)
    check("#957 matches alone", bool(pattern.search("#957")))
    check("#957 at start of title", bool(pattern.search("#957 some text")))
    check("#957 at end of title", bool(pattern.search("fix #957")))
    check("#957 surrounded", bool(pattern.search("fix #957 bug")))
    # These must NOT match
    check("#9571 rejected", not pattern.search("#9571"))
    check("#9570 rejected", not pattern.search("#9570"))
    check("#957 in #9571", not pattern.search("fix #9571 bug"))
    check("#957 at end of #9570", not pattern.search("fix bug #9570"))


def test_close_issue_tasks_word_boundary_close():
    """close_issue_tasks completes cards with exact #N, not #Nxx."""
    tasks = [
        {"id": "t_exact", "title": "#957 exact match", "status": "todo"},
        {"id": "t_longer", "title": "#9571 should not match", "status": "todo"},
        {"id": "t_prefix", "title": "fix #957 bug", "status": "ready"},
        {"id": "t_suffix", "title": "fix #9570 bug", "status": "ready"},
    ]
    completed = []
    with mock.patch.object(kanban, "list_tasks", return_value=tasks):
        with mock.patch.object(kanban, "complete", side_effect=lambda s, t, **kw: completed.append(t) or True):
            result = kanban.close_issue_tasks("slug", 957)
    check("t_exact completed", "t_exact" in result)
    check("t_prefix completed", "t_prefix" in result)
    check("t_longer NOT completed", "t_longer" not in result)
    check("t_suffix NOT completed", "t_suffix" not in result)


# ── Body/handoff fallback ─────────────────────────────────────────────────────


def test_close_issue_tasks_body_fallback():
    """Cards without #N in title are matched via body/handoff text."""
    tasks = [
        # Title has no #700, body references #700 → should match
        {"id": "t_body", "title": "Developer card", "body": "Issue #700: fix bug", "status": "todo"},
        # Title has #700 → should match
        {"id": "t_title", "title": "#700: fix bug", "body": "", "status": "todo"},
        # Neither has #700 → should NOT match
        {"id": "t_other", "title": "Some other card", "body": "nothing here", "status": "todo"},
    ]
    with mock.patch.object(kanban, "list_tasks", return_value=tasks):
        with mock.patch.object(kanban, "complete", return_value=True):
            result = kanban.close_issue_tasks("slug", 700)
    check("body-only card matched", "t_body" in result)
    check("title card matched", "t_title" in result)
    check("unrelated card skipped", "t_other" not in result)


# ── Idempotency ───────────────────────────────────────────────────────────────


def test_close_issue_tasks_skips_terminal_states():
    """close_issue_tasks skips already-terminal cards (done/cancelled)."""
    tasks = [
        {"id": "t_done", "title": "#957 done card", "status": "done"},
        {"id": "t_cancelled", "title": "#957 cancelled", "status": "cancelled"},
        {"id": "t_ready", "title": "#957 still ready", "status": "ready"},
        {"id": "t_blocked", "title": "#957 blocked", "status": "blocked"},
    ]
    with mock.patch.object(kanban, "list_tasks", return_value=tasks):
        with mock.patch.object(kanban, "complete", return_value=True):
            result = kanban.close_issue_tasks("slug", 957)
    check("done card skipped", "t_done" not in result)
    check("cancelled card skipped", "t_cancelled" not in result)
    check("ready card completed", "t_ready" in result)
    check("blocked card completed", "t_blocked" in result)


# ── Runner ────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    print("close_issue_tasks word-boundary and body-fallback tests")
    test_close_issue_tasks_word_boundary_no_false_positive()
    test_close_issue_tasks_word_boundary_close()
    test_close_issue_tasks_body_fallback()
    test_close_issue_tasks_skips_terminal_states()
    print("all passed")
