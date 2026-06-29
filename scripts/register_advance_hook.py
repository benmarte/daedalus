#!/usr/bin/env python3
"""Register the daedalus session-end advance hook in a Hermes profile config.yaml.

The daedalus pipeline advances in near-real-time only when each Hermes profile's
``config.yaml`` lists the advance hook under ``hooks.on_session_end``. Hermes fires
a profile's ``on_session_end`` commands when that profile's agent session ends; the
advance hook (``daedalus-advance.sh``) then resolves the worker's project and
dispatches the next lifecycle stage immediately instead of waiting up to 60 minutes
for the hourly cron tick.

If the block is absent, the session ends and the hook never runs — the pipeline
stalls. This was the ``planner-daedalus`` stall in issue #962: ``provision_roster.sh``
never wrote the block for any role, so roles that hadn't acquired it via external
edits silently never self-advanced.

This module exposes :func:`register_advance_hook` (importable + unit-tested) and a
thin CLI so ``provision_roster.sh`` can call it once per profile during setup.
"""

from __future__ import annotations

import sys
from typing import Any

import yaml

ADVANCE_HOOK_BASENAME = "daedalus-advance.sh"
DEFAULT_TIMEOUT = 90


def register_advance_hook(
    cfg: dict[str, Any],
    advance_hook_path: str,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Ensure ``cfg`` registers the advance hook on session end. Mutates and returns ``cfg``.

    Idempotent: the ``on_session_end`` list keeps exactly one ``daedalus-advance.sh``
    command entry regardless of how many times this runs (matched by command basename,
    so a differing home prefix never produces a duplicate). Non-destructive: existing
    ``hooks`` keys and all other config are preserved.
    """
    hooks = cfg.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = {}
        cfg["hooks"] = hooks

    on_session_end = hooks.get("on_session_end")
    if not isinstance(on_session_end, list):
        on_session_end = []

    already_registered = any(
        isinstance(entry, dict)
        and str(entry.get("command", "")).rstrip("/").endswith(ADVANCE_HOOK_BASENAME)
        for entry in on_session_end
    )
    if not already_registered:
        on_session_end.append({"command": advance_hook_path, "timeout": timeout})

    hooks["on_session_end"] = on_session_end
    cfg["hooks_auto_accept"] = True
    return cfg


def register_in_file(config_path: str, advance_hook_path: str) -> None:
    """Load ``config_path``, register the advance hook, and write it back in place."""
    try:
        with open(config_path) as f:
            cfg = yaml.safe_load(f) or {}
    except FileNotFoundError:
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}

    register_advance_hook(cfg, advance_hook_path)

    with open(config_path, "w") as f:
        yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=False)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(
            "usage: register_advance_hook.py <profile_config.yaml> <advance_hook_path>",
            file=sys.stderr,
        )
        return 2
    register_in_file(argv[1], argv[2])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
