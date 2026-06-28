#!/usr/bin/env python3
"""补充测试：retry cap 通知的边界路径和中间 retry 通知逻辑。

覆盖范围：
1. validator 中间 retry 触发 retry-attempt 而非 retry-cap-exhausted
2. PM 中间 retry 触发 retry-attempt 通知
3. retry_count 边界值（恰好等于 max_retries vs 超过）
4. _has_notified_block / _mark_notified_block 直接单元测试
5. 多 issue 并发时 marker 互不干扰
6. validator_profile 参数隔离验证
"""
import importlib.util
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_dispatch():
    """Load scripts/daedalus_dispatch.py as a standalone module."""
    p = ROOT / "scripts" / "daedalus_dispatch.py"
    spec = importlib.util.spec_from_file_location("disp", str(p))
    assert spec and spec.loader, f"Cannot load dispatcher from {p}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _minimal_resolved(*, notifications=None):
    cron = {}
    if notifications is not None:
        cron["notifications"] = notifications
    return {"cron": cron}


@pytest.fixture
def disp():
    return _load_dispatch()


def _default_profile():
    return {"validator": "validator-daedalus", "pm": "project-manager-daedalus"}


# ── 中间 retry 通知逻辑 ─────────────────────────────────────────────────────

def test_validator_intermediate_retry_triggers_retry_attempt_not_cap_exhausted(disp):
    """validator retry_count=1, max_retries=2 → 触发 retry-attempt，不触发 cap-exhausted"""
    fake_tasks = [
        {"id": "t1", "title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done"},
        {"id": "t2", "title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done"},
    ]

    resolved = _minimal_resolved(
        notifications=[
            {"platform": "Slack", "target": "slack:ops", "events": ["retry-cap-exhausted"]},
            {"platform": "Slack", "target": "slack:ops2", "events": ["retry-attempt"]},
        ]
    )

    with mock.patch.object(disp.kanban, "list_tasks", return_value=fake_tasks), \
         mock.patch.object(disp.kanban, "show_card", return_value={"comments": []}), \
         mock.patch.object(disp.kanban, "comment"), \
         mock.patch.object(disp, "_validator_github_comment_outcome", return_value=""), \
         mock.patch.object(disp, "_send_retry_cap_notification") as mock_cap, \
         mock.patch.object(disp, "_send_retry_attempt_notification") as mock_attempt:

        disp._check_confirmed_validators(
            "slug", "owner/repo",
            {42: {"number": 42, "title": "fix bug", "body": ""}},
            3, "/tmp", "", "main", "github",
            provider=None,
            resolved=resolved,
            profiles=_default_profile(),
        )

        # retry_count=2, max_retries=2 → 2 >= 3 为 False，应触发 retry-attempt
        assert mock_cap.call_count == 0, "retry_count=2 < max+1=3，不应触发 cap-exhausted"
        assert mock_attempt.called, "retry_count=2 > 0，应触发 retry-attempt"
        kw = mock_attempt.call_args.kwargs
        assert kw["role"] == "validator"
        assert kw["issue_number"] == 42
        assert kw["retry_count"] == 2
        assert kw["max_retries"] == 2


def test_pm_intermediate_retry_triggers_retry_attempt_not_cap_exhausted(disp):
    """PM stale_count=1, max_retries=3 → 触发 retry-attempt，不触发 cap-exhausted"""
    fake_tasks = [
        {"id": "t_v1", "title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done",
         "summary": "CONFIRMED: valid issue"},
    ]

    def fake_pm_task_state(slug, issue_nr, pm_profile):
        return ("stale", 1)  # stale_count=1 < _MAX_PM_RETRIES=3

    resolved = _minimal_resolved(
        notifications=[
            {"platform": "Slack", "target": "slack:ops", "events": ["retry-cap-exhausted"]},
            {"platform": "Slack", "target": "slack:ops2", "events": ["retry-attempt"]},
        ]
    )

    with mock.patch.object(disp.kanban, "list_tasks", return_value=fake_tasks), \
         mock.patch.object(disp.kanban, "show_card", return_value={"comments": []}), \
         mock.patch.object(disp.kanban, "comment"), \
         mock.patch.object(disp, "_validator_github_comment_outcome", return_value=""), \
         mock.patch.object(disp, "_pm_task_state", side_effect=fake_pm_task_state), \
         mock.patch.object(disp, "_send_retry_cap_notification") as mock_cap, \
         mock.patch.object(disp, "_send_retry_attempt_notification") as mock_attempt:

        disp._check_confirmed_validators(
            "slug", "owner/repo",
            {42: {"number": 42, "title": "fix bug", "body": ""}},
            3, "/tmp", "", "main", "github",
            provider=None,
            resolved=resolved,
            profiles=_default_profile(),
        )

        assert mock_cap.call_count == 0, "stale_count=1 < max=3，不应触发 cap-exhausted"
        assert mock_attempt.called, "stale_count=1 > 0，应触发 retry-attempt"
        kw = mock_attempt.call_args.kwargs
        assert kw["role"] == "pm"
        assert kw["issue_number"] == 42
        assert kw["retry_count"] == 1
        assert kw["max_retries"] == 3


# ── 边界值测试 ─────────────────────────────────────────────────────────────────

def test_validator_retry_count_exactly_at_boundary(disp):
    """validator retry_count=2, max_retries=2 → retry_count < max+1=3，不触发 cap-exhausted"""
    fake_tasks = [
        {"id": "t1", "title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done"},
        {"id": "t2", "title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done"},
    ]

    resolved = _minimal_resolved()

    with mock.patch.object(disp.kanban, "list_tasks", return_value=fake_tasks), \
         mock.patch.object(disp.kanban, "show_card", return_value={"comments": []}), \
         mock.patch.object(disp.kanban, "comment"), \
         mock.patch.object(disp, "_validator_github_comment_outcome", return_value=""), \
         mock.patch.object(disp, "_send_retry_cap_notification") as mock_notify:

        disp._check_confirmed_validators(
            "slug", "owner/repo",
            {42: {"number": 42, "title": "fix bug", "body": ""}},
            3, "/tmp", "", "main", "github",
            provider=None,
            resolved=resolved,
            profiles=_default_profile(),
        )

        # 2 < 3，不满足 retry_count >= max_retries + 1
        assert mock_notify.call_count == 0, "retry_count=2 < max+1=3，不触发通知"


def test_validator_retry_count_one_over_boundary(disp):
    """validator retry_count=3, max_retries=2 → retry_count >= max+1=3，触发 cap-exhausted。

    show_card 返回空 comments → _has_notified_block 始终为 False，循环里每个 task 都会触发通知。
    实际使用中通过 _mark_notified_block 保证只通知一次；这里只验证“边界值=3 时会触发”。
    """
    fake_tasks = [
        {"id": "t1", "title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done"},
        {"id": "t2", "title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done"},
        {"id": "t3", "title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done"},
    ]

    resolved = _minimal_resolved()

    with mock.patch.object(disp.kanban, "list_tasks", return_value=fake_tasks), \
         mock.patch.object(disp.kanban, "show_card", return_value={"comments": []}), \
         mock.patch.object(disp.kanban, "comment"), \
         mock.patch.object(disp, "_validator_github_comment_outcome", return_value=""), \
         mock.patch.object(disp, "_send_retry_cap_notification") as mock_notify:

        disp._check_confirmed_validators(
            "slug", "owner/repo",
            {42: {"number": 42, "title": "fix bug", "body": ""}},
            3, "/tmp", "", "main", "github",
            provider=None,
            resolved=resolved,
            profiles=_default_profile(),
        )

        # 3 >= 3，满足 retry_count >= max_retries + 1，通知至少被调用一次
        assert mock_notify.call_count >= 1, "retry_count=3 >= max+1=3，必须触发通知"
        # 第一次调用的参数正确
        kw = mock_notify.call_args_list[0].kwargs
        assert kw["role"] == "validator"
        assert kw["issue_number"] == 42
        assert kw["retry_count"] == 3
        assert kw["max_retries"] == 2


def test_pm_stale_count_exactly_at_max(disp):
    """PM stale_count=3, max_retries=3 → stale_count >= max_retries，触发 cap-exhausted"""
    fake_tasks = [
        {"id": "t_v1", "title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done",
         "summary": "CONFIRMED: valid issue"},
    ]

    def fake_pm_task_state(slug, issue_nr, pm_profile):
        return ("stale", 3)  # stale_count=3 >= _MAX_PM_RETRIES=3

    resolved = _minimal_resolved()

    with mock.patch.object(disp.kanban, "list_tasks", return_value=fake_tasks), \
         mock.patch.object(disp.kanban, "show_card", return_value={"comments": []}), \
         mock.patch.object(disp.kanban, "comment"), \
         mock.patch.object(disp, "_validator_github_comment_outcome", return_value=""), \
         mock.patch.object(disp, "_pm_task_state", side_effect=fake_pm_task_state), \
         mock.patch.object(disp, "_send_retry_cap_notification") as mock_notify:

        disp._check_confirmed_validators(
            "slug", "owner/repo",
            {42: {"number": 42, "title": "fix bug", "body": ""}},
            3, "/tmp", "", "main", "github",
            provider=None,
            resolved=resolved,
            profiles=_default_profile(),
        )

        assert mock_notify.call_count == 1, "stale_count=3 >= max=3，触发通知"


# ── _has_notified_block 直接单元测试 ──────────────────────────────────────────

def test_has_notified_block_returns_true_when_marker_present(disp):
    """_has_notified_block returns True when marker is found in task comments"""
    fake_tasks = [
        {"id": "t1", "title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done"},
    ]
    fake_card = {
        "comments": [
            {"body": "<!-- daedalus:retry-cap-notified -->"},
            {"body": "other comment"},
        ]
    }

    with mock.patch.object(disp.kanban, "list_tasks", return_value=fake_tasks), \
         mock.patch.object(disp.kanban, "show_card", return_value=fake_card):
        result = disp._has_notified_block(
            "slug", 42,
            validator_profile="validator-daedalus",
            marker=disp._RETRY_CAP_MARKER,
        )
        assert result is True, "marker found in comments, should return True"


def test_has_notified_block_returns_false_when_marker_absent(disp):
    """_has_notified_block returns False when marker is not found"""
    fake_tasks = [
        {"id": "t1", "title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done"},
    ]
    fake_card = {
        "comments": [
            {"body": "some other comment"},
        ]
    }

    with mock.patch.object(disp.kanban, "list_tasks", return_value=fake_tasks), \
         mock.patch.object(disp.kanban, "show_card", return_value=fake_card):
        result = disp._has_notified_block(
            "slug", 42,
            validator_profile="validator-daedalus",
            marker=disp._RETRY_CAP_MARKER,
        )
        assert result is False, "marker not found, should return False"


def test_has_notified_block_returns_false_no_comments(disp):
    """_has_notified_block returns False when task has no comments"""
    fake_tasks = [
        {"id": "t1", "title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done"},
    ]
    fake_card = {"comments": []}

    with mock.patch.object(disp.kanban, "list_tasks", return_value=fake_tasks), \
         mock.patch.object(disp.kanban, "show_card", return_value=fake_card):
        result = disp._has_notified_block(
            "slug", 42,
            validator_profile="validator-daedalus",
            marker=disp._RETRY_CAP_MARKER,
        )
        assert result is False, "empty comments, should return False"


def test_has_notified_block_filters_by_validator_profile(disp):
    """_has_notified_block only checks tasks matching the validator_profile"""
    fake_tasks = [
        {"id": "t1", "title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done"},
        {"id": "t2", "title": "#42 fix bug", "assignee": "different-validator", "status": "done"},
    ]
    fake_card_t1 = {"comments": [{"body": "<!-- daedalus:retry-cap-notified -->"}]}
    fake_card_t2 = {"comments": [{"body": "<!-- daedalus:retry-cap-notified -->"}]}

    def fake_show_card(slug, tid):
        if tid == "t1":
            return fake_card_t1
        return fake_card_t2

    with mock.patch.object(disp.kanban, "list_tasks", return_value=fake_tasks), \
         mock.patch.object(disp.kanban, "show_card", side_effect=fake_show_card):
        result = disp._has_notified_block(
            "slug", 42,
            validator_profile="different-validator",
            marker=disp._RETRY_CAP_MARKER,
        )
        assert result is True, "marker found in task with matching validator_profile"


# ── _mark_notified_block 直接单元测试 ─────────────────────────────────────────

def test_mark_notified_block_stamps_marker(disp):
    """_mark_notified_block calls kanban.comment with the marker"""
    fake_tasks = [
        {"id": "t1", "title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done"},
    ]

    comments_store = []

    def fake_comment(slug, tid, body):
        comments_store.append({"slug": slug, "tid": tid, "body": body})

    with mock.patch.object(disp.kanban, "list_tasks", return_value=fake_tasks), \
         mock.patch.object(disp.kanban, "show_card", return_value={"comments": []}), \
         mock.patch.object(disp.kanban, "comment", side_effect=fake_comment):
        disp._mark_notified_block(
            "slug", 42,
            validator_profile="validator-daedalus",
            marker=disp._RETRY_CAP_MARKER,
        )

        assert len(comments_store) == 1, "kanban.comment called once"
        assert comments_store[0]["tid"] == "t1"
        assert disp._RETRY_CAP_MARKER in comments_store[0]["body"]


def test_mark_notified_block_only_marks_matching_profile(disp):
    """_mark_notified_block only marks tasks matching the validator_profile"""
    fake_tasks = [
        {"id": "t1", "title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done"},
        {"id": "t2", "title": "#42 fix bug", "assignee": "different-validator", "status": "done"},
    ]

    comments_store = []

    def fake_comment(slug, tid, body):
        comments_store.append({"slug": slug, "tid": tid, "body": body})

    with mock.patch.object(disp.kanban, "list_tasks", return_value=fake_tasks), \
         mock.patch.object(disp.kanban, "show_card", return_value={"comments": []}), \
         mock.patch.object(disp.kanban, "comment", side_effect=fake_comment):
        disp._mark_notified_block(
            "slug", 42,
            validator_profile="different-validator",
            marker=disp._RETRY_CAP_MARKER,
        )

        assert len(comments_store) == 1
        assert comments_store[0]["tid"] == "t2", "only task with matching profile marked"


# ── 多 issue 并发隔离 ─────────────────────────────────────────────────────────

def test_multiple_issues_marker_isolation(disp):
    """marker for issue #42 does not affect issue #43"""
    fake_tasks = [
        {"id": "t1", "title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done"},
        {"id": "t2", "title": "#43 fix another bug", "assignee": "validator-daedalus", "status": "done"},
        {"id": "t3", "title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done"},
        {"id": "t4", "title": "#43 fix another bug", "assignee": "validator-daedalus", "status": "done"},
        {"id": "t5", "title": "#42 fix bug", "assignee": "validator-daedalus", "status": "done"},
        {"id": "t6", "title": "#43 fix another bug", "assignee": "validator-daedalus", "status": "done"},
    ]

    # Per-task comments store: #42 already marked, #43 is not.
    per_task_comments = {
        "t1": [{"body": disp._RETRY_CAP_MARKER}],   # #42 — marker stamped
        "t3": [],
        "t5": [],
        "t2": [],    # #43 — no marker yet
        "t4": [],
        "t6": [],
    }

    def fake_show_card(slug, tid):
        return {"comments": per_task_comments.get(tid, [])}

    resolved = _minimal_resolved(
        notifications=[{"platform": "Slack", "target": "slack:ops", "events": ["retry-cap-exhausted"]}]
    )

    with mock.patch.object(disp.kanban, "list_tasks", return_value=fake_tasks), \
         mock.patch.object(disp.kanban, "show_card", side_effect=fake_show_card), \
         mock.patch.object(disp.kanban, "comment"), \
         mock.patch.object(disp, "_validator_github_comment_outcome", return_value=""), \
         mock.patch.object(disp, "_send_retry_cap_notification") as mock_notify:

        disp._check_confirmed_validators(
            "slug", "owner/repo",
            {42: {"number": 42, "title": "fix bug", "body": ""},
             43: {"number": 43, "title": "fix another bug", "body": ""}},
            3, "/tmp", "", "main", "github",
            provider=None,
            resolved=resolved,
            profiles=_default_profile(),
        )

        # Issue #42 has marker already → skipped
        # Issue #43 has no marker → notification fires (once, on its first iteration)
        assert mock_notify.call_count >= 1, "#43 should trigger notification"
        # All calls must be for issue 43, never 42
        for call in mock_notify.call_args_list:
            kw = call.kwargs
            assert kw["issue_number"] == 43, f"expected #43, got #{kw['issue_number']}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
