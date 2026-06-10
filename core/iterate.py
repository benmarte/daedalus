"""CI-aware auto-advance routing and self-healing loop.

For every blocked card on the board, classify its blocked state into an action,
then execute that action (complete, create fix-up tasks, unblock, escalate).
Runs as part of the daedalus dispatcher auto-advance block.

Pure helpers are unit-testable; the executors call ``core.kanban`` and
``core.github_project`` and are guarded so failures log and continue.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from core import github_project as gp
from core import kanban

logger = logging.getLogger("daedalus.iterate")

# Actions that classify_blocked can return
ADVANCE = "advance"            # dev card with green CI → complete, advance chain
DEV_FIX_CI = "dev_fix_ci"     # dev card with red CI → create fix card
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

    Returns one of: {advance, dev_fix_ci, pm_route, approve_advance, escalate}.
    """
    assignee = (card_assignee or "").lower().strip()
    handoff = _parse_handoff(handoff_text)

    # Resolve PR number: handoff first, then the explicit fallback.
    effective_pr = handoff["pr_number"] or pr_number

    # ── developer card ───────────────────────────────────────────────────
    if assignee == "developer":
        # Exceeded max fix attempts → escalate
        if fix_attempts >= MAX_FIX_ATTEMPTS:
            return ESCALATE
        # Review-required handoff with PR → check CI
        if handoff["is_review_required"] and effective_pr:
            if ci_green:
                return ADVANCE
            else:
                return DEV_FIX_CI
        # No PR or not review-required — nothing to do (leave blocked)
        return ""

    # ── reviewer / security-analyst card ─────────────────────────────────
    if assignee in ("reviewer", "security-analyst"):
        # Exceeded max fix attempts → escalate
        if fix_attempts >= MAX_FIX_ATTEMPTS:
            return ESCALATE
        if handoff["is_changes_requested"]:
            return PM_ROUTE
        if handoff["is_approved"]:
            return APPROVE_ADVANCE
        return ""

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
    return True


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
        assignee="developer",
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


def _execute_pm_route(
    slug: str,
    card: dict,
    repo: str,
    handoff_text: str,
    *,
    workdir: str = "",
    router_profile: str = "project-manager",
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
        assignee="developer",
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
    """Escalate a card that has exceeded max fix attempts."""
    tid = card.get("id")
    pr = pr_number or _parse_pr_number(handoff_text)
    msg = (
        f"⚠️ ESCALATE: card {tid} (PR #{pr or '?'}) has exceeded "
        f"{MAX_FIX_ATTEMPTS} fix attempts. Manual intervention required."
    )
    logger.warning("iterate: %s", msg)

    if dry_run:
        logger.info("[dry-run] would escalate %s", tid)
        return True

    kanban.comment(slug, tid, msg)
    # Leave the card blocked for human review (do not auto-advance)
    return True


# ── action lookup ───────────────────────────────────────────────────────────

_ACTION_EXECUTORS = {
    ADVANCE: _execute_advance,
    DEV_FIX_CI: _execute_dev_fix_ci,
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
    dry_run: bool = False,
) -> tuple[Dict[str, int], List[int]]:
    """Run the auto-advance routing and self-healing loop.

    For every blocked card on the board, classify its state and execute the
    appropriate action. Returns (counts, advance_prs) where advance_prs lists
    PR numbers for cards that were successfully advanced.

    Args:
        slug: Kanban board slug.
        repo: GitHub repo (org/name).
        resolved: Optional resolved project config (for workdir, notify_target).
        dry_run: If True, log intentions without mutating anything.

    Returns:
        (counts, advance_prs) tuple — counts has action→int, advance_prs has PR numbers.
    """
    counts: Dict[str, int] = {
        ADVANCE: 0,
        DEV_FIX_CI: 0,
        PM_ROUTE: 0,
        APPROVE_ADVANCE: 0,
        ESCALATE: 0,
    }
    advance_prs: List[int] = []  # PR numbers for cards that were advanced

    workdir = (resolved or {}).get("workdir", "")
    notify_target = (resolved or {}).get("cron", {}).get("deliver", "")
    router_profile = (resolved or {}).get("router_profile", "project-manager")

    blocked_cards = kanban.list_blocked(slug)
    if not blocked_cards:
        return counts, advance_prs

    # Collect PR→CI cache so we don't call gh for the same PR twice
    ci_cache: Dict[int, bool] = {}

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
            if branch_name:
                pr = gp.open_pr_for_branch(repo, branch_name)
                if pr is not None:
                    logger.info("iterate: %s resolved PR #%s via branch %s",
                                tid, pr, branch_name)

        ci_green = False
        if pr is not None:
            if pr not in ci_cache:
                ci_cache[pr] = gp.pr_ci_green(repo, pr)
            ci_green = ci_cache[pr]

        action = classify_blocked(assignee, handoff, ci_green,
                                  fix_attempts=fix_attempts, pr_number=pr)

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
        except Exception as e:
            logger.error("iterate: executor %s failed for card %s: %s", action, tid, e)

    return counts, advance_prs
