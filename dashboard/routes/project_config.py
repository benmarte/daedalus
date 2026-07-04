"""
Per-project config endpoints: /project/create, /project/{name}/config (GET+POST),
/project/{name}/run, /project/{name} (DELETE).

Extracted from ``dashboard/plugin_api.py`` (issue #1155, PR 2/3) with NO
behaviour change.

Patchability contract: all calls to symbols that tests mock through
``dashboard.plugin_api.*`` go through the ``_api`` module reference so that
``mock.patch("dashboard.plugin_api.<name>")`` patches the live target seen by
these handlers at call time.  Symbols that are not mock-patched are imported
directly from ``dashboard._shared`` / ``dashboard.cron_helpers`` for clarity.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException, Request

from dashboard._shared import (
    ConfigLoader,
    _board_slug,
    _resolve_project_path,
    _strip_secrets,
    deep_merge,
    detect_repo_vcs,
    logger,
    validate_failover,
    validate_vcs,
)
from dashboard import _shared  # module reference – used for _shared.registry
from dashboard.cron_helpers import _validate_notifications

# ``_api`` is the ``dashboard.plugin_api`` module.  Attribute look-ups happen
# at *call* time, so test patches applied to ``dashboard.plugin_api.<name>``
# are visible to the handlers here.  The import is deferred to avoid a
# circular-import at module load time: ``plugin_api`` imports this module at
# its *bottom*, after all its own definitions are registered.
import dashboard.plugin_api as _api  # noqa: E402 (after package definitions)

project_config_router = APIRouter(prefix="/project", tags=["daedalus-project-config"])

# ── Constants ────────────────────────────────────────────────────────────────

# Read-only identity keys that cannot be changed via POST
_READ_ONLY_KEYS = {"name", "repo", "workdir"}

# Known config section keys that should be dicts (used for basic type validation)
_CONFIG_SECTION_KEYS = {
    "vcs", "cron", "execution", "tracking", "delivery", "issues",
    "lifecycle", "model", "sources", "notification", "platform", "stats",
}

_PLUGIN_TEMPLATE_PATH = Path(__file__).resolve().parent.parent.parent / "templates" / "daedalus.yaml"


# ── Endpoints ──────────────────────────────────────────────────────────────

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
    if _shared.registry is not None:
        try:
            _shared.registry.add_project(str(workdir_path))
            registered = True
        except Exception as exc:
            logger.warning("registry: add_project failed for %s: %s", workdir_path, exc)
            registered = False

    # Each project gets its OWN kanban board (idempotent create).
    cron_cfg = cfg.get("cron") or {}
    board_slug = _board_slug(repo, name)
    board_ok = bool(_api.ensure_board(board_slug)) if _api.ensure_board is not None else False

    # …and its OWN cron job.
    cron_result = _api._reconcile_cron(name, cron_cfg, cfg_path)

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
    cron_result = _api._reconcile_cron(name, cron_cfg, cfg_path)

    return {"status": "saved", "path": str(cfg_path), "cron": cron_result}


@project_config_router.post("/{name}/run")
async def run_dispatch_dry_run(name: str) -> dict[str, Any]:
    """Trigger a dry-run dispatch tick for a project and return the log output."""
    workdir = _resolve_project_path(name)
    dispatch_script = Path(__file__).resolve().parent.parent.parent / "scripts" / "daedalus_dispatch.py"
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
    ok, _ = _api._hermes_cmd("cron", "remove", cron_name)
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
    ok2, _ = _api._hermes_cmd("kanban", "boards", "rm", slug)
    if ok2:
        removed.append(f"kanban board archived: {slug}")
    else:
        skipped.append(f"kanban board: {slug} (not found or already removed)")

    # 3. Remove from registry — do this last so lookups above still work.
    if _shared.registry is not None:
        try:
            _shared.registry.remove_project(str(workdir))
            removed.append(f"registry entry: {workdir}")
        except Exception as exc:
            skipped.append(f"registry entry: {exc}")
    else:
        skipped.append("registry: module unavailable")

    return {"ok": True, "removed": removed, "skipped": skipped}
