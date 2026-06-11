"""
Daedalus Config Loader — YAML-based multi-project configuration.

Single source of truth: daedalus.yaml
- `defaults` are inherited by all projects
- Each project can override any default
- Deep merge: nested dicts merge recursively
- Lists are replaced (not concatenated)
- No secrets in YAML — use `token_env` references

Usage:
    loader = ConfigLoader(path="/path/to/daedalus.yaml")
    config = loader.load()
    project = loader.resolve_project("my-project")
    all = loader.resolve_all()
    loader.add_project("new", "org/repo", "/path")
    loader.save()
"""

from __future__ import annotations

import copy
import json
import logging
import shutil
from pathlib import Path
from typing import Any, Optional

try:
    import yaml
except ImportError:
    yaml = None  # Will error gracefully in load()

logger = logging.getLogger("daedalus.config")

DEFAULT_CONFIG_PATH = Path.home() / ".hermes" / "daedalus.yaml"
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


def strip_project_key(config: dict) -> dict:
    """Remove top-level 'projects' key from resolved config (it's not a setting)."""
    result = copy.deepcopy(config)
    result.pop("projects", None)
    return result


# ---------------------------------------------------------------------------
# ConfigLoader
# ---------------------------------------------------------------------------

class ConfigLoader:
    """Parse, validate, and mutate daedalus.yaml."""

    def __init__(self, path: Optional[str | Path] = None):
        self.path = Path(path) if path else DEFAULT_CONFIG_PATH

    # -- loading / saving --

    def load(self) -> dict:
        """Return raw YAML config dict (with 'defaults' and 'projects')."""
        if yaml is None:
            raise RuntimeError("PyYAML is required. Install with: pip install pyyaml")
        if not self.path.exists():
            return self._empty()
        with open(self.path, "r") as f:
            raw = yaml.safe_load(f) or {}
        # Normalize empty config
        if not raw:
            return self._empty()
        # Normalize legacy format to defaults + projects
        return self._normalize(raw)

    def save(self, config: Optional[dict] = None) -> None:
        """Write config back to YAML (defaults to self.load() result modified by mutations)."""
        if yaml is None:
            raise RuntimeError("PyYAML is required. Install with: pip install pyyaml")
        if not config:
            config = self.load()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

    # -- normalization --

    def _empty(self) -> dict:
        return {"defaults": {}, "projects": []}

    def _normalize(self, raw: dict) -> dict:
        """Convert legacy single-project format to defaults + projects[] if needed."""
        if "projects" in raw or "defaults" in raw:
            return raw
        # Legacy: top-level keys are project settings
        legacy = {}
        for key in raw:
            if key not in ("repo", "workdir", "name"):
                legacy[key] = raw[key]
        legacy["name"] = raw.get("repo", "").split("/")[-1] if "/" in raw.get("repo", "") else "default"
        legacy["repo"] = raw.get("repo", "")
        legacy["workdir"] = raw.get("workdir", "")
        return {"defaults": legacy, "projects": [legacy]}

    # -- resolution --

    def resolve_project(self, name: str) -> dict:
        """Merge defaults + project overrides for one project."""
        config = self.load()
        defaults = config.get("defaults", {})
        if not defaults:
            defaults = self._load_defaults_template()
        projects = config.get("projects", [])
        project = next((p for p in projects if p.get("name") == name), None)
        if project is None:
            raise ValueError(f"Project '{name}' not found. Available: {[p.get('name') for p in projects]}")
        project_copy = copy.deepcopy(project)
        # Remove 'name', 'repo', 'workdir' from overrides — these are identity fields
        overrides = {}
        for key in ("name", "repo", "workdir"):
            overrides[key] = project_copy.pop(key, None)
        resolved = deep_merge(defaults, project_copy)
        if overrides.get("name"):
            resolved["name"] = overrides["name"]
        if overrides.get("repo"):
            resolved["repo"] = overrides["repo"]
        if overrides.get("workdir"):
            resolved["workdir"] = overrides["workdir"]
        return strip_project_key(resolved)

    def resolve_all(self) -> dict:
        """Merge defaults + all project overrides. Returns full resolved config."""
        config = self.load()
        defaults = config.get("defaults", {})
        if not defaults:
            defaults = self._load_defaults_template()
        projects = config.get("projects", [])
        resolved_projects = []
        for proj in projects:
            p_copy = copy.deepcopy(proj)
            overrides = {}
            for key in ("name", "repo", "workdir"):
                overrides[key] = p_copy.pop(key, None)
            merged = deep_merge(defaults, p_copy)
            if overrides.get("name"):
                merged["name"] = overrides["name"]
            if overrides.get("repo"):
                merged["repo"] = overrides["repo"]
            if overrides.get("workdir"):
                merged["workdir"] = overrides["workdir"]
            resolved_projects.append(strip_project_key(merged))
        return {"defaults": defaults, "projects": resolved_projects}

    def _load_defaults_template(self) -> dict:
        """Load from template file as fallback defaults."""
        if yaml is None or not TEMPLATE_PATH.exists():
            return {}
        with open(TEMPLATE_PATH, "r") as f:
            raw = yaml.safe_load(f) or {}
        return raw.get("defaults", raw)

    # -- project CRUD --

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
            raise RuntimeError("PyYAML is required.")

        with open(cfg_path, "r") as f:
            repo_cfg = yaml.safe_load(f) or {}

        defaults = self._load_defaults_template()
        # Strip identity fields from defaults so the file always wins.
        for key in ("name", "repo", "workdir"):
            defaults.pop(key, None)

        resolved = deep_merge(defaults, repo_cfg)
        resolved["workdir"] = str(repo)
        return resolved

    def list_projects(self) -> list[dict]:
        """Return list of project summaries (name, repo, workdir)."""
        config = self.load()
        projects = config.get("projects", [])
        return [
            {
                "name": p.get("name", "unnamed"),
                "repo": p.get("repo", ""),
                "workdir": p.get("workdir", ""),
            }
            for p in projects
        ]

    def add_project(
        self,
        name: str,
        repo: str,
        workdir: str,
        provider: str = "anthropic",
        model_name: str = "",
        schedule: str = "60m",
        channel: str = "slack",
        **kwargs,
    ) -> dict:
        """Add a new project. Returns the new project dict."""
        config = self.load()
        projects = config.get("projects", [])
        if any(p.get("name") == name for p in projects):
            raise ValueError(f"Project '{name}' already exists.")
        project = {
            "name": name,
            "repo": repo,
            "workdir": workdir,
            "model": {"provider": provider},
            "cron": {"schedule": schedule},
            "delivery": {"channel": channel},
            "sources": {"github": {"enabled": True}},
        }
        if model_name:
            project["model"]["name"] = model_name
        if kwargs:
            project.update(kwargs)
        projects.append(project)
        config["projects"] = projects
        self.save(config)
        return project

    def edit_project(self, name: str, overrides: dict) -> dict:
        """Edit an existing project. Returns the updated project dict."""
        config = self.load()
        projects = config.get("projects", [])
        project = next((p for p in projects if p.get("name") == name), None)
        if project is None:
            raise ValueError(f"Project '{name}' not found.")
        # Deep merge overrides into project (returns a new merged dict)
        merged = deep_merge(project, overrides)
        # Replace project in the list
        idx = next(i for i, p in enumerate(projects) if p.get("name") == name)
        projects[idx] = merged
        config["projects"] = projects
        self.save(config)
        return copy.deepcopy(merged)

    def clone_project(self, source: str, name: str) -> dict:
        """Clone a project (copy all settings). Returns the new project dict."""
        config = self.load()
        projects = config.get("projects", [])
        source_proj = next((p for p in projects if p.get("name") == source), None)
        if source_proj is None:
            raise ValueError(f"Source project '{source}' not found.")
        # Check target doesn't exist
        if any(p.get("name") == name for p in projects):
            raise ValueError(f"Project '{name}' already exists.")
        new_proj = copy.deepcopy(source_proj)
        new_proj["name"] = name
        projects.append(new_proj)
        config["projects"] = projects
        self.save(config)
        return copy.deepcopy(new_proj)

    def remove_project(self, name: str) -> None:
        """Remove a project by name."""
        config = self.load()
        projects = config.get("projects", [])
        new_projects = [p for p in projects if p.get("name") != name]
        if len(new_projects) == len(projects):
            raise ValueError(f"Project '{name}' not found.")
        config["projects"] = new_projects
        self.save(config)

    # -- conversion --

    def convert_to_multi(self) -> dict:
        """Convert legacy single-project config to multi-project format.
        
        Top-level keys become 'defaults', repo/workdir/name become a project entry.
        """
        config = self.load()
        if "projects" in config:
            return {"mode": "multi", "message": "Already in multi-project mode"}
        legacy = {}
        for key in config:
            if key not in ("repo", "workdir", "name"):
                legacy[key] = config[key]
        legacy["name"] = config.get("name", config.get("repo", "default").split("/")[-1] if "/" in config.get("repo", "") else "default")
        legacy["repo"] = config.get("repo", "")
        legacy["workdir"] = config.get("workdir", "")
        new_config = {"defaults": legacy, "projects": [legacy]}
        self.save(new_config)
        return {"mode": "multi", "message": "Converted to multi-project mode"}

    # -- validation --

    def validate(self) -> tuple[bool, list[str]]:
        """Validate YAML syntax and config logic. Returns (success, errors)."""
        errors = []
        try:
            raw = self.load()
        except Exception as e:
            return False, [f"YAML parse error: {e}"]
        if "defaults" not in raw:
            errors.append("Missing 'defaults' section")
        if "projects" not in raw:
            errors.append("Missing 'projects' section")
        defaults = raw.get("defaults", {})
        if not errors:
            for i, proj in enumerate(raw["projects"]):
                proj_name = proj.get("name", f"index_{i}")
                if not proj.get("name"):
                    errors.append(f"Project at index {i} missing 'name'")
                if not proj.get("repo"):
                    errors.append(f"Project '{proj_name}' missing 'repo'")
                if not proj.get("workdir"):
                    errors.append(f"Project '{proj_name}' missing 'workdir'")
                # Validate model provider (project overrides defaults)
                model = proj.get("model", {})
                provider = model.get("provider", defaults.get("model", {}).get("provider"))
                if provider and provider not in ("anthropic", "openai", "google", "custom"):
                    errors.append(
                        f"Project '{proj_name}': invalid provider '{provider}'. "
                        f"Valid: anthropic, openai, google, custom"
                    )
                # Validate cron schedule (project overrides defaults)
                cron = proj.get("cron", {})
                schedule = cron.get("schedule", defaults.get("cron", {}).get("schedule", ""))
                if schedule and not self._valid_schedule(schedule):
                    errors.append(
                        f"Project '{proj_name}': invalid cron schedule '{schedule}'. "
                        f"Examples: 60m, every 2h, 0 9 * * *"
                    )
        return len(errors) == 0, errors

    @staticmethod
    def _valid_schedule(schedule: str) -> bool:
        """Check if cron schedule is valid format."""
        if not schedule:
            return False
        # Simple formats: N (minutes), Nh (hours), ND (days)
        if schedule.endswith("m") or schedule.endswith("h") or schedule.endswith("d"):
            return True
        # cron expression: 5 fields
        parts = schedule.split()
        if len(parts) == 5:
            return True
        # ISO timestamp
        if schedule.startswith("20") or schedule.startswith("202"):
            return True
        # "every X"
        if schedule.startswith("every "):
            return True
        return False

    # -- export --

    def export_json(self) -> str:
        """Export full resolved config as JSON string."""
        resolved = self.resolve_all()
        return json.dumps(resolved, indent=2, default=str)

    def export_yaml(self) -> str:
        """Export full resolved config as YAML string."""
        if yaml is None:
            raise RuntimeError("PyYAML is required.")
        resolved = self.resolve_all()
        return yaml.dump(resolved, default_flow_style=False, sort_keys=False)

    # -- init template --

    @staticmethod
    def init_template(dest: Optional[str | Path] = None) -> str:
        """Copy the template file to a destination path. Returns the path."""
        dest_path = Path(dest) if dest else DEFAULT_CONFIG_PATH
        if TEMPLATE_PATH.exists():
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(TEMPLATE_PATH, dest_path)
            return str(dest_path)
        raise FileNotFoundError(f"Template not found at {TEMPLATE_PATH}")
