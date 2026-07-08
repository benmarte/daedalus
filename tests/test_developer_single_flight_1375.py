"""Tests for the developer single-flight guard (#1375).

A false-failure re-spawn (crash-retry unblock, or a duplicate developer card) must
NOT fire a second developer delegate onto the same branch/worktree while the first
is still live — two agents editing one `.worktrees/dev-<n>` checkout is a data-loss
hazard. ``_developer_delegate_in_flight`` detects a live delegate and
``direct_dispatch`` suppresses the second spawn.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.dispatch import direct_dispatch as dd  # noqa: E402

_DELEG_BODY = (
    "Implement issue benmarte/daedalus#42\n"
    + dd._DELEGATION_MARKER
    + " CLAUDE CODE:\n  ... spawn delegate.sh ...\n"
)

_EXEC_ON = {
    "direct_delegate": True,
    "coding_agent": "claude-code",
    "coding_agent_cmd": "claude --dangerously-skip-permissions -p",
}


def _wire(monkeypatch, tasks, cards, claimed):
    monkeypatch.setattr(dd.kanban, "list_tasks", lambda slug, status="": tasks)
    monkeypatch.setattr(dd.kanban, "show_card", lambda slug, cid: cards.get(cid))
    monkeypatch.setattr(dd.kanban, "claim", lambda slug, cid, **k: claimed.append(cid) or True)


# ── unit: the guard predicate ─────────────────────────────────────────────────

def test_guard_true_when_process_references_branch():
    """A live process whose command line references the feature branch counts as
    an in-flight developer."""
    ps = ["bash daedalus-delegate.sh --branch fix/issue-42 --role developer"]
    assert dd._developer_delegate_in_flight("b", 42, "fix/issue-42", "t1", ps_lines=ps)


def test_guard_true_when_process_references_worktree_path():
    """A live inner agent running inside the deterministic worktree counts too."""
    ps = ["claude -p  (cwd=/repos/x/.worktrees/dev-42)"]
    assert dd._developer_delegate_in_flight("b", 42, "fix/issue-42", "t1", ps_lines=ps)


def test_guard_true_when_other_developer_card_running(monkeypatch):
    """A second developer card for the same issue already ``running`` is a
    board-level double-dispatch and is suppressed."""
    other = {"id": "t9", "assignee": "developer-daedalus",
             "title": "#42 dev", "status": "running"}
    monkeypatch.setattr(dd.kanban, "list_tasks", lambda slug, status="": [other])
    assert dd._developer_delegate_in_flight(
        "b", 42, "fix/issue-42", "t1", ps_lines=[],
        developer_profile="developer-daedalus",
    )


def test_guard_false_when_nothing_in_flight(monkeypatch):
    """No live process, no other running dev card → not in flight (spawn allowed)."""
    monkeypatch.setattr(dd.kanban, "list_tasks", lambda slug, status="": [])
    assert not dd._developer_delegate_in_flight(
        "b", 42, "fix/issue-42", "t1", ps_lines=[],
        developer_profile="developer-daedalus",
    )


def test_guard_ignores_unrelated_branch():
    """A live delegate for a DIFFERENT issue must not suppress this one."""
    ps = ["bash daedalus-delegate.sh --branch fix/issue-99 --role developer"]
    assert not dd._developer_delegate_in_flight("b", 42, "fix/issue-42", "t1", ps_lines=ps)


def test_guard_ignores_same_card():
    """The card being dispatched is not itself counted as an in-flight rival."""
    same = {"id": "t1", "assignee": "developer-daedalus",
            "title": "#42 dev", "status": "running"}
    assert not dd._developer_delegate_in_flight(
        "b", 42, "fix/issue-42", "t1", ps_lines=[], kanban_mod=_FakeKanban([same]),
        developer_profile="developer-daedalus",
    )


class _FakeKanban:
    def __init__(self, tasks):
        self._tasks = tasks

    def list_tasks(self, slug, status=""):
        return self._tasks


# ── integration: direct_dispatch suppresses the second spawn ──────────────────

def test_direct_dispatch_suppresses_second_developer_when_in_flight(monkeypatch):
    """test (b): with a live developer delegate for #42, direct_dispatch does NOT
    claim or spawn a second developer for the same issue."""
    claimed, spawned = [], []
    tasks = [{"id": "t1", "assignee": "developer-daedalus", "title": "#42 dev"}]
    cards = {"t1": {"id": "t1", "title": "#42 dev", "body": _DELEG_BODY}}
    _wire(monkeypatch, tasks, cards, claimed)
    # Simulate a live delegate on the branch.
    monkeypatch.setattr(
        dd, "_running_processes",
        lambda: ["bash daedalus-delegate.sh --branch fix/issue-42 --role developer"],
    )
    resolved = {"execution": _EXEC_ON, "workdir": "/repos/x", "vcs": {"target_branch": "main"}}
    n = dd.direct_dispatch("b", resolved, spawn=lambda **k: spawned.append(k))
    assert n == 0, "second developer must be suppressed"
    assert claimed == [], "must not claim the card when a delegate is already live"
    assert spawned == [], "must not spawn a concurrent developer"


def test_direct_dispatch_spawns_developer_when_not_in_flight(monkeypatch):
    """Control: with no live delegate, the developer is dispatched normally and
    receives the PR grace window."""
    claimed, spawned = [], []
    tasks = [{"id": "t1", "assignee": "developer-daedalus", "title": "#42 dev"}]
    cards = {"t1": {"id": "t1", "title": "#42 dev", "body": _DELEG_BODY}}
    _wire(monkeypatch, tasks, cards, claimed)
    monkeypatch.setattr(dd, "_running_processes", lambda: [])
    resolved = {
        "execution": _EXEC_ON, "workdir": "/repos/x",
        "vcs": {"target_branch": "main"},
        "pipeline": {"developer_pr_grace_secs": 90},
    }
    n = dd.direct_dispatch("b", resolved, spawn=lambda **k: spawned.append(k))
    assert n == 1 and claimed == ["t1"]
    assert spawned[0]["role"] == "developer"
    assert spawned[0]["pr_grace_secs"] == 90, "developer must get the configured grace window"


def test_non_developer_role_gets_no_grace_window(monkeypatch):
    """Grace is developer-only: a review role is spawned with pr_grace_secs=0."""
    claimed, spawned = [], []
    tasks = [{"id": "t1", "assignee": "validator-daedalus", "title": "#42 v"}]
    cards = {"t1": {"id": "t1", "title": "#42 v", "body": _DELEG_BODY}}
    _wire(monkeypatch, tasks, cards, claimed)
    monkeypatch.setattr(dd, "_running_processes", lambda: [])
    n = dd.direct_dispatch("b", {"execution": _EXEC_ON}, spawn=lambda **k: spawned.append(k))
    assert n == 1 and spawned[0]["role"] == "validator"
    assert spawned[0]["pr_grace_secs"] == 0
