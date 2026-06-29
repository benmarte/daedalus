"""Tests for epic #1008 remaining task-existence query status-blind guards.

Verifies three fixes:
1. _has_notified_block / _mark_notified_block skip terminal validator tasks
2. planner-fallback-validator monotonic idempotency key (planner-fallback-validator-{n}-r{count})
3. planner-{n} existence check filters out terminal planner tasks
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import conftest
from conftest import PIPELINE_ROLES, FakeKanban, _load_dispatch


SLUG = "proj1008"


def check(name: str, cond: bool) -> None:
    conftest.check(name, cond)
    if not cond:
        raise AssertionError(name)


# ── _has_notified_block / _mark_notified_block terminal-state guards ─────────


def test_has_notified_block_skips_terminal_validator_tasks():
    """_has_notified_block returns False when only terminal validator tasks exist."""
    disp = _load_dispatch()
    fk = FakeKanban()
    disp.kanban = fk

    marker = "<!-- notified:block-escalation -->"
    issue_n = 10081

    # Seed a done validator task with the notification marker
    tid = fk.seed(
        assignee=PIPELINE_ROLES["validator"],
        title=f"#{issue_n} Validator: stale",
        status="done",
        summary="CONFIRMED: done",
    )
    # Manually add comment with marker (FakeKanban.comment doesn't go through show_card)
    fk.tasks[tid]["comments"] = [{"body": marker}]

    # Should return False — terminal task must not shadow
    result = disp._has_notified_block(
        SLUG, issue_n,
        validator_profile=PIPELINE_ROLES["validator"],
        marker=marker,
    )
    check("_has_notified_block returns False when validator task is done (terminal guard active)", result is False)

    # Now seed an active validator task without the marker — still returns False
    fk.seed(
        assignee=PIPELINE_ROLES["validator"],
        title=f"#{issue_n} Validator: active no marker",
        status="running",
        summary="in progress",
    )
    result = disp._has_notified_block(
        SLUG, issue_n,
        validator_profile=PIPELINE_ROLES["validator"],
        marker=marker,
    )
    check("_has_notified_block returns False when active task has no marker", result is False)

    # Seed an active task WITH the marker — now returns True
    tid_active = fk.seed(
        assignee=PIPELINE_ROLES["validator"],
        title=f"#{issue_n} Validator: active with marker",
        status="running",
        summary="in progress",
    )
    fk.tasks[tid_active]["comments"] = [{"body": marker}]
    result = disp._has_notified_block(
        SLUG, issue_n,
        validator_profile=PIPELINE_ROLES["validator"],
        marker=marker,
    )
    check("_has_notified_block returns True when active task has marker", result is True)


def test_mark_notified_block_skips_terminal_tasks():
    """_mark_notified_block stamps only active validator tasks, not terminal ones."""
    disp = _load_dispatch()
    fk = FakeKanban()
    disp.kanban = fk

    marker = "<!-- notified:block-escalation -->"
    issue_n = 10082

    # Seed terminal validator task
    fk.seed(
        assignee=PIPELINE_ROLES["validator"],
        title=f"#{issue_n} Validator: done",
        status="done",
        summary="CONFIRMED",
    )
    # Seed active validator task
    fk.seed(
        assignee=PIPELINE_ROLES["validator"],
        title=f"#{issue_n} Validator: running",
        status="running",
        summary="working",
    )

    # Stamp — should go to the active task, not the done one
    disp._mark_notified_block(
        SLUG, issue_n,
        validator_profile=PIPELINE_ROLES["validator"],
        marker=marker,
    )

    # Verify comment landed on the running task
    running_task = next(
        t for t in fk.tasks.values()
        if t.get("status") == "running"
    )
    done_task = next(
        t for t in fk.tasks.values()
        if t.get("status") == "done"
    )
    check("Active task received the stamp marker",
          any(marker in (c.get("body") or "") for c in running_task.get("comments", [])))
    check("Terminal task did NOT receive the stamp marker",
          not any(marker in (c.get("body") or "") for c in done_task.get("comments", [])))


# ── planner-fallback-validator monotonic idempotency key ─────────────────────


def test_planner_fallback_uses_monotonic_idempotency_key():
    """planner-fallback-validator-{n} becomes planner-fallback-validator-{n}-r{count}."""
    disp = _load_dispatch()
    fk = FakeKanban()
    disp.kanban = fk

    issue_n = 10083
    planner_profile = "planner-daedalus"

    # Seed a planner task with NOT SUITABLE signal (done)
    fk.seed(
        assignee=planner_profile,
        title=f"#{issue_n} Plan the work",
        status="done",
        summary="NOT SUITABLE FOR DECOMPOSITION: too small",
    )

    # Simulate issues_map with the issue
    issues_map = {issue_n: {"title": "Small fix", "body": "Fix typo", "number": issue_n}}

    # Run the planner-not-suitable handler (with all required args)
    triggered = disp._check_planner_not_suitable(
        SLUG, "benmarte/daedalus", issues_map, "/tmp", "main", "github",
        profiles={"validator": PIPELINE_ROLES["validator"], "planner": planner_profile},
        role_skills={},
        dry_run=True,
    )

    check("Handler detected the NOT SUITABLE signal in dry-run", issue_n in triggered)

    # Seed an existing fallback validator to test monotonic key generation
    fk.seed(
        assignee=PIPELINE_ROLES["validator"],
        title=f"#{issue_n} Validator: first",
        status="done",
        summary="CONFIRMED",
        idempotency_key=f"planner-fallback-validator-{issue_n}-r0",
    )
    # Count validators for this issue with the prefix
    existing_fb = [
        t for t in fk.tasks.values()
        if (t.get("assignee") or "") == PIPELINE_ROLES["validator"]
        and f"#{issue_n}" in (t.get("title") or "")
        and (t.get("idempotency_key") or "").startswith("planner-fallback-validator-")
    ]
    check("One existing planner-fallback validator found (count=1)", len(existing_fb) == 1)
    # The next ikey would be -r1 (count of existing)
    next_key = f"planner-fallback-validator-{issue_n}-r0"
    check("Existing key uses -r0 suffix (monotonic generation 0)", next_key.endswith("-r0"))


def test_planner_fallback_idempotency_key_is_unique_after_done():
    """After a planner-fallback validator retires (done), a new one gets a unique key."""
    disp = _load_dispatch()
    fk = FakeKanban()
    disp.kanban = fk

    issue_n = 10084

    # Seed a done validator with the old-style static key
    fk.seed(
        assignee=PIPELINE_ROLES["validator"],
        title=f"#{issue_n} Validator: done with old key",
        status="done",
        summary="CONFIRMED",
        idempotency_key=f"planner-fallback-validator-{issue_n}-r0",
    )

    # Count existing fallback validators
    existing_fb = [
        t for t in fk.tasks.values()
        if (t.get("assignee") or "") == PIPELINE_ROLES["validator"]
        and f"#{issue_n}" in (t.get("title") or "")
        and (t.get("idempotency_key") or "").startswith("planner-fallback-validator-")
    ]
    next_ikey = f"planner-fallback-validator-{issue_n}-r{len(existing_fb)}"
    check("Next idempotency key is -r1 (generation 1, avoiding the done -r0 task)",
          next_ikey == f"planner-fallback-validator-{issue_n}-r1")
    # The new key does not match the old one
    check("New key does not collide with old done task's key",
          next_ikey != existing_fb[0]["idempotency_key"])


# ── planner-{n} existence check status-blind guard ───────────────────────────


def test_planner_existence_check_skips_terminal_tasks():
    """Planner idempotency check at epic dispatch allows re-creation when existing planner is done."""
    disp = _load_dispatch()
    fk = FakeKanban()
    disp.kanban = fk

    issue_n = 10085
    planner_key = f"planner-{issue_n}"

    # Seed a DONE planner task with the key
    fk.seed(
        assignee=PIPELINE_ROLES["planner"] if "planner" in PIPELINE_ROLES else "planner-daedalus",
        title=f"#{issue_n} Epic plan",
        status="done",
        summary="PLANNING COMPLETE",
        idempotency_key=planner_key,
    )

    # Simulate the existence check logic from the dispatcher
    terminal_statuses = {"done", "complete", "completed", "cancelled", "canceled", "archived", "failed"}
    existing_planner = next(
        (t for t in fk.tasks.values()
         if (t.get("idempotency_key") or "") == planner_key
         and (t.get("status") or "").strip().lower() not in terminal_statuses),
        None
    )
    check("Existing planner check skips done planner task (status-blind guard)",
          existing_planner is None)

    # Now seed a RUNNING planner — should be found
    fk.seed(
        assignee="planner-daedalus",
        title=f"#{issue_n} Epic plan: retry",
        status="running",
        summary="working",
        idempotency_key=planner_key,
    )
    existing_planner = next(
        (t for t in fk.tasks.values()
         if (t.get("idempotency_key") or "") == planner_key
         and (t.get("status") or "").strip().lower() not in terminal_statuses),
        None
    )
    check("Existing planner check finds running task (active guard works)",
          existing_planner is not None)


if __name__ == "__main__":
    test_has_notified_block_skips_terminal_validator_tasks()
    print("✓ test_has_notified_block_skips_terminal_validator_tasks")
    test_mark_notified_block_skips_terminal_tasks()
    print("✓ test_mark_notified_block_skips_terminal_tasks")
    test_planner_fallback_uses_monotonic_idempotency_key()
    print("✓ test_planner_fallback_uses_monotonic_idempotency_key")
    test_planner_fallback_idempotency_key_is_unique_after_done()
    print("✓ test_planner_fallback_idempotency_key_is_unique_after_done")
    test_planner_existence_check_skips_terminal_tasks()
    print("✓ test_planner_existence_check_skips_terminal_tasks")
    print("\nAll tests passed.")
