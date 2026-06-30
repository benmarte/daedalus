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


# ── Guard chain: each individual guard fails → no redirect ──────────────────

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
        called_executable = args[0][0]
        called_argv = args[0][1]
        called_env = args[0][2]
        # sys.executable is the interpreter, dev_script is the first script arg
        check("execve called with sys.executable", called_executable == sys.executable)
        check("execve argv starts with sys.executable", called_argv[0] == sys.executable)
        check("execve argv has dev script as second arg", called_argv[1] == str(dev_script))
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


# ── Edge cases: missing keys, non-dict dev_mode, permission errors ──────────

def test_dev_mode_key_missing_no_execve():
    """resolved has no 'dev_mode' key at all → os.execve NOT called."""
    resolved = _make_resolved()  # no dev_mode block
    with mock.patch.object(disp.os, "execve") as mock_execve:
        disp._maybe_redirect_dev_mode(resolved)
    check("execve not called when dev_mode key missing", mock_execve.call_count == 0)


def test_dev_mode_not_a_dict_no_execve():
    """dev_mode set to a non-dict value (string, bool, list) → os.execve NOT called."""
    for bad_val in ["true", True, 42, ["enabled", "path"], None]:
        resolved = _make_resolved(bad_val)
        with mock.patch.object(disp.os, "execve") as mock_execve:
            disp._maybe_redirect_dev_mode(resolved)
        check(f"execve not called when dev_mode is {type(bad_val).__name__}",
              mock_execve.call_count == 0)


def test_dev_mode_path_not_a_string_no_execve():
    """dev_mode.path is not a string (e.g., int or list) → os.execve NOT called."""
    resolved = _make_resolved({"enabled": True, "path": 12345})
    with mock.patch.object(disp.os, "execve") as mock_execve:
        disp._maybe_redirect_dev_mode(resolved)
    check("execve not called when path is not a string", mock_execve.call_count == 0)


def test_dev_mode_isfile_permission_error_no_execve(tmp_path):
    """os.path.isfile raises PermissionError → fail safe, no execve, warning logged."""
    dev_dir = tmp_path / "dev-checkout-perm"
    (dev_dir / "scripts").mkdir(parents=True)
    dev_script = dev_dir / "scripts" / "daedalus_dispatch.py"
    dev_script.write_text("# fake dispatcher\n")

    resolved = _make_resolved({"enabled": True, "path": str(dev_dir)})

    env_patch = mock.patch.dict(os.environ, {}, clear=False)
    env_patch.start()
    os.environ.pop("DAEDALUS_DEV", None)
    try:
        with mock.patch.object(disp.os.path, "isfile", side_effect=PermissionError("denied")), \
             mock.patch.object(disp.os, "execve") as mock_execve, \
             mock.patch.object(disp.logger, "warning") as mock_warning:
            disp._maybe_redirect_dev_mode(resolved)

        check("execve not called on isfile permission error", mock_execve.call_count == 0)
        check("warning logged for stat failure", mock_warning.call_count >= 1)
    finally:
        env_patch.stop()


def test_dev_mode_execve_oserror_fail_safe(tmp_path):
    """os.execve raises OSError → fail safe, no crash, warning logged, returns None."""
    dev_dir = tmp_path / "dev-checkout-execve-fail"
    (dev_dir / "scripts").mkdir(parents=True)
    dev_script = dev_dir / "scripts" / "daedalus_dispatch.py"
    dev_script.write_text("# fake dispatcher\n")

    resolved = _make_resolved({"enabled": True, "path": str(dev_dir)})

    env_patch = mock.patch.dict(os.environ, {}, clear=False)
    env_patch.start()
    os.environ.pop("DAEDALUS_DEV", None)
    try:
        with mock.patch.object(disp.os, "execve", side_effect=OSError("exec failed")), \
             mock.patch.object(disp.logger, "warning") as mock_warning:
            # Should return None (not raise)
            result = disp._maybe_redirect_dev_mode(resolved)
        check("returns None on execve OSError", result is None)
        check("warning logged for execve failure", mock_warning.call_count >= 1)
    finally:
        env_patch.stop()


# ── Integration test: end-to-end redirect behavior ───────────────────────────

def test_dev_mode_integration_full_redirect_chain(tmp_path):
    """Integration: all guards pass → execve called with correct interpreter,
    script path, DAEDALUS_DEV=1, and PYTHONPATH containing the dev checkout.

    This exercises the full function (no per-step mocks) against a real temp
    filesystem layout to verify the end-to-end redirect behavior.
    """
    # Build a realistic dev checkout structure
    dev_dir = tmp_path / "daedalus-dev"
    scripts_dir = dev_dir / "scripts"
    scripts_dir.mkdir(parents=True)
    dev_script = scripts_dir / "daedalus_dispatch.py"
    dev_script.write_text("#!/usr/bin/env python3\n# dev dispatcher\n")

    resolved = {
        "name": "my-project",
        "repo": "org/my-project",
        "dev_mode": {
            "enabled": True,
            "path": str(dev_dir),
        },
    }

    env_patch = mock.patch.dict(os.environ, {}, clear=False)
    env_patch.start()
    os.environ.pop("DAEDALUS_DEV", None)
    try:
        with mock.patch.object(disp.os, "execve") as mock_execve, \
             mock.patch.object(disp.logger, "info") as mock_info:
            disp._maybe_redirect_dev_mode(resolved)

        # Verify execve was called
        check("integration: execve called", mock_execve.call_count == 1)

        # Verify the info log fired
        check("integration: info log for redirect", mock_info.call_count >= 1)

        args = mock_execve.call_args
        called_executable = args[0][0]
        called_argv = args[0][1]
        called_env = args[0][2]

        # Verify the executable is the Python interpreter
        check("integration: executable is sys.executable",
              called_executable == sys.executable)

        # Verify argv: [sys.executable, dev_script, *sys.argv[1:]]
        check("integration: argv[0] is sys.executable",
              called_argv[0] == sys.executable)
        check("integration: argv[1] is dev script path",
              called_argv[1] == str(dev_script))

        # Verify DAEDALUS_DEV is set to "1"
        check("integration: DAEDALUS_DEV=1 in env",
              called_env.get("DAEDALUS_DEV") == "1")

        # Verify PYTHONPATH starts with the dev checkout path
        pp = called_env.get("PYTHONPATH", "")
        check("integration: PYTHONPATH starts with dev dir",
              pp.startswith(str(dev_dir)))

        # Verify existing PYTHONPATH is preserved (appended after dev path)
        # (not directly testable without pre-setting it, but the structure
        # is validated by the prepend logic above)
    finally:
        env_patch.stop()


# ── standalone runner ──────────────────────────────────────────────────────

if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])