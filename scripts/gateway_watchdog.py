#!/usr/bin/env python3
"""Gateway watchdog for Hermes.

Detects silent gateway death via `hermes gateway status`, tracks restart attempts
with exponential backoff and rate limiting, and restarts the gateway when safe.

Usage:
    gateway_watchdog.py [options]

Options:
    --state-file PATH       Path to state file (default: ~/.hermes/gateway-watchdog-state.json)
    --stop-marker PATH      STOP marker file (default: ~/.hermes/gateway-stop)
    --logs-dir PATH         Logs directory (default: ~/.hermes/logs)
    --max-restarts N        Max restart attempts per window (default: 3)
    --backoff-base SECS     Initial exponential backoff in seconds (default: 10)
    --backoff-cap   SECS    Max backoff seconds (default: 300)
    --window-seconds SECS   Rate-limit window (default: 3600)
    --lookback-seconds SECS Crash log lookback (default: 300)
    --no-dispatch           Skip dispatcher re-exec after restart
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional


DEFAULT_STATE_FILE = "~/.hermes/gateway-watchdog-state.json"
DEFAULT_STOP_MARKER = "~/.hermes/gateway-stop"
DEFAULT_LOGS_DIR = "~/.hermes/logs"
DEFAULT_MAX_RESTARTS = 3
DEFAULT_BACKOFF_BASE = 10
DEFAULT_BACKOFF_CAP = 300
DEFAULT_WINDOW = 3600
DEFAULT_LOOKBACK = 300


# ── pure detection helpers ───────────────────────────────────────────────────


def is_gateway_running() -> bool:
    """True when `hermes gateway status` exits 0 and does NOT mention 'not running'.

    NOTE: `hermes gateway status` always exits 0 regardless of state.
    We must parse stdout, not the exit code. Any failure (timeout, missing CLI,
    OSError) is treated as "not running" so the watchdog is conservative.
    """
    try:
        result = subprocess.run(
            ["hermes", "gateway", "status"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False

    output = ((result.stdout or "") + (result.stderr or "")).lower()
    # Conservative: only treat as "running" if exit is clean AND output lacks the
    # "not running" sentinel.
    if result.returncode != 0:
        return False
    return "not running" not in output


def stop_requested(marker: Path) -> bool:
    """True if the user created the STOP marker to inhibit restarts."""
    return marker.is_file()


def has_crash_log(logs_dir: Path, lookback_seconds: int = DEFAULT_LOOKBACK) -> bool:
    """True if a gateway-related crash log was updated within the lookback window.

    Matches files whose name starts with 'gateway' or 'hermes.' and ends with '.log'.
    """
    if not logs_dir.is_dir():
        return False

    cutoff = time.time() - lookback_seconds
    for log_file in logs_dir.iterdir():
        if not log_file.is_file():
            continue
        name = log_file.name.lower()
        if not name.endswith(".log"):
            continue
        if not (name.startswith("gateway") or name.startswith("hermes.")):
            continue
        try:
            if log_file.stat().st_mtime >= cutoff:
                return True
        except OSError:
            continue
    return False


def read_state(state_file: Path) -> dict:
    """Load watchdog state JSON. Returns empty dict on missing/corrupt file."""
    if not state_file.is_file():
        return {}
    try:
        return json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def write_state(state_file: Path, state: dict) -> None:
    """Write watchdog state JSON atomically via tmp-rename."""
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    tmp.replace(state_file)


def recent_restarts(state_file: Path, window_seconds: int = DEFAULT_WINDOW) -> List[float]:
    """Sorted list of restart timestamps within window_seconds (from now)."""
    state = read_state(state_file)
    restarts = state.get("restarts", [])
    if not isinstance(restarts, list):
        return []
    cutoff = time.time() - window_seconds
    recent: List[float] = []
    for ts in restarts:
        try:
            ts_float = float(ts)
        except (TypeError, ValueError):
            continue
        if ts_float >= cutoff:
            recent.append(ts_float)
    return sorted(recent)


def backoff_seconds(num_recent: int, base: int = DEFAULT_BACKOFF_BASE,
                    cap: int = DEFAULT_BACKOFF_CAP) -> int:
    """Exponential backoff (base * 2**(n-1), capped). Returns 0 for n==0."""
    if num_recent == 0:
        return 0
    delay = base * (2 ** (num_recent - 1))
    return min(delay, cap)


def decide_action(
    running: bool,
    stop: bool,
    recent_count: int,
    max_restarts: int,
) -> str:
    """Pick an action from: noop, respect_stop, rate_limited, restart.

    Priority (first match wins):
      1. running       → noop
      2. stop marker   → respect_stop
      3. over cap      → rate_limited
      4. otherwise     → restart
    """
    if running:
        return "noop"
    if stop:
        return "respect_stop"
    if recent_count >= max_restarts:
        return "rate_limited"
    return "restart"


def update_state_after_restart(state_file: Path,
                               max_window: int = DEFAULT_WINDOW) -> None:
    """Append a restart timestamp and prune entries older than max_window."""
    state = read_state(state_file)
    restarts = state.get("restarts", [])
    if not isinstance(restarts, list):
        restarts = []

    now = time.time()
    cutoff = now - max_window
    pruned: List[float] = []
    for ts in restarts:
        try:
            ts_float = float(ts)
        except (TypeError, ValueError):
            continue
        if ts_float >= cutoff:
            pruned.append(ts_float)
    pruned.append(now)
    state["restarts"] = pruned
    write_state(state_file, state)


# ── CLI driver ───────────────────────────────────────────────────────────────


def _parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Hermes gateway watchdog")
    parser.add_argument("--state-file", default=DEFAULT_STATE_FILE)
    parser.add_argument("--stop-marker", default=DEFAULT_STOP_MARKER)
    parser.add_argument("--logs-dir", default=DEFAULT_LOGS_DIR)
    parser.add_argument("--max-restarts", type=int, default=DEFAULT_MAX_RESTARTS)
    parser.add_argument("--backoff-base", type=int, default=DEFAULT_BACKOFF_BASE)
    parser.add_argument("--backoff-cap", type=int, default=DEFAULT_BACKOFF_CAP)
    parser.add_argument("--window-seconds", type=int, default=DEFAULT_WINDOW)
    parser.add_argument("--lookback-seconds", type=int, default=DEFAULT_LOOKBACK)
    parser.add_argument("--no-dispatch", action="store_true",
                        help="Skip dispatcher re-exec after successful restart")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    """Run one watchdog pass. Returns 0 for all non-failure paths."""
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))

    state_file = Path(args.state_file).expanduser()
    stop_marker = Path(args.stop_marker).expanduser()
    logs_dir = Path(args.logs_dir).expanduser()

    running = is_gateway_running()
    stop = stop_requested(stop_marker)
    crash = has_crash_log(logs_dir, lookback_seconds=args.lookback_seconds)
    recent = recent_restarts(state_file, window_seconds=args.window_seconds)

    action = decide_action(running, stop, len(recent), args.max_restarts)

    print(f"gateway-watchdog: running={running} stop={stop} crash={crash} "
          f"recent={len(recent)}/{args.max_restarts} action={action}")

    if action in ("noop", "respect_stop", "rate_limited"):
        return 0

    # action == "restart"
    if recent and args.backoff_base > 0:
        backoff = backoff_seconds(len(recent), args.backoff_base, args.backoff_cap)
        elapsed = time.time() - recent[-1]
        if backoff > 0 and elapsed < backoff:
            print(f"gateway-watchdog: backoff active "
                  f"({elapsed:.1f}s < {backoff}s), deferring")
            return 0

    print("gateway-watchdog: issuing `hermes gateway restart`")
    try:
        result = subprocess.run(
            ["hermes", "gateway", "restart"],
            capture_output=True, text=True, timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        print(f"gateway-watchdog: restart invocation failed: {exc}", file=sys.stderr)
        return 1

    if result.returncode != 0:
        print(f"gateway-watchdog: restart failed (exit {result.returncode}): "
              f"{(result.stderr or '').strip()[:200]}", file=sys.stderr)
        return 1

    print("gateway-watchdog: restart command succeeded; recording in state")
    update_state_after_restart(state_file, max_window=args.window_seconds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
