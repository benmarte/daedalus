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
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ── isolate the dispatcher process-mutex FileLock per xdist worker (issue #1198)
# daedalus_dispatch.main() acquires a host-global FileLock at _MUTEX_LOCK_PATH.
# Under ``pytest -n auto`` every xdist worker is a separate process on the same
# host, so two workers that each call main() collide on that one lock: the loser
# gets Timeout and returns early WITHOUT running _main_inner(), which flakes any
# test asserting on dispatch side effects (e.g. test_main_scopes_to_cwd_project).
# Point the lock at a unique per-worker file so workers never contend. Set at
# conftest import — before any test module loads ``disp`` — and honored by the
# ``DAEDALUS_DISPATCH_LOCK`` override in daedalus_dispatch, so every freshly
# re-exec'd module (see _load_dispatch) picks it up. Dedicated lock-contention
# suites still override _MUTEX_LOCK_PATH directly, so they are unaffected.
if "DAEDALUS_DISPATCH_LOCK" not in os.environ:
    _worker = os.environ.get("PYTEST_XDIST_WORKER", "master")
    os.environ["DAEDALUS_DISPATCH_LOCK"] = str(
        Path(tempfile.gettempdir()) / f".daedalus_dispatch_test_{_worker}.lock"
    )


@pytest.fixture(autouse=True)
def _isolate_hermes_home(tmp_path, monkeypatch):
    """Guarantee NO test ever writes to the real ~/.hermes kanban board.

    A test that exercises the real dispatcher or the ``hermes kanban`` CLI
    without an isolated HERMES_HOME creates cards on the LIVE board; the running
    gateway then executes them, spawning real agents that create more cards — a
    runaway loop (2026-07-02 incident, e.g. ``disp.run(dry_run=False)`` in
    test_profile_resync_integration.py). Forcing HERMES_HOME to a throwaway dir
    for every test makes that impossible. Tests that set their own (tmp)
    HERMES_HOME inside ``with`` blocks still override this — also isolated.
    """
    home = tmp_path / "hermes-home"
    (home / "kanban").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "test-isolated")
    yield


def _load_dispatch():
    """Load scripts/daedalus_dispatch.py as a standalone module named 'disp'."""
    p = ROOT / "scripts" / "daedalus_dispatch.py"
    spec = importlib.util.spec_from_file_location("disp", str(p))
    mod = importlib.util.module_from_spec(spec)
    # Register before exec so the module can resolve itself via sys.modules
    # (e.g. main()'s ``sys.modules[__name__]`` for the --self-test harness) — the
    # idiomatic importlib pattern. Each call replaces the entry with a fresh
    # module; callers keep their own reference, so isolation is unchanged.
    sys.modules["disp"] = mod
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
        self.decomposed: List[str] = []

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
        # Preserve latest_summary when no new summary is provided — mirrors
        # the real kanban CLI which doesn't overwrite on empty summary.
        if summary:
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

    def create_triage(
        self,
        slug: str,
        issue_number: Optional[int],
        title: str,
        body: str = "",
        idempotency_key: Optional[str] = None,
        workspace: Optional[str] = None,
        **_kwargs: Any,
    ) -> Optional[str]:
        """Create a TRIAGE card for a (sub-)issue and return its id (#891).

        Honours ``idempotency_key`` like the real CLI so a re-issued decompose
        returns the existing card rather than a duplicate.
        """
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
            "assignee": "",
            "status": "triage",
            "summary": "",
            "latest_summary": "",
            "idempotency_key": idempotency_key or "",
            "workspace": workspace or "",
            "issue_number": issue_number,
            "reason": "",
            "comments": [],
        }
        self.tasks[tid] = rec
        self.created.append(dict(rec))
        return tid

    def decompose(self, slug: str, task_id: str) -> bool:
        """Fan a triage card out into role sub-tasks. Records the call (#891)."""
        t = self.tasks.get(task_id)
        if not t:
            return False
        t["status"] = "decomposed"
        self.decomposed.append(task_id)
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
        open_prs: Optional[set[int]] = None,
        merged_prs: Optional[set[int]] = None,
        get_issue_failures: int = 0,
        closed_issues: Optional[set[int]] = None,
        close_issue_fail_for: Optional[set[int]] = None,
        post_issue_comment_fail_for: Optional[set[int]] = None,
    ) -> None:
        self.name = name
        self._ci = ci_status
        self._issues = issues or {}
        self._branch_prs = branch_prs or {}
        self.supports_ci_status = supports_ci_status
        self._blockers = blockers or {}
        # #953: set of open PR numbers for the pre-QA gate. None (default) means
        # "assume any resolved PR is open" — preserves pre-#953 advance tests.
        self._open_prs = open_prs
        # #957: set of merged PR numbers. None (default) means "no PR is merged"
        # so is_pr_merged returns False — preserves pre-#957 behaviour.
        self._merged_prs = merged_prs
        # Number of leading get_issue calls that return None before serving the
        # issue — models a transient outage that recovers (issue #185).
        self._get_issue_failures = get_issue_failures
        self.get_issue_calls = 0
        self.get_issue_comments_calls = 0
        self.posted_issue_comments: List[tuple] = []
        self.posted_pr_comments: List[tuple] = []
        self.merged: List[tuple] = []
        self.close_calls: List[int] = []
        self._closed_issues: set[int] = set(closed_issues or [])
        self._close_issue_fail_for: set[int] = set(close_issue_fail_for or [])
        self._post_comment_fail_for: set[int] = set(post_issue_comment_fail_for or [])
        # Sub-issue decomposition (#891 / #902): create_issue registers a fresh
        # issue into ``_issues`` (so get_issue/get_issue_comments see it) and
        # records it here; add_label tracks labels per issue number.
        self.created_issues: List[Dict[str, Any]] = []
        self.labels: Dict[int, List[str]] = {}

    def get_pr_ci_status(self, pr_number: int) -> str:
        if isinstance(self._ci, dict):
            return self._ci.get(pr_number, "unknown")
        return self._ci

    def pr_ci_green(self, pr_number: int) -> bool:
        return self.get_pr_ci_status(pr_number) == "green"

    def find_pr_for_branch(self, branch: str) -> Optional[int]:
        return self._branch_prs.get(branch)

    def is_pr_open(self, pr_number: int) -> bool:
        # Default (no open_prs configured): treat every PR as open so existing
        # advance tests are unaffected. When configured, membership decides.
        if self._open_prs is None:
            return True
        return pr_number in self._open_prs

    def is_pr_merged(self, pr_number: int) -> bool:
        # #957: only the explicitly-configured merged set counts. Default None
        # → no PR is merged, so pre-#957 tests are unaffected.
        if self._merged_prs is None:
            return False
        return pr_number in self._merged_prs

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

    def create_issue(self, title: str, body: str = "", labels: Optional[List[str]] = None) -> int:
        """Allocate the next issue number, register and record the new issue.

        Mirrors the provider call ``_execute_planner_decompose_inner`` makes for
        each sub-issue (#891). The new issue is added to ``_issues`` so a later
        ``get_issue``/``get_issue_comments`` for the triage step resolves it, and
        appended to ``created_issues`` so tests can assert how many were created.
        """
        n = (max(self._issues) if self._issues else 1000) + 1
        rec = {"number": n, "title": title, "body": body, "labels": list(labels or [])}
        self._issues[n] = rec
        self.created_issues.append(rec)
        if labels:
            self.labels.setdefault(n, []).extend(labels)
        return n

    def add_label(self, issue_number: int, label: str) -> bool:
        """Record a label applied to an issue (e.g. ``Ready`` / ``epic``, #891)."""
        self.labels.setdefault(issue_number, []).append(label)
        return True

    def has_label(self, issue_number: int, label_name: str) -> bool:
        """Return True if ``label_name`` is present for ``issue_number`` (#998)."""
        return label_name in self.labels.get(issue_number, [])

    def get_issue_comments(self, issue_number: int) -> List[Dict[str, str]]:
        """Return comments previously posted to *issue_number* via this provider.

        Decompose's second-pass idempotency check reads these to detect the
        marker comment it posted on the first pass (#891).
        """
        self.get_issue_comments_calls += 1
        return [{"body": body} for (n, body) in self.posted_issue_comments if n == issue_number]

    def post_issue_comment(self, issue_number: int, body: str) -> bool:
        self.posted_issue_comments.append((issue_number, body))
        if issue_number in self._post_comment_fail_for:
            return False
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


# ── multi-tick pipeline harness (issue #901) ──────────────────────────────────
# A reusable driver that runs N *real* dispatcher ticks over the shared in-memory
# board, simulating each role's agent between ticks, and records the stage
# progression so tests can assert the pipeline advances one stage per tick and
# reaches a terminal ``done`` state without sticking or looping. Where
# ``test_e2e_full_pipeline`` hand-drives every handoff in sequence, this harness
# generalises that into an N-tick loop other suites (e.g. #902) can build on.

# role name → assignee profile (mirrors the live ``*-daedalus`` roster).
PIPELINE_ROLES = {
    "validator": "validator-daedalus",
    "pm": "project-manager-daedalus",
    "developer": "developer-daedalus",
    "qa": "qa-daedalus",
    "reviewer": "reviewer-daedalus",
    "security": "security-analyst-daedalus",
    "accessibility": "accessibility-daedalus",
    "docs": "documentation-daedalus",
}

# Dependency order the live pipeline advances in. The harness always drives the
# earliest-in-order card still ``running`` (the frontier), so downstream roles
# (qa, reviewer, …) never hand off before the developer's PR exists — even though
# all five team cards are created ``running`` at PM-spec time.
STAGE_ORDER = [
    "validator", "pm", "developer", "qa",
    "reviewer", "security", "accessibility", "docs",
]

# Reverse map: assignee profile → role name.
_ASSIGNEE_ROLE = {profile: role for role, profile in PIPELINE_ROLES.items()}


def _role_handoff(role: str, *, issue: int, repo: str, pr: int) -> tuple:
    """Return ``(action, signal)`` for a role's simulated agent.

    ``action`` is ``"complete"`` (validator/PM emit a done-card summary the
    dispatcher reads) or ``"block"`` (team roles emit a ``review-required:``
    handoff the dispatcher auto-advances). The signal strings mirror the live
    handoffs exercised by ``test_e2e_full_pipeline``.
    """
    signals = {
        "validator": ("complete", "CONFIRMED: reproduced on main; scope is clear"),
        "pm": ("complete", "SPEC: acceptance criteria defined"),
        "developer": ("block", f"review-required: PR #{pr} opened for {repo}#{issue}"),
        "qa": ("block", f"review-required: qa-passed: PR #{pr} — suite green"),
        "reviewer": ("block", f"review-required: No findings. Approved for merge. PR #{pr}"),
        "security": ("block", f"review-required: No findings. Approved for merge. PR #{pr}"),
        "accessibility": ("block", f"review-required: a11y-skipped: no UI changes. PR #{pr}"),
        "docs": ("block", f"review-required: docs posted: issue #{issue} PR #{pr} — README updated"),
    }
    return signals[role]


class MultiTickHarness:
    """Drive a seeded issue through the pipeline by running real dispatcher ticks.

    Each :meth:`tick` (1) simulates the *frontier* role's agent — completing the
    validator/PM card or blocking a team card with its ``review-required:``
    handoff — then (2) runs one real dispatcher pass (``run_iterate`` →
    ``_check_confirmed_validators`` → ``_check_completed_pm``) over the shared
    in-memory board, in the same order the live ``run()`` does. ``stage_log``
    records the role processed each tick so callers can assert stage progression.
    """

    def __init__(
        self,
        pipeline: Any,
        provider: Any,
        *,
        repo: str = "benmarte/daedalus",
        slug: str = "proj",
        issue: int = 901,
        pr: int = 5901,
    ) -> None:
        self.disp = pipeline.disp
        self.iterate = pipeline.iterate
        self.kanban = pipeline.kanban
        self.provider = provider
        self.repo = repo
        self.slug = slug
        self.issue = issue
        self.pr = pr
        self.issues_map: Dict[int, Dict[str, Any]] = {}
        self.stage_log: List[str] = []

    # ---- setup ----

    def seed(self, issue_card: Dict[str, Any]) -> None:
        """Seed the issue's validator card (``running``) and register the issue."""
        self.issues_map = {self.issue: issue_card}
        self.kanban.seed(
            assignee=PIPELINE_ROLES["validator"],
            title=f"#{self.issue} {issue_card['title']}",
            status="running",
        )

    # ---- one tick ----

    def _frontier(self) -> tuple:
        """Return ``(role, card)`` for the earliest-in-order ``running`` card."""
        running: Dict[str, Dict[str, Any]] = {}
        for t in self.kanban.tasks.values():
            if (t.get("status") or "") != "running":
                continue
            role = _ASSIGNEE_ROLE.get(t.get("assignee"))
            if role is not None:
                running.setdefault(role, t)
        for role in STAGE_ORDER:
            if role in running:
                return role, running[role]
        return None, None

    def _simulate_agent(self, role: str, card: Dict[str, Any]) -> None:
        action, signal = _role_handoff(role, issue=self.issue, repo=self.repo, pr=self.pr)
        if action == "complete":
            self.kanban.complete(self.slug, card["id"], signal)
        else:
            self.kanban.block_task(self.slug, card["id"], signal)

    def _dispatch_pass(self) -> None:
        """Run one dispatcher pass over the board (iterate → validator → PM)."""
        self.iterate.run_iterate(self.slug, self.repo, provider=self.provider)
        self.disp._check_confirmed_validators(
            self.slug, self.repo, self.issues_map, 3, "", "", "dev", "github",
        )
        self.disp._check_completed_pm(
            self.slug, self.repo, self.issues_map, 3, "", "", "dev", "github",
        )

    def tick(self) -> Optional[str]:
        """Simulate the frontier agent (if any) then run one dispatcher pass.

        Returns the role processed this tick, or ``None`` when the board is idle
        — terminal or idempotent, where the dispatcher pass is a pure no-op.
        """
        role, card = self._frontier()
        if role is not None:
            self._simulate_agent(role, card)
            self.stage_log.append(role)
        self._dispatch_pass()
        return role

    def run(self, max_ticks: int = 20) -> List[str]:
        """Tick until the board is terminal or the budget runs out.

        Returns ``stage_log``. Stops as soon as a tick finds no frontier (every
        pipeline card is ``done``), so a clean run never burns the full budget.
        """
        for _ in range(max_ticks):
            if self.tick() is None:
                break
        return self.stage_log

    # ---- assertions ----

    def pipeline_cards(self) -> List[Dict[str, Any]]:
        """Every card assigned to a pipeline role (validator … docs)."""
        return [t for t in self.kanban.tasks.values()
                if _ASSIGNEE_ROLE.get(t.get("assignee")) is not None]

    def all_done(self) -> bool:
        """True when every pipeline card has reached ``done``."""
        cards = self.pipeline_cards()
        return bool(cards) and all((t.get("status") or "") == "done" for t in cards)

    def created_count(self) -> int:
        return len(self.kanban.created)


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


@pytest.fixture
def multi_tick_harness(pipeline, fake_provider):
    """Factory for a :class:`MultiTickHarness` wired to the shared board.

    Builds a green-CI provider, constructs the harness, and seeds the issue's
    validator card so the caller can start ticking immediately.
    """

    def _make(issue_card: Dict[str, Any], *, issue: int = 901, **kwargs: Any) -> MultiTickHarness:
        provider = fake_provider(ci_status=kwargs.pop("ci_status", "green"))
        h = MultiTickHarness(pipeline, provider, issue=issue, **kwargs)
        h.seed(issue_card)
        return h

    return _make
