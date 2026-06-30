"""Tests for dev_mode redirect behavior in daedalus_dispatch.py (issue #1071)."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import conftest  # noqa: E402,F401
from conftest import _load_dispatch, check  # noqa: E402,F401

disp = _load_dispatch()


def _make_resolved(dev_mode=None):
    """Build a minimal resolved config dict with an optional dev_mode block."""
    resolved = {"name": "test-repo", "repo": "org/repo"}
    if dev_mode is not None:
        resolved["dev_mode"] = dev_mode
    return resolved


def test_dev_mode_disabled_no_execve():
    """enabled: false → os.execve NOT called."""
    resolved = _make_resolved({"enabled": False, "path": "/tmp/fake-checkout"})
    with mock.patch.object(disp.os, "execve") as mock_execve:
        disp._maybe_redirect_dev_mode(resolved)
    check("execve not called when disabled", mock_execve.call_count == 0)


def test_dev_mode_path_absent_no_execve():
    """enabled: true, path absent → os.execve NOT called."""
    resolved = _make_resolved({"enabled": True, "path": ""})
    with mock.patch.object(disp.os, "execve") as mock_execve:
        disp._maybe_redirect_dev_mode(resolved)
    check("execve not called when path empty", mock_execve.call_count == 0)


def test_dev_mode_enabled_with_valid_path_calls_execve(tmp_path):
    """enabled: true, path + script exist, not already in dev → os.execve called."""
    # Create a fake dev checkout with scripts/daedalus_dispatch.py
    dev_dir = tmp_path / "dev-checkout"
    (dev_dir / "scripts").mkdir(parents=True)
    dev_script = dev_dir / "scripts" / "daedalus_dispatch.py"
    dev_script.write_text("# fake dispatcher\n")

    resolved = _make_resolved({"enabled": True, "path": str(dev_dir)})

    # Ensure DAEDALUS_DEV is not set
    env_patch = mock.patch.dict(os.environ, {}, clear=False)
    env_patch.start()
    os.environ.pop("DAEDALUS_DEV", None)
    try:
        with mock.patch.object(disp.os, "execve") as mock_execve:
            disp._maybe_redirect_dev_mode(resolved)

        check("execve called exactly once", mock_execve.call_count == 1)
        args = mock_execve.call_args
        called_script = args[0][0]
        called_env = args[0][2]
        check("execve called with dev script path", called_script == str(dev_script))
        check("DAEDALUS_DEV=1 in env", called_env.get("DAEDALUS_DEV") == "1")
        check("dev path prepended to PYTHONPATH", called_env.get("PYTHONPATH", "").startswith(str(dev_dir)))
    finally:
        env_patch.stop()


def test_dev_mode_already_set_no_execve():
    """DAEDALUS_DEV=1 already set → os.execve NOT called."""
    resolved = _make_resolved({"enabled": True, "path": "/tmp/fake-checkout"})
    env_patch = mock.patch.dict(os.environ, {"DAEDALUS_DEV": "1"})
    env_patch.start()
    try:
        with mock.patch.object(disp.os, "execve") as mock_execve:
            disp._maybe_redirect_dev_mode(resolved)
        check("execve not called when DAEDALUS_DEV already set", mock_execve.call_count == 0)
    finally:
        env_patch.stop()


def test_dev_mode_script_missing_warns_no_execve(tmp_path):
    """enabled: true, script file missing → os.execve NOT called, logger.warning called."""
    # dev directory exists but scripts/daedalus_dispatch.py does not
    dev_dir = tmp_path / "dev-checkout-no-script"
    dev_dir.mkdir()

    resolved = _make_resolved({"enabled": True, "path": str(dev_dir)})

    env_patch = mock.patch.dict(os.environ, {}, clear=False)
    env_patch.start()
    os.environ.pop("DAEDALUS_DEV", None)
    try:
        with mock.patch.object(disp.os, "execve") as mock_execve, \
             mock.patch.object(disp.logger, "warning") as mock_warning:
            disp._maybe_redirect_dev_mode(resolved)

        check("execve not called when script missing", mock_execve.call_count == 0)
        check("logger.warning called for missing script", mock_warning.call_count >= 1)
    finally:
        env_patch.stop()


def test_dev_mode_already_running_from_dev_no_execve(tmp_path):
    """already running from dev path (abspath match) → os.execve NOT called."""
    # Create a fake dev checkout and make __file__ point at its dispatcher script.
    dev_dir = tmp_path / "dev-checkout-same"
    (dev_dir / "scripts").mkdir(parents=True)
    dev_script = dev_dir / "scripts" / "daedalus_dispatch.py"
    dev_script.write_text("# fake dispatcher\n")

    resolved = _make_resolved({"enabled": True, "path": str(dev_dir)})

    env_patch = mock.patch.dict(os.environ, {}, clear=False)
    env_patch.start()
    os.environ.pop("DAEDALUS_DEV", None)
    try:
        # Patch __file__ to be the same as the dev script path
        with mock.patch.object(disp, "__file__", str(dev_script)), \
             mock.patch.object(disp.os, "execve") as mock_execve:
            disp._maybe_redirect_dev_mode(resolved)
        check("execve not called when already running from dev", mock_execve.call_count == 0)
    finally:
        env_patch.stop()


# ── standalone runner ──────────────────────────────────────────────────────

if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])