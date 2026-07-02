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
import os
import re
import subprocess
from typing import List, Optional, Set

from core.db import connect_wal

logger = logging.getLogger("daedalus.kanban")


def _hk(args: List[str], timeout: int = 60) -> tuple[int, str, str]:
    """Run ``hermes kanban <args>``; return (rc, stdout, stderr). Patched in tests."""
    try:
        r = subprocess.run(["hermes", "kanban"] + args, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except Exception as e:
        return 1, "", str(e)


def ensure_board(slug: str) -> bool:
    """Create the board if missing (idempotent). True if usable.

    In Hermes v0.16.0+ ``hermes kanban --board <slug> init`` REQUIRES the
    board to already exist, so it NEVER creates one.  The correct command is
    ``hermes kanban boards create <slug>``, which creates the board if absent
    and succeeds (exit 0) when it already exists.
    """
    rc, out, err = _hk(["boards", "create", slug])
    if rc == 0:
        return True
    # Some versions may return non-zero with an "already exists" message
    # (e.g. stderr).  Treat that as success — the board is usable.
    combined = ((out or "") + (err or "")).lower()
    if "already exists" in combined or "already exist" in combined:
        return True
    logger.warning("kanban: could not ensure board '%s': %s", slug, (err or "").strip())
    return False


_ISSUE_RE = re.compile(r"#(\d+)")


def list_issue_numbers(slug: str) -> Set[int]:
    """Issue numbers that already have a task on the board (parsed from titles).

    Uses the structured JSON list (``hermes kanban list --json``) so that task
    title truncation in the human-readable ``ls`` table never causes an issue
    number — regardless of how many digits — to be missed.
    """
    tasks = list_tasks(slug)
    if not tasks:
        return set()
    nums: Set[int] = set()
    for task in tasks:
        title = task.get("title") or ""
        if not title:
            continue
        for m in _ISSUE_RE.finditer(title):
            nums.add(int(m.group(1)))
    return nums


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


def get_latest_summary(slug: str, task_id: str) -> str:
    """Return the latest_summary for a task (from show --json), or empty string."""
    rc, out, _ = _hk(["--board", slug, "show", task_id, "--json"])
    if rc != 0 or not out:
        return ""
    try:
        return json.loads(out).get("latest_summary") or ""
    except Exception:
        return ""


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


def complete(slug: str, task_id: str, summary: str = "") -> bool:
    """Mark a task complete (advances any children that were blocked on it).
    
    Args:
        slug: Board slug
        task_id: Task ID to complete
        summary: Optional summary message to record with the completion
    """
    args = ["--board", slug, "complete", task_id]
    if summary:
        args += ["--summary", summary]
    rc, out, err = _hk(args)
    if rc != 0:
        logger.warning("kanban: complete %s failed: %s", task_id, (err or out or "").strip())
        return False
    logger.info("kanban: completed %s", task_id)
    return True


def edit_summary(slug: str, task_id: str, summary: str) -> bool:
    """Rewrite a task's recorded result summary (``hermes kanban edit --result``).

    Used by the dispatcher to self-heal a done PM card whose summary was lost to
    the hermes premature-completion bug when the spec survives as a GitHub issue
    comment (issue #1161) — the same recovery an operator performs manually.
    """
    args = ["--board", slug, "edit", task_id, "--result", "--summary", summary]
    rc, out, err = _hk(args)
    if rc != 0:
        logger.warning("kanban: edit_summary %s failed: %s", task_id, (err or out or "").strip())
        return False
    logger.info("kanban: edited result summary of %s", task_id)
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
    skills: Optional[List[str]] = None,
    goal: bool = False,
    goal_max_turns: Optional[int] = None,
    max_retries: Optional[int] = None,
) -> Optional[str]:
    """Create a regular (non-triage) task. Returns task id or None.

    ``skills`` attaches named Hermes skills to the task so the worker has them
    pre-loaded without needing to call skill_view() themselves.
    ``goal`` spawns the worker in goal mode (multi-turn with adjudication).
    ``goal_max_turns`` sets the turn budget (only meaningful when goal=True).
    ``max_retries`` overrides the default failure-before-block retry cap.
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
    if skills:
        for s in skills:
            args += ["--skill", s]
    if max_retries is not None:
        args += ["--max-retries", str(max_retries)]
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
        args += [reason]  # hermes kanban block uses positional reason, not --reason
    rc, out, err = _hk(args)
    if rc != 0:
        logger.warning("kanban: block %s failed: %s", task_id, (err or out or "").strip())
        return False
    return True


def archive_task(slug: str, task_id: str) -> bool:
    """Archive a task off the active board (``hermes kanban archive``).

    Used by the stale-blocked sweeper to move long-stuck cards out of the active
    columns. Returns True on success; degrades gracefully (logs + returns False).
    """
    rc, out, err = _hk(["--board", slug, "archive", task_id])
    if rc != 0:
        logger.warning("kanban: archive %s failed: %s", task_id, (err or out or "").strip())
        return False
    logger.info("kanban: archived %s", task_id)
    return True


def reassign_task(slug: str, task_id: str, profile: str, *, reclaim: bool = False) -> bool:
    """Reassign a task to a different profile. Returns True on success."""
    args = ["--board", slug, "reassign", task_id, profile]
    if reclaim:
        args.append("--reclaim")
    rc, out, err = _hk(args)
    if rc != 0:
        logger.warning("kanban: reassign %s → %s failed: %s", task_id, profile, (err or out or "").strip())
        return False
    return True


def rename_task(slug: str, task_id: str, new_title: str) -> bool:
    """Update a task's title via direct SQLite write. Returns True on success.

    There is no ``hermes kanban rename`` CLI command, so this writes directly to
    the board's SQLite database at ~/.hermes/kanban/boards/<slug>/kanban.db.
    """
    db_path = os.path.expanduser(f"~/.hermes/kanban/boards/{slug}/kanban.db")
    if not os.path.exists(db_path):
        logger.warning("kanban: rename: DB not found for board %r", slug)
        return False
    try:
        conn = connect_wal(db_path)
        conn.execute("UPDATE tasks SET title = ? WHERE id = ?", (new_title, task_id))
        conn.commit()
        conn.close()
        return True
    except Exception as exc:
        logger.warning("kanban: rename %s failed: %s", task_id, exc)
        return False


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


def close_non_blocked_issue_tasks(slug: str, issue_number: int) -> List[str]:
    """Complete pending/in-progress tasks for issue_number, skipping blocked ones.

    Used when the validator has blocked an issue — downstream tasks (developer,
    reviewer, etc.) are completed immediately so they can't be dispatched, but
    the validator's blocked card is left intact so humans can see the reason.
    """
    tasks = list_tasks(slug)
    pattern = f"#{issue_number}"
    completed_ids: List[str] = []
    for t in tasks:
        if pattern not in (t.get("title") or ""):
            continue
        status = (t.get("status") or "").lower()
        if status in ("done", "complete", "completed", "blocked"):
            continue  # leave the blocked validator card; skip already-done tasks
        tid = t.get("id") or t.get("task_id")
        if tid and complete(slug, str(tid)):
            completed_ids.append(str(tid))
    return completed_ids


def close_issue_tasks(slug: str, issue_number: int, *, summary: str = "", dry_run: bool = False) -> List[str]:
    """Complete all non-done kanban tasks that reference #issue_number in their title.
    Uses word-boundary regex matching (#957 does not match #9571/#9570).

    Also walks the task tree to find any blocked children with review-required
    summaries and completes them with the provided summary message.
    Returns the list of task IDs that were completed.
    
    Args:
        slug: Board slug
        issue_number: Issue number to close tasks for
        summary: Optional summary message for completed blocked/review-required children
        dry_run: If True, log what would be completed without acting
    """
    tasks = list_tasks(slug)
    pattern = re.compile(rf"(?<!\d)#{issue_number}(?!\d)")
    completed_ids: List[str] = []
    
    # First pass: complete all non-done tasks whose title or body/handoff references the issue
    for t in tasks:
        title = t.get("title") or ""
        body = t.get("body") or ""
        if not (pattern.search(title) or pattern.search(body)):
            continue
        status = (t.get("status") or "").lower()
        if status in ("done", "complete", "completed", "cancelled"):
            continue
        tid = t.get("id") or t.get("task_id")
        if not tid:
            continue
        if dry_run:
            logger.info("[dry-run] would complete task %s (status: %s)", tid, status)
            completed_ids.append(str(tid))
        elif complete(slug, str(tid)):
            completed_ids.append(str(tid))
    
    # Second pass: walk task trees and complete blocked/review-required children
    if summary:
        for t in tasks:
            title = t.get("title") or ""
            body = t.get("body") or ""
            if not (pattern.search(title) or pattern.search(body)):
                continue
            tid = t.get("id") or t.get("task_id")
            if not tid:
                continue
            # Get full card details to find children
            card = show_card(slug, str(tid))
            if not card:
                continue
            children = card.get("children") or []
            for child_id in children:
                child_card = show_card(slug, child_id)
                if not child_card:
                    continue
                child_task = child_card.get("task", child_card)
                child_status = (child_task.get("status") or "").lower()
                if child_status in ("done", "complete", "completed"):
                    continue
                # Complete blocked/review-required children
                if child_status == "blocked":
                    latest_summary = child_card.get("latest_summary") or ""
                    if latest_summary.startswith("review-required:"):
                        if dry_run:
                            logger.info("[dry-run] would complete blocked/review-required child %s with summary: %s", 
                                      child_id, summary)
                            completed_ids.append(child_id)
                        elif complete(slug, child_id, summary=summary):
                            completed_ids.append(child_id)
    
    return completed_ids
