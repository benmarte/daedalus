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

import logging
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException, Request

# The graceful-degradation import blocks (config + core helpers), module
# logger, provider-env bootstrap, secret-stripping, and project-resolution
# helpers now live in dashboard/_shared.py; the cron reconcile machinery lives
# in dashboard/cron_helpers.py (issue #1155, PR 1/3, behaviour-neutral). They
# are re-imported here so every historical ``dashboard.plugin_api._X`` import
# path and test patch keeps resolving unchanged.
from dashboard._shared import (  # noqa: F401  (re-exported for import-path / test-patch compatibility)
    ConfigLoader,
    _SECRET_KEYS,
    _board_slug,
    _bootstrap_provider_env,
    _hermes_cli,
    _parse_env_file,
    _project_repo,
    _project_resolved,
    _real_home,
    _resolve_project_path,
    _schedule_to_crontab,
    _strip_secrets,
    deep_merge,
    detect_repo_vcs,
    ensure_board,
    get_provider,
    kanban_diagnostics,
    list_tasks,
    logger,
    parse_cron_jobs,
    validate_failover,
    validate_vcs,
)
from dashboard.cron_helpers import (  # noqa: F401  (re-exported for import-path / test-patch compatibility)
    NOTIFY_EVENTS,
    _parse_cron_jobs,
    _reconcile_cron,
    _validate_notifications,
    _write_schedule_to_config,
)

# ``registry`` (from ``core``) and ``_cron_cli`` are the two dependency-injection
# seams exercised by BOTH the extracted helpers (_shared / cron_helpers) and the
# route handlers still living here. They are referenced module-qualified
# (``_shared.registry`` / ``cron_helpers._cron_cli``) so a single patch target in
# the authoritative module reaches every caller — see issue #1155 PR 1/3.
from dashboard import _shared, cron_helpers  # noqa: F401


# ── Kanban helpers (degrade gracefully) ─────────────────────────────────────

def _fetch_project_tasks(slug: str) -> list[dict[str, Any]] | None:
    """Fetch all tasks for a board once; callers share this result."""
    if list_tasks is None:
        return None
    try:
        return list_tasks(slug)
    except Exception as exc:
        logger.warning("kanban: list_tasks failed for board %r: %s", slug, exc)
        return None


def _kanban_summary(slug: str, tasks: list[dict[str, Any]] | None = None) -> dict[str, int] | None:
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


def _needs_attention(slug: str, all_tasks: list[dict[str, Any]] | None = None) -> list[dict[str, str]] | None:
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


def _open_prs(provider) -> dict[str, Any] | None:
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


# ── Route groups extracted in PR 2/3 and PR 3/3 ──────────────────────────────
# Tracking-mode, project-entry builders, and per-project config endpoints have
# been moved to:
#   • dashboard/routes/projects.py       (_tracking_mode, _build_project_entry,
#                                         _build_registry_only_entry, get_projects)
#   • dashboard/routes/project_config.py (create_project, get_project_config,
#                                         post_project_config, run_dispatch_dry_run,
#                                         delete_project, plus their constants)
# /meta/* handlers and notification/roster/version helpers have been moved to:
#   • dashboard/routes/meta.py           (meta_router + all helpers)
# /profiles/* handlers have been moved to:
#   • dashboard/routes/admin.py          (profiles_router)
# (issue #1155, PR 2/3 and PR 3/3).  Sub-routers are imported and included at
# the *bottom* of this module so route handlers can call patchable names via
# this module's namespace and all existing ``mock.patch("dashboard.plugin_api.<name>")``
# test targets remain intact.


# ── Top-level router (defined at end so sub-routers are already populated) ───

# Import the extracted sub-routers AFTER all helper functions are defined above
# so the route modules can do ``import dashboard.plugin_api as _api`` and find
# _fetch_project_tasks, _kanban_summary, _needs_attention, _project_provider,
# _open_prs, _cron_health_all, _hermes_cmd, ensure_board, _reconcile_cron, etc.
# without a circular-import error.  The ``from … import`` also re-exports the
# router objects and helper symbols so ``dashboard.plugin_api.projects_router``,
# ``dashboard.plugin_api.meta_router``, ``dashboard.plugin_api._cron_health_all``
# etc. keep resolving (tests use them).
from dashboard.routes.projects import projects_router  # noqa: E402
from dashboard.routes.projects import _build_registry_only_entry  # noqa: E402,F401  (re-export for test-patch compat)
from dashboard.routes.project_config import project_config_router  # noqa: E402
from dashboard.routes.meta import (  # noqa: E402,F401  (re-exported for import-path / test-patch compatibility)
    meta_router,
    _channel_target_and_label,
    _hermes_status_configured_platforms,
    _NOTIF_PROBE_TTL_SECONDS,
    _notif_probe_cache,
    _reset_notif_probe_cache,
    _list_notification_methods,
    _compute_notification_methods,
    _parse_send_list_output,
    _TEST_MESSAGE,
    _cron_health_all,
    _hermes_cmd,
    _ALL_DAEDALUS_PROFILES,
    _PROVISION_SCRIPT,
    _semver_key,
)
from dashboard.routes.admin import profiles_router  # noqa: E402,F401  (re-export for test-patch compat)

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
