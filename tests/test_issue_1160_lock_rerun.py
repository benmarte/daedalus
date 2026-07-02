"""Regression tests for issue #1160 — advance-hook dispatch silently dropped on
FileLock contention.

Before the fix, ``main()`` acquired the process mutex with ``timeout=0`` and on
``Timeout`` logged a warning and returned 0 — the dispatch was silently
discarded, stalling stage handoffs until the next hourly cron tick.

After the fix:
- On ``Timeout`` the dispatch's scope is appended to a rerun marker file next to
  the lock, and the lock HOLDER consumes the marker before releasing, running one
  scoped pass per queued scope (capped).
- After writing the marker the waiter retries ``acquire(timeout=0)`` once, so a
  holder that released in the race window cannot strand the marker.
- The lock wait is configurable via ``DAEDALUS_LOCK_WAIT`` so the advance hook
  can serialize with a bounded wait instead of dropping.
- The advance hook logs dispatch output instead of sending it to /dev/null.
"""

from __future__ import annotations

import fcntl
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import _load_dispatch

disp = _load_dispatch()

ROOT = Path(__file__).resolve().parent.parent


# ── helpers ───────────────────────────────────────────────────────────────────


class _HeldLock:
    """Hold the dispatcher FileLock from the test via raw flock."""

    def __init__(self, lock_path: Path):
        self.fd = open(lock_path, "w")

    def __enter__(self):
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        self.fd.close()


def _use_tmp_lock(monkeypatch, tmp_path: Path) -> Path:
    lock_path = tmp_path / "dispatch.lock"
    monkeypatch.setattr(disp, "_MUTEX_LOCK_PATH", str(lock_path))
    return lock_path


# ── waiter side: Timeout queues a scoped rerun marker ─────────────────────────


def test_timeout_queues_scoped_marker_and_exits_zero(monkeypatch, tmp_path):
    lock_path = _use_tmp_lock(monkeypatch, tmp_path)
    monkeypatch.setattr(
        sys, "argv", ["daedalus_dispatch.py", "--repo", "/some/project"]
    )
    calls = []
    monkeypatch.setattr(disp, "_main_inner", lambda *a, **k: calls.append(a) or 0)

    with _HeldLock(lock_path):
        rc = disp.main()

    assert rc == 0
    assert calls == [], "waiter must not run a dispatch pass while lock is held"
    marker = disp._rerun_marker_path()
    assert marker.exists(), "Timeout must queue a rerun marker (issue #1160)"
    assert "/some/project" in marker.read_text(encoding="utf-8")


def test_timeout_readonly_invocations_do_not_queue(monkeypatch, tmp_path):
    lock_path = _use_tmp_lock(monkeypatch, tmp_path)
    monkeypatch.setattr(disp, "_main_inner", lambda *a, **k: 0)

    for argv in (["--history"], ["--history", "5"], ["--self-test"], ["--dry-run"]):
        monkeypatch.setattr(sys, "argv", ["daedalus_dispatch.py"] + argv)
        with _HeldLock(lock_path):
            rc = disp.main()
        assert rc == 0
        assert not disp._rerun_marker_path().exists(), (
            f"read-only/dry invocation {argv} must not queue a rerun"
        )


def test_timeout_unresolvable_scope_does_not_queue(monkeypatch, tmp_path):
    lock_path = _use_tmp_lock(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", ["daedalus_dispatch.py"])
    monkeypatch.setattr(disp, "_resolve_repo_from_cwd", lambda: None)

    with _HeldLock(lock_path):
        rc = disp.main()

    assert rc == 0
    assert not disp._rerun_marker_path().exists()


def test_timeout_warning_keeps_legacy_wording(monkeypatch, tmp_path, caplog):
    """The pre-existing subprocess serialization test greps for this phrase."""
    lock_path = _use_tmp_lock(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", ["daedalus_dispatch.py", "--repo", "/p"])

    with _HeldLock(lock_path), caplog.at_level("WARNING"):
        disp.main()

    assert any("FileLock already held" in r.message for r in caplog.records)


# ── holder side: marker consumed before release ───────────────────────────────


def test_holder_runs_queued_scopes_before_release(monkeypatch, tmp_path):
    _use_tmp_lock(monkeypatch, tmp_path)
    marker = disp._rerun_marker_path()
    marker.write_text("/proj/a\n/proj/a\n/proj/b\n", encoding="utf-8")

    calls = []

    def fake_inner(argv=None):
        calls.append(argv)
        return 0

    monkeypatch.setattr(disp, "_main_inner", fake_inner)
    monkeypatch.setattr(sys, "argv", ["daedalus_dispatch.py"])

    rc = disp.main()

    assert rc == 0
    assert calls[0] is None, "own pass runs first with real argv"
    assert calls[1:] == [["--repo", "/proj/a"], ["--repo", "/proj/b"]], (
        "holder must rerun each queued scope exactly once (deduped, in order)"
    )
    assert not marker.exists(), "marker must be consumed by the holder"


def test_holder_rerun_passes_are_capped(monkeypatch, tmp_path):
    _use_tmp_lock(monkeypatch, tmp_path)
    marker = disp._rerun_marker_path()
    marker.write_text("/proj/a\n", encoding="utf-8")

    calls = []

    def fake_inner(argv=None):
        calls.append(argv)
        # Adversarial: a new dispatch gets queued during every pass.
        marker.write_text("/proj/a\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(disp, "_main_inner", fake_inner)
    monkeypatch.setattr(sys, "argv", ["daedalus_dispatch.py"])

    rc = disp.main()

    assert rc == 0
    # own pass + at most _RERUN_MAX_PASSES rerun passes
    assert len(calls) <= 1 + disp._RERUN_MAX_PASSES
    assert marker.exists(), "cap hit — leftover marker survives for the next tick"


def test_rerun_failure_does_not_break_remaining_scopes(monkeypatch, tmp_path):
    _use_tmp_lock(monkeypatch, tmp_path)
    marker = disp._rerun_marker_path()
    marker.write_text("/proj/bad\n/proj/good\n", encoding="utf-8")

    calls = []

    def fake_inner(argv=None):
        calls.append(argv)
        if argv and argv[1] == "/proj/bad":
            raise RuntimeError("boom")
        return 0

    monkeypatch.setattr(disp, "_main_inner", fake_inner)
    monkeypatch.setattr(sys, "argv", ["daedalus_dispatch.py"])

    rc = disp.main()

    assert rc == 0
    assert ["--repo", "/proj/good"] in calls


# ── race closure: retry acquire after writing the marker ─────────────────────


def test_waiter_becomes_holder_when_lock_released_in_race_window(monkeypatch, tmp_path):
    lock_path = _use_tmp_lock(monkeypatch, tmp_path)

    class RacyLock:
        """First acquire times out; the holder 'releases' before the retry."""

        def __init__(self, path):
            self.attempts = 0

        def acquire(self, timeout=None):
            self.attempts += 1
            if self.attempts == 1:
                raise disp.Timeout(str(lock_path))

        def release(self):
            pass

    monkeypatch.setattr(disp, "FileLock", RacyLock)
    monkeypatch.setattr(sys, "argv", ["daedalus_dispatch.py", "--repo", "/proj/a"])

    calls = []
    monkeypatch.setattr(disp, "_main_inner", lambda argv=None: calls.append(argv) or 0)

    rc = disp.main()

    assert rc == 0
    assert calls, "retry acquire must promote the waiter to holder and run"
    assert not disp._rerun_marker_path().exists(), (
        "the marker the waiter wrote must be consumed by its own rerun loop"
    )


# ── bounded lock wait via env ─────────────────────────────────────────────────


def test_lock_wait_env_parsing(monkeypatch):
    monkeypatch.delenv("DAEDALUS_LOCK_WAIT", raising=False)
    assert disp._lock_wait_secs() == 0.0
    monkeypatch.setenv("DAEDALUS_LOCK_WAIT", "120")
    assert disp._lock_wait_secs() == 120.0
    monkeypatch.setenv("DAEDALUS_LOCK_WAIT", "not-a-number")
    assert disp._lock_wait_secs() == 0.0
    monkeypatch.setenv("DAEDALUS_LOCK_WAIT", "-5")
    assert disp._lock_wait_secs() == 0.0


# ── advance hook: bounded wait + visible dispatch output ─────────────────────


def test_advance_hook_serializes_and_logs_dispatch_output():
    hook = (ROOT / "scripts" / "daedalus-advance.sh").read_text(encoding="utf-8")
    dispatch_lines = [
        ln for ln in hook.splitlines() if "daedalus-cron.sh" in ln and "--repo" in ln
    ]
    assert dispatch_lines, "hook must still dispatch via daedalus-cron.sh --repo"
    line = dispatch_lines[0]
    assert ">/dev/null" not in line.replace(" ", ""), (
        "dispatch output must not be discarded (issue #1160)"
    )
    assert '>>"$dispatch_log"' in line
    assert "daedalus-advance-dispatch.log" in hook
    assert "DAEDALUS_LOCK_WAIT" in line, (
        "hook must request a bounded lock wait so bursts serialize"
    )
