"""Deterministic Hermes-kanban tracking for the daedalus.

A thin, idempotent wrapper over the ``hermes kanban`` CLI: ensure a board exists,
list its tasks, and create exactly one task per issue (keyed by ``#<n>`` in the
title) so re-runs never duplicate. Hermes Kanban's own dispatch/worker then
executes each task and tracks status/runs/heartbeat/comments on the board
automatically — that is what makes tracking deterministic and live.

Every call degrades gracefully (logs + returns falsy) so tracking never breaks a
run.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from typing import List, Optional, Set

logger = logging.getLogger("daedalus.kanban")


def _hk(args: List[str], timeout: int = 60):
    """Run ``hermes kanban <args>``; return (rc, stdout, stderr). Patched in tests."""
    try:
        r = subprocess.run(["hermes", "kanban"] + args, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except Exception as e:
        return 1, "", str(e)


def ensure_board(slug: str) -> bool:
    """Create the board if missing (idempotent). True if usable."""
    rc, _, err = _hk(["--board", slug, "init"])
    if rc != 0:
        logger.warning("kanban: could not ensure board '%s': %s", slug, (err or "").strip())
        return False
    return True


def list_issue_numbers(slug: str) -> Set[int]:
    """Issue numbers that already have a task on the board (parsed from titles)."""
    rc, out, _ = _hk(["--board", slug, "ls"])
    if rc != 0:
        return set()
    return {int(n) for n in re.findall(r"#(\d+)", out or "")}


def create_triage(slug: str, issue_number: Optional[int], title: str, body: str,
                  idempotency_key: Optional[str] = None,
                  workspace: Optional[str] = None,
                  goal: bool = False,
                  goal_max_turns: Optional[int] = None) -> Optional[str]:
    """Create a TRIAGE card for an issue (to be fanned out by decompose()).

    Lands in the triage column rather than assigned to one profile, so the
    decomposer can split it into role sub-tasks (developer / reviewer /
    security-analyst / documentation). Returns the task id or None.

    ``issue_number`` can be None for non-issue cards (spec-file triggers,
    manual triage cards, etc.) — the card title is used as-is without a
    ``#<n>`` prefix.

    ``workspace`` (e.g. ``dir:/path/to/checkout`` or ``worktree:...``) pins where
    work happens. Hermes propagates it to every decomposed child, so a single pin
    here keeps the whole roster in the right checkout — without it, children land
    in a bare ``scratch`` workspace and a worker can wander into the wrong repo.

    ``goal`` spawns the worker in goal mode (multi-turn with adjudication).
    ``goal_max_turns`` sets the turn budget (only meaningful when goal=True).

    NOTE: The native ``decompose`` children do NOT inherit ``--goal`` because
    ``hermes kanban decompose`` does not support goal mode. If you need
    decomposed children in goal mode, create them individually with ``create_task(…, goal=True)``
    instead of using ``decompose()``.
    """
    card_title = f"#{issue_number} {title}" if issue_number is not None else title
    args = ["--board", slug, "create", card_title, "--body", body, "--triage"]
    if idempotency_key:
        args += ["--idempotency-key", idempotency_key]
    if workspace:
        args += ["--workspace", workspace]
    if goal:
        args += ["--goal"]
        if goal_max_turns is not None:
            args += ["--goal-max-turns", str(goal_max_turns)]
    rc, out, err = _hk(args)
    if rc != 0:
        logger.warning("kanban: create triage for #%s failed: %s", issue_number, (err or "").strip())
        return None
    m = re.search(r"\bt_[0-9a-f]+\b", out or "")
    tid = m.group(0) if m else None
    logger.info("kanban: created triage %s for #%s on board %s", tid, issue_number, slug)
    return tid


def decompose(slug: str, task_id: str) -> bool:
    """Fan a triage card out into role sub-tasks via the Hermes decomposer.

    The decomposer routes by each profile's description, so dev/review/security/
    documentation work lands on the right agents. Non-deterministic (LLM) — a
    terse triage body fans out broadly; a prescriptive one may stay single.
    """
    rc, out, err = _hk(["--board", slug, "decompose", task_id], timeout=180)
    if rc != 0:
        logger.warning("kanban: decompose %s failed: %s", task_id, (err or out or "").strip())
        return False
    logger.info("kanban: decomposed %s", task_id)
    return True


def list_blocked(slug: str) -> List[dict]:
    """Cards currently in the 'blocked' column (full --json dicts)."""
    rc, out, _ = _hk(["--board", slug, "list", "--status", "blocked", "--json"])
    if rc != 0:
        return []
    try:
        return json.loads(out) or []
    except Exception:
        return []


def review_handoff_pr(slug: str, task_id: str) -> Optional[int]:
    """If task_id is a 'review-required' handoff, return the PR number it opened.

    The worker records the handoff (with `PR #<n>`) in its run summary/events, so
    we read the full card detail and parse it. Returns None if not a review-required
    handoff or no PR is referenced.
    """
    rc, out, _ = _hk(["--board", slug, "show", task_id, "--json"])
    if rc != 0:
        return None
    blob = out or ""
    if "review-required" not in blob:
        return None
    m = re.search(r"PR #(\d+)", blob)
    return int(m.group(1)) if m else None


def complete(slug: str, task_id: str) -> bool:
    """Mark a task complete (advances any children that were blocked on it)."""
    rc, out, err = _hk(["--board", slug, "complete", task_id])
    if rc != 0:
        logger.warning("kanban: complete %s failed: %s", task_id, (err or out or "").strip())
        return False
    logger.info("kanban: completed %s", task_id)
    return True


def decompose_all_triage(slug: str) -> bool:
    """Decompose every card in the board's triage column (kanban-only mode).

    When there's no GitHub Project board, a human creates a triage card directly
    on the board; this fans every such card out across the roster. Idempotent —
    a decomposed card leaves the triage column, so re-running only touches new
    triage cards.
    """
    rc, out, err = _hk(["--board", slug, "decompose", "--all"], timeout=180)
    if rc != 0:
        logger.warning("kanban: decompose --all failed: %s", (err or out or "").strip())
        return False
    return True


def dispatch(slug: str, max_spawns: int = 5) -> bool:
    """Spawn workers for ready tasks on the board (Hermes tracks them live)."""
    rc, out, err = _hk(["--board", slug, "dispatch", "--max", str(max_spawns)])
    if rc != 0:
        logger.warning("kanban: dispatch failed: %s", (err or out or "").strip())
        return False
    return True


# ── iterate helpers ─────────────────────────────────────────────────────────


def show_card(slug: str, task_id: str) -> Optional[dict]:
    """Return full card detail as a parsed JSON dict, or None."""
    rc, out, _ = _hk(["--board", slug, "show", task_id, "--json"])
    if rc != 0:
        return None
    try:
        return json.loads(out or "{}")
    except Exception:
        return None


def create_task(
    slug: str,
    title: str,
    *,
    body: str = "",
    assignee: str = "",
    workspace: str = "",
    idempotency_key: str = "",
    parents: Optional[List[str]] = None,
    goal: bool = False,
    goal_max_turns: Optional[int] = None,
) -> Optional[str]:
    """Create a regular (non-triage) task. Returns task id or None.

    ``goal`` spawns the worker in goal mode (multi-turn with adjudication).
    ``goal_max_turns`` sets the turn budget (only meaningful when goal=True).
    """
    args = ["--board", slug, "create", title]
    if body:
        args += ["--body", body]
    if assignee:
        args += ["--assignee", assignee]
    if workspace:
        args += ["--workspace", workspace]
    if idempotency_key:
        args += ["--idempotency-key", idempotency_key]
    if parents:
        for p in parents:
            args += ["--parent", p]
    if goal:
        args += ["--goal"]
        if goal_max_turns is not None:
            args += ["--goal-max-turns", str(goal_max_turns)]
    rc, out, err = _hk(args)
    if rc != 0:
        logger.warning("kanban: create task failed: %s", (err or "").strip())
        return None
    m = re.search(r"\bt_[0-9a-f]+\b", out or "")
    tid = m.group(0) if m else None
    logger.info("kanban: created task %s (%s)", tid, title)
    return tid


def comment(slug: str, task_id: str, body: str) -> bool:
    """Append a comment to a task. Returns True on success."""
    rc, out, err = _hk(["--board", slug, "comment", task_id, body])
    if rc != 0:
        logger.warning("kanban: comment on %s failed: %s", task_id, (err or out or "").strip())
        return False
    return True


def unblock_task(slug: str, task_id: str, reason: str = "") -> bool:
    """Unblock a task, optionally with a reason comment. Returns True on success."""
    args = ["--board", slug, "unblock", task_id]
    if reason:
        args += ["--reason", reason]
    rc, out, err = _hk(args)
    if rc != 0:
        logger.warning("kanban: unblock %s failed: %s", task_id, (err or out or "").strip())
        return False
    return True


def block_task(slug: str, task_id: str, reason: str = "") -> bool:
    """Block a task. Returns True on success."""
    args = ["--board", slug, "block", task_id]
    if reason:
        args += ["--reason", reason]
    rc, out, err = _hk(args)
    if rc != 0:
        logger.warning("kanban: block %s failed: %s", task_id, (err or out or "").strip())
        return False
    return True


def diagnostics(slug: str) -> List[dict]:
    """Run ``hermes kanban diagnostics --json`` and return parsed diagnostics.

    Identifies stuck-in-blocked cards and their severity. Degrades gracefully
    if the command is unavailable (returns empty list). The diagnostics CLI is
    a native Hermes Kanban command that scans for stale, blocked, or otherwise
    problematic cards.

    Returns a list of dicts, each with keys like ``task_id``, ``severity``,
    ``message``, ``status``, and ``assignee``.
    """
    rc, out, _ = _hk(["--board", slug, "diagnostics", "--json"])
    if rc != 0:
        return []
    try:
        data = json.loads(out or "[]")
        return data if isinstance(data, list) else []
    except Exception:
        return []


def list_tasks(slug: str, status: str = "") -> List[dict]:
    """List all tasks on a board, optionally filtered by status. Returns parsed JSON list."""
    args = ["--board", slug, "list", "--json"]
    if status:
        args += ["--status", status]
    rc, out, _ = _hk(args)
    if rc != 0:
        return []
    try:
        return json.loads(out or "[]")
    except Exception:
        return []
