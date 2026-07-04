"""classify layer — action constants, handoff parsing, and routing decisions.

Pure classification logic extracted from ``core.iterate`` as PR 1/3 of the
``core/iterate.py`` → ``core/iterate/`` package split (issue #1154).

Nothing here touches kanban or the file system.  ``classify_blocked`` and
``_parse_handoff`` are the only callers and both are stateless functions.

All symbols are re-exported via ``core/iterate/__init__.py`` so existing
imports and ``mock.patch`` targets remain unchanged.

Phase 1 of #1170 adds structured-outcome routing on top of the existing prefix
logic.  See ``core/iterate/outcomes.py`` for the schema and parser.

(role, verdict) → action mapping table
---------------------------------------
Each row maps a JSON outcome record's (role, verdict) pair to the dispatcher
action the *equivalent prefix* would produce.  Column "equivalent prefix" shows
the legacy handoff text that the prefix path would match.  Guards
(fix_attempts, pr_is_open, pr_is_merged, awaiting-fix:) are applied ON TOP
by ``_classify_by_outcome`` exactly as they are in the prefix branches.

| role      | verdict             | equivalent prefix                    | action          |
|-----------|---------------------|--------------------------------------|-----------------|
| planner   | plan                | "PLANNING COMPLETE"                  | PLANNER_DECOMPOSE |
| planner   | not_suitable        | other planner output                 | PM_ROUTE        |
| docs      | posted              | "docs posted"                        | APPROVE_ADVANCE |
| pm        | escalated           | PM blocked (not awaiting-fix:)       | ESCALATE        |
| pm        | spec/assigned/      | (work-in-progress — analogous to     | "" (no-op)      |
|           | clarified           |  "awaiting-fix:" for reviewer)       |                 |
| developer | pr_opened           | "review-required: PR #N"             | ADVANCE*        |
| developer | blocked             | other dev block reason               | PM_ROUTE        |
| reviewer  | approved            | approval signals                     | APPROVE_ADVANCE*|
| reviewer  | changes_requested   | "review-changes-requested:"          | PM_ROUTE*       |
| security  | approved            | approval signals (incl. "cleared")   | APPROVE_ADVANCE*|
| security  | changes_requested   | "security-changes-requested:"        | PM_ROUTE*       |
| qa        | passed              | "qa-passed"                          | ADVANCE         |
| qa        | failed              | "qa-failed"                          | QA_FIX          |
| a11y      | approved            | "approved", "a11y-approved"          | ADVANCE         |
| a11y      | na                  | "accessibility-na"                   | ADVANCE         |
| a11y      | skipped             | "a11y-skipped"                       | ADVANCE         |
| a11y      | changes_requested   | "changes requested"                  | PM_ROUTE        |
| validator | * (any)             | blocked validator                    | ESCALATE        |

*: additional guards apply (fix_attempts >= max → ESCALATE;
   "awaiting-fix:" in text → ""; pr_is_open/pr_is_merged for developer).

Phase-1 invariant: ``_guard_prefix_on_done`` in ``core/dispatch/checks.py``
still requires the prefix line to be present — a valid JSON block alone does
NOT satisfy the guard.  Dual-write (prefix + JSON) is required throughout
Phase 1.
"""

from __future__ import annotations

import logging
from typing import Any

from core.iterate.outcomes import OutcomeRecord
from core.iterate.outcomes import parse as _parse_outcome
from core.util import extract_pr_number_from_summary

logger = logging.getLogger("daedalus.iterate.classify")

# ── action constants ──────────────────────────────────────────────────────────
# Actions that classify_blocked can return.

ADVANCE = "advance"            # dev card with open PR → complete, advance chain (CI gated at merge-time)
QA_FIX = "qa_fix"             # QA card with failing tests → create fix card
PENDING_SIGNAL = "pending_signal"  # QA/accessibility card with unrecognized signal → wait
PENDING_PR = "pending_pr"     # dev card with awaiting-pr block → search VCS for PR, update when found
PM_ROUTE = "pm_route"         # reviewer flagged changes → create PM routing card
APPROVE_ADVANCE = "approve_advance"  # reviewer approved → complete card
ESCALATE = "escalate"         # max iterations exceeded → log + notify
PLANNER_DECOMPOSE = "planner_decompose"  # planner completed → create sub-issues
RECONCILE_MERGED = "reconcile_merged"  # dev card whose PR merged outside the pipeline → close issue cards

# Maximum fix attempts per PR before escalation
MAX_FIX_ATTEMPTS = 3


# ── pure helpers ──────────────────────────────────────────────────────────────


# ── structured-outcome routing (Phase 1 of #1170) ────────────────────────────

# Maps kanban profile names → JSON role names used in OutcomeRecord.
_ASSIGNEE_TO_ROLE: dict[str, str] = {
    "validator-daedalus":        "validator",
    "qa-daedalus":               "qa",
    "reviewer-daedalus":         "reviewer",
    "security-analyst-daedalus": "security",
    "accessibility-daedalus":    "a11y",
    "documentation-daedalus":    "docs",
    "planner-daedalus":          "planner",
    "project-manager-daedalus":  "pm",
    "developer-daedalus":        "developer",
}


def _classify_by_outcome(
    outcome: OutcomeRecord,
    handoff_text: str,
    fix_attempts: int,
    max_fix_attempts: int,
    effective_pr: int | None,
    pr_is_open: bool | None,
    pr_is_merged: bool | None,
) -> str | None:
    """Route by (role, verdict) → action using the mapping table above.

    Applies the same guards (fix_attempts, pr_is_merged, pr_is_open,
    awaiting-fix:) as the prefix branches so behaviour is identical for
    equivalent inputs.

    Returns the action string (possibly ``""`` for no-op) or ``None`` when
    the role is not in the mapping table (caller should fall back to prefix).
    """
    role = outcome.role
    verdict = outcome.verdict
    # Prefer the PR reference from the outcome record; fall back to the
    # caller-supplied value derived from the handoff text.
    pr = outcome.pr_ref if outcome.pr_ref is not None else effective_pr

    if role == "planner":
        return PLANNER_DECOMPOSE if verdict == "plan" else PM_ROUTE

    if role == "docs":
        # "posted" is the only valid docs verdict; always APPROVE_ADVANCE.
        return APPROVE_ADVANCE

    if role == "pm":
        # "awaiting-fix:" in handoff text is still the canonical no-op signal
        # for PM cards (the dispatcher writes it when blocking the card while
        # waiting for a developer fix to arrive).  Dual-write preserves it.
        if "awaiting-fix:" in handoff_text.lower():
            return ""
        return ESCALATE if verdict == "escalated" else ""

    if role == "validator":
        # A blocked validator card is always an error (validators only complete).
        return ESCALATE

    if role == "developer":
        if fix_attempts >= max_fix_attempts:
            return ESCALATE
        if verdict == "pr_opened" and pr:
            # #957: reconcile when the PR was merged outside the pipeline.
            if pr_is_merged is True:
                return RECONCILE_MERGED
            # #953: hold when the provider says the PR is not open.
            if pr_is_open is False:
                return PENDING_PR
            return ADVANCE
        # verdict == "blocked" or pr_opened without a resolvable PR number.
        return PM_ROUTE

    if role in ("reviewer", "security"):
        if fix_attempts >= max_fix_attempts:
            return ESCALATE
        # "awaiting-fix:" guard: a fix card is already in flight, don't re-route.
        if "awaiting-fix:" in handoff_text.lower():
            return ""
        if verdict == "approved":
            return APPROVE_ADVANCE
        if verdict == "changes_requested":
            return PM_ROUTE
        return ""

    if role == "qa":
        if verdict == "passed":
            return ADVANCE
        if verdict == "failed":
            return QA_FIX
        return PENDING_SIGNAL

    if role == "a11y":
        if verdict in ("approved", "na", "skipped"):
            return ADVANCE
        if verdict == "changes_requested":
            return PM_ROUTE
        return PENDING_SIGNAL

    return None  # unknown role — caller should fall back to prefix


# ── pure helpers ──────────────────────────────────────────────────────────────


def _parse_handoff(handoff_text: str) -> dict[str, Any]:
    """Parse a handoff string for key signals (review-required, PR #, changes requested, approved).

    Returns a dict with keys: is_review_required, pr_number, is_changes_requested,
    is_approved, findings_text.
    """
    text = handoff_text or ""
    result: dict[str, Any] = {
        "is_review_required": "review-required" in text.lower(),
        "pr_number": extract_pr_number_from_summary(text),
        "is_changes_requested": False,
        "is_approved": False,
        "findings_text": text,
    }

    # Detect review outcomes
    lower = text.lower().lstrip()
    # Changes-requested signals — checked with startswith to avoid false positives.
    # Role-specific prefixes are listed explicitly because e.g. "review-changes-requested:"
    # starts with "review-" not "changes-", so a plain "changes-requested" prefix would miss it.
    change_signals = [
        "changes-requested",           # bare hyphenated form
        "changes requested",           # bare spaced form (accessibility: "changes requested: …")
        "changes required",
        "blocking findings",
        "request changes",
        "needs fixes",
        "need fixes",
        "review-changes-requested",    # reviewer SOUL: "review-changes-requested: <reason>"
        "security-changes-requested",  # security SOUL: "security-changes-requested: <reason>"
        "a11y-changes-requested",      # accessibility SOUL legacy form
    ]
    if any(lower.startswith(s) for s in change_signals):
        result["is_changes_requested"] = True

    # Approval signals — checked with startswith to prevent mid-string matches.
    # Example false-positive avoided: "changes-requested: approved workaround" no longer
    # sets is_approved=True (it starts with "changes-requested", not "approved").
    # "review-approved" is explicit because "review-approved: PR #N" does not start
    # with the bare "approved" token (#1125 F1).
    # Removed: "pass" (ambiguous — caused false positives on "tests pass", "password").
    approve_signals = [
        "approved",           # bare approval: "approved: …" or "approved — …"
        "review-approved",    # reviewer SOUL: "review-approved: PR #N"
        "sign-off",
        "signoff",
        "lgtm",
        "looks good",
        "no findings",
        ":+1:",
        "qa-passed",          # qa: "qa-passed: PR #N verified"
        "security-approved",  # security: "security-approved: PR #N"
        "security-passed",
        # The security agent's documented pass signal is 'security: cleared'
        # (#1185). Without it, classify_blocked returned "" for a cleared
        # security card — it never APPROVE_ADVANCEd and the #1182 PM-consult
        # skip could not recognise it as a passing handoff.
        "security: cleared",
        "security cleared",
    ]
    if any(lower.startswith(s) for s in approve_signals):
        result["is_approved"] = True

    return result


def classify_blocked(
    card_assignee: str,
    handoff_text: str,
    ci_green: bool,
    *,
    fix_attempts: int = 0,
    pr_number: int | None = None,
    raw_ci: str | None = None,
    pr_is_open: bool | None = None,
    pr_is_merged: bool | None = None,
    skip_qa: bool = False,
    max_fix_attempts: int = MAX_FIX_ATTEMPTS,
    _source_collector: list[str] | None = None,
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
        pr_is_open: Whether the resolved PR is a *real, currently-open* PR per
                the provider (#953). ``True`` / ``None`` (unverified) preserve
                prior behaviour; ``False`` means the provider affirmatively
                reports no open PR, so a developer card is held in PENDING_PR
                instead of advancing — this prevents releasing the QA child
                against a phantom/stale PR or a concurrent mid-edit tree.
        pr_is_merged: Whether the resolved PR was *merged* per the provider
                (#957). ``True`` means the developer's work landed (e.g. a human
                merged the review-required PR directly, bypassing the pipeline),
                so a developer card reconciles — completing it and its sibling
                pipeline cards — instead of looping forever in PENDING_PR.
                Checked before the ``pr_is_open is False`` gate because a merged
                PR is also "not open" but means the opposite (done, not phantom).
        skip_qa: When True, the QA card bypasses the ``qa-passed`` signal
                 requirement and advances immediately (used when the PR has
                 the ``skip-qa`` label applied).
        max_fix_attempts: The escalation cap for developer/reviewer/security
                 fix cycles. Defaults to the module constant ``MAX_FIX_ATTEMPTS``
                 (3); the dispatcher threads the ``execution.max_fix_attempts``
                 config override in via ``run_iterate``.

    Returns one of: {advance, qa_fix, pending_signal, pm_route, approve_advance,
    escalate, reconcile_merged}.

    Note: For developer-daedalus cards with ``review-required: PR #N``, ADVANCE
    fires immediately regardless of CI state — CI gating is enforced at
    merge-time only (per epic #1074). The ``ci_green`` and ``raw_ci`` parameters
    are still accepted for backward compatibility and merge-gate logic but no
    longer gate the ADVANCE action for developer cards.
    """
    assignee = (card_assignee or "").lower().strip()
    handoff = _parse_handoff(handoff_text)

    # Resolve PR number: handoff first, then the explicit fallback.
    effective_pr = handoff["pr_number"] or pr_number

    # ── Phase 1 (#1170): try structured JSON outcome FIRST ───────────────────
    # Parse the last fenced JSON block (or bare JSON object) from the summary.
    # When valid AND the role matches the card assignee, route by the
    # (role, verdict) → action table and record "json" telemetry.
    # On None (no JSON / invalid JSON / role mismatch), fall through to the
    # prefix routing below unchanged.
    #
    # QA skip_qa bypass: the skip-qa label overrides ALL signal requirements
    # (prefix *and* JSON) and advances immediately — check it before JSON.
    if assignee == "qa-daedalus" and skip_qa:
        if _source_collector is not None:
            # skip_qa overrides routing entirely; JSON outcome may or may not
            # be present — record the source of the outcome that would have
            # been used otherwise so the telemetry reflects signal presence.
            _outcome_for_tel = _parse_outcome(handoff_text or "")
            expected_role_tel = _ASSIGNEE_TO_ROLE.get(assignee)
            if (
                _outcome_for_tel is not None
                and expected_role_tel is not None
                and _outcome_for_tel.role == expected_role_tel
            ):
                _source_collector.append("json")
            else:
                _source_collector.append("prefix")
        return ADVANCE

    _outcome = _parse_outcome(handoff_text or "")
    _expected_role = _ASSIGNEE_TO_ROLE.get(assignee)
    _use_json = (
        _outcome is not None
        and _expected_role is not None
        and _outcome.role == _expected_role
    )
    if _source_collector is not None:
        _source_collector.append("json" if _use_json else "prefix")

    if _use_json:
        # _outcome is not None here (mypy: assert for type narrowing)
        assert _outcome is not None
        _json_action = _classify_by_outcome(
            _outcome,
            handoff_text or "",
            fix_attempts,
            max_fix_attempts,
            effective_pr,
            pr_is_open,
            pr_is_merged,
        )
        if _json_action is not None:
            return _json_action
        # _classify_by_outcome returned None → unknown role in mapping table
        # (should not happen for a validated outcome, but be defensive).
        logger.debug(
            "classify: JSON outcome role=%r not in mapping table — "
            "falling back to prefix routing",
            _outcome.role,
        )

    # ── planner → decompose or PM ────────────────────────────────────────
    if assignee == "planner-daedalus":
        # Use startswith so a mid-string "PLANNING COMPLETE" in unrelated text
        # cannot trip this gate (#1125 F1).
        if (handoff_text or "").upper().lstrip().startswith("PLANNING COMPLETE"):
            return PLANNER_DECOMPOSE
        return PM_ROUTE  # unexpected planner output → escalate to PM

    # ── documentation-daedalus → terminal complete ────────────────────────
    # Docs is the last pipeline stage. When it blocks with 'docs posted:'
    # the job is done — complete the card. Anything else routes to PM.
    if assignee == "documentation-daedalus":
        if (handoff_text or "").lower().lstrip().startswith("docs posted"):
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
        if fix_attempts >= max_fix_attempts:
            return ESCALATE
        # Review-required handoff with PR → ADVANCE immediately (CI no longer gates)
        # CI gating is enforced at merge-time only (per epic #1074), so
        # QA/reviewer/security dispatch happens as soon as the PR is opened.
        if handoff["is_review_required"] and effective_pr:
            # #957: the resolved PR was merged outside the pipeline (a human
            # merged the review-required PR directly). The board may already
            # show the issue as Done, so neither the board-sync nor the
            # orphaned-close cleanup reaches this card — it would loop forever
            # in PENDING_PR. The work has landed, so reconcile: complete this
            # card and any sibling pipeline cards. Checked before the #953
            # not-open gate below because a merged PR is also "not open" but
            # means done, not phantom.
            if pr_is_merged is True:
                return RECONCILE_MERGED
            # #953 hard gate: never advance (which completes the dev card and
            # releases its QA child) when the provider affirmatively reports
            # the resolved PR is NOT open. The handoff's "PR #N" is just a
            # string the agent typed — it may be stale, wrong, or never opened
            # (the developer was still mid-edit). Hold in PENDING_PR; the
            # pending-PR executor re-checks the VCS for a real PR next tick.
            if pr_is_open is False:
                return PENDING_PR
            # CI state is no longer a gate for ADVANCE — the PR is open and
            # review-required, so dispatch QA/reviewer/security immediately.
            # CI status is still captured by the main loop (ci_cache) and
            # enforced at merge-time by the auto-merge gate.
            return ADVANCE
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
        if fix_attempts >= max_fix_attempts:
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
    # NOTE: skip_qa bypass is handled earlier (before JSON/prefix routing),
    # so by the time we reach here skip_qa is always False.
    if assignee == "qa-daedalus":
        # Use startswith to prevent a mid-string match such as
        # "qa-passed: but then qa-failed on retry" advancing the pipeline (#1125 F1).
        summary = (handoff_text or "").lower().lstrip()
        if summary.startswith("qa-passed"):
            return ADVANCE
        if summary.startswith("qa-failed"):
            return QA_FIX
        return PENDING_SIGNAL

    # ── accessibility-daedalus card ───────────────────────────────────────
    # Accessibility auditors PRs for WCAG 2.1 AA compliance. Posts a signal
    # starting with 'approved' / 'a11y-approved' / 'accessibility-na' /
    # 'a11y-skipped' to advance, 'changes requested:' to route back to the PM,
    # otherwise pending.
    # startswith prevents "changes-requested: approved workaround" from falsely
    # matching the 'approved' advance gate (#1125 F1).
    # 'a11y-approved' is kept alongside bare 'approved' because the SOUL emits
    # 'a11y-approved: PR #N' whose prefix is 'a11y-approved', not 'approved'.
    if assignee == "accessibility-daedalus":
        summary = (handoff_text or "").lower().lstrip()
        if summary.startswith(("approved", "a11y-approved", "accessibility-na", "a11y-skipped")):
            return ADVANCE
        if summary.startswith("changes requested"):
            return PM_ROUTE
        return PENDING_SIGNAL

    # ── validator-daedalus card ───────────────────────────────────────────
    # Validators should only ever complete (CONFIRMED/BLOCKED/ALREADY_FIXED).
    # If one is blocked with awaiting-pr the delegated CC agent used the
    # developer protocol by mistake — escalate so a human can manually
    # complete it with the correct verdict.
    if assignee == "validator-daedalus":
        return ESCALATE

    # ── unknown assignee ─────────────────────────────────────────────────
    return ""
