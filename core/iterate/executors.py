"""core.iterate.executors — Executor functions and their private helpers.

This is the second extracted layer of core/iterate (PR 2/3, issue #1154).

Package layout (final after PR 3/3):

  classify.py   — action constants, _parse_handoff, classify_blocked  (PR 1/3)
  executors.py  — decompose lock, fix-attempt tracking, role-gate helpers,
                  all _execute_* functions, planner helpers, _ACTION_EXECUTORS (PR 2/3)
  sources.py    — Phase 4 epic-context extraction + source-file reading (PR 3/3)
  gates.py      — block-loop rescue, merge-gate, CI-rerun, sweep_deferred_merges
                  (PR 3/3)
  __init__.py   — package root: re-exports all layers + kanban binding,
                  _source_reading_fallback_count, run_iterate

kanban BINDING MECHANICS
------------------------
This module must NOT bind ``kanban`` at import time via ``from core import kanban``.
Doing so would bypass the package-level mock-patch style used by the test suite:

    mock.patch("core.iterate.kanban", fake_kanban)

When a test replaces ``core.iterate.kanban`` with a FakeKanban, only the NAME in
``core.iterate.__init__``'s namespace is swapped.  Any executor that already holds
the original ``core.kanban`` module object would call the wrong object — the real
kanban with a stubbed ``_hk`` that returns failure codes for every operation.

The fix: all kanban access in this module goes through ``_pkg().kanban`` at call
time, where ``_pkg()`` returns ``sys.modules["core.iterate"]``.  This resolves to
whatever the package's ``kanban`` attribute is at the moment of the call — real
module OR FakeKanban — so both patch styles work:

  Style 1 (whole-object replacement):
    mock.patch("core.iterate.kanban", fk)
    → replaces the 'kanban' attr in core.iterate.__init__
    → _pkg().kanban returns fk  ✓

  Style 2 (method mutation on the module object):
    mock.patch("core.iterate.kanban.list_tasks", return_value=[...])
    → patches list_tasks on the kanban MODULE OBJECT
    → _pkg().kanban.list_tasks returns the mock  ✓
    → also visible via any direct ``from core import kanban`` binding  ✓

The same mechanism covers every package-level name that tests rebind and that
executor code calls internally.  Audited patch targets and their resolution:

  Patch target                              Resolved via
  ───���────────────────────────────────────  ──────────────────────────────────────
  core.iterate.kanban                       _pkg().kanban  (all executor funcs)
  core.iterate.kanban.X                     _pkg().kanban.X  (attribute on object)
  core.iterate.load_known_components        _pkg().load_known_components
  core.iterate.identify_relevant_files      _pkg().identify_relevant_files
  core.iterate.read_source_files            _pkg().read_source_files
  core.iterate._read_fix_attempts           re-exported in __init__; run_iterate
                                            calls it by name there → mock works;
                                            _count_fix_attempts calls directly
                                            (acceptable — only test path is the
                                            run_iterate return-value check)
  core.iterate._execute_planner_decompose   re-exported in __init__; dispatcher
                                            lazy-imports from core.iterate → mock
                                            works; run_iterate uses _ACTION_EXECUTORS
                                            dict (holds function object — mock does
                                            not affect dict, but no test relies on
                                            this path being mocked for run_iterate)
  core.iterate._qa_passed_for_issue         re-exported in __init__; run_iterate
  core.iterate._reviewer_passed_for_issue   calls them by name in __init__ namespace
  core.iterate._security_passed_for_issue   → mock.patch replaces name there ✓

Functions that are accessed through _pkg() inside _execute_planner_decompose_inner
because they reside in __init__.py (source-reading layer, extracted in PR 3/3):
  load_known_components, extract_epic_context, _build_aggregate_context,
  identify_relevant_files, read_source_files, _merge_same_file_tasks,
  _compute_sub_issue_dependencies, EpicContext, _source_reading_fallback_count
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from core.iterate.classify import MAX_FIX_ATTEMPTS
from core.util import extract_issue_number, extract_pr_number_from_summary

logger = logging.getLogger("daedalus.iterate")


def _pkg() -> Any:
    """Return the core.iterate package, resolved at call time.

    Used throughout executors.py to access ``kanban`` and other package-level
    names so that ``mock.patch("core.iterate.kanban")`` and similar test patches
    take effect even for code defined in this submodule.
    """
    return sys.modules["core.iterate"]


# ── decompose lock (issue #891) ──────────────────────────────────────��───────

_DECOMPOSE_LOCK_STALE_SECONDS = 60


def _lock_file_path(workdir: str) -> Path:
    """Return the decompose lock file path for the given workdir."""
    return Path(workdir) / ".hermes" / "decompose-lock.json"


def _acquire_decompose_lock(parent_n: int, workdir: str, *, dry_run: bool = False) -> bool:
    """Acquire the decompose lock. Returns True if acquired, False if lock is held.

    The lock is considered stale after 60 seconds and will be overwritten.
    """
    if not workdir:
        return True  # No lock when workdir is empty

    lock_path = _lock_file_path(workdir)

    try:
        if lock_path.exists():
            lock_data = json.loads(lock_path.read_text(encoding="utf-8"))
            acquired_at = lock_data.get("acquired_at", 0)
            age = time.time() - acquired_at

            if age < _DECOMPOSE_LOCK_STALE_SECONDS:
                # Lock is held and not stale - skip
                logger.info(
                    "iterate: decompose lock held by pid=%s for issue #%s (age=%0.1fs), skipping",
                    lock_data.get("pid"), lock_data.get("issue_n"), age,
                )
                return False
            else:
                logger.info("iterate: stale decompose lock (age=%0.1fs), overwriting", age)

        if dry_run:
            return True

        # Write lock file
        lock_data = {
            "pid": os.getpid(),
            "issue_n": parent_n,
            "acquired_at": int(time.time()),
        }
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(json.dumps(lock_data, indent=2), encoding="utf-8")
        return True

    except Exception as exc:
        logger.warning("iterate: failed to acquire decompose lock: %s", exc)
        return True  # Proceed on lock failure


def _release_decompose_lock(workdir: str) -> None:
    """Release the decompose lock by removing the lock file."""
    if not workdir:
        return

    lock_path = _lock_file_path(workdir)
    try:
        if lock_path.exists():
            lock_path.unlink()
    except Exception as exc:
        logger.warning("iterate: failed to release decompose lock: %s", exc)


def _fix_attempts_path(workdir: str) -> str:
    return str(Path(workdir) / ".hermes" / "daedalus-fix-attempts.json")


def _read_fix_attempts(workdir: str) -> dict[str, int]:
    """Read the per-card fix attempt counter file."""
    try:
        path = _fix_attempts_path(workdir)
        if Path(path).is_file():
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning("iterate: failed to read fix-attempts counter for %s: %s", workdir, exc)
    return {}


def _write_fix_attempts(workdir: str, data: dict[str, int]) -> None:
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
    kanban = _pkg().kanban
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
            #   fix-ci-{tid}-attempt-N   (dev fix for QA-reported test failures)
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


def _parse_pr_number(handoff_text: str) -> int | None:
    """Extract a PR number from handoff text."""
    return extract_pr_number_from_summary(handoff_text)


def _extract_issue_number_from_card(card: dict) -> int | None:
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
    kanban = _pkg().kanban
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


# Role → the ASSIGNEE-profile prefix for that role's card. Gate lookups match by
# assignee (+ the issue ref in the title) rather than a title role-word, because
# card TITLE formats are inconsistent ("#<n> QA:", "QA: verify PR for #<n>",
# "Review PR for issue #<n>:"), while the assignee is always the stable role
# profile. idempotency_key is no longer returned by the kanban API, so assignee is
# the reliable key; matching assignee also avoids the developer card being picked up
# by the security gate when the issue title contains "fix(security):".
_ROLE_ASSIGNEE_PREFIX: dict[str, str] = {
    "qa": "qa-",
    "reviewer": "reviewer-",
    "security": "security-",
    "documentation": "documentation-",
    "docs": "documentation-",
}


def _role_cards_for_issue(
    slug: str,
    issue_number: int,
    role: str,
    *,
    active_tasks: list[dict[str, Any]] | None = None,
    archived_tasks: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return the kanban cards for a given (role, issue), matched by assignee.

    Matches the assignee profile prefix (stable) plus the issue reference in the
    title (digit-boundary so #114 != #1140). Title role-words are unreliable — the
    dispatcher emits several QA/reviewer title formats.

    Crucially, a completed gate card ARCHIVES (QA finishes first, so it archives
    first), and the default ``list_tasks`` excludes archived cards. Reading only
    active cards therefore lost the QA verdict once its card archived → the gate
    evaluated "not passed" → auto-merge stranded the PR (observed on #1141). So when
    the active list has no match, fall back to archived cards, where the verdict
    still lives.

    ``active_tasks``/``archived_tasks`` accept pre-fetched card lists so a caller
    scanning many issues in one tick can fetch each board list once and thread it
    through, instead of paying an active (+ archived) ``list_tasks`` subprocess per
    issue (#1135). Either defaulting to ``None`` preserves the self-fetch path used
    by existing call sites.
    """
    kanban = _pkg().kanban
    prefix = _ROLE_ASSIGNEE_PREFIX.get(role, role + "-")
    pat = re.compile(rf"#{issue_number}(?!\d)")

    def _match(tasks: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        return [
            t for t in (tasks or [])
            if (t.get("assignee") or "").strip().lower().startswith(prefix)
            and pat.search(t.get("title") or "")
        ]

    if active_tasks is not None:
        found = _match(active_tasks)
    else:
        try:
            found = _match(kanban.list_tasks(slug))
        except Exception as e:
            logger.error("iterate: failed to list tasks for board %s: %s", slug, e)
            found = []
    if found:
        return found
    # Fall back to archived cards — a done gate card may have already archived.
    if archived_tasks is not None:
        return _match(archived_tasks)
    try:
        return _match(kanban.list_tasks(slug, status="archived"))
    except Exception as e:
        logger.error("iterate: failed to list archived tasks for board %s: %s", slug, e)
        return []


def _role_gate_passed(
    slug: str,
    issue_number: int | None,
    role: str,
    approval_signals: list[str],
    *,
    active_tasks: list[dict[str, Any]] | None = None,
    archived_tasks: list[dict[str, Any]] | None = None,
) -> bool:
    """True if ANY card for (role, issue) has an approval signal in its summary.

    Scans all matching cards (there may be retries) and passes if any one's
    latest_summary contains an approval signal.

    ``active_tasks``/``archived_tasks`` are threaded to ``_role_cards_for_issue``
    so a per-issue gate check reuses a once-per-tick board snapshot (#1135).
    """
    kanban = _pkg().kanban
    if issue_number is None:
        return False
    cards = _role_cards_for_issue(
        slug, issue_number, role,
        active_tasks=active_tasks, archived_tasks=archived_tasks,
    )
    if not cards:
        logger.debug("iterate: no %s card found for issue #%s", role, issue_number)
        return False
    for card in cards:
        try:
            detail = kanban.show_card(slug, card["id"])
        except Exception as e:
            logger.error("iterate: failed to get %s card %s: %s", role, card.get("id"), e)
            continue
        summary = ((detail or {}).get("latest_summary") or "").lower().lstrip()
        # startswith prevents a mid-string signal match, e.g.
        # "changes-requested: approved workaround" no longer passes the gate (#1125 F1).
        if summary and any(summary.startswith(sig) for sig in approval_signals):
            return True
    return False


def _qa_passed_for_issue(
    slug: str,
    issue_number: int | None,
    *,
    active_tasks: list[dict[str, Any]] | None = None,
    archived_tasks: list[dict[str, Any]] | None = None,
) -> bool:
    """Check if QA has passed for an issue (QA card summary contains 'qa-passed')."""
    return _role_gate_passed(
        slug, issue_number, "qa", ["qa-passed"],
        active_tasks=active_tasks, archived_tasks=archived_tasks,
    )


def _reviewer_passed_for_issue(
    slug: str,
    issue_number: int | None,
    *,
    active_tasks: list[dict[str, Any]] | None = None,
    archived_tasks: list[dict[str, Any]] | None = None,
) -> bool:
    """Check if the reviewer has approved the PR for an issue."""
    return _role_gate_passed(slug, issue_number, "reviewer", [
        "approved", "review-approved", "lgtm", "sign-off", "signoff",
        "looks good", "no findings", ":+1:",
    ], active_tasks=active_tasks, archived_tasks=archived_tasks)


def _security_passed_for_issue(
    slug: str,
    issue_number: int | None,
    *,
    active_tasks: list[dict[str, Any]] | None = None,
    archived_tasks: list[dict[str, Any]] | None = None,
) -> bool:
    """Check if the security analyst has cleared the PR for an issue.

    The security agent's documented pass signal is ``security: cleared`` (its
    fail signal is ``security: flagged: <finding>``), so ``cleared`` must be an
    accepted approval token — without it the gate rejected every real clearance
    and auto-merge never fired. ``flagged`` does not contain ``cleared``, so this
    stays a clean pass/fail split.
    """
    return _role_gate_passed(slug, issue_number, "security", [
        "security: cleared", "security cleared", "cleared",
        "security-approved", "security approved",
        "security-passed", "security passed",
        "no findings", "approved",
    ], active_tasks=active_tasks, archived_tasks=archived_tasks)


# ── action executors ────────────────────────────────────────────────────────


def _execute_advance(
    slug: str,
    card: dict,
    repo: str,
    handoff_text: str,
    *,
    dry_run: bool = False,
    pr_number: int | None = None,
    metadata_transport: bool = False,
    upfront_dag: bool = False,
    **_kwargs: Any,
) -> bool:
    """Complete a developer card to advance the chain (CI no longer gates this).

    The developer card advances as soon as its PR is opened and confirmed
    real/open — CI status is no longer a gate for dispatch of QA/reviewer/
    security (per epic #1074). CI is enforced at merge-time by the auto-merge
    gate instead.

    Also unblocks any reviewer/security cards that were blocked with
    'awaiting-fix: {this_card_id}' so they re-engage after the fix lands.
    After completing the developer card, creates downstream QA, reviewer,
    security-analyst, accessibility, and documentation tasks if they don't already exist.
    """
    kanban = _pkg().kanban
    tid = (card.get("id") or "")
    if not tid:
        return False
    pr = pr_number or _parse_pr_number(handoff_text)
    if dry_run:
        logger.info("[dry-run] would advance %s (PR #%s)", tid, pr)
        return True
    # #1288: completion handoff — emit the developer outcome onto the closing
    # run as native metadata when metadata_transport is ON.  This is the
    # developer→QA completion; blocked handoffs (review-required, awaiting-pr)
    # cannot carry metadata and keep their free-text reason (see
    # scripts/daedalus-delegate.sh; full elimination awaits #1290).
    if metadata_transport:
        issue_n = _extract_issue_number_from_card(card)
        _metadata = {
            "daedalus_outcome": 1,
            "role": "developer",
            "verdict": "pr_opened",
            "refs": {"issue": issue_n, "pr": pr},
        }
        _completed = kanban.complete(slug, tid, metadata=_metadata)
    else:
        # Flag OFF: byte-identical to the pre-#1288 call — no metadata kwarg, so
        # existing kanban doubles / signatures are unaffected.
        _completed = kanban.complete(slug, tid)
    if not _completed:
        return False
    logger.info("iterate: advanced %s — PR #%s (CI gated at merge-time)", tid, pr)

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
        _pkg()._create_downstream_review_tasks(
            slug, issue_number, card,
            pr_number=pr, dry_run=dry_run, upfront_dag=upfront_dag,
        )
    else:
        logger.warning(
            "iterate: advanced %s but could not extract issue number — "
            "skipping downstream review task creation", tid,
        )
    return True


def _execute_reconcile_merged(
    slug: str,
    card: dict,
    repo: str,
    handoff_text: str,
    *,
    dry_run: bool = False,
    pr_number: int | None = None,
    **_kwargs: Any,
) -> bool:
    """Reconcile a developer card whose PR merged outside the pipeline (#957).

    When a human merges the review-required PR directly, the issue may already
    be Done on the board. Neither the board-sync nor the orphaned-close cleanup
    reaches the card then — the latter deliberately skips issues with active
    kanban tasks (the blocked dev card counts as active) to avoid clobbering an
    accidental close. The dev card would otherwise loop forever in PENDING_PR.

    The merged PR is the work landing, so there is nothing left to QA/review:
    complete this card *and* all sibling pipeline cards for the issue so the
    board reaches a terminal state. Reuses ``kanban.close_issue_tasks`` (the
    same helper the dispatcher uses for externally-closed issues).
    """
    kanban = _pkg().kanban
    tid = (card.get("id") or "")
    if not tid:
        return False
    pr = pr_number or _parse_pr_number(handoff_text)
    issue_number = _extract_issue_number_from_card(card)
    summary = f"skipped: PR #{pr} merged outside pipeline"
    if dry_run:
        logger.info(
            "[dry-run] would reconcile %s (PR #%s merged) and close issue #%s pipeline cards",
            tid, pr, issue_number)
        return True
    if issue_number is not None:
        closed = kanban.close_issue_tasks(slug, issue_number, summary=summary)
        logger.info(
            "iterate: reconciled merged PR #%s for issue #%s — closed %d card(s): %s",
            pr, issue_number, len(closed), closed)
        return True
    # No issue number on the card → at least complete this card so it doesn't
    # loop in PENDING_PR forever.
    if not kanban.complete(slug, tid, summary=summary):
        return False
    logger.info("iterate: reconciled merged PR #%s — completed %s (no issue number)", pr, tid)
    return True


# Downstream review-task role mapping (idempotency suffix → assignee).
_DOWNSTREAM_REVIEW_ROLES = [
    ("qa", "qa-daedalus"),
    ("reviewer", "reviewer-daedalus"),
    ("security", "security-analyst-daedalus"),
    ("accessibility", "accessibility-daedalus"),
    ("docs", "documentation-daedalus"),
]


def _downstream_parents(
    role_suffix: str,
    dev_id: str,
    role_ids: dict[str, str],
) -> list[str] | None:
    """Resolve the parent card(s) for a downstream review role.

    Enforces the QA gate by mirroring the primary dispatch path
    (``daedalus_dispatch.py``): only QA is parented to the developer card; every
    other role is gated behind QA so it cannot unblock until QA completes (#955).

        dev → qa → [reviewer, security, accessibility] → docs

    Falls back to ``None`` (no parent) only when no suitable gate id is
    resolvable, rather than silently parenting a review role to the dev card.
    """
    qa_id = role_ids.get("qa")
    if role_suffix == "qa":
        return [dev_id] if dev_id else None
    if role_suffix == "docs":
        docs_parents = [
            role_ids[r] for r in ("reviewer", "security", "accessibility") if role_ids.get(r)
        ]
        if docs_parents:
            return docs_parents
        return [qa_id] if qa_id else None
    # reviewer / security / accessibility are all gated behind QA.
    return [qa_id] if qa_id else None


def _create_downstream_review_tasks(
    slug: str,
    issue_number: int,
    card: dict,
    *,
    pr_number: int | None = None,
    dry_run: bool = False,
    upfront_dag: bool = False,
) -> list[str]:
    """Create qa/reviewer/security/accessibility/docs tasks after a developer card completes.

    Each task uses an idempotency key (``qa-{n}``, ``reviewer-{n}``, ``security-{n}``,
    ``accessibility-{n}``, ``docs-{n}``) so re-runs never duplicate.  If a task with that key already
    exists on the board (any status), creation is skipped for that role.

    Returns the list of newly-created task ids.

    ``upfront_dag`` (#1290, default False): when the ``pipeline.upfront_dag`` flag
    is ON the full stage graph is built at Ready-time by :func:`build_pipeline_dag`
    and Hermes auto-promotes each stage via ``--kind dependency`` blocks. In that
    world the per-tick post-developer creation would double-own the downstream
    cards, so it no-ops here. When the flag is OFF (the default) this runs exactly
    as before — byte-identical.
    """
    if upfront_dag:
        logger.debug(
            "iterate: pipeline.upfront_dag ON — per-tick downstream creation for "
            "issue #%s no-ops (upfront DAG owns the stages)", issue_number,
        )
        return []
    kanban = _pkg().kanban
    created: list[str] = []
    tid = card.get("id") or ""
    workspace = card.get("workspace") or ""

    # Build a concise body referencing the issue and PR.
    pr_ref = f"PR #{pr_number}" if pr_number else "(PR number unknown)"
    base_body = (
        f"The developer has completed work for issue #{issue_number} "
        f"({pr_ref}). The PR is open. CI may still be running — reviews "
        f"proceed in parallel with CI.\n\n"
        f"Developer card: {tid}\n"
        f"Workspace: {workspace}\n"
    )

    # Idempotency: check which keys already exist on the board, and remember the
    # id of each existing card so an already-created QA gate can still parent the
    # downstream roles (#955).
    existing_keys: set[str] = set()
    key_to_id: dict[str, str] = {}
    for task in kanban.list_tasks(slug):
        ikey = task.get("idempotency_key") or ""
        if ikey:
            existing_keys.add(ikey)
            tid_existing = task.get("id")
            if tid_existing:
                key_to_id[ikey] = tid_existing

    # Map role_suffix → created/recovered card id so the parent chain can be
    # resolved per-role (dev → qa → [reviewer, security, accessibility] → docs),
    # mirroring the primary dispatch path. Pre-seed from already-existing cards.
    role_ids: dict[str, str] = {}
    for role_suffix, _assignee in _DOWNSTREAM_REVIEW_ROLES:
        recovered = key_to_id.get(f"{role_suffix}-{issue_number}")
        if recovered:
            role_ids[role_suffix] = recovered

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

        parents = _downstream_parents(role_suffix, tid, role_ids)
        new_tid = kanban.create_task(
            slug,
            title,
            body=body,
            assignee=assignee,
            workspace=workspace,
            idempotency_key=ikey,
            parents=parents,
        )
        if new_tid:
            role_ids[role_suffix] = new_tid
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


# ── Upfront pipeline DAG (#1290, KEYSTONE of #1276) ──────────────────────────
#
# The full stage graph, built in ONE shot at Ready-time when the
# ``pipeline.upfront_dag`` flag is ON. Each non-root stage is created with
# ``--parent`` edges to its predecessor(s) and then blocked with
# ``--kind dependency`` so Hermes auto-promotes it the moment ALL of its parents
# complete. Ordering mirrors the invariant pipeline:
#
#     validator → pm → developer → qa → [reviewer, security, accessibility] → docs
#
# ``docs`` is multi-parented to reviewer + security + accessibility so it only
# unblocks once every review branch has finished. The list is topologically
# ordered so a parent id is always resolvable before its children are created.
_PIPELINE_DAG_STAGES: list[tuple[str, tuple[str, ...]]] = [
    ("validator", ()),
    ("pm", ("validator",)),
    ("developer", ("pm",)),
    ("qa", ("developer",)),
    ("reviewer", ("qa",)),
    ("security", ("qa",)),
    ("accessibility", ("qa",)),
    ("docs", ("reviewer", "security", "accessibility")),
]


def build_pipeline_dag(
    slug: str,
    issue_number: int,
    role_specs: dict[str, dict],
    *,
    dry_run: bool = False,
) -> dict[str, str]:
    """Create the full pipeline stage graph as blocked, parent-linked cards.

    Called at Ready-time ONLY when ``pipeline.upfront_dag`` is ON (#1290). Builds
    every stage in :data:`_PIPELINE_DAG_STAGES` topological order:

      * The root (``validator``) is created *unblocked* so it dispatches
        immediately.
      * Every other stage is created with ``--parent`` edges to its predecessors
        and then blocked with ``kind="dependency"`` so Hermes auto-promotes it
        when ALL of its parents complete. ``docs`` is multi-parented to
        reviewer + security + accessibility.

    ``role_specs`` maps each role key to its creation kwargs::

        {"validator": {"assignee": "validator-daedalus", "body": "...",
                       "workspace": "dir:/w", "skills": [...], "extra": {...}},
         "pm": {...}, ...}

    ``extra`` (optional) is merged into :func:`kanban.create_task` kwargs (used
    for native bounds like ``max_runtime``). A role absent from ``role_specs`` is
    skipped (its edges collapse to whatever parents *do* exist).

    Idempotency: each card uses a stable ``<role>-{issue_number}`` key. Existing
    cards (any status) are recovered — never re-created — so a re-tick never
    double-creates. Returns ``{role_key: task_id}`` for every stage that now
    exists (created or recovered). Never raises — degrades per-role on failure.
    """
    kanban = _pkg().kanban
    role_ids: dict[str, str] = {}

    # Idempotency: recover the id of any stage card that already exists so a
    # re-tick both skips creation AND can still resolve parent edges.
    #
    # NOTE: Human-gate resume path — known limitation shared with CANCEL/ESCALATE.
    # When a human re-marks an issue Ready after a needs_more_info/block_for_review
    # verdict, build_pipeline_dag recovers existing cards by idempotency key and will
    # NOT recreate cards that are already "done" (including the arbiter-deferred
    # downstream stages from the previous run). This means those deferred cards never
    # get reset, and the pipeline will be missing those stages.
    # Operator workaround: archive the deferred cards before re-marking Ready,
    # which allows build_pipeline_dag to recreate them fresh.
    # A proper reset mechanism (auto-archive on re-Ready) is a follow-up item
    # shared with the CANCEL/ESCALATE branch cleanup.
    existing_keys: dict[str, str] = {}
    for task in kanban.list_tasks(slug):
        ikey = (task.get("idempotency_key") or "")
        tid_existing = task.get("id")
        if ikey and tid_existing:
            existing_keys[ikey] = tid_existing
    for role_key, _parents in _PIPELINE_DAG_STAGES:
        recovered = existing_keys.get(f"{role_key}-{issue_number}")
        if recovered:
            role_ids[role_key] = recovered

    for role_key, parent_keys in _PIPELINE_DAG_STAGES:
        spec = role_specs.get(role_key)
        if spec is None:
            continue
        ikey = f"{role_key}-{issue_number}"
        if role_key in role_ids:
            logger.info(
                "iterate: upfront-DAG stage '%s' already exists for issue #%s — skip",
                ikey, issue_number,
            )
            continue

        parents = [role_ids[p] for p in parent_keys if role_ids.get(p)]

        if dry_run:
            logger.info(
                "[dry-run] would create upfront-DAG stage %s for issue #%s "
                "(parents=%s, dependency-block=%s)",
                ikey, issue_number, parents, bool(parent_keys),
            )
            continue

        extra = dict(spec.get("extra") or {})
        new_tid = kanban.create_task(
            slug,
            spec.get("title") or f"#{issue_number} {role_key}",
            body=spec.get("body", ""),
            assignee=spec.get("assignee", ""),
            workspace=spec.get("workspace", ""),
            idempotency_key=ikey,
            parents=parents or None,
            skills=spec.get("skills") or None,
            **extra,
        )
        if not new_tid:
            logger.warning(
                "iterate: upfront-DAG failed to create stage '%s' for issue #%s",
                role_key, issue_number,
            )
            continue
        role_ids[role_key] = new_tid
        # Non-root stages sit dependency-blocked until every parent completes.
        if parent_keys:
            kanban.block_task(
                slug, new_tid,
                f"dependency: awaiting {', '.join(parent_keys)} (upfront DAG)",
                kind="dependency",
            )
        logger.info(
            "iterate: upfront-DAG created stage %s (%s) for issue #%s (parents=%s)",
            new_tid, role_key, issue_number, parents,
        )

    return role_ids


def _check_and_maybe_escalate(
    slug: str,
    card: dict,
    repo: str,
    handoff_text: str,
    *,
    workdir: str = "",
    dry_run: bool = False,
    max_fix_attempts: int = MAX_FIX_ATTEMPTS,
) -> bool | int:
    """Shared fix-attempt escalation guard for the fix-card executors.

    Returns the incremented ``fix_attempts`` count (an ``int``) when the caller
    should proceed with creating a fix card, or the ``bool`` result of
    ``_execute_escalate()`` when the cap is exceeded. Callers must check
    ``isinstance(res, bool)`` and return early on the escalate path — a plain
    truthiness check would misread attempt counts as escalation results.
    """
    # Read-then-increment is not atomic, but the dispatcher is a single-process
    # cron (projects processed sequentially) — no lock needed unless that changes.
    fix_attempts = _count_fix_attempts(card, slug=slug, workdir=workdir) + 1
    if fix_attempts > max_fix_attempts:
        return _execute_escalate(slug, card, repo, handoff_text, workdir=workdir,
                                 dry_run=dry_run, max_fix_attempts=max_fix_attempts)
    return fix_attempts


def _execute_qa_fix(
    slug: str,
    card: dict,
    repo: str,
    handoff_text: str,
    *,
    workdir: str = "",
    dry_run: bool = False,
    pr_number: int | None = None,
    max_fix_attempts: int = MAX_FIX_ATTEMPTS,
    **_kwargs: Any,
) -> bool:
    """Create a developer fix card for QA-reported test failures, idempotent per (card, attempt).

    Returns True when a fix card was successfully created (or would be in dry_run),
    False when no PR number could be found or kanban.create_task returned falsy.
    Callers that gate side-effects on True (e.g. qa_failed_cards) will only fire
    on genuine card creation, not on executor errors.
    """
    kanban = _pkg().kanban
    tid = card.get("id")
    pr = pr_number or _parse_pr_number(handoff_text)
    if not pr:
        logger.warning("iterate: qa_fix on %s but no PR found in handoff", tid)
        return False
    res = _check_and_maybe_escalate(slug, card, repo, handoff_text,
                                    workdir=workdir, dry_run=dry_run,
                                    max_fix_attempts=max_fix_attempts)
    if isinstance(res, bool):
        return res
    fix_attempts = res

    title = f"Task 2.3 FIX — QA failure on PR #{pr} — fix and push"
    body = (
        f"QA reported failing tests on PR #{pr} (repo {repo}). "
        f"Fix the failing tests/build and push. Fix attempt {fix_attempts}/{max_fix_attempts}."
    )

    idem_key = f"fix-ci-{tid}-attempt-{fix_attempts}"
    ws = f"dir:{workdir}" if workdir else card.get("workspace", "")

    if dry_run:
        logger.info("[dry-run] would create CI fix card for %s (attempt %s/%s, PR #%s)",
                     tid, fix_attempts, max_fix_attempts, pr)
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
                       f"Created CI fix task {fix_tid} (attempt {fix_attempts}/{max_fix_attempts})")
        # Persist the incremented fix attempt count so escalation works across ticks.
        _increment_fix_attempts(card, workdir)
        logger.info("iterate: created CI fix card %s for %s (attempt %s/%s)",
                     fix_tid, tid, fix_attempts, max_fix_attempts)
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
    native_gates: bool = False,
    **_kwargs: Any,
) -> bool:
    """Search VCS for a PR linked to this card's issue number; update block reason when found.

    Called when a developer card is blocked with 'review-required: awaiting-pr'.
    Searches open PRs for one that references the issue number in its title,
    body, or branch name. If found, updates the block reason so the next cron
    tick can advance the pipeline normally. If not found, does nothing (stays
    blocked until Claude Code opens the PR).
    """
    kanban = _pkg().kanban
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
    issue_linked_to_pr = _pkg().issue_linked_to_pr
    for pr in open_prs:
        if issue_linked_to_pr(pr, issue_n):
            found_pr = pr.number
            break

    if found_pr is None:
        logger.debug("iterate: pending_pr %s — no PR found yet for issue #%s", tid, issue_n)
        return False

    new_handoff = f"review-required: PR #{found_pr}"
    # Phase-3 (#1291): tag the review-required developer gate with the native
    # `needs_input` kind when pipeline.native_gates is on.  The PR is open and
    # awaiting human review/merge (QA/reviewer/security run in the meantime), so
    # this is the in-pipeline human gate.  The kind is advisory metadata only —
    # classify_blocked still routes off the "review-required:" reason string
    # (→ ADVANCE), so the card never strands and flag-off omits --kind entirely
    # (plain block, byte-identical).
    gate_kind = "needs_input" if native_gates else None
    if dry_run:
        logger.info("[dry-run] pending_pr %s — would update block reason to '%s'%s",
                    tid, new_handoff,
                    f" (kind={gate_kind})" if gate_kind else "")
        return True

    # hermes kanban block refuses to re-block an already-blocked card ("cannot block").
    # Unblock first so the new reason takes effect.
    kanban.unblock_task(slug, tid, "pending-pr: PR found, updating block reason")
    kanban.block_task(slug, tid, new_handoff, kind=gate_kind)
    logger.info("iterate: pending_pr %s — PR #%s found for issue #%s, updated block reason%s",
                tid, found_pr, issue_n,
                f" (kind={gate_kind})" if gate_kind else "")
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
    pr_number: int | None = None,
    max_fix_attempts: int = MAX_FIX_ATTEMPTS,
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
    kanban = _pkg().kanban
    tid = card.get("id")
    # Guard: awaiting-pr means Claude Code was spawned but hasn't opened a PR yet.
    # The PENDING_PR executor handles this — PM routing cannot unblock it (issue #87).
    if "awaiting-pr" in (handoff_text or "").lower():
        logger.info("iterate: %s blocked awaiting-pr — skipping PM route", tid)
        return False
    pr = pr_number or _parse_pr_number(handoff_text)
    res = _check_and_maybe_escalate(slug, card, repo, handoff_text,
                                    workdir=workdir, dry_run=dry_run,
                                    max_fix_attempts=max_fix_attempts)
    if isinstance(res, bool):
        return res
    fix_attempts = res

    rp = (router_profile or "").strip()
    ws = f"dir:{workdir}" if workdir else card.get("workspace", "")

    if not rp:
        # Fallback: empty router_profile → direct developer routing
        return _execute_legacy_dev_fix_review(
            slug, card, repo, handoff_text,
            workdir=workdir, dry_run=dry_run,
            max_fix_attempts=max_fix_attempts,
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
                     tid, rp, fix_attempts, max_fix_attempts, pr)
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
                       f"Created PM routing card {pm_tid} (attempt {fix_attempts}/{max_fix_attempts})")
        # Mark the reviewer card as blocked (awaiting-fix) so pending state is visible
        kanban.block_task(slug, tid, f"awaiting-fix: {pm_tid}")
        _increment_fix_attempts(card, workdir)
        logger.info("iterate: created PM routing card %s for %s via %s (attempt %s/%s)",
                     pm_tid, tid, rp, fix_attempts, max_fix_attempts)
        return True

    # CLI create failed — profile likely absent; fall back to direct developer
    logger.warning("iterate: PM routing card creation failed (profile '%s' absent?), "
                   "falling back to direct developer routing", rp)
    return _execute_legacy_dev_fix_review(
        slug, card, repo, handoff_text,
        workdir=workdir, dry_run=dry_run,
        max_fix_attempts=max_fix_attempts,
    )


def _execute_legacy_dev_fix_review(
    slug: str,
    card: dict,
    repo: str,
    handoff_text: str,
    *,
    workdir: str = "",
    dry_run: bool = False,
    pr_number: int | None = None,
    max_fix_attempts: int = MAX_FIX_ATTEMPTS,
) -> bool:
    """Fallback: create a developer fix card directly (old behavior).

    Used when router_profile is empty or the PM profile is absent.
    """
    kanban = _pkg().kanban
    tid = card.get("id")
    pr = pr_number or _parse_pr_number(handoff_text)
    res = _check_and_maybe_escalate(slug, card, repo, handoff_text,
                                    workdir=workdir, dry_run=dry_run,
                                    max_fix_attempts=max_fix_attempts)
    if isinstance(res, bool):
        return res
    fix_attempts = res

    title = f"Task 2.3 FIX — address review findings for PR #{pr or '?'} — push changes"
    body = (
        f"Review findings for PR #{pr or '?'} (repo {repo}):\n\n"
        f"{handoff_text}\n\n"
        f"Address all findings and push. Fix attempt {fix_attempts}/{max_fix_attempts}."
    )

    idem_key = f"fix-review-{tid}-attempt-{fix_attempts}"
    ws = f"dir:{workdir}" if workdir else card.get("workspace", "")

    if dry_run:
        logger.info("[dry-run] would create legacy review-fix card for %s (attempt %s/%s, PR #%s)",
                     tid, fix_attempts, max_fix_attempts, pr)
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
                       f"Created review-fix task {fix_tid} (fallback, attempt {fix_attempts}/{max_fix_attempts})")
        kanban.block_task(slug, tid, f"awaiting-fix: {fix_tid}")
        logger.info("iterate: created legacy review-fix card %s for %s (attempt %s/%s)",
                     fix_tid, tid, fix_attempts, max_fix_attempts)
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
    kanban = _pkg().kanban
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
    pr_number: int | None = None,
    max_fix_attempts: int = MAX_FIX_ATTEMPTS,
    **_kwargs: Any,
) -> bool:
    """Escalate a card that has exceeded max fix attempts.

    After posting the escalation comment, stamps the card with an
    ``escalated: issue #N`` comment so future dispatcher ticks skip it.
    Returns True on success.
    """
    kanban = _pkg().kanban
    tid = card.get("id")
    if not tid:
        return False
    pr = pr_number or _parse_pr_number(handoff_text)
    msg = (
        f"⚠️ ESCALATE: card {tid} (PR #{pr or '?'}) has exceeded "
        f"{max_fix_attempts} fix attempts. Manual intervention required."
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

def _build_decomposed_marker() -> str:
    """Build the new idempotency marker with current UTC timestamp.

    Returns a string like: <!-- daedalus:decomposed:1720000000 -->
    """
    timestamp = int(time.time())
    return f"<!-- daedalus:decomposed:{timestamp} -->"

# Idempotency marker regex: matches any variation like
#   <!-- daedalus:decomposed:123456789 -->
#   <!--daedalus:decomposed:...-->
#   <!--  daedalus:decomposed:...  -->
# The marker is posted as an HTML comment on the parent issue body or a comment.
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


# Marker injected into parent issue body/comment to prevent re-decomposition.
_DECOMPOSED_MARKER_RE = re.compile(r'<!--\s*daedalus:decomposed(?::\d+)?\s*-->', re.IGNORECASE)
# Legacy format marker pattern (sub-issues list)
_LEGACY_DECOMPOSED_MARKER_RE = re.compile(r'<!--\s*daedalus:sub-issues:\[.*?\]\s*-->', re.IGNORECASE)


def has_decomposed_marker(text: str | None) -> bool:
    """Return True if *text* contains any decomposed marker (old or new format).

    Supports both:
    - New format: ``<!-- daedalus:decomposed[:timestamp] -->``
    - Legacy format: ``<!-- daedalus:sub-issues:[...] -->``

    The marker is an idempotency signal: once present on a parent epic (body or
    a posted comment), re-running the dispatcher must skip decomposition entirely
    and create zero sub-issues. Detection is tolerant of whitespace variations
    and optional Unix-timestamp suffix on the new format.

    Markers inside fenced code blocks (``` or ~~~) are ignored to prevent false
    positives from documentation examples.
    """
    if not text:
        return False
    # Strip code blocks to avoid false positives from documentation examples
    stripped_text = _strip_code_blocks(text)
    # Check for either the new format or legacy format
    return bool(
        _DECOMPOSED_MARKER_RE.search(stripped_text)
        or _LEGACY_DECOMPOSED_MARKER_RE.search(stripped_text)
    )


def _extract_sub_issues_from_body(body: str) -> list[str]:
    """Return checklist item texts from an epic body (capped at _MAX_SUB_ISSUES)."""
    items = [m.group(1).strip() for m in _CHECKLIST_RE.finditer(body or "")]
    return [i for i in items if i][:_MAX_SUB_ISSUES]


def _default_sub_issue_titles(parent_n: int, parent_title: str) -> list[str]:
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
            parts.append(f"- … and {overflow} additional file(s)\n")
        parts.append("\n")
    if syms:
        shown = syms[:_FILE_SYMBOL_CAP]
        overflow = len(syms) - len(shown)
        parts.append("**Symbols:**\n")
        parts.extend(f"- `{s}`\n" for s in shown)
        if overflow:
            parts.append(f"- … and {overflow} additional symbol(s)\n")
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
    kanban = _pkg().kanban
    tid: str = card.get("id") or ""  # kanban task id; always a str at runtime
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

    # Concurrency lock (issue #891): prevent concurrent dispatcher ticks from
    # racing to decompose the same epic.  Acquire a process-wide coarser lock
    # AFTER the marker check (so idempotent hits short-circuit before any lock I/O).
    # If workdir is empty we still proceed — lock is best-effort.
    if not _acquire_decompose_lock(parent_n, workdir, dry_run=dry_run):
        logger.warning(
            "iterate: planner_decompose #%s — lock held by another process, "
            "yielding (will retry next tick)", parent_n,
        )
        return True  # idempotent: complete without sub-issue creation

    try:
        return _execute_planner_decompose_inner(
            slug, tid, parent_n, parent_title, parent_body, parent_labels,
            workdir, dry_run, provider,
        )
    finally:
        _release_decompose_lock(workdir)


def _execute_planner_decompose_inner(
    slug: str,
    tid: str,
    parent_n: int,
    parent_title: str,
    parent_body: str,
    parent_labels: list,
    workdir: str,
    dry_run: bool,
    provider: Any,
) -> bool:
    """Inner implementation of planner decomposition, called while lock is held.

    Functions from the source-reading layer (PR 3/3, still in __init__.py) are
    accessed through ``_pkg()`` so that ``mock.patch("core.iterate.X")`` patches
    applied in tests take effect here as well.
    """
    _p = _pkg()
    kanban = _p.kanban

    checklist_items = _extract_sub_issues_from_body(parent_body)
    if checklist_items:
        sub_titles = checklist_items
        sub_scopes = checklist_items
    else:
        sub_titles = _default_sub_issue_titles(parent_n, parent_title)
        sub_scopes = [t.split(" — ", 1)[0] for t in sub_titles]

    # Phase 4: source-file analysis for decomposition planning.
    # Source files are analyzed to derive per-sub-issue context (file paths and
    # symbols), but their *contents* are deliberately NOT injected into sub-issue
    # bodies: doing so blew past GitHub's 65,536-char body limit and produced a
    # 422 "body is too long", silently stranding the epic (issue #899).  The
    # concise affected-files/symbols metadata still flows into the body via
    # `per_sub_contexts` below.
    # If reading fails or workdir is unavailable, fall back to Phase 3
    # behavior (template-only generation without analysis).
    #
    # _source_reading_fallback_count lives in __init__.py (the package) and is
    # accessed via _p to mutate the canonical counter there.
    full_issue_text = f"{parent_title}\n\n{parent_body}"
    per_sub_contexts: list = []
    if workdir and Path(workdir).exists():
        try:
            # Build per-sub-issue epic context from checklist items
            known_components = _p.load_known_components(workdir)
            per_sub_contexts = [_p.extract_epic_context(item, known_components) for item in sub_scopes]
            epic_agg = _p._build_aggregate_context(sub_scopes, known_components)

            rel_files, _file_metadata = _p.identify_relevant_files(full_issue_text, workdir, epic_context=epic_agg)
            if rel_files:
                analyzed = _p.read_source_files(rel_files, workdir)
                logger.info(
                    "iterate: planner_decompose #%s — analyzed %d source files "
                    "(contents NOT injected into sub-issue bodies, see #899)",
                    parent_n, len(analyzed),
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "iterate: planner_decompose #%s — source-reading failed (degrading gracefully): %s",
                parent_n, exc,
            )
            _p._source_reading_fallback_count += 1
    else:
        logger.info(
            "iterate: planner_decompose #%s — workdir unavailable (%s), skipping codebase reading (Phase 3 fallback)",
            parent_n, workdir or "<empty>",
        )
        _p._source_reading_fallback_count += 1

    # Merge tasks that touch exactly the same set of files into one task
    # (issue #1060: consolidate all-same-file tasks into single task).
    sub_titles, sub_scopes, per_sub_contexts = _p._merge_same_file_tasks(
        sub_titles, sub_scopes, per_sub_contexts,
    )
    if dry_run:
        logger.info("[dry-run] planner_decompose #%s: would create %d sub-issues: %s",
                    parent_n, len(sub_titles), sub_titles)
        return True

    inherit_labels = [lbl for lbl in parent_labels if lbl and lbl.lower() != "epic"]
    created_numbers: list[int] = []
    ready_numbers: list[int] = []
    for idx, (title, scope) in enumerate(zip(sub_titles, sub_scopes)):
        sub_ctx = per_sub_contexts[idx] if idx < len(per_sub_contexts) else _p.EpicContext()
        # Compute dependencies based on file overlap with previous sub-issues
        dependencies = _p._compute_sub_issue_dependencies(
            per_sub_contexts,
            index=idx,
            created_numbers=created_numbers,
        )
        # The body carries only the concise checklist-derived scope plus the
        # affected files/symbols metadata and dependencies — NOT raw source contents (issue #899).
        sub_body = _sub_issue_body(
            parent_n,
            parent_title,
            scope,
            dependencies,
            file_paths=sub_ctx.file_paths,
            identifiers=sub_ctx.identifiers,
        )
        sub_labels = inherit_labels + ["subtask"]
        sub_n = provider.create_issue(title, sub_body, labels=sub_labels)
        if sub_n is not None:
            created_numbers.append(sub_n)
            logger.info("iterate: planner_decompose — created sub-issue #%s: %s", sub_n, title)

            if not dependencies:
                # No dependencies = immediately actionable, apply Ready label + enroll on board
                provider.add_label(sub_n, "Ready")
                ready_numbers.append(sub_n)
                if getattr(provider, "board_configured", lambda: False)():
                    try:
                        provider.board_set_status(sub_n, "Ready")
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("iterate: planner_decompose — board_set_status(#%s, Ready) failed: %s", sub_n, exc)
                logger.info("iterate: planner_decompose — applied Ready label to sub-issue #%s (no dependencies)", sub_n)
            else:
                # Has dependencies = add to board in Backlog status (not yet actionable)
                if getattr(provider, "board_configured", lambda: False)():
                    try:
                        provider.board_ensure_backlog(sub_n)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("iterate: planner_decompose — board_ensure_backlog(#%s) failed: %s", sub_n, exc)
                logger.info("iterate: planner_decompose — sub-issue #%s has %d dependencies, skipping Ready label", sub_n, len(dependencies))
        else:
            logger.warning("iterate: planner_decompose — create_issue failed for %r", title)

    # Post idempotency marker on parent
    # Use the new timestamped marker format (<!-- daedalus:decomposed:<ts> -->)
    # so subsequent runs detect it via has_decomposed_marker().
    # Also include the sub-issue list for traceability.
    provider.post_issue_comment(
        parent_n,
        f"{_build_decomposed_marker()}\n"
        f"Daedalus decomposed epic #{parent_n} into {len(created_numbers)} sub-issue(s): "
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

# Import classify action constants for the executor dispatch table.
from core.iterate.classify import (  # noqa: E402
    ADVANCE,
    APPROVE_ADVANCE,
    ESCALATE,
    PENDING_PR,
    PLANNER_DECOMPOSE,
    PM_ROUTE,
    QA_FIX,
    RECONCILE_MERGED,
)

# _ACTION_EXECUTORS CONTRACT
# --------------------------
# Values are import-time function references captured when this module loads.
# This means:
#   • The table is PATCH-IMMUNE — mock.patch("core.iterate._execute_X") replaces
#     the name in __init__'s namespace AFTER this dict was built, so the dict
#     still holds the original function object.
#   • run_iterate calls executor = _ACTION_EXECUTORS.get(action), then
#     executor(slug, card, ...) — it calls the real function directly, not via
#     a name lookup that would see a patch.
#   • Tests that need to intercept an executor must patch the kanban methods it
#     calls (mock.patch.object(kanban, "complete", ...)) or use the direct-call
#     paths (call the executor function directly in a unit test).
#   • No test currently patches _execute_X and expects run_iterate to call the
#     mock — there is no need to route through _pkg() here.
_ACTION_EXECUTORS: dict[str, Any] = {
    ADVANCE: _execute_advance,
    QA_FIX: _execute_qa_fix,
    PENDING_PR: _execute_pending_pr,
    PM_ROUTE: _execute_pm_route,
    APPROVE_ADVANCE: _execute_approve_advance,
    ESCALATE: _execute_escalate,
    PLANNER_DECOMPOSE: _execute_planner_decompose,
    RECONCILE_MERGED: _execute_reconcile_merged,
}
