"""Tests for merge_pr worktree/already-merged resilience (fixes #1034).

When GitHub returns an error (405/422) after a merge attempt — e.g. because the
branch was in a worktree and cleanup failed, or because the PR was already merged
in a concurrent tick — merge_pr() must verify the actual PR state before reporting
failure. If merged_at is set, the outcome is SUCCESS, not FAILURE.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.providers.github import GitHubProvider  # noqa: E402
from core.providers.http import ProviderError  # noqa: E402


def _make_provider(token: str = "tok") -> GitHubProvider:
    cfg = {
        "provider": "github",
        "repo": "owner/repo",
        "token_env": "_UNUSED_",
    }
    with patch.dict("os.environ", {"_UNUSED_": token}):
        return GitHubProvider(cfg)


class TestMergePrAlreadyMerged:
    """merge_pr returns True when PR is already MERGED on GitHub (#1034)."""

    def test_returns_true_when_pr_already_merged(self):
        """If PUT fails but GET shows merged_at is set → return True."""
        provider = _make_provider()
        provider._http = MagicMock()
        provider._http.put_json.side_effect = ProviderError("405 Method Not Allowed")
        provider._http.get_json.return_value = {
            "number": 42,
            "state": "closed",
            "merged_at": "2026-06-29T10:00:00Z",
        }

        result = provider.merge_pr(42)
        assert result is True

    def test_returns_false_when_pr_genuinely_not_merged(self):
        """If PUT fails and GET shows merged_at is None → return False."""
        provider = _make_provider()
        provider._http = MagicMock()
        provider._http.put_json.side_effect = ProviderError("422 Unprocessable")
        provider._http.get_json.return_value = {
            "number": 42,
            "state": "open",
            "merged_at": None,
        }

        result = provider.merge_pr(42)
        assert result is False

    def test_returns_false_when_state_check_also_fails(self):
        """If both PUT and the fallback GET fail → return False (no crash)."""
        provider = _make_provider()
        provider._http = MagicMock()
        provider._http.put_json.side_effect = ProviderError("connection error")
        provider._http.get_json.side_effect = ProviderError("connection error")

        result = provider.merge_pr(42)
        assert result is False

    def test_both_put_and_get_fail_logs_warning(self, caplog):
        """When both PUT and fallback GET fail, a warning is emitted with both errors."""
        import logging
        provider = _make_provider()
        provider._http = MagicMock()
        provider._http.put_json.side_effect = ProviderError("PUT failed")
        provider._http.get_json.side_effect = ProviderError("GET failed")

        with caplog.at_level(logging.WARNING, logger="daedalus.providers.github"):
            provider.merge_pr(42)

        assert any(
            "fallback state-check GET also failed" in r.message and "PUT failed" in r.message
            for r in caplog.records
        ), f"expected combined warning not found in: {[r.message for r in caplog.records]}"

    def test_returns_true_on_successful_put(self):
        """Normal path: PUT succeeds → return True without any GET."""
        provider = _make_provider()
        provider._http = MagicMock()
        provider._http.put_json.return_value = {"sha": "abc123"}

        result = provider.merge_pr(42, merge_method="squash")
        assert result is True
        provider._http.get_json.assert_not_called()

    def test_merge_method_defaults_to_squash(self):
        """Invalid merge_method falls back to squash."""
        provider = _make_provider()
        provider._http = MagicMock()
        provider._http.put_json.return_value = {}

        provider.merge_pr(42, merge_method="invalid")
        call_args = provider._http.put_json.call_args
        assert call_args[0][1]["merge_method"] == "squash"
