"""Tests for the planner 'NOT SUITABLE FOR DECOMPOSITION' fallback handler.

When the planner agent completes a card and signals that the parent issue is
not suitable for decomposition (instead of the typical `PLANNING COMPLETE:`
path), the dispatcher must route the issue to a validator task so the parent
issue does not get stuck in-progress with no active child task.

Refs: issue #931 / epic #918.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import _load_dispatch  # noqa: E402

disp = _load_dispatch()


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_planner_task(
    number: int,
    summary: str,
    task_id: str = "t_planner",
) -> dict:
    """Build a minimal DONE planner task dict as returned by kanban.list_tasks."""
    return {
        "id": task_id,
        "title": f"#{number} Epic parent",
        "body": "epic body",
        "assignee": "planner-daedalus",
        "status": "done",
        "summary": summary,
        "idempotency_key": f"planner-{number}",
    }


def _make_issue_obj(number: int = 42, title: str = "Epic title", body: str = "epic body"):
    class _Obj:
        def as_dict(self_):
            return {
                "number": number,
                "title": title,
                "body": body,
                "labels": ["epic"],
                "url": f"https://github.com/o/r/issues/{number}",
            }
    return _Obj()


def _issue_map_entry(n: int):
    """Return a dict mapping issue number n to a minimal issue dict."""
    return {n: {"number": n, "title": "Epic title", "body": "epic body",
                "labels": [], "url": f"https://github.com/o/r/issues/{n}"}}


def _check(slug="slug", *, repo="o/r", issues_map=None, workdir="/tmp/work",
           base_branch="dev", provider_name="github", profiles=None,
           role_skills=None, coding_agent="none", coding_agent_cmd="",
           notify_targets=None, dry_run=False, provider=None,
           done_tasks=(), all_tasks=()):
    """Call the handler with sensible defaults and in-memory kanban doubles."""
    profiles = profiles or disp._DEFAULT_PROFILES
    issues_map = issues_map if issues_map is not None else {}
    show_map = {t["id"]: {**t, "latest_summary": t.get("summary", "")} for t in done_tasks}
    list_map = {None: list(all_tasks), "done": list(done_tasks)}
    created = []

    def _fake_create_task(slug_, title, *, body="", assignee="", idempotency_key="",
                          workspace="", skills=None, **_):
        # Mimic kanban.create_task idempotency semantics: when the same
        # idempotency_key has been seen before, return the prior task id
        # instead of creating a duplicate.
        for prev in created:
            if prev["idempotency_key"] == idempotency_key:
                return prev["id"]
        # Also check all_tasks to simulate persistent kanban state.
        for prev in all_tasks:
            if (prev.get("idempotency_key") or "") == idempotency_key:
                return prev["id"]
        new_id = f"t_new_{len(created)}"
        created.append({
            "id": new_id, "title": title, "assignee": assignee,
            "idempotency_key": idempotency_key, "body": body,
        })
        return new_id

    def _fake_list_tasks(slug_, status=None):
        return list(list_map.get(status, list_map[None]))

    def _fake_show_card(slug_, tid):
        return show_map.get(tid)

    with (
        mock.patch.object(disp.kanban, "list_tasks", side_effect=_fake_list_tasks),
        mock.patch.object(disp.kanban, "show_card", side_effect=_fake_show_card),
        mock.patch.object(disp.kanban, "create_task", side_effect=_fake_create_task),
    ):
        triggered = disp._check_planner_not_suitable(
            slug, repo=repo, issues_map=issues_map, workdir=workdir,
            base_branch=base_branch, provider_name=provider_name,
            profiles=profiles, role_skills=role_skills or {},
            coding_agent=coding_agent, coding_agent_cmd=coding_agent_cmd,
            notify_targets=notify_targets, dry_run=dry_run, provider=provider,
        )
    return triggered, created


# ── signal parsing ───────────────────────────────────────────────────────────

def test_detects_not_suitable_summary_case_insensitive():
    task = _make_planner_task(42, "NOT SUITABLE FOR DECOMPOSITION: issue is already small")
    triggered, created = _check(done_tasks=[task], issues_map=_issue_map_entry(42))
    assert triggered == [42]
    assert created, "a validator task must be created"


def test_detects_mixed_case_summary():
    task = _make_planner_task(7, "Not Suitable for Decomposition — single-file fix")
    triggered, created = _check(done_tasks=[task], issues_map=_issue_map_entry(7))
    assert triggered == [7]
    assert created


def test_ignores_planning_complete_summary():
    """PLANNING COMPLETE is the happy path, handled separately, not by us."""
    task = _make_planner_task(42, "PLANNING COMPLETE: ready for decomposition")
    triggered, created = _check(done_tasks=[task])
    assert triggered == []
    assert created == []


def test_ignores_unrelated_planner_summary():
    task = _make_planner_task(42, "just rambling about the epic")
    triggered, created = _check(done_tasks=[task])
    assert triggered == []
    assert created == []


# ── task creation ─────────────────────────────────────────────────────────────

def test_creates_validator_task_with_correct_assignee():
    task = _make_planner_task(42, "NOT SUITABLE FOR DECOMPOSITION: reason")
    triggered, created = _check(
        done_tasks=[task],
        issues_map=_issue_map_entry(42),
    )
    assert triggered == [42]
    assert len(created) == 1
    assert created[0]["assignee"] == "validator-daedalus"


def test_validator_task_idempotency_key_contains_issue_number():
    task = _make_planner_task(42, "NOT SUITABLE FOR DECOMPOSITION: reason")
    _triggered, created = _check(done_tasks=[task], issues_map=_issue_map_entry(42))
    assert created[0]["idempotency_key"] == "planner-fallback-validator-42"


def test_validator_task_body_mentions_parent_issue():
    task = _make_planner_task(42, "NOT SUITABLE FOR DECOMPOSITION: single-file")
    _triggered, created = _check(done_tasks=[task], issues_map=_issue_map_entry(42))
    body = created[0]["body"]
    assert "#42" in body
    assert "NOT SUITABLE" in body or "not suitable" in body.lower()


def test_dry_run_does_not_create_task_but_reports_trigger():
    task = _make_planner_task(42, "NOT SUITABLE FOR DECOMPOSITION: reason")
    triggered, created = _check(
        done_tasks=[task],
        issues_map=_issue_map_entry(42),
        dry_run=True,
    )
    assert triggered == [42]
    assert created == []


# ── edge cases ────────────────────────────────────────────────────────────────

def test_missing_parent_issue_in_map_and_no_provider_skips():
    """If the issue is outside the issues_map window and no provider is given,
    we cannot build a validator body — skip silently."""
    task = _make_planner_task(42, "NOT SUITABLE FOR DECOMPOSITION: reason")
    triggered, created = _check(done_tasks=[task], issues_map={}, provider=None)
    assert triggered == []
    assert created == []


def test_falls_back_to_provider_when_issue_not_in_map():
    """When the issue number is missing from issues_map (outside the poll window),
    the handler must fetch it from the provider directly."""
    task = _make_planner_task(42, "NOT SUITABLE FOR DECOMPOSITION: reason")
    provider = mock.MagicMock()
    provider.get_issue.return_value = _make_issue_obj(number=42)
    triggered, created = _check(
        done_tasks=[task], issues_map={}, provider=provider,
    )
    assert triggered == [42]
    assert created
    provider.get_issue.assert_called_with(42)


def test_duplicate_signal_idempotent_only_one_validator_created():
    """Calling the handler twice must not create two validator tasks — the
    idempotency_key on the created task is the guard."""
    task = _make_planner_task(42, "NOT SUITABLE FOR DECOMPOSITION: reason")
    issues_map = _issue_map_entry(42)
    # First call: creates the task.
    _t1, c1 = _check(done_tasks=[task], issues_map=issues_map)
    assert len(c1) == 1
    # Second call: include the previously-created task in all_tasks to simulate
    # persistent kanban state. The create side_effect must return the existing id.
    existing = {"id": "t_prev", "idempotency_key": "planner-fallback-validator-42"}
    _t2, c2 = _check(done_tasks=[task], issues_map=issues_map, all_tasks=[task, existing])
    assert len(c2) == 0, "no duplicate task should be created with same idempotency key"


def test_title_without_issue_number_skipped():
    task = _make_planner_task(42, "NOT SUITABLE FOR DECOMPOSITION: reason")
    task["title"] = "no issue reference here"  # can't extract N
    triggered, created = _check(done_tasks=[task])
    assert triggered == []
    assert created == []


def test_non_planner_assignee_skipped():
    task = _make_planner_task(42, "NOT SUITABLE FOR DECOMPOSITION: reason")
    task["assignee"] = "developer-daedalus"
    triggered, created = _check(done_tasks=[task], issues_map=_issue_map_entry(42))
    assert triggered == []
    assert created == []


def test_custom_validator_profile_from_config():
    """User-configured validator profile override must be honored."""
    profiles = {**disp._DEFAULT_PROFILES, "validator": "custom-validator"}
    task = _make_planner_task(42, "NOT SUITABLE FOR DECOMPOSITION: reason")
    triggered, created = _check(
        done_tasks=[task],
        issues_map=_issue_map_entry(42),
        profiles=profiles,
    )
    assert triggered == [42]
    assert created[0]["assignee"] == "custom-validator"
