#!/usr/bin/env python3
"""Issue #1209 — airtight test isolation for the kanban board.

#1203 isolated the board by env (``HERMES_HOME``), but under the pipeline QA/PM
worker environment real cards still leaked onto the live board because the env
override failed to propagate to some subprocess path. This suite locks in the
defense-in-depth follow-up:

  1. The autouse ``_isolate_hermes_home`` fixture stubs ``core.kanban._hk`` by
     default — no test spawns the real ``hermes kanban`` CLI.
  2. A test's own explicit ``mock.patch`` still wins over that stub (ordering).
  3. ``core.kanban._guard_test_isolation`` raises if ``_hk`` is reached under
     pytest without an isolated HERMES_HOME, converting any future gap into a
     loud failure instead of a silent live-board write.
  4. The guard is a no-op in production (sentinel unset).

Run: python3 tests/test_issue_1209_airtight_isolation.py
"""

import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import check  # noqa: E402
from core import kanban  # noqa: E402


# ── 1. the chokepoint is stubbed by default ──────────────────────────────────


def test_hk_stubbed_by_default():
    """With no explicit patch, ``_hk`` is the in-memory stub (never a subprocess)."""
    rc, out, err = kanban._hk(["list", "--json"])
    check("default _hk is stubbed (rc != 0)", rc != 0)
    check("stub identifies issue #1209", "1209" in err)


def test_card_creation_never_reaches_real_cli():
    """A real card-creation path funnels through the stubbed ``_hk`` and returns
    falsy — it must not spawn a subprocess or write to any board."""
    with mock.patch("subprocess.run") as spawned:
        tid = kanban.create_triage("test-isolated", 999, "phantom", "body")
    check("create_triage returns falsy under the stub", not tid)
    check("no real subprocess was spawned", spawned.call_count == 0)


# ── 2. an explicit patch still wins over the autouse stub ─────────────────────


def test_explicit_patch_wins_over_autouse_stub():
    """A test that patches ``_hk`` itself overrides the fixture stub (ordering)."""

    def fake_hk(args, timeout=60):
        return (0, "Created t_abc (triage)", "")

    with mock.patch.object(kanban, "_hk", fake_hk):
        tid = kanban.create_triage("slug", 7, "title", "body")
    check("explicit patch wins → parsed tid", tid == "t_abc")
    # After the with-block the autouse stub is restored, not the real _hk.
    rc, _out, err = kanban._hk(["list"])
    check("autouse stub restored after explicit patch exits", "1209" in err)


# ── 3. the hard guard refuses a real-board write under pytest ─────────────────


def test_guard_raises_on_real_board(monkeypatch):
    """Reaching the real ``_hk`` under pytest with a real/unset HERMES_HOME raises."""
    real_home = str(Path.home() / ".hermes")
    monkeypatch.setenv("HERMES_HOME", real_home)
    with pytest.raises(RuntimeError, match="1209"):
        kanban._guard_test_isolation()


def test_guard_raises_when_hermes_home_unset(monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    with pytest.raises(RuntimeError, match="1209"):
        kanban._guard_test_isolation()


def test_guard_allows_isolated_tmp_home(monkeypatch, tmp_path):
    """An isolated tmp HERMES_HOME passes the guard (no raise)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    kanban._guard_test_isolation()  # must not raise
    check("guard allows an isolated tmp HERMES_HOME", True)


# ── 4. the guard is a no-op in production (sentinel unset) ────────────────────


def test_guard_noop_in_production(monkeypatch):
    """With PYTEST_CURRENT_TEST unset, the guard returns even on the real board."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(Path.home() / ".hermes"))
    kanban._guard_test_isolation()  # must not raise — real runs are unaffected
    check("guard is a no-op when the pytest sentinel is unset", True)


# ── 5. opt-out marker exposes the real (guarded) _hk ──────────────────────────


@pytest.mark.uses_real_hk
def test_uses_real_hk_marker_skips_stub():
    """``@pytest.mark.uses_real_hk`` opts out of the stub; the real guarded ``_hk``
    is in place (identifiable because the stub message is gone). It stays safe:
    the guard still blocks a real-board write, and the tmp HERMES_HOME is fine."""
    # The autouse fixture set an isolated tmp HERMES_HOME, so the guard passes and
    # the real _hk would attempt a subprocess — assert it is NOT the stub.
    with mock.patch("subprocess.run", return_value=mock.Mock(returncode=0, stdout="", stderr="")):
        rc, _out, err = kanban._hk(["list"])
    check("uses_real_hk exposes the real _hk (stub bypassed)", "1209" not in err)


if __name__ == "__main__":
    import conftest

    # Minimal standalone runner: emulate the autouse fixture's stub + env.
    import os
    import tempfile

    os.environ.setdefault("PYTEST_CURRENT_TEST", "standalone")
    _tmp = tempfile.mkdtemp()
    os.environ["HERMES_HOME"] = _tmp
    kanban._hk = lambda args, timeout=60: (1, "", "stub 1209")  # type: ignore
    for name in sorted(n for n in dir() if n.startswith("test_")):
        fn = globals()[name]
        try:
            import inspect

            if inspect.signature(fn).parameters:
                continue  # skip fixture-dependent tests in standalone mode
            fn()
        except Exception as e:  # noqa: BLE001
            conftest.check(name, False)
            print(f"    {e}")
    print(f"\n{conftest._passed} passed, {conftest._failed} failed")
    sys.exit(1 if conftest._failed else 0)
