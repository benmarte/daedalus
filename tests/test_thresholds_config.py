"""Tests for threshold configuration registry (_TH) and config-driven behavior."""
import pytest
from unittest.mock import MagicMock, patch


def test_th_module_level_storage():
    """Verify _TH can be set and read at module level."""
    from core.iterate import set_thresholds, _th

    # Set thresholds
    test_config = {"max_sub_issues": 5, "source_file_max_size": 75000}
    set_thresholds(test_config)

    # Verify reads
    assert _th("max_sub_issues") == 5
    assert _th("source_file_max_size") == 75000
    assert _th("nonexistent", default=10) == 10


def test_extract_sub_issues_uses_parameter():
    """Verify _extract_sub_issues_from_body respects max_sub_issues parameter."""
    from core.iterate import _extract_sub_issues_from_body

    body = """Checklist:
- [ ] Task 1
- [ ] Task 2
- [ ] Task 3
- [ ] Task 4
- [ ] Task 5
- [ ] Task 6
- [ ] Task 7
"""

    # Test with explicit parameter
    result = _extract_sub_issues_from_body(body, max_sub_issues=3)
    assert len(result) == 3
    assert result == ["Task 1", "Task 2", "Task 3"]

    # Test with higher limit
    result = _extract_sub_issues_from_body(body, max_sub_issues=10)
    assert len(result) == 7


def test_extract_sub_issues_uses_config():
    """Verify _extract_sub_issues_from_body uses config from _TH when no parameter."""
    from core.iterate import _extract_sub_issues_from_body, set_thresholds

    body = """Checklist:
- [ ] Task 1
- [ ] Task 2
- [ ] Task 3
- [ ] Task 4
- [ ] Task 5
- [ ] Task 6
"""

    # Set config threshold
    set_thresholds({"max_sub_issues": 4})

    # Should use config value
    result = _extract_sub_issues_from_body(body)
    assert len(result) == 4

    # Reset to default
    set_thresholds({"max_sub_issues": 10})


def test_resolve_thresholds_parses_config():
    """Verify _resolve_thresholds correctly parses execution config."""
    from scripts.daedalus_dispatch import _resolve_thresholds

    execution = {
        "thresholds": {
            "max_fix_attempts": 2,
            "hermes_send_timeout": 60,
            "source_file_max_size": 100000,
        }
    }

    result = _resolve_thresholds(execution)

    assert result["max_fix_attempts"] == 2
    assert result["hermes_send_timeout"] == 60
    assert result["source_file_max_size"] == 100000


def test_resolve_thresholds_defaults_when_empty():
    """Verify _resolve_thresholds returns defaults when config is empty."""
    from scripts.daedalus_dispatch import _resolve_thresholds, _THRESHOLD_DEFAULTS

    result = _resolve_thresholds({})

    assert result == _THRESHOLD_DEFAULTS


def test_resolve_thresholds_partial_config():
    """Verify _resolve_thresholds fills missing keys with defaults."""
    from scripts.daedalus_dispatch import _resolve_thresholds, _THRESHOLD_DEFAULTS

    execution = {
        "thresholds": {
            "max_sub_issues": 15,
        }
    }

    result = _resolve_thresholds(execution)

    assert result["max_sub_issues"] == 15
    assert result["hermes_send_timeout"] == _THRESHOLD_DEFAULTS["hermes_send_timeout"]
    assert result["source_file_max_size"] == _THRESHOLD_DEFAULTS["source_file_max_size"]


def test_hermes_send_uses_threshold():
    """Verify _hermes_send reads timeout from _TH."""
    from scripts.daedalus_dispatch import _hermes_send, _TH

    # Set custom timeout
    _TH["hermes_send_timeout"] = 90

    with patch('subprocess.run') as mock_run:
        with patch('tempfile.NamedTemporaryFile') as mock_temp:
            mock_temp.return_value.__enter__.return_value.name = "/tmp/test.md"
            mock_temp.return_value.__exit__ = lambda *args: None

            _hermes_send("slack:test", "test message")

            # Verify timeout was passed to subprocess
            assert mock_run.called
            call_kwargs = mock_run.call_args.kwargs
            assert call_kwargs.get("timeout") == 90

    # Reset
    _TH["hermes_send_timeout"] = 30
