"""VCS provider factory.

``get_provider(resolved)`` returns the configured provider for a resolved
per-project config, or None (with a logged warning) when the provider is
unknown or its required config is missing. A missing token never disables
the plugin — providers degrade per-call instead.

Extensible: future trackers (Jira, Linear, Gitea, Bitbucket, …) call
``register_provider("jira", JiraProvider)`` and become selectable via
``vcs.provider: jira`` without touching the dispatcher.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Type

from .base import (CIStatus, Comment, DELIVERY_MARKER, IssueSummary, LabelDef,
                   PRSummary, ProviderConfigError, VCSProvider)
from .http import ProviderError
from .azure_devops import AzureDevOpsProvider
from .github import GitHubProvider
from .gitlab import GitLabProvider

logger = logging.getLogger("daedalus.providers")

PROVIDER_REGISTRY: Dict[str, Type[VCSProvider]] = {}

_ALIASES = {
    "github": "github",
    "gitlab": "gitlab",
    "azuredevops": "azuredevops",
    "azure": "azuredevops",
    "ado": "azuredevops",
}


def register_provider(name: str, cls: Type[VCSProvider]) -> None:
    PROVIDER_REGISTRY[_canonical(name)] = cls


def _canonical(name: str) -> str:
    key = (name or "").lower().replace("-", "").replace("_", "").replace(" ", "")
    return _ALIASES.get(key, key)


register_provider("github", GitHubProvider)
register_provider("gitlab", GitLabProvider)
register_provider("azuredevops", AzureDevOpsProvider)


def provider_name(resolved: Dict[str, Any]) -> str:
    """Canonical provider name for a resolved project config (default github)."""
    vcs = (resolved or {}).get("vcs") or {}
    return _canonical(vcs.get("provider") or "github")


def get_provider(resolved: Dict[str, Any]) -> Optional[VCSProvider]:
    """Build the configured provider, or None (logged) when unusable."""
    name = provider_name(resolved)
    cls = PROVIDER_REGISTRY.get(name)
    if cls is None:
        logger.warning("unknown vcs.provider '%s' — VCS integration disabled "
                       "(known: %s)", name, ", ".join(sorted(PROVIDER_REGISTRY)))
        return None
    try:
        return cls(resolved)
    except ProviderConfigError as e:
        logger.warning("vcs provider '%s' disabled: %s", name, e)
        return None
    except Exception:
        logger.warning("vcs provider '%s' failed to initialise", name, exc_info=True)
        return None


__all__ = [
    "CIStatus", "Comment", "DELIVERY_MARKER", "IssueSummary", "LabelDef",
    "PRSummary", "ProviderConfigError", "ProviderError", "VCSProvider",
    "AzureDevOpsProvider", "GitHubProvider", "GitLabProvider",
    "PROVIDER_REGISTRY", "get_provider", "provider_name", "register_provider",
]
