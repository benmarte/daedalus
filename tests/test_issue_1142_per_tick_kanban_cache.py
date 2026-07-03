"""Tests for the per-tick list_tasks cache in core/kanban.py (issue #1142).

Verifies:
  1. N repeated list_tasks(slug, status) collapse to 1 _hk subprocess per
     distinct (slug, status) key when the cache is enabled.
  2. A mutation between two identical list_tasks calls forces a refetch
     (invalidation works — no stale reads within a tick).
  3. When disabled (default), every list_tasks call hits _hk (no caching).
  4. enable/reset/disable lifecycle: disable clears the cache; data is
     identical whether cached or uncached.
  5. Mutating functions all invalidate the cache.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

# Ensure project root is on sys.path before importing core.kanban
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.path.insert(0, str(Path(__file__).resolve().parent))

import conftest  # noqa: E402
from conftest import check  # noqa: E402,F401
from core import kanban  # noqa: E402

# Save a reference to the real list_tasks at import time — some tests in
# test_daedalus.py replace ``kanban.list_tasks`` with a lambda and never
# restore it (pre-existing test hygiene issue).  We restore the real function
# in the _clean_cache fixture so our cache assertions always hit _hk.
_ORIG_LIST_TASKS = kanban.list_tasks


@pytest.fixture(autouse=True)
def _clean_cache():
    """Ensure each test starts with the cache disabled and empty.

    Also restores the real ``list_tasks`` function — some tests in
    test_daedalus.py replace ``kanban.list_tasks`` with a lambda and never
    restore it (pre-existing test hygiene issue), which would bypass the
    cache and make our assertions fail when run after those tests.
    """
    kanban.disable_tick_cache()
    kanban._tick_cache.clear()
    kanban.list_tasks = _ORIG_LIST_TASKS
    yield
    kanban.disable_tick_cache()
    kanban._tick_cache.clear()
    kanban.list_tasks = _ORIG_LIST_TASKS


def _make_hk_counter():
    """Return a mock _hk that counts calls and returns a fixed task list."""
    call_count = {"n": 0}
    tasks_v1 = [{"id": "t_aaa", "title": "#1 task one", "status": "running"}]
    tasks_v2 = [{"id": "t_aab", "title": "#2 task two", "status": "running"}]
    # Track which "version" of the board we're on so mutations can change data
    state = {"version": 0}

    def mock_hk(args, timeout=60):
        if "list" in args and "--json" in args:
            call_count["n"] += 1
            data = tasks_v1 if state["version"] == 0 else tasks_v2
            return 0, json.dumps(data), ""
        # Mutating commands (create, complete, block, decompose, edit, etc.)
        if any(cmd in args for cmd in ("create", "complete", "block", "decompose", "edit")):
            state["version"] = 1  # simulate board change
            return 0, "t_aab", ""
        return 0, "", ""

    return mock_hk, call_count, state


# ── Test 1: repeated calls collapse to 1 subprocess per distinct key ────────


def test_cache_collapses_repeated_calls():
    """N repeated list_tasks(slug, status) with cache enabled → 1 _hk call."""
    mock_hk, call_count, _state = _make_hk_counter()

    with mock.patch("core.kanban._hk", side_effect=mock_hk):
        kanban.enable_tick_cache()
        kanban.reset_tick_cache()

        for _ in range(5):
            result = kanban.list_tasks("test-board", "running")

        check("5 calls → 1 _hk subprocess", call_count["n"] == 1)
        check("cached result is correct", len(result) == 1)
        check("cached result has right id", result[0]["id"] == "t_aaa")


def test_cache_distinct_keys_cached_separately():
    """Distinct (slug, status) keys each get their own cache entry."""
    call_count = {"n": 0}

    def mock_hk(args, timeout=60):
        if "list" in args and "--json" in args:
            call_count["n"] += 1
            status = ""
            for i, a in enumerate(args):
                if a == "--status" and i + 1 < len(args):
                    status = args[i + 1]
            if status == "running":
                return 0, json.dumps([{"id": "t_1", "status": "running"}]), ""
            elif status == "blocked":
                return 0, json.dumps([{"id": "t_2", "status": "blocked"}]), ""
            else:
                return 0, json.dumps([{"id": "t_3", "status": "done"}]), ""
        return 0, "", ""

    with mock.patch("core.kanban._hk", side_effect=mock_hk):
        kanban.enable_tick_cache()
        kanban.reset_tick_cache()

        # 3 distinct keys, each called 3 times → 3 subprocess calls total
        for _ in range(3):
            kanban.list_tasks("board", "running")
        for _ in range(3):
            kanban.list_tasks("board", "blocked")
        for _ in range(3):
            kanban.list_tasks("board", "done")

        check("3 distinct keys × 3 calls → 3 _hk subprocesses", call_count["n"] == 3)


# ── Test 2: mutation invalidates cache → refetch ────────────────────────────


def test_mutation_between_identical_calls_forces_refetch():
    """A mutation between two list_tasks calls forces a fresh subprocess."""
    mock_hk, call_count, state = _make_hk_counter()

    with mock.patch("core.kanban._hk", side_effect=mock_hk):
        kanban.enable_tick_cache()
        kanban.reset_tick_cache()

        # First call: populates cache
        r1 = kanban.list_tasks("board", "running")
        check("first call hits _hk", call_count["n"] == 1)
        check("first result is v1", r1[0]["id"] == "t_aaa")

        # Mutation: complete a task → invalidates cache
        kanban.complete("board", "t_aaa")

        # Second call: cache was cleared, must refetch
        r2 = kanban.list_tasks("board", "running")
        check("second call hits _hk again (invalidation)", call_count["n"] == 2)
        check("second result reflects mutation", r2[0]["id"] == "t_aab")


def test_no_stale_reads_after_mutation():
    """Post-mutation list_tasks reflects the change within the tick."""
    mock_hk, call_count, state = _make_hk_counter()

    with mock.patch("core.kanban._hk", side_effect=mock_hk):
        kanban.enable_tick_cache()
        kanban.reset_tick_cache()

        r1 = kanban.list_tasks("board", "running")
        kanban.block_task("board", "t_aaa", reason="blocked")
        r2 = kanban.list_tasks("board", "running")

        check("results differ after mutation", r1 != r2)
        check("r1 has old data", r1[0]["id"] == "t_aaa")
        check("r2 has new data", r2[0]["id"] == "t_aab")


# ── Test 3: disabled by default — every call hits _hk ────────────────────────


def test_disabled_by_default_every_call_hits_hk():
    """When cache is disabled (default), every list_tasks call spawns _hk."""
    call_count = {"n": 0}

    def mock_hk(args, timeout=60):
        if "list" in args and "--json" in args:
            call_count["n"] += 1
            return 0, json.dumps([{"id": "t_1"}]), ""
        return 0, "", ""

    with mock.patch("core.kanban._hk", side_effect=mock_hk):
        # Cache is disabled by default (guaranteed by _clean_cache fixture)
        for _ in range(5):
            kanban.list_tasks("board", "running")

        check("disabled → 5 calls = 5 _hk subprocesses", call_count["n"] == 5)


def test_enable_then_disable_roundtrip():
    """Enable → cached; disable → uncached again."""
    call_count = {"n": 0}

    def mock_hk(args, timeout=60):
        if "list" in args and "--json" in args:
            call_count["n"] += 1
            return 0, json.dumps([{"id": "t_1"}]), ""
        return 0, "", ""

    with mock.patch("core.kanban._hk", side_effect=mock_hk):
        # Enabled → 1 call
        kanban.enable_tick_cache()
        kanban.reset_tick_cache()
        kanban.list_tasks("board", "running")
        kanban.list_tasks("board", "running")
        check("enabled: 2 calls → 1 _hk", call_count["n"] == 1)

        # Disable → every call hits _hk again
        kanban.disable_tick_cache()
        kanban.list_tasks("board", "running")
        kanban.list_tasks("board", "running")
        check("disabled: 2 more calls → 2 more _hk (total 3)", call_count["n"] == 3)


# ── Test 4: cached == uncached data ──────────────────────────────────────────


def test_cached_equals_uncached_data():
    """Cached and uncached list_tasks return identical data for the same state."""
    tasks = [{"id": f"t_{i}", "title": f"#{i} task", "status": "running"} for i in range(10)]

    def mock_hk(args, timeout=60):
        if "list" in args and "--json" in args:
            return 0, json.dumps(tasks), ""
        return 0, "", ""

    with mock.patch("core.kanban._hk", side_effect=mock_hk):
        # Uncached
        kanban.disable_tick_cache()
        uncached = kanban.list_tasks("board", "running")

        # Cached
        kanban.enable_tick_cache()
        kanban.reset_tick_cache()
        cached = kanban.list_tasks("board", "running")

        check("cached == uncached data", cached == uncached)
        check("both have 10 tasks", len(cached) == 10 == len(uncached))


# ── Test 5: every mutating function invalidates the cache ──────────────────


def test_all_mutations_invalidate_cache():
    """Each board-mutating function clears the cache so the next list_tasks
    refetches."""
    mutations = [
        ("create_task", lambda: kanban.create_task("board", "title")),
        ("complete", lambda: kanban.complete("board", "t_1")),
        ("block_task", lambda: kanban.block_task("board", "t_1", reason="x")),
        ("decompose", lambda: kanban.decompose("board", "t_1")),
        ("decompose_all_triage", lambda: kanban.decompose_all_triage("board")),
        ("edit_summary", lambda: kanban.edit_summary("board", "t_1", "sum")),
        ("create_triage", lambda: kanban.create_triage("board", 99, "title", "body")),
    ]

    for name, mutate_fn in mutations:
        call_count = {"n": 0}

        def mock_hk(args, timeout=60, _cc=call_count):
            if "list" in args and "--json" in args:
                _cc["n"] += 1
                return 0, json.dumps([{"id": "t_1"}]), ""
            return 0, "t_aab", ""

        with mock.patch("core.kanban._hk", side_effect=mock_hk):
            kanban.enable_tick_cache()
            kanban.reset_tick_cache()

            # Populate cache
            kanban.list_tasks("board", "running")
            assert call_count["n"] == 1, f"{name}: first call should hit _hk"

            # Mutate
            mutate_fn()

            # Next list_tasks must refetch (cache was invalidated)
            kanban.list_tasks("board", "running")
            check(f"{name}: mutation forces refetch", call_count["n"] == 2)

        kanban.disable_tick_cache()


def test_edit_body_invalidates_cache():
    """edit_body (direct SQLite write) also invalidates the cache."""
    call_count = {"n": 0}

    def mock_hk(args, timeout=60):
        if "list" in args and "--json" in args:
            call_count["n"] += 1
            return 0, json.dumps([{"id": "t_1"}]), ""
        return 0, "", ""

    with mock.patch("core.kanban._hk", side_effect=mock_hk), \
         mock.patch("os.path.exists", return_value=True), \
         mock.patch("core.kanban.connect_wal") as mock_conn:

        mock_conn.return_value.execute.return_value.rowcount = 1
        mock_conn.return_value.execute.return_value.fetchone.return_value = None

        kanban.enable_tick_cache()
        kanban.reset_tick_cache()

        # Populate cache
        kanban.list_tasks("board", "running")
        assert call_count["n"] == 1

        # edit_body uses direct SQLite, not _hk
        kanban.edit_body("board", "t_1", "new body")

        # Next list_tasks must refetch
        kanban.list_tasks("board", "running")
        check("edit_body forces refetch", call_count["n"] == 2)

    kanban.disable_tick_cache()


# ── Test 6: reset_tick_cache clears entries without disabling ───────────────


def test_reset_tick_cache_clears_entries():
    """reset_tick_cache clears cached entries but keeps caching enabled."""
    call_count = {"n": 0}

    def mock_hk(args, timeout=60):
        if "list" in args and "--json" in args:
            call_count["n"] += 1
            return 0, json.dumps([{"id": "t_1"}]), ""
        return 0, "", ""

    with mock.patch("core.kanban._hk", side_effect=mock_hk):
        kanban.enable_tick_cache()
        kanban.reset_tick_cache()

        kanban.list_tasks("board", "running")  # cache miss → _hk
        kanban.list_tasks("board", "running")  # cache hit → no _hk
        check("2 calls → 1 _hk", call_count["n"] == 1)

        kanban.reset_tick_cache()  # clear cache, keep enabled

        kanban.list_tasks("board", "running")  # cache miss → _hk
        check("after reset: 1 more _hk call (total 2)", call_count["n"] == 2)


# ── Run all tests standalone ─────────────────────────────────────────────────


if __name__ == "__main__":
    tests = [
        test_cache_collapses_repeated_calls,
        test_cache_distinct_keys_cached_separately,
        test_mutation_between_identical_calls_forces_refetch,
        test_no_stale_reads_after_mutation,
        test_disabled_by_default_every_call_hits_hk,
        test_enable_then_disable_roundtrip,
        test_cached_equals_uncached_data,
        test_all_mutations_invalidate_cache,
        test_edit_body_invalidates_cache,
        test_reset_tick_cache_clears_entries,
    ]
    for t in tests:
        print(f"\n--- {t.__name__} ---")
        try:
            t()
        except Exception as e:
            conftest._failed += 1
            print(f"  FAIL  (raised {type(e).__name__}: {e})")

    print(f"\n{'='*60}")
    print(f"Results: {conftest._passed} passed, {conftest._failed} failed")
    if conftest._failed:
        sys.exit(1)