"""
Shared surface for the Daedalus dashboard plugin API.

Houses the graceful-degradation import blocks (config / core helpers), the
module logger, provider-env bootstrap, and the small project-resolution
helpers used across every route group in ``dashboard.plugin_api``.

Extracted from ``dashboard/plugin_api.py`` (issue #1155, PR 1/3) with NO
behaviour change — every symbol is re-exported from ``plugin_api`` so all
existing import paths and test patches (``dashboard.plugin_api._X``) keep
resolving.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from fastapi import HTTPException

# ConfigLoader + deep_merge live in the daedalus package root (config/__init__.py).
# When the dashboard host runs, it adds the plugin dir to sys.path so
# relative imports work. Fall back to absolute import for testing.
# deep_merge / validate_* are re-exported for dashboard.plugin_api (and future
# route modules); noqa the "unused" warning — they are part of the shared surface.
try:
    from config import ConfigLoader, deep_merge, validate_failover, validate_vcs  # noqa: F401
except ImportError:
    import sys

    _repo_root = Path(__file__).resolve().parent.parent
    if str(_repo_root) not in sys.path:
        sys.path.insert(0, str(_repo_root))
    from config import ConfigLoader  # type: ignore[no-redef]
    from config import deep_merge  # type: ignore[no-redef]  # noqa: F401
    from config import validate_failover  # type: ignore[no-redef]  # noqa: F401
    from config import validate_vcs  # type: ignore[no-redef]  # noqa: F401

# Shared cron-list parser (single implementation, issue #1148). Importable once
# the config block above has ensured the plugin root is on sys.path. Re-exported
# via cron_helpers + plugin_api.
from core.cron_parser import parse_cron_jobs  # noqa: F401

# Core helpers (degrade gracefully — never raise on missing data).
try:
    from core.cli import hermes_cli as _hermes_cli
except ImportError:
    def _hermes_cli(args, timeout=30):  # type: ignore[misc]
        try:
            r = subprocess.run(["hermes"] + list(args), capture_output=True,
                               text=True, timeout=timeout)
            return r.returncode, (r.stdout + r.stderr).strip()
        except Exception as exc:
            return -1, str(exc)

try:
    from core.util import (
        board_slug as _board_slug,
        parse_env_file as _parse_env_file,
        schedule_to_crontab as _schedule_to_crontab,
    )
except ImportError:
    def _board_slug(repo, name=""):  # type: ignore[misc]
        slug = repo.replace("/", "-") if repo else name
        return re.sub(r"[^a-zA-Z0-9_-]", "-", slug).strip("-").lower() or name

    def _schedule_to_crontab(schedule):  # type: ignore[misc]
        s = re.sub(r"^every\s+", "", schedule.strip().lower())
        if re.match(r"^[\d*/,\-]+(\s+[\d*/,\-]+){4}$", s):
            return schedule.strip()
        m = re.match(r"^(\d+)m$", s)
        if m:
            minutes = int(m.group(1))
            if minutes >= 60 and minutes % 60 == 0:
                hours = minutes // 60
                return "0 * * * *" if hours == 1 else f"0 */{hours} * * *"
            return f"*/{minutes} * * * *"
        m = re.match(r"^(\d+)h$", s)
        if m:
            hours = int(m.group(1))
            return "0 * * * *" if hours == 1 else f"0 */{hours} * * *"
        return schedule.strip()

    def _parse_env_file(path):  # type: ignore[misc]
        try:
            result = {}
            for line in Path(path).read_text().split("\n"):
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                result[k.strip()] = v.strip().strip('"').strip("'")
            return result
        except OSError:
            return {}

try:
    from core import registry
except ImportError:
    registry = None  # type: ignore[assignment]
try:
    from core.kanban import (list_tasks, ensure_board,
                             diagnostics as kanban_diagnostics)
except ImportError:
    list_tasks = None  # type: ignore[assignment]
    ensure_board = None  # type: ignore[assignment]
    kanban_diagnostics = None  # type: ignore[assignment]
try:
    from core.providers import get_provider
    from core.providers.detect import detect_repo_vcs
except ImportError:
    get_provider = None  # type: ignore[assignment]
    detect_repo_vcs = None  # type: ignore[assignment]


# ── Authentication ───────────────────────────────────────────────────────────
# Auth is a Hermes-host concern, not a plugin one. The daedalus plugin API is
# mounted under /api/plugins/daedalus/* in the dashboard host, whose global
# /api/ auth middleware already gates every plugin route with the session token
# (hermes_cli/web_server.py). Harden at the host layer (loopback bind + tunnel,
# or OAuth/password gated mode); the plugin does not re-implement auth. See #1231.

# Module logger for degrade-gracefully paths. These handlers intentionally return
# a fallback ([], {}, None) so the dashboard keeps rendering, but a silent swallow
# hides the root cause (malformed config, missing token, CLI failure). Log the
# exception detail at each site so failures are diagnosable. See #1133.
logger = logging.getLogger("daedalus.dashboard.plugin_api")

def _bootstrap_provider_env() -> None:
    """Inject provider tokens from ~/.hermes/.env into os.environ.

    The Hermes gateway process is typically started without the user's shell
    environment, so provider tokens (GITHUB_TOKEN, GITLAB_TOKEN, etc.) that
    live in ~/.hermes/.env are invisible to resolve_token() which only checks
    os.environ. This runs once at module load using setdefault so it never
    overwrites tokens that were already exported into the process environment.
    """
    _TOKEN_KEYS = {
        "GITHUB_TOKEN", "GH_TOKEN",
        "GITLAB_TOKEN", "AZURE_DEVOPS_PAT",
        "BITBUCKET_TOKEN",
    }
    import pathlib as _pl
    _home = _pl.Path.home()
    _parts = _home.parts
    if ".hermes" in _parts and "profiles" in _parts:
        _idx = _parts.index(".hermes")
        _home = _pl.Path(*_parts[:_idx])
    _env_path = _home / ".hermes" / ".env"
    for k, v in _parse_env_file(_env_path).items():
        if k in _TOKEN_KEYS and v:
            os.environ.setdefault(k, v)


_bootstrap_provider_env()


def _real_home() -> Path:
    """Return the real macOS user home, even when HOME is sandboxed.

    When running under Hermes, HOME is set to
    ~/.hermes/profiles/<profile>/home. Detect this and return the
    actual user home directory instead.
    """
    home = Path.home()
    parts = home.parts
    # Sandbox detection: path ends with .hermes/profiles/<name>/home
    if ".hermes" in parts and "profiles" in parts:
        idx = parts.index(".hermes")
        # Real home is everything before .hermes
        return Path(*parts[:idx])
    return home


# ── secret keys to strip before returning ──────────────────────────────────
_SECRET_KEYS = {"secret", "api_key", "password", "token"}


def _strip_secrets(obj: Any) -> Any:
    """Recursively remove secret keys from a dict or list. Returns a new object."""
    if isinstance(obj, dict):
        return {
            k: _strip_secrets(v)
            for k, v in obj.items()
            if k not in _SECRET_KEYS
        }
    if isinstance(obj, list):
        return [_strip_secrets(item) for item in obj]
    return obj


def _resolve_project_path(name: str) -> Path:
    """Look up a project by name. Iterates registry entries, resolves each
    per-repo config via ConfigLoader.resolve_repo_config, and matches on
    the ``name`` field from the on-disk config.

    Raises HTTPException(404) if not found.
    """
    if registry is None:
        raise HTTPException(status_code=404, detail=f"Project '{name}' not found")

    try:
        repo_paths = registry.list_projects()
    except Exception as exc:
        logger.warning("registry: list_projects failed while locating workdir: %s", exc)
        repo_paths = []

    loader = ConfigLoader()
    for rp in repo_paths:
        try:
            resolved = loader.resolve_repo_config(rp)
        except Exception as exc:
            logger.warning("config: resolve_repo_config failed for %r — skipping: %s", rp, exc)
            continue
        if resolved.get("name") == name:
            return Path(rp)

    raise HTTPException(status_code=404, detail=f"Project '{name}' not found")


def _project_resolved(name: str) -> dict[str, Any]:
    """Resolve a project name to its full per-repo config dict.

    Raises HTTPException(404) if the project is not found or has no repo.
    """
    repo_path = _resolve_project_path(name)
    loader = ConfigLoader()
    try:
        resolved = loader.resolve_repo_config(str(repo_path))
    except FileNotFoundError:
        raise HTTPException(
            status_code=404,
            detail=f"No daedalus config found for project '{name}'",
        )
    if not resolved.get("repo", ""):
        raise HTTPException(
            status_code=404,
            detail=f"Project '{name}' has no repo configured",
        )
    return resolved


def _project_repo(name: str) -> str:
    """Resolve a project name to its owner/repo string (404 on missing)."""
    return _project_resolved(name).get("repo", "")
