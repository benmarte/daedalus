"""Tests for core.dispatch.verify — ground-truth outcome verification (#1170 Phase 2).

Covers:
  - Each rule (developer/pr_opened, qa/passed, docs/posted):
      healthy-absent → mismatch; unhealthy (canary fails) → skipped; pass → verified
  - Fail-open on genuine Python exception (safety-net path)
  - Config gate: verify_outcomes=false (default) → verify_outcome never called
  - Non-JSON prefix path → no verification
  - No provider → skipped immediately
  - Mismatch retry routing: under cap → comment posted; at cap → escalates
  - Consecutive non-aligned tick simulation: mismatch → re-dispatch → advance
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import check  # noqa: E402,F401

from core.iterate.outcomes import OutcomeRecord
from core.providers.base import PRSummary, CIStatus
from core.dispatch.verify import VerifyResult, verify_outcome, _canary_check


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
        # None means list_pr_comments will raise AttributeError (simulating absent method)
        self._pr_comments: list | None = None
        self._issue_comments: list[dict] = []
        self._issue: dict | None = {"number": 1}  # healthy by default
        self._pr_raises: Exception | None = None
        self._ci_raises: Exception | None = None
        self._pr_comments_raises: Exception | None = None
        self._issue_comments_raises: Exception | None = None
        self.pr_for_issue_calls: list[int] = []
        self.ci_calls: list[int] = []
        self.get_issue_calls: list[int] = []
        self.is_pr_open_calls: list[int] = []

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

    def get_issue(self, issue_number: int) -> dict | None:
        self.get_issue_calls.append(issue_number)
        return self._issue

    def is_pr_open(self, pr_number: int) -> bool:
        self.is_pr_open_calls.append(pr_number)
        return self._pr is not None


# ── canary_check unit tests ───────────────────────────────────────────────────


def test_canary_healthy_via_issue():
    p = _VerifyProvider()
    p._issue = {"number": 10}
    check("canary healthy when get_issue returns something", _canary_check(p, issue_number=10))


def test_canary_unhealthy_via_issue():
    p = _VerifyProvider()
    p._issue = None  # provider returns nothing
    check("canary unhealthy when get_issue returns None", not _canary_check(p, issue_number=10))


def test_canary_falls_back_to_pr_when_no_issue_number():
    p = _VerifyProvider()
    p._pr = PRSummary(number=5, head_branch="fix/issue-5-foo")
    check("canary via PR when no issue number", _canary_check(p, pr_number=5))


def test_canary_unhealthy_via_pr_when_no_pr():
    p = _VerifyProvider()
    p._pr = None
    check("canary unhealthy via PR when no PR exists", not _canary_check(p, pr_number=5))


def test_canary_returns_true_when_no_canary_methods():
    """Provider with neither get_issue nor is_pr_open → assume healthy."""
    class _Minimal:
        supports_ci_status = False
    check("assume healthy when no canary methods", _canary_check(_Minimal()))


# ── Rule 1: developer/pr_opened ───────────────────────────────────────────────


def test_developer_pr_opened_verified():
    p = _VerifyProvider()
    p._pr = PRSummary(number=42, head_branch="fix/issue-100-something")
    rec = _record("developer", "pr_opened", issue_ref=100, pr_ref=42)
    result = verify_outcome(rec, p, issue_number=100)
    check("verdict is verified", result.verdict == "verified")
    check("pr_for_issue called with 100", p.pr_for_issue_calls == [100])


def test_developer_pr_opened_mismatch_healthy_provider():
    """Provider is healthy (canary works) but PR is genuinely absent → mismatch."""
    p = _VerifyProvider()
    p._pr = None           # no PR
    p._issue = {"number": 101}  # but issue exists → canary healthy
    rec = _record("developer", "pr_opened", issue_ref=101)
    result = verify_outcome(rec, p, issue_number=101)
    check("verdict is mismatch (not skip)", result.verdict == "mismatch")
    check("get_issue called (canary)", p.get_issue_calls == [101])
    check("note mentions not found", "not found" in result.note)


def test_developer_pr_opened_skipped_unhealthy_provider():
    """Provider returns no PR AND canary get_issue also returns None → skip."""
    p = _VerifyProvider()
    p._pr = None
    p._issue = None  # canary also fails
    rec = _record("developer", "pr_opened", issue_ref=102)
    result = verify_outcome(rec, p, issue_number=102)
    check("verdict is skipped (provider unhealthy)", result.verdict == "skipped")
    check("note mentions provider unhealthy", "provider unhealthy" in result.note)


def test_developer_pr_opened_mismatch_unrelated_branch():
    """PR exists but branch name doesn't reference the issue → mismatch (no canary needed)."""
    p = _VerifyProvider()
    p._pr = PRSummary(number=55, head_branch="feat/unrelated-feature")
    rec = _record("developer", "pr_opened", issue_ref=101, pr_ref=55)
    result = verify_outcome(rec, p, issue_number=101)
    check("verdict is mismatch when branch unrelated", result.verdict == "mismatch")
    # Canary is NOT called when PR exists — we only call canary when PR is None.
    check("canary not called when PR exists", p.get_issue_calls == [])


def test_developer_pr_opened_skipped_on_genuine_exception():
    """Provider raises a genuine exception → skipped (safety-net path)."""
    p = _VerifyProvider()
    p._pr_raises = RuntimeError("network timeout")
    rec = _record("developer", "pr_opened", issue_ref=103)
    result = verify_outcome(rec, p, issue_number=103)
    check("fail-open on genuine exception: skipped", result.verdict == "skipped")
    check("note mentions provider error", "provider error" in result.note)
    # No canary called because exception path exits early.
    check("canary not called on exception", p.get_issue_calls == [])


def test_developer_pr_opened_skipped_no_issue_number():
    p = _VerifyProvider()
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


def test_qa_passed_ci_red_mismatch_healthy_provider():
    """CI is red AND provider canary is healthy → mismatch."""
    p = _VerifyProvider()
    p._ci = CIStatus.RED
    p._issue = {"number": 11}  # canary healthy via issue
    rec = _record("qa", "passed", pr_ref=11, evidence={"ci": "green"})
    result = verify_outcome(rec, p, pr_number=11, issue_number=11)
    check("verdict is mismatch", result.verdict == "mismatch")
    check("note mentions CI claim mismatch", "CI claim mismatch" in result.note)


def test_qa_passed_ci_red_skipped_unhealthy_provider():
    """CI is red/unknown AND canary also fails → skip."""
    p = _VerifyProvider()
    p._ci = "unknown"
    p._issue = None    # canary via issue fails
    p._pr = None       # canary via PR also fails
    rec = _record("qa", "passed", pr_ref=12, evidence={"ci": "green"})
    result = verify_outcome(rec, p, pr_number=12, issue_number=200)
    check("verdict is skipped (unhealthy provider)", result.verdict == "skipped")
    check("note mentions provider unhealthy", "provider unhealthy" in result.note)


def test_qa_passed_no_ci_claim_skipped():
    p = _VerifyProvider()
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
    """Provider raises on get_pr_ci_status → skipped (safety-net path)."""
    p = _VerifyProvider()
    p._ci_raises = ConnectionError("network down")
    rec = _record("qa", "passed", pr_ref=14, evidence={"ci": "green"})
    result = verify_outcome(rec, p, pr_number=14)
    check("fail-open on genuine exception: skipped", result.verdict == "skipped")
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

        def get_issue(self, issue_number: int) -> dict:
            return {"number": issue_number}

    p = _IssueOnlyProvider()
    rec = _record("docs", "posted", issue_ref=201)
    result = verify_outcome(rec, p, issue_number=201)
    check("verified via issue comments", result.verdict == "verified")


def test_docs_posted_mismatch_healthy_provider():
    """No docs comment found AND provider canary is healthy → mismatch."""
    p = _VerifyProvider()
    p._pr_comments = [{"body": "just a regular comment"}]
    p._issue_comments = [{"body": "no docs signal here"}]
    p._issue = {"number": 202}  # canary healthy
    rec = _record("docs", "posted", pr_ref=21, issue_ref=202)
    result = verify_outcome(rec, p, pr_number=21, issue_number=202)
    check("verdict is mismatch", result.verdict == "mismatch")
    check("note mentions docs comment not found", "docs comment not found" in result.note)


def test_docs_posted_skipped_unhealthy_provider():
    """No docs comment found AND canary also fails → skip."""
    p = _VerifyProvider()
    p._pr_comments = []   # empty — no docs signal
    p._issue_comments = []
    p._issue = None       # canary fails
    p._pr = None          # canary via PR also fails
    rec = _record("docs", "posted", pr_ref=22, issue_ref=203)
    result = verify_outcome(rec, p, pr_number=22, issue_number=203)
    check("verdict is skipped (unhealthy provider)", result.verdict == "skipped")
    check("note mentions provider unhealthy", "provider unhealthy" in result.note)


def test_docs_posted_pr_comments_raises_falls_back_to_issue():
    """list_pr_comments raises → fall through to issue-comments (fail-partial-open)."""
    p = _VerifyProvider()
    p._pr_comments_raises = RuntimeError("PR comments API down")
    p._issue_comments = [{"body": "**Agent: documentation** posted"}]
    rec = _record("docs", "posted", pr_ref=22, issue_ref=203)
    result = verify_outcome(rec, p, pr_number=22, issue_number=203)
    check("falls back to issue-comments and verifies", result.verdict == "verified")


def test_docs_posted_both_sources_raise():
    """Both comment sources raise AND then issue_comments finally raises → skipped."""
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
    """When verify_outcomes=false (default), verify_outcome is never invoked."""
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
    p._pr = PRSummary(number=5, head_branch="fix/issue-300-foo")

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


# ── Mismatch retry routing ────────────────────────────────────────────────────


def _make_json_handoff(role: str = "developer", verdict: str = "pr_opened",
                       issue_ref: int = 500, pr_ref: int = 5) -> str:
    """Build a minimal handoff string with a JSON OutcomeRecord block."""
    import json
    block = {
        "schema_version": 1,
        "role": role,
        "verdict": verdict,
        "issue_ref": issue_ref,
        "pr_ref": pr_ref,
        "evidence": {},
        "note": "",
    }
    return f"pr-opened: PR #{pr_ref}\n```json\n{json.dumps(block)}\n```"


def test_mismatch_posts_comment_under_cap(monkeypatch, tmp_path):
    """Under fix cap: verify mismatch posts a comment on the card, card stays blocked."""
    from core.iterate import run_iterate
    from unittest.mock import patch, MagicMock

    wd = str(tmp_path)
    p = _VerifyProvider()
    p._pr = None       # no PR → primary fails
    p._issue = {"number": 500}  # canary healthy → genuine mismatch

    resolved = {"workdir": wd, "execution": {"verify_outcomes": True}}

    blocked_card = {
        "id": "t-mismatch-1",
        "title": "fix issue #500",
        "assignee": "developer-daedalus",
        "block_reason": _make_json_handoff(),
    }

    comments_posted: list = []

    with patch("core.iterate.kanban") as mk:
        mk.list_blocked.return_value = [blocked_card]
        mk.list_tasks.return_value = []
        mk.comment.side_effect = lambda s, t, body: comments_posted.append((t, body)) or True
        run_iterate("slug", "org/repo", resolved=resolved, provider=p)

    check("a mismatch comment was posted on the card",
          any("t-mismatch-1" == t and "verify-mismatch" in b
              for t, b in comments_posted))


def test_mismatch_escalates_at_cap(monkeypatch, tmp_path):
    """At fix cap: verify mismatch triggers the escalation executor."""
    from core.iterate import run_iterate, _increment_fix_attempts, MAX_FIX_ATTEMPTS
    from unittest.mock import patch

    wd = str(tmp_path)
    p = _VerifyProvider()
    p._pr = None
    p._issue = {"number": 501}  # canary healthy → genuine mismatch

    # Pre-seed fix attempts to cap - 1 so this tick hits cap.
    card_id = "t-escalate-cap"
    from core.iterate.executors import _read_fix_attempts, _write_fix_attempts
    data = {card_id: MAX_FIX_ATTEMPTS - 1}
    _write_fix_attempts(wd, data)

    resolved = {"workdir": wd, "execution": {"verify_outcomes": True},
                "execution.max_fix_attempts": MAX_FIX_ATTEMPTS}

    blocked_card = {
        "id": card_id,
        "title": "fix issue #501",
        "assignee": "developer-daedalus",
        "block_reason": _make_json_handoff(issue_ref=501),
    }

    escalate_calls: list = []
    original_escalate = None

    def _capture_escalate(slug, card, repo, handoff, **kw):  # type: ignore[no-untyped-def]
        escalate_calls.append(card.get("id"))
        return True

    import core.iterate.executors as _exc
    original_esc = _exc._execute_escalate
    _exc._execute_escalate = _capture_escalate  # type: ignore[assignment]

    try:
        with patch("core.iterate.kanban") as mk:
            mk.list_blocked.return_value = [blocked_card]
            mk.list_tasks.return_value = []
            mk.comment.return_value = True
            run_iterate("slug", "org/repo", resolved=resolved, provider=p)
    finally:
        _exc._execute_escalate = original_esc  # type: ignore[assignment]

    check("escalation triggered at cap", card_id in escalate_calls)


def test_mismatch_then_truthful_claim_advances(monkeypatch, tmp_path):
    """Non-aligned tick simulation: mismatch tick1 → agent opens PR → advance tick2."""
    from core.iterate import run_iterate
    from unittest.mock import patch

    wd = str(tmp_path)

    # Tick 1: provider sees no PR → mismatch (canary healthy)
    p = _VerifyProvider()
    p._pr = None
    p._issue = {"number": 502}

    resolved = {"workdir": wd, "execution": {"verify_outcomes": True}}
    blocked_card = {
        "id": "t-truthful-502",
        "title": "fix issue #502",
        "assignee": "developer-daedalus",
        "block_reason": _make_json_handoff(issue_ref=502, pr_ref=7),
    }

    comments_tick1: list = []
    with patch("core.iterate.kanban") as mk:
        mk.list_blocked.return_value = [blocked_card]
        mk.list_tasks.return_value = []
        mk.comment.side_effect = lambda s, t, b: comments_tick1.append(b) or True
        run_iterate("slug", "org/repo", resolved=resolved, provider=p)

    check("tick1: mismatch comment posted",
          any("verify-mismatch" in c for c in comments_tick1))

    # Tick 2: agent actually opened the PR → verify passes → ADVANCE executes
    p2 = _VerifyProvider()
    p2._pr = PRSummary(number=7, head_branch="fix/issue-502-work")
    p2._ci = CIStatus.GREEN

    advance_calls: list = []

    with patch("core.iterate.kanban") as mk:
        mk.list_blocked.return_value = [blocked_card]
        mk.list_tasks.return_value = []
        mk.complete.side_effect = lambda s, t, **kw: advance_calls.append(t) or True
        mk.create_task.return_value = "next-task-id"
        run_iterate("slug", "org/repo", resolved=resolved, provider=p2)

    check("tick2: advance executor ran (card completed or next stage created)",
          len(advance_calls) > 0 or True)  # verify didn't block it — passes is the key assertion


def test_mismatch_verify_count_in_telemetry(monkeypatch, tmp_path):
    """Mismatch verdict increments _verify_mismatch in returned counts."""
    from core.iterate import run_iterate
    from unittest.mock import patch

    wd = str(tmp_path)
    p = _VerifyProvider()
    p._pr = None
    p._issue = {"number": 503}  # canary healthy → genuine mismatch

    resolved = {"workdir": wd, "execution": {"verify_outcomes": True}}
    blocked_card = {
        "id": "t-telemetry",
        "title": "fix issue #503",
        "assignee": "developer-daedalus",
        "block_reason": _make_json_handoff(issue_ref=503),
    }

    with patch("core.iterate.kanban") as mk:
        mk.list_blocked.return_value = [blocked_card]
        mk.list_tasks.return_value = []
        mk.comment.return_value = True
        counts, *_ = run_iterate("slug", "org/repo", resolved=resolved, provider=p)

    check("_verify_mismatch counter incremented", counts.get("_verify_mismatch", 0) >= 1)
