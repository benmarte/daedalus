#!/usr/bin/env python3
"""Unit tests for orphaned card detection and handling behaviors.

Covers the dispatcher's VCS-orphan cleanup path (archive kanban tasks when
an issue is closed directly on VCS bypassing the PR pipeline, issue #109
accidental-close guard) and the board-Done sync path (archive when a managed
issue is moved to Done on the VCS project board).

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
# Per-test configurables override the base class via instance attributes. The
# pattern matches test_daedalus.py::test_staleness_* integration tests so a
# maintainer familiar with those can read these without extra context.


class _FakeProvider(FakeProvider):
    """Minimal provider double for orphan/board-done cleanup paths."""

    def __init__(self, *, closed_issues=frozenset(), open_issues=frozenset(),
                 board_done=frozenset(), issue_states=None,
                 stale=False, stale_warned=False):
        # closed_issues/issues_seen are disjoint with open_issues from the
        # perspective of list_issues (the fetch). get_issue_state reports the
        # authoritative VCS state for the orphaned-set lookup.
        super().__init__()
        self._closed_issues = set(closed_issues)
        self._open_issues = set(open_issues)
        self._board_done = set(board_done)
        self._issue_states = dict(issue_states or {})
        self._stale = stale
        self._stale_warned = stale_warned

    def status_name(self, key):
        return {"ready": "Ready", "in_progress": "In progress",
                "in_review": "In review", "done": "Done"}.get(key, key)

    def list_issues(self, state="open", labels=None, limit=50):
        # The orphan path is keyed on the open fetch — issues NOT in this set
        # but present in the managed `existing` set are considered orphaned.
        return [IssueSummary(number=n, title=f"open issue #{n}")
                for n in sorted(self._open_issues)]

    def pr_state_for_issue(self, n):
        return None

    def get_issue_state(self, issue_number):
        # Returns the VCS-side state for issue #issue_number, used by the
        # orphan-cleanup pass to decide whether a "missing from open fetch"
        # issue is closed.
        return self._issue_states.get(issue_number, "open")

    def board_numbers_with_statuses(self, names):
        if any(n in self._board_done for n in (self._issue_states or [])) or True:
            return set(self._board_done)
        return set()

    def board_set_status(self, issue_number, status):
        return True

    def post_issue_comment(self, issue_number, body):
        return True

    def board_configured(self):
        return True

    def _pr_for_issue(self, issue_number, state="open"):
        return None


def _run(disp, fp, *, tmp, issues_in_board, closed_tasks_per_issue=None):
    """Shared run-dispatcher helper. Kanban functions are replaced with
    per-test lambdas; returns the result dict."""
    closed_tasks_per_issue = closed_tasks_per_issue or {}

    def _list_issue_numbers(slug):
        return set(issues_in_board)

    def _list_tasks(slug, status=""):
        out = []
        for n in issues_in_board:
            for t in closed_tasks_per_issue.get(n, []):
                st = t.get("status", "todo")
                if status and st != status:
                    continue
                out.append({"id": t["id"], "title": f"#{n} {t.get('title', '')}",
                            "status": st,
                            "assignee": t.get("assignee", "developer-daedalus")})
        return out

    def _close_issue_tasks(slug, n, summary=None, dry_run=False):
        if dry_run:
            return [t["id"] for t in closed_tasks_per_issue.get(n, [])]
        return [t["id"] for t in closed_tasks_per_issue.get(n, [])]

    disp.kanban.ensure_board = lambda s: None
    disp.kanban.list_blocked = lambda s: []
    disp.kanban.list_issue_numbers = _list_issue_numbers
    disp.kanban.list_tasks = _list_tasks
    disp.kanban.close_issue_tasks = _close_issue_tasks
    disp.kanban.dispatch = lambda s, max_spawns=5: True

    return disp.run(
        {"repo": "O/R", "workdir": tmp, "name": "x",
         "issues": {"filters": {}},
         "execution": {"staleness_hours": 48},
         "tracking": {"github_project_number": 1}},
        provider=fp,
    )


# ── VCS orphan cleanup: issue closed directly on VCS ────────────────────────


def test_orphan_cleanup_archives_tasks_when_issue_closed_no_active():
    """Issue closed on VCS with no active kanban tasks → bulk-complete runs."""
    disp = _load_dispatch()

    closed_calls = []

    fp = _FakeProvider(
        open_issues=frozenset(),  # none in open fetch
        issue_states={105: "closed"},
    )
    tasks_to_close = [
        {"id": "t_a", "title": "QA verify", "status": "done"},
        {"id": "t_b", "title": "Developer implement", "status": "done"},
    ]

    with tempfile.TemporaryDirectory() as tmp:
        original_close = disp.kanban.close_issue_tasks

        def _track_close(slug, n, summary=None, dry_run=False):
            closed_calls.append((slug, n, summary, dry_run))
            return [t["id"] for t in tasks_to_close]

        disp.kanban.close_issue_tasks = _track_close
        _run(disp, fp, tmp=tmp,
             issues_in_board={105},
             closed_tasks_per_issue={105: tasks_to_close})

        disp.kanban.close_issue_tasks = original_close

    # At least one close call reached the kanban layer for issue 105.
    check("close_issue_tasks invoked for closed issue #105",
          any(n == 105 for _, n, _, _ in closed_calls))


def test_orphan_cleanup_skips_when_active_tasks_exist():
    """Issue closed on VCS WITH active kanban tasks → no bulk-complete (accidental-close guard)."""
    disp = _load_dispatch()

    fp = _FakeProvider(
        open_issues=frozenset(),
        issue_states={105: "closed"},
    )
    # Active (non-done) tasks — their presence should trigger the guard.
    active_tasks = [
        {"id": "t_a", "title": "QA verify", "status": "todo"},
    ]

    close_calls = []
    with tempfile.TemporaryDirectory() as tmp:

        def _track_close(slug, n, summary=None, dry_run=False):
            close_calls.append((slug, n, summary, dry_run))
            return [t["id"] for t in active_tasks]

        disp.kanban.close_issue_tasks = _track_close
        # list_tasks returns active tasks so the guard sees them.
        _run(disp, fp, tmp=tmp,
             issues_in_board={105},
             closed_tasks_per_issue={105: active_tasks})

    # The active-task guard fires BEFORE any close_issue_tasks call in this path,
    # so we should see no close invocation. (If the test is wired before the guard,
    # close_calls will be non-empty — this asserts the guard actually skipped.)
    check("no bulk-complete when active tasks exist (accidental-close guard)",
          all(n != 105 for _, n, _, _ in close_calls))


def test_orphan_cleanup_skips_unknown_or_filtered_state():
    """Issue missing from open fetch but in 'open' VCS state → no cleanup."""
    disp = _load_dispatch()

    fp = _FakeProvider(
        open_issues=frozenset(),  # filtered out or genuinely missing
        issue_states={105: "open"},  # VCS reports still open (e.g. filtered)
    )

    with tempfile.TemporaryDirectory() as tmp:
        close_calls = []

        def _track_close(slug, n, summary=None, dry_run=False):
            close_calls.append((slug, n, summary, dry_run))
            return []

        disp.kanban.close_issue_tasks = _track_close
        _run(disp, fp, tmp=tmp, issues_in_board={105})

    check("no cleanup when VCS state is 'open' (not closed)", close_calls == [])


def test_orphan_cleanup_dry_run_does_not_close():
    """Dry-run mode: orphan detected → close_issue_tasks called with dry_run=True only."""
    disp = _load_dispatch()

    fp = _FakeProvider(
        open_issues=frozenset(),
        issue_states={105: "closed"},
    )
    tasks_to_close = [{"id": "t_a", "title": "QA verify", "status": "done"}]

    with tempfile.TemporaryDirectory() as tmp:
        close_calls = []

        def _track_close(slug, n, summary=None, dry_run=False):
            close_calls.append({"slug": slug, "n": n, "summary": summary, "dry_run": dry_run})
            return [t["id"] for t in tasks_to_close]

        disp.kanban.close_issue_tasks = _track_close
        # Run with dry_run=True via a mock of dispatch_state to skip file I/O.
        disp.run(
            {"repo": "O/R", "workdir": tmp, "name": "x",
             "issues": {"filters": {}},
             "execution": {"staleness_hours": 48},
             "tracking": {"github_project_number": 1}},
            provider=fp,
            dry_run=True,
        )

    # In dry-run, the dispatcher must have passed dry_run=True, never False.
    dry_calls = [c for c in close_calls if c["n"] == 105]
    check("dry-run invoked close_issue_tasks for issue 105", len(dry_calls) >= 1)
    check("no non-dry close call in dry-run mode",
          all(c["dry_run"] for c in dry_calls))


def test_orphan_cleanup_already_handled_no_crash():
    """Issue previously closed (tasks already done) does not raise on re-pass."""
    disp = _load_dispatch()

    fp = _FakeProvider(
        open_issues=frozenset(),
        issue_states={105: "closed"},
    )

    with tempfile.TemporaryDirectory() as tmp:
        # list_tasks returns only "done" tasks — the guard sees 0 active and
        # close_issue_tasks returns empty (already handled). Should not raise.
        disp.kanban.ensure_board = lambda s: None
        disp.kanban.list_blocked = lambda s: []
        disp.kanban.list_issue_numbers = lambda s: {105}
        disp.kanban.list_tasks = lambda s: [
            {"id": "t1", "title": "#105 QA verify", "status": "done",
             "assignee": "developer-daedalus"},
        ]
        call_log = []

        def _close(slug, n, summary=None, dry_run=False):
            call_log.append((n, dry_run))
            return []  # nothing to archive (already done)

        disp.kanban.close_issue_tasks = _close
        disp.kanban.dispatch = lambda s, max_spawns=5: True

        try:
            disp.run(
                {"repo": "O/R", "workdir": tmp, "name": "x",
                 "issues": {"filters": {}},
                 "execution": {"staleness_hours": 48},
                 "tracking": {"github_project_number": 1}},
                provider=fp,
            )
            raised = False
        except Exception as e:
            raised = True
            print(f"FAIL: unexpected exception: {e}")

    check("already-handled orphan does not raise", not raised)
    check("close_issue_tasks invoked at least once (idempotent)", len(call_log) >= 1)


# ── Board-Done sync path ─────────────────────────────────────────────────────


def test_board_done_sync_archives_kanban_tasks():
    """Issue moved to Done on VCS board → close_issue_tasks archives its tasks."""
    disp = _load_dispatch()

    fp = _FakeProvider(
        open_issues=frozenset({105}),  # still in open fetch
        board_done=frozenset({105}),   # but marked Done on VCS board
        issue_states={105: "open"},
    )
    tasks_to_close = [
        {"id": "t_a", "title": "QA verify", "status": "done"},
        {"id": "t_b", "title": "Dev impl", "status": "done"},
    ]

    with tempfile.TemporaryDirectory() as tmp:
        close_calls = []

        def _track_close(slug, n, summary=None, dry_run=False):
            close_calls.append((slug, n, summary, dry_run))
            return [t["id"] for t in tasks_to_close]

        disp.kanban.close_issue_tasks = _track_close
        disp.kanban.ensure_board = lambda s: None
        disp.kanban.list_blocked = lambda s: []
        disp.kanban.list_issue_numbers = lambda s: {105}
        disp.kanban.list_tasks = lambda s, status="": []
        disp.kanban.dispatch = lambda s, max_spawns=5: True

        disp.run(
            {"repo": "O/R", "workdir": tmp, "name": "x",
             "issues": {"filters": {}},
             "execution": {"staleness_hours": 48},
             "tracking": {"github_project_number": 1}},
            provider=fp,
        )

    check("close_issue_tasks invoked for board-done issue #105",
          any(n == 105 for _, n, _, _ in close_calls))


def test_board_done_sync_dry_run_does_not_close():
    """Board-Done sync in dry-run mode → close_issue_tasks called with dry_run=True only."""
    disp = _load_dispatch()

    fp = _FakeProvider(
        open_issues=frozenset({105}),
        board_done=frozenset({105}),
        issue_states={105: "open"},
    )

    with tempfile.TemporaryDirectory() as tmp:
        close_calls = []

        def _track_close(slug, n, summary=None, dry_run=False):
            close_calls.append({"n": n, "dry_run": dry_run})
            return []

        disp.kanban.close_issue_tasks = _track_close
        disp.kanban.ensure_board = lambda s: None
        disp.kanban.list_blocked = lambda s: []
        disp.kanban.list_issue_numbers = lambda s: {105}
        disp.kanban.list_tasks = lambda s, status="": []
        disp.kanban.dispatch = lambda s, max_spawns=5: True

        disp.run(
            {"repo": "O/R", "workdir": tmp, "name": "x",
             "issues": {"filters": {}},
             "execution": {"staleness_hours": 48},
             "tracking": {"github_project_number": 1}},
            provider=fp,
            dry_run=True,
        )

    done_calls = [c for c in close_calls if c["n"] == 105]
    check("dry-run invoked close for board-done issue 105", len(done_calls) >= 1)
    check("no non-dry close call in dry-run mode",
          all(c["dry_run"] for c in done_calls))


def test_board_done_sync_skips_already_completed():
    """Issue both on board-Done AND already in completed set → no duplicate archive."""
    disp = _load_dispatch()

    fp = _FakeProvider(
        open_issues=frozenset({105}),
        board_done=frozenset({105}),
        issue_states={105: "closed"},  # VCS reports closed → orphan path will
                                        # also see it; but orphan path's "already
                                        # completed" guard should prevent double-close.
    )

    with tempfile.TemporaryDirectory() as tmp:
        close_calls = []

        def _track_close(slug, n, summary=None, dry_run=False):
            close_calls.append(n)
            return []

        disp.kanban.close_issue_tasks = _track_close
        disp.kanban.ensure_board = lambda s: None
        disp.kanban.list_blocked = lambda s: []
        disp.kanban.list_issue_numbers = lambda s: {105}
        disp.kanban.list_tasks = lambda s, status="": []
        disp.kanban.dispatch = lambda s, max_spawns=5: True

        disp.run(
            {"repo": "O/R", "workdir": tmp, "name": "x",
             "issues": {"filters": {}},
             "execution": {"staleness_hours": 48},
             "tracking": {"github_project_number": 1}},
            provider=fp,
        )

    # The orphan path archives first (state=closed); the board-done path should
    # then skip 105 because it's already in the completed set. Either order is
    # fine: total close calls == 1 for this issue (no double-close).
    check("issue closed via both paths results in exactly one close_issue_tasks call",
          close_calls.count(105) == 1)


# ── Edge cases ───────────────────────────────────────────────────────────────


def test_orphan_cleanup_partial_state_some_issues_not_in_existing():
    """Issues not in the managed 'existing' set are ignored by cleanup paths."""
    disp = _load_dispatch()

    fp = _FakeProvider(
        open_issues=frozenset(),  # issue 999 not in open fetch
        issue_states={999: "closed"},  # VCS says closed
    )

    with tempfile.TemporaryDirectory() as tmp:
        close_calls = []

        def _track_close(slug, n, summary=None, dry_run=False):
            close_calls.append(n)
            return []

        disp.kanban.close_issue_tasks = _track_close
        disp.kanban.ensure_board = lambda s: None
        disp.kanban.list_blocked = lambda s: []
        # 999 NOT in issues_in_board (i.e. not in `existing` set)
        disp.kanban.list_issue_numbers = lambda s: {100}
        disp.kanban.list_tasks = lambda s, status="": []
        disp.kanban.dispatch = lambda s, max_spawns=5: True

        disp.run(
            {"repo": "O/R", "workdir": tmp, "name": "x",
             "issues": {"filters": {}},
             "execution": {"staleness_hours": 48},
             "tracking": {"github_project_number": 1}},
            provider=fp,
        )

    check("cleanup skipped issue 999 (not in managed existing set)",
          999 not in close_calls)


def test_orphan_cleanup_mixed_issues_some_closed_some_open():
    """Mixed state: one issue closed, one open → only the closed one is archived."""
    disp = _load_dispatch()

    fp = _FakeProvider(
        open_issues=frozenset({106}),  # 106 still in open fetch
        issue_states={105: "closed", 106: "open"},
    )

    with tempfile.TemporaryDirectory() as tmp:
        close_calls = []

        def _track_close(slug, n, summary=None, dry_run=False):
            close_calls.append(n)
            return []

        disp.kanban.close_issue_tasks = _track_close
        disp.kanban.ensure_board = lambda s: None
        disp.kanban.list_blocked = lambda s: []
        disp.kanban.list_issue_numbers = lambda s: {105, 106}
        disp.kanban.list_tasks = lambda s, status="": []
        disp.kanban.dispatch = lambda s, max_spawns=5: True

        disp.run(
            {"repo": "O/R", "workdir": tmp, "name": "x",
             "issues": {"filters": {}},
             "execution": {"staleness_hours": 48},
             "tracking": {"github_project_number": 1}},
            provider=fp,
        )

    check("closed issue 105 archived", 105 in close_calls)
    check("open issue 106 NOT archived", 106 not in close_calls)


# ── runner ───────────────────────────────────────────────────────────────────


def main():
    tests = [
        test_orphan_cleanup_archives_tasks_when_issue_closed_no_active,
        test_orphan_cleanup_skips_when_active_tasks_exist,
        test_orphan_cleanup_skips_unknown_or_filtered_state,
        test_orphan_cleanup_dry_run_does_not_close,
        test_orphan_cleanup_already_handled_no_crash,
        test_board_done_sync_archives_kanban_tasks,
        test_board_done_sync_dry_run_does_not_close,
        test_board_done_sync_skips_already_completed,
        test_orphan_cleanup_partial_state_some_issues_not_in_existing,
        test_orphan_cleanup_mixed_issues_some_closed_some_open,
    ]

    print("orphaned card handling behavior tests")
    print("=" * 60)
    for fn in tests:
        name = fn.__name__
        try:
            fn()
            print(f"  ✓ {name}")
        except AssertionError as e:
            print(f"  ✗ {name}: {e}")
            raise
        except Exception as e:
            print(f"  ✗ {name} (unexpected): {e}")
            raise
    print()
    print(f"  {len(tests)}/{len(tests)} tests passed")


if __name__ == "__main__":
    main()
