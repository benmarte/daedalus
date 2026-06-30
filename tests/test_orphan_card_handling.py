#!/usr/bin/env python3
"""Unit tests for orphaned card detection and handling behaviors.

Covers the dispatcher's VCS-orphan cleanup path (archive kanban tasks when
an issue is closed directly on VCS bypassing the PR pipeline) and the
board-Done sync path (archive when a managed issue is moved to Done on the
VCS project board).

Also covers _repair_orphan_tasks (generic-assignee and missing-issue-prefix
repairs) and _count_active_issue_tasks (the accidental-close guard).

Run: python3 tests/test_orphan_card_handling.py
"""
import sys
import tempfile
from pathlib import Path
from unittest import mock

# Make the package root importable (config/, core/) and the tests dir (conftest).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import conftest  # noqa: E402
from conftest import FakeProvider, _load_dispatch, check  # noqa: E402,F401
from core.providers.base import IssueSummary  # noqa: E402


# ── _FakeProvider ─────────────────────────────────────────────────────────────
# Per-test configurables override the base class via instance attributes.

class _FakeProvider(FakeProvider):
    """Minimal provider double for orphan/board-done cleanup paths."""

    def __init__(self, *, closed_issues=frozenset(), open_issues=frozenset(),
                 board_done=frozenset(), issue_states=None,
                 stale=False, stale_warned=False):
        super().__init__()
        self._closed_issues = set(closed_issues)
        self._open_issues = set(open_issues)
        self._board_done = set(board_done)
        self._issue_states = dict(issue_states or {})

    def status_name(self, key):
        return {"ready": "Ready", "in_progress": "In progress",
                "in_review": "In review", "done": "Done"}.get(key, key)

    def list_issues(self, state="open", labels=None, limit=50):
        return [IssueSummary(number=n, title=f"open issue #{n}")
                for n in sorted(self._open_issues)]

    def pr_state_for_issue(self, n):
        return None

    def get_issue_state(self, issue_number):
        state = self._issue_states.get(issue_number)
        if state is not None:
            return state
        if issue_number in self._closed_issues:
            return "closed"
        return "open"

    def board_numbers_with_statuses(self, names):
        return set(self._board_done)

    def board_set_status(self, issue_number, status):
        return True

    def post_issue_comment(self, issue_number, body):
        return True

    def board_configured(self):
        return True

    def _pr_for_issue(self, issue_number, state="open"):
        return None


def _base_config(tmp):
    """Return a minimal resolved config for disp.run()."""
    return {
        "repo": "O/R", "workdir": tmp, "name": "x",
        "issues": {"filters": {}},
        "execution": {"staleness_hours": 48},
        "tracking": {"github_project_number": 1},
    }


def _wire_dispatch(disp, *, issues_in_board, cards=None, close_tracker=None):
    """Replace kanban functions on disp with test doubles.

    ``cards`` is a list of dicts with keys: id, title, status, assignee (optional).
    ``close_tracker`` receives all close_issue_tasks calls as dicts.
    """
    cards = cards or []

    def _list_issue_numbers(slug):
        return set(issues_in_board)

    def _list_tasks(slug, status=""):
        out = []
        for c in cards:
            st = c.get("status", "todo")
            if status and st != status:
                continue
            out.append({
                "id": c["id"],
                "title": c.get("title", ""),
                "status": st,
                "assignee": c.get("assignee", "developer-daedalus"),
            })
        return out

    def _close_issue_tasks(slug, n, summary=None, dry_run=False):
        matched = [c["id"] for c in cards
                   if f"#{n}" in (c.get("title") or "") and c.get("status", "todo") == "done"]
        if close_tracker is not None:
            close_tracker.append({"slug": slug, "n": n, "summary": summary,
                                 "dry_run": dry_run, "ids": list(matched)})
        return matched

    def _show_card(slug, task_id):
        for c in cards:
            if c["id"] == task_id:
                return dict(c)
        return {}

    disp.kanban.ensure_board = lambda s: None
    disp.kanban.list_blocked = lambda s: []
    disp.kanban.list_issue_numbers = _list_issue_numbers
    disp.kanban.list_tasks = _list_tasks
    disp.kanban.close_issue_tasks = _close_issue_tasks
    disp.kanban.show_card = _show_card
    disp.kanban.dispatch = lambda s, max_spawns=5: True


# ═══════════════════════════════════════════════════════════════════════════════
# Section 1: VCS orphan cleanup — issue closed directly on VCS
# ═══════════════════════════════════════════════════════════════════════════════

def test_orphan_cleanup_archives_tasks_when_issue_closed_no_active():
    """Issue closed on VCS with no active kanban tasks → bulk-complete runs."""
    disp = _load_dispatch()
    close_tracker = []
    tasks = [
        {"id": "t_a", "title": "#105 QA verify", "status": "done"},
        {"id": "t_b", "title": "#105 Developer implement", "status": "done"},
    ]
    fp = _FakeProvider(open_issues=frozenset(), issue_states={105: "closed"})

    with tempfile.TemporaryDirectory() as tmp:
        _wire_dispatch(disp, issues_in_board={105}, cards=tasks,
                       close_tracker=close_tracker)
        disp.run(_base_config(tmp), provider=fp)

    close_105 = [c for c in close_tracker if c["n"] == 105 and not c["dry_run"]]
    check("close_issue_tasks invoked (non-dry) for closed issue #105",
          len(close_105) >= 1)


def test_orphan_cleanup_skips_when_active_tasks_exist():
    """Issue closed on VCS WITH active kanban tasks → no bulk-complete (accidental-close guard)."""
    disp = _load_dispatch()
    close_tracker = []
    active_tasks = [
        {"id": "t_a", "title": "#105 QA verify", "status": "todo"},
        {"id": "t_b", "title": "#105 Developer implement", "status": "running"},
    ]
    fp = _FakeProvider(open_issues=frozenset(), issue_states={105: "closed"})

    with tempfile.TemporaryDirectory() as tmp:
        _wire_dispatch(disp, issues_in_board={105}, cards=active_tasks,
                       close_tracker=close_tracker)
        disp.run(_base_config(tmp), provider=fp)

    close_105 = [c for c in close_tracker if c["n"] == 105 and not c["dry_run"]]
    check("no bulk-complete when active tasks exist (accidental-close guard)",
          len(close_105) == 0)


def test_orphan_cleanup_skips_when_issue_still_open():
    """Issue missing from open fetch but VCS state='open' → no cleanup."""
    disp = _load_dispatch()
    close_tracker = []
    fp = _FakeProvider(open_issues=frozenset(), issue_states={105: "open"})

    with tempfile.TemporaryDirectory() as tmp:
        _wire_dispatch(disp, issues_in_board={105}, close_tracker=close_tracker)
        disp.run(_base_config(tmp), provider=fp)

    check("no cleanup when VCS state is 'open' (filtered but not closed)",
          len(close_tracker) == 0)


def test_orphan_cleanup_skips_unknown_state():
    """Issue missing from open fetch and VCS state is unknown → no cleanup."""
    disp = _load_dispatch()
    close_tracker = []
    fp = _FakeProvider(open_issues=frozenset(), issue_states={105: "unknown"})

    with tempfile.TemporaryDirectory() as tmp:
        _wire_dispatch(disp, issues_in_board={105}, close_tracker=close_tracker)
        disp.run(_base_config(tmp), provider=fp)

    check("no cleanup when VCS state is unknown",
          len(close_tracker) == 0)


def test_orphan_cleanup_dry_run_does_not_close():
    """Dry-run mode: orphan detected → close_issue_tasks called with dry_run=True only."""
    disp = _load_dispatch()
    close_tracker = []
    tasks = [{"id": "t_a", "title": "#105 QA verify", "status": "done"}]
    fp = _FakeProvider(open_issues=frozenset(), issue_states={105: "closed"})

    with tempfile.TemporaryDirectory() as tmp:
        _wire_dispatch(disp, issues_in_board={105}, cards=tasks,
                       close_tracker=close_tracker)
        disp.run(_base_config(tmp), provider=fp, dry_run=True)

    close_105 = [c for c in close_tracker if c["n"] == 105]
    check("dry-run invoked close_issue_tasks for issue 105", len(close_105) >= 1)
    check("all close calls are dry_run=True in dry-run mode",
          all(c["dry_run"] for c in close_105))


def test_orphan_cleanup_already_handled_no_duplicate():
    """Previously completed issue: close_issue_tasks returns empty → idempotent, no double-close."""
    disp = _load_dispatch()
    close_tracker = []
    # All tasks already done, close returns empty
    tasks = [{"id": "t1", "title": "#105 QA verify", "status": "done"}]
    fp = _FakeProvider(open_issues=frozenset(), issue_states={105: "closed"})

    with tempfile.TemporaryDirectory() as tmp:
        _wire_dispatch(disp, issues_in_board={105}, cards=tasks,
                       close_tracker=close_tracker)
        # Override close to return empty (already archived)
        orig_close = disp.kanban.close_issue_tasks
        def _close_empty(slug, n, summary=None, dry_run=False):
            close_tracker.append({"slug": slug, "n": n, "summary": summary,
                                 "dry_run": dry_run, "ids": []})
            return []
        disp.kanban.close_issue_tasks = _close_empty

        try:
            disp.run(_base_config(tmp), provider=fp)
            raised = False
        except Exception:
            raised = True

        disp.kanban.close_issue_tasks = orig_close

    check("already-handled orphan does not raise", not raised)


def test_orphan_cleanup_partial_state_not_in_existing():
    """Issues not in the managed 'existing' set are ignored by cleanup paths."""
    disp = _load_dispatch()
    close_tracker = []
    fp = _FakeProvider(open_issues=frozenset(), issue_states={999: "closed"})

    with tempfile.TemporaryDirectory() as tmp:
        # 999 NOT in issues_in_board (i.e. not in `existing` set)
        _wire_dispatch(disp, issues_in_board={100}, close_tracker=close_tracker)
        disp.run(_base_config(tmp), provider=fp)

    check("cleanup skipped issue 999 (not in managed existing set)",
          all(c["n"] != 999 for c in close_tracker))


def test_orphan_cleanup_mixed_issues():
    """Mixed state: one issue closed, one open → only the closed one is archived."""
    disp = _load_dispatch()
    close_tracker = []
    tasks = [
        {"id": "t_a", "title": "#105 QA verify", "status": "done"},
    ]
    fp = _FakeProvider(
        open_issues=frozenset({106}),
        issue_states={105: "closed", 106: "open"},
    )

    with tempfile.TemporaryDirectory() as tmp:
        _wire_dispatch(disp, issues_in_board={105, 106}, cards=tasks,
                       close_tracker=close_tracker)
        disp.run(_base_config(tmp), provider=fp)

    closed_nums = {c["n"] for c in close_tracker if not c["dry_run"]}
    check("closed issue 105 archived", 105 in closed_nums)
    check("open issue 106 NOT archived", 106 not in closed_nums)


def test_orphan_cleanup_board_set_done_called():
    """Orphan cleanup sets board status to Done on provider after closing tasks."""
    disp = _load_dispatch()
    close_tracker = []
    tasks = [{"id": "t_a", "title": "#105 QA verify", "status": "done"}]
    fp = _FakeProvider(open_issues=frozenset(), issue_states={105: "closed"})

    board_set_calls = []
    orig_board_set = fp.board_set_status
    def _track_board_set(issue_number, status):
        board_set_calls.append((issue_number, status))
        return orig_board_set(issue_number, status)
    fp.board_set_status = _track_board_set

    with tempfile.TemporaryDirectory() as tmp:
        _wire_dispatch(disp, issues_in_board={105}, cards=tasks,
                       close_tracker=close_tracker)
        disp.run(_base_config(tmp), provider=fp)

    done_calls = [(n, s) for n, s in board_set_calls if s == "Done"]
    check("board_set_status called with 'Done' for orphan issue 105",
          any(n == 105 for n, _ in done_calls))


# ═══════════════════════════════════════════════════════════════════════════════
# Section 2: Board-Done sync path
# ═══════════════════════════════════════════════════════════════════════════════

def test_board_done_sync_archives_kanban_tasks():
    """Issue moved to Done on VCS board → close_issue_tasks archives its tasks."""
    disp = _load_dispatch()
    close_tracker = []
    tasks = [
        {"id": "t_a", "title": "#105 QA verify", "status": "done"},
        {"id": "t_b", "title": "#105 Dev impl", "status": "done"},
    ]
    fp = _FakeProvider(
        open_issues=frozenset({105}),
        board_done=frozenset({105}),
        issue_states={105: "open"},
    )

    with tempfile.TemporaryDirectory() as tmp:
        _wire_dispatch(disp, issues_in_board={105}, cards=tasks,
                       close_tracker=close_tracker)
        disp.run(_base_config(tmp), provider=fp)

    done_calls = [c for c in close_tracker if c["n"] == 105 and not c["dry_run"]]
    check("close_issue_tasks invoked for board-done issue #105",
          len(done_calls) >= 1)


def test_board_done_sync_dry_run_does_not_close():
    """Board-Done sync in dry-run mode → close_issue_tasks called with dry_run=True only."""
    disp = _load_dispatch()
    close_tracker = []
    fp = _FakeProvider(
        open_issues=frozenset({105}),
        board_done=frozenset({105}),
        issue_states={105: "open"},
    )

    with tempfile.TemporaryDirectory() as tmp:
        _wire_dispatch(disp, issues_in_board={105}, close_tracker=close_tracker)
        disp.run(_base_config(tmp), provider=fp, dry_run=True)

    done_calls = [c for c in close_tracker if c["n"] == 105]
    check("dry-run invoked close for board-done issue 105", len(done_calls) >= 1)
    check("no non-dry close call in dry-run mode",
          all(c["dry_run"] for c in done_calls))


def test_board_done_sync_skips_already_completed():
    """Issue both on board-Done AND closed on VCS → no duplicate archive."""
    disp = _load_dispatch()
    close_tracker = []
    fp = _FakeProvider(
        open_issues=frozenset({105}),
        board_done=frozenset({105}),
        issue_states={105: "closed"},
    )

    with tempfile.TemporaryDirectory() as tmp:
        _wire_dispatch(disp, issues_in_board={105}, close_tracker=close_tracker)
        disp.run(_base_config(tmp), provider=fp)

    close_105 = [c for c in close_tracker if c["n"] == 105 and not c["dry_run"]]
    check("issue closed via both paths results in at most a few close calls (not excessive)",
          len(close_105) <= 3)


# ═══════════════════════════════════════════════════════════════════════════════
# Section 3: _count_active_issue_tasks (accidental-close guard)
# ═══════════════════════════════════════════════════════════════════════════════

def test_count_active_issue_tasks_zero_when_all_done():
    """_count_active_issue_tasks returns 0 when all tasks for issue are done."""
    disp = _load_dispatch()

    tasks = [
        {"id": "t_a", "title": "#42 Impl", "status": "done"},
        {"id": "t_b", "title": "#42 QA", "status": "done"},
    ]

    disp.kanban.list_tasks = lambda s, status="": [
        {"id": t["id"], "title": t["title"], "status": t["status"]}
        for t in tasks
    ]

    result = disp._count_active_issue_tasks("slug", 42)
    check("_count_active_issue_tasks returns 0 when all done", result == 0)


def test_count_active_issue_tasks_counts_running_and_todo():
    """_count_active_issue_tasks counts tasks in running/todo/ready/blocked states."""
    disp = _load_dispatch()

    tasks = [
        {"id": "t_a", "title": "#42 Impl", "status": "running"},
        {"id": "t_b", "title": "#42 QA", "status": "todo"},
        {"id": "t_c", "title": "#42 Review", "status": "done"},
    ]

    disp.kanban.list_tasks = lambda s, status="": [
        {"id": t["id"], "title": t["title"], "status": t["status"]}
        for t in tasks
    ]

    result = disp._count_active_issue_tasks("slug", 42)
    check("_count_active_issue_tasks counts running+todo as active", result == 2)


def test_count_active_issue_tasks_excludes_cancelled():
    """_count_active_issue_tasks does not count cancelled tasks as active."""
    disp = _load_dispatch()

    tasks = [
        {"id": "t_a", "title": "#42 Impl", "status": "cancelled"},
        {"id": "t_b", "title": "#42 QA", "status": "done"},
    ]

    disp.kanban.list_tasks = lambda s, status="": [
        {"id": t["id"], "title": t["title"], "status": t["status"]}
        for t in tasks
    ]

    result = disp._count_active_issue_tasks("slug", 42)
    check("_count_active_issue_tasks excludes cancelled tasks", result == 0)


def test_count_active_issue_tasks_ignores_other_issues():
    """_count_active_issue_tasks only counts tasks matching the given issue number."""
    disp = _load_dispatch()

    tasks = [
        {"id": "t_a", "title": "#42 Impl", "status": "running"},
        {"id": "t_b", "title": "#99 QA", "status": "running"},
        {"id": "t_c", "title": "#42 Review", "status": "todo"},
    ]

    disp.kanban.list_tasks = lambda s, status="": [
        {"id": t["id"], "title": t["title"], "status": t["status"]}
        for t in tasks
    ]

    result_42 = disp._count_active_issue_tasks("slug", 42)
    result_99 = disp._count_active_issue_tasks("slug", 99)
    check("_count_active_issue_tasks counts only issue #42 tasks", result_42 == 2)
    check("_count_active_issue_tasks counts only issue #99 tasks", result_99 == 1)


def test_count_active_issue_tasks_handles_blocked():
    """Blocked tasks are considered active (not done/cancelled)."""
    disp = _load_dispatch()

    tasks = [
        {"id": "t_a", "title": "#42 Impl", "status": "blocked"},
        {"id": "t_b", "title": "#42 QA", "status": "done"},
    ]

    disp.kanban.list_tasks = lambda s, status="": [
        {"id": t["id"], "title": t["title"], "status": t["status"]}
        for t in tasks
    ]

    result = disp._count_active_issue_tasks("slug", 42)
    check("_count_active_issue_tasks counts blocked as active", result == 1)


# ═══════════════════════════════════════════════════════════════════════════════
# Section 4: _repair_orphan_tasks (generic-assignee and missing-issue-prefix)
# ═══════════════════════════════════════════════════════════════════════════════

def test_repair_remaps_generic_developer():
    """_repair_orphan_tasks remaps generic 'developer' → developer-daedalus."""
    disp = _load_dispatch()

    tasks = [
        {"id": "t_fix", "title": "Some task", "status": "todo",
         "assignee": "developer", "body": "See #55 for details"},
    ]

    reassign_calls = []
    disp.kanban.list_tasks = lambda s, status="": [
        dict(t) for t in tasks
        if not status or t.get("status") == status
    ]
    disp.kanban.reassign_task = lambda s, tid, new_a: (
        reassign_calls.append((s, tid, new_a)) or True
    )

    repaired = disp._repair_orphan_tasks("slug", disp._DEFAULT_PROFILES)
    check("_repair_orphan_tasks remapped 'developer' assignee",
          any(new_a == "developer-daedalus" for _, _, new_a in reassign_calls))


def test_repair_skips_running_tasks():
    """_repair_orphan_tasks only acts on todo/ready tasks."""
    disp = _load_dispatch()

    tasks = [
        {"id": "t_run", "title": "Running task", "status": "running",
         "assignee": "developer"},
    ]

    reassign_calls = []
    disp.kanban.list_tasks = lambda s, status="": [
        dict(t) for t in tasks
        if not status or t.get("status") == status
    ]
    disp.kanban.reassign_task = lambda s, tid, new_a: (
        reassign_calls.append((s, tid, new_a)) or True
    )

    repaired = disp._repair_orphan_tasks("slug", disp._DEFAULT_PROFILES)
    check("_repair_orphan_tasks skipped running task", len(reassign_calls) == 0)


def test_repair_skips_done_tasks():
    """_repair_orphan_tasks skips done tasks."""
    disp = _load_dispatch()

    tasks = [
        {"id": "t_done", "title": "Done task", "status": "done",
         "assignee": "developer"},
    ]

    reassign_calls = []
    disp.kanban.list_tasks = lambda s, status="": [
        dict(t) for t in tasks
        if not status or t.get("status") == status
    ]
    disp.kanban.reassign_task = lambda s, tid, new_a: (
        reassign_calls.append((s, tid, new_a)) or True
    )

    disp._repair_orphan_tasks("slug", disp._DEFAULT_PROFILES)
    check("_repair_orphan_tasks skipped done task", len(reassign_calls) == 0)


def test_repair_skips_unknown_assignee():
    """_repair_orphan_tasks skips tasks with assignees not in _GENERIC_TO_ROLE."""
    disp = _load_dispatch()

    tasks = [
        {"id": "t_un", "title": "Unknown task", "status": "todo",
         "assignee": "some-random-user"},
    ]

    reassign_calls = []
    disp.kanban.list_tasks = lambda s, status="": [
        dict(t) for t in tasks
        if not status or t.get("status") == status
    ]
    disp.kanban.reassign_task = lambda s, tid, new_a: (
        reassign_calls.append((s, tid, new_a)) or True
    )

    disp._repair_orphan_tasks("slug", disp._DEFAULT_PROFILES)
    check("_repair_orphan_tasks skipped unknown assignee", len(reassign_calls) == 0)


def test_repair_dry_run_does_not_mutate():
    """Dry-run mode: _repair_orphan_tasks logs but does not reassign."""
    disp = _load_dispatch()

    tasks = [
        {"id": "t_fix", "title": "Some task", "status": "todo",
         "assignee": "developer", "body": "See #55"},
    ]

    reassign_calls = []
    disp.kanban.list_tasks = lambda s, status="": [
        dict(t) for t in tasks
        if not status or t.get("status") == status
    ]
    disp.kanban.reassign_task = lambda s, tid, new_a: (
        reassign_calls.append((s, tid, new_a)) or True
    )

    repaired = disp._repair_orphan_tasks("slug", disp._DEFAULT_PROFILES, dry_run=True)
    check("_repair_orphan_tasks dry_run does not call reassign_task",
          len(reassign_calls) == 0)
    check("_repair_orphan_tasks dry_run still returns count", repaired >= 1)


def test_repair_idempotent_already_fixed():
    """Re-running _repair_orphan_tasks on already-fixed tasks is a no-op."""
    disp = _load_dispatch()

    # Already has correct assignee
    tasks = [
        {"id": "t_ok", "title": "#55 Some task", "status": "todo",
         "assignee": "developer-daedalus"},
    ]

    reassign_calls = []
    rename_calls = []
    disp.kanban.list_tasks = lambda s, status="": [
        dict(t) for t in tasks
        if not status or t.get("status") == status
    ]
    disp.kanban.reassign_task = lambda s, tid, new_a: (
        reassign_calls.append((s, tid, new_a)) or True
    )
    disp.kanban.rename_task = lambda s, tid, new_name: (
        rename_calls.append((s, tid, new_name)) or True
    )

    repaired = disp._repair_orphan_tasks("slug", disp._DEFAULT_PROFILES)
    check("_repair_orphan_tasks no-op on already-fixed task", repaired == 0)
    check("no reassign calls on fixed task", len(reassign_calls) == 0)
    check("no rename calls on fixed task", len(rename_calls) == 0)


def test_repair_handles_multiple_generic_assignees():
    """_repair_orphan_tasks remaps developer, qa, reviewer generically."""
    disp = _load_dispatch()

    tasks = [
        {"id": "t_dev", "title": "Dev task", "status": "todo",
         "assignee": "developer", "body": "See #10"},
        {"id": "t_qa", "title": "QA task", "status": "ready",
         "assignee": "qa", "body": "See #10"},
        {"id": "t_rev", "title": "Review task", "status": "todo",
         "assignee": "reviewer", "body": "See #10"},
    ]

    reassign_targets = []
    disp.kanban.list_tasks = lambda s, status="": [
        dict(t) for t in tasks
        if not status or t.get("status") == status
    ]
    disp.kanban.reassign_task = lambda s, tid, new_a: (
        reassign_targets.append(new_a) or True
    )

    repaired = disp._repair_orphan_tasks("slug", disp._DEFAULT_PROFILES)
    check("repaired developer → developer-daedalus",
          "developer-daedalus" in reassign_targets)
    check("repaired qa → qa-daedalus",
          "qa-daedalus" in reassign_targets)
    check("repaired reviewer → reviewer-daedalus",
          "reviewer-daedalus" in reassign_targets)


def test_repair_custom_profiles():
    """_repair_orphan_tasks respects custom profile names."""
    disp = _load_dispatch()

    custom_profiles = {**disp._DEFAULT_PROFILES, "developer": "my-custom-dev"}

    tasks = [
        {"id": "t_dev", "title": "Dev task", "status": "todo",
         "assignee": "developer", "body": "See #20"},
    ]

    reassign_targets = []
    disp.kanban.list_tasks = lambda s, status="": [
        dict(t) for t in tasks
        if not status or t.get("status") == status
    ]
    disp.kanban.reassign_task = lambda s, tid, new_a: (
        reassign_targets.append(new_a) or True
    )

    repaired = disp._repair_orphan_tasks("slug", custom_profiles)
    check("repaired to custom profile name",
          "my-custom-dev" in reassign_targets)


# ═══════════════════════════════════════════════════════════════════════════════
# Section 5: Edge cases / concurrent scenarios
# ═══════════════════════════════════════════════════════════════════════════════

def test_orphan_cleanup_multiple_issues_closed():
    """Multiple issues closed simultaneously → each archived independently."""
    disp = _load_dispatch()
    close_tracker = []
    tasks = [
        {"id": "t_a", "title": "#101 Impl", "status": "done"},
        {"id": "t_b", "title": "#102 Impl", "status": "done"},
        {"id": "t_c", "title": "#103 Impl", "status": "done"},
    ]
    fp = _FakeProvider(
        open_issues=frozenset(),
        issue_states={101: "closed", 102: "closed", 103: "closed"},
    )

    with tempfile.TemporaryDirectory() as tmp:
        _wire_dispatch(disp, issues_in_board={101, 102, 103}, cards=tasks,
                       close_tracker=close_tracker)
        disp.run(_base_config(tmp), provider=fp)

    closed_nums = {c["n"] for c in close_tracker if not c["dry_run"]}
    check("all three closed issues archived", {101, 102, 103}.issubset(closed_nums))


def test_orphan_cleanup_partial_active_partial_done():
    """Some tasks done, some active → guard prevents bulk-complete."""
    disp = _load_dispatch()
    close_tracker = []
    tasks = [
        {"id": "t_done", "title": "#105 Done task", "status": "done"},
        {"id": "t_active", "title": "#105 Still running", "status": "running"},
    ]
    fp = _FakeProvider(open_issues=frozenset(), issue_states={105: "closed"})

    with tempfile.TemporaryDirectory() as tmp:
        _wire_dispatch(disp, issues_in_board={105}, cards=tasks,
                       close_tracker=close_tracker)
        disp.run(_base_config(tmp), provider=fp)

    close_105 = [c for c in close_tracker if c["n"] == 105 and not c["dry_run"]]
    check("active task prevents bulk-complete even with done tasks present",
          len(close_105) == 0)


def test_orphan_cleanup_empty_cards():
    """No kanban cards exist for a closed issue → cleanup runs but archives nothing."""
    disp = _load_dispatch()
    close_tracker = []
    fp = _FakeProvider(open_issues=frozenset(), issue_states={105: "closed"})

    with tempfile.TemporaryDirectory() as tmp:
        _wire_dispatch(disp, issues_in_board={105}, cards=[],
                       close_tracker=close_tracker)
        try:
            disp.run(_base_config(tmp), provider=fp)
            raised = False
        except Exception:
            raised = True

    check("no crash when no cards exist for orphan issue", not raised)


def test_board_done_no_tasks_to_archive():
    """Board-Done issue with no kanban tasks → no crash."""
    disp = _load_dispatch()
    close_tracker = []
    fp = _FakeProvider(
        open_issues=frozenset({105}),
        board_done=frozenset({105}),
        issue_states={105: "open"},
    )

    with tempfile.TemporaryDirectory() as tmp:
        _wire_dispatch(disp, issues_in_board={105}, cards=[],
                       close_tracker=close_tracker)
        try:
            disp.run(_base_config(tmp), provider=fp)
            raised = False
        except Exception:
            raised = True

    check("board-done with no cards does not crash", not raised)


def test_orphan_cleanup_with_cancelled_tasks():
    """Cancelled tasks don't count as active → cleanup proceeds."""
    disp = _load_dispatch()
    close_tracker = []
    tasks = [
        {"id": "t_c1", "title": "#105 Cancelled QA", "status": "cancelled"},
        {"id": "t_d1", "title": "#105 Done Dev", "status": "done"},
    ]
    fp = _FakeProvider(open_issues=frozenset(), issue_states={105: "closed"})

    with tempfile.TemporaryDirectory() as tmp:
        _wire_dispatch(disp, issues_in_board={105}, cards=tasks,
                       close_tracker=close_tracker)
        disp.run(_base_config(tmp), provider=fp)

    close_105 = [c for c in close_tracker if c["n"] == 105 and not c["dry_run"]]
    check("cancelled tasks don't block orphan cleanup",
          len(close_105) >= 1)


# ── runner ───────────────────────────────────────────────────────────────────

def main():
    tests = [
        # Section 1: VCS orphan cleanup
        test_orphan_cleanup_archives_tasks_when_issue_closed_no_active,
        test_orphan_cleanup_skips_when_active_tasks_exist,
        test_orphan_cleanup_skips_when_issue_still_open,
        test_orphan_cleanup_skips_unknown_state,
        test_orphan_cleanup_dry_run_does_not_close,
        test_orphan_cleanup_already_handled_no_duplicate,
        test_orphan_cleanup_partial_state_not_in_existing,
        test_orphan_cleanup_mixed_issues,
        test_orphan_cleanup_board_set_done_called,
        # Section 2: Board-Done sync
        test_board_done_sync_archives_kanban_tasks,
        test_board_done_sync_dry_run_does_not_close,
        test_board_done_sync_skips_already_completed,
        # Section 3: _count_active_issue_tasks
        test_count_active_issue_tasks_zero_when_all_done,
        test_count_active_issue_tasks_counts_running_and_todo,
        test_count_active_issue_tasks_excludes_cancelled,
        test_count_active_issue_tasks_ignores_other_issues,
        test_count_active_issue_tasks_handles_blocked,
        # Section 4: _repair_orphan_tasks
        test_repair_remaps_generic_developer,
        test_repair_skips_running_tasks,
        test_repair_skips_done_tasks,
        test_repair_skips_unknown_assignee,
        test_repair_dry_run_does_not_mutate,
        test_repair_idempotent_already_fixed,
        test_repair_handles_multiple_generic_assignees,
        test_repair_custom_profiles,
        # Section 5: Edge cases
        test_orphan_cleanup_multiple_issues_closed,
        test_orphan_cleanup_partial_active_partial_done,
        test_orphan_cleanup_empty_cards,
        test_board_done_no_tasks_to_archive,
        test_orphan_cleanup_with_cancelled_tasks,
    ]

    print("orphaned card handling behavior tests")
    print("=" * 60)
    passed = 0
    failed = 0
    for fn in tests:
        name = fn.__name__
        try:
            fn()
            passed += 1
            print(f"  ✓ {name}")
        except AssertionError as e:
            failed += 1
            print(f"  ✗ {name}: {e}")
        except Exception as e:
            failed += 1
            print(f"  ✗ {name} (unexpected): {e}")
    print()
    print(f"  {passed}/{passed + failed} tests passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
