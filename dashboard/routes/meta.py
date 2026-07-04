"""
/meta/* route handlers — branches, check-update, detect, labels, notifications,
pick-directory, projects, provision-roster, restart, roster-status, statuses,
test-deliver, uninstall, update-plugin, version.

Extracted from ``dashboard/plugin_api.py`` (issue #1155, PR 3/3) with NO
behaviour change.

Patchability contract: all calls to symbols that tests mock through
``dashboard.plugin_api.*`` go through the ``_api`` module reference so that
``mock.patch("dashboard.plugin_api.<name>")`` patches the live target seen by
these handlers at call time.  Symbols that are not mock-patched (from
``dashboard._shared``, standard library) are imported directly for clarity.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

import logging

import yaml
from fastapi import APIRouter, HTTPException, Request

from dashboard._shared import (
    _parse_env_file,
    _project_resolved,
    _real_home,
    detect_repo_vcs,
    logger,
    parse_cron_jobs,
)
from dashboard import cron_helpers  # for cron_helpers._cron_cli

# ``_api`` is ``dashboard.plugin_api``.  Importing it as a module reference
# (not ``from … import name``) means attribute look-ups happen at *call* time,
# so test patches applied to ``dashboard.plugin_api.<name>`` are visible here.
# The import is deferred to the *bottom* of this module to avoid a
# circular-import at load time (``plugin_api`` imports this module at its own
# bottom, after all its definitions).  By call time ``plugin_api`` is fully
# initialised.

meta_router = APIRouter(prefix="/meta", tags=["daedalus-meta"])


# ── Notification channel helpers ─────────────────────────────────────────────

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
    rc, out = _api._hermes_cli(["status"], timeout=10)
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

    rc, out = _api._hermes_cli(["send", "--list", "--json"], timeout=10)
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
    rc2, out2 = _api._hermes_cli(["send", "--list"], timeout=10)
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


# ── Meta endpoints ────────────────────────────────────────────────────────────

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
    "✅ Daedalus test — your notification target works."
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
    provider = _api._project_provider(resolved)
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
    provider = _api._project_provider(resolved)
    if provider is None:
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
    provider = _api._project_provider(resolved)
    if provider is None or not provider.supports_boards:
        return {"owner": owner, "projects": []}
    try:
        boards = [{"number": b.number, "title": b.title} for b in provider.list_boards()]
        return {"owner": owner, "projects": boards}
    except Exception:
        return {"owner": owner, "projects": []}


@meta_router.get("/statuses")
async def get_meta_statuses(
    request: Request, project: str, github_project_number: int | None = None
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
    provider = _api._project_provider(resolved)
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
    rc, out = cron_helpers._cron_cli(["list", "--all"])
    if rc != 0:
        return {}
    return {j["name"]: {**j, "found": True} for j in parse_cron_jobs(out) if j.get("name")}


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
_PROVISION_SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "provision_roster.sh"


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
    rc, out = _api._hermes_cli(list(args), timeout=timeout)
    return rc == 0, out


@meta_router.get("/version")
async def get_meta_version() -> dict[str, Any]:
    """Return the installed plugin version from plugin.yaml."""
    plugin_yaml = Path(__file__).resolve().parent.parent.parent / "plugin.yaml"
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
    plugin_yaml = Path(__file__).resolve().parent.parent.parent / "plugin.yaml"
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
    rc, update_out = _api._hermes_cli(["plugins", "update", "daedalus"], timeout=120)
    # If hermes plugins update fails for any reason (non-git install, hermes-dashboard
    # install, or any other error) fall back to git clone + rsync from the source URL.
    if rc != 0:
        plugin_yaml = Path(__file__).resolve().parent.parent.parent / "plugin.yaml"
        try:
            with open(plugin_yaml) as f:
                py_data = yaml.safe_load(f) or {}
            source_url = (py_data.get("source") or "").strip()
        except Exception as exc:
            return {"ok": False, "output": f"Could not read plugin.yaml: {exc}"}
        if not source_url:
            return {"ok": False, "output": "No source URL in plugin.yaml — cannot auto-update."}
        plugin_dir = str(Path(__file__).resolve().parent.parent.parent)
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
    rc_cron, cron_out = cron_helpers._cron_cli(["list", "--all"])
    if rc_cron == 0 and cron_out:
        daedalus_jobs = [
            j["name"] for j in parse_cron_jobs(cron_out)
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


# ── Deferred import to avoid circular-import at load time ────────────────────
# ``plugin_api`` imports this module at its own bottom (after all definitions),
# so by the time Python processes this line the partially-initialised
# ``plugin_api`` module already carries all the functions the handlers above
# need.  Functions reference ``_api`` only at *call* time, so the partial
# state during import is harmless.
import dashboard.plugin_api as _api  # noqa: E402
