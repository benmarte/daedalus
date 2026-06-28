"""Unit tests for threshold and limit resolver functions.

Tests verify that resolver functions correctly read values from config
and fall back to sensible defaults when values are missing or invalid.
"""
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from scripts.daedalus_dispatch import (
    _resolve_github_api_issue_limit,
    _resolve_github_api_pr_limit,
    _resolve_follow_up_scan_limit,
)


class TestResolveGithubApiIssueLimit:
    """Tests for GitHub API issue limit resolver."""

    def test_default_value_when_not_configured(self):
        """Should return 100 when execution config is empty."""
        execution = {}
        result = _resolve_github_api_issue_limit(execution)
        assert result == 100

    def test_uses_configured_value(self):
        """Should use value from execution config when present."""
        execution = {"github_api_issue_limit": 50}
        result = _resolve_github_api_issue_limit(execution)
        assert result == 50

    def test_falls_back_on_invalid_type(self):
        """Should fall back to default when value is not an integer."""
        execution = {"github_api_issue_limit": "not a number"}
        result = _resolve_github_api_issue_limit(execution)
        assert result == 100

    def test_falls_back_on_negative_value(self):
        """Should fall back to default when value is negative."""
        execution = {"github_api_issue_limit": -10}
        result = _resolve_github_api_issue_limit(execution)
        assert result == 100

    def test_falls_back_on_zero_value(self):
        """Should fall back to default when value is zero."""
        execution = {"github_api_issue_limit": 0}
        result = _resolve_github_api_issue_limit(execution)
        assert result == 100

    def test_handles_none_execution(self):
        """Should handle None execution gracefully."""
        result = _resolve_github_api_issue_limit(None)
        assert result == 100


class TestResolveGithubApiPrLimit:
    """Tests for GitHub API PR limit resolver."""

    def test_default_value_when_not_configured(self):
        """Should return 50 when execution config is empty."""
        execution = {}
        result = _resolve_github_api_pr_limit(execution)
        assert result == 50

    def test_uses_configured_value(self):
        """Should use value from execution config when present."""
        execution = {"github_api_pr_limit": 75}
        result = _resolve_github_api_pr_limit(execution)
        assert result == 75

    def test_falls_back_on_invalid_type(self):
        """Should fall back to default when value is not an integer."""
        execution = {"github_api_pr_limit": "not a number"}
        result = _resolve_github_api_pr_limit(execution)
        assert result == 50

    def test_falls_back_on_negative_value(self):
        """Should fall back to default when value is negative."""
        execution = {"github_api_pr_limit": -5}
        result = _resolve_github_api_pr_limit(execution)
        assert result == 50

    def test_falls_back_on_zero_value(self):
        """Should fall back to default when value is zero."""
        execution = {"github_api_pr_limit": 0}
        result = _resolve_github_api_pr_limit(execution)
        assert result == 50

    def test_handles_none_execution(self):
        """Should handle None execution gracefully."""
        result = _resolve_github_api_pr_limit(None)
        assert result == 50


class TestResolveFollowUpScanLimit:
    """Tests for follow-up scan limit resolver."""

    def test_default_value_when_not_configured(self):
        """Should return 50 when follow-up config is empty."""
        follow_up = {}
        result = _resolve_follow_up_scan_limit(follow_up)
        assert result == 50

    def test_uses_configured_value(self):
        """Should use value from follow-up config when present."""
        follow_up = {"scan_pr_limit": 15}
        result = _resolve_follow_up_scan_limit(follow_up)
        assert result == 15

    def test_falls_back_on_invalid_type(self):
        """Should fall back to default when value is not an integer."""
        follow_up = {"scan_pr_limit": "not a number"}
        result = _resolve_follow_up_scan_limit(follow_up)
        assert result == 50

    def test_falls_back_on_negative_value(self):
        """Should fall back to default when value is negative."""
        follow_up = {"scan_pr_limit": -5}
        result = _resolve_follow_up_scan_limit(follow_up)
        assert result == 50

    def test_falls_back_on_zero_value(self):
        """Should fall back to default when value is zero."""
        follow_up = {"scan_pr_limit": 0}
        result = _resolve_follow_up_scan_limit(follow_up)
        assert result == 50

    def test_handles_none_follow_up(self):
        """Should handle None follow-up config gracefully."""
        result = _resolve_follow_up_scan_limit(None)
        assert result == 50

    def test_accepts_large_value(self):
        """Should accept large positive values."""
        follow_up = {"scan_pr_limit": 1000}
        result = _resolve_follow_up_scan_limit(follow_up)
        assert result == 1000


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
