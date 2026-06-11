"""Auto-detect the VCS provider and repo identity from a git remote URL.

Used by scripts/setup.sh and the dashboard's project-create endpoint so a
project's ``vcs.provider`` (and provider-specific identifiers) are selected
automatically from the repository's ``origin`` remote.

Detection result shape::

    {
        "provider": "github" | "gitlab" | "azuredevops",
        "repo": "<top-level repo field value>",   # owner/repo, group/proj,
                                                  # or org/project/repo (azure)
        "vcs_extra": { ...extra vcs.* keys... },  # gitlab base_url,
                                                  # azure org/project/repo
    }

Unknown hosts return None — the caller keeps its defaults (github) or asks
the user. Self-hosted GitLab is only detected when the hostname contains
"gitlab"; other self-hosted instances must be configured manually.
"""
from __future__ import annotations

import re
import subprocess
from typing import Any, Dict, List, Optional, Tuple


def _split_remote_url(url: str) -> Optional[Tuple[str, List[str]]]:
    """Normalize an https/ssh/scp-style git URL into (host, path_parts)."""
    url = (url or "").strip()
    if not url:
        return None
    if url.endswith(".git"):
        url = url[: -len(".git")]
    # scp-style: git@host:path
    m = re.match(r"^[\w.+-]+@([^:/]+):(.+)$", url)
    if m:
        host, path = m.group(1), m.group(2)
    else:
        # scheme://[user@]host/path
        m = re.match(r"^[a-z+]+://(?:[\w.+-]+@)?([^/:]+)(?::\d+)?/(.+)$", url)
        if not m:
            return None
        host, path = m.group(1), m.group(2)
    parts = [p for p in path.split("/") if p]
    return host.lower(), parts


def detect_from_url(url: str) -> Optional[Dict[str, Any]]:
    """Detect provider + identity from a git remote URL, or None if unknown."""
    split = _split_remote_url(url)
    if not split:
        return None
    host, parts = split

    # ── GitHub ────────────────────────────────────────────────────────────
    if host == "github.com" or host.endswith(".github.com"):
        if len(parts) >= 2:
            return {"provider": "github", "repo": f"{parts[0]}/{parts[1]}",
                    "vcs_extra": {}}
        return None

    # ── Azure DevOps ──────────────────────────────────────────────────────
    # https://dev.azure.com/<org>/<project>/_git/<repo>
    # git@ssh.dev.azure.com:v3/<org>/<project>/<repo>
    # https://<org>.visualstudio.com/<project>/_git/<repo>
    if host in ("dev.azure.com", "ssh.dev.azure.com"):
        cleaned = [p for p in parts if p not in ("_git", "v3")]
        if len(cleaned) >= 3:
            org, project, repo = cleaned[0], cleaned[1], cleaned[2]
            return {"provider": "azuredevops",
                    "repo": f"{org}/{project}/{repo}",
                    "vcs_extra": {"org": org, "project": project, "repo": repo}}
        return None
    if host.endswith(".visualstudio.com"):
        org = host.split(".")[0]
        cleaned = [p for p in parts if p != "_git"]
        if len(cleaned) >= 2:
            project, repo = cleaned[0], cleaned[1]
            return {"provider": "azuredevops",
                    "repo": f"{org}/{project}/{repo}",
                    "vcs_extra": {"org": org, "project": project, "repo": repo}}
        return None

    # ── GitLab (gitlab.com or self-hosted with "gitlab" in the hostname) ──
    if host == "gitlab.com" or "gitlab" in host:
        if len(parts) >= 2:
            result: Dict[str, Any] = {"provider": "gitlab",
                                      "repo": "/".join(parts),  # nested groups OK
                                      "vcs_extra": {}}
            if host != "gitlab.com":
                result["vcs_extra"]["base_url"] = f"https://{host}"
            return result
        return None

    return None  # unknown host — caller keeps defaults / asks the user


def detect_repo_vcs(workdir: str) -> Optional[Dict[str, Any]]:
    """Detect provider + identity from ``workdir``'s origin remote, or None."""
    try:
        proc = subprocess.run(
            ["git", "-C", workdir, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    return detect_from_url(proc.stdout.strip())
