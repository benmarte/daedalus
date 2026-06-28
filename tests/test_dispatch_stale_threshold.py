"""
Test cases for _dispatch_stale threshold parameter.

Verifies that _dispatch_stale() accepts and respects a threshold_hours
parameter instead of using a hardcoded 2-hour value.
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

# Ensure scripts/ is importable
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from watchdog import _dispatch_stale


def test_dispatch_stale_default_threshold():
    """Test default threshold (2 hours) - backwards compatibility."""
    mock_result = MagicMock()
    
    # Just under 2 hours - should not be stale
    mock_result.returncode = 0
    mock_result.stdout = "Last dispatch: 7199 seconds ago"
    
    with patch('watchdog.subprocess.run', return_value=mock_result):
        assert _dispatch_stale() is False
    
    # Exactly 2 hours - should be stale
    mock_result.stdout = "Last dispatch: 7200 seconds ago"
    with patch('watchdog.subprocess.run', return_value=mock_result):
        assert _dispatch_stale() is True
    
    # Over 2 hours - should be stale
    mock_result.stdout = "Last dispatch: 7201 seconds ago"
    with patch('watchdog.subprocess.run', return_value=mock_result):
        assert _dispatch_stale() is True


def test_dispatch_stale_one_hour_threshold():
    """Test custom 1-hour threshold."""
    mock_result = MagicMock()
    mock_result.returncode = 0
    
    # Just under 1 hour - should not be stale
    mock_result.stdout = "Last dispatch: 3599 seconds ago"
    with patch('watchdog.subprocess.run', return_value=mock_result):
        assert _dispatch_stale(threshold_hours=1) is False
    
    # Exactly 1 hour - should be stale
    mock_result.stdout = "Last dispatch: 3600 seconds ago"
    with patch('watchdog.subprocess.run', return_value=mock_result):
        assert _dispatch_stale(threshold_hours=1) is True
    
    # Over 1 hour - should be stale
    mock_result.stdout = "Last dispatch: 3601 seconds ago"
    with patch('watchdog.subprocess.run', return_value=mock_result):
        assert _dispatch_stale(threshold_hours=1) is True


def test_dispatch_stale_three_hour_threshold():
    """Test custom 3-hour threshold."""
    mock_result = MagicMock()
    mock_result.returncode = 0
    
    # Just under 3 hours - should not be stale
    mock_result.stdout = "Last dispatch: 10799 seconds ago"
    with patch('watchdog.subprocess.run', return_value=mock_result):
        assert _dispatch_stale(threshold_hours=3) is False
    
    # Exactly 3 hours - should be stale
    mock_result.stdout = "Last dispatch: 10800 seconds ago"
    with patch('watchdog.subprocess.run', return_value=mock_result):
        assert _dispatch_stale(threshold_hours=3) is True
    
    # Over 3 hours - should be stale
    mock_result.stdout = "Last dispatch: 10801 seconds ago"
    with patch('watchdog.subprocess.run', return_value=mock_result):
        assert _dispatch_stale(threshold_hours=3) is True
