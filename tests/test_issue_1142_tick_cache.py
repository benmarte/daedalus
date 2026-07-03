"""Tests for the per-tick ``list_tasks`` cache (issue #1142).

The dispatcher calls ``kanban.list_tasks`` at ~30 sites per tick, each otherwise
spawning a ``hermes kanban list`` subprocess to read identical board state. These
tests pin the caching contract:

  - disabled by default → every call hits the subprocess (unchanged behavior)
  - enabled → N repeated ``(slug, status)`` reads collapse to ONE subprocess/key
  - distinct status filters are cached independently
  - a mutation between two identical reads forces a refetch (no stale reads)
  - post-mutation reads reflect the new board state within the tick
  - ``disable_tick_cache`` restores pass-through behavior
  - the dispatcher wraps its tick in enable/…/finally disable
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

# Ensure project root is on sys.path BEFORE importing core.kanban
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.path.insert(0, str(Path(__file__).resolve().parent))

import conftest  # noqa: E402
from conftest import check  # noqa: E402,F401
from core import kanban  # noqa: E402


# ── Helpers ──────────────────────────────────────────────────────────────────


def _list_hk(payload):
    """Build an ``_hk`` stub that returns ``payload`` (JSON str) for list calls."""

    def _hk(args, timeout=60):
        if "list" in args:
            return (0, payload, "")
        return (0, "", "")  # mutations succeed with no output

    return _hk


def _count_list_calls(mock_hk_obj) -> int:
    """How many of the recorded ``_hk`` calls were board list reads."""
    return sum(1 for c in mock_hk_obj.call_args_list if "list" in c.args[0])


# ── disabled by default ──────────────────────────────────────────────────────


def test_cache_disabled_by_default_every_call_spawns():
    """Without enable_tick_cache(), each list_tasks() spawns a fresh subprocess."""
    kanban.disable_tick_cache()  # ensure clean state
    with mock.patch("core.kanban._hk", side_effect=_list_hk("[]")) as m:
        kanban.list_tasks("b")
        kanban.list_tasks("b")
        kanban.list_tasks("b")
    check("3 uncached reads → 3 list subprocesses", _count_list_calls(m) == 3)


# ── enabled: repeated reads collapse to one subprocess per key ────────────────


def test_cache_collapses_repeated_reads():
    """Enabled cache: N identical (slug, status) reads → exactly 1 subprocess."""
    kanban.enable_tick_cache()
    try:
        with mock.patch("core.kanban._hk", side_effect=_list_hk('[{"id": "t_1"}]')) as m:
            r1 = kanban.list_tasks("b")
            r2 = kanban.list_tasks("b")
            r3 = kanban.list_tasks("b")
        check("10x reads collapse to 1 subprocess", _count_list_calls(m) == 1)
        check("cached data is correct", r1 == [{"id": "t_1"}])
        check("cached == subsequent reads", r1 == r2 == r3)
    finally:
        kanban.disable_tick_cache()


def test_cache_keys_on_status_filter():
    """Distinct status filters are cached independently (one subprocess each)."""
    kanban.enable_tick_cache()
    try:
        with mock.patch("core.kanban._hk", side_effect=_list_hk("[]")) as m:
            kanban.list_tasks("b")               # key (b, "")
            kanban.list_tasks("b")               # cache hit
            kanban.list_tasks("b", "blocked")    # key (b, "blocked")
            kanban.list_tasks("b", "blocked")    # cache hit
            kanban.list_tasks("b2")              # key (b2, "")
        check("3 distinct keys → 3 subprocesses", _count_list_calls(m) == 3)
    finally:
        kanban.disable_tick_cache()


# ── mutations invalidate → forced refetch, no stale reads ─────────────────────


def test_mutation_forces_refetch():
    """A mutation between two identical reads invalidates the cache → refetch."""
    kanban.enable_tick_cache()
    try:
        with mock.patch("core.kanban._hk", side_effect=_list_hk("[]")) as m:
            kanban.list_tasks("b")          # subprocess #1
            kanban.list_tasks("b")          # cache hit
            kanban.create_task("b", "new")  # mutation → invalidates
            kanban.list_tasks("b")          # subprocess #2 (refetch)
            kanban.list_tasks("b")          # cache hit again
        check("mutation forces exactly one refetch", _count_list_calls(m) == 2)
    finally:
        kanban.disable_tick_cache()


def test_no_stale_reads_after_mutation():
    """Post-mutation reads reflect the new board state within the tick."""
    states = ['[{"id": "t_1"}]', '[{"id": "t_1"}, {"id": "t_2"}]']
    calls = {"n": 0}

    def _hk(args, timeout=60):
        if "list" in args:
            payload = states[min(calls["n"], len(states) - 1)]
            calls["n"] += 1
            return (0, payload, "")
        return (0, "", "")

    kanban.enable_tick_cache()
    try:
        with mock.patch("core.kanban._hk", side_effect=_hk):
            before = kanban.list_tasks("b")
            _ = kanban.list_tasks("b")  # cached — must equal `before`
            kanban.complete("b", "t_1")  # mutation → invalidate
            after = kanban.list_tasks("b")  # refetch → new state
        check("read before mutation", before == [{"id": "t_1"}])
        check("read after mutation reflects new state",
              after == [{"id": "t_1"}, {"id": "t_2"}])
    finally:
        kanban.disable_tick_cache()


def test_various_mutations_invalidate():
    """Every board mutation clears the cache (no stale reads for any of them)."""
    mutations = [
        lambda: kanban.create_task("b", "t"),
        lambda: kanban.create_triage("b", 1, "t", "body"),
        lambda: kanban.complete("b", "t_1"),
        lambda: kanban.block_task("b", "t_1", "reason"),
        lambda: kanban.unblock_task("b", "t_1"),
        lambda: kanban.decompose("b", "t_1"),
        lambda: kanban.decompose_all_triage("b"),
        lambda: kanban.edit_summary("b", "t_1", "s"),
        lambda: kanban.comment("b", "t_1", "c"),
        lambda: kanban.reassign_task("b", "t_1", "developer"),
        lambda: kanban.archive_task("b", "t_1"),
        lambda: kanban.dispatch("b"),
    ]
    for mut in mutations:
        kanban.enable_tick_cache()
        try:
            with mock.patch("core.kanban._hk", side_effect=_list_hk("[]")) as m:
                kanban.list_tasks("b")  # populate cache
                mut()                   # should invalidate
                kanban.list_tasks("b")  # must refetch
            check(f"{mut.__name__ if hasattr(mut, '__name__') else mut} invalidates cache",
                  _count_list_calls(m) == 2)
        finally:
            kanban.disable_tick_cache()


# ── disable restores pass-through ─────────────────────────────────────────────


def test_disable_restores_passthrough():
    """After disable_tick_cache(), reads spawn a subprocess again."""
    kanban.enable_tick_cache()
    with mock.patch("core.kanban._hk", side_effect=_list_hk("[]")) as m:
        kanban.list_tasks("b")
        kanban.list_tasks("b")  # cache hit → still 1 so far
        kanban.disable_tick_cache()
        kanban.list_tasks("b")  # pass-through
        kanban.list_tasks("b")  # pass-through
    check("2 reads cached + 2 uncached → 3 subprocesses", _count_list_calls(m) == 3)


def test_failed_read_not_cached():
    """A failed (rc!=0) read returns [] and is retried on the next call."""
    kanban.enable_tick_cache()
    try:
        def _hk(args, timeout=60):
            return (1, "", "boom")
        with mock.patch("core.kanban._hk", side_effect=_hk) as m:
            r1 = kanban.list_tasks("b")
            r2 = kanban.list_tasks("b")
        check("failed read returns []", r1 == [] and r2 == [])
        check("failed read is not cached (retried)", _count_list_calls(m) == 2)
    finally:
        kanban.disable_tick_cache()


# ── dispatcher wiring: run() enables then finally-disables the cache ──────────


def test_run_wraps_tick_with_cache_lifecycle():
    """dispatch.run() enables the cache, runs the tick, and disables in finally."""
    import importlib
    disp = importlib.import_module("scripts.daedalus_dispatch")

    seen = {}

    def _fake_tick(resolved, **kwargs):
        seen["cache_enabled_during_tick"] = kanban._TICK_CACHE is not None
        return {"ok": True}

    kanban.disable_tick_cache()
    with mock.patch.object(disp, "_run_tick", side_effect=_fake_tick):
        out = disp.run({"repo": "o/r"})
    check("run returns the tick summary", out == {"ok": True})
    check("cache was enabled during the tick", seen.get("cache_enabled_during_tick") is True)
    check("cache disabled after run (finally)", kanban._TICK_CACHE is None)


def test_run_disables_cache_on_exception():
    """Even if the tick raises, run() disables the cache in finally."""
    import importlib
    disp = importlib.import_module("scripts.daedalus_dispatch")

    def _boom(resolved, **kwargs):
        raise RuntimeError("tick failed")

    kanban.disable_tick_cache()
    with mock.patch.object(disp, "_run_tick", side_effect=_boom):
        raised = False
        try:
            disp.run({"repo": "o/r"})
        except RuntimeError:
            raised = True
    check("exception propagates", raised)
    check("cache disabled even after exception", kanban._TICK_CACHE is None)


# ── Run all tests ─────────────────────────────────────────────────────────────


if __name__ == "__main__":
    tests = [
        test_cache_disabled_by_default_every_call_spawns,
        test_cache_collapses_repeated_reads,
        test_cache_keys_on_status_filter,
        test_mutation_forces_refetch,
        test_no_stale_reads_after_mutation,
        test_various_mutations_invalidate,
        test_disable_restores_passthrough,
        test_failed_read_not_cached,
        test_run_wraps_tick_with_cache_lifecycle,
        test_run_disables_cache_on_exception,
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
