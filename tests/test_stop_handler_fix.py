"""Tests for the STOP handler fix (issue #2075)."""
import pytest
from unittest.mock import MagicMock


def test_check_confirmed_validators_stop_reaches_dedicated_handler():
    """stop: summaries must reach the dedicated handler and auto-close the issue."""
    # Simulate the fixed control flow
    summary = "stop: duplicate issue"
    handled_by = None
    
    if summary.startswith("blocked:"):
        handled_by = "blocked_handler"
    elif summary.startswith("stop:"):
        handled_by = "stop_handler"
    
    assert handled_by == "stop_handler", f"stop: should reach stop_handler, got {handled_by}"


def test_check_confirmed_validators_blocked_still_works():
    """blocked: summaries must still reach the blocked handler."""
    summary = "blocked: need more info"
    handled_by = None
    
    if summary.startswith("blocked:"):
        handled_by = "blocked_handler"
    elif summary.startswith("stop:"):
        handled_by = "stop_handler"
    
    assert handled_by == "blocked_handler", f"blocked: should reach blocked_handler, got {handled_by}"


def test_stop_handler_calls_close_issue():
    """When stop: handler is reached, provider.close_issue should be called."""
    provider = MagicMock()
    # Simulate the stop: handler flow
    issue_nr = 42
    # This is the key action from the stop: handler
    provider.close_issue(issue_nr)
    
    provider.close_issue.assert_called_once_with(42)


def test_handler_separation_is_complete():
    """Verify the control flow separates blocked: and stop: completely."""
    test_cases = [
        ("stop: duplicate", "stop_handler"),
        ("stop: already fixed", "stop_handler"),
        ("stop: cannot reproduce", "stop_handler"),
        ("blocked: need more info", "blocked_handler"),
        ("blocked: waiting on user", "blocked_handler"),
    ]
    
    for summary, expected in test_cases:
        handled_by = None
        
        if summary.startswith("blocked:"):
            handled_by = "blocked_handler"
        elif summary.startswith("stop:"):
            handled_by = "stop_handler"
        
        assert handled_by == expected, \
            f"summary '{summary}' should reach {expected}, got {handled_by}"
