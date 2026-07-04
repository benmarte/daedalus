"""core.dispatch.housekeeping — per-tick board maintenance helpers.

Collects the low-level board-hygiene functions that run once per dispatcher
tick and do NOT depend on mutable dispatcher globals or on functions that
tests patch via ``disp.<name> = ...`` replacement:

  issue fetch helpers      — _fetch_issues, _GET_ISSUE_RETRY_DELAYS,
                             _fetch_issue_with_retry, _is_issue_closed_cached
  follow-up extraction     — _FOLLOWUP_MARKER, _FOLLOWUP_MARKER_RE,
                             _extract_follow_ups_from_pr_comment,
                             _check_follow_ups_from_reviewer_prs
  generic-role remapping   — _GENERIC_TO_ROLE, _remap_generic_role_assignees
  orphan helpers           — _find_issue_n_from_parents,
                             _global_reconcile_orphan_cards
  worktree sweep           — _WT_BRANCH_ISSUE_RE, _WT_PATH_ISSUE_RE,
                             _WT_TERMINAL_STATUSES, _sweep_orphan_worktrees
  stall detection          — _check_stalled_in_progress
  brain failover recovery  — _maybe_reset_brain_to_primary

Functions that call _get_task_summary, _has_active_pm_consultation,
_is_consult_resolved, or _stamp_resolved_consultations (which all depend on
``disp.kanban = fk`` replacement in tests) STAY in scripts/daedalus_dispatch.py
to preserve the patching surface.

_repair_orphan_tasks also stays: it calls _find_issue_n_from_parents (moved here
and re-exported) but tests do ``disp._find_issue_n_from_parents = mock`` — the
re-export makes that rebind visible to the dispatcher caller, keeping tests green.

Moved from scripts/daedalus_dispatch.py (issue #1153 PR 2/4).
The dispatcher re-exports every symbol so the public surface is unchanged.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from core import dispatch_state  # noqa: E402
from core import kanban  # noqa: E402
from core import provider_failover  # noqa: E402
from core.db import connect_wal  # noqa: E402
from core.dispatch.resolvers import _DEFAULT_PROFILES, _parse_follow_ups  # noqa: E402
from core.util import extract_issue_number  # noqa: E402

logger = logging.getLogger("daedalus.dispatch")


# ── Issue fetch helpers ───────────────────────────────────────────────────────


def _fetch_issues(provider, filters: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Open issues matching the configured label filter (ANY label), deduped.

    Paginates until the provider returns all issues; logs a WARNING when more
    than one page is fetched so operators know the board is growing large.
    An optional ``filters.max_issues`` ceiling caps the total result count for
    performance-sensitive deployments.
    """
    if provider is None:
        return []
    state = filters.get("state", "open")
    # ``limit`` is the page size sent to the provider (max 100 per GitHub API).
    # list_issues() now paginates automatically, so this is no longer a hard cap.
    page_size = int(filters.get("limit", 100))
    max_issues = filters.get("max_issues")  # optional hard ceiling
    labels = [lbl for lbl in (filters.get("labels") or []) if lbl]
    issues = [
        i.as_dict()
        for i in provider.list_issues(state=state, labels=labels, limit=page_size)
    ]
    if len(issues) > page_size:
        logger.warning(
            "dispatch: _fetch_issues returned %d issues (>%d page_size) — "
            "board has multiple pages; set filters.max_issues to cap if needed",
            len(issues),
            page_size,
        )
    if max_issues is not None:
        ceiling = int(max_issues)
        if len(issues) > ceiling:
            logger.warning(
                "dispatch: _fetch_issues truncated to max_issues=%d (total=%d)",
                ceiling,
                len(issues),
            )
            issues = issues[:ceiling]
    return issues


_GET_ISSUE_RETRY_DELAYS = (1.0, 2.0)  # seconds; len == extra attempts after the first


def _fetch_issue_with_retry(provider, n: int):
    """Fetch an issue not in the list window, retrying on transient failure.

    ``get_issue`` collapses transient (exhausted 429/5xx, transport) and
    permanent (404/PR/deleted) failures into ``None``. For a confirmed-spec
    issue the issue almost always exists, so a ``None`` is treated as likely
    transient and retried a bounded number of times with short backoff before
    falling through to the caller's warn-and-skip path.
    """
    fetched = provider.get_issue(n)
    if fetched:
        return fetched
    for delay in _GET_ISSUE_RETRY_DELAYS:
        time.sleep(delay)
        fetched = provider.get_issue(n)
        if fetched:
            logger.info("dispatch: get_issue #%s succeeded on retry", n)
            return fetched
    return None


def _is_issue_closed_cached(
    provider, issue_number: int, cache: Dict[int, Optional[bool]]
) -> Optional[bool]:
    """Three-state closed-issue check, memoized per-tick to avoid redundant API calls.

    Returns:
        ``True``  — the GitHub issue is confirmed closed.
        ``False`` — the issue is confirmed open (or no provider/method is
                    available, so we fail open for tests without a full provider).
        ``None``  — the state is *unknown*: ``provider.get_issue_state`` raised
                    (e.g. a 403 rate-limit error). Callers MUST treat ``None`` as
                    "skip / do not process" rather than "open", otherwise the
                    stale scan keeps processing under rate limit and reinforces
                    the very rate limiting that defeats this guard (issue #1120).

    Callers should therefore gate on ``is not False`` (skip when closed *or*
    unknown) rather than the plain truthiness of the return value.
    """
    if issue_number not in cache:
        if provider is not None and hasattr(provider, "get_issue_state"):
            try:
                cache[issue_number] = provider.get_issue_state(issue_number) == "closed"
            except Exception as exc:  # rate limit / transient API error
                logger.warning(
                    "dispatch: get_issue_state(#%s) failed (%s) — treating state as "
                    "unknown and skipping to avoid reinforcing rate limiting (#1120)",
                    issue_number,
                    exc,
                )
                cache[issue_number] = None
        else:
            cache[issue_number] = False
    return cache[issue_number]


# ── Follow-up extraction ──────────────────────────────────────────────────────

_FOLLOWUP_MARKER = "<!-- daedalus:follow-up-extracted PR #{pr} issue #{issue} -->"
_FOLLOWUP_MARKER_RE = re.compile(
    r"<!-- daedalus:follow-up-extracted PR #(\d+) issue #(\d+) -->",
)


def _extract_follow_ups_from_pr_comment(
    slug: str,
    repo: str,
    provider,
    pr_number: int,
    workdir: str,
    reviewer_slugs: List[str],
    labels: List[str],
    triage_assignee: str,
    extra_patterns: List[str],
    *,
    dry_run: bool = False,
) -> List[int]:
    """Extract follow-ups from one PR's reviewer/QA comments and create tracking issues.

    Returns list of newly created GitHub issue numbers.  Idempotent: already-extracted
    items are skipped via embedded HTML comment markers in the PR summary comment.
    """
    comments = provider.list_pr_comments(pr_number)

    # Collect already-extracted issue numbers from marker comments (idempotency).
    already_extracted: set = set()
    for c in comments:
        for m in _FOLLOWUP_MARKER_RE.finditer(c.body or ""):
            if int(m.group(1)) == pr_number:
                already_extracted.add(int(m.group(2)))

    # Filter to reviewer / QA comments.
    reviewer_comments = [c for c in comments if (c.author or "") in reviewer_slugs]
    if not reviewer_comments:
        return []

    # Parse follow-up items from each qualifying comment.
    follow_ups: List[tuple] = []  # (title, source_excerpt)
    for c in reviewer_comments:
        items = _parse_follow_ups(c.body or "", extra_patterns)
        for item in items:
            excerpt = (c.body or "")[:600]
            follow_ups.append((item, excerpt))

    if not follow_ups:
        return []

    # Deduplicate titles across comments.
    seen_titles: set = set()
    deduped: List[tuple] = []
    for title, excerpt in follow_ups:
        key = title.lower()
        if key not in seen_titles:
            seen_titles.add(key)
            deduped.append((title, excerpt))

    created: List[int] = []
    pr_url = (
        provider.pr_url(pr_number) if hasattr(provider, "pr_url") else f"#{pr_number}"
    )

    for title, excerpt in deduped:
        issue_title = f"[Follow-up from PR #{pr_number}] {title}"

        # Skip titles that look like already-existing issues (exact title match guard).
        try:
            existing_issues = provider.list_issues(
                state="open", labels=["follow-up"], limit=100
            )
            existing_titles = {i.title.lower() for i in existing_issues}
            if issue_title.lower() in existing_titles:
                logger.debug(
                    "follow-up already exists as open issue, skipping: %r", issue_title
                )
                continue
        except Exception as exc:
            # #1111: a failed dedup query means we cannot verify this title is
            # unique. Skip creation instead of blindly making a potential
            # duplicate, and log a warning so the bypass is never silent.
            logger.warning(
                "follow-up dedup query failed for PR #%s (%s) — skipping "
                "creation of %r to avoid a silent duplicate (#1111)",
                pr_number,
                exc,
                issue_title,
            )
            continue

        issue_body = (
            f"_Auto-extracted by Daedalus from PR #{pr_number} reviewer/QA comment._\n\n"
            f"**Original PR:** {pr_url}\n\n"
            f"**Follow-up item:** {title}\n\n"
            f"---\n\n"
            f"**Comment excerpt:**\n\n"
            f"```\n{excerpt}\n```\n"
        )

        if dry_run:
            logger.info(
                "[dry-run] would create follow-up issue: %r (PR #%s)", title, pr_number
            )
            created.append(0)
            continue

        issue_num = provider.create_issue(issue_title, issue_body, labels)
        if not issue_num:
            logger.warning(
                "follow-up extraction: create_issue failed for PR #%s: %r",
                pr_number,
                title,
            )
            continue

        if issue_num in already_extracted:
            logger.debug("follow-up #%s already tracked (PR #%s)", issue_num, pr_number)
            continue

        kanban.create_triage(
            slug,
            issue_num,
            issue_title,
            issue_body,
            idempotency_key=f"follow-up-{pr_number}-{issue_num}",
            workspace=f"dir:{workdir}" if workdir else None,
        )
        created.append(issue_num)
        logger.info(
            "follow-up extracted: PR #%s → issue #%s %r", pr_number, issue_num, title
        )

    if created and not dry_run:
        markers = "\n".join(
            _FOLLOWUP_MARKER.format(pr=pr_number, issue=n) for n in created
        )
        issue_refs = "\n".join(f"- #{n}" for n in created)
        summary = (
            f"**Agent: dispatcher**\n\n"
            f"Follow-up items extracted from reviewer/QA comments:\n\n"
            f"{issue_refs}\n\n"
            f"{markers}"
        )
        provider.post_pr_comment(pr_number, summary)

    return created


def _check_follow_ups_from_reviewer_prs(
    slug: str,
    repo: str,
    provider,
    workdir: str,
    profiles: Dict[str, str],
    follow_up_cfg: Dict[str, Any],
    *,
    dry_run: bool = False,
) -> int:
    """Scan recent PRs for follow-up items in reviewer/QA comments.

    Called from run() after _check_completed_pm.  Returns count of new issues created.
    Controlled by follow_up_extraction: enabled: true/false in daedalus.yaml.
    """
    if not follow_up_cfg.get("enabled", True):
        return 0

    reviewer_slugs = [
        profiles.get("reviewer", _DEFAULT_PROFILES["reviewer"]),
        "qa-daedalus",
    ]
    labels: List[str] = follow_up_cfg.get("labels") or ["enhancement", "follow-up"]
    triage_assignee: str = follow_up_cfg.get(
        "assign_triage_to", profiles.get("pm", _DEFAULT_PROFILES["pm"])
    )
    extra_patterns: List[str] = follow_up_cfg.get("patterns") or []
    scan_limit: int = int(follow_up_cfg.get("scan_pr_limit", 20))

    try:
        prs = provider.list_prs(state="all", limit=scan_limit)
    except Exception as exc:
        logger.warning("follow-up extraction: list_prs failed: %s", exc)
        return 0

    total = 0
    for pr in prs:
        try:
            created = _extract_follow_ups_from_pr_comment(
                slug,
                repo,
                provider,
                pr.number,
                workdir,
                reviewer_slugs,
                labels,
                triage_assignee,
                extra_patterns,
                dry_run=dry_run,
            )
            total += len(created)
        except Exception as exc:
            logger.warning("follow-up extraction: PR #%s failed: %s", pr.number, exc)
    return total


# ── Generic-role remapping ────────────────────────────────────────────────────

# Generic role names the PM agent may use instead of Daedalus profile names.
# Maps generic name → key in the profiles dict.
_GENERIC_TO_ROLE: Dict[str, str] = {
    "developer": "developer",
    "qa": "qa",
    "reviewer": "reviewer",
    "security-analyst": "security",
    "security": "security",
    "documentation": "documentation",
    "accessibility": "accessibility",
    "planner": "pm",
}


def _remap_generic_role_assignees(
    slug: str,
    profiles: Dict[str, str],
    *,
    dry_run: bool = False,
) -> Dict[str, tuple]:
    """Auto-correct generic role names to Daedalus profile names on todo/ready tasks.

    The PM agent sometimes sets assignee='developer' instead of 'developer-daedalus'.
    This runs each tick before dispatch so tasks are corrected before workers are spawned.
    Returns {task_id: (original_assignee, remapped_assignee)} for any remapped tasks.
    """
    profile_values = set(profiles.values())
    remapped: Dict[str, tuple] = {}
    for status in ("todo", "ready"):
        for task in kanban.list_tasks(slug, status=status):
            original = (task.get("assignee") or "").strip()
            if not original or original in profile_values:
                continue
            role = _GENERIC_TO_ROLE.get(original)
            if role is None:
                logger.debug(
                    "dispatch: remap: unknown assignee %r on task %s — skipping",
                    original,
                    task.get("id", "?"),
                )
                continue
            new_assignee = profiles.get(role)
            if not new_assignee:
                continue
            task_id = (task.get("id") or task.get("task_id") or "").strip()
            if not task_id:
                continue
            if dry_run:
                logger.info(
                    "[dry-run] remap: would reassign %s: %s → %s",
                    task_id,
                    original,
                    new_assignee,
                )
                remapped[task_id] = (original, new_assignee)
            elif kanban.reassign_task(slug, task_id, new_assignee):
                remapped[task_id] = (original, new_assignee)
    if remapped:
        lines = "\n".join(
            f"  {tid}: {orig} → {new}" for tid, (orig, new) in remapped.items()
        )
        logger.info(
            "dispatch: remapped %d generic assignee(s) → Daedalus profiles:\n%s",
            len(remapped),
            lines,
        )
    return remapped


# ── Orphan helpers ────────────────────────────────────────────────────────────


def _find_issue_n_from_parents(slug: str, task_id: str) -> Optional[str]:
    """Return the first issue number found in a parent task's title or body.

    Queries task_links in the board SQLite DB directly since the kanban CLI
    does not expose parent IDs in list output.
    """
    db_path = os.path.expanduser(f"~/.hermes/kanban/boards/{slug}/kanban.db")
    if not os.path.exists(db_path):
        return None
    try:
        conn = connect_wal(db_path)
        rows = conn.execute(
            "SELECT t.title, t.body FROM task_links l JOIN tasks t ON t.id = l.parent_id WHERE l.child_id = ?",
            (task_id,),
        ).fetchall()
        conn.close()
        for parent_title, parent_body in rows:
            text = (parent_title or "") + " " + (parent_body or "")
            num = extract_issue_number(text)
            if num is not None:
                return str(num)
    except Exception as exc:
        logger.debug("dispatch: repair: parent lookup failed for %s: %s", task_id, exc)
    return None


# _count_active_issue_tasks stays in scripts/daedalus_dispatch.py: tests use
# ``disp.kanban = fk`` which replaces kanban only on the dispatcher module
# (issue #1153 PR 2/4).


def _global_reconcile_orphan_cards(
    slug: str, provider, *, dry_run: bool = False
) -> None:
    """Sweep all non-terminal kanban cards and complete those whose issue is Done.

    Safety net: if a card references an issue that's already Done on the board
    but the card itself is still non-terminal (bug in earlier cleanup paths,
    card added after the issue moved to Done, etc.), complete it here.
    Idempotent — re-running never double-completes or thrashes terminal cards.
    """
    if provider is None:
        return
    board_done_nums = set(
        provider.board_numbers_with_statuses([provider.status_name("done")])
    )
    terminal_states = {"done", "complete", "completed", "cancelled"}
    for t in kanban.list_tasks(slug):
        # Skip already-terminal cards
        if (t.get("status") or "").lower() in terminal_states:
            continue
        # Resolve issue number from title or body
        title = t.get("title") or ""
        body = t.get("body") or ""
        num = extract_issue_number(title)
        if num is None:
            num = extract_issue_number(body)
        if num is None or num not in board_done_nums:
            continue
        # This card belongs to a Done issue — complete it
        tid = t.get("id") or t.get("task_id")
        if not tid:
            continue
        if dry_run:
            logger.info(
                "[dry-run] would complete orphan card %s (parent issue #%s is Done)",
                tid,
                num,
            )
        elif kanban.complete(slug, str(tid), summary="orphan: parent issue is Done"):
            logger.info(
                "dispatch: completed orphan card %s (parent issue #%s reached Done)",
                tid,
                num,
            )


# ── Worktree sweep ────────────────────────────────────────────────────────────

# Issue attribution for registered worktrees: the branch name our developer
# worktrees are spawned on (fix/issue-<N>), falling back to the directory name
# convention (.worktrees/dev-<N>). Worktrees matching neither are left alone —
# the sweep never removes what it cannot attribute to an issue.
_WT_BRANCH_ISSUE_RE = re.compile(r"fix/issue-(\d+)$")
_WT_PATH_ISSUE_RE = re.compile(r"(?:issue|dev)-(\d+)$")

# Kanban statuses that mean "this issue's pipeline is finished" — a worktree
# whose issue has ONLY tasks in these states (or none at all) is an orphan.
_WT_TERMINAL_STATUSES = ("done", "complete", "completed", "cancelled", "archived")


def _sweep_orphan_worktrees(workdir: str, slug: str, *, dry_run: bool = False) -> int:
    """Remove registered git worktrees whose issue has no active kanban task.

    Agents are instructed to ``git worktree remove --force`` on cleanup, but a
    crashed/reclaimed agent leaves its worktree behind and they accumulate
    unboundedly (issue #1114). This enforcement sweep runs once per tick:

      1. Enumerate worktrees via ``git worktree list --porcelain``.
      2. Attribute each to an issue number (branch ``fix/issue-<N>``, falling
         back to a ``dev-<N>``/``issue-<N>`` directory name). The main worktree
         and unattributable entries are always skipped.
      3. Remove any whose issue has no active (non-terminal) kanban task, then
         ``git worktree prune`` the stale metadata.

    Every git call is wrapped so a single broken worktree (locked, missing,
    permission error) is logged at WARNING and skipped — this function never
    raises and never aborts the tick. If the board itself can't be read the
    sweep aborts without removing anything (active worktrees are at risk when
    the active-task set is unknown). Returns the count of removed worktrees
    (in dry-run mode, the count that would be removed).
    """
    if not workdir:
        return 0
    try:
        listed = subprocess.run(
            ["git", "-C", workdir, "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception as exc:
        logger.warning("dispatch: worktree sweep skipped — git list failed: %s", exc)
        return 0
    if listed.returncode != 0:
        logger.debug(
            "dispatch: worktree sweep skipped — %s is not a usable git repo", workdir
        )
        return 0

    # Porcelain output: one block per worktree ("worktree <path>" / "HEAD <sha>"
    # / "branch refs/heads/<name>" or "detached"), blocks separated by blank lines.
    entries: List[Dict[str, str]] = []
    cur: Dict[str, str] = {}
    for line in (listed.stdout or "").splitlines():
        if not line.strip():
            if cur:
                entries.append(cur)
                cur = {}
        elif line.startswith("worktree "):
            cur["path"] = line[len("worktree ") :].strip()
        elif line.startswith("branch "):
            cur["branch"] = line[len("branch ") :].strip()
    if cur:
        entries.append(cur)

    root = os.path.realpath(workdir)
    candidates: List[Tuple[str, str, int]] = []
    for entry in entries:
        path = entry.get("path") or ""
        if not path or os.path.realpath(path) == root:
            continue  # never touch the main worktree
        branch = entry.get("branch") or ""
        if branch.startswith("refs/heads/"):
            branch = branch[len("refs/heads/") :]
        m = _WT_BRANCH_ISSUE_RE.search(branch) or _WT_PATH_ISSUE_RE.search(
            os.path.basename(path.rstrip("/"))
        )
        if not m:
            logger.debug(
                "dispatch: worktree sweep: cannot attribute %s to an issue — skipping",
                path,
            )
            continue
        candidates.append((path, branch, int(m.group(1))))
    if not candidates:
        return 0

    try:
        tasks = kanban.list_tasks(slug)
    except Exception as exc:
        logger.warning("dispatch: worktree sweep skipped — kanban list failed: %s", exc)
        return 0
    active: set = set()
    for task in tasks:
        if (task.get("status") or "").lower() in _WT_TERMINAL_STATUSES:
            continue
        n = extract_issue_number(task.get("title") or "")
        if n is not None:
            active.add(n)

    removed = 0
    for path, branch, issue_n in candidates:
        if issue_n in active:
            continue
        if dry_run:
            logger.info(
                "[dry-run] would remove orphan worktree %s (branch=%s, issue=#%d)",
                path,
                branch,
                issue_n,
            )
            removed += 1
            continue
        try:
            rm = subprocess.run(
                ["git", "-C", workdir, "worktree", "remove", "--force", path],
                capture_output=True,
                text=True,
                timeout=120,
            )
        except Exception as exc:
            logger.warning(
                "dispatch: failed to remove orphan worktree %s: %s — skipping",
                path,
                exc,
            )
            continue
        if rm.returncode != 0:
            logger.warning(
                "dispatch: failed to remove orphan worktree %s: %s — skipping",
                path,
                (rm.stderr or rm.stdout or "").strip()[:200],
            )
            continue
        logger.info(
            "dispatch: swept orphan worktree %s (branch=%s, issue=#%d)",
            path,
            branch,
            issue_n,
        )
        removed += 1

    if removed and not dry_run:
        try:
            subprocess.run(
                ["git", "-C", workdir, "worktree", "prune"],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except Exception as exc:
            logger.warning("dispatch: git worktree prune failed: %s", exc)
    return removed


# ── Stall detection ───────────────────────────────────────────────────────────


def _check_stalled_in_progress(
    slug: str,
    stall_minutes: int = 30,
    *,
    dry_run: bool = False,
) -> List[str]:
    """Detect stalled in-progress cards and move them to blocked.

    For every card in 'running' status whose last update is older than
    ``stall_minutes``, move it to 'blocked' with a STALLED summary so that
    _check_team_blockers picks it up on the next tick and routes it to PM.

    Returns list of task ids that were transitioned.
    """
    stalled: List[str] = []
    # List running tasks
    for task in kanban.list_tasks(slug, status="running"):
        tid = (task.get("id") or task.get("task_id") or "").strip()
        if not tid:
            continue
        # Fetch full card to get updated_at
        card = kanban.show_card(slug, tid) or {}
        updated_raw = card.get("updated_at") or card.get("started_at") or ""
        if not updated_raw:
            continue
        try:
            # Try parsing ISO timestamp
            if isinstance(updated_raw, str):
                # Handle various ISO formats
                updated_dt = datetime.fromisoformat(updated_raw.replace("Z", "+00:00"))
            elif isinstance(updated_raw, (int, float)):
                updated_dt = datetime.fromtimestamp(updated_raw, tz=timezone.utc)
            else:
                continue
            # If naive, assume UTC
            if updated_dt.tzinfo is None:
                updated_dt = updated_dt.replace(tzinfo=timezone.utc)
            age_minutes = (
                datetime.now(timezone.utc) - updated_dt
            ).total_seconds() / 60.0
        except (ValueError, TypeError, OSError):
            continue
        if age_minutes < stall_minutes:
            continue
        # Stalled — move to blocked
        if dry_run:
            logger.info(
                "[dry-run] stalled card %s (age=%.0fm) — would move to blocked",
                tid,
                age_minutes,
            )
            stalled.append(tid)
            continue
        # Use kanban.block to move to blocked state
        try:
            kanban.block_task(
                slug,
                tid,
                f"STALLED: session ended without completing (age={age_minutes:.0f}m)",
            )
            logger.info(
                "dispatch: stalled card %s moved to blocked (age=%.0fm)",
                tid,
                age_minutes,
            )
            stalled.append(tid)
        except Exception as e:
            logger.warning("dispatch: failed to block stalled card %s: %s", tid, e)
    return stalled


# ── Brain failover recovery ───────────────────────────────────────────────────


def _maybe_reset_brain_to_primary(
    workdir: str, failover_ctx: Dict[str, Any], dry_run: bool
) -> None:
    """Restore the primary brain provider once its cooldown expires (#1207).

    Brain failover is global (profiles are resynced for every role), so
    recovery is a per-tick check rather than per-card: when the profiles are
    on a fallback entry, ``reset_to_primary`` is set, and the primary's
    cooldown window has passed, resync back and clear the cooldown.
    """
    if dry_run:
        return
    fcfg = failover_ctx["cfg"]
    chain = failover_ctx["chains"]["brain"]
    if len(chain) < 2 or not fcfg.get("reset_to_primary", True):
        return
    if dispatch_state.get_brain_active_index(workdir) <= 0:
        return
    primary = chain[0]
    key = provider_failover.provider_key(
        provider_failover.LAYER_BRAIN, primary["provider"]
    )
    if dispatch_state.get_provider_cooldowns(workdir).get(key, 0) > time.time():
        return  # still cooling — stay on the fallback
    if failover_ctx["apply"]["brain"]({}, primary):
        dispatch_state.clear_provider_cooldown(workdir, key)
        failover_ctx["current"]["brain"] = primary["provider"]
        logger.info(
            "failover: primary brain provider %s recovered — profiles "
            "resynced back (reset_to_primary)",
            primary["provider"],
        )
