"""
GET /projects route — aggregated status for every registered project.

Extracted from ``dashboard/plugin_api.py`` (issue #1155, PR 2/3) with NO
behaviour change.

Patchability contract: all calls to symbols that tests mock through
``dashboard.plugin_api.*`` go through the ``_api`` module reference so that
``mock.patch("dashboard.plugin_api.<name>")`` patches the live target seen by
these handlers at call time. Symbols that are not mock-patched (ConfigLoader,
_board_slug, _strip_secrets, …) are imported directly from ``dashboard._shared``
for clarity.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request

from dashboard._shared import (
    ConfigLoader,
    _board_slug,
    _strip_secrets,
    logger,
)
from dashboard import _shared  # module reference – used for _shared.registry

# ``_api`` is the ``dashboard.plugin_api`` module.  Importing it as a module
# reference (not ``from … import name``) means attribute look-ups happen at
# *call* time, so test patches applied to ``dashboard.plugin_api.<name>``
# are visible to the handlers here.  The import is deferred to avoid a
# circular-import at module load time: ``plugin_api`` imports this module at
# its *bottom*, after all its own definitions are registered, so by the time
# Python evaluates this ``import`` the partially-initialised ``plugin_api``
# module already carries every function the handlers need.
import dashboard.plugin_api as _api  # noqa: E402 (after package definitions)

projects_router = APIRouter(prefix="/projects", tags=["daedalus-projects"])


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


# ── Project-entry builders ──────��───────────────────────────────────────────

def _build_project_entry(proj: dict[str, Any],
                          cron_all: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Build a single project status entry from a resolved per-repo config.

    ``cron_all`` is the batched ``{cron_name: health}`` map from
    :func:`dashboard.plugin_api._cron_health_all`, fetched once per
    :func:`get_projects` call.
    """
    name = proj.get("name", "")
    repo = proj.get("repo", "")
    workdir = proj.get("workdir", "")
    slug = _board_slug(repo, name)

    # Fetch tasks once — shared by kanban_summary and needs_attention.
    tasks = _api._fetch_project_tasks(slug)
    kanban_summary = _api._kanban_summary(slug, tasks=tasks)
    needs_attention = _api._needs_attention(slug, all_tasks=tasks)

    # Open PRs (via the project's configured VCS provider)
    open_prs = _api._open_prs(_api._project_provider(proj))

    # Cron info — use pre-fetched batch result when available.
    cron_cfg = proj.get("cron") or {}
    cron_name = f"{name}-daedalus"
    health = cron_all.get(cron_name) or {"name": cron_name, "found": False,
                                          "state": None, "last_run": None,
                                          "last_status": None}
    cfg_schedule = (cron_cfg.get("schedule") or "").strip()
    live_schedule = (health.get("schedule") or "").strip()
    schedule = cfg_schedule or live_schedule
    cron: dict[str, Any] | None = None
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
    tasks = _api._fetch_project_tasks(slug)

    return {
        "name": name,
        "repo": repo_path,
        "workdir": repo_path,
        "kanban_summary": _api._kanban_summary(slug, tasks=tasks),
        "open_prs": None,
        "cron": None,
        "needs_attention": _api._needs_attention(slug, all_tasks=tasks),
        "tracking_mode": "kanban",
        "sources": None,
    }


# ── Endpoints ──────────────────────────────────────────────────────────────

@projects_router.get("")
async def get_projects(request: Request) -> list[dict[str, Any]]:
    """Return aggregated status for every project in the registry.

    Performance: cron health is fetched ONCE for all projects (single subprocess),
    kanban tasks are fetched once per project (shared between summary + attention),
    and all projects are built concurrently via asyncio.gather + asyncio.to_thread.
    """
    registry_repos: list[str] = []
    if _shared.registry is not None:
        try:
            registry_repos = _shared.registry.list_projects()
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
    cron_all: dict[str, dict[str, Any]] = await asyncio.to_thread(_api._cron_health_all)

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
