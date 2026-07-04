"""Regression test for issue #1278.

The Hermes web server loads dashboard/plugin_api.py standalone via
``importlib.util.spec_from_file_location``. That registers the module in
sys.modules under the spec name (e.g. ``hermes_plugin_daedalus_api``) rather
than as ``dashboard.plugin_api``, and does NOT add the dashboard package
directory to sys.path — so ``from dashboard._shared import ...`` raised
``ModuleNotFoundError: No module named 'dashboard'`` on every installed
deployment after the #1155 route split.

The fix (inserted before the first dashboard import) registers a synthetic
``dashboard`` package in sys.modules so the absolute imports resolve, and
self-aliases the module as ``dashboard.plugin_api`` to prevent the circular
double-exec that the routes modules triggered via
``import dashboard.plugin_api as _api``.

This test MUST be run in a subprocess that strips the repo root from
PYTHONPATH so that pytest's own sys.path bootstrap does not mask the bug.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PLUGIN_API = _REPO_ROOT / "dashboard" / "plugin_api.py"


def _build_probe(plugin_api_path: Path) -> str:
    """Return a Python one-shot probe that mimics the Hermes web-server load."""
    return f"""
import importlib.util
import sys

spec = importlib.util.spec_from_file_location(
    "hermes_plugin_daedalus_api",
    {str(plugin_api_path)!r},
)
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)

route_count = len(m.router.routes)
# Floor of 10: a partial mount (some route modules failing to import while
# others succeed) must fail this test, not just a total failure. 23 routes
# exist today; the floor leaves headroom for route removals without going
# blind to a half-dead plugin.
assert route_count >= 10, f"Expected routes >= 10, got {{route_count}}"
print(f"routes={{route_count}}")
"""


def _clean_env(tmp_hermes: Path) -> dict[str, str]:
    """Return an env dict suitable for the probe subprocess.

    * HERMES_HOME is redirected to an isolated tmp dir so the probe never
      touches the live kanban board.
    * PYTHONPATH is cleared so the repo root is NOT importable — this is the
      exact condition that #1278 was triggered by.
    * PYTHONDONTWRITEBYTECODE avoids .pyc pollution in the source tree.
    """
    env = {k: v for k, v in os.environ.items()}
    env["HERMES_HOME"] = str(tmp_hermes)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # Strip repo root from PYTHONPATH so 'dashboard' is NOT on the path.
    raw_pp = env.get("PYTHONPATH", "")
    filtered = ":".join(
        p for p in raw_pp.split(":") if p and Path(p).resolve() != _REPO_ROOT
    )
    if filtered:
        env["PYTHONPATH"] = filtered
    else:
        env.pop("PYTHONPATH", None)
    return env


def test_spec_load_registers_routes(tmp_path: Path) -> None:
    """plugin_api.py loaded via spec_from_file_location must expose routes.

    Simulates the exact context in which the Hermes web server loads the file:
    module registered under a spec name, repo NOT on sys.path.

    Pre-fix proof: without the sys.modules bootstrap block at the top of
    plugin_api.py this probe fails with ``ModuleNotFoundError: No module
    named 'dashboard'`` (verified by stashing the fix and re-running); the
    probe subprocess is what makes that reproducible — in-process pytest
    masks the bug because the repo root is on sys.path.
    """
    tmp_hermes = tmp_path / "hermes_home"
    tmp_hermes.mkdir()

    probe = _build_probe(_PLUGIN_API)
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(tmp_path),  # neutral cwd — not the repo root
        env=_clean_env(tmp_hermes),
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, (
        f"Spec-load probe failed (exit {result.returncode}).\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
    assert "routes=" in result.stdout, (
        f"Expected 'routes=N' in probe stdout; got: {result.stdout!r}"
    )
    route_count = int(result.stdout.strip().split("routes=")[-1].split()[0])
    assert route_count > 0, f"Expected routes > 0, got {route_count}"
