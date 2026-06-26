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
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from core import kanban
from core.providers.base import CIStatus, issue_linked_to_pr
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

# Maximum fix attempts per PR before escalation
MAX_FIX_ATTEMPTS = 3

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

    # ── planner → PM route ───────────────────────────────────────────────
    if assignee == "planner-daedalus":
        return PM_ROUTE

    # ── documentation-daedalus → terminal complete ────────────────────────
    # Docs is the last pipeline stage. When it blocks with 'docs posted:'
    # the job is done — complete the card. Anything else routes to PM.
    if assignee == "documentation-daedalus":
        if "docs posted" in (handoff_text or "").lower():
            return APPROVE_ADVANCE
        return PM_ROUTE

    # ── project-manager blocked → escalate (human gate — PM can't consult itself) ──
    if assignee == "project-manager-daedalus":
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
        # No PR or not review-required → PM route (was: silent drop returning "")
        return PM_ROUTE

    # ── reviewer / security-analyst card ─────────────────────────────────
    if assignee in ("reviewer-daedalus", "security-analyst-daedalus"):
        # Exceeded max fix attempts → escalate
        if fix_attempts >= MAX_FIX_ATTEMPTS:
            return ESCALATE
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
    if tid and slug:
        tasks = kanban.list_tasks(slug)
        board_count = 0
        for task in tasks:
            ikey = (task.get("idempotency_key") or "")
            # Fix card idempotency keys:
            #   fix-ci-{tid}-attempt-N   (dev fix for CI-red)
            #   fix-review-{tid}-attempt-N  (legacy direct-dev review fix)
            #   pm-route-{tid}-attempt-N   (PM routing card)
            if f"-{tid}-attempt-" in ikey:
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
        f"Create the appropriate fix card (pinned, assigned to the chosen profile) "
        f"with the findings. When the fix lands green, unblock and re-engage the "
        f"original reviewer/security-analyst cards."
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


# ── action lookup ───────────────────────────────────────────────────────────

_ACTION_EXECUTORS = {
    ADVANCE: _execute_advance,
    DEV_FIX_CI: _execute_dev_fix_ci,
    PENDING_PR: _execute_pending_pr,
    PM_ROUTE: _execute_pm_route,
    APPROVE_ADVANCE: _execute_approve_advance,
    ESCALATE: _execute_escalate,
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
