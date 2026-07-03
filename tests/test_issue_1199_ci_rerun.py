"""Tests for issue #1199 — auto-merge sweep re-runs failed CI (bounded), then escalates.

A pipeline-complete PR (docs card done, issue open, open ``fix/issue-<n>`` PR) whose
required CI is genuinely RED used to sit clean-but-unmerged forever — nothing re-ran
CI. ``sweep_deferred_merges`` now bounded-retries the failed CI run (N=CI_RERUN_MAX per
head SHA) and, once the budget is spent and CI is still red, escalates with the failing
run URL instead of looping. Idempotency is anchored on per-SHA marker comments posted
to the PR, so a same-tick re-invocation never exceeds the budget.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import iterate  # noqa: E402
from core.providers.base import CIStatus  # noqa: E402
from core.providers.github import GitHubProvider  # noqa: E402

GREEN = CIStatus.GREEN
RED = CIStatus.RED
PENDING = CIStatus.PENDING
UNKNOWN = CIStatus.UNKNOWN
SHA = "deadbeef00"


def _comment(body):
    return SimpleNamespace(body=body)


class _FakeProvider:
    """Provider stub whose PR comments grow as markers are posted, so the same
    idempotency source of truth (the PR) is exercised across calls/ticks."""

    supports_ci_status = True
    supports_ci_rerun = True

    def __init__(
        self, *, sha=SHA, rerun_ok=True, run_url="https://ci/run/1", comments=None
    ):
        self._sha = sha
        self._rerun_ok = rerun_ok
        self._run_url = run_url
        self._comments = list(comments or [])
        self.rerun_calls = 0
        self.posted = []

    def get_pr_head_sha(self, pr):
        return self._sha

    def list_pr_comments(self, pr):
        return list(self._comments)

    def rerun_failed_ci(self, pr):
        self.rerun_calls += 1
        return self._rerun_ok

    def failed_ci_run_url(self, pr):
        return self._run_url

    def post_pr_comment(self, pr, body):
        self.posted.append(body)
        self._comments.append(_comment(body))
        return True


# ── _rerun_or_escalate_red_ci ─────────────────────────────────────────────────


def test_red_ci_issues_rerun_first_attempt():
    p = _FakeProvider()
    action = iterate._rerun_or_escalate_red_ci("slug", 42, 99, p)
    assert action == "rerun"
    assert p.rerun_calls == 1
    # One re-run marker posted for this SHA.
    assert any(f"ci-rerun:{SHA}:1" in b for b in p.posted)


def test_red_ci_escalates_after_budget_exhausted():
    prior = [
        _comment(f"<!-- daedalus:ci-rerun:{SHA}:1 -->"),
        _comment(f"<!-- daedalus:ci-rerun:{SHA}:2 -->"),
    ]
    p = _FakeProvider(comments=prior)
    action = iterate._rerun_or_escalate_red_ci("slug", 42, 99, p)
    assert action == "escalated"
    assert p.rerun_calls == 0  # budget spent — never re-run again
    assert any("ci-escalated" in b and "https://ci/run/1" in b for b in p.posted)


def test_already_escalated_is_a_noop_no_loop():
    prior = [_comment(f"<!-- daedalus:ci-escalated:{SHA} -->")]
    p = _FakeProvider(comments=prior)
    action = iterate._rerun_or_escalate_red_ci("slug", 42, 99, p)
    assert action == ""
    assert p.rerun_calls == 0
    assert p.posted == []  # no duplicate escalation


def test_rerun_budget_not_exceeded_across_same_tick_reinvocation():
    # Two consecutive calls (idempotent re-invocation): attempt 1, attempt 2,
    # then escalate — never a third re-run.
    p = _FakeProvider()
    assert iterate._rerun_or_escalate_red_ci("slug", 42, 99, p) == "rerun"
    assert iterate._rerun_or_escalate_red_ci("slug", 42, 99, p) == "rerun"
    assert iterate._rerun_or_escalate_red_ci("slug", 42, 99, p) == "escalated"
    assert iterate._rerun_or_escalate_red_ci("slug", 42, 99, p) == ""
    assert p.rerun_calls == iterate.CI_RERUN_MAX == 2


def test_failed_rerun_does_not_burn_attempt():
    # rerun_failed_ci returns False (no failed run / API error): no marker posted,
    # so the next tick retries the same attempt number.
    p = _FakeProvider(rerun_ok=False)
    action = iterate._rerun_or_escalate_red_ci("slug", 42, 99, p)
    assert action == ""
    assert p.posted == []


def test_no_op_without_provider_ci_rerun_support():
    p = _FakeProvider()
    p.supports_ci_rerun = False
    assert iterate._rerun_or_escalate_red_ci("slug", 42, 99, p) == ""
    assert p.rerun_calls == 0


def test_dry_run_issues_no_side_effects():
    p = _FakeProvider()
    action = iterate._rerun_or_escalate_red_ci("slug", 42, 99, p, dry_run=True)
    assert action == "rerun"
    assert p.rerun_calls == 0
    assert p.posted == []


# ── sweep_deferred_merges integration ─────────────────────────────────────────

_RESOLVED = {"execution": {"auto_merge": True, "merge_method": "squash"}}


def _sweep_provider(*, ci, sha=SHA, comments=None, merged=False):
    """MagicMock provider wired for the sweep path (docs card → branch PR → CI)."""
    p = mock.MagicMock()
    p.supports_ci_status = True
    p.supports_ci_rerun = True
    p.find_pr_for_branch.return_value = 99
    p.get_pr_ci_status.return_value = ci
    p.is_pr_merged.return_value = merged
    p.is_issue_open.return_value = True
    p.has_label.return_value = False
    p.merge_pr.return_value = True
    p.get_pr_head_sha.return_value = sha
    p.list_pr_comments.return_value = list(comments or [])
    p.rerun_failed_ci.return_value = True
    p.failed_ci_run_url.return_value = "https://ci/run/1"
    return p


def _docs_task(issue_n=42):
    return [
        {
            "assignee": "documentation-daedalus",
            "title": f"#{issue_n} Docs: fix the thing",
            "status": "done",
        }
    ]


def _all_gates_pass():
    return mock.patch.multiple(
        iterate,
        _qa_passed_for_issue=mock.DEFAULT,
        _reviewer_passed_for_issue=mock.DEFAULT,
        _security_passed_for_issue=mock.DEFAULT,
    )


def test_sweep_reruns_ci_on_red_pipeline_complete_pr():
    p = _sweep_provider(ci=RED)
    with (
        mock.patch.object(iterate.kanban, "list_tasks", return_value=_docs_task()),
        _all_gates_pass() as m,
    ):
        for k in m:
            m[k].return_value = True
        merged = iterate.sweep_deferred_merges("slug", "owner/repo", p, _RESOLVED)
    assert merged == []  # not merged this tick
    p.merge_pr.assert_not_called()
    p.rerun_failed_ci.assert_called_once_with(99)


def test_sweep_merges_when_ci_green_after_rerun():
    # The tick after a re-run turns CI green → existing merge path takes over.
    p = _sweep_provider(ci=GREEN)
    with (
        mock.patch.object(iterate.kanban, "list_tasks", return_value=_docs_task()),
        _all_gates_pass() as m,
    ):
        for k in m:
            m[k].return_value = True
        merged = iterate.sweep_deferred_merges("slug", "owner/repo", p, _RESOLVED)
    assert merged == [99]
    p.merge_pr.assert_called_once_with(99, merge_method="squash")
    p.rerun_failed_ci.assert_not_called()


def test_sweep_escalates_when_red_after_budget():
    prior = [
        _comment(f"<!-- daedalus:ci-rerun:{SHA}:1 -->"),
        _comment(f"<!-- daedalus:ci-rerun:{SHA}:2 -->"),
    ]
    p = _sweep_provider(ci=RED, comments=prior)
    with (
        mock.patch.object(iterate.kanban, "list_tasks", return_value=_docs_task()),
        _all_gates_pass() as m,
    ):
        for k in m:
            m[k].return_value = True
        merged = iterate.sweep_deferred_merges("slug", "owner/repo", p, _RESOLVED)
    assert merged == []
    p.rerun_failed_ci.assert_not_called()
    assert any("ci-escalated" in c.args[1] for c in p.post_pr_comment.call_args_list)


def test_sweep_does_not_rerun_on_pending_or_unknown_ci():
    for ci in (PENDING, UNKNOWN):
        p = _sweep_provider(ci=ci)
        with (
            mock.patch.object(iterate.kanban, "list_tasks", return_value=_docs_task()),
            _all_gates_pass() as m,
        ):
            for k in m:
                m[k].return_value = True
            merged = iterate.sweep_deferred_merges("slug", "owner/repo", p, _RESOLVED)
        assert merged == []
        p.rerun_failed_ci.assert_not_called()


# ── GitHub provider mechanics ─────────────────────────────────────────────────


def _gh_provider():
    with mock.patch.dict("os.environ", {"GITHUB_TOKEN": "tok"}, clear=False):
        p = GitHubProvider({"repo": "octo/repo"})
    p._http = mock.MagicMock()
    return p


def test_github_supports_ci_rerun_flag():
    assert GitHubProvider.supports_ci_rerun is True


def test_github_get_pr_head_sha():
    p = _gh_provider()
    p._http.get_json.return_value = {"head": {"sha": "cafef00d"}}
    assert p.get_pr_head_sha(99) == "cafef00d"


def test_github_rerun_failed_ci_posts_rerun_failed_jobs():
    p = _gh_provider()

    def _get_json(path, **kw):
        if path.endswith("/pulls/99"):
            return {"head": {"sha": SHA}}
        if "/actions/runs" in path:
            return {
                "workflow_runs": [
                    {
                        "id": 7,
                        "conclusion": "success",
                        "html_url": "u-ok",
                        "created_at": "2026-01-01T00:00:00Z",
                    },
                    {
                        "id": 8,
                        "conclusion": "failure",
                        "html_url": "u-fail",
                        "created_at": "2026-01-02T00:00:00Z",
                    },
                ]
            }
        return None

    p._http.get_json.side_effect = _get_json
    assert p.rerun_failed_ci(99) is True
    path, body = p._http.post_json.call_args[0]
    assert path == "/repos/octo/repo/actions/runs/8/rerun-failed-jobs"


def test_github_rerun_failed_ci_noop_when_no_failed_run():
    p = _gh_provider()

    def _get_json(path, **kw):
        if path.endswith("/pulls/99"):
            return {"head": {"sha": SHA}}
        if "/actions/runs" in path:
            return {"workflow_runs": [{"id": 7, "conclusion": "success"}]}
        return None

    p._http.get_json.side_effect = _get_json
    assert p.rerun_failed_ci(99) is False
    p._http.post_json.assert_not_called()


def test_github_failed_ci_run_url_returns_latest_failed():
    p = _gh_provider()

    def _get_json(path, **kw):
        if path.endswith("/pulls/99"):
            return {"head": {"sha": SHA}}
        if "/actions/runs" in path:
            return {
                "workflow_runs": [
                    {
                        "id": 8,
                        "conclusion": "failure",
                        "html_url": "u-fail",
                        "created_at": "2026-01-02T00:00:00Z",
                    },
                ]
            }
        return None

    p._http.get_json.side_effect = _get_json
    assert p.failed_ci_run_url(99) == "u-fail"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if failures else 0)
