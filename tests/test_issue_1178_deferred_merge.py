"""Tests for issue #1178 — auto-merge is no longer one-shot.

The docs-completion merge only fired once, at the instant the docs card completed.
If the PR was un-mergeable then (CI pending, or a momentary conflict), the merge was
lost forever. `sweep_deferred_merges` retries every tick for pipeline-complete PRs,
and `_try_merge_if_gates_pass` returns False (rather than consuming the only attempt)
when a gate fails or merge_pr fails, so a later tick can retry.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import iterate  # noqa: E402

GREEN = iterate.CIStatus.GREEN
PENDING = getattr(iterate.CIStatus, "PENDING", "pending")


def _provider(*, merged=False, ci=GREEN, merge_ok=True, pr=99):
    p = mock.MagicMock()
    p.supports_ci_status = True
    # sweep_deferred_merges prefers find_pr_for_issue (suffix-tolerant); keep
    # find_pr_for_branch aligned for the exact-match fallback path.
    p.find_pr_for_issue.return_value = pr
    p.find_pr_for_branch.return_value = pr
    p.get_pr_ci_status.return_value = ci
    p.is_pr_merged.return_value = merged
    p.has_label.return_value = False
    p.merge_pr.return_value = merge_ok
    return p


def _all_gates_pass():
    return mock.patch.multiple(
        iterate,
        _qa_passed_for_issue=mock.DEFAULT,
        _reviewer_passed_for_issue=mock.DEFAULT,
        _security_passed_for_issue=mock.DEFAULT,
    )


# ── _try_merge_if_gates_pass ──────────────────────────────────────────────────


def test_merges_when_all_gates_pass():
    p = _provider()
    with _all_gates_pass() as m:
        m["_qa_passed_for_issue"].return_value = True
        m["_reviewer_passed_for_issue"].return_value = True
        m["_security_passed_for_issue"].return_value = True
        ok = iterate._try_merge_if_gates_pass(
            "slug", 42, 99, p, merge_method="squash", skip_qa=False, ci_status=GREEN)
    assert ok is True
    p.merge_pr.assert_called_once_with(99, merge_method="squash")


def test_no_merge_when_qa_fails():
    p = _provider()
    with _all_gates_pass() as m:
        m["_qa_passed_for_issue"].return_value = False
        m["_reviewer_passed_for_issue"].return_value = True
        m["_security_passed_for_issue"].return_value = True
        ok = iterate._try_merge_if_gates_pass(
            "slug", 42, 99, p, merge_method="squash", skip_qa=False, ci_status=GREEN)
    assert ok is False
    p.merge_pr.assert_not_called()


def test_no_merge_when_ci_not_green():
    p = _provider(ci=PENDING)
    with _all_gates_pass() as m:
        for k in m:
            m[k].return_value = True
        ok = iterate._try_merge_if_gates_pass(
            "slug", 42, 99, p, merge_method="squash", skip_qa=False, ci_status=PENDING)
    assert ok is False
    p.merge_pr.assert_not_called()


def test_idempotent_skip_when_already_merged():
    p = _provider(merged=True)
    with _all_gates_pass() as m:
        for k in m:
            m[k].return_value = True
        ok = iterate._try_merge_if_gates_pass(
            "slug", 42, 99, p, merge_method="squash", skip_qa=False, ci_status=GREEN)
    assert ok is False
    p.merge_pr.assert_not_called()


def test_failed_merge_is_retryable_not_fatal():
    # merge_pr returns False (e.g. PR momentarily dirty) → helper returns False,
    # no exception, so the next tick retries.
    p = _provider(merge_ok=False)
    with _all_gates_pass() as m:
        for k in m:
            m[k].return_value = True
        ok = iterate._try_merge_if_gates_pass(
            "slug", 42, 99, p, merge_method="squash", skip_qa=False, ci_status=GREEN)
    assert ok is False
    p.merge_pr.assert_called_once()


def test_skip_qa_bypasses_gates():
    p = _provider()
    with _all_gates_pass() as m:
        # gates would fail, but skip_qa bypasses them
        for k in m:
            m[k].return_value = False
        ok = iterate._try_merge_if_gates_pass(
            "slug", 42, 99, p, merge_method="squash", skip_qa=True, ci_status=GREEN)
    assert ok is True
    p.merge_pr.assert_called_once()


# ── sweep_deferred_merges ─────────────────────────────────────────────────────

_RESOLVED = {"execution": {"auto_merge": True, "merge_method": "squash"}}


def test_sweep_merges_done_docs_card_pr():
    p = _provider(pr=99)
    tasks = [{"assignee": "documentation-daedalus", "title": "#42 Docs: fix the thing", "status": "done"}]
    with mock.patch.object(iterate.kanban, "list_tasks", return_value=tasks), _all_gates_pass() as m:
        for k in m:
            m[k].return_value = True
        merged = iterate.sweep_deferred_merges("slug", "owner/repo", p, _RESOLVED)
    assert merged == [99]
    p.merge_pr.assert_called_once_with(99, merge_method="squash")


def test_sweep_noop_when_auto_merge_disabled():
    p = _provider()
    with mock.patch.object(iterate.kanban, "list_tasks", return_value=[]):
        merged = iterate.sweep_deferred_merges(
            "slug", "owner/repo", p, {"execution": {"auto_merge": False}})
    assert merged == []
    p.merge_pr.assert_not_called()


def test_sweep_ignores_non_done_docs_cards():
    p = _provider()
    tasks = [{"assignee": "documentation-daedalus", "title": "#42 Docs: fix the thing", "status": "running"}]
    with mock.patch.object(iterate.kanban, "list_tasks", return_value=tasks):
        merged = iterate.sweep_deferred_merges("slug", "owner/repo", p, _RESOLVED)
    assert merged == []
    p.merge_pr.assert_not_called()


def test_sweep_skips_when_no_pr_for_issue():
    p = _provider()
    p.find_pr_for_issue.return_value = None
    tasks = [{"assignee": "documentation-daedalus", "title": "#42 Docs: fix the thing", "status": "done"}]
    with mock.patch.object(iterate.kanban, "list_tasks", return_value=tasks):
        merged = iterate.sweep_deferred_merges("slug", "owner/repo", p, _RESOLVED)
    assert merged == []
    p.merge_pr.assert_not_called()


def test_sweep_resolves_pr_by_issue_number():
    p = _provider(pr=99)
    tasks = [{"assignee": "documentation-daedalus", "title": "#1234 Docs: fix the thing", "status": "done"}]
    with mock.patch.object(iterate.kanban, "list_tasks", return_value=tasks), _all_gates_pass() as m:
        for k in m:
            m[k].return_value = True
        iterate.sweep_deferred_merges("slug", "owner/repo", p, _RESOLVED)
    # Suffix-tolerant lookup is keyed on the issue number, not a synthesized branch.
    p.find_pr_for_issue.assert_called_with(1234)


def test_sweep_falls_back_to_branch_lookup_without_helper():
    # A provider double lacking find_pr_for_issue (older doubles) still merges via
    # the exact-branch fallback so the sweep degrades gracefully.
    p = _provider(pr=99)
    del p.find_pr_for_issue
    tasks = [{"assignee": "documentation-daedalus", "title": "#42 Docs: fix the thing", "status": "done"}]
    with mock.patch.object(iterate.kanban, "list_tasks", return_value=tasks), _all_gates_pass() as m:
        for k in m:
            m[k].return_value = True
        merged = iterate.sweep_deferred_merges("slug", "owner/repo", p, _RESOLVED)
    assert merged == [99]
    p.find_pr_for_branch.assert_called_with("fix/issue-42")


# ── issue #1226: docs card already archived must still merge ───────────────────


def _split_board(active, archived):
    """list_tasks side_effect: active board vs the status='archived' list.

    Mirrors the real kanban CLI, where the default ``list`` EXCLUDES archived
    cards and only ``--status archived`` returns them.
    """
    def _side_effect(slug, status=None, **_kw):
        return list(archived) if status == "archived" else list(active)
    return _side_effect


def test_sweep_merges_when_docs_card_already_archived():
    # Regression for #1226: completed gate cards archive quickly (#1141), so the
    # terminal documentation card is frequently ARCHIVED by the time this every-tick
    # sweep runs. It was absent from the active board, so the PR was never even
    # considered → stranded open until a manual merge. The sweep must scan the
    # archived list too and treat an archived docs card as a completion signal.
    p = _provider(pr=99)
    archived = [{"assignee": "documentation-daedalus",
                 "title": "#42 Docs: fix the thing", "status": "archived"}]
    with mock.patch.object(iterate.kanban, "list_tasks",
                           side_effect=_split_board([], archived)), _all_gates_pass() as m:
        for k in m:
            m[k].return_value = True
        merged = iterate.sweep_deferred_merges("slug", "owner/repo", p, _RESOLVED)
    assert merged == [99]
    p.merge_pr.assert_called_once_with(99, merge_method="squash")


def test_sweep_does_not_double_consider_docs_card_in_both_lists():
    # An issue appears once even if its docs card shows up in both active and
    # archived snapshots (seen_issues dedup) — no double merge attempt.
    p = _provider(pr=99)
    card = {"assignee": "documentation-daedalus",
            "title": "#42 Docs: fix the thing", "status": "done"}
    with mock.patch.object(iterate.kanban, "list_tasks",
                           side_effect=_split_board([card], [card])), _all_gates_pass() as m:
        for k in m:
            m[k].return_value = True
        merged = iterate.sweep_deferred_merges("slug", "owner/repo", p, _RESOLVED)
    assert merged == [99]
    p.merge_pr.assert_called_once_with(99, merge_method="squash")


# ── find_pr_for_issue: suffix-tolerant branch matching ────────────────────────

from core.providers.base import PRSummary, VCSProvider  # noqa: E402


class _PRList:
    """Duck-typed self exposing only list_prs, so the real base-class
    find_pr_for_issue runs without implementing every abstract method."""

    def __init__(self, *branches):
        self._branches = branches

    def list_prs(self, state="all", limit=50):
        return [PRSummary(number=100 + i, head_branch=b) for i, b in enumerate(self._branches)]

    def find_pr_for_issue(self, issue_number):
        return VCSProvider.find_pr_for_issue(self, issue_number)


def _find(issue_n, *branches):
    return _PRList(*branches).find_pr_for_issue(issue_n)


def test_find_pr_for_issue_matches_exact_branch():
    assert _find(42, "main", "fix/issue-42") == 101


def test_find_pr_for_issue_matches_suffixed_branch():
    # The local-agent path pushes fix/issue-<n>-<slug>; it must still resolve.
    assert _find(42, "main", "fix/issue-42-negate-function") == 101


def test_find_pr_for_issue_rejects_adjacent_issue_numbers():
    # fix/issue-42 must NOT match issue 4 or issue 421 (prefix boundary is '-').
    assert _find(4, "fix/issue-42", "fix/issue-421") is None
    assert _find(42, "fix/issue-421") is None


def test_find_pr_for_issue_none_when_no_open_pr():
    assert _find(42, "main", "feature/other") is None


def test_sweep_merges_pr_with_suffixed_branch():
    # End-to-end through the real base method: a done docs card whose PR lives on
    # a suffixed branch still auto-merges (the F13 regression this fixes).
    class _P(_PRList):
        supports_ci_status = True
        def get_pr_ci_status(self, pr):  # noqa: ANN001
            return GREEN
        def is_pr_merged(self, pr):  # noqa: ANN001
            return False
        def has_label(self, pr, label):  # noqa: ANN001
            return False
        def is_issue_open(self, n):  # noqa: ANN001
            return True
        def merge_pr(self, pr, merge_method="squash"):  # noqa: ANN001
            self.merged = pr
            return True

    p = _P("fix/issue-42-negate-function")
    tasks = [{"assignee": "documentation-daedalus", "title": "#42 Docs: fix", "status": "done"}]
    with mock.patch.object(iterate.kanban, "list_tasks", return_value=tasks), _all_gates_pass() as m:
        for k in m:
            m[k].return_value = True
        merged = iterate.sweep_deferred_merges("slug", "owner/repo", p, _RESOLVED)
    assert merged == [100]
    assert getattr(p, "merged", None) == 100


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
