"""Tests for the provider factory — selection, aliases, config validation,
token resolution, extensibility."""
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import providers  # noqa: E402
from core.providers import (AzureDevOpsProvider, GitHubProvider, GitLabProvider,  # noqa: E402
                            get_provider, provider_name, register_provider)
from core.providers.base import VCSProvider, resolve_token  # noqa: E402


GH_CFG = {"repo": "octo/repo"}
GL_CFG = {"repo": "group/proj", "vcs": {"provider": "gitlab"}}
ADO_CFG = {"vcs": {"provider": "azuredevops", "org": "acme", "project": "Web",
                   "repo": "web-app"}}


def test_default_provider_is_github():
    assert provider_name({}) == "github"
    p = get_provider(GH_CFG)
    assert isinstance(p, GitHubProvider)
    assert p.repo == "octo/repo"


def test_gitlab_selection_uses_repo_as_project_path():
    p = get_provider(GL_CFG)
    assert isinstance(p, GitLabProvider)
    assert p._project == "group%2Fproj"


def test_azure_aliases():
    for alias in ("azuredevops", "azure-devops", "azure", "ado", "Azure_DevOps"):
        cfg = {"vcs": {**ADO_CFG["vcs"], "provider": alias}}
        p = get_provider(cfg)
        assert isinstance(p, AzureDevOpsProvider), alias


def test_unknown_provider_returns_none():
    assert get_provider({"vcs": {"provider": "sourceforge"}}) is None


def test_missing_required_config_returns_none():
    assert get_provider({"vcs": {"provider": "gitlab"}}) is None        # no project
    assert get_provider({"vcs": {"provider": "azuredevops"}}) is None   # no org/project/repo
    assert get_provider({"repo": "not-a-slash"}) is None                # github needs owner/repo


def test_register_custom_provider():
    class FakeJira(VCSProvider):
        name = "jira"

        def list_issues(self, state="open", labels=None, limit=50):
            return []

        def close_issue(self, issue_number):
            return True

        def list_prs(self, state="all", limit=50):
            return []

    register_provider("jira", FakeJira)
    try:
        p = get_provider({"vcs": {"provider": "jira"}})
        assert isinstance(p, FakeJira)
    finally:
        providers.PROVIDER_REGISTRY.pop("jira", None)


def test_resolve_token_order():
    cfg = {"vcs": {"token_env": "MY_CUSTOM_TOKEN"}}
    with mock.patch.dict("os.environ",
                         {"MY_CUSTOM_TOKEN": "custom", "GITHUB_TOKEN": "default"},
                         clear=False):
        assert resolve_token(cfg, ("GITHUB_TOKEN",)) == "custom"
    with mock.patch.dict("os.environ", {"GITHUB_TOKEN": "default"}, clear=False):
        assert resolve_token({}, ("GITHUB_TOKEN", "GH_TOKEN")) == "default"
    with mock.patch.dict("os.environ", {}, clear=True):
        assert resolve_token({}, ("GITHUB_TOKEN",)) == ""


def test_missing_token_does_not_disable_provider():
    """A missing token must never return None — providers degrade per call."""
    with mock.patch.dict("os.environ", {}, clear=True):
        assert isinstance(get_provider(GH_CFG), GitHubProvider)
        assert isinstance(get_provider(GL_CFG), GitLabProvider)
        assert isinstance(get_provider(ADO_CFG), AzureDevOpsProvider)


def test_status_map_defaults_and_override():
    p = get_provider({"repo": "o/r", "vcs": {"status_map": {"done": "Shipped"}}})
    assert p.status_name("done") == "Shipped"
    assert p.status_name("ready") == "Ready"
    assert p.status_name("in_review") == "In review"
