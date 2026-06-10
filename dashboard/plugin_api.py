"""
Daedalus Dashboard Plugin API — FastAPI APIRouter.

Mounted by the Hermes dashboard at /api/plugins/daedalus/.
Provides config read/write with validation, and per-project status aggregation.
Never reads or returns secrets.

Endpoints:
    GET  /config    — full resolved config + meta (profiles, slack targets, path)
    POST /config    — validate and persist the full daedalus.yaml document
    GET  /projects  — aggregated status for all registered projects
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Optional

import yaml
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

# ConfigLoader + deep_merge live in the daedalus package root (config/__init__.py).
# When the dashboard host runs, it adds the plugin dir to sys.path so
# relative imports work. Fall back to absolute import for testing.
try:
    from config import ConfigLoader, deep_merge
except ImportError:
    import sys

    _repo_root = Path(__file__).resolve().parent.parent
    if str(_repo_root) not in sys.path:
        sys.path.insert(0, str(_repo_root))
    from config import ConfigLoader  # type: ignore[no-redef]
    from config import deep_merge  # type: ignore[no-redef]

# Core helpers (degrade gracefully — never raise on missing data).
try:
    from core import registry
except ImportError:
    registry = None  # type: ignore[assignment]
try:
    from core.kanban import list_tasks, diagnostics as kanban_diagnostics
except ImportError:
    list_tasks = None  # type: ignore[assignment]
    kanban_diagnostics = None  # type: ignore[assignment]
try:
    from core.github_project import _gh_json, pr_ci_green
except ImportError:
    _gh_json = None  # type: ignore[assignment]
    pr_ci_green = None  # type: ignore[assignment]

config_router = APIRouter(prefix="/config", tags=["daedalus-config"])
projects_router = APIRouter(prefix="/projects", tags=["daedalus-projects"])
project_config_router = APIRouter(prefix="/project", tags=["daedalus-project-config"])
meta_router = APIRouter(prefix="/meta", tags=["daedalus-meta"])


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


HERMES_PROFILES_DIR = _real_home() / ".hermes" / "profiles"
DEFAULT_CONFIG_PATH = _real_home() / ".hermes" / "daedalus.yaml"

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


def _list_profiles() -> list[str]:
    """Return directory names under ~/.hermes/profiles/."""
    if not HERMES_PROFILES_DIR.exists():
        return []
    return sorted(
        p.name
        for p in HERMES_PROFILES_DIR.iterdir()
        if p.is_dir() and not p.name.startswith(".")
    )


def _list_slack_targets() -> list[str]:
    """Return slack target strings from `hermes send --list`."""
    try:
        result = subprocess.run(
            ["hermes", "send", "--list"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return []
        targets: list[str] = []
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if line.startswith("slack:"):
                targets.append(line)
        return targets
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []


def _list_notification_methods() -> dict[str, list[str]]:
    """Return notification channels grouped by method from `hermes send --list`.

    The command output groups targets under method headers like::

        Slack:
          slack:tasks (private)
          slack:#engineering
        Discord (Glados):
          discord:#general

    Returns a dict mapping method name (e.g. 'Slack', 'Discord') to a list
    of raw target strings with trailing annotations stripped
    (e.g. 'slack:tasks', 'discord:#general').

    Degrades gracefully to an empty dict if the command fails or the output
    is unparseable.
    """
    try:
        result = subprocess.run(
            ["hermes", "send", "--list"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return {}
        return _parse_send_list_output(result.stdout)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return {}


def _parse_send_list_output(output: str) -> dict[str, list[str]]:
    """Parse `hermes send --list` output into method -> targets dict.

    Skips the intro header line (e.g. "Available messaging targets:"),
    strips trailing parenthesized annotations from method keys (e.g.
    'Discord (Glados):' → 'Discord') and from target values (e.g.
    'slack:tasks (private)' → 'slack:tasks').
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

        # Method header: ends with ':' and is not indented
        if not line.startswith(" ") and line.endswith(":"):
            # Strip trailing colon, then strip parenthesized profile
            # annotations like '(Glados)', so 'Discord (Glados):' → 'Discord'
            method_raw = line.rstrip(":").strip()
            method_clean = re.sub(r"\s*\([^)]*\)\s*$", "", method_raw).strip()
            current_method = method_clean
            methods[current_method] = []
            continue

        # Indented target line
        if current_method is not None:
            # Strip trailing annotations like '(private)'
            target = line.strip()
            # Remove parenthesized suffixes: 'slack:tasks (private)' -> 'slack:tasks'
            target = re.sub(r"\s*\([^)]*\)\s*$", "", target).strip()
            if target:
                methods[current_method].append(target)

    return methods


# ── Board slug derivation (mirrors _board_slug in daedalus_dispatch.py) ─

def _board_slug(repo: str, name: str = "") -> str:
    """Derive kanban board slug from repo path (org/repo -> org-repo)."""
    slug = repo.replace("/", "-") if repo else name
    return re.sub(r"[^a-zA-Z0-9_-]", "-", slug).strip("-").lower() or name


# ── Kanban helpers (degrade gracefully) ─────────────────────────────────────

def _kanban_summary(slug: str) -> Optional[dict[str, int]]:
    """Return counts of kanban cards by status, or None if unavailable."""
    if list_tasks is None:
        return None
    try:
        tasks = list_tasks(slug)
    except Exception:
        return None
    if not tasks:
        return None
    counts: dict[str, int] = {}
    for t in tasks:
        status = (t.get("status") or "unknown").lower()
        counts[status] = counts.get(status, 0) + 1
    return counts


def _needs_attention(slug: str) -> Optional[list[dict[str, str]]]:
    """Return blocked/gave_up cards with ids and short reasons, or None."""
    if list_tasks is None:
        return None
    attention_states = {"blocked", "gave_up"}
    items: list[dict[str, str]] = []
    for state in attention_states:
        try:
            tasks = list_tasks(slug, status=state)
        except Exception:
            continue
        for t in (tasks or []):
            entry: dict[str, str] = {
                "task_id": t.get("id", ""),
                "title": t.get("title", ""),
                "status": state,
            }
            # Extract block reason from summary or result
            summary = t.get("summary") or t.get("result") or ""
            if summary:
                entry["reason"] = summary[:200]
            items.append(entry)
    return items if items else None


# ── GitHub PR helpers (degrade gracefully) ──────────────────────────────────

def _open_prs(repo: str) -> Optional[dict[str, Any]]:
    """Return open/in-review PRs with counts, numbers, and CI state.

    Returns None when gh is unavailable or the repo has no open PRs.
    """
    if _gh_json is None:
        return None
    data = _gh_json([
        "pr", "list", "--repo", repo, "--state", "open", "--limit", "20",
        "--json", "number,title,headRefName,state",
    ])
    if not data:
        return None
    pr_list: list[dict[str, Any]] = []
    for pr in data:
        num = pr.get("number")
        ci = None
        if num is not None and pr_ci_green is not None:
            try:
                ci = pr_ci_green(repo, int(num))
            except Exception:
                ci = None
        pr_list.append({
            "number": num,
            "title": pr.get("title", ""),
            "branch": pr.get("headRefName", ""),
            "ci_green": ci,
        })
    return {
        "count": len(pr_list),
        "prs": pr_list,
    }


# ── Tracking mode ───────────────────────────────────────────────────────────

def _tracking_mode(project_cfg: dict[str, Any]) -> str:
    """Determine tracking mode: 'github' if a project board is configured, else 'kanban'."""
    tracking = project_cfg.get("tracking") or {}
    if tracking.get("github_project_number"):
        return "github"
    return "kanban"


# ── Pydantic models for POST validation ────────────────────────────────────


class ProjectConfig(BaseModel):
    """A single project entry in daedalus.yaml."""

    name: str = Field(...)
    repo: str = Field(...)
    workdir: str = Field(...)
    tracking: dict[str, Any] = Field(default_factory=dict)
    execution: dict[str, Any] = Field(default_factory=dict)
    cron: dict[str, Any] = Field(default_factory=dict)
    delivery: dict[str, Any] = Field(default_factory=dict)
    vcs: dict[str, Any] = Field(default_factory=dict)
    issues: dict[str, Any] = Field(default_factory=dict)
    lifecycle: dict[str, Any] = Field(default_factory=dict)
    model: dict[str, Any] = Field(default_factory=dict)
    sources: dict[str, Any] = Field(default_factory=dict)
    notification: dict[str, Any] = Field(default_factory=dict)
    platform: dict[str, Any] = Field(default_factory=dict)
    stats: dict[str, Any] = Field(default_factory=dict)
    tech_stack: str = "auto"


class FullConfig(BaseModel):
    """The full daedalus.yaml document."""

    defaults: dict[str, Any] = Field(default_factory=dict)
    projects: list[ProjectConfig] = Field(default_factory=list)


# ── Endpoints ──────────────────────────────────────────────────────────────


@config_router.get("")
async def get_config(request: Request) -> dict[str, Any]:
    """Return the full resolved config + meta.

    Response shape:
        {
            "defaults": {...},
            "projects": [{...}, ...],
            "meta": {
                "profiles": ["developer", "planner", ...],
                "slack_targets": ["slack:...", ...],
                "path": "/Users/.../.hermes/daedalus.yaml"
            }
        }
    """
    loader = ConfigLoader(DEFAULT_CONFIG_PATH)
    resolved = loader.resolve_all()

    # Strip secrets from the resolved config
    safe = _strip_secrets(resolved)

    safe["meta"] = {
        "profiles": _list_profiles(),
        "slack_targets": _list_slack_targets(),
        "path": str(DEFAULT_CONFIG_PATH),
    }
    return safe


@config_router.post("")
async def post_config(request: Request, body: FullConfig) -> dict[str, Any]:
    """Validate and persist the full daedalus.yaml document.

    Validation rules (per project):
        - name, repo, workdir are required
        - tracking.github_project_number must be numeric if present
        - execution.worker_profile must exist in ~/.hermes/profiles/
    """
    errors: list[str] = []

    # Validate each project
    profiles = _list_profiles()
    for i, proj in enumerate(body.projects):
        prefix = f"projects[{i}] ({proj.name or 'unnamed'})"

        if not proj.name or not proj.name.strip():
            errors.append(f"{prefix}: 'name' is required")
        if not proj.repo or not proj.repo.strip():
            errors.append(f"{prefix}: 'repo' is required")
        if not proj.workdir or not proj.workdir.strip():
            errors.append(f"{prefix}: 'workdir' is required")

        # github_project_number: optional but must be numeric
        gh_num = proj.tracking.get("github_project_number")
        if gh_num is not None:
            if not isinstance(gh_num, int):
                try:
                    int(gh_num)
                except (ValueError, TypeError):
                    errors.append(
                        f"{prefix}: tracking.github_project_number must be numeric, got {gh_num!r}"
                    )

        # worker_profile: validate only when profiles directory has content.
        # An empty or absent profiles dir means the local machine isn't
        # populated; a valid profile name should never 422 in that case.
        worker = proj.execution.get("worker_profile")
        if worker is not None and profiles:
            if worker not in profiles:
                errors.append(
                    f"{prefix}: execution.worker_profile '{worker}' not found in "
                    f"~/.hermes/profiles/. Available: {profiles}"
                )

    if errors:
        raise HTTPException(
            status_code=422,
            detail={"errors": errors},
        )

    # Persist: convert Pydantic models to plain dicts, save via ConfigLoader
    raw: dict[str, Any] = {
        "defaults": body.defaults,
        "projects": [p.model_dump(exclude_unset=False) for p in body.projects],
    }

    loader = ConfigLoader(DEFAULT_CONFIG_PATH)
    loader.save(raw)

    return {"status": "saved", "path": str(DEFAULT_CONFIG_PATH)}


# ── Projects endpoint ───────────────────────────────────────────────────────


@projects_router.get("")
async def get_projects(request: Request) -> list[dict[str, Any]]:
    """Return aggregated status for all registered projects.

    Each entry includes:
        - name, repo, workdir (read-only identity fields)
        - kanban_summary: counts by status (kanban mode only)
        - open_prs: open/in-review PRs with counts and CI state
        - cron: schedule and delivery target
        - needs_attention: blocked/gave_up cards with ids and reasons
        - tracking_mode: 'github' or 'kanban'
        - sources: enabled sources dict (stripped of secrets)

    All fields degrade gracefully — nulls for missing data, never 500.
    """
    loader = ConfigLoader(DEFAULT_CONFIG_PATH)
    resolved = loader.resolve_all()

    # Collect projects from config
    config_projects: dict[str, dict[str, Any]] = {}
    for proj in resolved.get("projects", []):
        repo = proj.get("repo", "")
        if repo:
            config_projects[repo] = proj

    # Also collect from registry (repo paths registered but not yet in config)
    registry_repos: list[str] = []
    if registry is not None:
        try:
            registry_repos = registry.list_projects()
        except Exception:
            registry_repos = []

    # Build unique list; prefer config entries (which have name/workdir/settings)
    seen_repos: set[str] = set()
    projects: list[dict[str, Any]] = []

    for proj in resolved.get("projects", []):
        repo = proj.get("repo", "")
        if not repo or repo in seen_repos:
            continue
        seen_repos.add(repo)
        projects.append(_build_project_entry(proj, resolved.get("defaults", {})))

    # Add registry-only repos as lightweight entries
    for repo_path in registry_repos:
        if repo_path in seen_repos:
            continue
        seen_repos.add(repo_path)
        name = Path(repo_path).name
        projects.append(_build_registry_only_entry(repo_path, name))

    return projects


def _build_project_entry(
    proj: dict[str, Any],
    defaults: dict[str, Any],
) -> dict[str, Any]:
    """Build a single project status entry from a resolved config project."""
    name = proj.get("name", "")
    repo = proj.get("repo", "")
    workdir = proj.get("workdir", "")
    slug = _board_slug(repo, name)

    # Kanban summary
    kanban_summary = _kanban_summary(slug)

    # Open PRs
    open_prs = _open_prs(repo)

    # Cron info
    cron_cfg = proj.get("cron") or defaults.get("cron") or {}
    cron: Optional[dict[str, Any]] = None
    if cron_cfg:
        cron_name = f"{name}-daedalus"
        cron = {
            "name": cron_name,
            "schedule": cron_cfg.get("schedule"),
            "deliver": cron_cfg.get("deliver"),
            "last_run": cron_cfg.get("last_run"),
            "health": _cron_health(cron_name),
        }
        # Remove empty values
        cron = {k: v for k, v in cron.items() if v is not None}
        if not cron:
            cron = None

    # Needs attention
    needs_attention = _needs_attention(slug)

    # Sources (strip secrets)
    sources = _strip_secrets(proj.get("sources") or defaults.get("sources") or {})

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

    return {
        "name": name,
        "repo": repo_path,
        "workdir": repo_path,
        "kanban_summary": _kanban_summary(slug),
        "open_prs": _open_prs(repo_path) if "/" in repo_path else None,
        "cron": None,
        "needs_attention": _needs_attention(slug),
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
    except Exception:
        repo_paths = []

    loader = ConfigLoader()
    for rp in repo_paths:
        try:
            resolved = loader.resolve_repo_config(rp)
        except Exception:
            continue
        if resolved.get("name") == name:
            return Path(rp)

    raise HTTPException(status_code=404, detail=f"Project '{name}' not found")


def _reconcile_cron(project_name: str, cron_cfg: dict) -> dict:
    """Reconcile the real ``hermes cron`` job with the config on save.

    Cron job name = ``f"{project_name}-daedalus"``.  Idempotent — remove any
    existing job first, then re-create if ``cron_cfg.schedule`` is non-empty.
    A cron CLI failure is captured as an error string; this function NEVER raises, so
    a broken ``hermes`` binary cannot fail the config save.

    Args:
        project_name: The project name from the config.
        cron_cfg: The ``cron`` dict from the resolved project config.
            Keys used: ``schedule`` (str), ``deliver`` (str, optional).

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

    # 1. Remove any existing job (ignore "not found").
    try:
        subprocess.run(
            ["hermes", "cron", "remove", cron_name],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        pass  # non-fatal — the remove is best-effort

    # 2. If a schedule is set, re-create.
    schedule = cron_cfg.get("schedule", "").strip() if cron_cfg else ""
    if not schedule:
        result["cron"] = "removed"
        return result

    deliver = cron_cfg.get("deliver", "").strip() if cron_cfg else ""

    cmd = [
        "hermes", "cron", "create", schedule,
        "--name", cron_name,
        "--script", "daedalus-cron.sh",
        "--no-agent",
    ]
    if deliver:
        cmd += ["--deliver", deliver]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode != 0:
            result["error"] = (proc.stderr + proc.stdout).strip()[:500] or f"exit code {proc.returncode}"
        else:
            result["cron"] = "created" if "created" in (proc.stdout + proc.stderr).lower() else "updated"
    except FileNotFoundError:
        result["error"] = "hermes CLI not found"
    except subprocess.TimeoutExpired:
        result["error"] = "hermes cron create timed out after 10s"
    except OSError as exc:
        result["error"] = f"hermes cron create failed: {exc}"

    return result


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
    cron_result = _reconcile_cron(name, cron_cfg)

    return {"status": "saved", "path": str(cfg_path), "cron": cron_result}


# ── Meta endpoints ───────────────────────────────────────────────────────────


@meta_router.get("/notifications")
async def get_notifications(request: Request) -> dict[str, list[str]]:
    """Return notification methods and their deliverable targets.

    Calls ``hermes send --list`` and parses the output into a dict mapping
    method names (e.g. 'Slack', 'Discord') to lists of raw target strings
    (e.g. 'slack:tasks', 'discord:#general').

    Degrades gracefully to an empty dict if the command is unavailable.
    """
    return _list_notification_methods()


# ── Meta helpers ─────────────────────────────────────────────────────────────


def _project_repo(name: str) -> str:
    """Resolve a project name to its owner/repo string.

    Uses _resolve_project_path to find the repo directory, then loads the
    per-repo config to extract the canonical repo field (e.g. 'org/repo').
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
    repo = resolved.get("repo", "")
    if not repo:
        raise HTTPException(
            status_code=404,
            detail=f"Project '{name}' has no repo configured",
        )
    return repo


@meta_router.get("/branches")
async def get_meta_branches(request: Request, project: str) -> dict[str, Any]:
    """Return repo branches via gh api.

    Degrades gracefully to an empty list on any error.
    """
    try:
        repo = _project_repo(project)
    except HTTPException:
        return {"repo": "", "branches": []}
    if _gh_json is None:
        return {"repo": repo, "branches": []}
    try:
        data = _gh_json(["api", f"repos/{repo}/branches?per_page=100"])
        branches = [b["name"] for b in (data or [])] if data else []
        return {"repo": repo, "branches": branches}
    except Exception:
        return {"repo": repo, "branches": []}


@meta_router.get("/labels")
async def get_meta_labels(request: Request, project: str) -> dict[str, Any]:
    """Return repo labels with names and colors via gh label list.

    Degrades gracefully to an empty list on any error.
    """
    try:
        repo = _project_repo(project)
    except HTTPException:
        return {"repo": "", "labels": []}
    if _gh_json is None:
        return {"repo": repo, "labels": []}
    try:
        data = _gh_json(["label", "list", "--repo", repo, "--json", "name,color", "--limit", "200"])
        labels = [{"name": l["name"], "color": l.get("color", "")} for l in (data or [])]
        return {"repo": repo, "labels": labels}
    except Exception:
        return {"repo": repo, "labels": []}


@meta_router.get("/projects")
async def get_meta_projects(request: Request, project: str) -> dict[str, Any]:
    """Return GitHub Projects v2 boards for the repo's owner via gh project list.

    Degrades gracefully to an empty list on any error.
    """
    try:
        repo = _project_repo(project)
    except HTTPException:
        return {"owner": "", "projects": []}
    owner = repo.split("/")[0] if "/" in repo else repo
    if _gh_json is None:
        return {"owner": owner, "projects": []}
    try:
        data = _gh_json(["project", "list", "--owner", owner, "--format", "json"])
        projects = [{"number": p["number"], "title": p.get("title", "")} for p in (data or {}).get("projects", [])]
        return {"owner": owner, "projects": projects}
    except Exception:
        return {"owner": owner, "projects": []}


@meta_router.get("/statuses")
async def get_meta_statuses(
    request: Request, project: str, github_project_number: Optional[int] = None
) -> dict[str, Any]:
    """Return Status field options for a GitHub Project board.

    Requires ``github_project_number`` to query the board's fields.
    Degrades gracefully to an empty list on any error.
    """
    if github_project_number is None:
        return {"statuses": []}
    try:
        repo = _project_repo(project)
    except HTTPException:
        return {"statuses": []}
    owner = repo.split("/")[0] if "/" in repo else repo
    if _gh_json is None:
        return {"statuses": []}
    try:
        data = _gh_json(["project", "field-list", str(github_project_number), "--owner", owner, "--format", "json"])
        fields = (data or {}).get("fields", [])
        for f in fields:
            if str(f.get("name", "")).lower() == "status":
                return {"statuses": [o.get("name", "") for o in f.get("options", [])]}
        return {"statuses": []}
    except Exception:
        return {"statuses": []}


# ── Cron health ──────────────────────────────────────────────────────────────


def _cron_health(cron_name: str) -> dict[str, Any]:
    """Check cron job health by parsing ``hermes cron list --all`` output.

    Returns a dict with:
        name: str
        found: bool
        state: "active" | "paused" | None
        last_run: iso str or None
        last_status: "ok" | ... | None

    Degrades gracefully to found=False on any error.
    """
    result: dict[str, Any] = {
        "name": cron_name,
        "found": False,
        "state": None,
        "last_run": None,
        "last_status": None,
    }
    try:
        proc = subprocess.run(
            ["hermes", "cron", "list", "--all"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if proc.returncode != 0:
            return result
        output = proc.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return result

    # Split into blocks: each block starts with a header line like "job_id [state]"
    blocks: list[str] = []
    current: list[str] = []
    header_re = re.compile(r"^\s*[0-9a-fA-F]{6,}\s+\[(\w+)\]")
    for line in output.split("\n"):
        if header_re.match(line):
            if current:
                blocks.append("\n".join(current))
            current = [line]
        elif current:
            current.append(line)
    if current:
        blocks.append("\n".join(current))

    for block in blocks:
        # Extract the header line
        header_line = block.split("\n")[0]
        m = header_re.match(header_line)
        state = m.group(1) if m else ""
        # Extract Name: field
        name_match = re.search(r"^\s*Name:\s*(.+)$", block, re.MULTILINE)
        if not name_match or name_match.group(1).strip() != cron_name:
            continue
        # Found it
        result["found"] = True
        result["state"] = state
        # Extract Last run: line -> "iso_time  status"
        last_match = re.search(r"^\s*Last run:\s+(\S+)\s+(\S+)", block, re.MULTILINE)
        if last_match:
            result["last_run"] = last_match.group(1)
            result["last_status"] = last_match.group(2)
        break

    return result


# ── Top-level router (defined at end so sub-routers are already populated) ───

router = APIRouter(tags=["daedalus"])
router.include_router(config_router)
router.include_router(projects_router)
router.include_router(project_config_router)
router.include_router(meta_router)
