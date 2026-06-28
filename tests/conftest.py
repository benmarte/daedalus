"""Shared pytest fixtures for Daedalus pipeline scenario tests (issue #118).

Provides in-memory doubles for the kanban board and VCS provider so that
full pipeline stage-sequences can be driven end-to-end without any network,
subprocess, or filesystem access. Every scenario gets a fresh, isolated board.

The two collaborators the pipeline talks to are:
  * ``core.kanban``      — the board (list/create/complete/block/...).
  * a VCS provider       — CI status, issue lookup, PR lookup.

``FakeKanban`` and ``FakeProvider`` stand in for those. The ``pipeline``
fixture wires a single shared ``FakeKanban`` into both the dispatcher and the
``core.iterate`` module so a card created by one stage is visible to the next.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load_dispatch():
    """Load scripts/daedalus_dispatch.py as a standalone module named 'disp'."""
    p = ROOT / "scripts" / "daedalus_dispatch.py"
    spec = importlib.util.spec_from_file_location("disp", str(p))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── shared hand-rolled assertion printer ──────────────────────────────────────
# Several legacy suites double as standalone scripts (``python tests/test_x.py``)
# and print PASS/FAIL via ``check`` while tallying ``_passed``/``_failed``. This
# is the single source of truth; the suites import it from here. The counters are
# module-level so a standalone ``__main__`` block can read ``conftest._failed``.

_passed = 0
_failed = 0


def check(name, cond):
    """Record and print a PASS/FAIL line; tally into module-level counters."""
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {name}")
    else:
        _failed += 1
        print(f"  FAIL  {name}")


# ── in-memory kanban double ───────────────────────────────────────────────────


class FakeKanban:
    """In-memory kanban board recording every mutating call.

    Mirrors the subset of ``core.kanban`` the pipeline uses. Each call that the
    production code makes is recorded so tests can assert on what the pipeline
    did (``completed``, ``blocked_calls``, ``unblocked_calls``, ``created``,
    ``comments``).

    Seed pre-existing cards with :meth:`seed` / :meth:`add` — those do NOT count
    as pipeline mutations, so ``completed``/``blocked_calls`` reflect only what
    the code under test triggered.
    """

    def __init__(self) -> None:
        self.tasks: Dict[str, Dict[str, Any]] = {}
        self._counter = 0
        self.completed: List[tuple] = []
        self.blocked_calls: List[tuple] = []
        self.unblocked_calls: List[tuple] = []
        self.created: List[Dict[str, Any]] = []
        self.comments: List[tuple] = []
        self.archived: List[str] = []

    # ---- seeding (not counted as pipeline mutations) ----

    def seed(
        self,
        *,
        assignee: str,
        title: str,
        status: str = "running",
        summary: str = "",
        body: str = "",
        idempotency_key: str = "",
        reason: str = "",
        tid: Optional[str] = None,
    ) -> str:
        """Insert a card directly onto the board and return its id."""
        self._counter += 1
        tid = tid or f"t{self._counter}"
        self.tasks[tid] = {
            "id": tid,
            "assignee": assignee,
            "title": title,
            "status": status,
            "summary": summary,
            "latest_summary": reason or summary,
            "body": body,
            "idempotency_key": idempotency_key,
            "reason": reason,
            "comments": [],
        }
        return tid

    def add(self, card: Dict[str, Any]) -> str:
        """Insert a pre-built card dict (e.g. from ``fake_blocked_card``)."""
        tid = card["id"]
        card.setdefault("comments", [])
        card.setdefault("latest_summary", card.get("reason", ""))
        self.tasks[tid] = card
        return tid

    # ---- recorded API used by the pipeline ----

    def list_tasks(self, slug: str, status: Optional[str] = None) -> List[Dict[str, Any]]:
        out = []
        for t in self.tasks.values():
            if status is not None and (t.get("status") or "") != status:
                continue
            out.append(dict(t))
        return out

    def list_blocked(self, slug: str) -> List[Dict[str, Any]]:
        return [dict(t) for t in self.tasks.values() if (t.get("status") or "") == "blocked"]

    def get_latest_summary(self, slug: str, task_id: str) -> str:
        t = self.tasks.get(task_id) or {}
        return t.get("latest_summary") or t.get("summary") or ""

    def show_card(self, slug: str, task_id: str) -> Optional[Dict[str, Any]]:
        t = self.tasks.get(task_id)
        return dict(t) if t else None

    def create_task(
        self,
        slug: str,
        title: str,
        *,
        body: str = "",
        assignee: str = "",
        workspace: str = "",
        idempotency_key: str = "",
        parents: Optional[List[str]] = None,
        skills: Optional[List[str]] = None,
        goal: bool = False,
        goal_max_turns: Optional[int] = None,
        max_retries: Optional[int] = None,
    ) -> Optional[str]:
        # Idempotency: the real CLI returns the existing id for a known key.
        if idempotency_key:
            for t in self.tasks.values():
                if t.get("idempotency_key") == idempotency_key:
                    return t["id"]
        self._counter += 1
        tid = f"t{self._counter}"
        rec = {
            "id": tid,
            "title": title,
            "body": body,
            "assignee": assignee,
            "status": "running",
            "summary": "",
            "latest_summary": "",
            "idempotency_key": idempotency_key,
            "workspace": workspace,
            "parents": parents or [],
            "reason": "",
            "comments": [],
        }
        self.tasks[tid] = rec
        self.created.append(dict(rec))
        return tid

    def complete(self, slug: str, task_id: str, summary: str = "") -> bool:
        t = self.tasks.get(task_id)
        if not t:
            return False
        t["status"] = "done"
        t["summary"] = summary
        t["latest_summary"] = summary
        self.completed.append((task_id, summary))
        return True

    def block_task(self, slug: str, task_id: str, reason: str = "") -> bool:
        t = self.tasks.get(task_id)
        if not t:
            return False
        t["status"] = "blocked"
        t["reason"] = reason
        t["latest_summary"] = reason
        self.blocked_calls.append((task_id, reason))
        return True

    def unblock_task(self, slug: str, task_id: str, reason: str = "") -> bool:
        t = self.tasks.get(task_id)
        if not t:
            return False
        t["status"] = "running"
        self.unblocked_calls.append((task_id, reason))
        return True

    def comment(self, slug: str, task_id: str, body: str) -> bool:
        t = self.tasks.get(task_id)
        if t is not None:
            t.setdefault("comments", []).append({"body": body})
        self.comments.append((task_id, body))
        return True

    def archive_task(self, slug: str, task_id: str) -> bool:
        t = self.tasks.get(task_id)
        if not t:
            return False
        t["status"] = "archived"
        self.archived.append(task_id)
        return True

    # ---- assertion helpers ----

    def created_with_key(self, idempotency_key: str) -> Optional[Dict[str, Any]]:
        """Return the created card whose idempotency key matches, or None."""
        for t in self.tasks.values():
            if t.get("idempotency_key") == idempotency_key:
                return t
        return None

    def comments_on(self, task_id: str) -> List[str]:
        return [body for (tid, body) in self.comments if tid == task_id]


# ── in-memory VCS provider double ─────────────────────────────────────────────


class FakeProvider:
    """Configurable VCS provider double — CI status, issue list, PR lookup.

    ``ci_status`` may be a single status string (applied to every PR) or a
    ``{pr_number: status}`` dict. ``issues`` maps issue number → IssueSummary
    (or any object with ``.as_dict()``). ``branch_prs`` maps branch → PR number.
    """

    def __init__(
        self,
        *,
        name: str = "github",
        ci_status: Any = "green",
        issues: Optional[Dict[int, Any]] = None,
        branch_prs: Optional[Dict[str, int]] = None,
        supports_ci_status: bool = True,
        blockers: Optional[Dict[int, List[int]]] = None,
        get_issue_failures: int = 0,
        closed_issues: Optional[set[int]] = None,
        close_issue_fail_for: Optional[set[int]] = None,
    ) -> None:
        self.name = name
        self._ci = ci_status
        self._issues = issues or {}
        self._branch_prs = branch_prs or {}
        self.supports_ci_status = supports_ci_status
        self._blockers = blockers or {}
        # Number of leading get_issue calls that return None before serving the
        # issue — models a transient outage that recovers (issue #185).
        self._get_issue_failures = get_issue_failures
        self.get_issue_calls = 0
        self.posted_issue_comments: List[tuple] = []
        self.posted_pr_comments: List[tuple] = []
        self.merged: List[tuple] = []
        self.close_calls: List[int] = []
        self._closed_issues: set[int] = set(closed_issues or [])
        self._close_issue_fail_for: set[int] = set(close_issue_fail_for or [])

    def get_pr_ci_status(self, pr_number: int) -> str:
        if isinstance(self._ci, dict):
            return self._ci.get(pr_number, "unknown")
        return self._ci

    def pr_ci_green(self, pr_number: int) -> bool:
        return self.get_pr_ci_status(pr_number) == "green"

    def find_pr_for_branch(self, branch: str) -> Optional[int]:
        return self._branch_prs.get(branch)

    def get_issue(self, issue_number: int) -> Any:
        self.get_issue_calls += 1
        if self._get_issue_failures > 0:
            self._get_issue_failures -= 1
            return None
        return self._issues.get(issue_number)

    def blockers(self, issue_number: int) -> List[int]:
        return list(self._blockers.get(issue_number, []))

    def list_issues(self, *args: Any, **kwargs: Any) -> List[Any]:
        return list(self._issues.values())

    def post_issue_comment(self, issue_number: int, body: str) -> bool:
        self.posted_issue_comments.append((issue_number, body))
        return True

    def post_pr_comment(self, pr_number: int, body: str) -> bool:
        self.posted_pr_comments.append((pr_number, body))
        return True

    def get_issue_state(self, issue_number: int) -> str:
        """Mock get_issue_state — return 'closed' if in _closed_issues, else 'open'."""
        return "closed" if issue_number in self._closed_issues else "open"

    def close_issue(self, issue_number: int) -> bool:
        """Mock close_issue — record call, simulate failure if requested."""
        # Already-closed → short-circuit (no API call, no recording)
        if issue_number in self._closed_issues:
            return True
        self.close_calls.append(issue_number)
        if issue_number in self._close_issue_fail_for:
            return False
        self._closed_issues.add(issue_number)
        return True

    def merge_pr(self, pr_number: int, merge_method: str = "squash") -> bool:
        self.merged.append((pr_number, merge_method))
        return True

    def board_configured(self) -> bool:
        return False


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def fake_issue():
    """Factory: build an issue dict in the shape the dispatcher consumes."""

    def _make(number: int, title: str = "Test issue", body: str = "") -> Dict[str, Any]:
        return {
            "number": number,
            "title": title,
            "body": body,
            "labels": [],
            "url": f"https://example.com/issues/{number}",
        }

    return _make


@pytest.fixture
def fake_blocked_card():
    """Factory: build a blocked card dict (handoff carried in ``reason``)."""

    def _make(
        tid: str,
        assignee: str,
        handoff: str,
        *,
        title: str = "",
        body: str = "",
    ) -> Dict[str, Any]:
        return {
            "id": tid,
            "assignee": assignee,
            "status": "blocked",
            "reason": handoff,
            "latest_summary": handoff,
            "title": title,
            "body": body,
        }

    return _make


@pytest.fixture
def fake_kanban():
    """A fresh, isolated in-memory kanban board per test."""
    return FakeKanban()


@pytest.fixture
def fake_provider():
    """Factory for a configurable ``FakeProvider``."""

    def _make(**kwargs: Any) -> FakeProvider:
        return FakeProvider(**kwargs)

    return _make


@pytest.fixture
def pipeline(fake_kanban, monkeypatch):
    """Wire a shared FakeKanban into the dispatcher and core.iterate.

    Returns a namespace with ``.disp`` (dispatcher module), ``.iterate``
    (core.iterate module) and ``.kanban`` (the shared FakeKanban) so a scenario
    can drive real pipeline functions against one in-memory board.
    """
    disp = _load_dispatch()
    from core import iterate

    monkeypatch.setattr(disp, "kanban", fake_kanban)
    monkeypatch.setattr(iterate, "kanban", fake_kanban)
    return SimpleNamespace(disp=disp, iterate=iterate, kanban=fake_kanban)
