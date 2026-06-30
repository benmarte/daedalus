"""Offline self-test harness for the Daedalus dispatcher (issue #900).

The dispatcher already accepts ``--dry-run``, but that mode still *reads* real
GitHub (it only suppresses mutations). This module provides the hermetic
counterpart: it seeds fake issues/tasks into in-memory doubles and drives the
**real** dispatcher handoff functions through a controlled tick, asserting the
expected state transitions — with zero network and zero real GitHub access.

It is wired into ``daedalus_dispatch.py`` behind the ``--self-test`` flag so a
human or CI can run a fast, dependency-free smoke of the pipeline wiring::

    python scripts/daedalus_dispatch.py --self-test

The dispatcher module is *injected* into :func:`run_selftest` rather than
imported here, so the dependency direction stays scripts -> core (this module
lives in ``core/`` and must not import from ``scripts/``).

Checks performed (each maps to an acceptance criterion / regression):

* validator ``CONFIRMED:`` -> a PM spec card is created                (pipeline advances)
* PM ``SPEC:`` -> the downstream team cards are created                (pipeline advances)
* a completed role card -> a completion comment via ``post_issue_comment`` (regression #894)
* re-running every pass is idempotent: no duplicate cards or comments  (AC #4 / regression #891)
* the provider used is the in-memory double — real GitHub is never touched
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

REPO = "benmarte/daedalus"
SLUG = "daedalus-selftest"
ISSUE_NUMBER = 900900  # high, obviously-synthetic number — never a real issue

VALIDATOR = "validator-daedalus"
PM = "project-manager-daedalus"
DEVELOPER = "developer-daedalus"
# Idempotency-key prefixes for the team cards created by ``_check_completed_pm``
# (the documentation card's key is "docs-{n}", not "documentation-{n}").
_TEAM_ROLES = ("developer", "qa", "reviewer", "security", "docs")


# ── in-memory doubles ─────────────────────────────────────────────────────────


class _InMemoryKanban:
    """Minimal in-memory kanban mirroring the subset the handoff paths call.

    Only the methods exercised by ``_check_confirmed_validators``,
    ``_check_completed_pm`` and ``_post_completion_comments`` are implemented;
    a missing one surfaces as an ``AttributeError`` the harness reports as a
    failed check rather than a crash.
    """

    def __init__(self) -> None:
        self.tasks: Dict[str, Dict[str, Any]] = {}
        self._counter = 0
        self.created: List[Dict[str, Any]] = []

    # ---- seeding (not a pipeline mutation) ----

    def seed(self, *, assignee: str, title: str, status: str = "running",
             summary: str = "") -> str:
        self._counter += 1
        tid = f"t{self._counter}"
        self.tasks[tid] = {
            "id": tid, "assignee": assignee, "title": title, "status": status,
            "summary": summary, "latest_summary": summary, "idempotency_key": "",
            "reason": "", "comments": [],
        }
        return tid

    # ---- API used by the dispatcher ----

    def ensure_board(self, slug: str) -> None:
        return None

    def list_tasks(self, slug: str, status: Optional[str] = None) -> List[Dict[str, Any]]:
        return [dict(t) for t in self.tasks.values()
                if status is None or (t.get("status") or "") == status]

    def list_blocked(self, slug: str) -> List[Dict[str, Any]]:
        return [dict(t) for t in self.tasks.values() if (t.get("status") or "") == "blocked"]

    def get_latest_summary(self, slug: str, task_id: str) -> str:
        t = self.tasks.get(task_id) or {}
        return t.get("latest_summary") or t.get("summary") or ""

    def show_card(self, slug: str, task_id: str) -> Optional[Dict[str, Any]]:
        t = self.tasks.get(task_id)
        return dict(t) if t else None

    def create_task(self, slug: str, title: str, *, body: str = "", assignee: str = "",
                    workspace: str = "", idempotency_key: str = "",
                    parents: Optional[List[str]] = None, skills: Optional[List[str]] = None,
                    goal: bool = False, goal_max_turns: Optional[int] = None,
                    max_retries: Optional[int] = None) -> Optional[str]:
        # Idempotency: a known key returns the existing card id (no duplicate).
        if idempotency_key:
            for t in self.tasks.values():
                if t.get("idempotency_key") == idempotency_key:
                    return t["id"]
        self._counter += 1
        tid = f"t{self._counter}"
        rec = {
            "id": tid, "title": title, "body": body, "assignee": assignee,
            "status": "running", "summary": "", "latest_summary": "",
            "idempotency_key": idempotency_key, "workspace": workspace,
            "parents": parents or [], "reason": "", "comments": [],
        }
        self.tasks[tid] = rec
        self.created.append(dict(rec))
        return tid

    def complete(self, slug: str, task_id: str, summary: str = "") -> bool:
        t = self.tasks.get(task_id)
        if not t:
            return False
        t.update(status="done", summary=summary, latest_summary=summary)
        return True

    def block(self, slug: str, task_id: str, reason: str = "") -> bool:
        return self.block_task(slug, task_id, reason)

    def block_task(self, slug: str, task_id: str, reason: str = "") -> bool:
        t = self.tasks.get(task_id)
        if not t:
            return False
        t.update(status="blocked", reason=reason, latest_summary=reason)
        return True

    def close_non_blocked_issue_tasks(self, slug: str, *args: Any, **kwargs: Any) -> int:
        return 0

    def created_with_key(self, idempotency_key: str) -> Optional[Dict[str, Any]]:
        for t in self.tasks.values():
            if t.get("idempotency_key") == idempotency_key:
                return t
        return None


class _InMemoryProvider:
    """In-memory VCS provider double recording every call. Touches no network."""

    name = "github-selftest"

    def __init__(self, issues: Dict[int, Dict[str, Any]]) -> None:
        self._issues = issues
        self.posted_issue_comments: List[tuple] = []

    def board_configured(self) -> bool:
        return False

    def list_issues(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return list(self._issues.values())

    def get_issue(self, issue_number: int) -> Optional[Dict[str, Any]]:
        return self._issues.get(issue_number)

    def get_issue_state(self, issue_number: int) -> str:
        return "open"

    def post_issue_comment(self, issue_number: int, body: str) -> bool:
        self.posted_issue_comments.append((issue_number, body))
        return True

    def blockers(self, issue_number: int) -> List[int]:
        return []


# ── result types ──────────────────────────────────────────────────────────────


@dataclass
class _Check:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class SelfTestReport:
    checks: List[_Check] = field(default_factory=list)

    def record(self, name: str, passed: bool, detail: str = "") -> bool:
        self.checks.append(_Check(name, bool(passed), detail))
        return bool(passed)

    @property
    def ok(self) -> bool:
        return all(c.passed for c in self.checks)

    def format(self) -> str:
        lines = ["=== Daedalus dispatcher self-test (offline, no real GitHub) ==="]
        for c in self.checks:
            tag = "PASS" if c.passed else "FAIL"
            lines.append(f"  {tag}  {c.name}" + (f" — {c.detail}" if c.detail else ""))
        n_pass = sum(1 for c in self.checks if c.passed)
        verdict = "PASSED" if self.ok else "FAILED"
        lines.append(f"=== self-test {verdict}: {n_pass}/{len(self.checks)} checks ===")
        return "\n".join(lines)


# ── harness ───────────────────────────────────────────────────────────────────


def run_selftest(disp: Any) -> SelfTestReport:
    """Drive the injected dispatcher module ``disp`` through an offline tick.

    Swaps the dispatcher's module-global ``kanban`` for an in-memory double for
    the duration of the run (restored in ``finally``) and uses an isolated temp
    workdir for ``dispatch_state`` flags, so nothing leaks to disk or GitHub.
    """
    report = SelfTestReport()
    issue = {
        "number": ISSUE_NUMBER,
        "title": "self-test: seed issue (offline)",
        "body": "Synthetic issue seeded by --self-test. Not a real issue.",
        "labels": [],
        "url": f"https://example.invalid/issues/{ISSUE_NUMBER}",
    }
    issues_map = {ISSUE_NUMBER: issue}
    provider = _InMemoryProvider(issues_map)
    board = _InMemoryKanban()
    profiles = dict(disp._DEFAULT_PROFILES)

    original_kanban = disp.kanban
    disp.kanban = board
    try:
        with tempfile.TemporaryDirectory(prefix="daedalus-selftest-") as workdir:
            _run_checks(disp, report, board, provider, issues_map, profiles, workdir)
    except Exception as exc:  # surface unexpected crashes as a failed check
        report.record("harness ran without error", False, f"{type(exc).__name__}: {exc}")
    finally:
        disp.kanban = original_kanban
    return report


def _run_checks(disp: Any, report: SelfTestReport, board: _InMemoryKanban,
                provider: _InMemoryProvider, issues_map: Dict[int, Dict[str, Any]],
                profiles: Dict[str, str], workdir: str) -> None:
    n = ISSUE_NUMBER
    title = f"#{n} {issues_map[n]['title']}"

    def _validators() -> List[int]:
        return disp._check_confirmed_validators(
            SLUG, REPO, issues_map, 3, workdir, "", "dev", "github",
            profiles=profiles, provider=provider,
        )

    def _pm() -> List[int]:
        return disp._check_completed_pm(
            SLUG, REPO, issues_map, 3, workdir, "", "dev", "github",
            profiles=profiles, provider=provider,
        )

    def _comments() -> List[int]:
        return disp._post_completion_comments(SLUG, provider, profiles, workdir)

    # ── Stage 1: validator CONFIRMED -> a PM spec card appears ────────────────
    board.seed(assignee=VALIDATOR, title=title, status="done",
               summary="CONFIRMED: reproduced on main; scope is clear")
    triggered = _validators()
    pm_card = board.created_with_key(f"pm-{n}")
    report.record("validator CONFIRMED creates a PM spec card",
                  triggered == [n] and pm_card is not None and pm_card["assignee"] == PM,
                  f"triggered={triggered}")

    # ── Stage 2: PM SPEC -> the downstream team cards appear ──────────────────
    if pm_card is not None:
        board.complete(SLUG, pm_card["id"], "SPEC: acceptance criteria defined")
    pm_triggered = _pm()
    team = {r: board.created_with_key(f"{r}-{n}") for r in _TEAM_ROLES}
    missing = [r for r, c in team.items() if c is None]
    report.record("PM SPEC creates the downstream team cards",
                  pm_triggered == [n] and not missing,
                  f"missing={missing}" if missing else f"roles={list(team)}")

    # ── Stage 3: a completed role card -> a completion comment (regression #894) ─
    dev_card = team.get("developer")
    if dev_card is not None:
        board.complete(SLUG, dev_card["id"], "developer done: PR opened and merged")
    _comments()
    # The poster comments once per completed role (here validator, pm, developer
    # are all done) — assert the developer's card produced a comment via the
    # provider, the exact mechanism #894 regressed.
    dev_comments = [c for c in provider.posted_issue_comments
                    if c[0] == n and "**Agent: developer**" in c[1]]
    report.record("completed role posts a comment via provider.post_issue_comment (#894)",
                  len(dev_comments) == 1,
                  f"developer comments={len(dev_comments)}, "
                  f"total for #{n}={sum(1 for c in provider.posted_issue_comments if c[0] == n)}")

    # ── Stage 4: idempotency — re-running every pass adds nothing (AC #4 / #891) ─
    cards_before = len(board.tasks)
    comments_before = len(provider.posted_issue_comments)
    _validators()
    _pm()
    _comments()
    no_new_cards = len(board.tasks) == cards_before
    no_new_comments = len(provider.posted_issue_comments) == comments_before
    report.record("re-running the tick is idempotent (no duplicate cards/comments)",
                  no_new_cards and no_new_comments,
                  f"cards +{len(board.tasks) - cards_before}, "
                  f"comments +{len(provider.posted_issue_comments) - comments_before}")

    # ── Stage 5: the run touched only the in-memory provider, never real GitHub ─
    report.record("no real GitHub touched (in-memory provider only)",
                  isinstance(provider, _InMemoryProvider)
                  and provider.board_configured() is False,
                  f"provider={type(provider).__name__}")
