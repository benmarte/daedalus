"""Tests for core.dispatch.verify — ground-truth outcome verification (#1170 Phase 2).

Covers:
  - Each rule (developer/pr_opened, qa/passed, docs/posted): pass / mismatch / skip
  - Fail-open on provider exception
  - Config gate: verify_outcomes=false (default) → verify_outcome never called
  - Non-JSON prefix path → no verification
  - No provider → skipped immediately
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import check  # noqa: E402,F401

from core.iterate.outcomes import OutcomeRecord
from core.providers.base import PRSummary, CIStatus
from core.dispatch.verify import VerifyResult, verify_outcome


# ── helpers ───────────────────────────────────────────────────────────────────

def _record(role: str, verdict: str, *, issue_ref: int | None = None,
            pr_ref: int | None = None, evidence: dict | None = None,
            note: str = "") -> OutcomeRecord:
    return OutcomeRecord(
        schema_version=1,
        role=role,
        verdict=verdict,
        issue_ref=issue_ref,
        pr_ref=pr_ref,
        evidence=evidence or {},
        note=note,
    )


class _VerifyProvider:
    """Minimal provider double for verify_outcome tests.

    Configurable per-test: set attributes to control what each method returns
    or whether it raises.
    """

    supports_ci_status: bool = True

    def __init__(self) -> None:
        self._pr: PRSummary | None = None
        self._ci: str = CIStatus.GREEN
        self._pr_comments: list | None = None  # None = no list_pr_comments attr
        self._issue_comments: list[dict] = []
        self._pr_raises: Exception | None = None
        self._ci_raises: Exception | None = None
        self._pr_comments_raises: Exception | None = None
        self._issue_comments_raises: Exception | None = None
        self.pr_for_issue_calls: list[int] = []
        self.ci_calls: list[int] = []

    def _pr_for_issue(self, issue_number: int) -> PRSummary | None:
        self.pr_for_issue_calls.append(issue_number)
        if self._pr_raises:
            raise self._pr_raises
        return self._pr

    def get_pr_ci_status(self, pr_number: int) -> str:
        self.ci_calls.append(pr_number)
        if self._ci_raises:
            raise self._ci_raises
        return self._ci

    def list_pr_comments(self, pr_number: int) -> list:
        if self._pr_comments_raises:
            raise self._pr_comments_raises
        if self._pr_comments is None:
            raise AttributeError("list_pr_comments not configured")
        return list(self._pr_comments)

    def get_issue_comments(self, issue_number: int) -> list[dict]:
        if self._issue_comments_raises:
            raise self._issue_comments_raises
        return list(self._issue_comments)


# ── Rule 1: developer/pr_opened ───────────────────────────────────────────────


def test_developer_pr_opened_verified():
    p = _VerifyProvider()
    p._pr = PRSummary(number=42, head_branch="fix/issue-100-something")
    rec = _record("developer", "pr_opened", issue_ref=100, pr_ref=42)
    result = verify_outcome(rec, p, issue_number=100)
    check("verdict is verified", result.verdict == "verified")
    check("pr_for_issue called with 100", p.pr_for_issue_calls == [100])


def test_developer_pr_opened_mismatch_no_pr():
    p = _VerifyProvider()
    p._pr = None
    rec = _record("developer", "pr_opened", issue_ref=101)
    result = verify_outcome(rec, p, issue_number=101)
    check("verdict is mismatch", result.verdict == "mismatch")
    check("note mentions not found", "not found" in result.note)


def test_developer_pr_opened_mismatch_unrelated_branch():
    p = _VerifyProvider()
    # Branch name doesn't reference issue 101
    p._pr = PRSummary(number=55, head_branch="feat/unrelated-feature")
    rec = _record("developer", "pr_opened", issue_ref=101, pr_ref=55)
    result = verify_outcome(rec, p, issue_number=101)
    check("verdict is mismatch when branch unrelated", result.verdict == "mismatch")


def test_developer_pr_opened_skipped_on_provider_error():
    p = _VerifyProvider()
    p._pr_raises = RuntimeError("API timeout")
    rec = _record("developer", "pr_opened", issue_ref=102)
    result = verify_outcome(rec, p, issue_number=102)
    check("fail-open: verdict is skipped", result.verdict == "skipped")
    check("note mentions provider error", "provider error" in result.note)


def test_developer_pr_opened_skipped_no_issue_number():
    p = _VerifyProvider()
    # No issue_ref in record and no override → skipped
    rec = _record("developer", "pr_opened")
    result = verify_outcome(rec, p)
    check("verdict is skipped when no issue_number", result.verdict == "skipped")
    check("pr_for_issue not called", p.pr_for_issue_calls == [])


def test_developer_pr_opened_no_provider():
    rec = _record("developer", "pr_opened", issue_ref=1)
    result = verify_outcome(rec, None, issue_number=1)
    check("no provider → skipped", result.verdict == "skipped")
    check("note mentions no provider", "no provider" in result.note)


# ── Rule 2: qa/passed (ci:green) ─────────────────────────────────────────────


def test_qa_passed_ci_green_verified():
    p = _VerifyProvider()
    p._ci = CIStatus.GREEN
    rec = _record("qa", "passed", pr_ref=10, evidence={"ci": "green"})
    result = verify_outcome(rec, p, pr_number=10)
    check("verdict is verified", result.verdict == "verified")
    check("ci status checked for PR 10", p.ci_calls == [10])


def test_qa_passed_ci_red_mismatch():
    p = _VerifyProvider()
    p._ci = CIStatus.RED
    rec = _record("qa", "passed", pr_ref=11, evidence={"ci": "green"})
    result = verify_outcome(rec, p, pr_number=11)
    check("verdict is mismatch", result.verdict == "mismatch")
    check("note mentions CI claim mismatch", "CI claim mismatch" in result.note)


def test_qa_passed_no_ci_claim_skipped():
    p = _VerifyProvider()
    # evidence has no "ci" key → no claim to verify
    rec = _record("qa", "passed", pr_ref=12, evidence={})
    result = verify_outcome(rec, p, pr_number=12)
    check("no ci claim → skipped", result.verdict == "skipped")
    check("ci status not checked", p.ci_calls == [])


def test_qa_passed_provider_no_ci_support():
    p = _VerifyProvider()
    p.supports_ci_status = False
    rec = _record("qa", "passed", pr_ref=13, evidence={"ci": "green"})
    result = verify_outcome(rec, p, pr_number=13)
    check("no ci support → skipped", result.verdict == "skipped")
    check("ci status not checked", p.ci_calls == [])


def test_qa_passed_ci_provider_error():
    p = _VerifyProvider()
    p._ci_raises = ConnectionError("network down")
    rec = _record("qa", "passed", pr_ref=14, evidence={"ci": "green"})
    result = verify_outcome(rec, p, pr_number=14)
    check("fail-open: verdict is skipped", result.verdict == "skipped")
    check("note has provider error", "provider error" in result.note)


def test_qa_passed_no_pr_number():
    p = _VerifyProvider()
    rec = _record("qa", "passed", evidence={"ci": "green"})
    result = verify_outcome(rec, p)
    check("no pr_number → skipped", result.verdict == "skipped")


# ── Rule 3: docs/posted ──────────────────────────────────────────────────────


def _doc_comment():
    """A fake Comment with the docs signal."""
    from types import SimpleNamespace
    return SimpleNamespace(body="**Agent: documentation** — see PR #99", author="docs-daedalus")


def test_docs_posted_verified_via_pr_comments():
    p = _VerifyProvider()
    p._pr_comments = [_doc_comment()]
    rec = _record("docs", "posted", pr_ref=20, issue_ref=200)
    result = verify_outcome(rec, p, pr_number=20, issue_number=200)
    check("verified via PR comments", result.verdict == "verified")


def test_docs_posted_verified_via_issue_comments():
    """Provider without list_pr_comments falls back to issue comments."""

    class _IssueOnlyProvider:
        """Provider with no list_pr_comments — only issue comments."""
        supports_ci_status = False

        def get_issue_comments(self, issue_number: int) -> list[dict]:
            return [{"body": "**Agent: documentation** done"}]

    p = _IssueOnlyProvider()
    rec = _record("docs", "posted", issue_ref=201)
    result = verify_outcome(rec, p, issue_number=201)
    check("verified via issue comments", result.verdict == "verified")


def test_docs_posted_mismatch_no_comment():
    p = _VerifyProvider()
    p._pr_comments = [{"body": "just a regular comment"}]
    p._issue_comments = [{"body": "no docs signal here"}]
    rec = _record("docs", "posted", pr_ref=21, issue_ref=202)
    result = verify_outcome(rec, p, pr_number=21, issue_number=202)
    check("verdict is mismatch", result.verdict == "mismatch")
    check("note mentions docs comment not found", "docs comment not found" in result.note)


def test_docs_posted_pr_comments_raises_falls_back_to_issue():
    """list_pr_comments raises → fall through to issue-comments (fail-partial-open)."""
    p = _VerifyProvider()
    p._pr_comments_raises = RuntimeError("PR comments API down")
    p._issue_comments = [{"body": "**Agent: documentation** posted"}]
    rec = _record("docs", "posted", pr_ref=22, issue_ref=203)
    result = verify_outcome(rec, p, pr_number=22, issue_number=203)
    check("falls back to issue-comments and verifies", result.verdict == "verified")


def test_docs_posted_both_sources_raise():
    """Both comment sources raise → skipped (fail-open)."""
    p = _VerifyProvider()
    p._pr_comments_raises = RuntimeError("PR comments down")
    p._issue_comments_raises = ConnectionError("issue comments down")
    rec = _record("docs", "posted", pr_ref=23, issue_ref=204)
    result = verify_outcome(rec, p, pr_number=23, issue_number=204)
    check("fail-open on both errors", result.verdict == "skipped")


def test_docs_posted_no_pr_no_issue():
    p = _VerifyProvider()
    rec = _record("docs", "posted")
    result = verify_outcome(rec, p)
    check("no pr and no issue → skipped", result.verdict == "skipped")


# ── Roles with no verifiable claims ──────────────────────────────────────────


def test_validator_confirmed_skipped():
    p = _VerifyProvider()
    rec = _record("validator", "confirmed", issue_ref=1)
    result = verify_outcome(rec, p, issue_number=1)
    check("validator/confirmed has no VCS claims → skipped", result.verdict == "skipped")
    check("no PR or CI lookups made", p.pr_for_issue_calls == [] and p.ci_calls == [])


def test_reviewer_approved_skipped():
    p = _VerifyProvider()
    rec = _record("reviewer", "approved", pr_ref=99)
    result = verify_outcome(rec, p, pr_number=99)
    check("reviewer/approved has no VCS claims → skipped", result.verdict == "skipped")


# ── Config gate via run_iterate ───────────────────────────────────────────────


def test_verify_not_called_when_config_off(monkeypatch):
    """When verify_outcomes=false (default), verify_outcome is never invoked.

    We monkeypatch verify_outcome at the core.iterate level to detect calls.
    """
    called: list[bool] = []

    import core.iterate as _it

    original = _it.verify_outcome

    def _spy(*args, **kwargs):  # type: ignore[no-untyped-def]
        called.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(_it, "verify_outcome", _spy)

    from core.iterate import run_iterate
    from unittest.mock import patch, MagicMock

    p = _VerifyProvider()
    p._pr = PRSummary(number=5, head_branch="fix/issue-300-foo")

    # resolved config has verify_outcomes=false (default)
    resolved = {"workdir": "", "execution": {"verify_outcomes": False}}

    with patch("core.iterate.kanban") as mk:
        mk.list_blocked.return_value = []
        mk.list_tasks.return_value = []
        run_iterate("slug", "org/repo", resolved=resolved, provider=p)

    check("verify_outcome not called when config=false", called == [])


def test_verify_not_called_for_prefix_routed_card(monkeypatch):
    """Prefix-only cards (no JSON OutcomeRecord) never trigger verify_outcome."""
    called: list[bool] = []

    import core.iterate as _it
    original = _it.verify_outcome

    def _spy(*args, **kwargs):  # type: ignore[no-untyped-def]
        called.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(_it, "verify_outcome", _spy)

    from core.iterate import run_iterate
    from unittest.mock import patch

    p = _VerifyProvider()
    resolved = {"workdir": "", "execution": {"verify_outcomes": True}}

    blocked_card = {
        "id": "t1",
        "title": "fix issue #310",
        "assignee": "developer-daedalus",
        "block_reason": "pr-opened: PR #5",  # prefix-only, no JSON block
    }

    with patch("core.iterate.kanban") as mk:
        mk.list_blocked.return_value = [blocked_card]
        mk.list_tasks.return_value = []
        mk.show_card.return_value = None
        run_iterate("slug", "org/repo", resolved=resolved, provider=p)

    check("verify_outcome not called for prefix-routed card", called == [])
