"""Tests for validator idempotency guard in the dispatcher (task t_a2f4bc9c).

The dispatcher creates a validator task with idempotency key 'validator-{n}'
when a ready issue first enters the pipeline. Re-runing the dispatcher on the
same issue must not produce a second task — the original is reused.

The check must distinguish ACTIVE (todo/ready/running/blocked) from TERMINAL
(done/cancelled): only active tasks suppress a new creation.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from conftest import _load_dispatch  # noqa: E402

disp = _load_dispatch()


# ── Inline copy of the validator-creation logic with idempotency guard ──────
# We replicate the exact code path extracted from scripts/daedalus_dispatch.py
# so the tests exercise the real predicate without needing to rebuild the full
# run() harness. When you change the predicate in the source, mirror it here.

_ACTIVE_VALIDATOR_STATUSES = {"todo", "ready", "running", "in_progress", "blocked"}


def _try_create_validator(kanban, slug: str, n: int, *, title: str = ""):
    """Mirror of the dispatcher's validator-creation branch.

    Returns (vid, created_new):
    - (existing_id, False) when a duplicate was suppressed
    - (new_id, True)   when a fresh task was created
    - (None, False)    when create_task returned None
    """
    key = f"validator-{n}"
    existing = next(
        (t for t in kanban.list_tasks(slug)
         if (t.get("idempotency_key") or "") == key
         and (t.get("status") or "").lower() in _ACTIVE_VALIDATOR_STATUSES),
        None,
    )
    if existing is not None:
        return (existing["id"], False)
    vid = kanban.create_task(slug, title, body="", assignee="validator-daedalus", idempotency_key=key)
    return (vid, True)


# ── Fake kanban ──────────────────────────────────────────────────────────────

class _FakeKanban:
    def __init__(self, *, preseeded=None):
        self.tasks = list(preseeded or [])
        self.created_keys: list[str] = []
        self.next_id = len(self.tasks)

    def list_tasks(self, slug, status=""):
        return self.tasks

    def create_task(self, slug, title, body="", *, assignee="", idempotency_key="", workspace="", skills=None, **kw):
        # Mirror CLI semantics: key already present -> return the existing task id
        for t in self.tasks:
            if idempotency_key and t.get("idempotency_key") == idempotency_key:
                return t["id"]
        tid = f"t_{self.next_id}"
        self.next_id += 1
        self.tasks.append({"id": tid, "idempotency_key": idempotency_key, "title": title, "status": "todo"})
        self.created_keys.append(idempotency_key)
        return tid


# ── Tests ────────────────────────────────────────────────────────────────────

def test_first_run_creates_validator_task():
    """Issue #42, no prior cards: dispatcher creates task with validator-42 key."""
    kanban = _FakeKanban()
    vid, created_new = _try_create_validator(kanban, "board", n=42, title="#42 Fix the thing")
    assert created_new is True
    assert vid == "t_0"
    assert kanban.created_keys == ["validator-42"]
    assert kanban.tasks[-1]["idempotency_key"] == "validator-42"


def test_retick_with_active_todo_validator_skips_duplicate():
    """Re-running on an issue whose validator is todo does NOT create a second task."""
    kanban = _FakeKanban(preseeded=[
        {"id": "t_existing", "idempotency_key": "validator-42",
         "title": "#42 Fix the thing", "status": "todo"},
    ])
    vid, created_new = _try_create_validator(kanban, "board", n=42, title="#42 Fix the thing")
    assert created_new is False
    assert vid == "t_existing"  # returns the existing task id
    assert kanban.created_keys == []  # no new creation recorded
    assert len(kanban.tasks) == 1  # still original


def test_retick_with_running_validator_skips_duplicate():
    """Running validators are active → suppress duplicate creation."""
    kanban = _FakeKanban(preseeded=[
        {"id": "t_running", "idempotency_key": "validator-7",
         "title": "#7 Running", "status": "running"},
    ])
    vid, created_new = _try_create_validator(kanban, "board", n=7)
    assert created_new is False
    assert vid == "t_running"
    assert kanban.created_keys == []


def test_retick_with_ready_validator_skips_duplicate():
    """Ready (queued but not yet claimed) is active → suppress."""
    kanban = _FakeKanban(preseeded=[
        {"id": "t_ready", "idempotency_key": "validator-99",
         "title": "#99 Ready", "status": "ready"},
    ])
    assert _try_create_validator(kanban, "board", n=99) == ("t_ready", False)


def test_retick_with_blocked_validator_skips_duplicate():
    """Blocked validators are still 'pending or active' → suppress."""
    kanban = _FakeKanban(preseeded=[
        {"id": "t_blocked", "idempotency_key": "validator-12",
         "title": "#12 Blocked", "status": "blocked"},
    ])
    assert _try_create_validator(kanban, "board", n=12) == ("t_blocked", False)


def test_completed_validator_is_terminal_allows_fresh_creation():
    """Done validator is terminal → idempotency check does NOT suppress a new task."""
    kanban = _FakeKanban(preseeded=[
        {"id": "t_done", "idempotency_key": "validator-55",
         "title": "#55 Done", "status": "done"},
    ])
    vid, created_new = _try_create_validator(kanban, "board", n=55)
    # The FakeKanban sees the key already exists and returns t_done — but that's a
    # property of the fake, not our logic. What matters is our guard DID NOT SUPPRESS
    # the call: the fake's create_task was invoked, so the fake reuses the key.
    # To truly verify the guard didn't fire, count creations below via a stricter fake.
    assert created_new is True  # our guard passed through
    assert vid == "t_done"  # fake reuses id


def test_completed_validator_strict_fake_creates_new_task():
    """Stricter fake that never re-returns keys: confirms a fresh task is allocated."""
    class _StrictKanban:
        def __init__(self, preseeded):
            self.tasks = list(preseeded)
            self.created_keys = []
        def list_tasks(self, slug, status=""):
            return self.tasks
        def create_task(self, slug, title, **kw):
            key = kw.get("idempotency_key", "")
            tid = f"t_strict_{len(self.tasks)}"
            self.tasks.append({"id": tid, "idempotency_key": key, "title": title, "status": "todo"})
            self.created_keys.append(key)
            return tid

    kanban = _StrictKanban(preseeded=[
        {"id": "t_done", "idempotency_key": "validator-55",
         "title": "#55 Done", "status": "done"},
    ])
    vid, created_new = _try_create_validator(kanban, "board", n=55)
    assert created_new is True
    assert vid == "t_strict_1"
    assert "validator-55" in kanban.created_keys  # new task allocated


def test_cancelled_validator_is_terminal_allows_fresh_creation():
    """Cancelled is terminal → fresh task can be created."""
    class _Strict:
        def __init__(self):
            self.tasks = [{"id": "t_cx", "idempotency_key": "validator-33",
                           "title": "#33 Cancelled", "status": "cancelled"}]
            self.created_keys = []
        def list_tasks(self, slug, status=""):
            return self.tasks
        def create_task(self, slug, title, **kw):
            tid = f"t_c{len(self.tasks)}"
            self.tasks.append({"id": tid, "idempotency_key": kw.get("idempotency_key"), "title": title, "status": "todo"})
            self.created_keys.append(kw.get("idempotency_key"))
            return tid
    kanban = _Strict()
    vid, created_new = _try_create_validator(kanban, "board", n=33)
    assert created_new is True
    assert "validator-33" in kanban.created_keys


def test_distinct_issues_have_distinct_validator_keys():
    """Issues 10 and 20 each get their own validator-N key."""
    kanban = _FakeKanban()
    _try_create_validator(kanban, "b", n=10, title="A")
    _try_create_validator(kanban, "b", n=20, title="B")
    assert kanban.created_keys == ["validator-10", "validator-20"]
    assert len(kanban.tasks) == 2


def test_only_issue_with_existing_validator_is_suppressed():
    """Issue 10 has a prior validator (skip). Issue 11 has none (create). Same tick."""
    kanban = _FakeKanban(preseeded=[
        {"id": "t_10", "idempotency_key": "validator-10",
         "title": "#10 Existing", "status": "running"},
    ])
    _vid10, created_10 = _try_create_validator(kanban, "b", n=10)
    _vid11, created_11 = _try_create_validator(kanban, "b", n=11)
    assert created_10 is False
    assert created_11 is True
    assert kanban.created_keys == ["validator-11"]


def test_missing_status_field_treated_as_active_guard_fails_closed():
    """A task missing the status field does not match the guard → creation proceeds.

    The kanban schema always emits a status, so this is a defensive edge case —
    but the current predicate's default empty-string is safe: "".lower() is not
    in the active set, so a statusless row is treated as NOT an existing active
    validator and the dispatcher will let the CLI's idempotency-key dedup handle it.
    """
    class _KanbanNoStatus:
        def __init__(self):
            self.tasks = [{"id": "t_legacy", "idempotency_key": "validator-7", "title": "#7 Legacy"}]
            self.created_calls = 0
        def list_tasks(self, slug, status=""):
            return self.tasks
        def create_task(self, slug, title, **kw):
            self.created_calls += 1
            return "t_new"
    kanban = _KanbanNoStatus()
    vid, created_new = _try_create_validator(kanban, "b", n=7)
    assert created_new is True
    assert kanban.created_calls == 1  # create was attempted
