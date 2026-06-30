"""Register the daedalus session-end advance hook in a Hermes profile config.

The daedalus pipeline advances in near-real-time by firing
``daedalus-advance.sh`` from each profile's ``hooks.on_session_end`` when a
worker session ends. Copying the script into ``~/.hermes/agent-hooks/`` is NOT
sufficient: Hermes only runs a profile-agent's session-end hooks when they are
registered in that profile's ``config.yaml``. Profiles missing the block (e.g.
``planner-daedalus`` in issue #962) silently stall until the next hourly cron
tick — up to a 60-minute delay per pipeline stage.

``provision_roster.sh`` calls this module for every role so the registration can
never drift per-role again. The mutation is idempotent and non-destructive:
re-running provisioning leaves exactly one ``daedalus-advance.sh`` command entry
and preserves any other config keys.
"""

from __future__ import annotations

import sys

import yaml

DEFAULT_TIMEOUT = 90


def ensure_advance_hook(
    cfg: dict, hook_command: str, timeout: int = DEFAULT_TIMEOUT
) -> dict:
    """Idempotently register the advance hook in a profile config dict.

    Adds ``hooks.on_session_end`` → ``{command: hook_command, timeout: timeout}``
    and sets ``hooks_auto_accept: true``. Matches existing entries on the command
    path so a second application is a no-op (no duplicate entry). All other
    existing config — including unrelated ``hooks`` keys — is preserved.

    Mutates and returns ``cfg`` for convenience.
    """
    hooks = cfg.setdefault("hooks", {})
    on_session_end = hooks.get("on_session_end") or []
    already_registered = any(
        isinstance(entry, dict) and entry.get("command") == hook_command
        for entry in on_session_end
    )
    if not already_registered:
        on_session_end.append({"command": hook_command, "timeout": timeout})
    hooks["on_session_end"] = on_session_end
    cfg["hooks_auto_accept"] = True
    return cfg


def register_in_file(
    config_path: str, hook_command: str, timeout: int = DEFAULT_TIMEOUT
) -> None:
    """Apply :func:`ensure_advance_hook` to a profile config.yaml in place.

    A missing config file is treated as an empty config (the file is created).
    Output preserves key order and uses block style to match the rest of the
    provisioner's YAML writes.
    """
    try:
        with open(config_path) as f:
            cfg = yaml.safe_load(f) or {}
    except FileNotFoundError:
        cfg = {}

    ensure_advance_hook(cfg, hook_command, timeout)

    with open(config_path, "w") as f:
        yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=False)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(
            "usage: register_advance_hook.py <config.yaml> <hook_command> [timeout]",
            file=sys.stderr,
        )
        return 2
    config_path = argv[0]
    hook_command = argv[1]
    timeout = int(argv[2]) if len(argv) > 2 else DEFAULT_TIMEOUT
    register_in_file(config_path, hook_command, timeout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
