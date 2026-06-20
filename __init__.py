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


def register(ctx) -> None:
    """Hermes plugin entry point — import-safe, never raises.

    Registers the daedalus dispatcher as an auxiliary LLM task so users
    can configure its provider/model independently of the main chat model.
    Also registers an on_session_end hook so any worker completion triggers
    dispatch immediately instead of waiting for the next 60-min cron tick.
    """
    try:
        ctx.register_auxiliary_task(
            key="daedalus_dispatch",
            display_name="Daedalus Dispatch",
            description="Issue/spec → reviewed-PR: scans GitHub Projects boards and kanban queues, "
                        "decomposes triage cards, dispatches worker agents.",
        )
        ctx.register_hook("on_session_end", _on_session_end)
    except Exception:
        logger.debug("daedalus register() failed", exc_info=True)
