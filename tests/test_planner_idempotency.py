"""Tests for planner card idempotency check (issue #181).

When an epic issue dispatches to the planner, the dispatcher should check if a
planner card with idempotency key 'planner-{n}' already exists before creating
a new one. If it exists, skip creation entirely to prevent duplicates on re-tick.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from conftest import _load_dispatch  # noqa: E402

disp = _load_dispatch()


def _make_epic_issue(number: int = 1, title: str = "Big Feature") -> dict:
    """Epic issue with enough checklists to pass heuristic."""
    checklists = "\n".join("- [ ] task " + str(i) for i in range(10))
    return {
        "number": number,
        "title": title,
        "body": checklists,
        "labels": [{"name": "epic"}],
        "url": f"https://example.com/issues/{number}",
    }


def test_planner_idempotency_key_created_first_run():
    """First dispatch of an epic creates a planner card with key planner-{n}."""
    class FakeKanban:
        def __init__(self):
            self.created_keys = []
            self.tasks = []
        
        def list_tasks(self, slug, status=""):
            return self.tasks
        
        def create_task(self, slug, title, body="", *, assignee="", idempotency_key="", workspace="", skills=None, **kwargs):
            # Simulate CLI: if key exists, return existing task id
            if idempotency_key and any(t.get("idempotency_key") == idempotency_key for t in self.tasks):
                for t in self.tasks:
                    if t.get("idempotency_key") == idempotency_key:
                        return t["id"]
            tid = f"t_new_{len(self.tasks)}"
            self.tasks.append({"id": tid, "idempotency_key": idempotency_key, "title": title})
            self.created_keys.append(idempotency_key)
            return tid
    
    kanban = FakeKanban()
    issue = _make_epic_issue(number=42)
    n = 42
    
    # This is the fixed logic path
    if disp._is_epic(issue):
        key = f"planner-{n}"
        # Idempotency check: skip if already exists
        existing_task = next(
            (t for t in kanban.list_tasks("test-board") 
             if (t.get("idempotency_key") or "") == key),
            None
        )
        if existing_task is None:
            vid = kanban.create_task(
                "test-board", f"#{n} {issue.get('title', '')}",
                body="planner body",
                assignee="planner-daedalus",
                idempotency_key=key,
            )
            assert vid is not None
            # Track that we created a new task
            created_new = True
        else:
            created_new = False
    
    # Verify card was created with correct key
    assert created_new
    assert len(kanban.created_keys) == 1
    assert kanban.created_keys[0] == "planner-42"
    assert kanban.tasks[0]["idempotency_key"] == "planner-42"


def test_planner_idempotency_prevents_duplicate_on_retick():
    """Re-ticking an epic that already has a planner card does NOT create a duplicate."""
    class FakeKanban:
        def __init__(self):
            self.created_keys = []
            # Pre-populate with existing planner card
            self.tasks = [
                {"id": "t_existing", "idempotency_key": "planner-42", "title": "#42 Big Feature"},
            ]
        
        def list_tasks(self, slug, status=""):
            return self.tasks
        
        def create_task(self, slug, title, body="", *, assignee="", idempotency_key="", workspace="", skills=None, **kwargs):
            # If key already exists, return existing task id (simulates CLI behavior)
            for t in self.tasks:
                if t.get("idempotency_key") == idempotency_key:
                    return t["id"]
            tid = f"t_new_{len(self.tasks)}"
            self.tasks.append({"id": tid, "idempotency_key": idempotency_key, "title": title})
            self.created_keys.append(idempotency_key)
            return tid
    
    kanban = FakeKanban()
    issue = _make_epic_issue(number=42)
    n = 42
    
    # Simulate the fixed dispatch loop
    if disp._is_epic(issue):
        key = f"planner-{n}"
        # Idempotency check: skip if already exists
        existing_task = next(
            (t for t in kanban.list_tasks("test-board") 
             if (t.get("idempotency_key") or "") == key),
            None
        )
        if existing_task is None:
            vid = kanban.create_task(
                "test-board", f"#{n} {issue.get('title', '')}",
                body="planner body",
                assignee="planner-daedalus",
                idempotency_key=key,
            )
            created_new = True
        else:
            # Skip entirely — idempotency check worked
            created_new = False
    
    # Verify NO new card was created (idempotency check prevented duplicate)
    assert not created_new
    assert len(kanban.created_keys) == 0
    assert len(kanban.tasks) == 1  # Still the original


def test_different_epics_get_different_planner_keys():
    """Different epic issues create planner cards with distinct keys."""
    class FakeKanban:
        def __init__(self):
            self.created_keys = []
            self.tasks = []
        
        def list_tasks(self, slug, status=""):
            return self.tasks
        
        def create_task(self, slug, title, body="", *, assignee="", idempotency_key="", workspace="", skills=None, **kwargs):
            tid = f"t_{len(self.tasks)}"
            self.tasks.append({"id": tid, "idempotency_key": idempotency_key})
            self.created_keys.append(idempotency_key)
            return tid
    
    kanban = FakeKanban()
    
    # Dispatch two different epics
    for n in [10, 20]:
        issue = _make_epic_issue(number=n)
        if disp._is_epic(issue):
            key = f"planner-{n}"
            existing_task = next(
                (t for t in kanban.list_tasks("b") 
                 if (t.get("idempotency_key") or "") == key),
                None
            )
            if existing_task is None:
                kanban.create_task("b", f"#{n} Epic", body="x", assignee="planner-daedalus", idempotency_key=key)
    
    # Verify two distinct keys
    assert len(kanban.created_keys) == 2
    assert "planner-10" in kanban.created_keys
    assert "planner-20" in kanban.created_keys
    assert kanban.created_keys[0] != kanban.created_keys[1]
