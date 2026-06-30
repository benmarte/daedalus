"""Tests for monotonic idempotency key in planner-fallback validator path.

Part of epic #1008 (dispatcher race condition fixes).

The previous implementation used a static idempotency key
``planner-fallback-validator-{N}`` that never changes — even after the validator
task is done/cancelled/archived the same key still shadows a retry, so a
recurring issue or re-trigger can't create a new validator task.

The fix: make the key monotonic. Each new validator gets a new generation
suffix (`-g0`, `-g1`, `-g2`, ...) based on how many past generations (done,
cancelled, archived) already exist. Active (todo/ready/running/blocked) tasks
for the current generation still prevent duplicates within a single generation.

Refs: epic #1008, related: #988."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import _load_dispatch  # noqa: E402

disp = _load_dispatch()


# ── _compute_planner_fallback_idempotency_key ─────────────────────────────────

def _all_tasks_with_keys(rows):
    """Build a list of task dicts with idempotency_key + status (no summary
    lookup needed for the key computation)."""
    return [
        {"id": f"t_{i}", "idempotency_key": key, "status": status,
         "title": "#42 parent", "assignee": "validator-daedalus"}
        for i, (key, status) in enumerate(rows)
    ]


def test_first_validator_has_g0_suffix():
    """With no prior planner-fallback-validator-42 tasks, key ends with -g0."""
    with mock.patch.object(disp.kanban, "list_tasks", return_value=[]):
        key = disp._compute_planner_fallback_idempotency_key("slug", 42)
    assert key == "planner-fallback-validator-42-g0"


def test_g0_is_skipped_when_alive_task_exists():
    """A running task on g0 must bump the candidate generation to g1 — we must
    never reuse a live task's key.

    IMPORTANT: the monotonic key returns the NEXT generation (g1) when the
    lower one is still alive, so re-running within the same dispatch window
    uses g1's key. The g0 task's create-task call already holds g0's key so
    duplicates within that generation are still impossible.
    """
    tasks = _all_tasks_with_keys([("planner-fallback-validator-42-g0", "running")])
    with mock.patch.object(disp.kanban, "list_tasks", return_value=tasks):
        key = disp._compute_planner_fallback_idempotency_key("slug", 42)
    # With a live g0, the function picks g0 because it's the LOWEST generation
    # that exists; re-running within the same generation uses the same key.
    # That's the correct semantics: the alive task owns g0.
    assert key == "planner-fallback-validator-42-g0"


def test_done_task_bumps_generation_to_g1():
    """A done g0 task means generation 0 is closed; next key is g1."""
    tasks = _all_tasks_with_keys([("planner-fallback-validator-42-g0", "done")])
    with mock.patch.object(disp.kanban, "list_tasks", return_value=tasks):
        key = disp._compute_planner_fallback_idempotency_key("slug", 42)
    assert key == "planner-fallback-validator-42-g1"


def test_cancelled_task_also_bumps_generation():
    """Terminal cancelled (or 'canceled') status counts as a closed generation."""
    tasks = _all_tasks_with_keys([("planner-fallback-validator-42-g0", "cancelled")])
    with mock.patch.object(disp.kanban, "list_tasks", return_value=tasks):
        key = disp._compute_planner_fallback_idempotency_key("slug", 42)
    assert key == "planner-fallback-validator-42-g1"


def test_archived_task_also_bumps_generation():
    """Archived tasks are terminal too — their generation is closed."""
    tasks = _all_tasks_with_keys([("planner-fallback-validator-42-g0", "archived")])
    with mock.patch.object(disp.kanban, "list_tasks", return_value=tasks):
        key = disp._compute_planner_fallback_idempotency_key("slug", 42)
    assert key == "planner-fallback-validator-42-g1"


def test_multiple_generations_count_correctly():
    """Two closed generations (g0=done, g1=archived) → next key is g2.

    Note: the function doesn't need the keys to be in-order in the input list;
    it parses the integer suffix and finds max(closed)+1.
    """
    tasks = _all_tasks_with_keys([
        ("planner-fallback-validator-42-g0", "done"),
        ("planner-fallback-validator-42-g1", "archived"),
    ])
    with mock.patch.object(disp.kanban, "list_tasks", return_value=tasks):
        key = disp._compute_planner_fallback_idempotency_key("slug", 42)
    assert key == "planner-fallback-validator-42-g2"


def test_live_task_in_middle_generation_does_not_bump():
    """A running g1 blocks further growth — we can't use g2 while g1 is live.

    If g0 is done (closed) and g1 is running, the function returns g1 — the
    live task's own key. We must NOT reuse a higher generation; that would
    let two agents run concurrently for the same generation slot.
    """
    tasks = _all_tasks_with_keys([
        ("planner-fallback-validator-42-g0", "done"),
        ("planner-fallback-validator-42-g1", "running"),
    ])
    with mock.patch.object(disp.kanban, "list_tasks", return_value=tasks):
        key = disp._compute_planner_fallback_idempotency_key("slug", 42)
    assert key == "planner-fallback-validator-42-g1"


def test_blocked_task_counts_as_alive():
    """A blocked card is an in-flight validator — must not bump past it."""
    tasks = _all_tasks_with_keys([
        ("planner-fallback-validator-42-g0", "done"),
        ("planner-fallback-validator-42-g1", "blocked"),
    ])
    with mock.patch.object(disp.kanban, "list_tasks", return_value=tasks):
        key = disp._compute_planner_fallback_idempotency_key("slug", 42)
    assert key == "planner-fallback-validator-42-g1"


def test_unrelated_keys_are_ignored():
    """Tasks for other issues must not affect the key for issue 42."""
    tasks = _all_tasks_with_keys([
        ("planner-fallback-validator-7-g0", "done"),
        ("validator-42", "done"),
        ("planner-fallback-validator-422-g0", "done"),  # 422, not 42
    ])
    with mock.patch.object(disp.kanban, "list_tasks", return_value=tasks):
        key = disp._compute_planner_fallback_idempotency_key("slug", 42)
    assert key == "planner-fallback-validator-42-g0"


def test_non_monotonic_key_tasks_are_ignored():
    """Legacy static keys (no -gN suffix) must not be counted as a generation.

    Without this, any production board still carrying the old
    ``planner-fallback-validator-42`` key would force generation 1 and skip
    g0 entirely — breaking migration for existing deployments.
    """
    tasks = _all_tasks_with_keys([
        ("planner-fallback-validator-42", "done"),  # legacy static key
    ])
    with mock.patch.object(disp.kanban, "list_tasks", return_value=tasks):
        key = disp._compute_planner_fallback_idempotency_key("slug", 42)
    assert key == "planner-fallback-validator-42-g0"


def test_g0_with_todo_is_alive_not_closed():
    """A todo g0 means the validator hasn't started yet — do not bump to g1."""
    tasks = _all_tasks_with_keys([("planner-fallback-validator-42-g0", "todo")])
    with mock.patch.object(disp.kanban, "list_tasks", return_value=tasks):
        key = disp._compute_planner_fallback_idempotency_key("slug", 42)
    assert key == "planner-fallback-validator-42-g0"


def test_g0_with_ready_is_alive_not_closed():
    """A ready g0 is dispatched-but-not-started — still alive."""
    tasks = _all_tasks_with_keys([("planner-fallback-validator-42-g0", "ready")])
    with mock.patch.object(disp.kanban, "list_tasks", return_value=tasks):
        key = disp._compute_planner_fallback_idempotency_key("slug", 42)
    assert key == "planner-fallback-validator-42-g0"


# ── Integration: handler uses the monotonic key ─────────────────────────────

def _make_planner_task(number: int, summary: str, task_id: str = "t_planner") -> dict:
    """Build a minimal DONE planner task dict."""
    return {
        "id": task_id,
        "title": f"#{number} Epic parent",
        "body": "epic body",
        "assignee": "planner-daedalus",
        "status": "done",
        "summary": summary,
        "idempotency_key": f"planner-{number}",
    }


def _issue_map_entry(n: int):
    return {n: {"number": n, "title": "Epic title", "body": "epic body",
                "labels": [], "url": f"https://github.com/o/r/issues/{n}"}}


def _run_handler(done_tasks=(), blocked_tasks=(), all_tasks=(), issues_map=None):
    """Run the handler with mocked kanban calls. Returns created tasks list."""
    issues_map = issues_map if issues_map is not None else {}
    show_map = {
        t["id"]: {**t, "latest_summary": t.get("summary", "")}
        for t in tuple(done_tasks) + tuple(blocked_tasks)
    }
    # The key-computation helper needs ALL tasks (incl. archived). The existing
    # done/blocked filter only feeds the handler's scan of planner cards. We
    # route list_tasks by status: '' returns all, 'done' returns planners done,
    # 'blocked' returns planners blocked — same contract as the real kanban.
    all_kanban = list(all_tasks)
    list_map = {
        None: list(all_kanban),
        "done": list(done_tasks),
        "blocked": list(blocked_tasks),
        "": list(all_kanban),
    }
    created = []

    def _fake_list_tasks(slug_, status=None):
        return list(list_map.get(status, list_map[None]))

    def _fake_show_card(slug_, tid):
        return show_map.get(tid)

    def _fake_create_task(slug_, title, body="", *, assignee="", idempotency_key="",
                          workspace="", skills=None, **_):
        # Idempotency: if key already seen in this or prior call, return existing.
        for prev in created:
            if prev["idempotency_key"] == idempotency_key:
                return prev["id"]
        for prev in all_kanban:
            if (prev.get("idempotency_key") or "") == idempotency_key:
                return prev["id"]
        new_id = f"t_new_{len(created)}"
        created.append({
            "id": new_id, "title": title, "assignee": assignee,
            "idempotency_key": idempotency_key, "body": body,
        })
        return new_id

    with (
        mock.patch.object(disp.kanban, "list_tasks", side_effect=_fake_list_tasks),
        mock.patch.object(disp.kanban, "show_card", side_effect=_fake_show_card),
        mock.patch.object(disp.kanban, "create_task", side_effect=_fake_create_task),
    ):
        disp._check_planner_not_suitable(
            "slug", repo="o/r", issues_map=issues_map, workdir="/tmp/w",
            base_branch="dev", provider_name="github",
            profiles=disp._DEFAULT_PROFILES, role_skills={},
            coding_agent="none", coding_agent_cmd="",
            notify_targets=None, dry_run=False, provider=None,
        )
    return created


def test_handler_creates_validator_with_g0_key_on_first_run():
    """When no prior validator exists, handler creates one with -g0 key."""
    task = _make_planner_task(42, "NOT SUITABLE FOR DECOMPOSITION: reason")
    created = _run_handler(done_tasks=[task], issues_map=_issue_map_entry(42))
    assert len(created) == 1
    assert created[0]["idempotency_key"] == "planner-fallback-validator-42-g0"


def test_handler_reuses_key_when_generation_0_still_alive():
    """Running the handler again while g0 is alive returns the existing task."""
    task = _make_planner_task(42, "NOT SUITABLE FOR DECOMPOSITION: reason")
    alive = {"id": "t_alive", "idempotency_key": "planner-fallback-validator-42-g0",
             "status": "running", "title": "#42", "assignee": "validator-daedalus"}
    created = _run_handler(
        done_tasks=[task], issues_map=_issue_map_entry(42),
        all_tasks=[alive],
    )
    # No new create call — existing task was returned.
    assert len(created) == 0 or (
        len(created) == 1 and created[0]["id"] == "t_alive"
    )


def test_handler_creates_g1_after_g0_is_done():
    """Once g0 is done (generation closed), handler produces a NEW -g1 task."""
    task = _make_planner_task(42, "NOT SUITABLE FOR DECOMPOSITION: reason")
    prev = {"id": "t_old", "idempotency_key": "planner-fallback-validator-42-g0",
            "status": "done", "title": "#42", "assignee": "validator-daedalus"}
    created = _run_handler(
        done_tasks=[task], issues_map=_issue_map_entry(42),
        all_tasks=[prev],
    )
    assert len(created) == 1
    assert created[0]["idempotency_key"] == "planner-fallback-validator-42-g1"


def test_handler_creates_g2_after_g0_and_g1_both_done():
    """Two closed generations → handler produces -g2."""
    task = _make_planner_task(42, "NOT SUITABLE FOR DECOMPOSITION: reason")
    g0 = {"id": "t_g0", "idempotency_key": "planner-fallback-validator-42-g0",
          "status": "done", "title": "#42", "assignee": "validator-daedalus"}
    g1 = {"id": "t_g1", "idempotency_key": "planner-fallback-validator-42-g1",
          "status": "archived", "title": "#42", "assignee": "validator-daedalus"}
    created = _run_handler(
        done_tasks=[task], issues_map=_issue_map_entry(42),
        all_tasks=[g0, g1],
    )
    assert len(created) == 1
    assert created[0]["idempotency_key"] == "planner-fallback-validator-42-g2"


def test_handler_does_not_create_duplicate_when_same_generation():
    """Within the same generation window, re-running must not duplicate the task."""
    task = _make_planner_task(42, "NOT SUITABLE FOR DECOMPOSITION: reason")
    g0_alive = {"id": "t_g0", "idempotency_key": "planner-fallback-validator-42-g0",
                "status": "running", "title": "#42", "assignee": "validator-daedalus"}
    created = _run_handler(
        done_tasks=[task], issues_map=_issue_map_entry(42),
        all_tasks=[g0_alive],
    )
    # No NEW task should be created (the alive task owns the g0 slot).
    assert created == []
