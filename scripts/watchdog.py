"""Gateway watchdog — health-check detection + rate-limited restart logic.

Used by scripts/daedalus-cron.sh to detect silent gateway death (process
alive but dispatcher goroutine no longer ticking) and attempt a restart with
cooldown + rate-limit safeguards.

All side-effects (http probing, hermes CLI calls, file I/O) are injectable
for testing. Run tests with:
    pytest tests/test_watchdog.py -v
"""

import json
import os
import subprocess
import sys
import tempfile
from argparse import ArgumentParser
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Optional


# ---------------------------------------------------------------------------
# Defaults (overridable via DAEDALUS_GW_* env vars)
# ---------------------------------------------------------------------------
DEFAULT_STATE_PATH = "~/.hermes/daedalus-gateway-watchdog.json"
DEFAULT_ALERT_PATH = "~/.hermes/daedalus-gateway-watchdog.alert"
DEFAULTS = {
    "enabled": True,
    "health_port": 8900,
    "health_timeout": 5,
    "stale_threshold_hours": 2,
    "max_restarts": 3,
    "restart_window_secs": 3600,
    "cooldown_secs": 600,
    "state_path": DEFAULT_STATE_PATH,
    "alert_path": DEFAULT_ALERT_PATH,
    "dry_run": False,
}


def load_config() -> SimpleNamespace:
    """Build config from env vars (DAEDALUS_GW_*) or fall back to defaults.

    Env vars override the config file, which overrides the defaults. All
    values are cast to the expected type so callers can assume correct types.
    """
    state_file = Path(
        os.environ.get("DAEDALUS_GW_STATE_PATH", DEFAULT_STATE_PATH)
    ).expanduser()
    alert_file = Path(
        os.environ.get("DAEDALUS_GW_ALERT_PATH", DEFAULT_ALERT_PATH)
    ).expanduser()

    def _b(key: str, default: bool) -> bool:
        val = os.environ.get(key, str(default)).lower()
        return val in ("1", "true", "yes", "on")

    return SimpleNamespace(
        enabled=_b("DAEDALUS_GW_ENABLED", DEFAULTS["enabled"]),
        health_port=int(os.environ.get("DAEDALUS_GW_HEALTH_PORT", str(DEFAULTS["health_port"]))),
        health_timeout=int(os.environ.get("DAEDALUS_GW_HEALTH_TIMEOUT", str(DEFAULTS["health_timeout"]))),
        stale_threshold_hours=int(os.environ.get("DAEDALUS_GW_STALE_THRESHOLD_HOURS", str(DEFAULTS["stale_threshold_hours"]))),
        max_restarts=int(os.environ.get("DAEDALUS_GW_MAX_RESTARTS", str(DEFAULTS["max_restarts"]))),
        restart_window_secs=int(os.environ.get("DAEDALUS_GW_RESTART_WINDOW_SECS", str(DEFAULTS["restart_window_secs"]))),
        cooldown_secs=int(os.environ.get("DAEDALUS_GW_COOLDOWN_SECS", str(DEFAULTS["cooldown_secs"]))),
        state_path=state_file,
        alert_path=alert_file,
        dry_run=_b("DAEDALUS_GW_DRY_RUN", False),
    )


# ---------------------------------------------------------------------------
# State management (atomic read/write; tolerates corruption)
# ---------------------------------------------------------------------------
EMPTY_STATE = {"restarts": [], "last_restart": 0, "last_alert_sent": 0}


def load_state(path: Path) -> dict:
    """Read state file as JSON. Return EMPTY_STATE on missing/corrupt file."""
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {"restarts": [], "last_restart": 0, "last_alert_sent": 0}
    except (OSError, json.JSONDecodeError):
        return {"restarts": [], "last_restart": 0, "last_alert_sent": 0}


def save_state(path: Path, state: dict) -> None:
    """Atomically write state (write-to-tmp then rename). Idempotent."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".watchdog-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, p)  # atomic on POSIX
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def prune_restarts(restarts: list, now: int, window: int) -> list:
    """Return a new restarts list containing only entries within `window`.

    Non-numeric timestamps are silently dropped (defensive filtering).
    """
    cutoff = now - window
    result = []
    for r in restarts:
        try:
            ts = int(r.get("timestamp", 0))
        except (TypeError, ValueError):
            continue
        if ts > cutoff:
            result.append(r)
    return result


# ---------------------------------------------------------------------------
# Injected helpers (defaults do real I/O; tests swap these)
# ---------------------------------------------------------------------------
def _health_probe(port: int, timeout: int) -> bool:
    """HTTP GET /health on localhost:<port>. True iff 2xx response."""
    try:
        import urllib.request
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/health",
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= (resp.status or 0) < 300
    except Exception:
        return False


def _check_status() -> Optional[bool]:
    """Run `hermes gateway status`. True=running, False=not running, None=CLI missing."""
    try:
        out = subprocess.run(
            ["hermes", "gateway", "status"],
            capture_output=True, text=True, timeout=15,
        ).stdout.lower()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if "not running" in out:
        return False
    if "running" in out:
        return True
    return None


def _do_restart() -> bool:
    """Run `hermes gateway restart`. True on success (exit 0)."""
    try:
        result = subprocess.run(
            ["hermes", "gateway", "restart"],
            capture_output=True, text=True, timeout=60,
        )
        print(
            f"watchdog: restart exit={result.returncode}",
            file=sys.stderr,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        print(f"watchdog: restart failed: {e}", file=sys.stderr)
        return False


def _dispatch_stale(threshold_hours: int = 2) -> bool:
    """True iff hermes gateway status --deep reports no dispatch in threshold_hours+ hours."""
    try:
        out = subprocess.run(
            ["hermes", "gateway", "status", "--deep"],
            capture_output=True, text=True, timeout=15,
        ).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False
    # Accept two formats the CLI may emit for last-dispatch time.
    for prefix in ("Last dispatch:", "last_dispatch:"):
        for line in out.splitlines():
            if line.strip().lower().startswith(prefix.lower()):
                try:
                    secs = int(line.split(":", 1)[1].split()[0])
                    return secs >= threshold_hours * 3600
                except (ValueError, IndexError):
                    return False
    return False


# ---------------------------------------------------------------------------
# Gateway check + restart decision (pure functions for testability)
# ---------------------------------------------------------------------------
@dataclass
class GatewayResult:
    alive: bool       # HTTP probe succeeded (primary indicator of health)
    has_pid: bool     # `hermes gateway status` reports PID present (restart-worthy target)
    _dispatch_stale: bool  # Process alive but no dispatch activity in 2+ hours


def check_gateway(probe_fn, status_fn, stale_fn, port: int, timeout: int) -> GatewayResult:
    """Probe gateway liveness. HTTP is primary; hermes status provides PID signal.

    A "zombie" gateway is one whose HTTP probe fails but `hermes gateway status`
    reports it as still having a PID. A second kind of zombie is one where the
    probe responds but dispatch hasn't ticked in 2+ hours (silent goroutine death).
    Both cases trigger a restart attempt from run_watchdog.
    """
    probe_ok = probe_fn(port, timeout)
    status_running = status_fn()  # True/False/None (None = CLI missing)

    # alive reflects HTTP probe — true health signal
    alive = probe_ok

    # has_pid: we know a gateway process exists. Status says True → yes.
    # Status says False or None → no known PID to restart.
    has_pid = status_running is True

    # Dispatch staleness — only check if something looks alive.
    # (If probe succeeded, the goroutine could still be stuck — silent death.)
    # (If status_running but probe failed, we already know it's a zombie.)
    dispatch_stale = False
    if has_pid or probe_ok:
        dispatch_stale = stale_fn() if (not probe_ok or status_running is True) else False

    return GatewayResult(alive=alive, has_pid=has_pid, _dispatch_stale=dispatch_stale)


def is_dispatch_stale(now: int, last_dispatch: Optional[int], threshold_hours: int) -> bool:
    """True iff last_dispatch is missing or older than threshold_hours."""
    if last_dispatch is None:
        return True
    return (now - last_dispatch) >= threshold_hours * 3600


def _allowed(restarts: list, last_restart: int, now: int,
             max_n: int, window: int, cooldown: int) -> tuple:
    """Return (allowed: bool, pruned_restarts, error_reason).

    Error reason is empty string on success; otherwise 'rate_limit' or 'cooldown'.
    """
    after = prune_restarts(restarts, now, window)
    if len(after) >= max_n:
        return False, after, "rate_limit"
    if last_restart > 0 and (now - last_restart) < cooldown:
        return False, after, "cooldown"
    return True, after, ""


def decide_restart(state, now: int, max_n: int, window: int, cooldown: int) -> tuple:
    """
    Decide whether a restart is allowed given current rate-limit state.

    Returns (allowed, new_state, error_reason). `new_state` always has
    restarts pruned, even if a restart is denied.
    """
    ok, after, reason = _allowed(
        state.get("restarts", []),
        int(state.get("last_restart", 0)),
        now, max_n, window, cooldown,
    )
    new_state = {"restarts": after, "last_restart": state.get("last_restart", 0),
                 "last_alert_sent": state.get("last_alert_sent", 0)}
    return ok, new_state, reason


def record_restart(state: dict, now: int, profile: str) -> dict:
    """Append a restart-attempt entry and update last_restart."""
    state.setdefault("restarts", []).append({"timestamp": now, "profile": profile})
    state["last_restart"] = now
    return state


# ---------------------------------------------------------------------------
# Alert / notification
# ---------------------------------------------------------------------------
def write_alert(alert_path: Path, message: str) -> None:
    """Write a CRITICAL alert to disk (best-effort, never raises)."""
    try:
        p = Path(alert_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        content = f"CRITICAL: {message}\nTimestamp: {datetime.now(timezone.utc).isoformat()}\n"
        p.write_text(content)
    except Exception as e:
        print(f"watchdog: failed to write alert: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def run_watchdog(cfg=None, probe_fn=_health_probe, status_fn=_check_status,
                 restart_fn=_do_restart, stale_fn=_dispatch_stale,
                 now: Optional[int] = None,
                 out=sys.stderr) -> SimpleNamespace:
    """Main watchdog entry point. Returns a result summary (for tests / callers)."""
    if now is None:
        now = int(datetime.now(timezone.utc).timestamp())
    if cfg is None:
        cfg = load_config()

    if not cfg.enabled:
        return SimpleNamespace(
            enabled=False, checked=False, needed_restart=False, allowed=False,
            reason="disabled", alert_written=False, restart_attempted=False, restart_succeeded=False,
        )

    state = load_state(cfg.state_path)

    # Default profile is the only one we scope for now; future enhancement
    # can iterate over multiple profiles from `hermes gateway list`.
    profiles = ["DEFAULT"]

    # Wrap stale_fn to inject threshold_hours from config (#396)
    threshold_hours = getattr(cfg, "stale_threshold_hours", 2)

    def _stale_wrapper():
        import inspect
        sig = inspect.signature(stale_fn)
        if len(sig.parameters) > 0:
            return stale_fn(threshold_hours)
        else:
            return stale_fn()

    checked = False
    needed_restart = False
    allowed = False
    reason = ""
    alert_written = False
    restart_attempted = False
    restart_succeeded = False

    for profile in profiles:
        checked = True
        gw = check_gateway(probe_fn, status_fn, _stale_wrapper, cfg.health_port, cfg.health_timeout)

        need_restart = (not gw.alive and gw.has_pid) or gw._dispatch_stale

        if not need_restart:
            print(
                f"watchdog[{profile}]: gateway healthy (alive={gw.alive} pid={gw.has_pid})",
                file=out,
            )
            continue

        needed_restart = True
        print(
            f"watchdog[{profile}]: gateway DOWN (alive={gw.alive} pid={gw.has_pid} "
            f"stale={gw._dispatch_stale}) — checking rate limits",
            file=out,
        )

        allowed_decision, state, deny_reason = decide_restart(
            state, now, cfg.max_restarts, cfg.restart_window_secs, cfg.cooldown_secs,
        )

        if not allowed_decision:
            reason = deny_reason
            if deny_reason == "rate_limit":
                msg = (
                    f"Gateway watchdog — restart limit exhausted "
                    f"({cfg.max_restarts}/{cfg.restart_window_secs}s), profile={profile}. "
                    f"Manual intervention required."
                )
                print(f"watchdog[{profile}]: {msg}", file=out)
                write_alert(cfg.alert_path, msg)
                alert_written = True
            else:
                print(
                    f"watchdog[{profile}]: throttled — cooldown active "
                    f"({cfg.cooldown_secs}s between restarts)",
                    file=out,
                )
            continue

        if cfg.dry_run:
            print(
                f"watchdog[{profile}]: dry-run — would restart (not executing)",
                file=out,
            )
            state = record_restart(state, now, profile)
            save_state(cfg.state_path, state)
            reason = "dry_run"
            continue

        print(f"watchdog[{profile}]: attempting restart", file=out)
        success = restart_fn()
        restart_attempted = True
        if success:
            restart_succeeded = True
            print(f"watchdog[{profile}]: restart succeeded", file=out)
        else:
            print(f"watchdog[{profile}]: restart command failed", file=out)

        state = record_restart(state, now, profile)
        save_state(cfg.state_path, state)
        reason = "succeeded" if success else "restart_failed"

    return SimpleNamespace(
        enabled=True, checked=checked, needed_restart=needed_restart,
        allowed=allowed or (reason == "succeeded"),
        reason=reason, alert_written=alert_written,
        restart_attempted=restart_attempted, restart_succeeded=restart_succeeded,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _cli() -> int:
    p = ArgumentParser("watchdog")
    p.add_argument("--dry-run", action="store_true", help="Run detection; do not actually restart")
    args = p.parse_args()

    if args.dry_run:
        os.environ["DAEDALUS_GW_DRY_RUN"] = "1"

    cfg = load_config()
    run_watchdog(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
