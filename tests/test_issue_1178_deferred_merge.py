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


def test_sweep_skips_when_no_pr_for_branch():
    p = _provider()
    p.find_pr_for_branch.return_value = None
    tasks = [{"assignee": "documentation-daedalus", "title": "#42 Docs: fix the thing", "status": "done"}]
    with mock.patch.object(iterate.kanban, "list_tasks", return_value=tasks):
        merged = iterate.sweep_deferred_merges("slug", "owner/repo", p, _RESOLVED)
    assert merged == []
    p.merge_pr.assert_not_called()


def test_sweep_uses_deterministic_branch_name():
    p = _provider(pr=99)
    tasks = [{"assignee": "documentation-daedalus", "title": "#1234 Docs: fix the thing", "status": "done"}]
    with mock.patch.object(iterate.kanban, "list_tasks", return_value=tasks), _all_gates_pass() as m:
        for k in m:
            m[k].return_value = True
        iterate.sweep_deferred_merges("slug", "owner/repo", p, _RESOLVED)
    p.find_pr_for_branch.assert_called_with("fix/issue-1234")


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
