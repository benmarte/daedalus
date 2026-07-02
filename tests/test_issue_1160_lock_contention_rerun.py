"""Regression tests for issue #1160 — dispatch dropped on FileLock contention.

A dispatch invocation that loses the process-mutex race must record a rerun
request (marker file next to the lock) instead of being silently discarded;
the lock HOLDER consumes the marker and runs one extra pass per recorded scope
before releasing, so a session-end advance hook that collides with an in-flight
tick still advances the handoff at lock release instead of stalling until the
next cron tick (up to 60 min).

Covers spec ACs:
- AC1: contended main() records the intended scope and returns 0
- AC2: --dry-run / --history / --self-test losers do NOT record a rerun
- AC3: holder drains the marker (deduped, scoped reruns) before releasing
- AC4: drain rounds are capped at _RERUN_MAX_PASSES
- AC5: a failing rerun pass is logged, lock still released, rc unaffected
- AC6: advance hook logs dispatch output instead of >/dev/null
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest
from filelock import FileLock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import scripts.daedalus_dispatch as disp  # noqa: E402

_HOOK_PATH = Path(__file__).resolve().parent.parent / "scripts" / "daedalus-advance.sh"


@pytest.fixture
def tmp_lock(tmp_path):
    """Point the dispatch mutex (and thus the rerun marker) at tmp_path."""
    lock_path = tmp_path / "dispatch.lock"
    with mock.patch.object(disp, "_MUTEX_LOCK_PATH", str(lock_path)):
        yield lock_path


def _marker(lock_path: Path) -> Path:
    return lock_path.with_suffix(".rerun")


# ── Module surface ────────────────────────────────────────────────────────────


def test_rerun_constants_defined():
    assert getattr(disp, "_RERUN_MAX_PASSES", 0) >= 1
    assert getattr(disp, "_RERUN_GLOBAL_SCOPE", None) == "*"


def test_marker_path_derives_from_lock_path(tmp_lock):
    assert disp._rerun_marker_path() == _marker(tmp_lock)


# ── Scope parsing (dropper side) ─────────────────────────────────────────────


def test_scope_from_argv_repo_flag():
    with mock.patch.object(disp, "_resolve_repo_arg", return_value="/proj/a"):
        assert disp._rerun_scope_from_argv(["--repo", "/proj/a"]) == "/proj/a"


def test_scope_from_argv_repo_equals_form():
    with mock.patch.object(disp, "_resolve_repo_arg", return_value="/proj/b"):
        assert disp._rerun_scope_from_argv(["--repo=/proj/b"]) == "/proj/b"


def test_scope_from_argv_unresolved_repo_falls_back_to_literal_path(tmp_path):
    with mock.patch.object(disp, "_resolve_repo_arg", return_value=None):
        scope = disp._rerun_scope_from_argv(["--repo", str(tmp_path)])
    assert scope == str(tmp_path.resolve())


def test_scope_from_argv_unscoped_uses_cwd_project():
    with mock.patch.object(disp, "_resolve_repo_from_cwd", return_value="/proj/cwd"):
        assert disp._rerun_scope_from_argv([]) == "/proj/cwd"


def test_scope_from_argv_unscoped_no_cwd_project_is_global():
    with mock.patch.object(disp, "_resolve_repo_from_cwd", return_value=None):
        assert disp._rerun_scope_from_argv([]) == disp._RERUN_GLOBAL_SCOPE


@pytest.mark.parametrize(
    "argv",
    [
        ["--dry-run"],
        ["--history"],
        ["--history", "5"],
        ["--history=5"],
        ["--self-test"],
    ],
)
def test_scope_from_argv_readonly_invocations_return_none(argv):
    """AC2: non-mutating invocations must not request a rerun."""
    assert disp._rerun_scope_from_argv(argv) is None


# ── AC1/AC2: contended main() records (or skips) the rerun marker ────────────


def test_contended_main_records_rerun_marker(tmp_lock, caplog):
    holder = FileLock(str(tmp_lock))
    holder.acquire()
    try:
        with mock.patch.object(
            sys, "argv", ["daedalus_dispatch.py", "--repo", "/proj/a"]
        ):
            with mock.patch.object(disp, "_resolve_repo_arg", return_value="/proj/a"):
                with caplog.at_level(logging.WARNING, logger="daedalus.dispatch"):
                    rc = disp.main()
    finally:
        holder.release()

    assert rc == 0
    assert _marker(tmp_lock).read_text() == "/proj/a\n"
    assert any("rerun" in r.message.lower() for r in caplog.records)


def test_contended_dry_run_does_not_record_marker(tmp_lock):
    holder = FileLock(str(tmp_lock))
    holder.acquire()
    try:
        with mock.patch.object(sys, "argv", ["daedalus_dispatch.py", "--dry-run"]):
            rc = disp.main()
    finally:
        holder.release()

    assert rc == 0
    assert not _marker(tmp_lock).exists()


# ── AC3: holder drains the marker before releasing ───────────────────────────


def test_holder_drains_marker_scoped_deduped(tmp_lock):
    _marker(tmp_lock).write_text("/proj/a\n*\n/proj/a\n")
    calls = []

    def fake_inner(argv=None):
        calls.append(argv)
        return 0

    with mock.patch.object(sys, "argv", ["daedalus_dispatch.py"]):
        with mock.patch.object(disp, "_main_inner", side_effect=fake_inner):
            rc = disp.main()

    assert rc == 0
    # Initial pass, then one rerun per unique recorded scope.
    assert calls[0] is None or calls[0] == []
    assert calls[1:] == [["--repo", "/proj/a"], []]
    assert not _marker(tmp_lock).exists(), "marker must be consumed by the holder"
    assert not FileLock(str(tmp_lock)).is_locked


def test_holder_without_marker_runs_single_pass(tmp_lock):
    with mock.patch.object(disp, "_main_inner", return_value=7) as inner:
        rc = disp.main()
    assert rc == 7
    inner.assert_called_once()


# ── AC4: drain rounds are capped ─────────────────────────────────────────────


def test_drain_rounds_are_capped(tmp_lock, caplog):
    _marker(tmp_lock).write_text("/proj/a\n")
    rerun_calls = []

    def retrigger(argv=None):
        # Every pass re-drops a request, as if contenders kept losing the race.
        if argv is not None:
            rerun_calls.append(argv)
        _marker(tmp_lock).write_text("/proj/a\n")
        return 0

    with mock.patch.object(sys, "argv", ["daedalus_dispatch.py"]):
        with mock.patch.object(disp, "_main_inner", side_effect=retrigger):
            with caplog.at_level(logging.WARNING, logger="daedalus.dispatch"):
                rc = disp.main()

    assert rc == 0
    assert len(rerun_calls) == disp._RERUN_MAX_PASSES
    assert _marker(tmp_lock).exists(), "leftover marker stays for the next tick"
    assert any("marker still present" in r.message for r in caplog.records)
    assert not FileLock(str(tmp_lock)).is_locked


# ── AC5: rerun failure is non-fatal ──────────────────────────────────────────


def test_rerun_pass_failure_is_logged_not_fatal(tmp_lock, caplog):
    _marker(tmp_lock).write_text("/proj/a\n")

    def fail_on_rerun(argv=None):
        if argv is not None:
            raise RuntimeError("boom")
        return 0

    with mock.patch.object(sys, "argv", ["daedalus_dispatch.py"]):
        with mock.patch.object(disp, "_main_inner", side_effect=fail_on_rerun):
            with caplog.at_level(logging.ERROR, logger="daedalus.dispatch"):
                rc = disp.main()

    assert rc == 0
    assert any("rerun pass failed" in r.message for r in caplog.records)
    assert not FileLock(str(tmp_lock)).is_locked


# ── AC6: advance hook captures dispatch output ───────────────────────────────


def test_advance_hook_dispatch_output_not_devnull():
    text = _HOOK_PATH.read_text()
    dispatch_lines = [ln for ln in text.splitlines() if "daedalus-cron.sh" in ln]
    assert dispatch_lines, "hook must still launch daedalus-cron.sh"
    for ln in dispatch_lines:
        # stdin may come from /dev/null; stdout/stderr must not go there.
        assert ">/dev/null" not in ln.replace(" ", ""), ln
    assert "daedalus-advance-dispatch.log" in text, (
        "hook must capture dispatch output in the advance-dispatch log"
    )


def test_advance_hook_bash_syntax_ok():
    subprocess.run(["bash", "-n", str(_HOOK_PATH)], check=True)
