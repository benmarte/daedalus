"""
Daedalus Config Loader — per-repo YAML configuration.

Each project carries its own checked-in ``<repo>/.hermes/daedalus.yaml``
(scaffolded by scripts/setup.sh or the dashboard's Add Project), deep-merged
over the packaged template defaults (``templates/daedalus.yaml``). The
registry (core.registry, ``~/.hermes/daedalus/projects``) lists which repos
the dispatcher sweeps.

- Deep merge: nested dicts merge recursively; lists are replaced
- No secrets in YAML — tokens come from environment variables only

Usage:
    loader = ConfigLoader()
    resolved = loader.resolve_repo_config("/path/to/repo")
    errors = validate_vcs(resolved)
"""

from __future__ import annotations

import copy
import logging
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None  # Will error gracefully in resolve_repo_config()

logger = logging.getLogger("daedalus.config")

TEMPLATE_PATH = Path(__file__).parent.parent / "templates" / "daedalus.yaml"

# ---------------------------------------------------------------------------
# Deep merge utilities
# ---------------------------------------------------------------------------

def deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into *base*. Lists are replaced."""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


VALID_VCS_PROVIDERS = ("github", "gitlab", "azuredevops")


def validate_vcs(resolved: dict) -> list[str]:
    """Validate the vcs section of a resolved per-repo config.

    Returns a list of human-readable errors (empty when valid). A missing
    vcs section defaults to GitHub.
    """
    errors: list[str] = []
    vcs = resolved.get("vcs") or {}
    provider = (vcs.get("provider") or "github").lower().replace("-", "").replace("_", "")
    provider = {"azure": "azuredevops", "ado": "azuredevops"}.get(provider, provider)
    if provider not in VALID_VCS_PROVIDERS:
        errors.append(f"vcs.provider '{vcs.get('provider')}' is not one of: "
                      + ", ".join(VALID_VCS_PROVIDERS))
        return errors
    if provider == "github":
        repo = (resolved.get("repo") or "").strip()
        if "/" not in repo:
            errors.append("github provider requires top-level repo: \"owner/repo\"")
    elif provider == "gitlab":
        path = (vcs.get("project_path") or resolved.get("repo") or "").strip()
        if not vcs.get("project_id") and "/" not in path:
            errors.append("gitlab provider requires vcs.project_id or "
                          "vcs.project_path (\"group/project\")")
    elif provider == "azuredevops":
        for key in ("org", "project", "repo"):
            if not (vcs.get(key) or "").strip():
                errors.append(f"azuredevops provider requires vcs.{key}")
    for key, val in (vcs.get("status_map") or {}).items():
        if key not in ("ready", "in_progress", "in_review", "done"):
            errors.append(f"vcs.status_map: unknown status key '{key}' "
                          "(expected ready/in_progress/in_review/done)")
        elif not isinstance(val, str) or not val.strip():
            errors.append(f"vcs.status_map.{key} must be a non-empty string")
    # webhook_secret_env names the env var holding the HMAC secret (like
    # token_env — never a raw secret in YAML). Absence is valid (verification
    # off); when present it must be a non-empty string.
    if "webhook_secret_env" in vcs:
        wse = vcs.get("webhook_secret_env")
        if not isinstance(wse, str) or not wse.strip():
            errors.append("vcs.webhook_secret_env must be a non-empty string "
                          "naming the environment variable that holds the "
                          "webhook HMAC secret")
    return errors


# ---------------------------------------------------------------------------
# ConfigLoader
# ---------------------------------------------------------------------------

class ConfigLoader:
    """Resolve per-repo daedalus configs over the packaged template defaults."""

    def resolve_repo_config(self, repo_path: str) -> dict:
        """Load <repo_path>/.hermes/daedalus.yaml, deep-merge over
        packaged defaults, and return a fully-resolved project dict.

        *workdir* is always resolved to the absolute *repo_path*.
        Raises FileNotFoundError if the per-repo config file is missing.
        """
        repo = Path(repo_path).resolve()
        cfg_path = repo / ".hermes" / "daedalus.yaml"
        if not cfg_path.exists():
            raise FileNotFoundError(
                f"No daedalus config found at {cfg_path}. "
                f"Run scripts/setup.sh in the repo to scaffold one."
            )
        if yaml is None:
            raise RuntimeError("PyYAML is required. Install with: pip install pyyaml")

        with open(cfg_path, "r") as f:
            repo_cfg = yaml.safe_load(f) or {}

        defaults = self._load_defaults_template()
        # Strip identity fields from defaults so the file always wins.
        for key in ("name", "repo", "workdir"):
            defaults.pop(key, None)

        resolved = deep_merge(defaults, repo_cfg)
        resolved["workdir"] = str(repo)
        return resolved

    def _load_defaults_template(self) -> dict:
        """Load the packaged template as fallback defaults."""
        if yaml is None or not TEMPLATE_PATH.exists():
            return {}
        with open(TEMPLATE_PATH, "r") as f:
            raw = yaml.safe_load(f) or {}
        return raw.get("defaults", raw)
