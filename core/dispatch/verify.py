"""core.dispatch.verify — ground-truth outcome verification gate (#1170 Phase 2).

``verify_outcome()`` is the single public entry point.  It validates the VCS
claims in a parsed :class:`~core.iterate.outcomes.OutcomeRecord` against the
live provider **before** the dispatcher acts on a stage completion, so phantom-
PR / premature-completion bugs are caught structurally rather than retried around.

Verification rules (JSON-path only; prefix-only completions skip entirely):

  developer / pr_opened
    ``provider._pr_for_issue(issue_number)`` must return a PR that
    :func:`~core.providers.base.issue_linked_to_pr` says references the issue.
    Missing → ``mismatch("verify: claimed PR #N not found")``.

  qa / passed  (when ``evidence["ci"] == "green"``)
    ``provider.get_pr_ci_status(pr_number)`` must return ``CIStatus.GREEN``.
    Red/absent → ``mismatch("verify: CI claim mismatch")``.

  docs / posted
    ``provider.list_pr_comments(pr_number)`` (or ``get_issue_comments``) must
    contain an ``**Agent: documentation**`` comment.
    Absent → ``mismatch("verify: docs comment not found")``.

Fail-open semantics (CRITICAL — never brick the pipeline on API hiccups):
  * Any provider exception → ``VerifyResult("skipped", "provider error: ...")``.
    Logged at WARNING so telemetry shows skips without halting the tick.
  * ``provider is None`` → ``skipped`` immediately.
  * Roles / verdicts with no verifiable claim → ``skipped`` (not ``verified``)
    so telemetry accurately counts only checks that actually ran.

Config gate:
  ``execution.verify_outcomes: false`` (the default).  Callers MUST check the
  config flag and only call this function when it is ``true``.

Telemetry:
  Callers record the ``VerifyResult.verdict`` in the dispatch history JSONL
  alongside the Phase-1 ``outcome_source`` telemetry so operators can observe
  the verification pass-rate and tune the roll-out.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from core.iterate.outcomes import OutcomeRecord
from core.providers.base import CIStatus, issue_linked_to_pr

logger = logging.getLogger("daedalus.dispatch.verify")

# ── public result type ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class VerifyResult:
    """Result of a single ground-truth verification check.

    ``verdict`` is one of:
      ``"verified"`` — the VCS claim checks out; dispatcher may proceed.
      ``"mismatch"`` — the claim is false; dispatcher should downgrade action.
      ``"skipped"``  — check not performed (no provider, error, or N/A); fail-open.
    ``note`` carries the human-readable detail (empty when clean).
    """

    verdict: str   # "verified" | "mismatch" | "skipped"
    note: str


# ── sentinel instances (avoid repeated allocation in the hot path) ────────────

_VERIFIED: VerifyResult = VerifyResult("verified", "")
_SKIP_NO_PROVIDER: VerifyResult = VerifyResult("skipped", "no provider")


# ── public API ────────────────────────────────────────────────────────────────


def verify_outcome(
    record: OutcomeRecord,
    provider: Any,
    *,
    issue_number: int | None = None,
    pr_number: int | None = None,
) -> VerifyResult:
    """Verify the VCS claims in *record* against the live provider state.

    Parameters
    ----------
    record:
        The validated :class:`~core.iterate.outcomes.OutcomeRecord` extracted
        from the agent's card summary.  Must not be ``None``.
    provider:
        Live :class:`~core.providers.base.VCSProvider` instance.  ``None``
        → ``skipped`` immediately (fail-open).
    issue_number:
        Override for the issue number when the record's ``issue_ref`` is absent.
    pr_number:
        Override for the PR number when the record's ``pr_ref`` is absent.

    Returns
    -------
    VerifyResult
        Verdict + note.  Never raises.
    """
    if provider is None:
        return _SKIP_NO_PROVIDER

    role = record.role
    verdict = record.verdict

    # ── rule 1: developer / pr_opened ─────────────────────────────────────────
    if role == "developer" and verdict == "pr_opened":
        return _verify_pr_opened(record, provider, issue_number)

    # ── rule 2: qa / passed with evidence.ci == "green" ───────────────────────
    if role == "qa" and verdict == "passed":
        claimed_ci = (record.evidence.get("ci") or "").lower()
        if claimed_ci == "green":
            return _verify_ci_green(record, provider, pr_number)
        # No CI claim in evidence → nothing to verify.
        return VerifyResult("skipped", "qa/passed: no ci claim in evidence")

    # ── rule 3: docs / posted ─────────────────────────────────────────────────
    if role == "docs" and verdict == "posted":
        return _verify_docs_posted(record, provider, issue_number, pr_number)

    # All other roles / verdicts have no verifiable VCS claims in Phase 2.
    return VerifyResult("skipped", f"{role}/{verdict}: no verifiable claims in Phase 2")


# ── rule implementations ──────────────────────────────────────────────────────


def _verify_pr_opened(
    record: OutcomeRecord,
    provider: Any,
    issue_number: int | None,
) -> VerifyResult:
    """Rule 1 — developer/pr_opened: a matching PR must exist in the VCS."""
    # Prefer the record's issue_ref; fall back to the caller-supplied number.
    iss: int | None = record.issue_ref if record.issue_ref is not None else issue_number
    if iss is None:
        return VerifyResult("skipped", "developer/pr_opened: no issue_number available")

    claimed_pr: int | None = record.pr_ref  # may be None when agent omitted it

    try:
        pr = provider._pr_for_issue(iss)
    except Exception as exc:
        logger.warning(
            "verify: _pr_for_issue(#%s) raised %s — skipping PR verification (fail-open)",
            iss,
            exc,
        )
        return VerifyResult("skipped", f"provider error: {exc}")

    if pr is None or not getattr(pr, "number", None):
        note = (
            f"verify: claimed PR #{claimed_pr} not found "
            f"(provider returned no PR for issue #{iss})"
        )
        logger.warning("verify: developer/pr_opened MISMATCH — %s", note)
        return VerifyResult("mismatch", note)

    pr_num: int = pr.number  # type: ignore[assignment]
    if not issue_linked_to_pr(pr, iss):
        note = (
            f"verify: PR #{pr_num} does not reference issue #{iss} "
            f"(head_branch={getattr(pr, 'head_branch', '')!r})"
        )
        logger.warning("verify: developer/pr_opened MISMATCH — %s", note)
        return VerifyResult("mismatch", note)

    logger.info(
        "verify: developer/pr_opened VERIFIED — PR #%s references issue #%s",
        pr_num,
        iss,
    )
    return _VERIFIED


def _verify_ci_green(
    record: OutcomeRecord,
    provider: Any,
    pr_number: int | None,
) -> VerifyResult:
    """Rule 2 — qa/passed with ci:green claim: provider must report CI green."""
    pr: int | None = record.pr_ref if record.pr_ref is not None else pr_number
    if pr is None:
        return VerifyResult("skipped", "qa/passed: no pr_number available for CI check")

    if not getattr(provider, "supports_ci_status", False):
        return VerifyResult(
            "skipped",
            f"qa/passed: provider does not support CI status (PR #{pr})",
        )

    try:
        ci: str = provider.get_pr_ci_status(pr)
    except Exception as exc:
        logger.warning(
            "verify: get_pr_ci_status(#%s) raised %s — skipping CI verification (fail-open)",
            pr,
            exc,
        )
        return VerifyResult("skipped", f"provider error: {exc}")

    if ci != CIStatus.GREEN:
        note = (
            f"verify: CI claim mismatch — "
            f"agent claimed green but provider reports {ci!r} for PR #{pr}"
        )
        logger.warning("verify: qa/passed ci:green MISMATCH — %s", note)
        return VerifyResult("mismatch", note)

    logger.info("verify: qa/passed ci:green VERIFIED — PR #%s CI is green", pr)
    return _VERIFIED


def _verify_docs_posted(
    record: OutcomeRecord,
    provider: Any,
    issue_number: int | None,
    pr_number: int | None,
) -> VerifyResult:
    """Rule 3 — docs/posted: an **Agent: documentation** comment must exist."""
    pr: int | None = record.pr_ref if record.pr_ref is not None else pr_number
    iss: int | None = record.issue_ref if record.issue_ref is not None else issue_number

    if pr is None and iss is None:
        return VerifyResult(
            "skipped",
            "docs/posted: no pr_number or issue_number available",
        )

    _DOC_SIGNAL = "**Agent: documentation**"

    # ── Check PR comments first (primary location) ────────────────────────────
    if pr is not None:
        _list_pr_comments = getattr(provider, "list_pr_comments", None)
        if callable(_list_pr_comments):
            try:
                for c in _list_pr_comments(pr):
                    if _DOC_SIGNAL in (getattr(c, "body", None) or ""):
                        logger.info(
                            "verify: docs/posted VERIFIED — "
                            "found Agent: documentation comment on PR #%s",
                            pr,
                        )
                        return _VERIFIED
            except Exception as exc:
                logger.warning(
                    "verify: list_pr_comments(#%s) raised %s — "
                    "falling through to issue-comments check",
                    pr,
                    exc,
                )

    # ── Fall back to issue comments ───────────────────────────────────────────
    if iss is not None:
        _get_issue_comments = getattr(provider, "get_issue_comments", None)
        if callable(_get_issue_comments):
            try:
                for c in _get_issue_comments(iss):
                    body = c.get("body") if isinstance(c, dict) else getattr(c, "body", "")
                    if _DOC_SIGNAL in (body or ""):
                        logger.info(
                            "verify: docs/posted VERIFIED — "
                            "found Agent: documentation comment on issue #%s",
                            iss,
                        )
                        return _VERIFIED
            except Exception as exc:
                logger.warning(
                    "verify: get_issue_comments(#%s) raised %s — "
                    "skipping docs verification (fail-open)",
                    iss,
                    exc,
                )
                return VerifyResult("skipped", f"provider error: {exc}")

    note = (
        f"verify: docs comment not found "
        f"(checked PR #{pr} / issue #{iss})"
    )
    logger.warning("verify: docs/posted MISMATCH — %s", note)
    return VerifyResult("mismatch", note)
