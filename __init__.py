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

logger = logging.getLogger(__name__)


def register(ctx) -> None:
    """Hermes plugin entry point — import-safe, never raises.

    Registers the daedalus dispatcher as an auxiliary LLM task so users
    can configure its provider/model independently of the main chat model.
    """
    try:
        ctx.register_auxiliary_task(
            key="daedalus_dispatch",
            display_name="Daedalus Dispatch",
            description="Issue/spec → reviewed-PR: scans GitHub Projects boards and kanban queues, "
                        "decomposes triage cards, dispatches worker agents.",
        )
    except Exception:
        logger.debug("daedalus register() failed", exc_info=True)
