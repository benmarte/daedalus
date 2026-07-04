"""
/profiles/* route handlers — profile model read and sync.

Extracted from ``dashboard/plugin_api.py`` (issue #1155, PR 3/3) with NO
behaviour change.

No test mocks target ``dashboard.plugin_api._get_profile_models`` /
``_sync_profiles_to_model`` / ``_get_global_model`` directly, so these are
imported once at module level rather than through the ``_api`` call-time
reference.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

profiles_router = APIRouter(prefix="/profiles", tags=["daedalus-profiles"])

# ── sync_profiles import block ───────────────────────────────────────────────

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


# ── Endpoints ────────────────────────────────────────────────────────────────

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
