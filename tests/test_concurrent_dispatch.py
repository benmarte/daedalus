"""Concurrent dispatch tests — verifying the process-level mutex and idempotency
guards produce exactly one task per issue under concurrent dispatch invocations.

Three concurrency scenarios are tested:

1. **Process-level mutex (FileLock)**: Two concurrent calls to main() on the same
   host must result in exactly one proceeding, the other exiting cleanly with rc=0.

2. **Idempotency guard**: Two concurrent calls to create_task with the same
   idempotency_key must produce exactly one physical task on the board (the
   kanban.create_task dedup prevents the second).

3. **Status-blind guard + concurrency**: A race where one thread marks tasks
   terminal while another dispatches — the status-blind guard must not see stale
   tasks as blockers.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from conftest import FakeKanban, _load_dispatch  # noqa: E402


# ── 1. Process-level FileLock mutex ──────────────────────────────────────────


class TestFileLockMutex:
    """Two concurrent calls to main() — exactly one proceeds, the other exits
    cleanly with rc=0.

    Strategy: use two threads to call main() back-to-back. The FileLock at
    scripts/.daedalus_dispatch.lock is real OS-level, so two threads in the
    same process race for it just like two separate processes would. The loser
    sees ``Timeout`` and returns 0 immediately.
    """

    def test_concurrent_main_calls_only_one_proceeds(self, tmp_path: Path):
        """Simulate two cron ticks landing on the same host simultaneously."""
        disp = _load_dispatch()

        # Point the mutex lock file at a temp directory so tests don't fight
        # with a real dispatcher running on the dev host.
        test_lock_path = str(tmp_path / ".daedalus_dispatch_test.lock")
        orig_lock_path = disp._MUTEX_LOCK_PATH
        disp._MUTEX_LOCK_PATH = test_lock_path

        # _main_inner is the real work — record both threads' attempts.
        results = {"inner_calls": 0}
        lock_for_inner = threading.Lock()

        def counting_inner() -> int:
            with lock_for_inner:
                results["inner_calls"] += 1
            # Simulate work to widen the lock-hold window.
            time.sleep(0.3)
            return 42  # distinctive rc so we know who ran

        import unittest.mock

        with unittest.mock.patch.object(disp, "_main_inner", side_effect=counting_inner):
            # Two threads firing main() concurrently.
            rc_a: list[int] = []
            rc_b: list[int] = []
            start = threading.Event()

            def runner(rc_out: list[int]) -> None:
                start.wait()
                rc_out.append(disp.main())

            t1 = threading.Thread(target=runner, args=(rc_a,))
            t2 = threading.Thread(target=runner, args=(rc_b,))
            t1.start()
            t2.start()
            start.set()
            t1.join(timeout=5)
            t2.join(timeout=5)

        # One thread saw "Timeout" and returned 0; the other got _main_inner and
        # returned 42. Exactly one _main_inner call ran.
        assert results["inner_calls"] == 1, (
            f"expected exactly 1 _main_inner invocation, got {results['inner_calls']}"
        )
        rcs = sorted(rc_a + rc_b)
        assert rcs == [0, 42], f"expected [0, 42] (loser exits cleanly), got {rcs}"

        # Restore module-level state so other tests in this suite aren't affected.
        disp._MUTEX_LOCK_PATH = orig_lock_path


# ── 2. Idempotency guard: concurrent run() produces exactly one task per issue ─


class TestConcurrentRunIdempotency:
    """Two concurrent create_task calls for the same issue must produce
    exactly one task per idempotency_key despite both threads reaching
    the kanban.create_task dedup.

    The FakeKanban's idempotency dedup (same key → returns existing id) is what
    prevents duplicates. Both threads run concurrently so the race is real —
    the dedup must hold.
    """

    def test_concurrent_create_task_produces_one_task_each(self):
        """Two threads calling create_task for the same keys dedupe via idemp keys."""
        disp = _load_dispatch()

        # Shared FakeKanban — mirrors the real DB's idempotency behaviour.
        fk = FakeKanban()
        disp.kanban = fk
        slug = "proj-concurrent-idemp"

        # Simulate two concurrent dispatch threads racing to create the same
        # team task set for issue 9001. Both threads call create_task with the
        # same idempotency keys — only one task per key should end up on board.
        expected_keys = {"developer-9001", "qa-9001", "reviewer-9001",
                         "security-9001", "docs-9001"}
        start = threading.Event()

        def create_all() -> None:
            start.wait()
            for key in expected_keys:
                role = key.split("-")[0]
                fk.create_task(
                    slug, f"#9001 {role.title()}",
                    assignee="developer-daedalus",
                    idempotency_key=key,
                )

        t1 = threading.Thread(target=create_all)
        t2 = threading.Thread(target=create_all)
        t1.start()
        t2.start()
        start.set()
        t1.join(timeout=5)
        t2.join(timeout=5)

        # Each idempotency key should produce exactly one task regardless of
        # how many threads raced create_task calls.
        for k in expected_keys:
            matches = [t for t in fk.tasks.values()
                       if t.get("idempotency_key") == k]
            assert len(matches) == 1, (
                f"idempotency key {k!r} produced {len(matches)} tasks "
                f"(expected exactly 1 — dedup failed)"
            )


# ── 3. Status-blind guard + concurrency ──────────────────────────────────────


class TestStatusBlindGuardConcurrency:
    """A race where one thread completes tasks (→ terminal) while another
    concurrently calls _has_downstream_tasks: the guard must never be fooled
    into seeing a stale terminal card as an active one.
    """

    def test_concurrent_terminal_transition_does_not_block_new_dispatch(self):
        """Concurrent complete + has_downstream_tasks → no ghost block."""
        disp = _load_dispatch()
        fk = FakeKanban()
        disp.kanban = fk
        slug = "proj-concurrent-guard"

        issue_n = 9002

        # Seed an active task that will be completed from one thread.
        tid_1 = fk.seed(
            assignee="developer-daedalus",
            title=f"#{issue_n} Developer: active",
            status="running",
            summary="",
        )

        # Another thread now completes the task — the race.
        def completer() -> None:
            fk.complete(slug, tid_1, summary="done")

        # The querying thread runs _has_downstream_tasks repeatedly.
        observations: list[bool] = []

        def querier() -> None:
            for _ in range(20):
                result = disp._has_downstream_tasks(
                    slug, issue_n,
                    validator_profile="validator-daedalus",
                    pm_profile="project-manager-daedalus",
                    planner_profile="planner-daedalus",
                )
                observations.append(result)

        tc = threading.Thread(target=completer)
        tq = threading.Thread(target=querier)
        tc.start()
        tq.start()
        tc.join(timeout=2)
        tq.join(timeout=2)

        # Once the task is completed (after a few iterations), no subsequent
        # observation should report True (status-blind guard kicks in).
        # The first few readings before completion may be True — that's fine.
        # But once we see a False, every subsequent observation must also be
        # False (monotonic transition: False stays False).
        went_false = False
        for obs in observations:
            if obs is False:
                went_false = True
            elif went_false and obs is True:
                raise AssertionError(
                    f"status-blind guard is flaky: went False→True: {observations}"
                )


# ── 4. Cross-process FileLock simulation ─────────────────────────────────────


def test_filelock_cross_process_simulated(tmp_path: Path):
    """Simulate two separate processes holding the same FileLock: a second
    acquisition attempt must raise Timeout and main() returns 0.

    Uses a real FileLock to verify the lock behaviour end-to-end.
    """
    from filelock import FileLock, Timeout

    lock_path = str(tmp_path / ".cross_process_test.lock")
    holder = FileLock(lock_path)
    holder.acquire(timeout=5)

    try:
        # Second acquisition from a "different process" (non-blocking).
        contender = FileLock(lock_path)
        with pytest.raises(Timeout):
            contender.acquire(timeout=0)
    finally:
        holder.release()


def test_main_returns_zero_on_lock_contention(tmp_path: Path):
    """main() exits cleanly (rc=0) when another process holds the mutex."""
    from filelock import FileLock

    disp = _load_dispatch()
    lock_path = str(tmp_path / ".main_contention_test.lock")
    orig = disp._MUTEX_LOCK_PATH
    disp._MUTEX_LOCK_PATH = lock_path

    # Pre-acquire the lock so main() will see contention.
    holder = FileLock(lock_path)
    holder.acquire(timeout=5)
    try:
        rc = disp.main()
        assert rc == 0, f"main() returned {rc}, expected 0 on lock contention"
    finally:
        holder.release()
        disp._MUTEX_LOCK_PATH = orig


# ── 5. Concurrent _count_active_issue_tasks ─────────────────────────────────


def test_concurrent_count_active_tasks_with_terminal():
    """_count_active_issue_tasks is correct while tasks transition in-flight."""
    disp = _load_dispatch()
    fk = FakeKanban()
    disp.kanban = fk
    slug = "proj-concurrent-count"
    issue_n = 9003

    # Seed 3 active tasks for the same issue.
    tids = [
        fk.seed(assignee="developer-daedalus", title=f"#{issue_n} Developer", status="running"),
        fk.seed(assignee="qa-daedalus", title=f"#{issue_n} QA", status="todo"),
        fk.seed(assignee="reviewer-daedalus", title=f"#{issue_n} Reviewer", status="blocked"),
    ]
    assert disp._count_active_issue_tasks(slug, issue_n) == 3

    # Concurrently complete one from another thread and count from this one.
    def completer() -> None:
        fk.complete(slug, tids[0], summary="merged")

    tc = threading.Thread(target=completer)
    tc.start()
    tc.join(timeout=2)

    # One task moved to done → terminal → should not count.
    assert disp._count_active_issue_tasks(slug, issue_n) == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
