"""Regression test for issue #961 — per-issue dedup of validator-retry API calls.

`_check_confirmed_validators` iterates every done validator kanban task. For a
task with an empty/unrecognized summary it fetches the issue (`get_issue`, via
`_fetch_issue_with_retry`) and the issue comments (`get_issue_comments`, via
`_validator_github_comment_outcome`). Both calls depend only on the issue
number, so when a single issue has N retry-round validator tasks the dispatcher
used to burn N×2 GitHub API calls for that one issue every tick — exhausting
rate limits before reaching Ready issues.

The fix memoizes both calls per issue number for the duration of one scan, so
each unique issue is fetched at most once regardless of how many tasks share it.

Run under pytest, or standalone: ``python tests/test_dispatch_validator_perf_961.py``.
"""

from __future__ import annotations

import sys
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from conftest import FakeProvider, _load_dispatch  # noqa: E402
from core.providers.base import IssueSummary  # noqa: E402


def _issue_summary(number: int, title: str = "feature") -> IssueSummary:
    return IssueSummary(number=number, title=title, body="", labels=[], state="open")


def _empty_validator_tasks(issue_number: int, count: int) -> list:
    """N done validator cards on one issue, all with empty summaries."""
    return [
        {
            "id": f"t_v{issue_number}_{i}",
            "title": f"#{issue_number} feature",
            "assignee": "validator-daedalus",
            "status": "done",
            "summary": "",
            "last_summary": "",
        }
        for i in range(count)
    ]


def _run_scan(disp, provider, tasks):
    """Drive _check_confirmed_validators over a fixed task list (no kanban writes)."""
    with ExitStack() as stack:
        stack.enter_context(
            mock.patch.object(
                disp.kanban, "list_tasks", lambda slug, status=None: list(tasks)
            )
        )
        # Empty summary -> show_card consulted; return no latest_summary.
        stack.enter_context(
            mock.patch.object(disp.kanban, "show_card", lambda s, t: {})
        )
        # Swallow retry-task creation so the snapshot of done tasks stays fixed.
        stack.enter_context(
            mock.patch.object(disp.kanban, "create_task", lambda *a, **k: "t_new")
        )
        stack.enter_context(
            mock.patch.object(disp.kanban, "comment", lambda *a, **k: True)
        )
        stack.enter_context(mock.patch.object(disp.time, "sleep", lambda _: None))
        return disp._check_confirmed_validators(
            slug="slug",
            repo="owner/repo",
            issues_map={},  # empty -> forces get_issue fallback
            iterations=3,
            workdir="/tmp",
            notify_target="",
            base_branch="dev",
            provider_name="github",
            provider=provider,
        )


def test_single_issue_many_validator_tasks_fetches_issue_once():
    """5 empty-summary validator tasks on one issue -> 1 get_issue + 1 get_issue_comments."""
    disp = _load_dispatch()
    provider = FakeProvider(issues={961: _issue_summary(961)})
    _run_scan(disp, provider, _empty_validator_tasks(961, 5))

    assert provider.get_issue_calls == 1, (
        f"get_issue should fire once per issue, not per task: {provider.get_issue_calls}"
    )
    assert provider.get_issue_comments_calls == 1, (
        f"get_issue_comments should fire once per issue, not per task: "
        f"{provider.get_issue_comments_calls}"
    )


def test_distinct_issues_each_fetched_once():
    """Tasks spanning 3 issues -> exactly 3 get_issue + 3 get_issue_comments."""
    disp = _load_dispatch()
    provider = FakeProvider(issues={n: _issue_summary(n) for n in (961, 962, 963)})
    tasks = (
        _empty_validator_tasks(961, 4)
        + _empty_validator_tasks(962, 3)
        + _empty_validator_tasks(963, 2)
    )
    _run_scan(disp, provider, tasks)

    assert provider.get_issue_calls == 3, provider.get_issue_calls
    assert provider.get_issue_comments_calls == 3, provider.get_issue_comments_calls


if __name__ == "__main__":
    failed = 0
    for fn in (
        test_single_issue_many_validator_tasks_fetches_issue_once,
        test_distinct_issues_each_fetched_once,
    ):
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {fn.__name__}: {exc}")
    sys.exit(1 if failed else 0)
