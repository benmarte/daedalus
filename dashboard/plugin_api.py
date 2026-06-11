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
import os
import re
import shutil
import subprocess
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any, Optional

import yaml
from fastapi import APIRouter, HTTPException, Request

# ConfigLoader + deep_merge live in the daedalus package root (config/__init__.py).
# When the dashboard host runs, it adds the plugin dir to sys.path so
# relative imports work. Fall back to absolute import for testing.
try:
    from config import ConfigLoader, deep_merge, validate_vcs
except ImportError:
    import sys

    _repo_root = Path(__file__).resolve().parent.parent
    if str(_repo_root) not in sys.path:
        sys.path.insert(0, str(_repo_root))
    from config import ConfigLoader  # type: ignore[no-redef]
    from config import deep_merge  # type: ignore[no-redef]
    from config import validate_vcs  # type: ignore[no-redef]

# Core helpers (degrade gracefully — never raise on missing data).
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
    try:
        # Use a temporary Path object; _real_home() is defined below, so read
        # the env file using the same logic inline.
        import pathlib as _pl
        _home = _pl.Path.home()
        _parts = _home.parts
        if ".hermes" in _parts and "profiles" in _parts:
            _idx = _parts.index(".hermes")
            _home = _pl.Path(*_parts[:_idx])
        _env_path = _home / ".hermes" / ".env"
        if not _env_path.exists():
            return
        for _line in _env_path.read_text().split("\n"):
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _k, _v = _line.split("=", 1)
            _k = _k.strip()
            _v = _v.strip().strip('"').strip("'")
            if _k in _TOKEN_KEYS and _v:
                os.environ.setdefault(_k, _v)
    except OSError:
        pass


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




def _list_notification_methods() -> dict[str, list[dict[str, str]]]:
    """Return notification channels grouped by method from `hermes send --list`.

    The command output groups targets under method headers like::

        Slack:
          slack:tasks (private)
          slack:#engineering

    Returns a dict mapping method name (e.g. 'Slack', 'Discord') to a list
    of ``{value, label}`` objects. For Slack targets, labels are resolved
    to human-readable names via the Slack Web API (cached). For non-Slack
    methods, the label is the raw target string.

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
        raw_methods = _parse_send_list_output(result.stdout)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return {}

    # Resolve Slack labels
    slack_ids: list[str] = []
    for method, targets in raw_methods.items():
        if method.lower() == "slack":
            slack_ids = targets
            break

    slack_labels: dict[str, str] = {}
    if slack_ids:
        slack_labels = _resolve_slack_labels(slack_ids)

    # Build the final shape: {value, label} per target
    result_dict: dict[str, list[dict[str, str]]] = {}
    for method, targets in raw_methods.items():
        entries: list[dict[str, str]] = []
        for t in targets:
            label = slack_labels.get(t, t) if method.lower() == "slack" else t
            entries.append({"value": t, "label": label})
        result_dict[method] = entries

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


# ── Slack channel name resolution ──────────────────────────────────────────

_SLACK_CACHE_DIR = _real_home() / ".hermes" / "daedalus"
_SLACK_CACHE_PATH = _SLACK_CACHE_DIR / "slack-channels.json"
_SLACK_CACHE_TTL = 3600  # 1 hour


def _load_slack_token() -> str | None:
    """Read SLACK_BOT_TOKEN from ~/.hermes/.env. Never logs the token."""
    env_path = _real_home() / ".hermes" / ".env"
    if not env_path.exists():
        return None
    try:
        for line in env_path.read_text().split("\n"):
            line = line.strip()
            if line.startswith("SLACK_BOT_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        return None
    return None


def _load_slack_cache() -> dict[str, dict[str, Any]]:
    """Load the id→label cache from disk. Returns {} on any failure."""
    if not _SLACK_CACHE_PATH.exists():
        return {}
    try:
        data = json.loads(_SLACK_CACHE_PATH.read_text())
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _save_slack_cache(cache: dict[str, dict[str, Any]]) -> None:
    """Persist the id→label cache to disk. Never raises."""
    try:
        _SLACK_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _SLACK_CACHE_PATH.write_text(json.dumps(cache, indent=2))
    except OSError:
        pass  # non-fatal — cache is best-effort


def _resolve_slack_labels(channel_ids: list[str]) -> dict[str, str]:
    """Resolve Slack channel IDs → human-readable labels via Slack Web API.

    Uses SLACK_BOT_TOKEN from ~/.hermes/.env. Caches results in
    ~/.hermes/daedalus/slack-channels.json with a 1-hour TTL.

    Resolution rules:
        - channel.name set → label ``#<name>``
        - channel.is_im (DM) → resolve user → label ``DM: <real_name>``
        - is_mpim / group with no name → label ``Group: <id>``
        - Any failure (no token, non-200, ok:false, timeout, network error)
          → graceful fallback to the raw ``slack:<id>`` label.

    Never logs the token. Never raises.
    """
    token = _load_slack_token()
    cache = _load_slack_cache()
    now = time.time()
    labels: dict[str, str] = {}

    for cid in channel_ids:
        # Strip "slack:" prefix to get bare channel ID for API calls
        bare_id = cid
        if cid.startswith("slack:"):
            bare_id = cid.split(":", 1)[1]

        # Check cache
        cached = cache.get(bare_id)
        if cached and isinstance(cached, dict) and (now - cached.get("ts", 0)) < _SLACK_CACHE_TTL:
            labels[cid] = cached.get("label", f"slack:{bare_id}")
            continue

        if not token:
            labels[cid] = f"slack:{bare_id}"
            continue

        # Resolve via Slack API
        label = _resolve_one_slack_channel(bare_id, token)
        labels[cid] = label

        # Update cache
        cache[bare_id] = {"label": label, "ts": now}

    _save_slack_cache(cache)
    return labels


def _resolve_one_slack_channel(channel_id: str, token: str) -> str:
    """Resolve a single Slack channel ID to a human-readable label.

    Returns the label string. Never raises — falls back to ``slack:<id>``.
    """
    try:
        # conversations.info
        url = f"https://slack.com/api/conversations.info?channel={channel_id}"
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            json.JSONDecodeError, TimeoutError):
        return f"slack:{channel_id}"

    if not body.get("ok"):
        return f"slack:{channel_id}"

    channel = body.get("channel", {})

    # Channel with a name → #<name>
    if channel.get("name"):
        return f"#{channel['name']}"

    # DM → resolve user
    if channel.get("is_im") and channel.get("user"):
        user_label = _resolve_slack_user(channel["user"], token)
        return f"DM: {user_label}"

    # MPIM / group with no name
    if channel.get("is_mpim") or channel.get("is_group"):
        return f"Group: {channel_id}"

    return f"slack:{channel_id}"


def _resolve_slack_user(user_id: str, token: str) -> str:
    """Resolve a Slack user ID to a display name. Never raises."""
    try:
        url = f"https://slack.com/api/users.info?user={user_id}"
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            json.JSONDecodeError, TimeoutError):
        return f"@{user_id}"

    if not body.get("ok"):
        return f"@{user_id}"

    user = body.get("user", {})
    return user.get("real_name") or user.get("display_name") or f"@{user_id}"


# ── Board slug derivation (mirrors _board_slug in daedalus_dispatch.py) ─

def _board_slug(repo: str, name: str = "") -> str:
    """Derive kanban board slug from repo path (org/repo -> org-repo)."""
    slug = repo.replace("/", "-") if repo else name
    return re.sub(r"[^a-zA-Z0-9_-]", "-", slug).strip("-").lower() or name


# ── Kanban helpers (degrade gracefully) ─────────────────────────────────────

def _kanban_summary(slug: str) -> Optional[dict[str, int]]:
    """Return counts of kanban cards by status, or None if the board is unavailable.

    Returns an empty dict ``{}`` when the board exists but has no tasks yet —
    this is distinct from ``None`` (board missing / CLI error) so the dashboard
    can show "board ready, 0 tasks" rather than "no kanban data".
    """
    if list_tasks is None:
        return None
    try:
        tasks = list_tasks(slug)
    except Exception:
        return None
    # list_tasks returns [] on CLI error AND on a genuinely empty board.
    # We can't distinguish without an extra CLI call, but a registered project
    # always has its board created at setup time, so treat [] as "board exists,
    # no tasks yet" → return {} so the frontend renders the board as empty
    # rather than missing.
    if tasks is None:
        return None
    counts: dict[str, int] = {}
    for t in (tasks or []):
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


# ── VCS PR helpers (degrade gracefully) ─────────────────────────────────────

def _project_provider(resolved: dict[str, Any]):
    """Build the VCS provider for a resolved project config, or None."""
    if get_provider is None or not resolved:
        return None
    try:
        return get_provider(resolved)
    except Exception:
        return None


def _open_prs(provider) -> Optional[dict[str, Any]]:
    """Return open/in-review PRs with counts, numbers, and CI state.

    Returns None when no provider is available or the repo has no open PRs.
    """
    if provider is None:
        return None
    try:
        prs = provider.list_prs(state="open", limit=20)
    except Exception:
        return None
    if not prs:
        return None
    pr_list: list[dict[str, Any]] = []
    for pr in prs:
        ci = None
        if pr.number is not None and provider.supports_ci_status:
            try:
                ci = provider.pr_ci_green(int(pr.number))
            except Exception:
                ci = None
        pr_list.append({
            "number": pr.number,
            "title": pr.title,
            "branch": pr.head_branch,
            "ci_green": ci,
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

    The registry (~/.hermes/daedalus/projects) is the single source of truth;
    each repo path resolves its own ``.hermes/daedalus.yaml`` for settings.
    Repos registered without a config yet appear as lightweight entries.

    Each entry includes:
        - name, repo, workdir (read-only identity fields)
        - kanban_summary: counts by status
        - open_prs: open/in-review PRs with counts and CI state
        - cron: schedule and delivery target
        - needs_attention: blocked/gave_up cards with ids and reasons
        - tracking_mode: provider name when a board is configured, else 'kanban'
        - sources: enabled sources dict (stripped of secrets)

    All fields degrade gracefully — nulls for missing data, never 500.
    """
    registry_repos: list[str] = []
    if registry is not None:
        try:
            registry_repos = registry.list_projects()
        except Exception:
            registry_repos = []

    loader = ConfigLoader()
    seen: set[str] = set()
    projects: list[dict[str, Any]] = []
    for repo_path in registry_repos:
        if repo_path in seen:
            continue
        seen.add(repo_path)
        try:
            resolved = loader.resolve_repo_config(repo_path)
        except Exception:
            # Registered but no per-repo config yet — lightweight entry.
            projects.append(_build_registry_only_entry(repo_path, Path(repo_path).name))
            continue
        projects.append(_build_project_entry(resolved))

    return projects


def _build_project_entry(proj: dict[str, Any]) -> dict[str, Any]:
    """Build a single project status entry from a resolved per-repo config."""
    name = proj.get("name", "")
    repo = proj.get("repo", "")
    workdir = proj.get("workdir", "")
    slug = _board_slug(repo, name)

    # Kanban summary
    kanban_summary = _kanban_summary(slug)

    # Open PRs (via the project's configured VCS provider)
    open_prs = _open_prs(_project_provider(proj))

    # Cron info — always probe the live cron system so the card reflects
    # reality even when the on-disk config is stale or has an empty cron block.
    cron_cfg = proj.get("cron") or {}
    cron_name = f"{name}-daedalus"
    health = _cron_health(cron_name)
    # Use schedule from config first; fall back to the value parsed from the
    # live cron job (populated by _cron_health when the job is found).
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

    # Needs attention
    needs_attention = _needs_attention(slug)

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

    return {
        "name": name,
        "repo": repo_path,
        "workdir": repo_path,
        "kanban_summary": _kanban_summary(slug),
        "open_prs": None,  # no per-repo config -> no VCS provider to query
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


def _parse_cron_list_blocks(output: str) -> list[dict[str, str]]:
    """Parse ``hermes cron list --all`` output into job blocks.

    A new block starts at a line matching ``^\\s*[0-9a-fA-F]{6,}\\s+\\[``
    (e.g. ``  99f7d116a95b [active]``).  Inside each block, capture the
    ``Name:`` value.  Box-drawing header/footer lines and warning lines are
    skipped.

    Returns a list of dicts with keys ``job_id`` and ``name``.
    """
    blocks: list[dict[str, str]] = []
    current: dict[str, str] | None = None

    for raw_line in output.split("\n"):
        line = raw_line.strip()
        if not line:
            continue

        # Skip box-drawing header/footer and warning lines
        if (
            line.startswith("┌")
            or line.startswith("└")
            or line.startswith("│")
            or line.startswith("⚠")
        ):
            continue

        # Block header: hex_id [status]
        if re.match(r"^[0-9a-fA-F]{6,}\s+\[", line):
            # Flush previous block
            if current is not None and current.get("job_id") and current.get("name"):
                blocks.append(current)
            # Start new block — extract job_id from header
            job_id = line.split()[0]
            current = {"job_id": job_id, "name": ""}
            continue

        # Inside a block: capture Name:
        if current is not None:
            m = re.match(r"^Name:\s+(.*)", line)
            if m:
                current["name"] = m.group(1).strip()

    # Flush final block
    if current is not None and current.get("job_id") and current.get("name"):
        blocks.append(current)

    return blocks


def _cron_cli(args: list[str]) -> tuple[int, str]:
    """Run a ``hermes cron`` subcommand. Returns (returncode, combined output).

    Never raises — CLI absence/timeouts return (-1, <error text>).
    """
    try:
        proc = subprocess.run(
            ["hermes", "cron"] + args,
            capture_output=True, text=True, timeout=10,
        )
        return proc.returncode, (proc.stdout + proc.stderr)
    except FileNotFoundError:
        return -1, "hermes CLI not found"
    except subprocess.TimeoutExpired:
        return -1, f"hermes cron {args[0]} timed out after 10s"
    except OSError as exc:
        return -1, f"hermes cron {args[0]} failed: {exc}"


def _reconcile_cron(project_name: str, cron_cfg: dict) -> dict:
    """Reconcile the real ``hermes cron`` job with the config on save.

    Cron job name = ``f"{project_name}-daedalus"``. Each project owns exactly
    one job. Editing a project UPDATES the existing job in place via the
    native ``hermes cron edit <id>`` — it never stacks a duplicate:

    - one existing job  → ``hermes cron edit <id> --schedule <s>``
      (falls back to remove+create if the installed hermes lacks ``edit``)
    - no existing job   → ``hermes cron create``
    - duplicates found  → keep none, remove all by hex ID, create fresh
    - empty schedule    → remove all matches

    A cron CLI failure is captured as an error string; this function NEVER
    raises, so a broken ``hermes`` binary cannot fail the config save.

    Args:
        project_name: The project name from the config.
        cron_cfg: The ``cron`` dict from the resolved project config.
            Keys used: ``schedule`` (str), ``deliver`` (str, optional),
            ``notifications`` (list, optional — when set, the dispatcher
            self-delivers and the cron gets NO --deliver target).

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

    schedule = cron_cfg.get("schedule", "").strip() if cron_cfg else ""
    # With notifications[] the dispatcher fans out itself — the cron job must
    # not double-deliver its stdout.
    has_notifications = bool(cron_cfg.get("notifications")) if cron_cfg else False
    deliver = "" if has_notifications else (cron_cfg.get("deliver", "").strip() if cron_cfg else "")

    # 1. Find existing jobs by name.
    matching_ids: list[str] = []
    rc, out = _cron_cli(["list", "--all"])
    if rc == 0:
        blocks = _parse_cron_list_blocks(out)
        matching_ids = [b["job_id"] for b in blocks if b.get("name") == cron_name]

    # 2. Empty schedule → remove all matches.
    if not schedule:
        for job_id in matching_ids:
            _cron_cli(["remove", job_id])
        result["cron"] = "removed"
        return result

    # 3. Exactly one job → update it in place (native `hermes cron edit`).
    if len(matching_ids) == 1:
        edit_args = ["edit", matching_ids[0], "--schedule", schedule]
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
        except Exception:
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
        except Exception:
            registered = False

    # Each project gets its OWN kanban board (idempotent create).
    cron_cfg = cfg.get("cron") or {}
    board_slug = _board_slug(repo, name)
    board_ok = bool(ensure_board(board_slug)) if ensure_board is not None else False

    # …and its OWN cron job.
    cron_result = _reconcile_cron(name, cron_cfg)

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
    cron_result = _reconcile_cron(name, cron_cfg)

    return {"status": "saved", "path": str(cfg_path), "cron": cron_result}


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

    # 2. Remove kanban board.
    loader = ConfigLoader()
    try:
        cfg = loader.resolve_repo_config(str(workdir))
        repo = cfg.get("repo") or ""
    except Exception:
        repo = ""
    slug = _board_slug(repo, name)
    ok2, _ = _hermes_cmd("kanban", "boards", "rm", slug, "--delete")
    if ok2:
        removed.append(f"kanban board: {slug}")
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
    if provider is None or not provider.supports_labels:
        return {"repo": repo, "labels": []}
    try:
        labels = [{"name": l.name, "color": l.color} for l in provider.list_labels()]
        return {"repo": repo, "labels": labels}
    except Exception:
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
        # Extract Schedule: line (e.g. "Schedule:  every 60m")
        sched_match = re.search(r"^\s*Schedule:\s+(.+)$", block, re.MULTILINE)
        if sched_match:
            result["schedule"] = sched_match.group(1).strip()
        # Extract Last run: line -> "iso_time  status"
        last_match = re.search(r"^\s*Last run:\s+(\S+)\s+(\S+)", block, re.MULTILINE)
        if last_match:
            result["last_run"] = last_match.group(1)
            result["last_status"] = last_match.group(2)
        break

    return result


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

_ROSTER_PROFILES = [
    "project-manager", "planner", "developer",
    "reviewer", "security-analyst", "documentation",
]
_PROVISION_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "provision_roster.sh"


@meta_router.get("/roster-status")
async def get_roster_status(request: Request) -> dict[str, Any]:
    """Check which of the six specialist profiles are provisioned.

    Returns ``{"all_provisioned": bool, "profiles": {name: bool}}``.
    """
    profiles_dir = _real_home() / ".hermes" / "profiles"
    status: dict[str, bool] = {}
    all_ok = True
    for profile in _ROSTER_PROFILES:
        exists = (profiles_dir / profile).is_dir()
        status[profile] = exists
        if not exists:
            all_ok = False
    return {"all_provisioned": all_ok, "profiles": status}


@meta_router.post("/provision-roster")
async def post_provision_roster(request: Request) -> dict[str, Any]:
    """Run provision_roster.sh to install the six specialist profiles.

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

    # Build an env that includes tokens from ~/.hermes/.env so profiles get
    # push auth automatically.
    env = dict(os.environ)
    env_path = _real_home() / ".hermes" / ".env"
    if env_path.exists():
        try:
            for line in env_path.read_text().split("\n"):
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        except OSError:
            pass

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


_DAEDALUS_PROFILES = [
    "developer", "reviewer", "security-analyst",
    "documentation", "planner", "project-manager",
]

def _hermes_cmd(*args: str, timeout: int = 30) -> tuple[bool, str]:
    """Run a hermes CLI command. Returns (success, combined_output)."""
    try:
        r = subprocess.run(
            ["hermes", *args], capture_output=True, text=True, timeout=timeout
        )
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    except Exception as exc:
        return False, str(exc)


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
    ok, cron_out = _hermes_cmd("cron", "list", "--all")
    if ok and cron_out:
        # Extract job names: lines like "  abc123 [active]\n  Name: foo-daedalus"
        job_names: list[str] = []
        current_name = ""
        current_script = ""
        in_block = False
        for line in cron_out.splitlines():
            if re.match(r"^\s*[0-9a-fA-F]{6,}\s+\[", line):
                if in_block and current_name:
                    if current_name.endswith("-daedalus") or re.search(r"daedalus-[^/]*\.sh$", current_script):
                        job_names.append(current_name)
                in_block = True
                current_name = current_script = ""
            elif in_block:
                m = re.match(r"^\s*Name:\s+(\S+)", line)
                if m:
                    current_name = m.group(1)
                m2 = re.match(r"^\s*Script:\s+(\S+)", line)
                if m2:
                    current_script = m2.group(1)
        if in_block and current_name:
            if current_name.endswith("-daedalus") or re.search(r"daedalus-[^/]*\.sh$", current_script):
                job_names.append(current_name)
        for job in sorted(set(job_names)):
            ok2, _ = _hermes_cmd("cron", "remove", job)
            if ok2:
                removed.append(f"cron job: {job}")
            else:
                skipped.append(f"cron job: {job} (removal failed)")

    # ── 2. Profiles ─────────────────────────────────────────────────────────
    ok, prof_out = _hermes_cmd("profile", "list")
    existing_profiles = prof_out if ok else ""
    for role in _DAEDALUS_PROFILES:
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


# ── Top-level router (defined at end so sub-routers are already populated) ───

router = APIRouter(tags=["daedalus"])
router.include_router(projects_router)
router.include_router(project_config_router)
router.include_router(meta_router)
