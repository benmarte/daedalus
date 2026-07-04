"""Unit tests for scripts/check_dist_drift.py — dashboard drift guard (#1277).

Each test feeds a concrete file list to check_drift() and asserts the
(exit_code, message) pair.  No environment dependencies — no git, no
network, no filesystem access beyond importing the script.

Decision table under test
--------------------------
src changed  | dist changed | manifest changed | Expected result
-------------|--------------|------------------|----------------
no           | no           | no               | skip  (exit 0)
yes          | yes          | yes              | pass  (exit 0)
yes          | yes          | no               | fail  (exit 1, manifest missing)
yes          | no           | yes              | fail  (exit 1, dist missing)
yes          | no           | no               | fail  (exit 1, both missing)
no           | yes          | no               | fail  (exit 1, hand-edited dist)
no           | yes          | yes              | fail  (exit 1, hand-edited dist)
no           | no           | yes              | fail  (exit 1, hand-edited manifest)
other only   | —            | —                | skip  (exit 0)
src + other  | no           | no               | fail  (exit 1, rebuild missing)
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# ── load script without installing it ────────────────────────────────────────
_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "scripts" / "check_dist_drift.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_dist_drift", str(_SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load()
check_drift = _mod.check_drift
classify_files = _mod.classify_files


# ── classify_files ─────────────────────────────────────────────────────────-─

def test_classify_src_jsx():
    c = classify_files(["dashboard/src/App.jsx"])
    assert c == {"src": True, "dist": False, "manifest": False}


def test_classify_build_js():
    c = classify_files(["dashboard/build.js"])
    assert c == {"src": True, "dist": False, "manifest": False}


def test_classify_dist():
    c = classify_files(["dashboard/dist/index-ABC123.js"])
    assert c == {"src": False, "dist": True, "manifest": False}


def test_classify_manifest():
    c = classify_files(["dashboard/manifest.json"])
    assert c == {"src": False, "dist": False, "manifest": True}


def test_classify_other():
    c = classify_files(["core/iterate.py", "tests/test_foo.py", "README.md"])
    assert c == {"src": False, "dist": False, "manifest": False}


def test_classify_mixed():
    c = classify_files([
        "dashboard/src/App.jsx",
        "dashboard/dist/index-NEW.js",
        "dashboard/manifest.json",
        "core/iterate.py",
    ])
    assert c == {"src": True, "dist": True, "manifest": True}


# ── check_drift — skip cases (exit 0) ────────────────────────────────────────

def test_empty_list_skip():
    code, msg = check_drift([])
    assert code == 0
    assert "skip" in msg


def test_no_dashboard_files_skip():
    code, msg = check_drift([
        "core/iterate.py",
        "tests/test_foo.py",
        "docs/README.md",
        "scripts/watchdog.py",
    ])
    assert code == 0
    assert "skip" in msg


def test_all_three_areas_pass():
    code, msg = check_drift([
        "dashboard/src/App.jsx",
        "dashboard/dist/index-NEW1234.js",
        "dashboard/manifest.json",
    ])
    assert code == 0
    assert "pass" in msg


def test_all_three_plus_other_pass():
    """Other files alongside the full dashboard triple should still pass."""
    code, msg = check_drift([
        "dashboard/src/App.jsx",
        "dashboard/build.js",
        "dashboard/dist/index-NEW.js",
        "dashboard/manifest.json",
        "core/util.py",
    ])
    assert code == 0
    assert "pass" in msg


# ── check_drift — fail cases (exit 1) ────────────────────────────────────────

def test_src_only_fail():
    """src changed but dist and manifest missing → rebuild not committed."""
    code, msg = check_drift(["dashboard/src/App.jsx"])
    assert code == 1
    assert "FAIL" in msg
    assert "dashboard/dist/" in msg
    assert "dashboard/manifest.json" in msg


def test_src_and_dist_no_manifest_fail():
    """src + dist changed but manifest missing → incomplete rebuild."""
    code, msg = check_drift([
        "dashboard/src/App.jsx",
        "dashboard/dist/index-NEW.js",
    ])
    assert code == 1
    assert "FAIL" in msg
    # The diagnostic line (first line) must name manifest as the missing item.
    diagnostic = msg.splitlines()[0]
    assert "dashboard/manifest.json" in diagnostic
    # dist is present — must not appear in the "not updated" part of the diagnostic.
    assert "dashboard/dist/" not in diagnostic


def test_src_and_manifest_no_dist_fail():
    """src + manifest changed but dist missing → partial rebuild."""
    code, msg = check_drift([
        "dashboard/src/App.jsx",
        "dashboard/manifest.json",
    ])
    assert code == 1
    assert "FAIL" in msg
    # The diagnostic line must name dist as the missing item.
    diagnostic = msg.splitlines()[0]
    assert "dashboard/dist/" in diagnostic
    # manifest is present — must not appear in the "not updated" diagnostic.
    assert "dashboard/manifest.json" not in diagnostic


def test_build_js_only_fail():
    """build.js changed counts as src; dist+manifest must follow."""
    code, msg = check_drift(["dashboard/build.js"])
    assert code == 1
    assert "FAIL" in msg


def test_dist_only_fail():
    """dist changed without src → hand-edited bundle."""
    code, msg = check_drift(["dashboard/dist/index-HAND.js"])
    assert code == 1
    assert "FAIL" in msg
    assert "hand" in msg.lower() or "without" in msg.lower()


def test_dist_and_manifest_no_src_fail():
    """dist + manifest changed without src → still a hand-edit."""
    code, msg = check_drift([
        "dashboard/dist/index-HAND.js",
        "dashboard/manifest.json",
    ])
    assert code == 1
    assert "FAIL" in msg


def test_manifest_only_fail():
    """manifest changed alone → hand-edited manifest."""
    code, msg = check_drift(["dashboard/manifest.json"])
    assert code == 1
    assert "FAIL" in msg


def test_src_plus_other_fail():
    """src + unrelated files changed, but no dist/manifest → still a drift."""
    code, msg = check_drift([
        "dashboard/src/App.jsx",
        "core/iterate.py",
        "tests/test_foo.py",
    ])
    assert code == 1
    assert "FAIL" in msg


# ── fix hint included in failures ─────────────────────────────────────────────

def test_src_fail_includes_fix_hint():
    _, msg = check_drift(["dashboard/src/App.jsx"])
    # The hint tells the author exactly what to run.
    assert "npm run build" in msg


def test_dist_fail_includes_fix_hint():
    _, msg = check_drift(["dashboard/dist/index-HAND.js"])
    assert "npm run build" in msg
