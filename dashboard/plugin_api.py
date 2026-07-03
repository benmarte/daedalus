"""
Daedalus Dashboard Plugin API — FastAPI APIRouter.

Mounted by the Hermes dashboard at /api/plugins/daedalus/.
Provides config read/write with validation, and per-project status aggregation.
Never reads or returns secrets.

Endpoints:
    GET  /projects                 — aggregated status for every registered project
    POST /project/create           — scaffold + register a new project (board + cron)
    GET/POST /project/{name}/config — read/edit a project's .hermes/daedalus.yaml
    /meta/*                        — branches/labels/boards/statuses/notifications pickers
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any, Optional

import yaml
from fastapi import APIRouter, HTTPException, Request

# ConfigLoader + deep_merge live in the daedalus package root (config/__init__.py).
# When the dashboard host runs, it adds the plugin dir to sys.path so
# relative imports work. Fall back to absolute import for testing.
try:
    from config import ConfigLoader, deep_merge, validate_failover, validate_vcs
except ImportError:
    import sys

    _repo_root = Path(__file__).resolve().parent.parent
    if str(_repo_root) not in sys.path:
        sys.path.insert(0, str(_repo_root))
    from config import ConfigLoader  # type: ignore[no-redef]
    from config import deep_merge  # type: ignore[no-redef]
    from config import validate_failover  # type: ignore[no-redef]
    from config import validate_vcs  # type: ignore[no-redef]

# Shared cron-list parser (single implementation, issue #1148). Importable once
# the config block above has ensured the plugin root is on sys.path.
from core.cron_parser import parse_cron_jobs

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

projects_router = APIRouter(prefix="/projects", tags=["daedalus-projects"])
project_config_router = APIRouter(prefix="/project", tags=["daedalus-project-config"])
meta_router = APIRouter(prefix="/meta", tags=["daedalus-meta"])
profiles_router = APIRouter(prefix="/profiles", tags=["daedalus-profiles"])


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




def _channel_target_and_label(platform_name: str, channel: dict) -> tuple[str, str]:
    """Build ``(target, label)`` from a JSON channel object.

    ``target`` is the ``hermes send -t`` string (always uses the stable
    channel ID when available); ``label`` is the human-readable display
    name shown in the picker. Returns ``('', '')`` to skip the entry.

    Rule: value = platform:ID (stable, machine-readable)
          label = friendly name (human-readable)
    This way the picker shows readable names while configs store IDs that
    survive channel renames.
    """
    # Skip per-thread entries (Slack, Discord) — thread_id marks them
    if channel.get("thread_id"):
        return "", ""

    name = (channel.get("name") or "").strip()
    ch_id = (channel.get("id") or "").strip()
    guild = (channel.get("guild") or "").strip()

    if platform_name == "discord":
        # Discord adapter requires a numeric channel ID — name lookup fails
        if not ch_id:
            return "", ""
        label = (f"#{name}" if name else ch_id) + (f" ({guild})" if guild else "")
        return f"discord:{ch_id}", label

    if platform_name == "slack":
        # Prefer the C-prefixed channel ID; fall back to name for older Hermes
        if ch_id:
            return f"slack:{ch_id}", (name or ch_id)
        if name:
            return f"slack:{name}", name
        return "", ""

    # Generic: prefer ID over name when the channel object carries one.
    # Most platforms (Teams, Mattermost, Matrix, …) expose either id or name.
    if ch_id:
        return f"{platform_name}:{ch_id}", (name or ch_id)
    if not name:
        return "", ""
    return f"{platform_name}:{name}", name


def _hermes_status_configured_platforms() -> set[str]:
    """Parse ``hermes status`` to find platforms marked '✓ configured'.

    Returns a set of lowercase platform names (e.g. ``{'discord', 'slack'}``).
    Degrades gracefully to an empty set on any error.
    """
    rc, out = _hermes_cli(["status"], timeout=10)
    if rc != 0:
        return set()
    configured: set[str] = set()
    in_messaging = False
    for line in out.splitlines():
        if "Messaging Platforms" in line:
            in_messaging = True
            continue
        if in_messaging:
            if line.strip().startswith("◆"):
                break
            if "✓" in line:
                name = line.strip().split()[0].lower()
                configured.add(name)
    return configured


# ── notification-probe cache ─────────────────────────────────────────────────
# `_compute_notification_methods` shells out to `hermes send --list [--json]`
# and `hermes status` on every call. The configured platforms/channels change
# rarely, so the result is cached for a short TTL: repeated /meta/notifications
# GETs within the window reuse the cached value and spawn no new subprocesses.
# A monotonic clock keeps the cache robust to wall-clock changes.
_NOTIF_PROBE_TTL_SECONDS = 60.0
_notif_probe_cache: dict[str, tuple[float, dict[str, list[dict[str, str]]]]] = {}


def _reset_notif_probe_cache() -> None:
    """Clear the notification-probe cache. Primarily a test seam."""
    _notif_probe_cache.clear()


def _list_notification_methods() -> dict[str, list[dict[str, str]]]:
    """Return notification channels grouped by platform (TTL-cached).

    Thin cache wrapper over :func:`_compute_notification_methods`. Serves the
    cached result while it is younger than ``_NOTIF_PROBE_TTL_SECONDS``;
    refreshes (and re-probes) on the first call after expiry.
    """
    now = time.monotonic()
    cached = _notif_probe_cache.get("methods")
    if cached is not None and (now - cached[0]) < _NOTIF_PROBE_TTL_SECONDS:
        return cached[1]
    result = _compute_notification_methods()
    _notif_probe_cache["methods"] = (now, result)
    return result


def _compute_notification_methods() -> dict[str, list[dict[str, str]]]:
    """Return notification channels grouped by platform.

    Uses ``hermes send --list --json`` as the primary source — channel names
    come directly from Hermes's platform adapters, so no platform-specific API
    calls are needed. Platforms with no channels are only included when
    ``hermes status`` confirms they are configured (prevents flooding the picker
    with all ~25 supported-but-unconfigured platforms). Falls back to the text
    parser for older Hermes versions that don't support ``--json``.

    Returns ``{display_name: [{value, label}, ...]}``.
    """
    result_dict: dict[str, list[dict[str, str]]] = {}

    rc, out = _hermes_cli(["send", "--list", "--json"], timeout=10)
    if rc == 0:
        try:
            platforms = json.loads(out or "{}").get("platforms") or {}
        except Exception as exc:
            logger.warning("send-list: failed to parse `hermes send --list --json` output: %s", exc)
            platforms = {}

        if platforms:
            confirmed = _hermes_status_configured_platforms()
            for plat, channels in platforms.items():
                display = plat.capitalize()
                if not channels:
                    if plat.lower() in confirmed:
                        result_dict[display] = [{"value": plat, "label": f"{display} (home channel)"}]
                    continue
                entries: list[dict[str, str]] = []
                seen: set[str] = set()
                for ch in channels:
                    target, label = _channel_target_and_label(plat, ch)
                    if target and target not in seen:
                        seen.add(target)
                        entries.append({"value": target, "label": label or target})
                if entries:
                    result_dict[display] = entries
            return result_dict

    # Fallback: text parser for older Hermes versions without --json support.
    rc2, out2 = _hermes_cli(["send", "--list"], timeout=10)
    if rc2 == 0:
        for method, targets in _parse_send_list_output(out2).items():
            result_dict[method] = [{"value": t, "label": t} for t in targets]
    return result_dict


def _parse_send_list_output(output: str) -> dict[str, list[str]]:
    """Parse `hermes send --list` output into method -> targets dict.

    Skips the intro header line (e.g. "Available messaging targets:"),
    strips trailing parenthesized annotations from method keys (e.g.
    'Discord (Glados):' → 'Discord') and from target values (e.g.
    'slack:tasks (private)' → 'slack:tasks').

    For Slack targets, also strips the per-thread ``/ topic <ts>`` suffix
    and deduplicates to unique channel IDs (e.g. 33 thread rows → 6 unique
    ``slack:<id>`` entries).
    """
    methods: dict[str, list[str]] = {}
    current_method: str | None = None

    for raw_line in output.strip().split("\n"):
        line = raw_line.strip()
        if not line:
            continue

        # Skip the intro header line if present
        if line.lower() == "available messaging targets:":
            continue

        # Method header: ends with ':' and is not indented in the raw line
        if not raw_line[0:1].isspace() and line.endswith(":"):
            # Strip trailing colon, then strip parenthesized profile
            # annotations like '(Glados)', so 'Discord (Glados):' → 'Discord'
            method_raw = line.rstrip(":").strip()
            method_clean = re.sub(r"\s*\([^)]*\)\s*$", "", method_raw).strip()
            current_method = method_clean
            methods[current_method] = []
            continue

        # Indented target line — only process lines indented in the raw output
        # (footer prose like "Use these as..." starts at column 0 and must be skipped)
        if current_method is not None and raw_line[0:1].isspace():
            # Strip trailing annotations like '(private)'
            target = line.strip()
            # Remove parenthesized suffixes: 'slack:tasks (private)' -> 'slack:tasks'
            target = re.sub(r"\s*\([^)]*\)\s*$", "", target).strip()
            # For Slack: strip per-thread suffix ' / topic <ts>' → unique channel id
            if target.startswith("slack:"):
                target = re.sub(r"\s*/\s*topic\s+\S+.*$", "", target).strip()
            if target:
                methods[current_method].append(target)

    # Deduplicate Slack targets to unique channel IDs
    for method in methods:
        if method.lower() == "slack":
            seen: set[str] = set()
            deduped: list[str] = []
            for t in methods[method]:
                if t not in seen:
                    seen.add(t)
                    deduped.append(t)
            methods[method] = deduped

    return methods


# ── Board slug derivation ─────────────────────────────────────────────────────
# Imported from core.util above; this alias keeps internal call sites unchanged.


# ── Kanban helpers (degrade gracefully) ─────────────────────────────────────

def _fetch_project_tasks(slug: str) -> Optional[list[dict[str, Any]]]:
    """Fetch all tasks for a board once; callers share this result."""
    if list_tasks is None:
        return None
    try:
        return list_tasks(slug)
    except Exception as exc:
        logger.warning("kanban: list_tasks failed for board %r: %s", slug, exc)
        return None


def _kanban_summary(slug: str, tasks: Optional[list[dict[str, Any]]] = None) -> Optional[dict[str, int]]:
    """Return counts of kanban cards by status, or None if the board is unavailable.

    Returns an empty dict ``{}`` when the board exists but has no tasks yet —
    this is distinct from ``None`` (board missing / CLI error) so the dashboard
    can show "board ready, 0 tasks" rather than "no kanban data".

    Accepts a pre-fetched ``tasks`` list to avoid a redundant list_tasks call.
    """
    if tasks is None:
        tasks = _fetch_project_tasks(slug)
    if tasks is None:
        return None
    counts: dict[str, int] = {}
    for t in (tasks or []):
        status = (t.get("status") or "unknown").lower()
        counts[status] = counts.get(status, 0) + 1
    return counts


def _needs_attention(slug: str, all_tasks: Optional[list[dict[str, Any]]] = None) -> Optional[list[dict[str, str]]]:
    """Return blocked/gave_up cards with ids and short reasons, or None.

    Accepts a pre-fetched ``all_tasks`` list to avoid a redundant list_tasks
    call when the caller already has all tasks (e.g. _kanban_summary).
    """
    if list_tasks is None:
        return None
    attention_states = {"blocked", "gave_up"}
    tasks = all_tasks
    if tasks is None:
        try:
            tasks = list_tasks(slug)
        except Exception as exc:
            logger.warning("kanban: list_tasks failed for board %r (needs-attention): %s", slug, exc)
            return None
    items: list[dict[str, str]] = []
    for t in (tasks or []):
        state = (t.get("status") or "").lower()
        if state not in attention_states:
            continue
        entry: dict[str, str] = {
            "task_id": t.get("id", ""),
            "title": t.get("title", ""),
            "status": state,
        }
        summary = t.get("summary") or t.get("result") or ""
        if summary:
            entry["reason"] = summary[:200]
        items.append(entry)
    return items if items else None


# ── VCS PR helpers (degrade gracefully) ─────────────────────────────────────

def _project_provider(resolved: dict[str, Any]):
    """Build the VCS provider for a resolved project config, or None."""
    if get_provider is None or not resolved:
        return None
    try:
        return get_provider(resolved)
    except Exception as exc:
        logger.warning("vcs: failed to build provider for project %r — check vcs config and token: %s",
                       resolved.get("name") or resolved.get("repo"), exc)
        return None


def _open_prs(provider) -> Optional[dict[str, Any]]:
    """Return open/in-review PRs with counts, numbers, and CI state.

    Returns None when no provider is available or the repo has no open PRs.

    CI status is fetched in a single batch call (``get_prs_ci_status``) when
    the provider supports it, replacing up to 20 sequential per-PR round-trips.
    If the batch call raises, we gracefully degrade to the legacy sequential
    loop so a transient GraphQL failure never blanks the dashboard.
    """
    if provider is None:
        return None
    try:
        prs = provider.list_prs(state="open", limit=20)
    except Exception as exc:
        logger.warning("vcs: list_prs failed: %s", exc)
        return None
    if not prs:
        return None

    # ── Batch CI-status lookup (single round-trip, #1143) ────────────────
    # Collect PR numbers up-front, then make one batch call.  If the batch
    # call raises or the provider lacks the method, fall back to the legacy
    # sequential per-PR loop so the dashboard still renders.
    ci_map: dict[int, str] = {}
    batch_ok = False
    if provider.supports_ci_status:
        pr_numbers = [int(pr.number) for pr in prs if pr.number is not None]
        if pr_numbers:
            try:
                ci_map = provider.get_prs_ci_status(pr_numbers)
                batch_ok = True
            except Exception as exc:
                logger.warning(
                    "vcs: get_prs_ci_status batch failed (%s) — falling back "
                    "to sequential per-PR lookup", exc)

    pr_list: list[dict[str, Any]] = []
    for pr in prs:
        ci_status = None
        if pr.number is not None and provider.supports_ci_status:
            num = int(pr.number)
            if batch_ok:
                # Batch succeeded — use the pre-fetched map (missing == None).
                ci_status = ci_map.get(num)
            else:
                # Batch failed (or not attempted) — sequential fallback.
                try:
                    ci_status = provider.get_pr_ci_status(num)
                except Exception as exc:
                    logger.warning(
                        "vcs: get_pr_ci_status failed for PR #%s: %s",
                        pr.number, exc)
                    ci_status = None
        pr_list.append({
            "number": pr.number,
            "title": pr.title,
            "branch": pr.head_branch,
            "ci_status": ci_status,
        })
    return {
        "count": len(pr_list),
        "prs": pr_list,
    }


# ── Tracking mode ───────────────────────────────────────────────────────────

def _tracking_mode(project_cfg: dict[str, Any]) -> str:
    """Provider name when a VCS board is configured, else 'kanban'."""
    vcs = project_cfg.get("vcs") or {}
    provider = (vcs.get("provider") or "github").lower().replace("-", "").replace("_", "")
    provider = {"azure": "azuredevops", "ado": "azuredevops"}.get(provider, provider)
    tracking = project_cfg.get("tracking") or {}
    if provider == "github" and tracking.get("github_project_number"):
        return "github"
    if provider == "gitlab" and tracking.get("label_board"):
        return "gitlab"
    if provider == "azuredevops":
        return "azuredevops"  # board columns map to work-item states — always on
    return "kanban"


# ── Endpoints ──────────────────────────────────────────────────────────────


# ── Projects endpoint ───────────────────────────────────────────────────────


@projects_router.get("")
async def get_projects(request: Request) -> list[dict[str, Any]]:
    """Return aggregated status for every project in the registry.

    Performance: cron health is fetched ONCE for all projects (single subprocess),
    kanban tasks are fetched once per project (shared between summary + attention),
    and all projects are built concurrently via asyncio.gather + asyncio.to_thread.
    """
    import asyncio

    registry_repos: list[str] = []
    if registry is not None:
        try:
            registry_repos = registry.list_projects()
        except Exception as exc:
            logger.warning("registry: list_projects failed — project list will be empty: %s", exc)
            registry_repos = []

    # Deduplicate while preserving order.
    seen: set[str] = set()
    unique_repos: list[str] = []
    for r in registry_repos:
        if r not in seen:
            seen.add(r)
            unique_repos.append(r)

    # Fetch all cron health in a single subprocess call, shared across projects.
    cron_all: dict[str, dict[str, Any]] = await asyncio.to_thread(_cron_health_all)

    loader = ConfigLoader()

    def _build_one(repo_path: str) -> dict[str, Any]:
        try:
            resolved = loader.resolve_repo_config(repo_path)
        except Exception as exc:
            logger.warning("config: resolve_repo_config failed for %r — showing registry-only entry: %s",
                           repo_path, exc)
            return _build_registry_only_entry(repo_path, Path(repo_path).name)
        return _build_project_entry(resolved, cron_all)

    results = await asyncio.gather(
        *[asyncio.to_thread(_build_one, rp) for rp in unique_repos],
        return_exceptions=True,
    )
    projects: list[dict[str, Any]] = []
    for r in results:
        if isinstance(r, Exception):
            continue
        projects.append(r)  # type: ignore[arg-type]
    return projects


def _build_project_entry(proj: dict[str, Any],
                          cron_all: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Build a single project status entry from a resolved per-repo config.

    ``cron_all`` is the batched ``{cron_name: health}`` map from
    :func:`_cron_health_all`, fetched once per :func:`get_projects` call. It is
    required so no per-project cron fetch can be reintroduced here.
    """
    name = proj.get("name", "")
    repo = proj.get("repo", "")
    workdir = proj.get("workdir", "")
    slug = _board_slug(repo, name)

    # Fetch tasks once — shared by kanban_summary and needs_attention.
    tasks = _fetch_project_tasks(slug)
    kanban_summary = _kanban_summary(slug, tasks=tasks)
    needs_attention = _needs_attention(slug, all_tasks=tasks)

    # Open PRs (via the project's configured VCS provider)
    open_prs = _open_prs(_project_provider(proj))

    # Cron info — use pre-fetched batch result when available.
    cron_cfg = proj.get("cron") or {}
    cron_name = f"{name}-daedalus"
    health = cron_all.get(cron_name) or {"name": cron_name, "found": False,
                                          "state": None, "last_run": None,
                                          "last_status": None}
    cfg_schedule = (cron_cfg.get("schedule") or "").strip()
    live_schedule = (health.get("schedule") or "").strip()
    schedule = cfg_schedule or live_schedule
    cron: Optional[dict[str, Any]] = None
    if schedule or health.get("found"):
        cron = {
            "name": cron_name,
            "schedule": schedule or None,
            "deliver": cron_cfg.get("deliver") or None,
            "last_run": health.get("last_run") or cron_cfg.get("last_run") or None,
            "health": health,
        }
        cron = {k: v for k, v in cron.items() if v is not None}

    # Sources (strip secrets)
    sources = _strip_secrets(proj.get("sources") or {})

    return {
        "name": name,
        "repo": repo,
        "workdir": workdir,
        "kanban_summary": kanban_summary,
        "open_prs": open_prs,
        "cron": cron,
        "needs_attention": needs_attention,
        "tracking_mode": _tracking_mode(proj),
        "sources": sources if sources else None,
    }


def _build_registry_only_entry(
    repo_path: str,
    name: str,
) -> dict[str, Any]:
    """Build a lightweight entry for a repo in the registry but not in config."""
    slug = _board_slug(repo_path, name)
    tasks = _fetch_project_tasks(slug)

    return {
        "name": name,
        "repo": repo_path,
        "workdir": repo_path,
        "kanban_summary": _kanban_summary(slug, tasks=tasks),
        "open_prs": None,
        "cron": None,
        "needs_attention": _needs_attention(slug, all_tasks=tasks),
        "tracking_mode": "kanban",
        "sources": None,
    }


# ── Per-project config endpoints ────────────────────────────────────────────

# Read-only identity keys that cannot be changed via POST
_READ_ONLY_KEYS = {"name", "repo", "workdir"}

# Known config section keys that should be dicts (used for basic type validation)
_CONFIG_SECTION_KEYS = {
    "vcs", "cron", "execution", "tracking", "delivery", "issues",
    "lifecycle", "model", "sources", "notification", "platform", "stats",
}


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


# Notification event types a cron.notifications[] entry can subscribe to.
# Keep in sync with NOTIFY_EVENTS in scripts/daedalus_dispatch.py.
NOTIFY_EVENTS = ("doc-report", "dispatch-summary", "pipeline-failure", "pr-ready")


def _validate_notifications(value: Any) -> list[str]:
    """Validate a cron.notifications payload. Returns human-readable errors."""
    if not isinstance(value, list):
        return ["cron.notifications must be a list"]
    errors: list[str] = []
    for i, entry in enumerate(value):
        if not isinstance(entry, dict):
            errors.append(f"cron.notifications[{i}] must be a mapping")
            continue
        target = entry.get("target")
        if not isinstance(target, str) or not target.strip():
            errors.append(f"cron.notifications[{i}].target must be a non-empty string "
                          "(e.g. 'slack:C123', 'discord:#general')")
        platform = entry.get("platform")
        if platform is not None and not isinstance(platform, str):
            errors.append(f"cron.notifications[{i}].platform must be a string")
        events = entry.get("events")
        if events is not None and (
            not isinstance(events, list)
            or any(e not in NOTIFY_EVENTS for e in events)
        ):
            errors.append(f"cron.notifications[{i}].events must be a list drawn from: "
                          + ", ".join(NOTIFY_EVENTS))
    return errors


# Canonical parser lives in core/cron_parser.py (issue #1148); this alias keeps
# the historical private name used by call sites and tests.
_parse_cron_jobs = parse_cron_jobs


def _cron_cli(args: list[str]) -> tuple[int, str]:
    """Run a ``hermes cron`` subcommand via the shared CLI wrapper."""
    return _hermes_cli(["cron"] + args, timeout=10)


def _write_schedule_to_config(cfg_path: Path, crontab_schedule: str) -> None:
    """Rewrite the ``cron.schedule`` value in a daedalus.yaml in place.

    Used after ``_reconcile_cron`` normalises an interval schedule to crontab
    syntax so the persisted YAML matches the live cron (mirrors the write-back
    in ``_ensure_dispatch_crons``). Never raises — a write failure just leaves
    the YAML on the interval value, which the plugin-load self-heal corrects.
    """
    try:
        raw_cfg = cfg_path.read_text()
        new_cfg = re.sub(
            r"(schedule\s*:\s*).*",
            lambda m: f'{m.group(1)}"{crontab_schedule}"',
            raw_cfg,
            count=1,
        )
        if new_cfg != raw_cfg:
            cfg_path.write_text(new_cfg)
    except OSError:
        pass


def _reconcile_cron(
    project_name: str, cron_cfg: dict, cfg_path: Optional[Path] = None
) -> dict:
    """Reconcile the real ``hermes cron`` job with the config on save.

    Cron job name = ``f"{project_name}-daedalus"``. Each project owns exactly
    one job. Editing a project UPDATES the existing job in place via the
    native ``hermes cron edit <id>`` — it never stacks a duplicate:

    - one existing job  → ``hermes cron edit <id> --schedule <s>``
      (falls back to remove+create if the installed hermes lacks ``edit``)
    - no existing job   → ``hermes cron create``
    - duplicates found  → keep none, remove all by hex ID, create fresh
    - empty schedule    → remove all matches

    The schedule is normalised to crontab syntax via ``_schedule_to_crontab``
    BEFORE it reaches hermes (issue #134). Hermes treats interval syntax like
    ``60m`` as a *one-shot* job — it runs once, moves to ``[completed]`` and the
    dispatcher silently stops. Crontab syntax (``0 * * * *``) repeats forever.
    This mirrors what ``_ensure_dispatch_crons`` already does on plugin load, so
    a dashboard Save can never produce a one-shot cron.

    A cron CLI failure is captured as an error string; this function NEVER
    raises, so a broken ``hermes`` binary cannot fail the config save.

    Args:
        project_name: The project name from the config.
        cron_cfg: The ``cron`` dict from the resolved project config.
            Keys used: ``schedule`` (str), ``deliver`` (str, optional),
            ``notifications`` (list, optional — when set, the dispatcher
            self-delivers and the cron gets NO --deliver target).
        cfg_path: Optional path to the project's ``daedalus.yaml``. When given
            and the schedule was normalised (interval → crontab), the new
            crontab schedule is written back so the YAML stays consistent with
            the live cron (mirrors ``_ensure_dispatch_crons``).

    Returns:
        ``{"cron": "<created|updated|removed|skipped>", "name": "<cron_name>",
        "error": <str|None>}``
    """
    cron_name = f"{project_name}-daedalus"
    result: dict[str, Any] = {
        "cron": "skipped",
        "name": cron_name,
        "error": None,
    }

    raw_schedule = cron_cfg.get("schedule", "").strip() if cron_cfg else ""
    # Convert interval syntax ("60m", "every 2h") to crontab so the job repeats
    # forever — otherwise hermes creates a one-shot job (issue #134).
    schedule = _schedule_to_crontab(raw_schedule) if raw_schedule else ""
    # Keep the YAML in step with the live cron when we normalised the schedule.
    if cfg_path is not None and schedule and schedule != raw_schedule:
        _write_schedule_to_config(cfg_path, schedule)
    # With notifications[] the dispatcher fans out itself — the cron job must
    # not double-deliver its stdout.
    has_notifications = bool(cron_cfg.get("notifications")) if cron_cfg else False
    deliver = "" if has_notifications else (cron_cfg.get("deliver", "").strip() if cron_cfg else "")

    # Run the dispatcher from this repo's root so it auto-scopes to this project
    # instead of sweeping every registered repo (issue #137). The repo root is the
    # parent of the project's ``.hermes/`` dir, where daedalus.yaml lives.
    workdir = str(cfg_path.parent.parent.resolve()) if cfg_path is not None else ""

    # 1. Find existing jobs by name.
    matching_ids: list[str] = []
    rc, out = _cron_cli(["list", "--all"])
    if rc == 0:
        matching_ids = [j["job_id"] for j in _parse_cron_jobs(out) if j.get("name") == cron_name]

    # 2. Empty schedule → remove all matches.
    if not schedule:
        for job_id in matching_ids:
            _cron_cli(["remove", job_id])
        result["cron"] = "removed"
        return result

    # 3. Exactly one job → update it in place (native `hermes cron edit`).
    if len(matching_ids) == 1:
        edit_args = ["edit", matching_ids[0], "--schedule", schedule]
        if workdir:
            edit_args += ["--workdir", workdir]
        if deliver:
            edit_args += ["--deliver", deliver]
        rc, out = _cron_cli(edit_args)
        if rc == 0:
            result["cron"] = "updated"
            return result
        # Older hermes without `cron edit` (or edit failure): fall through to
        # remove+create so the save still converges on one correct job.

    # 4. Zero, several, or un-editable → remove all matches, create fresh.
    for job_id in matching_ids:
        _cron_cli(["remove", job_id])

    cmd = [
        "create", schedule,
        "--name", cron_name,
        "--script", "daedalus-cron.sh",
        "--no-agent",
    ]
    if workdir:
        cmd += ["--workdir", workdir]
    if deliver:
        cmd += ["--deliver", deliver]

    rc, out = _cron_cli(cmd)
    if rc != 0:
        result["error"] = out.strip()[:500] or f"exit code {rc}"
    else:
        result["cron"] = "created" if "created" in out.lower() else "updated"
    return result


_PLUGIN_TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "templates" / "daedalus.yaml"


@project_config_router.post("/create")
async def create_project(request: Request) -> dict[str, Any]:
    """Create or adopt a project — the API equivalent of scripts/setup.sh.

    If ``<workdir>/.hermes/daedalus.yaml`` does not yet exist, scaffolds it
    from the packaged template and deep-merges any config sections from the
    request body.  If it already exists, reads it as-is (adopted path) and
    skips scaffolding — so re-adding an existing repo just registers it and
    provisions the board + cron without overwriting any manual edits.

    Returns ``status: "created"`` for new projects, ``status: "adopted"`` for
    existing ones.  Returns 422 for missing/invalid input.  Never stores
    secrets in the config file.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="Body must be a JSON object")

    name = (body.get("name") or "").strip() if isinstance(body.get("name"), str) else ""
    repo = (body.get("repo") or "").strip() if isinstance(body.get("repo"), str) else ""
    workdir = (body.get("workdir") or "").strip() if isinstance(body.get("workdir"), str) else ""
    if not name:
        raise HTTPException(status_code=422, detail="'name' is required")
    if not workdir:
        raise HTTPException(status_code=422, detail="'workdir' is required")

    workdir_path = Path(workdir).expanduser()
    if not workdir_path.is_absolute():
        raise HTTPException(status_code=422, detail="'workdir' must be an absolute path")
    workdir_path = workdir_path.resolve()
    if not workdir_path.is_dir():
        raise HTTPException(status_code=422,
                            detail=f"'workdir' does not exist: {workdir_path}")

    # Auto-detect the provider + repo identity from the repo's origin remote
    # when the request doesn't pin a provider explicitly.
    body_vcs = body.get("vcs") if isinstance(body.get("vcs"), dict) else {}
    detected = None
    if detect_repo_vcs is not None and not (body_vcs or {}).get("provider"):
        try:
            detected = detect_repo_vcs(str(workdir_path))
        except Exception as exc:
            logger.warning("vcs: detect_repo_vcs failed for %s: %s", workdir_path, exc)
            detected = None
    if not repo and detected:
        repo = detected["repo"]
    if not repo:
        raise HTTPException(
            status_code=422,
            detail="'repo' is required (no origin remote found to auto-detect it from)",
        )

    cfg_path = workdir_path / ".hermes" / "daedalus.yaml"
    adopted = cfg_path.exists()

    if adopted:
        # Config already exists — read it and register/provision without overwriting.
        try:
            cfg = yaml.safe_load(cfg_path.read_text()) or {}
        except Exception:
            raise HTTPException(status_code=500,
                                detail=f"Existing config at {cfg_path} could not be parsed")
        # Fill in name/repo from existing config if not supplied in request.
        if not name:
            name = (cfg.get("name") or "").strip()
        if not repo:
            repo = (cfg.get("repo") or "").strip()
    else:
        # Scaffold from the packaged template (same one setup.sh uses).
        try:
            template = _PLUGIN_TEMPLATE_PATH.read_text()
        except OSError:
            raise HTTPException(status_code=500,
                                detail=f"Plugin template missing at {_PLUGIN_TEMPLATE_PATH}")
        rendered = (template.replace("{{NAME}}", name)
                            .replace("{{REPO}}", repo)
                            .replace("{{WORKDIR}}", str(workdir_path)))
        try:
            cfg = yaml.safe_load(rendered) or {}
        except Exception:
            raise HTTPException(status_code=500, detail="Failed to render config template")

        # Deep-merge optional config sections from the request body.
        for key in sorted(_CONFIG_SECTION_KEYS):
            if key in body:
                if body[key] is not None and not isinstance(body[key], dict):
                    raise HTTPException(
                        status_code=422,
                        detail=f"'{key}' must be a mapping, got {type(body[key]).__name__}",
                    )
                cfg[key] = deep_merge(cfg.get(key) or {}, body[key] or {})

        # Apply the auto-detected provider.
        if detected:
            vcs_cfg = cfg.setdefault("vcs", {})
            vcs_cfg["provider"] = detected["provider"]
            for k, v in (detected.get("vcs_extra") or {}).items():
                vcs_cfg.setdefault(k, v)

        # Validate notifications + provider config before anything touches disk.
        errors: list[str] = []
        cron_cfg = cfg.get("cron") or {}
        if cron_cfg.get("notifications") is not None:
            errors += _validate_notifications(cron_cfg["notifications"])
        errors += validate_vcs(cfg)
        errors += validate_failover(cfg)
        if errors:
            raise HTTPException(status_code=422, detail={"errors": errors})

        safe = _strip_secrets(cfg)
        try:
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            cfg_path.write_text(yaml.dump(safe, default_flow_style=False,
                                          sort_keys=False, allow_unicode=True))
        except OSError:
            raise HTTPException(status_code=500, detail="Failed to write config file")

    # Register in the daedalus registry (idempotent).
    registered = False
    if registry is not None:
        try:
            registry.add_project(str(workdir_path))
            registered = True
        except Exception as exc:
            logger.warning("registry: add_project failed for %s: %s", workdir_path, exc)
            registered = False

    # Each project gets its OWN kanban board (idempotent create).
    cron_cfg = cfg.get("cron") or {}
    board_slug = _board_slug(repo, name)
    board_ok = bool(ensure_board(board_slug)) if ensure_board is not None else False

    # …and its OWN cron job.
    cron_result = _reconcile_cron(name, cron_cfg, cfg_path)

    return {
        "status": "adopted" if adopted else "created",
        "config_path": str(cfg_path),
        "registered": registered,
        "board": board_slug,
        "board_created": board_ok,
        "cron": cron_result,
    }


@project_config_router.get("/{name}/config")
async def get_project_config(request: Request, name: str) -> dict[str, Any]:
    """Return the resolved per-project config, stripped of secrets.

    Reads ``<repo>/.hermes/daedalus.yaml`` via ConfigLoader.resolve_repo_config,
    strips secret keys, and returns the result.

    Returns 404 if the project name is not found in the registry.
    """
    repo_path = _resolve_project_path(name)
    loader = ConfigLoader()
    try:
        resolved = loader.resolve_repo_config(str(repo_path))
    except FileNotFoundError:
        raise HTTPException(
            status_code=404,
            detail=f"No daedalus config found for project '{name}' at {repo_path}",
        )
    return _strip_secrets(resolved)


@project_config_router.post("/{name}/config")
async def post_project_config(request: Request, name: str) -> dict[str, Any]:
    """Update per-project config, rejecting changes to read-only identity fields.

    Reads ``<repo>/.hermes/daedalus.yaml``, validates that the incoming
    payload does not attempt to change ``repo`` or ``workdir``, deep-merges
    editable fields into the on-disk config, strips secrets, and saves.

    Returns 404 for unknown projects, 422 for read-only violations or invalid values.
    """
    repo_path = _resolve_project_path(name)
    cfg_path = repo_path / ".hermes" / "daedalus.yaml"

    # Parse incoming body as plain dict (flexible — not a Pydantic model)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid JSON body")

    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="Body must be a JSON object")

    # Read the on-disk config to compare read-only fields
    if not cfg_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"No daedalus config found for project '{name}' at {repo_path}",
        )

    try:
        existing = yaml.safe_load(cfg_path.read_text()) or {}
    except Exception:
        raise HTTPException(status_code=422, detail="Failed to parse existing config YAML")

    # Reject attempts to change read-only identity fields
    for key in _READ_ONLY_KEYS:
        if key in body and body[key] != existing.get(key):
            raise HTTPException(
                status_code=422,
                detail=f"'{key}' is read-only and cannot be changed via this endpoint",
            )

    # Validate config sections that should be dicts
    for key, value in body.items():
        if key in _CONFIG_SECTION_KEYS and value is not None and not isinstance(value, dict):
            raise HTTPException(
                status_code=422,
                detail=f"'{key}' must be a mapping, got {type(value).__name__}",
            )

    # Validate multi-target notifications when present
    cron_body = body.get("cron")
    if isinstance(cron_body, dict) and cron_body.get("notifications") is not None:
        notif_errors = _validate_notifications(cron_body["notifications"])
        if notif_errors:
            raise HTTPException(status_code=422, detail={"errors": notif_errors})

    # Deep-merge editable fields into existing config
    merged = deep_merge(existing, body)

    # Strip secrets before persisting
    safe = _strip_secrets(merged)

    try:
        cfg_path.write_text(yaml.dump(safe, default_flow_style=False, sort_keys=False, allow_unicode=True))
    except OSError:
        raise HTTPException(status_code=500, detail="Failed to write config file")

    # Reconcile the cron job AFTER the YAML is safely on disk.
    cron_cfg = merged.get("cron") or {}
    cron_result = _reconcile_cron(name, cron_cfg, cfg_path)

    return {"status": "saved", "path": str(cfg_path), "cron": cron_result}


@project_config_router.post("/{name}/run")
async def run_dispatch_dry_run(name: str) -> dict[str, Any]:
    """Trigger a dry-run dispatch tick for a project and return the log output."""
    workdir = _resolve_project_path(name)
    dispatch_script = Path(__file__).resolve().parent.parent / "scripts" / "daedalus_dispatch.py"
    if not dispatch_script.exists():
        raise HTTPException(status_code=500, detail="dispatch script not found")
    try:
        cfg = ConfigLoader().resolve_repo_config(str(workdir))
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"project config not found: {exc}")
    _ = cfg  # validated; workdir is the run target
    try:
        result = subprocess.run(
            [sys.executable, str(dispatch_script), str(workdir), "--dry-run"],
            capture_output=True, text=True, timeout=120, env={**os.environ},
        )
        combined = result.stdout + (("\n" + result.stderr) if result.stderr.strip() else "")
        return {"ok": result.returncode == 0, "output": combined.strip(),
                "error": result.stderr.strip() or None}
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": "", "error": "dispatch timed out after 120s"}
    except Exception as exc:
        return {"ok": False, "output": "", "error": str(exc)}


@project_config_router.delete("/{name}")
async def delete_project(name: str) -> dict[str, Any]:
    """Remove a project from the registry and clean up its cron job and kanban board.

    The project's .hermes/daedalus.yaml is intentionally left untouched so it
    can be re-added at any time via '+ Add Project'.
    """
    workdir = _resolve_project_path(name)

    removed: list[str] = []
    skipped: list[str] = []

    # 1. Remove cron job.
    cron_name = f"{name}-daedalus"
    ok, _ = _hermes_cmd("cron", "remove", cron_name)
    if ok:
        removed.append(f"cron job: {cron_name}")
    else:
        skipped.append(f"cron job: {cron_name} (not found or already removed)")

    # 2. Archive kanban board (no --delete so it's recoverable via hermes kanban boards restore).
    loader = ConfigLoader()
    try:
        cfg = loader.resolve_repo_config(str(workdir))
        repo = cfg.get("repo") or ""
    except Exception:
        repo = ""
    slug = _board_slug(repo, name)
    ok2, _ = _hermes_cmd("kanban", "boards", "rm", slug)
    if ok2:
        removed.append(f"kanban board archived: {slug}")
    else:
        skipped.append(f"kanban board: {slug} (not found or already removed)")

    # 3. Remove from registry — do this last so lookups above still work.
    if registry is not None:
        try:
            registry.remove_project(str(workdir))
            removed.append(f"registry entry: {workdir}")
        except Exception as exc:
            skipped.append(f"registry entry: {exc}")
    else:
        skipped.append("registry: module unavailable")

    return {"ok": True, "removed": removed, "skipped": skipped}


# ── Meta endpoints ───────────────────────────────────────────────────────────


@meta_router.get("/notifications")
async def get_notifications(request: Request) -> dict[str, list[dict[str, str]]]:
    """Return notification methods and their deliverable targets.

    Calls ``hermes send --list`` and parses the output into a dict mapping
    method names (e.g. 'Slack', 'Discord') to lists of ``{value, label}``
    objects. Slack labels are resolved to human-readable names via the
    Slack Web API (cached); non-Slack methods use the raw target as label.

    Degrades gracefully to an empty dict if the command is unavailable.
    """
    return _list_notification_methods()


_TEST_MESSAGE = (
    "\u2705 Daedalus test \u2014 your notification target works."
    " (sent from the project config)"
)


@meta_router.post("/test-deliver")
async def test_deliver(request: Request) -> dict[str, Any]:
    """Send a one-shot test message to a delivery target via ``hermes send``.

    Accepts JSON body ``{"deliver": "<target>"}`` (e.g. ``slack:tasks``,
    ``discord:#general``).  Runs ``hermes send -t <deliver> "<test message>"``
    via ``subprocess.run`` with LIST-ARGS (no shell, 10s timeout) in root
    context.

    Returns:
        ``{"ok": bool, "target": "<deliver>", "error": <str|None>}``

        * ``ok=true`` if the command succeeds (exit 0 and output contains
          "sent").
        * ``ok=false`` with a short ``error`` string otherwise.
        * If ``deliver`` is empty or missing, returns
          ``{"ok": false, "error": "no delivery target selected"}`` without
          running the send command.

    This endpoint NEVER raises — all errors are returned in the response body.
    No secrets are logged.
    """
    # Parse the body — degrade gracefully on bad JSON.
    try:
        body = await request.json()
    except Exception:
        return {"ok": False, "target": "", "error": "invalid JSON body"}

    if not isinstance(body, dict):
        return {"ok": False, "target": "", "error": "body must be a JSON object"}

    deliver = (body.get("deliver") or "").strip()
    if not deliver:
        return {"ok": False, "target": "", "error": "no delivery target selected"}

    try:
        result = subprocess.run(
            ["hermes", "send", "-t", deliver, _TEST_MESSAGE],
            capture_output=True,
            text=True,
            timeout=10,
        )
        combined = (result.stdout + result.stderr).lower()
        if result.returncode == 0 and "sent" in combined:
            return {"ok": True, "target": deliver, "error": None}
        else:
            error = (
                result.stderr.strip()
                or result.stdout.strip()
                or f"exit code {result.returncode}"
            )[:500]
            return {"ok": False, "target": deliver, "error": error}
    except FileNotFoundError:
        return {"ok": False, "target": deliver, "error": "hermes CLI not found"}
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "target": deliver,
            "error": "hermes send timed out after 10s",
        }
    except OSError as exc:
        return {
            "ok": False,
            "target": deliver,
            "error": f"hermes send failed: {exc}"[:500],
        }


# ── Meta helpers ─────────────────────────────────────────────────────────────


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


@meta_router.get("/branches")
async def get_meta_branches(request: Request, project: str) -> dict[str, Any]:
    """Return repo branches via the project's VCS provider.

    Degrades gracefully to an empty list on any error.
    """
    try:
        resolved = _project_resolved(project)
    except HTTPException:
        return {"repo": "", "branches": []}
    repo = resolved.get("repo", "")
    provider = _project_provider(resolved)
    if provider is None or not provider.supports_branches:
        return {"repo": repo, "branches": []}
    try:
        return {"repo": repo, "branches": provider.list_branches()}
    except Exception:
        return {"repo": repo, "branches": []}


@meta_router.get("/labels")
async def get_meta_labels(request: Request, project: str) -> dict[str, Any]:
    """Return repo labels with names and colors via the project's VCS provider.

    Degrades gracefully to an empty list on any error.
    """
    try:
        resolved = _project_resolved(project)
    except HTTPException:
        return {"repo": "", "labels": []}
    repo = resolved.get("repo", "")
    provider = _project_provider(resolved)
    if provider is None:
        import logging
        logging.getLogger("daedalus.meta").warning(
            "get_meta_labels: no provider for project %r — check vcs config and token", project)
        return {"repo": repo, "labels": []}
    if not provider.supports_labels:
        return {"repo": repo, "labels": []}
    try:
        labels = [
            {"name": lbl.name, "color": lbl.color} for lbl in provider.list_labels()
        ]
        return {"repo": repo, "labels": labels}
    except Exception as exc:
        import logging
        logging.getLogger("daedalus.meta").warning(
            "get_meta_labels: list_labels raised for project %r: %s", project, exc)
        return {"repo": repo, "labels": []}


@meta_router.get("/projects")
async def get_meta_projects(request: Request, project: str) -> dict[str, Any]:
    """Return the provider's project boards (e.g. GitHub Projects v2).

    Degrades gracefully to an empty list on any error.
    """
    try:
        resolved = _project_resolved(project)
    except HTTPException:
        return {"owner": "", "projects": []}
    repo = resolved.get("repo", "")
    owner = repo.split("/")[0] if "/" in repo else repo
    provider = _project_provider(resolved)
    if provider is None or not provider.supports_boards:
        return {"owner": owner, "projects": []}
    try:
        boards = [{"number": b.number, "title": b.title} for b in provider.list_boards()]
        return {"owner": owner, "projects": boards}
    except Exception:
        return {"owner": owner, "projects": []}


@meta_router.get("/statuses")
async def get_meta_statuses(
    request: Request, project: str, github_project_number: Optional[int] = None
) -> dict[str, Any]:
    """Return Status field options for the configured board.

    Requires ``github_project_number`` to query the board's fields.
    Degrades gracefully to an empty list on any error.
    """
    if github_project_number is None:
        return {"statuses": []}
    try:
        resolved = _project_resolved(project)
    except HTTPException:
        return {"statuses": []}
    provider = _project_provider(resolved)
    if provider is None or not provider.supports_boards:
        return {"statuses": []}
    try:
        for f in provider.get_board_fields(str(github_project_number)):
            if f.name.lower() == "status":
                return {"statuses": [o.name for o in f.options]}
        return {"statuses": []}
    except Exception:
        return {"statuses": []}


# ── Cron health ──────────────────────────────────────────────────────────────


def _cron_health_all() -> dict[str, dict[str, Any]]:
    """Run ``hermes cron list --all`` once and return a map of {cron_name: health}."""
    rc, out = _cron_cli(["list", "--all"])
    if rc != 0:
        return {}
    return {j["name"]: {**j, "found": True} for j in _parse_cron_jobs(out) if j.get("name")}


@meta_router.get("/detect")
async def get_meta_detect(request: Request, workdir: str = "") -> dict[str, Any]:
    """Auto-detect VCS provider, repo slug, and project name from a local path.

    Used by the Add Project form to pre-fill fields when the user picks a folder.
    Returns {"detected": false} on any error or when workdir is empty.
    """
    if not workdir:
        return {"detected": False}
    path = Path(workdir).expanduser().resolve()
    if not path.is_dir():
        return {"detected": False}
    if detect_repo_vcs is None:
        return {"detected": False}
    try:
        result = detect_repo_vcs(str(path))
        repo = result.get("repo", "")
        name = repo.split("/")[-1] if "/" in repo else path.name
        return {
            "detected": True,
            "provider": result.get("provider", ""),
            "repo": repo,
            "name": name,
            "vcs_extra": result.get("vcs_extra") or {},
        }
    except Exception:
        return {"detected": False}


@meta_router.get("/pick-directory")
async def get_meta_pick_directory(request: Request) -> dict[str, Any]:
    """Open a native OS folder-picker dialog and return the selected path.

    macOS only (uses osascript). Returns {"path": ""} when the user cancels.
    """
    import sys
    if sys.platform != "darwin":
        return {"path": "", "error": "native folder picker is only supported on macOS"}
    try:
        result = subprocess.run(
            ["osascript", "-e",
             'POSIX path of (choose folder with prompt "Select repository folder")'],
            capture_output=True, text=True, timeout=120,
        )
        path = result.stdout.strip().rstrip("/")
        return {"path": path}
    except subprocess.TimeoutExpired:
        return {"path": ""}
    except Exception as exc:
        return {"path": "", "error": str(exc)}


# ── Roster provisioning ──────────────────────────────────────────────────────

_ALL_DAEDALUS_PROFILES = [
    "validator-daedalus",
    "project-manager-daedalus", "planner-daedalus", "developer-daedalus",
    "qa-daedalus", "reviewer-daedalus", "security-analyst-daedalus",
    "accessibility-daedalus", "documentation-daedalus",
]
_PROVISION_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "provision_roster.sh"


@meta_router.get("/roster-status")
async def get_roster_status(request: Request) -> dict[str, Any]:
    """Check which of the nine specialist profiles are provisioned.

    Returns ``{"all_provisioned": bool, "profiles": {name: bool}}``.
    """
    profiles_dir = _real_home() / ".hermes" / "profiles"
    status: dict[str, bool] = {}
    all_ok = True
    for profile in _ALL_DAEDALUS_PROFILES:
        exists = (profiles_dir / profile).is_dir()
        status[profile] = exists
        if not exists:
            all_ok = False
    return {"all_provisioned": all_ok, "profiles": status}


@meta_router.post("/provision-roster")
async def post_provision_roster(request: Request) -> dict[str, Any]:
    """Run provision_roster.sh to install the nine specialist profiles.

    Reads any tokens already in ~/.hermes/.env and passes them as environment
    variables so the profiles get push auth without the user re-typing them.

    Returns ``{"ok": bool, "output": str, "error": str|None}``.
    """
    if not _PROVISION_SCRIPT.exists():
        return {
            "ok": False,
            "output": "",
            "error": f"provision_roster.sh not found at {_PROVISION_SCRIPT}",
        }

    env = {**os.environ,
           **_parse_env_file(_real_home() / ".hermes" / ".env")}

    try:
        result = subprocess.run(
            ["bash", str(_PROVISION_SCRIPT)],
            capture_output=True,
            text=True,
            timeout=180,
            env=env,
        )
        combined = (result.stdout + result.stderr).strip()
        if result.returncode == 0:
            return {"ok": True, "output": combined[:3000], "error": None}
        return {
            "ok": False,
            "output": combined[:3000],
            "error": f"script exited with code {result.returncode}",
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": "", "error": "provision_roster.sh timed out after 180s"}
    except Exception as exc:
        return {"ok": False, "output": "", "error": str(exc)[:500]}


def _hermes_cmd(*args: str, timeout: int = 30) -> tuple[bool, str]:
    """Thin wrapper returning (success, output). Delegates to the shared _hermes_cli."""
    rc, out = _hermes_cli(list(args), timeout=timeout)
    return rc == 0, out


@meta_router.get("/version")
async def get_meta_version() -> dict[str, Any]:
    """Return the installed plugin version from plugin.yaml."""
    plugin_yaml = Path(__file__).resolve().parent.parent / "plugin.yaml"
    try:
        with open(plugin_yaml) as f:
            data = yaml.safe_load(f) or {}
        return {"version": data.get("version") or "unknown"}
    except Exception:
        return {"version": "unknown"}


def _semver_key(v: str) -> tuple:
    """Return a comparable tuple for a semver-ish version string like '1.0.0-beta.22'."""
    v = (v or "").lstrip("v").strip()
    parts = re.split(r"[.\-]", v)
    result: list = []
    for p in parts:
        try:
            result.append((1, int(p)))   # numeric: sort numerically
        except ValueError:
            order = {"alpha": -3, "beta": -2, "rc": -1}
            result.append((0, order.get(p.lower(), -4)))  # pre-release label
    return tuple(result)


@meta_router.get("/check-update")
async def get_check_update() -> dict[str, Any]:
    """Compare the installed version against the latest GitHub release.

    Tries the Releases API first (includes pre-releases, sorted by semver),
    falls back to the Tags API.  Fails gracefully on network errors.

    Returns:
        ``{"has_update": bool, "check_failed": bool, "current": str, "latest": str|null}``
    """
    plugin_yaml = Path(__file__).resolve().parent.parent / "plugin.yaml"
    try:
        with open(plugin_yaml) as f:
            data = yaml.safe_load(f) or {}
        current = (data.get("version") or "unknown").strip()
        source = (data.get("source") or "").strip()
    except Exception:
        return {"has_update": False, "check_failed": True, "current": "unknown", "latest": None}

    if not source:
        return {"has_update": False, "check_failed": False, "current": current, "latest": None}

    m = re.match(r"https?://github\.com/([^/]+/[^/.]+?)(?:\.git)?$", source)
    if not m:
        return {"has_update": False, "check_failed": False, "current": current, "latest": None}

    repo = m.group(1)
    headers: dict = {"Accept": "application/vnd.github+json",
                     "User-Agent": "daedalus-plugin/update-check"}
    token = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    latest: str | None = None

    # 1. Try releases API (returns drafts=false; includes pre-releases when listed).
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/releases?per_page=20",
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            releases = json.loads(resp.read())
        if isinstance(releases, list) and releases:
            names = [
                (r.get("tag_name") or "").lstrip("v").strip()
                for r in releases
                if not r.get("draft") and r.get("tag_name")
            ]
            if names:
                latest = max(names, key=_semver_key)
    except Exception:
        pass  # fall through to tags

    # 2. Fall back to tags API.
    if not latest:
        try:
            req = urllib.request.Request(
                f"https://api.github.com/repos/{repo}/tags?per_page=20",
                headers=headers,
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                tags = json.loads(resp.read())
            if isinstance(tags, list) and tags:
                names = [(t.get("name") or "").lstrip("v").strip() for t in tags if t.get("name")]
                if names:
                    latest = max(names, key=_semver_key)
        except Exception:
            return {"has_update": False, "check_failed": True, "current": current, "latest": None}

    if not latest:
        return {"has_update": False, "check_failed": False, "current": current, "latest": None}

    has_update = _semver_key(latest) > _semver_key(current)
    return {"has_update": has_update, "check_failed": False, "current": current, "latest": latest}


@meta_router.post("/update-plugin")
async def post_update_plugin() -> dict[str, Any]:
    """Update the Daedalus plugin then re-provision the roster.

    For git-cloned installs: runs ``hermes plugins update daedalus``.
    For non-git (cloud/copy) installs: clones the source repo into a temp dir
    and rsyncs the new code into the plugin directory, then re-runs
    ``provision_roster.sh`` so new agent profiles are created.
    """
    rc, update_out = _hermes_cli(["plugins", "update", "daedalus"], timeout=120)
    # If hermes plugins update fails for any reason (non-git install, hermes-dashboard
    # install, or any other error) fall back to git clone + rsync from the source URL.
    if rc != 0:
        plugin_yaml = Path(__file__).resolve().parent.parent / "plugin.yaml"
        try:
            with open(plugin_yaml) as f:
                py_data = yaml.safe_load(f) or {}
            source_url = (py_data.get("source") or "").strip()
        except Exception as exc:
            return {"ok": False, "output": f"Could not read plugin.yaml: {exc}"}
        if not source_url:
            return {"ok": False, "output": "No source URL in plugin.yaml — cannot auto-update."}
        plugin_dir = str(Path(__file__).resolve().parent.parent)
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                clone_result = subprocess.run(
                    ["git", "clone", "--depth=1", source_url, tmp_dir],
                    capture_output=True, text=True, timeout=120,
                )
                if clone_result.returncode != 0:
                    return {"ok": False, "output": (clone_result.stdout + clone_result.stderr).strip()}
                rsync_result = subprocess.run(
                    ["rsync", "-a", "--delete",
                     "--exclude=.git", "--exclude=__pycache__", "--exclude=*.pyc",
                     tmp_dir + "/", plugin_dir + "/"],
                    capture_output=True, text=True, timeout=60,
                )
                if rsync_result.returncode != 0:
                    return {"ok": False, "output": (rsync_result.stdout + rsync_result.stderr).strip()}
                update_out = f"Cloned from {source_url} and synced to {plugin_dir}."
        except Exception as exc:
            return {"ok": False, "output": f"Update failed: {exc}"}

    # Always re-run the provisioner so any newly added profiles are installed
    # and any broken symlinks that block profile creation are cleaned up.
    provision_out = ""
    provision_ok = True
    if _PROVISION_SCRIPT.exists():
        env = {**os.environ, **_parse_env_file(_real_home() / ".hermes" / ".env")}
        try:
            pr = subprocess.run(
                ["bash", str(_PROVISION_SCRIPT)],
                capture_output=True, text=True, timeout=180, env=env,
            )
            provision_out = (pr.stdout + pr.stderr).strip()
            provision_ok = pr.returncode == 0
        except Exception as exc:
            provision_out = f"[provisioner error: {exc}]"
            provision_ok = False

    combined = (update_out + ("\n" + provision_out if provision_out else "")).strip()
    return {"ok": provision_ok, "output": combined[:4000]}


@meta_router.get("/restart")
async def post_restart() -> dict[str, Any]:
    """Restart the Hermes gateway process.

    Spawns 'hermes gateway restart' in the background and returns immediately —
    the gateway process dies so the HTTP response may not always be received by
    the client.
    """
    try:
        # Detach so the child outlives this process being killed by the restart.
        subprocess.Popen(
            ["hermes", "gateway", "restart"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        return {"ok": True}
    except FileNotFoundError:
        return {"ok": False, "output": "hermes CLI not found"}
    except Exception as exc:
        return {"ok": False, "output": str(exc)}


@meta_router.post("/uninstall")
async def post_uninstall() -> dict[str, Any]:
    """Clean up all Daedalus host-side state and remove the plugin.

    Removes in order: cron jobs → profiles → kanban boards → registry dir →
    config.yaml plugin entry → plugin package (hermes plugins remove + rm -rf
    fallback). Returns a summary of what was removed and what was skipped.

    Safe to call even if some items are already gone (idempotent).
    """
    removed: list[str] = []
    skipped: list[str] = []
    hermes_home = _real_home() / ".hermes"

    # ── 1. Cron jobs ────────────────────────────────────────────────────────
    rc_cron, cron_out = _cron_cli(["list", "--all"])
    if rc_cron == 0 and cron_out:
        daedalus_jobs = [
            j["name"] for j in _parse_cron_jobs(cron_out)
            if j.get("name") and (
                j["name"].endswith("-daedalus") or
                re.search(r"daedalus-[^/]*\.sh$", j.get("script") or "")
            )
        ]
        for job in sorted(set(daedalus_jobs)):
            ok2, _ = _hermes_cmd("cron", "remove", job)
            if ok2:
                removed.append(f"cron job: {job}")
            else:
                skipped.append(f"cron job: {job} (removal failed)")

    # ── 2. Profiles ─────────────────────────────────────────────────────────
    ok, prof_out = _hermes_cmd("profile", "list")
    existing_profiles = prof_out if ok else ""
    for role in _ALL_DAEDALUS_PROFILES:
        if role in existing_profiles:
            ok2, _ = _hermes_cmd("profile", "delete", role, "-y")
            if ok2:
                removed.append(f"profile: {role}")
            else:
                skipped.append(f"profile: {role} (deletion failed)")

    # ── 3. Kanban boards ─────────────────────────────────────────────────────
    # Scan live boards list to catch orphaned boards too
    ok, boards_out = _hermes_cmd("kanban", "boards", "list")
    if ok and boards_out:
        for line in boards_out.splitlines():
            if not line.strip() or line.startswith(("SLUG", "Switch", "Current")):
                continue
            slug = re.sub(r"^[\s●]+", "", line).split()[0]
            if slug and slug != "default":
                ok2, _ = _hermes_cmd("kanban", "boards", "rm", slug, "--delete")
                if ok2:
                    removed.append(f"kanban board: {slug}")
                else:
                    skipped.append(f"kanban board: {slug} (removal failed)")

    # ── 4. Registry directory ─────────────────────────────────────────────────
    registry_dir = hermes_home / "daedalus"
    if registry_dir.is_dir():
        try:
            shutil.rmtree(registry_dir)
            removed.append(f"registry dir: {registry_dir}")
        except OSError as exc:
            skipped.append(f"registry dir: {exc}")

    # ── 5. Strip plugins.enabled/.disabled entry from config.yaml ─────────────
    cfg_path = hermes_home / "config.yaml"
    if cfg_path.exists():
        try:
            lines = cfg_path.read_text().splitlines(keepends=True)
            in_plugins = in_list = False
            new_lines = []
            for ln in lines:
                stripped = ln.rstrip()
                if re.match(r"^[^\s#]", stripped):
                    in_plugins = stripped.startswith("plugins:")
                    in_list = False
                if in_plugins and re.match(r"^\s+(enabled|disabled):", stripped):
                    in_list = True
                if in_plugins and in_list and re.match(r"^\s+-\s+daedalus\s*$", stripped):
                    continue  # drop this line
                new_lines.append(ln)
            new_text = "".join(new_lines)
            if new_text != cfg_path.read_text():
                cfg_path.write_text(new_text)
                removed.append("config.yaml daedalus plugin entry")
        except OSError:
            skipped.append("config.yaml (could not edit)")

    # ── 6. Plugin package ─────────────────────────────────────────────────────
    _hermes_cmd("plugins", "disable", "daedalus")
    ok2, _ = _hermes_cmd("plugins", "remove", "daedalus", timeout=15)
    if ok2:
        removed.append("plugin package (hermes plugins remove)")
    else:
        # Belt-and-suspenders: nuke the directory directly
        plugin_dir = hermes_home / "plugins" / "daedalus"
        if plugin_dir.is_dir():
            try:
                shutil.rmtree(plugin_dir)
                removed.append(f"plugin directory: {plugin_dir}")
            except OSError as exc:
                skipped.append(f"plugin directory rm failed: {exc}")
        else:
            skipped.append("plugin package (hermes plugins remove failed — may already be gone)")

    return {"ok": True, "removed": removed, "skipped": skipped}


# ── Profile model sync endpoints ─────────────────────────────────────────────

try:
    from core.sync_profiles import (
        get_profile_models as _get_profile_models,
        sync_profiles_to_model as _sync_profiles_to_model,
        _get_global_model as _get_global_model,
    )
except ImportError:
    _get_profile_models = None  # type: ignore[assignment]
    _sync_profiles_to_model = None  # type: ignore[assignment]
    _get_global_model = None  # type: ignore[assignment]


@profiles_router.get("/model")
async def get_profile_models_endpoint() -> dict[str, Any]:
    """Return current model settings for all *-daedalus profiles.

    GET /profiles/model
    Response: {
        "global": {"model_default": "...", "model_provider": "..."},
        "profiles": {
            "developer-daedalus": {
                "model_default": "...",
                "model_provider": "...",
                "is_daedalus": true,
                "path": "/abs/path"
            },
            ...
        },
        "stale": ["profile-name", ...]   // profiles whose model != global
    }
    """
    if _get_profile_models is None:
        return {"error": "sync_profiles module not available", "profiles": {}, "global": {}}

    profiles = _get_profile_models()
    if _get_global_model is not None:
        global_model, global_provider = _get_global_model()
    else:
        global_model, global_provider = "", ""

    stale = [
        name for name, info in profiles.items()
        if info["model_default"] and info["model_default"] != global_model
    ]

    return {
        "global": {
            "model_default": global_model,
            "model_provider": global_provider,
        },
        "profiles": profiles,
        "stale": stale,
    }


@profiles_router.post("/model/sync", response_model=None)
async def sync_profiles_model_endpoint(request: Request) -> dict[str, Any]:
    """Force sync all *-daedalus profiles to the current global model.

    POST /profiles/model/sync
    Body (optional):
        {"force": true, "model": "model-name", "provider": "provider-name"}
    Response:
        {"ok": true, "updated": N, "profiles": ["name1", "name2", ...]}
    """
    if _sync_profiles_to_model is None:
        return {"ok": False, "error": "sync_profiles module not available", "updated": 0, "profiles": []}

    force = True
    target_model = None
    target_provider = None

    if request is not None:
        try:
            body = await request.json()
        except Exception:
            body = {}
        if isinstance(body, dict):
            force = bool(body.get("force", True))
            target_model = body.get("model")
            target_provider = body.get("provider")

    updated, updated_list = _sync_profiles_to_model(
        force=force,
        target_model=target_model,
        target_provider=target_provider,
    )

    return {"ok": True, "updated": updated, "profiles": updated_list}


# ── Top-level router (defined at end so sub-routers are already populated) ───

# Gate every sub-router with the shared-secret dependency at include time, so
# all current AND future routes are covered — no endpoint can be accidentally
# left open (#1130).
# Auth is enforced by the Hermes host's global /api/ middleware (see the
# Authentication note above), so the plugin mounts its routers without a
# plugin-level auth dependency. See #1231.
router = APIRouter(tags=["daedalus"])
router.include_router(projects_router)
router.include_router(project_config_router)
router.include_router(meta_router)
router.include_router(profiles_router)
