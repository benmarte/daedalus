"""Unit tests for the base VCSProvider.get_prs_ci_status sequential fallback.

The base implementation iterates over ``get_pr_ci_status`` per PR and maps any
exception to ``CIStatus.UNKNOWN``. Providers with a true batch query (GitHub
GraphQL) override this — but the base must still be correct since it is the
fallback path used when the batch query fails.
"""
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.providers.base import CIStatus, VCSProvider  # noqa: E402


class _StubProvider(VCSProvider):
    """Minimal concrete provider for base batch CI testing."""

    name = "stub-ci"

    # Abstract methods — not exercised in these tests.
    def list_issues(self, state="open", labels=None, limit=50):
        return []

    def close_issue(self, issue_number):
        return False

    def list_prs(self, state="all", limit=50):
        return []


@pytest.fixture
def provider():
    return _StubProvider({})


# ── base sequential fallback ─────────────────────────────────────────────────


def test_base_batch_empty_returns_empty_dict(provider):
    assert provider.get_prs_ci_status([]) == {}


def test_base_batch_calls_get_pr_ci_status_once_per_pr(provider):
    provider.get_pr_ci_status = mock.Mock(
        side_effect=[CIStatus.GREEN, CIStatus.RED, CIStatus.PENDING])
    result = provider.get_prs_ci_status([1, 2, 3])
    assert result == {1: CIStatus.GREEN, 2: CIStatus.RED, 3: CIStatus.PENDING}
    assert provider.get_pr_ci_status.call_count == 3
    provider.get_pr_ci_status.assert_any_call(1)
    provider.get_pr_ci_status.assert_any_call(2)
    provider.get_pr_ci_status.assert_any_call(3)


def test_base_batch_maps_exception_to_unknown(provider):
    """A raised exception from get_pr_ci_status is caught and mapped to UNKNOWN."""
    provider.get_pr_ci_status = mock.Mock(
        side_effect=[CIStatus.GREEN, RuntimeError("boom"), CIStatus.RED])
    result = provider.get_prs_ci_status([10, 20, 30])
    assert result[10] == CIStatus.GREEN
    assert result[20] == CIStatus.UNKNOWN
    assert result[30] == CIStatus.RED
    assert provider.get_pr_ci_status.call_count == 3


def test_base_batch_single_pr(provider):
    """Single PR still works — one call, correct dict."""
    provider.get_pr_ci_status = mock.Mock(return_value=CIStatus.GREEN)
    result = provider.get_prs_ci_status([42])
    assert result == {42: CIStatus.GREEN}
    provider.get_pr_ci_status.assert_called_once_with(42)


def test_base_batch_preserves_order_with_duplicates(provider):
    """Duplicate PR numbers are passed through as-is (base does not dedup)."""
    provider.get_pr_ci_status = mock.Mock(return_value=CIStatus.GREEN)
    result = provider.get_prs_ci_status([5, 5])
    # Dict collapses duplicate keys, but the value is correct.
    assert result == {5: CIStatus.GREEN}
    # Base implementation calls once per entry — 2 calls for 2 entries.
    assert provider.get_pr_ci_status.call_count == 2


def test_base_batch_all_raise_returns_all_unknown(provider):
    """When every per-PR lookup raises, every entry is UNKNOWN."""
    provider.get_pr_ci_status = mock.Mock(
        side_effect=[ValueError("a"), ValueError("b")])
    result = provider.get_prs_ci_status([1, 2])
    assert result == {1: CIStatus.UNKNOWN, 2: CIStatus.UNKNOWN}
    assert provider.get_pr_ci_status.call_count == 2