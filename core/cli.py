"""Thin subprocess wrapper for the Hermes CLI.

Single entry-point for all `hermes <args>` calls so every module degrades
the same way and logging is consistent.
"""
from __future__ import annotations

import logging
import subprocess

logger = logging.getLogger("daedalus.cli")


def hermes_cli(args: list[str], timeout: int = 30) -> tuple[int, str]:
    """Run ``hermes <args>``. Returns ``(returncode, combined stdout+stderr)``.

    Never raises — errors are captured and returned as negative return codes
    with a descriptive string so every caller can degrade gracefully.
    """
    try:
        r = subprocess.run(
            ["hermes"] + args,
            capture_output=True, text=True, timeout=timeout,
        )
        return r.returncode, (r.stdout + r.stderr).strip()
    except FileNotFoundError:
        return -1, "hermes CLI not found"
    except subprocess.TimeoutExpired:
        cmd = args[0] if args else "command"
        return -1, f"hermes {cmd} timed out after {timeout}s"
    except OSError as exc:
        return -1, str(exc)
