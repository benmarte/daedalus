"""CI-aware auto-advance routing and self-healing loop.

For every blocked card on the board, classify its blocked state into an action,
then execute that action (complete, create fix-up tasks, unblock, escalate).
Runs as part of the daedalus dispatcher auto-advance block.

Pure helpers are unit-testable; the executors call ``core.kanban`` and the
configured VCS provider (``core.providers``) and are guarded so failures log
and continue.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from core import kanban
from core.providers.base import CIStatus, issue_linked_to_pr, parse_depends_on
from core.util import extract_issue_number

logger = logging.getLogger("daedalus.iterate")

# Actions that classify_blocked can return
ADVANCE = "advance"            # dev card with green CI → complete, advance chain
DEV_FIX_CI = "dev_fix_ci"     # dev card with red CI → create fix card
PENDING_CI = "pending_ci"     # dev card with CI still pending → wait (cron handles retry)
PENDING_PR = "pending_pr"     # dev card with awaiting-pr block → search VCS for PR, update when found
PM_ROUTE = "pm_route"         # reviewer flagged changes → create PM routing card
APPROVE_ADVANCE = "approve_advance"  # reviewer approved → complete card
ESCALATE = "escalate"         # max iterations exceeded → log + notify
PLANNER_DECOMPOSE = "planner_decompose"  # planner completed → create sub-issues

# Maximum fix attempts per PR before escalation
MAX_FIX_ATTEMPTS = 3

# Source-reading fallback counter for observability
_source_reading_fallback_count: int = 0


def get_source_reading_fallback_count() -> int:
    """Return the count of Phase 4 fallback events (for testing/monitoring)."""
    return _source_reading_fallback_count


def reset_source_reading_fallback_count() -> None:
    """Reset the source-reading fallback counter to zero (for tests)."""
    global _source_reading_fallback_count
    _source_reading_fallback_count = 0

# Source-reading fallback counter for observability
_source_reading_fallback_count: int = 0


def get_source_reading_fallback_count() -> int:
    """Return the count of Phase 4 fallback events (for testing/monitoring)."""
    return _source_reading_fallback_count


def reset_source_reading_fallback_count() -> None:
    """Reset the source-reading fallback counter to zero (for tests)."""
    global _source_reading_fallback_count
    _source_reading_fallback_count = 0


# ── pure helpers ────────────────────────────────────────────────────────────


def _parse_handoff(handoff_text: str) -> Dict[str, Any]:
    """Parse a handoff string for key signals (review-required, PR #, changes requested, approved).

    Returns a dict with keys: is_review_required, pr_number, is_changes_requested,
    is_approved, findings_text.
    """
    text = handoff_text or ""
    result: Dict[str, Any] = {
        "is_review_required": "review-required" in text.lower(),
        "pr_number": None,
        "is_changes_requested": False,
        "is_approved": False,
        "findings_text": text,
    }

    # Extract PR number
    m = re.search(r"PR #(\d+)", text)
    if m:
        result["pr_number"] = int(m.group(1))

    # Detect review outcomes
    lower = text.lower()
    # Changes requested signals
    change_signals = [
        "changes requested", "changes required", "blocking findings",
        "request changes", "needs fixes", "need fixes",
        "changes-requested",  # hyphenated form used by reviewer SOUL: "review-changes-requested:"
    ]
    if any(s in lower for s in change_signals):
        result["is_changes_requested"] = True

    # Approval signals
    approve_signals = [
        "approved", "sign-off", "signoff", "approved.", "lgtm",
        "looks good", "no findings", "pass", ":+1:",
    ]
    if any(s in lower for s in approve_signals):
        result["is_approved"] = True

    return result


def classify_blocked(
    card_assignee: str,
    handoff_text: str,
    ci_green: bool,
    *,
    fix_attempts: int = 0,
    pr_number: Optional[int] = None,
    raw_ci: Optional[str] = None,
) -> str:
    """Classify a blocked card into an action.

    Args:
        card_assignee: The profile assigned to the blocked card
                       (e.g. 'developer', 'reviewer', 'security-analyst').
        handoff_text: The handoff/reason text from the block — typically from
                      the most recent run's summary or block reason.
        ci_green: Whether the PR's CI is passing.
        fix_attempts: Number of fix attempts already made for this PR.
        pr_number: Explicit PR number from a non-handoff source (e.g. branch
                   lookup). Used as a fallback when the handoff doesn't
                   contain a ``PR #N`` reference.
        raw_ci: The raw CIStatus string (e.g. 'pending', 'red', 'green',
                'unknown'). When None (default), treated as 'unknown' for
                backward compatibility.

    Returns one of: {advance, dev_fix_ci, pending_ci, pm_route, approve_advance, escalate}.
    """
    assignee = (card_assignee or "").lower().strip()
    handoff = _parse_handoff(handoff_text)

    # Resolve PR number: handoff first, then the explicit fallback.
    effective_pr = handoff["pr_number"] or pr_number

    # ── planner → decompose or PM ────────────────────────────────────────
    if assignee == "planner-daedalus":
        if "PLANNING COMPLETE" in (handoff_text or "").upper():
            return PLANNER_DECOMPOSE
        return PM_ROUTE  # unexpected planner output → escalate to PM

    # ── documentation-daedalus → terminal complete ────────────────────────
    # Docs is the last pipeline stage. When it blocks with 'docs posted:'
    # the job is done — complete the card. Anything else routes to PM.
    if assignee == "documentation-daedalus":
        if "docs posted" in (handoff_text or "").lower():
            return APPROVE_ADVANCE
        return PM_ROUTE

    # ── project-manager blocked → escalate (human gate — PM can't consult itself) ──
    if assignee == "project-manager-daedalus":
        # PM blocked while waiting for a developer fix — not a real escalation.
        if "awaiting-fix:" in (handoff_text or "").lower():
            return ""
        return ESCALATE

    # ── developer card ───────────────────────────────────────────────────
    if assignee == "developer-daedalus":
        # Exceeded max fix attempts → escalate
        if fix_attempts >= MAX_FIX_ATTEMPTS:
            return ESCALATE
        # Review-required handoff with PR → check CI state
        if handoff["is_review_required"] and effective_pr:
            if ci_green:
                return ADVANCE
            # raw_ci is None (backward compat) or UNKNOWN → treat as RED (actionable)
            resolved_ci = (raw_ci or CIStatus.UNKNOWN)
            if resolved_ci == CIStatus.PENDING:
                return PENDING_CI
            else:
                return DEV_FIX_CI
        # Awaiting-PR block: Claude Code was spawned but hasn't opened a PR yet.
        # The executor will search VCS for a matching PR and update the block reason.
        if handoff["is_review_required"] and "awaiting-pr" in (handoff_text or "").lower():
            return PENDING_PR
        # Infrastructure / system crash — agent never ran or died at startup.
        # PM routing cannot fix a gateway/OS crash and only creates a loop where
        # each PM-ROUTE completes as "no-op" but a new one is spawned next tick.
        _crash_markers = (
            "coding-agent-failed:", "permission-error:", "coding_agent_died",
            "coding_agent_timeout", "exited with code", "agent crash",
        )
        if any(m in (handoff_text or "").lower() for m in _crash_markers):
            return ""  # infrastructure failure — human must fix env and unblock
        # No PR or not review-required → PM route
        return PM_ROUTE

    # ── reviewer / security-analyst card ─────────────────────────────────
    if assignee in ("reviewer-daedalus", "security-analyst-daedalus"):
        # Exceeded max fix attempts → escalate
        if fix_attempts >= MAX_FIX_ATTEMPTS:
            return ESCALATE
        # A developer fix card is already in flight — don't create another PM-ROUTE.
        # Concurrent cron ticks would otherwise each spawn a separate PM-ROUTE
        # before any of them has time to annotate the card with "awaiting-fix:".
        if "awaiting-fix:" in (handoff_text or "").lower():
            return ""
        if handoff["is_changes_requested"]:
            return PM_ROUTE
        if handoff["is_approved"]:
            return APPROVE_ADVANCE
        return ""

    # ── qa-daedalus card ──────────────────────────────────────────────────
    # QA sits between developer and reviewer/security. The QA agent posts one
    # of three signals: qa-passed (all good), qa-failed (tests/lint broken),
    # or something unspecified (still running / unclear). CI is not a gate for
    # QA — the dispatcher acts on the signal directly.
    if assignee == "qa-daedalus":
        summary = (handoff_text or "").lower()
        if "qa-passed" in summary:
            return ADVANCE
        if "qa-failed" in summary:
            return DEV_FIX_CI
        return PENDING_CI

    # ── accessibility-daedalus card ───────────────────────────────────────
    # Accessibility auditors PRs for WCAG 2.1 AA compliance. Posts
    # 'approved' / 'accessibility-na' to advance, 'changes requested' to
    # route back to the PM for re-routing, otherwise pending.
    if assignee == "accessibility-daedalus":
        summary = (handoff_text or "").lower()
        if "approved" in summary or "accessibility-na" in summary or "a11y-skipped" in summary:
            return ADVANCE
        if "changes requested" in summary:
            return PM_ROUTE
        return PENDING_CI

    # ── validator-daedalus card ───────────────────────────────────────────
    # Validators should only ever complete (CONFIRMED/BLOCKED/ALREADY_FIXED).
    # If one is blocked with awaiting-pr the delegated CC agent used the
    # developer protocol by mistake — escalate so a human can manually
    # complete it with the correct verdict.
    if assignee == "validator-daedalus":
        return ESCALATE

    # ── unknown assignee ─────────────────────────────────────────────────
    return ""


def _fix_attempts_path(workdir: str) -> str:
    return str(Path(workdir) / ".hermes" / "daedalus-fix-attempts.json")


def _read_fix_attempts(workdir: str) -> Dict[str, int]:
    """Read the per-card fix attempt counter file."""
    try:
        path = _fix_attempts_path(workdir)
        if Path(path).is_file():
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def _write_fix_attempts(workdir: str, data: Dict[str, int]) -> None:
    """Write the per-card fix attempt counter file atomically."""
    path = _fix_attempts_path(workdir)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(p)


def _increment_fix_attempts(card: dict, workdir: str) -> int:
    """Increment and return the fix attempt count for a card.

    Persists to .hermes/daedalus-fix-attempts.json in the workdir
    so the counter survives across dispatcher ticks (previously the
    counter was always 0 because nothing wrote to runs.metadata).
    """
    if not workdir:
        return 0
    tid = card.get("id", "")
    if not tid:
        return 0
    data = _read_fix_attempts(workdir)
    new_count = data.get(tid, 0) + 1
    data[tid] = new_count
    _write_fix_attempts(workdir, data)
    return new_count


def _count_fix_attempts(card: dict, slug: str = "", workdir: str = "") -> int:
    """Count fix attempts for a card from the persistent counter file.

    Also checks the board for fix cards that may have been created outside
    this dispatcher (cross-process resilience). Returns the max of the
    file counter and the board-based count.
    """
    tid = card.get("id", "")
    attempts = 0

    # Primary: read from the persistent counter file
    if tid and workdir:
        data = _read_fix_attempts(workdir)
        attempts = data.get(tid, 0)

    # Secondary: count fix cards on the board by idempotency-key pattern
    # (catches fix cards created by other dispatchers or manual runs)
    # Only count PENDING/active tasks — completed fix cards are already spent
    # and should not permanently block the counter from resetting.
    if tid and slug:
        tasks = kanban.list_tasks(slug)
        board_count = 0
        for task in tasks:
            ikey = (task.get("idempotency_key") or "")
            status = (task.get("status") or "").lower()
            # Fix card idempotency keys:
            #   fix-ci-{tid}-attempt-N   (dev fix for CI-red)
            #   fix-review-{tid}-attempt-N  (legacy direct-dev review fix)
            #   pm-route-{tid}-attempt-N   (PM routing card)
            if f"-{tid}-attempt-" in ikey and status not in ("done", "completed"):
                board_count += 1
        attempts = max(attempts, board_count)

    # Fallback: runs metadata (for backward compat in tests)
    runs = card.get("runs") or []
    for run in runs:
        meta = run.get("metadata") or {}
        attempts = max(attempts, int(meta.get("fix_attempts", 0)))

    return attempts


def _parse_pr_number(handoff_text: str) -> Optional[int]:
    """Extract a PR number from handoff text."""
    m = re.search(r"PR #(\d+)", handoff_text)
    return int(m.group(1)) if m else None


def _extract_issue_number_from_card(card: dict) -> Optional[int]:
    """Parse the GitHub issue number from a card body.

    Looks for ``{org}/{repo}#<n>`` or bare ``#<n>`` patterns in the card body.
    Prefers the repo-qualified form (e.g. ``benmarte/daedalus#21``) to avoid
    false matches on PR numbers embedded in prose.
    """
    return extract_issue_number(card.get("body") or "", prefer_qualified=True)


_ESCALATION_STAMP_PREFIX = "escalated: issue #"


def _is_card_already_escalated(slug: str, tid: str, issue_n: int) -> bool:
    """Return True if the card has an ``escalated: issue #N`` stamp comment.

    Fetches the card via ``kanban.show_card`` and inspects its comments.
    Returns False if the card cannot be fetched or has no matching stamp.
    """
    card = kanban.show_card(slug, tid)
    if not card:
        return False
    stamp = f"{_ESCALATION_STAMP_PREFIX}{issue_n}"
    for c in card.get("comments") or []:
        body = (c.get("body") or "").strip()
        if body == stamp:
            return True
    return False


def _handoff_from_card(card: dict) -> str:
    """Extract handoff text from a card dict.

    The handoff is typically in the most recent run's 'reason' field
    or the card's block reason in events.
    """
    runs = card.get("runs") or []
    # Most recent run first (or iterate to find the one with a reason)
    for run in runs:
        reason = (run.get("reason") or "").strip()
        if reason:
            return reason
    # Fallback: check card-level reason
    return (card.get("reason") or "").strip()


# ── action executors ────────────────────────────────────────────────────────


def _execute_advance(
    slug: str,
    card: dict,
    repo: str,
    handoff_text: str,
    *,
    dry_run: bool = False,
    pr_number: Optional[int] = None,
    **_kwargs: Any,
) -> bool:
    """Complete a developer card with green CI to advance the chain.

    Also unblocks any reviewer/security cards that were blocked with
    'awaiting-fix: {this_card_id}' so they re-engage after the fix lands.
    After completing the developer card, creates downstream QA, reviewer,
    security-analyst, accessibility, and documentation tasks if they don't already exist.
    """
    tid = (card.get("id") or "")
    if not tid:
        return False
    pr = pr_number or _parse_pr_number(handoff_text)
    if dry_run:
        logger.info("[dry-run] would advance %s (PR #%s CI green)", tid, pr)
        return True
    if not kanban.complete(slug, tid):
        return False
    logger.info("iterate: advanced %s — PR #%s CI green", tid, pr)

    # Re-engage: unblock any cards that were blocked awaiting this fix.
    # When a reviewer flags changes and a dev fix card is created, the
    # reviewer card gets blocked with "awaiting-fix: {fix_tid}". Now that
    # the fix is complete, unblock those cards so they re-review.
    blocked = kanban.list_blocked(slug)
    for b in blocked:
        block_reason = _handoff_from_card(b) or ""
        if f"{tid}" in block_reason and "awaiting-fix" in block_reason.lower():
            btid = b.get("id")
            if btid:
                unblocked = kanban.unblock_task(slug, btid,
                                                f"fix {tid} completed — re-engage review")
                if unblocked:
                    logger.info("iterate: unblocked %s (was awaiting fix %s)", btid, tid)

    # Post-developer handoff: create reviewer/security/docs tasks.
    issue_number = _extract_issue_number_from_card(card)
    if issue_number is not None:
        _create_downstream_review_tasks(
            slug, issue_number, card,
            pr_number=pr, dry_run=dry_run,
        )
    else:
        logger.warning(
            "iterate: advanced %s but could not extract issue number — "
            "skipping downstream review task creation", tid,
        )
    return True


# Downstream review-task role mapping (idempotency suffix → assignee).
_DOWNSTREAM_REVIEW_ROLES = [
    ("qa", "qa-daedalus"),
    ("reviewer", "reviewer-daedalus"),
    ("security", "security-analyst-daedalus"),
    ("accessibility", "accessibility-daedalus"),
    ("docs", "documentation-daedalus"),
]


def _create_downstream_review_tasks(
    slug: str,
    issue_number: int,
    card: dict,
    *,
    pr_number: Optional[int] = None,
    dry_run: bool = False,
) -> List[str]:
    """Create qa/reviewer/security/accessibility/docs tasks after a developer card completes.

    Each task uses an idempotency key (``qa-{n}``, ``reviewer-{n}``, ``security-{n}``,
    ``accessibility-{n}``, ``docs-{n}``) so re-runs never duplicate.  If a task with that key already
    exists on the board (any status), creation is skipped for that role.

    Returns the list of newly-created task ids.
    """
    created: List[str] = []
    tid = card.get("id") or ""
    workspace = card.get("workspace") or ""

    # Build a concise body referencing the issue and PR.
    pr_ref = f"PR #{pr_number}" if pr_number else "(PR number unknown)"
    base_body = (
        f"The developer has completed work for issue #{issue_number} "
        f"({pr_ref}). The PR is open and CI is green.\n\n"
        f"Developer card: {tid}\n"
        f"Workspace: {workspace}\n"
    )

    # Idempotency: check which keys already exist on the board.
    existing_keys: Set[str] = set()
    for task in kanban.list_tasks(slug):
        ikey = task.get("idempotency_key") or ""
        if ikey:
            existing_keys.add(ikey)

    for role_suffix, assignee in _DOWNSTREAM_REVIEW_ROLES:
        ikey = f"{role_suffix}-{issue_number}"
        if ikey in existing_keys:
            logger.info("iterate: downstream task with key '%s' already exists — skip", ikey)
            continue

        title = f"#{issue_number} {assignee.replace('-daedalus', '').title()} review"
        body = base_body

        if dry_run:
            logger.info(
                "[dry-run] would create downstream %s task for issue #%s (key=%s)",
                role_suffix, issue_number, ikey,
            )
            continue

        new_tid = kanban.create_task(
            slug,
            title,
            body=body,
            assignee=assignee,
            workspace=workspace,
            idempotency_key=ikey,
            parents=[tid] if tid else None,
        )
        if new_tid:
            created.append(new_tid)
            logger.info(
                "iterate: created downstream %s task %s for issue #%s",
                role_suffix, new_tid, issue_number,
            )
        else:
            logger.warning(
                "iterate: failed to create downstream %s task for issue #%s",
                role_suffix, issue_number,
            )

    if not dry_run and created:
        kanban.comment(
            slug, tid,
            f"Created {len(created)} downstream review task(s): "
            + ", ".join(created),
        )

    return created


def _execute_dev_fix_ci(
    slug: str,
    card: dict,
    repo: str,
    handoff_text: str,
    *,
    workdir: str = "",
    dry_run: bool = False,
    pr_number: Optional[int] = None,
    **_kwargs: Any,
) -> bool:
    """Create a developer fix card for CI-red PR, idempotent per (card, attempt)."""
    tid = card.get("id")
    pr = pr_number or _parse_pr_number(handoff_text)
    if not pr:
        logger.warning("iterate: dev_fix_ci on %s but no PR found in handoff", tid)
        return False
    # Read-then-increment is not atomic, but the dispatcher is a single-process
    # cron (projects processed sequentially) — no lock needed unless that changes.
    fix_attempts = _count_fix_attempts(card, slug=slug, workdir=workdir) + 1
    if fix_attempts > MAX_FIX_ATTEMPTS:
        return _execute_escalate(slug, card, repo, handoff_text, workdir=workdir, dry_run=dry_run)

    title = f"Task 2.3 FIX — CI red on PR #{pr} — fix and push"
    body = (
        f"CI is red on PR #{pr} (repo {repo}). "
        f"Fix the failing tests/build and push. Fix attempt {fix_attempts}/{MAX_FIX_ATTEMPTS}."
    )

    idem_key = f"fix-ci-{tid}-attempt-{fix_attempts}"
    ws = f"dir:{workdir}" if workdir else card.get("workspace", "")

    if dry_run:
        logger.info("[dry-run] would create CI fix card for %s (attempt %s/%s, PR #%s)",
                     tid, fix_attempts, MAX_FIX_ATTEMPTS, pr)
        return True

    fix_tid = kanban.create_task(
        slug,
        title,
        body=body,
        assignee="developer-daedalus",
        workspace=ws,
        idempotency_key=idem_key,
    )
    if fix_tid:
        kanban.comment(slug, tid,
                       f"Created CI fix task {fix_tid} (attempt {fix_attempts}/{MAX_FIX_ATTEMPTS})")
        # Persist the incremented fix attempt count so escalation works across ticks.
        _increment_fix_attempts(card, workdir)
        logger.info("iterate: created CI fix card %s for %s (attempt %s/%s)",
                     fix_tid, tid, fix_attempts, MAX_FIX_ATTEMPTS)
        return True
    return False


def _execute_pending_pr(
    slug: str,
    card: dict,
    repo: str,
    handoff_text: str,
    *,
    provider=None,
    dry_run: bool = False,
    **_kwargs: Any,
) -> bool:
    """Search VCS for a PR linked to this card's issue number; update block reason when found.

    Called when a developer card is blocked with 'review-required: awaiting-pr'.
    Searches open PRs for one that references the issue number in its title,
    body, or branch name. If found, updates the block reason so the next cron
    tick can advance the pipeline normally. If not found, does nothing (stays
    blocked until Claude Code opens the PR).
    """
    tid = card.get("id")
    if provider is None:
        logger.debug("iterate: pending_pr %s — no provider, skipping", tid)
        return False

    issue_n = _extract_issue_number_from_card(card)
    if issue_n is None:
        logger.debug("iterate: pending_pr %s — no issue number, skipping", tid)
        return False

    try:
        open_prs = provider.list_prs(state="open", limit=50)
    except Exception as exc:
        logger.warning("iterate: pending_pr %s — list_prs failed: %s", tid, exc)
        return False

    found_pr = None
    for pr in open_prs:
        if issue_linked_to_pr(pr, issue_n):
            found_pr = pr.number
            break

    if found_pr is None:
        logger.debug("iterate: pending_pr %s — no PR found yet for issue #%s", tid, issue_n)
        return False

    new_handoff = f"review-required: PR #{found_pr} — awaiting CI"
    if dry_run:
        logger.info("[dry-run] pending_pr %s — would update block reason to '%s'", tid, new_handoff)
        return True

    # hermes kanban block refuses to re-block an already-blocked card ("cannot block").
    # Unblock first so the new reason takes effect.
    kanban.unblock_task(slug, tid, "pending-pr: PR found, updating block reason")
    kanban.block_task(slug, tid, new_handoff)
    logger.info("iterate: pending_pr %s — PR #%s found for issue #%s, updated block reason",
                tid, found_pr, issue_n)
    return True


def _execute_pm_route(
    slug: str,
    card: dict,
    repo: str,
    handoff_text: str,
    *,
    workdir: str = "",
    router_profile: str = "project-manager-daedalus",
    dry_run: bool = False,
    pr_number: Optional[int] = None,
    **_kwargs: Any,
) -> bool:
    """Create a PM routing card for review findings (changes-requested).

    Instead of creating a developer fix card directly, this creates a card
    assigned to the router_profile (default 'project-manager') that carries
    the findings and the instruction to DECIDE the owner. The PM can route to:
    - developer (for code fixes)
    - security-analyst (for security hardening)
    - re-spec (if the request was wrong)

    Falls back to the old direct-developer routing if:
    - router_profile resolves empty
    - The configured profile is absent (CLI create fails)
    """
    tid = card.get("id")
    # Guard: awaiting-pr means Claude Code was spawned but hasn't opened a PR yet.
    # The PENDING_PR executor handles this — PM routing cannot unblock it (issue #87).
    if "awaiting-pr" in (handoff_text or "").lower():
        logger.info("iterate: %s blocked awaiting-pr — skipping PM route", tid)
        return False
    pr = pr_number or _parse_pr_number(handoff_text)
    fix_attempts = _count_fix_attempts(card, slug=slug, workdir=workdir) + 1
    if fix_attempts > MAX_FIX_ATTEMPTS:
        return _execute_escalate(slug, card, repo, handoff_text, workdir=workdir, dry_run=dry_run)

    rp = (router_profile or "").strip()
    ws = f"dir:{workdir}" if workdir else card.get("workspace", "")

    if not rp:
        # Fallback: empty router_profile → direct developer routing
        return _execute_legacy_dev_fix_review(
            slug, card, repo, handoff_text,
            workdir=workdir, dry_run=dry_run,
        )

    title = f"PM-ROUTE — decide fix owner for PR #{pr or '?'}"
    body = (
        f"A review flagged changes for PR #{pr or '?'} (repo {repo}). "
        f"Review card ID: {tid}.\n\n"
        f"Findings:\n{handoff_text}\n\n"
        f"DECIDE the owner:\n"
        f"- developer — code fix\n"
        f"- security-analyst — security hardening\n"
        f"- re-spec — the request itself was wrong\n\n"
        f"Create the appropriate fix card (assigned to the chosen profile) "
        f"with the findings. Include 'Review card ID: {tid}' in the fix card body "
        f"so the developer knows to unblock the reviewer directly instead of spawning "
        f"a new review pipeline.\n\n"
        f"IMPORTANT rules for the fix card:\n"
        f"- Do NOT set {tid} as a parent — circular dependency (fix waits for reviewer, reviewer waits for fix).\n"
        f"- The fix card must be independent (no parent link to the review card).\n"
        f"- When the developer finishes, they must kanban_unblock({tid}, 're-review: PR #N') "
        f"and then kanban_complete() their own card — NOT block with 'review-required:' "
        f"(that spawns 5 redundant review agents on top of the existing reviewer)."
    )

    idem_key = f"pm-route-{tid}-attempt-{fix_attempts}"

    if dry_run:
        logger.info("[dry-run] would create PM routing card for %s via %s (attempt %s/%s, PR #%s)",
                     tid, rp, fix_attempts, MAX_FIX_ATTEMPTS, pr)
        return True

    pm_tid = kanban.create_task(
        slug,
        title,
        body=body,
        assignee=rp,
        workspace=ws,
        idempotency_key=idem_key,
        goal=True,
    )
    if pm_tid:
        # Idempotency guard: create_task returns the existing task ID when a task
        # with the same key already exists (even if done). If it's already done,
        # the PM already handled this routing — don't re-increment fix_attempts or
        # flood the card with duplicate comments.
        pm_detail = kanban.show_card(slug, pm_tid)
        pm_status = ((pm_detail or {}).get("task") or {}).get("status", "")
        if pm_status in ("done", "completed"):
            logger.info("iterate: PM-ROUTE %s already resolved (done) — skipping increment", pm_tid)
            return True
        kanban.comment(slug, tid,
                       f"Created PM routing card {pm_tid} (attempt {fix_attempts}/{MAX_FIX_ATTEMPTS})")
        # Mark the reviewer card as blocked (awaiting-fix) so pending state is visible
        kanban.block_task(slug, tid, f"awaiting-fix: {pm_tid}")
        _increment_fix_attempts(card, workdir)
        logger.info("iterate: created PM routing card %s for %s via %s (attempt %s/%s)",
                     pm_tid, tid, rp, fix_attempts, MAX_FIX_ATTEMPTS)
        return True

    # CLI create failed — profile likely absent; fall back to direct developer
    logger.warning("iterate: PM routing card creation failed (profile '%s' absent?), "
                   "falling back to direct developer routing", rp)
    return _execute_legacy_dev_fix_review(
        slug, card, repo, handoff_text,
        workdir=workdir, dry_run=dry_run,
    )


def _execute_legacy_dev_fix_review(
    slug: str,
    card: dict,
    repo: str,
    handoff_text: str,
    *,
    workdir: str = "",
    dry_run: bool = False,
    pr_number: Optional[int] = None,
) -> bool:
    """Fallback: create a developer fix card directly (old behavior).

    Used when router_profile is empty or the PM profile is absent.
    """
    tid = card.get("id")
    pr = pr_number or _parse_pr_number(handoff_text)
    fix_attempts = _count_fix_attempts(card, slug=slug, workdir=workdir) + 1
    if fix_attempts > MAX_FIX_ATTEMPTS:
        return _execute_escalate(slug, card, repo, handoff_text, workdir=workdir, dry_run=dry_run)

    title = f"Task 2.3 FIX — address review findings for PR #{pr or '?'} — push changes"
    body = (
        f"Review findings for PR #{pr or '?'} (repo {repo}):\n\n"
        f"{handoff_text}\n\n"
        f"Address all findings and push. Fix attempt {fix_attempts}/{MAX_FIX_ATTEMPTS}."
    )

    idem_key = f"fix-review-{tid}-attempt-{fix_attempts}"
    ws = f"dir:{workdir}" if workdir else card.get("workspace", "")

    if dry_run:
        logger.info("[dry-run] would create legacy review-fix card for %s (attempt %s/%s, PR #%s)",
                     tid, fix_attempts, MAX_FIX_ATTEMPTS, pr)
        return True

    fix_tid = kanban.create_task(
        slug,
        title,
        body=body,
        assignee="developer-daedalus",
        workspace=ws,
        idempotency_key=idem_key,
    )
    if fix_tid:
        kanban.comment(slug, tid,
                       f"Created review-fix task {fix_tid} (fallback, attempt {fix_attempts}/{MAX_FIX_ATTEMPTS})")
        kanban.block_task(slug, tid, f"awaiting-fix: {fix_tid}")
        logger.info("iterate: created legacy review-fix card %s for %s (attempt %s/%s)",
                     fix_tid, tid, fix_attempts, MAX_FIX_ATTEMPTS)
        return True
    return False


def _execute_approve_advance(
    slug: str,
    card: dict,
    repo: str,
    handoff_text: str,
    *,
    dry_run: bool = False,
    **_kwargs: Any,
) -> bool:
    """Complete a reviewer/security card that approved the work."""
    tid = card.get("id")
    if dry_run:
        logger.info("[dry-run] would complete approved card %s", tid)
        return True
    if kanban.complete(slug, tid):
        logger.info("iterate: completed approved card %s", tid)
        return True
    return False


def _execute_escalate(
    slug: str,
    card: dict,
    repo: str,
    handoff_text: str,
    *,
    workdir: str = "",
    notify_target: str = "",
    dry_run: bool = False,
    pr_number: Optional[int] = None,
    **_kwargs: Any,
) -> bool:
    """Escalate a card that has exceeded max fix attempts.

    After posting the escalation comment, stamps the card with an
    ``escalated: issue #N`` comment so future dispatcher ticks skip it.
    Returns True on success.
    """
    tid = card.get("id")
    if not tid:
        return False
    pr = pr_number or _parse_pr_number(handoff_text)
    msg = (
        f"⚠️ ESCALATE: card {tid} (PR #{pr or '?'}) has exceeded "
        f"{MAX_FIX_ATTEMPTS} fix attempts. Manual intervention required."
    )
    logger.warning("iterate: %s", msg)

    if dry_run:
        issue_n = _extract_issue_number_from_card(card)
        logger.info("[dry-run] would escalate %s for issue #%s", tid, issue_n)
        return True

    kanban.comment(slug, tid, msg)

    # Stamp the card so future ticks skip it (cross-tick dedup).
    issue_n = _extract_issue_number_from_card(card)
    if issue_n is not None:
        kanban.comment(slug, tid, f"escalated: issue #{issue_n}")
        logger.info("iterate: stamped %s with escalated: issue #%s", tid, issue_n)

    # Leave the card blocked for human review (do not auto-advance)
    return True


# ── planner decompose ───────────────────────────────────────────────────────

_CHECKLIST_RE = re.compile(r"^\s*[-*+]\s*\[[ xX]\]\s*(.+)", re.MULTILINE)
_MAX_SUB_ISSUES = 10
_DECOMPOSE_MARKER_PREFIX = "<!-- daedalus:sub-issues:"

# Idempotency marker regex: matches any variation like
#   <!-- daedalus:decomposed:123456789 -->
#   <!--daedalus:decomposed:...-->
#   <!--  daedalus:decomposed:...  -->
# The marker is posted as an HTML comment on the parent issue body or a comment.
_DECOMPOSED_MARKER_RE = re.compile(
    r"<!--\s*daedalus:decomposed(?::\d+)?\s*-->", re.IGNORECASE
)

# Regex to match fenced code blocks (``` or ~~~) with optional language tag.
_CODE_BLOCK_RE = re.compile(
    r"(?:^```[^\n]*\n.*?^```)|(?:^~~~[^\n]*\n.*?^~~~)",
    re.MULTILINE | re.DOTALL,
)


def _strip_code_blocks(text: str) -> str:
    """Remove fenced code blocks from markdown text.

    Code blocks (```...``` or ~~~...~~~) are documentation examples and should
    not trigger idempotency detection.
    """
    return _CODE_BLOCK_RE.sub("", text)


def has_decomposed_marker(text: Optional[str]) -> bool:
    """Return True if *text* contains the ``<!-- daedalus:decomposed:... -->`` marker.

    The marker is an idempotency signal: once present on a parent epic (body or
    a posted comment), re-running the dispatcher must skip decomposition entirely
    and create zero sub-issues. Detection is tolerant of whitespace variations
    and optional Unix-timestamp suffix.

    Markers inside fenced code blocks (``` or ~~~) are ignored to prevent false
    positives from documentation examples.
    """
    if not text:
        return False
    # Fast path: substring presence before regex (cheap check)
    if "daedalus:decomposed" not in text.lower():
        return False
    # Strip code blocks to avoid false positives from documentation examples
    stripped_text = _strip_code_blocks(text)
    return bool(_DECOMPOSED_MARKER_RE.search(stripped_text))


def _extract_sub_issues_from_body(body: str) -> List[str]:
    """Return checklist item texts from an epic body (capped at _MAX_SUB_ISSUES)."""
    items = [m.group(1).strip() for m in _CHECKLIST_RE.finditer(body or "")]
    return [i for i in items if i][:_MAX_SUB_ISSUES]


def _default_sub_issue_titles(parent_n: int, parent_title: str) -> List[str]:
    """Three default sub-issues for epics without checklist items."""
    return [
        f"Research & Scoping — #{parent_n}: {parent_title}",
        f"Implementation — #{parent_n}: {parent_title}",
        f"Testing & Documentation — #{parent_n}: {parent_title}",
    ]


_FILE_SYMBOL_CAP = 50


def _render_affected_files_section(
    file_paths,
    identifiers,
):
    """Return a markdown block listing files and symbols, or \'\' if both are empty."""
    files = sorted(f for f in (file_paths or []) if f)
    syms = sorted(s for s in (identifiers or []) if s)
    if not files and not syms:
        return ""
    parts = ["### Affected files & symbols\n"]
    if files:
        shown = files[:_FILE_SYMBOL_CAP]
        overflow = len(files) - len(shown)
        parts.append("**Files:**\n")
        parts.extend(f"- `{f}`\n" for f in shown)
        if overflow:
            parts.append(f"- \u2026 and {overflow} additional file(s)\n")
        parts.append("\n")
    if syms:
        shown = syms[:_FILE_SYMBOL_CAP]
        overflow = len(syms) - len(shown)
        parts.append("**Symbols:**\n")
        parts.extend(f"- `{s}`\n" for s in shown)
        if overflow:
            parts.append(f"- \u2026 and {overflow} additional symbol(s)\n")
        parts.append("\n")
    return "".join(parts)


def _sub_issue_body(
    parent_n,
    parent_title,
    scope,
    depends_on,
    file_paths=None,
    identifiers=None,
):
    deps_str = ", ".join(f"#{n}" for n in depends_on) if depends_on else ""
    depends_line = f"depends_on: {deps_str}"
    affected = _render_affected_files_section(file_paths, identifiers)
    return (
        f"Part of epic #{parent_n}: {parent_title}\n\n"
        f"{depends_line}\n\n"
        f"## Scope\n{scope}\n\n"
        f"{affected}"
        f"## Acceptance Criteria\n"
        f"- [ ] Implementation complete per scope\n"
        f"- [ ] Tests pass (unit + integration where applicable)\n"
        f"- [ ] PR opened and passing CI\n\n"
        f"## Notes\nAuto-generated by Daedalus Phase 3 epic decomposition.\n"
    )


# ── Phase 4: source file reading & context injection ────────────────────────


def _extract_keywords(text: str, max_keywords: int = 10) -> List[str]:
    """Extract meaningful identifiers from *text*, skipping stop-words."""
    stop_words = {
        "the", "a", "an", "and", "or", "in", "on", "at", "to", "for", "of",
        "with", "by", "from", "is", "are", "was", "were", "be", "been",
        "being", "have", "has", "had", "do", "does", "did", "will", "would",
        "could", "should", "may", "might", "must", "shall", "can", "need",
        "dare", "ought", "used", "as", "if", "then", "than", "when", "where",
        "while", "how", "what", "which", "who", "whom", "this", "that",
        "these", "those", "i", "me", "my", "we", "our", "you", "your", "he",
        "him", "his", "she", "her", "it", "its", "they", "them", "their",
        "not", "no", "nor", "but", "about", "up", "down", "out", "off",
        "over", "under", "again", "further", "into", "through", "during",
        "before", "after", "above", "below", "any", "all", "each", "every",
        "both", "few", "more", "most", "other", "some", "such", "only",
        "own", "same", "so", "just", "new", "add", "update", "fix", "part",
    }
    words = re.findall(r"\b[a-zA-Z_][a-zA-Z0-9_]+\b", text)
    keywords: List[str] = []
    for w in words:
        lw = w.lower()
        if len(lw) > 3 and lw not in stop_words and lw not in keywords:
            keywords.append(lw)
            if len(keywords) >= max_keywords:
                break
    return keywords


# ── Phase 4b: epic-context-informed source reading ────────────────────────────


@dataclass
class EpicContext:
    """Structured extraction of context signals from a single sub-issue scope."""
    scope: str = ""
    file_paths: list[str] = field(default_factory=list)
    identifiers: list[str] = field(default_factory=list)
    component_names: list[str] = field(default_factory=list)
    dir_tags: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)


@dataclass
class AggregateEpicContext:
    """Aggregated context across all sub-issues in an epic."""
    per_sub_issues: list[EpicContext] = field(default_factory=list)
    all_file_paths: set[str] = field(default_factory=set)
    all_identifiers: set[str] = field(default_factory=set)
    all_component_names: set[str] = field(default_factory=set)
    all_dir_tags: set[str] = field(default_factory=set)


# Well-known directories for dir-tag extraction
_KNOWN_DIRS = frozenset({"src", "lib", "app", "core", "tests", "scripts", "providers"})


def extract_epic_context(
    scope_text: str,
    known_components: set[str] | None = None,
) -> EpicContext:
    """Extract structured context signals from a scope/checklist item.

    Pure function — no filesystem access.

    Args:
        scope_text: Single sub-issue scope text.
        known_components: Optional set of known component names.
    """
    scope_text = scope_text or ""
    scope_lower = scope_text.lower()

    # 1. File paths — reuse path_re regex
    path_re = re.compile(
        r"(?:^|(?<=\s)|(?<=[\"'`(]))"
        r"([a-zA-Z0-9_][\w\-./]*[a-zA-Z0-9_\-]"
        r"\.(?:py|js|ts|jsx|tsx|java|go|rs|rb|c|cpp|h|md|yaml|yml|json|toml|sh))"
        r"(?:\b|$)"
    )
    file_paths: list[str] = []
    for m in path_re.finditer(scope_text):
        p = m.group(1)
        if p not in file_paths:
            file_paths.append(p)

    # 2. Identifiers — def/class names mentioned
    func_re = re.compile(r"\b(?:def|class)\s+([a-zA-Z_][a-zA-Z0-9_]*)\b")
    identifiers: list[str] = []
    for m in func_re.finditer(scope_text):
        name = m.group(1)
        if name not in identifiers:
            identifiers.append(name)

    # 3. Component names — cross-reference scope words against known_components
    component_names: list[str] = []
    if known_components:
        words = re.findall(r"\b[a-zA-Z_][a-zA-Z0-9_]+\b", scope_text)
        for w in words:
            lw = w.lower()
            if lw in {c.lower() for c in known_components} and lw not in component_names:
                component_names.append(lw)

    # 4. Dir tags — known directory names mentioned in scope
    dir_tags: list[str] = []
    for d in _KNOWN_DIRS:
        if re.search(rf"\b{re.escape(d)}\b", scope_lower) and d not in dir_tags:
            dir_tags.append(d)

    # 5. Keywords — significant tokens via _extract_keywords
    keywords = _extract_keywords(scope_text)

    return EpicContext(
        scope=scope_text,
        file_paths=file_paths,
        identifiers=identifiers,
        component_names=component_names,
        dir_tags=dir_tags,
        keywords=keywords,
    )


def load_known_components(workdir: str) -> set[str]:
    """Derive known component names from project structure.

    Sources:
    1. config/souls/*.md filenames — strip -daedalus.md
    2. Top-level Python package dirs
    3. core/*.py module basenames
    """
    components: set[str] = set()
    try:
        workdir_path = Path(workdir)
        if not workdir_path.exists():
            return components

        # 1. SOUL profile names
        souls_dir = workdir_path / "config" / "souls"
        if souls_dir.exists() and souls_dir.is_dir():
            for f in souls_dir.glob("*-daedalus.md"):
                name = f.stem
                if name.endswith("-daedalus"):
                    components.add(name[: -len("-daedalus")])

        # 2. Top-level package dirs
        for entry in workdir_path.iterdir():
            if entry.is_dir() and (entry / "__init__.py").exists():
                components.add(entry.name)

        # 3. core/*.py module basenames
        core_dir = workdir_path / "core"
        if core_dir.exists() and core_dir.is_dir():
            for f in core_dir.glob("*.py"):
                if f.stem != "__init__":
                    components.add(f.stem)

    except (OSError, ValueError) as exc:
        logger.debug("load_known_components: failed to scan workdir %s: %s", workdir, exc)

    return components


def filter_context_for_sub(
    file_contents: dict[str, str],
    sub_context: EpicContext,
    file_metadata: dict[str, str],
) -> dict[str, str]:
    """Filter file_contents to files relevant to this sub-issue.

    A file is relevant if its path, directory, or content matches
    sub_context signals. When no files match, returns all unchanged
    (graceful degradation).
    """
    if not file_contents:
        return file_contents

    # If the sub-context has NO signals at all, return everything (graceful)
    has_any_signal = (
        sub_context.file_paths
        or sub_context.identifiers
        or sub_context.component_names
        or sub_context.dir_tags
    )
    if not has_any_signal:
        return file_contents

    relevant: dict[str, str] = {}
    for file_path, content in file_contents.items():
        # Check signal match
        if _file_matches_sub_context(file_path, content, sub_context):
            relevant[file_path] = content

    # Graceful degradation: if no match, return all
    return relevant if relevant else file_contents


def _file_matches_sub_context(
    file_path: str,
    content: str,
    ctx: EpicContext,
) -> bool:
    """Return True when *file_path*/*content* match any sub-context signal."""
    path_lower = file_path.lower()

    # File path matches explicit paths mentioned
    for p in ctx.file_paths:
        if p.lower() in path_lower or path_lower.endswith(p.lower()):
            return True

    # Directory matches dir_tags
    for d in ctx.dir_tags:
        if f"/{d}/" in path_lower or path_lower.startswith(f"{d}/"):
            return True

    # Content contains identifiers
    for ident in ctx.identifiers:
        if ident in content:
            return True

    # Content contains component names
    for comp in ctx.component_names:
        if comp in content.lower():
            return True

    return False


def _build_aggregate_context(
    checklist_items: list[str],
    known_components: set[str] | None = None,
) -> AggregateEpicContext:
    """Build AggregateEpicContext from per-checklist-item scopes."""
    per_sub = [extract_epic_context(item, known_components) for item in checklist_items]
    agg_file_paths: set[str] = set()
    agg_identifiers: set[str] = set()
    agg_component_names: set[str] = set()
    agg_dir_tags: set[str] = set()
    for ctx in per_sub:
        agg_file_paths.update(ctx.file_paths)
        agg_identifiers.update(ctx.identifiers)
        agg_component_names.update(ctx.component_names)
        agg_dir_tags.update(ctx.dir_tags)
    return AggregateEpicContext(
        per_sub_issues=per_sub,
        all_file_paths=agg_file_paths,
        all_identifiers=agg_identifiers,
        all_component_names=agg_component_names,
        all_dir_tags=agg_dir_tags,
    )


def identify_relevant_files(
    scope_text: str,
    workdir: str,
    max_files: int = 10,
    max_depth: int = 5,
    epic_context: "AggregateEpicContext | None" = None,
) -> tuple[List[Path], dict]:
    """Identify source files in *workdir* relevant to *scope_text*.

    Four strategies, each gated so they only fire when the scope actually
    provides a signal — otherwise we return nothing (graceful degradation).

    1. **Path extraction** — explicit ``src/foo.py`` mentions in the scope.
    2. **Function/class scan** — ``def X`` / ``class Y`` patterns, grepped.
    3. **Directory heuristic** — scan common dirs (src/lib/app/core/tests)
       only when the scope mentions one of those names.
    4. **Extension fallback** — only if earlier strategies already found
       candidates and we're below *max_files*.
    """
    import subprocess as _sp

    workdir_path = Path(workdir)
    if not workdir_path.exists():
        logger.warning("identify_relevant_files: workdir %s does not exist", workdir)
        return ([], {})

    candidates: Set[Path] = set()
    metadata: dict[str, str] = {}

    def _add(p: Path, why: str) -> bool:
        if p in candidates:
            return False
        candidates.add(p)
        metadata[str(p)] = why
        return len(candidates) >= max_files

    # ── Epic-context priority boost (Strategy 0) ─────────────────────────
    # When an AggregateEpicContext is provided, files mentioned in its
    # all_file_paths are added FIRST with the highest strategy tag, so they
    # appear before scope-only matches. All aggregated identifiers are also
    # grepped directly, giving the planner a wider signal window.
    if epic_context is not None:
        for p in sorted(epic_context.all_file_paths):
            try:
                fp = workdir_path / p
                if fp.exists() and fp.is_file() and fp.resolve().is_relative_to(workdir_path.resolve()):
                    if _add(fp, "epic_context:path"):
                        break
            except (OSError, ValueError):
                continue
        # Grep for aggregated identifiers directly (more precise than
        # re-extracting from raw scope text).
        if not (len(candidates) >= max_files):
            import subprocess as _sp_agg
            for ident in sorted(epic_context.all_identifiers):
                try:
                    res = _sp_agg.run(
                        ["grep", "-rl", f"--include=*.py", "-e", f"def {ident}", "-e", f"class {ident}", workdir],
                        capture_output=True, text=True, timeout=5,
                    )
                except (_sp_agg.SubprocessError, _sp_agg.TimeoutExpired, OSError):
                    continue
                if res.returncode == 0:
                    for line in res.stdout.splitlines():
                        fp = Path(line.strip())
                        try:
                            if fp.exists() and fp.resolve().is_relative_to(workdir_path.resolve()):
                                if _add(fp, f"epic_context:ident:{ident}"):
                                    break
                        except (OSError, ValueError):
                            continue
                if len(candidates) >= max_files:
                    break

    # Strategy 1 — explicit file paths mentioned in scope text.
    # Matches things like ``src/foo.py``, ``./lib/utils.js``, ``core/iterate.py``.
    path_re = re.compile(
        r"(?:^|(?<=\s)|(?<=[\"'`(]))"
        r"([a-zA-Z0-9_][\w\-./]*[a-zA-Z0-9_\-]"
        r"\.(?:py|js|ts|jsx|tsx|java|go|rs|rb|c|cpp|h|md|yaml|yml|json|toml|sh))"
        r"(?:\b|$)",
    )
    for m in path_re.finditer(scope_text or ""):
        fp = workdir_path / m.group(1)
        try:
            if fp.exists() and fp.is_file() and fp.resolve().is_relative_to(workdir_path.resolve()):
                if _add(fp, "path_extraction"):
                    break
        except (OSError, ValueError):
            continue

    # Strategy 2 — grep for def/class declarations named in the scope.
    func_re = re.compile(r"\b(?:def|class)\s+([a-zA-Z_][a-zA-Z0-9_]*)\b")
    for m in func_re.finditer(scope_text or ""):
        name = m.group(1)
        try:
            res = _sp.run(
                ["grep", "-rl", f"--include=*.py", "-e", f"def {name}", "-e", f"class {name}", workdir],
                capture_output=True, text=True, timeout=5,
            )
        except (_sp.SubprocessError, _sp.TimeoutExpired, OSError):
            continue
        if res.returncode == 0:
            for line in res.stdout.splitlines():
                fp = Path(line.strip())
                try:
                    if fp.exists() and fp.resolve().is_relative_to(workdir_path.resolve()):
                        if _add(fp, f"definition_scan:{name}"):
                            break
                except (OSError, ValueError):
                    continue
            if len(candidates) >= max_files:
                break

    # Strategy 3 — directory heuristic. Only fires when scope mentions
    # one of the common directory names, so "Add new feature" returns
    # empty rather than dumping the whole repo.
    common_dirs = ["src", "lib", "app", "core", "tests"]
    scope_lower = (scope_text or "").lower()
    scope_mentions_dir = any(
        re.search(rf"\b{re.escape(d)}\b", scope_lower) for d in common_dirs
    )
    if scope_mentions_dir:
        code_exts = ("*.py", "*.js", "*.ts", "*.jsx", "*.tsx", "*.java", "*.go", "*.rs")
        for dir_name in common_dirs:
            if not re.search(rf"\b{re.escape(dir_name)}\b", scope_lower):
                continue
            dir_path = workdir_path / dir_name
            if not (dir_path.exists() and dir_path.is_dir()):
                continue
            # BFS up to max_depth, but cap total files aggressively.
            for ext in code_exts:
                for fp in dir_path.rglob(ext):
                    # Respect max_depth
                    try:
                        rel = fp.resolve().relative_to(dir_path.resolve())
                        if len(rel.parts) > max_depth:
                            continue
                    except (OSError, ValueError):
                        continue
                    if fp.is_file():
                        if _add(fp, f"directory_scan:{dir_name}"):
                            break
                if len(candidates) >= max_files:
                    break
            if len(candidates) >= max_files:
                break

    # Strategy 4 — extension fallback. Only if we already have signal AND
    # haven't hit max_files yet. Walks the repo once.
    if 0 < len(candidates) < max_files:
        code_exts = ("*.py", "*.js", "*.ts", "*.jsx", "*.tsx", "*.java", "*.go", "*.rs")
        for ext in code_exts:
            if len(candidates) >= max_files:
                break
            for fp in workdir_path.rglob(ext):
                try:
                    if not fp.is_file() or not fp.resolve().is_relative_to(workdir_path.resolve()):
                        continue
                    # Skip common non-source dirs
                    rel = str(fp.relative_to(workdir_path))
                    if any(seg in rel for seg in ("node_modules", ".git", "__pycache__", ".venv", "venv", ".tox")):
                        continue
                except (OSError, ValueError):
                    continue
                if _add(fp, f"extension_fallback:{ext}"):
                    break
            if len(candidates) >= max_files:
                break

    return (list(candidates), metadata)


def read_source_files(
    file_paths: List[Path],
    workdir: str,
    max_size: int = 50_000,
) -> dict[str, str]:
    """Read source files with safety checks.

    - **Binary detection** — skip files whose first 1 KiB contains a NUL byte.
    - **Size limit** — truncate UTF-8 output to *max_size* bytes.
    - **Symlink safety** — resolve symlinks before reading.
    - **Path traversal** — refuse files outside *workdir*.
    """
    contents: dict[str, str] = {}
    try:
        workdir_path = Path(workdir).resolve()
    except (OSError, ValueError):
        return contents

    for file_path_obj in file_paths:
        try:
            resolved = file_path_obj.resolve()
            # Path-traversal guard
            if not resolved.is_relative_to(workdir_path):
                logger.warning("read_source_files: path traversal blocked: %s", file_path_obj)
                continue
            if not resolved.exists() or not resolved.is_file():
                logger.warning("read_source_files: file not found: %s", file_path_obj)
                continue
            # Binary detection — NUL byte in first 1 KiB
            with open(resolved, "rb") as fh:
                head = fh.read(1024)
                if b"\x00" in head:
                    logger.info("read_source_files: skipping binary file: %s", file_path_obj)
                    continue
            raw = resolved.read_text(encoding="utf-8", errors="ignore")
            # Truncate to max_size bytes (not chars)
            encoded_len = len(raw.encode("utf-8"))
            if encoded_len > max_size:
                # Binary-search the char cutoff that produces ≤ max_size bytes
                cutoff = max_size
                raw = raw[:cutoff]
                while len(raw.encode("utf-8")) > max_size and cutoff > 0:
                    cutoff = max(0, cutoff - max(1, (len(raw.encode("utf-8")) - max_size)))
                    raw = raw[:cutoff]
                logger.info(
                    "read_source_files: truncated %s from %d to %d bytes",
                    file_path_obj, encoded_len, len(raw.encode("utf-8")),
                )
            # Dict keys use the original string form for stable lookups.
            contents[str(file_path_obj)] = raw
        except Exception as exc:  # noqa: BLE001
            logger.warning("read_source_files: error reading %s: %s", file_path_obj, exc)
    return contents


def build_sub_issue_context(file_contents: dict[str, str]) -> str:
    """Format file contents into a markdown context block."""
    if not file_contents:
        return ""
    lines = ["## Relevant Source Context", ""]
    for file_path, content in file_contents.items():
        lines.append(f"### `{file_path}`")
        lines.append("")
        lines.append("```")
        lines.append(content)
        lines.append("```")
        lines.append("")
    return "\n".join(lines)


def build_enhanced_scope(original_scope: str, source_context: str) -> str:
    """Combine *original_scope* with *source_context*.

    If *source_context* is empty, return the original unchanged (graceful
    degradation). Otherwise append the context as an additional section.
    """
    if not source_context:
        return original_scope
    return f"{original_scope}\n\n{source_context}"


def _execute_planner_decompose(
    slug: str,
    card: dict,
    repo: str,
    handoff_text: str,
    *,
    workdir: str = "",
    dry_run: bool = False,
    provider: Any = None,
    **_kwargs: Any,
) -> bool:
    """Create sub-issues from an epic when the planner completes with PLANNING COMPLETE."""
    tid = card.get("id")
    parent_n = _extract_issue_number_from_card(card)
    if parent_n is None:
        logger.warning("iterate: planner_decompose — cannot parse issue number from card %s", tid)
        return False

    if provider is None:
        logger.warning("iterate: planner_decompose #%s — no provider (kanban-only mode), skipping", parent_n)
        return False

    parent = provider.get_issue(parent_n)
    if parent is None:
        logger.warning("iterate: planner_decompose #%s — get_issue returned None", parent_n)
        return False

    parent_dict = parent.as_dict() if hasattr(parent, "as_dict") else parent
    parent_title = parent_dict.get("title") or ""
    parent_body = parent_dict.get("body") or ""
    parent_labels = [
        (lbl if isinstance(lbl, str) else lbl.get("name", ""))
        for lbl in (parent_dict.get("labels") or [])
    ]

    # Idempotency: skip if any marker already posted in body or comments.
    # Two marker variants are checked (legacy and new):
    #   - <!-- daedalus:sub-issues:[...] -->  (legacy — only in comments)
    #   - <!-- daedalus:decomposed[:timestamp] -->  (new — in body OR comments)
    if has_decomposed_marker(parent_body):
        logger.info("iterate: planner_decompose #%s — already decomposed (body marker), skipping", parent_n)
        kanban.complete(slug, tid, summary=f"Already decomposed epic #{parent_n}")
        return True

    existing_comments = provider.get_issue_comments(parent_n) or []
    for c in existing_comments:
        body = c.get("body") or "" if isinstance(c, dict) else getattr(c, "body", "")
        if has_decomposed_marker(body) or _DECOMPOSE_MARKER_PREFIX in body:
            logger.info("iterate: planner_decompose #%s — already decomposed (comment marker), skipping", parent_n)
            kanban.complete(slug, tid, summary=f"Already decomposed epic #{parent_n}")
            return True

    checklist_items = _extract_sub_issues_from_body(parent_body)
    if checklist_items:
        sub_titles = checklist_items
        sub_scopes = checklist_items
    else:
        sub_titles = _default_sub_issue_titles(parent_n, parent_title)
        sub_scopes = [t.split(" — ", 1)[0] for t in sub_titles]

    # Phase 4: source-file reading & context injection
    # If reading fails or workdir is unavailable, fall back to Phase 3
    # behavior (template-only generation without analysis).
    global _source_reading_fallback_count
    # If reading fails or workdir is unavailable, fall back to Phase 3
    # behavior (template-only generation without analysis).
    global _source_reading_fallback_count
    full_issue_text = f"{parent_title}\n\n{parent_body}"
    source_context = ""
    file_contents: dict[str, str] = {}
    file_metadata: dict[str, str] = {}
    epic_agg: AggregateEpicContext | None = None
    per_sub_contexts: list[EpicContext] = []
    if workdir and Path(workdir).exists():
        try:
            # Build per-sub-issue epic context from checklist items
            known_components = load_known_components(workdir)
            per_sub_contexts = [extract_epic_context(item, known_components) for item in sub_scopes]
            epic_agg = _build_aggregate_context(sub_scopes, known_components)

            rel_files, file_metadata = identify_relevant_files(full_issue_text, workdir, epic_context=epic_agg)
            if rel_files:
                file_contents = read_source_files(rel_files, workdir)
                source_context = build_sub_issue_context(file_contents)
                logger.info(
                    "iterate: planner_decompose #%s — injected context from %d source files",
                    parent_n, len(file_contents),
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "iterate: planner_decompose #%s — source-reading failed (degrading gracefully): %s",
                parent_n, exc,
            )
            _source_reading_fallback_count += 1
    else:
        logger.info(
            "iterate: planner_decompose #%s — workdir unavailable (%s), skipping codebase reading (Phase 3 fallback)",
            parent_n, workdir or "<empty>",
        )
        _source_reading_fallback_count += 1

    if dry_run:
        logger.info("[dry-run] planner_decompose #%s: would create %d sub-issues: %s",
                    parent_n, len(sub_titles), sub_titles)
        return True
    inherit_labels = [l for l in parent_labels if l and l.lower() != "epic"]
    created_numbers: List[int] = []
    ready_numbers: List[int] = []
    for idx, (title, scope) in enumerate(zip(sub_titles, sub_scopes)):
        # Per-sub-issue scoped context: filter full file_contents to files
        # relevant only to this sub-issue's EpicContext.
        sub_ctx = per_sub_contexts[idx] if idx < len(per_sub_contexts) else EpicContext()
        if file_contents and epic_agg is not None and any(
            sub_ctx.file_paths or sub_ctx.identifiers or sub_ctx.dir_tags or sub_ctx.component_names
        ):
            filtered_contents = filter_context_for_sub(file_contents, sub_ctx, file_metadata)
            scoped_context = build_sub_issue_context(filtered_contents)
        else:
            scoped_context = source_context

        enhanced_scope = build_enhanced_scope(scope, scoped_context) if scoped_context else scope
        # Each sub-issue depends on all previously created sub-issues in this epic
        # (sequential tier ordering). The first sub-issue has empty depends_on and
        # is immediately actionable — labeled Ready below.
        # Pass per-sub-issue file_paths and identifiers from epic context analysis
        sub_body = _sub_issue_body(
            parent_n,
            parent_title,
            enhanced_scope,
            list(created_numbers),
            file_paths=sub_ctx.file_paths,
            identifiers=sub_ctx.identifiers,
        )
        sub_labels = inherit_labels + ["subtask"]
        sub_n = provider.create_issue(title, sub_body, labels=sub_labels)
        if sub_n is not None:
            created_numbers.append(sub_n)
            logger.info("iterate: planner_decompose — created sub-issue #%s: %s", sub_n, title)
            
            # Check if this sub-issue has any dependencies
            dependencies = parse_depends_on(sub_body)
            
            if not dependencies:
                # No dependencies = immediately actionable, apply Ready label
                provider.add_label(sub_n, "Ready")
                ready_numbers.append(sub_n)
                logger.info("iterate: planner_decompose — applied Ready label to sub-issue #%s (no dependencies)", sub_n)
            else:
                logger.info("iterate: planner_decompose — sub-issue #%s has %d dependencies, skipping Ready label", sub_n, len(dependencies))
        else:
            logger.warning("iterate: planner_decompose — create_issue failed for %r", title)

    # Post idempotency marker on parent
    marker_numbers = f"[{','.join(str(n) for n in created_numbers)}]"
    provider.post_issue_comment(
        parent_n,
        f"{_DECOMPOSE_MARKER_PREFIX}{marker_numbers} -->\n"
        f"Daedalus created {len(created_numbers)} sub-issue(s): "
        + ", ".join(f"#{n}" for n in created_numbers),
    )

    # Apply epic label to parent
    provider.add_label(parent_n, "epic")

    # Create kanban triage card per sub-issue and invoke decompose immediately
    ws = f"dir:{workdir}" if workdir else ""
    for sub_n in created_numbers:
        sub_issue = provider.get_issue(sub_n)
        if sub_issue is None:
            continue
        sub_dict = sub_issue.as_dict() if hasattr(sub_issue, "as_dict") else sub_issue
        triage_tid = kanban.create_triage(
            slug, sub_n, sub_dict.get("title", f"sub-issue #{sub_n}"),
            body=sub_dict.get("body", ""),
            idempotency_key=f"epic-sub-{sub_n}",
            workspace=ws,
        )
        # Decompose immediately so the fan-out happens now rather than waiting
        # for the next dispatcher tick's decompose_all_triage() sweep.
        if triage_tid:
            decomposed = kanban.decompose(slug, triage_tid)
            if not decomposed:
                logger.warning(
                    "iterate: planner_decompose — decompose(%s) failed for sub-issue #%s; "
                    "triage card will be swept on next tick",
                    triage_tid, sub_n,
                )

    kanban.complete(slug, tid,
                    summary=f"Decomposed epic #{parent_n} into {len(created_numbers)} sub-issues ({len(ready_numbers)} Ready)")
    logger.info("iterate: planner_decompose — completed #%s with %d sub-issues",
                parent_n, len(created_numbers))
    return True


# ── action lookup ───────────────────────────────────────────────────────────

_ACTION_EXECUTORS = {
    ADVANCE: _execute_advance,
    DEV_FIX_CI: _execute_dev_fix_ci,
    PENDING_PR: _execute_pending_pr,
    PM_ROUTE: _execute_pm_route,
    APPROVE_ADVANCE: _execute_approve_advance,
    ESCALATE: _execute_escalate,
    PLANNER_DECOMPOSE: _execute_planner_decompose,
}


# ── main loop ───────────────────────────────────────────────────────────────


def run_iterate(
    slug: str,
    repo: str,
    *,
    resolved: Optional[Dict[str, Any]] = None,
    provider: Optional[Any] = None,
    dry_run: bool = False,
) -> tuple[Dict[str, int], List[int], List[Dict[str, Any]]]:
    """Run the auto-advance routing and self-healing loop.

    For every blocked card on the board, classify its state and execute the
    appropriate action. Returns (counts, advance_prs, pending_ci_cards) where
    advance_prs lists PR numbers for cards that were successfully advanced,
    and pending_ci_cards lists cards skipped because CI was still pending.

    Args:
        slug: Kanban board slug.
        repo: Repo identifier (org/name) — used in card bodies only.
        resolved: Optional resolved project config (for workdir, notify_target).
        provider: Optional VCS provider (core.providers.VCSProvider) for PR/CI
            lookups. Without one, branch→PR resolution is skipped and CI is
            treated as not-green.
        dry_run: If True, log intentions without mutating anything.

    Returns:
        (counts, advance_prs, pending_ci_cards) tuple — counts has action→int,
        advance_prs has PR numbers, pending_ci_cards has card info for retry.
    """
    counts: Dict[str, int] = {
        ADVANCE: 0,
        DEV_FIX_CI: 0,
        PENDING_CI: 0,
        PENDING_PR: 0,
        PM_ROUTE: 0,
        APPROVE_ADVANCE: 0,
        ESCALATE: 0,
        PLANNER_DECOMPOSE: 0,
    }
    advance_prs: List[int] = []  # PR numbers for cards that were advanced
    pending_ci_cards: List[Dict[str, Any]] = []  # Cards skipped due to PENDING CI

    workdir = (resolved or {}).get("workdir", "")
    notify_target = (resolved or {}).get("cron", {}).get("deliver", "")
    router_profile = (resolved or {}).get("router_profile", "project-manager-daedalus")
    execution = (resolved or {}).get("execution") or {}
    auto_merge = bool(execution.get("auto_merge", False))
    merge_method = str(execution.get("merge_method", "squash")).lower()

    blocked_cards = kanban.list_blocked(slug)
    if not blocked_cards:
        return counts, advance_prs, pending_ci_cards

    # Collect PR→CI cache so we don't call the provider for the same PR twice.
    # Stores the raw CIStatus string (not bool) so UNKNOWN/PENDING are distinguishable.
    ci_cache: Dict[int, str] = {}

    # Per-tick escalation dedup: tracks which issue numbers have already been
    # escalated this tick. Maps issue number → first card's tid that escalated.
    escalated_issues: Dict[int, str] = {}

    for card in blocked_cards:
        tid = card.get("id")
        if not tid:
            continue

        assignee = (card.get("assignee") or "").strip()
        handoff = _handoff_from_card(card)

        # Fallback: list_blocked returns minimal dicts without runs/reasons.
        # Fetch the full card detail via show_card and use latest_summary.
        if not handoff and tid:
            detail = kanban.show_card(slug, tid)
            if detail:
                handoff = (detail.get("latest_summary") or "").strip()

        fix_attempts = _count_fix_attempts(card)

        pr = _parse_pr_number(handoff)

        # Fallback: if handoff has no PR #, try the card's branch_name.
        if pr is None:
            branch_name = (card.get("branch_name") or "").strip()
            if branch_name and provider is not None:
                pr = provider.find_pr_for_branch(branch_name)
                if pr is not None:
                    logger.info("iterate: %s resolved PR #%s via branch %s",
                                tid, pr, branch_name)

        ci_green = False
        raw_ci = CIStatus.UNKNOWN
        if pr is not None and provider is not None:
            if pr not in ci_cache:
                ci_cache[pr] = provider.get_pr_ci_status(pr)
            raw_ci = ci_cache[pr]

            # No CI configured → no gate: treat UNKNOWN as green when the
            # provider doesn't support CI status checks (e.g. no check runs).
            if not getattr(provider, "supports_ci_status", False) and raw_ci == CIStatus.UNKNOWN:
                logger.info("iterate: %s provider has no CI support — treating as green", tid)
                ci_green = True
            else:
                ci_green = (raw_ci == CIStatus.GREEN)

        action = classify_blocked(assignee, handoff, ci_green,
                                  fix_attempts=fix_attempts, pr_number=pr,
                                  raw_ci=raw_ci)

        # ── Escalation dedup (issue #35) ─────────────────────────────────
        # Before executing ESCALATE, check two layers of dedup:
        #   1. Cross-tick stamp: card already has "escalated: issue #N" comment.
        #   2. Per-tick sentinel: another card already escalated for this issue.
        # Both layers skip the card silently (or complete duplicates).
        if action == ESCALATE:
            issue_n = _extract_issue_number_from_card(card)

            # Layer 2: per-tick dedup (different card, same issue, same tick)
            if issue_n is not None and issue_n in escalated_issues:
                first_tid = escalated_issues[issue_n]
                if dry_run:
                    logger.info(
                        "[dry-run] would skip duplicate ESCALATE for %s "
                        "(already escalated by %s)", tid, first_tid)
                else:
                    logger.info(
                        "iterate: %s skipping duplicate ESCALATE for "
                        "issue #%s (already escalated by %s)",
                        tid, issue_n, first_tid)
                    kanban.complete(
                        slug, tid,
                        summary=f"skipped: escalated by {first_tid}")
                continue

            # Layer 1: cross-tick stamp (same card, previous tick already escalated)
            if issue_n is not None and _is_card_already_escalated(slug, tid, issue_n):
                logger.info(
                    "iterate: %s already stamped escalated: issue #%s — skipping",
                    tid, issue_n)
                continue

            # Record this card as the escalation owner for this issue/tick
            if issue_n is not None:
                escalated_issues[issue_n] = tid

        # PENDING_CI is a skip-action: card goes to pending_ci_cards for the
        # CI retry cron to pick up when CI resolves. No executor needed.
        if action == PENDING_CI:
            pending_ci_cards.append({"tid": tid, "pr": pr, "card": card})
            counts[PENDING_CI] += 1
            logger.info("iterate: %s CI still pending — deferred to retry cron", tid)
            continue

        # PENDING_PR: run the executor inline (it updates the block reason when
        # a PR is found; if no PR yet it's a no-op). Count and continue.
        if action == PENDING_PR:
            _execute_pending_pr(slug, card, repo, handoff, provider=provider, dry_run=dry_run)
            counts[PENDING_PR] += 1
            logger.info("iterate: %s awaiting PR for issue #%s", tid,
                        _extract_issue_number_from_card(card))
            continue

        if not action:
            continue  # nothing to do for this card

        executor = _ACTION_EXECUTORS.get(action)
        if not executor:
            logger.warning("iterate: unknown action '%s' for card %s", action, tid)
            continue

        try:
            ok = executor(
                slug, card, repo, handoff,
                workdir=workdir,
                notify_target=notify_target,
                router_profile=router_profile,
                dry_run=dry_run,
                pr_number=pr,
                provider=provider,
            )
            if ok:
                counts[action] += 1
                # Track PR number for advance actions so the human summary can
                # report which PRs were advanced (not just a count tuple).
                if action == ADVANCE and pr is not None:
                    advance_prs.append(pr)

                # Auto-merge: when the docs card completes and auto_merge is enabled,
                # the dispatcher merges the PR via the VCS API. This is the ONLY path
                # that can trigger a merge — agents never merge directly.
                if (
                    action == APPROVE_ADVANCE
                    and assignee == "documentation-daedalus"
                    and auto_merge
                    and pr is not None
                    and provider is not None
                ):
                    if dry_run:
                        logger.info(
                            "[dry-run] auto_merge=true: would merge PR #%s (%s)", pr, merge_method)
                    else:
                        merged = provider.merge_pr(pr, merge_method=merge_method)
                        if merged:
                            logger.info(
                                "iterate: auto-merged PR #%s (%s) after docs complete",
                                pr, merge_method)
                        else:
                            logger.warning(
                                "iterate: auto_merge failed for PR #%s — leaving open for human",
                                pr)
        except Exception as e:
            logger.error("iterate: executor %s failed for card %s: %s", action, tid, e)

    return counts, advance_prs, pending_ci_cards

