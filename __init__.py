"""Hermes Daedalus — native issue/spec → reviewed-PR automation.

Package marker and plugin entry point. The installable-plugin entry (`register(ctx)` +
`plugin.yaml`) wires the dispatcher (`scripts/daedalus_dispatch.py`) as an
auxiliary task. The dashboard tab (`dashboard/`) and native-wrapper core (`core/`,
`config/`) remain available but are not wired through the plugin system.

We deliberately do NOT insert this dir onto the global sys.path: doing so shadows
Hermes's own top-level modules (cli, tools, config, …) for every agent process.
Entrypoints (scripts/, dashboard/plugin_api.py) set up their own path locally.
"""

import logging
import os
import subprocess
import threading
from typing import Optional

logger = logging.getLogger(__name__)


def _on_session_end(session_id, completed, interrupted, model, platform, **kwargs):
    """Fire the daedalus dispatcher immediately after any worker session ends.

    Only triggers when the session is a Hermes kanban worker (HERMES_KANBAN_TASK
    is set by the kanban dispatcher). Runs daedalus-cron.sh in a daemon thread
    so it never blocks agent teardown. Exceptions are caught and logged at DEBUG
    to satisfy the Hermes hook contract (hooks must not raise).
    """
    if not os.environ.get("HERMES_KANBAN_TASK"):
        return
    hermes_home = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
    cron_script = os.path.join(hermes_home, "scripts", "daedalus-cron.sh")
    if not (os.path.isfile(cron_script) and os.access(cron_script, os.X_OK)):
        logger.debug("daedalus on_session_end: cron script missing or not executable: %s", cron_script)
        return

    def _run():
        try:
            subprocess.run(
                ["bash", cron_script],
                env=os.environ.copy(),
                timeout=120,
                check=False,
                capture_output=True,
            )
        except Exception as exc:
            logger.debug("daedalus on_session_end dispatch failed: %s", exc)

    threading.Thread(target=_run, name="daedalus-advance", daemon=True).start()


def _on_kanban_task_claimed(task_id, board, assignee, run_id, **kwargs):
    """Sync the global Hermes model config into the profile before it runs.

    Fires when any kanban task is claimed. For daedalus profiles (name ends
    with '-daedalus'), copies model/providers/fallback_providers/custom_providers
    from ~/.hermes/config.yaml into the profile's config.yaml so the profile
    always uses whatever model is selected in Hermes — no manual re-provisioning
    needed after a model switch.

    Per-profile override: set ``_daedalus_model_override: true`` in the
    profile's config.yaml to opt out and lock that profile to a specific model.
    """
    if not assignee or not str(assignee).endswith("-daedalus"):
        return
    try:
        import yaml
        hermes_home = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
        global_cfg_path = os.path.join(hermes_home, "config.yaml")
        profile_cfg_path = os.path.join(hermes_home, "profiles", str(assignee), "config.yaml")

        if not os.path.isfile(global_cfg_path) or not os.path.isfile(profile_cfg_path):
            return

        with open(global_cfg_path) as f:
            global_cfg = yaml.safe_load(f) or {}
        with open(profile_cfg_path) as f:
            profile_cfg = yaml.safe_load(f) or {}

        if profile_cfg.get("_daedalus_model_override"):
            return

        sync_keys = ("model", "providers", "fallback_providers", "custom_providers")
        changed = False
        for key in sync_keys:
            global_val = global_cfg.get(key)
            if profile_cfg.get(key) != global_val:
                if global_val is None:
                    profile_cfg.pop(key, None)
                else:
                    profile_cfg[key] = global_val
                changed = True

        if changed:
            with open(profile_cfg_path, "w") as f:
                yaml.safe_dump(profile_cfg, f, default_flow_style=False, sort_keys=False)
            logger.debug("daedalus: synced model config into profile %s", assignee)
    except Exception as exc:
        logger.debug("daedalus kanban_task_claimed sync failed: %s", exc)


def _read_env_value(env_path: str, key: str) -> Optional[str]:
    """Return the value of ``key`` from a dotenv-style file, or None.

    Parses ``KEY=value`` lines (ignoring blanks, comments, and ``export``
    prefixes). Surrounding quotes are stripped. Returns the first match.
    """
    try:
        with open(env_path) as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):].lstrip()
                name, sep, value = line.partition("=")
                if sep and name.strip() == key:
                    value = value.strip()
                    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                        value = value[1:-1]
                    return value
    except OSError:
        return None
    return None


def _sync_github_token() -> None:
    """Copy GITHUB_TOKEN from ~/.hermes/.env into every *-daedalus profile .env.

    provision_roster.sh only writes GITHUB_TOKEN into each profile's .env when a
    token is resolvable at provision time. On a fresh install where the token is
    only added to ~/.hermes/.env afterwards (or was absent during provisioning),
    the profiles are left permanently missing the token and agents fail with
    GitHub auth errors until someone re-provisions (issue #78).

    This runs on every plugin load and, for each ``~/.hermes/profiles/*-daedalus``
    profile whose .env lacks a ``GITHUB_TOKEN=`` line, appends the token from
    ~/.hermes/.env. Idempotent — profiles that already have the key are skipped,
    so the value is never duplicated or overwritten. Never raises.
    """
    try:
        hermes_home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
        token = _read_env_value(os.path.join(hermes_home, ".env"), "GITHUB_TOKEN")
        if not token:
            return

        profiles_dir = os.path.join(hermes_home, "profiles")
        if not os.path.isdir(profiles_dir):
            return

        for name in os.listdir(profiles_dir):
            if not name.endswith("-daedalus"):
                continue
            profile_dir = os.path.join(profiles_dir, name)
            if not os.path.isdir(profile_dir):
                continue
            env_file = os.path.join(profile_dir, ".env")

            # Idempotent: skip if a GITHUB_TOKEN line already exists.
            if _read_env_value(env_file, "GITHUB_TOKEN") is not None:
                continue

            try:
                # Match a trailing-newline-safe append, mirroring provision_roster.sh.
                with open(env_file, "a") as f:
                    f.write(f"\nGITHUB_TOKEN={token}\n")
                os.chmod(env_file, 0o600)
                logger.debug("daedalus: synced GITHUB_TOKEN into profile %s", name)
            except OSError as exc:
                logger.debug("daedalus: token sync failed for %s: %s", name, exc)
    except Exception:
        logger.debug("daedalus: github token sync failed", exc_info=True)


def _ensure_cron_wrapper() -> None:
    """Make sure ~/.hermes/scripts/daedalus-cron.sh exists on every plugin load.

    Hermes has no ``post_install`` hook for plugins — it only clones the repo —
    so ``scripts/postinstall.py`` is never run automatically by ``hermes plugin
    add``/``hermes update``. Without this, fresh installs leave the cron job
    pointing at a script that does not exist, and the cron silently fails.

    We load ``_install_cron_wrapper()`` from ``scripts/postinstall.py`` by file
    path (the scripts dir is deliberately not on ``sys.path`` — see the module
    docstring) and run it on every ``register()``. The write is idempotent
    (writes the file + chmod +x), so repeating it on every load is safe and
    cheap. Failures are logged, never raised.
    """
    try:
        import importlib.util

        postinstall_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "scripts", "postinstall.py"
        )
        spec = importlib.util.spec_from_file_location(
            "daedalus_postinstall", postinstall_path
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        ok, msg = module._install_cron_wrapper()
        if not ok:
            logger.warning("daedalus: %s", msg)
    except Exception:
        logger.debug("daedalus: cron wrapper install failed", exc_info=True)


def register(ctx) -> None:
    """Hermes plugin entry point — import-safe, never raises.

    Registers the daedalus dispatcher as an auxiliary LLM task so users
    can configure its provider/model independently of the main chat model.
    Also registers an on_session_end hook so any worker completion triggers
    dispatch immediately instead of waiting for the next 60-min cron tick,
    and a kanban_task_claimed hook to keep daedalus profile model configs
    in sync with the global Hermes model selection.

    Finally, it (re)installs the daedalus-cron.sh wrapper on every load so
    fresh installs and post-update environments always have the script the
    dispatcher cron job invokes — Hermes provides no post-install hook — and
    syncs GITHUB_TOKEN from ~/.hermes/.env into any *-daedalus profile .env
    that lacks it, so profiles provisioned before the token was set still work.
    """
    try:
        ctx.register_auxiliary_task(
            key="daedalus_dispatch",
            display_name="Daedalus Dispatch",
            description="Issue/spec → reviewed-PR: scans GitHub Projects boards and kanban queues, "
                        "decomposes triage cards, dispatches worker agents.",
        )
        ctx.register_hook("on_session_end", _on_session_end)
        ctx.register_hook("kanban_task_claimed", _on_kanban_task_claimed)
    except Exception:
        logger.debug("daedalus register() failed", exc_info=True)

    # Idempotent — each runs in its own try/except so it never blocks registration.
    _ensure_cron_wrapper()
    _sync_github_token()
