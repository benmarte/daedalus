"""core.dispatch.direct_dispatch — spawn the coding-agent wrapper directly (#1329).

When ``execution.direct_delegate`` is on AND a CLI ``coding_agent`` is configured,
daedalus spawns ``daedalus-delegate.sh --relay-verdict --role <role>`` for each
dispatchable *review/validator/pm/planner* card **itself** — claim → spawn — instead
of letting ``hermes kanban dispatch`` spawn a local-model (qwen) orchestrator that
must *decide* to delegate. That deciding hop is non-deterministic: it either delegated
(the relay path, fixed role-aware in #1330) or ran the work inline and never completed
the card (a stall that only the 30-min sweeper recovers). Removing the hop makes the
delegated stages deterministic.

Safe by construction:
- Default OFF (``direct_delegate`` unset) → :func:`direct_dispatch` is a no-op that
  returns 0, and the caller's normal ``kanban.dispatch`` does everything → byte-identical.
- Only NON-developer delegated roles are handled here; the developer keeps its existing
  worktree-spawn path. After this runs, the caller still calls ``kanban.dispatch``, which
  skips the cards this already claimed (they are ``running``) and handles the rest.
- Requires ``kanban.dispatch_in_gateway=false`` so daedalus is the sole dispatcher
  (otherwise the gateway daemon would also spawn a qwen agent for the same card).
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from core import kanban
from core.dispatch.bodies import _DELEGATION_MARKER, _inner_task_body, _ROLE_TMP_PREFIX
from core.dispatch.resolvers import (
    _DEFAULT_PROFILES,
    _apply_coding_agent_max_turns,
    _resolve_coding_agent,
    _resolve_coding_agent_cmd,
)
from core.util import extract_issue_number

logger = logging.getLogger("daedalus.dispatch.direct")

_PLUGIN_SCRIPTS = Path.home() / ".hermes" / "plugins" / "daedalus" / "scripts"
_DELEGATE_SH = _PLUGIN_SCRIPTS / "daedalus-delegate.sh"

# Statuses a fresh, not-yet-claimed card can have. A card this function claims
# moves to ``running`` and is thereafter skipped (claim fails on a running card).
_DISPATCHABLE_STATUS = "todo"

# Developer keeps its own worktree-spawn path (it works, and its --cmd needs shell
# env-var expansion that delegate.sh's single --cmd arg handles but a bare argv does
# not). Only these delegated roles are direct-spawned here.
_DIRECT_ROLES = frozenset(
    {"validator", "pm", "planner", "qa", "reviewer", "security", "accessibility", "documentation"}
)


def _default_spawn(*, card: str, board: str, cmd: str, role: str, taskf: str, outf: str) -> None:
    """Spawn the one-shot delegate wrapper detached (its own session), so it
    survives this dispatch process exiting. It heartbeats (refreshing the claim)
    and relays the verdict (role-aware complete/block, #1330)."""
    argv = [
        "bash", str(_DELEGATE_SH),
        "--task-file", taskf,
        "--cmd", cmd,
        "--card", card,
        "--board", board,
        "--out", outf,
        "--role", role,
        "--relay-verdict",
    ]
    subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def direct_dispatch(
    slug: str,
    resolved: Dict[str, Any],
    *,
    max_spawns: int = 1,
    dry_run: bool = False,
    profiles: Optional[Dict[str, str]] = None,
    spawn: Optional[Callable[..., None]] = None,
) -> int:
    """Directly spawn delegate wrappers for dispatchable non-developer delegated
    cards. Returns the number spawned (0 when the flag is off / no coding agent).

    The caller should still run its normal ``kanban.dispatch`` afterwards.
    """
    execution = (resolved or {}).get("execution") or {}
    if not execution.get("direct_delegate"):
        return 0
    agent = _resolve_coding_agent(execution)
    if agent in ("", "hermes", "none"):
        return 0
    cmd = _apply_coding_agent_max_turns(agent, _resolve_coding_agent_cmd(execution), execution)
    if not cmd:
        return 0

    role_by_assignee = {
        (a or "").strip(): r
        for r, a in (profiles or _DEFAULT_PROFILES).items()
        if (a or "").strip()
    }
    spawn = spawn or _default_spawn
    spawned = 0

    for card in kanban.list_tasks(slug, status=_DISPATCHABLE_STATUS):
        if spawned >= max_spawns:
            break
        role = role_by_assignee.get((card.get("assignee") or "").strip())
        if role not in _DIRECT_ROLES:
            continue  # developer / unknown → normal path
        cid = card.get("id")
        if not cid:
            continue
        detail = kanban.show_card(slug, cid) or card
        body = detail.get("body") or ""
        if _DELEGATION_MARKER not in body:
            continue  # not a delegated body → let the normal path handle it
        inner = _inner_task_body(body)
        issue = extract_issue_number(detail.get("title") or card.get("title") or "") or 0
        pfx = _ROLE_TMP_PREFIX.get(role, role)
        taskf = f"/tmp/{pfx}-{issue}-task.txt"
        outf = f"/tmp/{pfx}-{issue}-out.txt"

        if dry_run:
            logger.info(
                "direct-dispatch: [dry-run] would spawn %s wrapper for #%s (card %s)",
                role, issue, cid,
            )
            spawned += 1
            continue

        try:
            Path(taskf).write_text(inner, encoding="utf-8")
        except OSError as exc:
            logger.warning("direct-dispatch: task-file write failed %s: %s", taskf, exc)
            continue
        # Claim starts the run (running + run_id) so complete/block have a run to
        # close; delegate.sh heartbeats to refresh the claim TTL. A claim failure
        # means the card is already running/claimed — skip it (no double-spawn).
        if not kanban.claim(slug, cid):
            logger.info("direct-dispatch: claim failed for %s — already running? skipping", cid)
            continue
        try:
            spawn(card=cid, board=slug, cmd=cmd, role=role, taskf=taskf, outf=outf)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("direct-dispatch: spawn failed for %s (%s): %s", role, cid, exc)
            continue
        logger.info(
            "direct-dispatch: spawned %s wrapper for #%s (card %s) — no local-model hop (#1329)",
            role, issue, cid,
        )
        spawned += 1

    return spawned
