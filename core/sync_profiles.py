"""Explicit profile model sync — user-triggered resync bypassing manual-override skip.

This module provides:
- `sync_profiles_to_model()` — force sync all (or selected) *-daedalus profiles
- `get_profile_models()` — inspect current profile models vs global
- `sync_profiles_cli()` — CLI entry point for headless operation

Usage:
    # Force sync all profiles
    from core.sync_profiles import sync_profiles_to_model
    updated = sync_profiles_to_model(force=True)

    # Check which profiles are stale
    from core.sync_profiles import get_profile_models
    status = get_profile_models()

    # CLI usage
    python -m core.sync_profiles --force
    python -m core.sync_profiles --status
"""

from __future__ import annotations

import argparse
import logging
import os
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

logger = logging.getLogger("daedalus.sync_profiles")


def _get_hermes_home() -> Path:
    """Get HERMES_HOME from environment or default."""
    return Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))


def _get_global_model() -> Tuple[str, str]:
    """Read current global model.default and model.provider from Hermes config.

    Returns:
        Tuple of (model_default, model_provider)
    """
    hermes_home = _get_hermes_home()
    config_path = hermes_home / "config.yaml"

    if not config_path.is_file():
        logger.warning("Global config not found at %s", config_path)
        return "", ""

    try:
        config = yaml.safe_load(config_path.read_text()) or {}
        model_block = config.get("model") or {}
        return (
            (model_block.get("default") or "").strip(),
            (model_block.get("provider") or "").strip(),
        )
    except Exception as exc:
        logger.warning("Failed to read global config: %s", exc)
        return "", ""


def get_profile_models() -> Dict[str, Dict[str, str]]:
    """Get current model settings for all *-daedalus profiles.

    Returns:
        Dict mapping profile name to {model_default, model_provider, is_daedalus, path}
    """
    hermes_home = _get_hermes_home()
    profiles_dir = hermes_home / "profiles"

    if not profiles_dir.is_dir():
        return {}

    profiles = {}
    for profile_dir in sorted(profiles_dir.iterdir()):
        if not profile_dir.name.endswith("-daedalus"):
            continue

        cfg_path = profile_dir / "config.yaml"
        if not cfg_path.is_file():
            continue

        try:
            cfg = yaml.safe_load(cfg_path.read_text()) or {}
            model_block = cfg.get("model") or {}
            profiles[profile_dir.name] = {
                "model_default": (model_block.get("default") or "").strip(),
                "model_provider": (model_block.get("provider") or "").strip(),
                "is_daedalus": True,
                "path": str(profile_dir),
            }
        except Exception as exc:
            logger.warning("Failed to read %s: %s", cfg_path, exc)
            continue

    return profiles


def sync_profiles_to_model(
    force: bool = True,
    target_model: Optional[str] = None,
    target_provider: Optional[str] = None,
    profile_names: Optional[List[str]] = None,
    old_model: Optional[str] = None,
) -> Tuple[int, List[str]]:
    """Sync *-daedalus profiles to a target model.

    Args:
        force: If True, override manual overrides (profiles with differing model).
               If False, skip profiles that have been manually customized
               (model differs from old_model).
        target_model: Target model.default value. Defaults to current global.
        target_provider: Target model.provider value. Defaults to current global.
        profile_names: Optional list of specific profile names to sync.
                      If None, syncs all *-daedalus profiles.
        old_model: Previous global model value for override detection.
                  If None, defaults to current global.

    Returns:
        Tuple of (count_updated, list_of_updated_profile_names)
    """
    hermes_home = _get_hermes_home()
    profiles_dir = hermes_home / "profiles"

    if not profiles_dir.is_dir():
        logger.warning("Profiles directory not found: %s", profiles_dir)
        return 0, []

    # Get global model if not specified
    if target_model is None or target_provider is None:
        global_model, global_provider = _get_global_model()
        if target_model is None:
            target_model = global_model
        if target_provider is None:
            target_provider = global_provider

    # Determine old_model for override detection
    # If not provided, use target_model (assume old == new for standalone use)
    if old_model is None:
        old_model = target_model

    # If still empty, nothing to sync
    if not target_model:
        logger.warning("No target model specified and global model is empty")
        return 0, []

    updated = []
    for profile_dir in sorted(profiles_dir.iterdir()):
        if not profile_dir.name.endswith("-daedalus"):
            continue

        # Filter by specific profile names if provided
        if profile_names and profile_dir.name not in profile_names:
            continue

        cfg_path = profile_dir / "config.yaml"
        if not cfg_path.is_file():
            continue

        try:
            cfg = yaml.safe_load(cfg_path.read_text()) or {}
            model_block = cfg.get("model") or {}
            current_model = (model_block.get("default") or "").strip()

            # Skip non-force sync if profile has explicit override
            # A profile is a "manual override" if its current_model differs
            # from the OLD global model (i.e., user changed it intentionally)
            if not force and current_model and current_model != old_model:
                logger.debug(
                    "Skipping %s (manual override: %s != %s)",
                    profile_dir.name,
                    current_model,
                    old_model,
                )
                continue

            # Skip if already at target
            if current_model == target_model:
                logger.debug("%s already at target model %s", profile_dir.name, target_model)
                continue

            # Update profile config
            if not isinstance(cfg.get("model"), dict):
                cfg["model"] = {}
            cfg["model"]["default"] = target_model
            cfg["model"]["provider"] = target_provider or ""

            # Write atomically
            fd, tmp = tempfile.mkstemp(dir=cfg_path.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
                os.replace(tmp, cfg_path)
                updated.append(profile_dir.name)
                logger.info(
                    "Synced %s: model=%s provider=%s",
                    profile_dir.name,
                    target_model,
                    target_provider or "",
                )
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except Exception as exc:
            logger.warning("Failed to sync %s: %s", profile_dir.name, exc)
            continue

    return len(updated), updated


def sync_profiles_cli() -> None:
    """CLI entry point for sync_profiles_to_model."""
    parser = argparse.ArgumentParser(
        description="Sync *-daedalus profiles to current global model"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        default=True,
        help="Force sync even if profiles have manual overrides (default: True)",
    )
    parser.add_argument(
        "--no-force",
        action="store_true",
        help="Skip profiles with manual overrides",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Target model.default (defaults to global)",
    )
    parser.add_argument(
        "--provider",
        type=str,
        default=None,
        help="Target model.provider (defaults to global)",
    )
    parser.add_argument(
        "--profiles",
        nargs="+",
        default=None,
        help="Specific profile names to sync (e.g., developer-daedalus planner-daedalus)",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show current profile model status without syncing",
    )
    parser.add_argument(
        "--global-model",
        action="store_true",
        help="Show current global model",
    )

    args = parser.parse_args()

    # Set force based on args
    force = not args.no_force

    if args.global_model:
        global_model, global_provider = _get_global_model()
        print(f"Global model.default: {global_model}")
        print(f"Global model.provider: {global_provider}")
        return

    if args.status:
        profiles = get_profile_models()
        if not profiles:
            print("No *-daedalus profiles found")
            return

        print(f"{'Profile':<30} {'Model':<40} {'Provider':<15}")
        print("-" * 85)
        for name, info in sorted(profiles.items()):
            print(
                f"{name:<30} {info['model_default']:<40} {info['model_provider']:<15}"
            )
        return

    updated, updated_list = sync_profiles_to_model(
        force=force,
        target_model=args.model,
        target_provider=args.provider,
        profile_names=args.profiles,
    )

    if updated > 0:
        print(f"Synced {updated} profile(s):")
        for name in updated_list:
            print(f"  - {name}")
    else:
        print("No profiles updated")


if __name__ == "__main__":
    sync_profiles_cli()
