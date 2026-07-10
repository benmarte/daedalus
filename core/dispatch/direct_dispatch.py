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
from typing import Any, Callable, Dict, List, Optional

from core import dispatch_state, kanban
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

# Statuses a fresh, not-yet-claimed card can have — the same buckets
# ``hermes kanban dispatch`` consumes. With ``dispatch_in_gateway=false`` a newly
# created daedalus card sits in ``ready`` (NOT ``todo``), so ``ready`` must be first;
# ``todo`` is included for robustness. A card this function claims moves to
# ``running`` and is thereafter skipped (claim fails on a running card). (#1333)
_DISPATCHABLE_STATUSES = ("ready", "todo")

# A parent card in one of these states no longer gates its children (#1339).
_TERMINAL_STATUSES = frozenset({"done", "complete", "completed", "cancelled", "archived"})

# Every pipeline role is direct-delegated to the configured coding agent when
# ``direct_delegate`` is on — including the developer (#1339). Uniformity is the point:
# in coding-agent mode NO role touches the local model. The developer is special only
# in that it writes code + opens a PR, so delegate.sh gives it an isolated per-issue
# worktree and transitions its card by PR-detection (not verdict relay); see
# ``_DEVELOPER_ROLE`` handling below and in daedalus-delegate.sh.
_DEVELOPER_ROLE = "developer"
_DIRECT_ROLES = frozenset(
    {"validator", "pm", "planner", "qa", "reviewer", "security",
     "accessibility", "documentation", _DEVELOPER_ROLE}
)

# The developer's deliverable is an opened PR, not a verdict signal — so it gets a
# different override than the review roles: still "don't touch kanban" (delegate.sh
# completes the card from the detected PR), but "open the PR" instead of "emit a
# signal line".
_DEV_MODE_OVERRIDE = (
    "\n\n---\n"
    "⚠️ RELAY MODE — THE DISPATCHER RECORDS YOUR RESULT FOR YOU.\n"
    "Do NOT run `hermes kanban complete`, `hermes kanban block`, or ANY other kanban "
    "state command — this OVERRIDES any step above that tells you to complete/block your "
    "own card. Your deliverable is an OPEN pull request on the branch this worktree is "
    "already checked out on; commit your work and open the PR (e.g. via `gh pr create`). "
    "The dispatcher detects the PR on that branch and completes your card automatically.\n"
)

# Relay-mode override appended to every directly-spawned inner task body (#1329).
# The shared role bodies instruct the agent to complete/block ITS OWN kanban card
# (correct under the legacy `hermes -p` orchestrator, which owns the card). Under
# direct-delegate the wrapper (delegate.sh --relay-verdict) owns the transition and
# relays the verdict the agent EMITS. If the inner agent also runs a kanban state
# command it races the relay: the agent's bare `complete` (no --result) usually wins,
# leaving the card done with an empty result, which the dispatcher reads as an empty
# completion and re-creates the card (duplicate loop). This directive supersedes the
# self-completion step so the agent only emits its verdict and never touches kanban.
_RELAY_MODE_OVERRIDE = (
    "\n\n---\n"
    "⚠️ RELAY MODE — THE DISPATCHER RECORDS YOUR VERDICT FOR YOU.\n"
    "Do NOT run `hermes kanban complete`, `hermes kanban block`, or ANY other kanban "
    "state command. Your card is transitioned automatically from the verdict you emit. "
    "This OVERRIDES any step above that tells you to complete or block your own card.\n"
    "Instead, emit your verdict as your FINAL assistant message, beginning with the EXACT "
    "signal prefix the steps above specify (e.g. `CONFIRMED:`, `spec:`, `qa-passed`, "
    "`review-approved`, `docs posted`, `ESCALATE:`, `BLOCKED:`). Any kanban command you "
    "run will race the dispatcher and corrupt the pipeline.\n"
)


# Default developer PR grace window (#1375). Mirrors the delegate.sh default so a
# repo that never sets ``pipeline.developer_pr_grace_secs`` gets the same 120s poll.
_DEFAULT_PR_GRACE_SECS = 120

# Cross-tick single-flight marker TTL buffer (#1404). The persistent developer
# marker suppresses a second dispatch for at most ``pr_grace_secs + this`` seconds
# — long enough to cover the first delegate's post-exit PR-grace poll plus one
# cron tick (the window where the first card can go done-without-PR yet its
# branch/worktree still lingers), but short enough that a genuinely-dead
# developer is re-dispatched promptly on a later tick (a qa-fix round, which
# follows a full QA run, always lands well past this window).
_MARKER_TTL_BUFFER_SECS = 120


def _running_processes() -> List[str]:
    """Return the command line of every process on the host (best-effort).

    Used by the developer single-flight guard (#1375) to detect a live delegate /
    inner coding-agent still working a branch. Never raises — returns [] if ``ps``
    is unavailable, so the guard fails open (a transient ps failure must never
    permanently block the developer stage)."""
    try:
        out = subprocess.run(
            ["ps", "-eo", "args"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:  # pragma: no cover - defensive (ps missing / timeout)
        return []
    return [ln for ln in out.stdout.splitlines() if ln.strip()]


def _developer_delegate_in_flight(
    slug: str,
    issue: int,
    branch: str,
    cid: str,
    *,
    kanban_mod: Any = kanban,
    ps_lines: Optional[List[str]] = None,
    developer_profile: str = "",
    workdir: str = "",
    marker_ttl_secs: float = 0.0,
) -> bool:
    """Developer single-flight guard (#1375 + #1404): is a developer delegate
    already (or very recently) working this issue/branch?

    The concurrent-delegate hazard is a re-spawn (crash-retry unblock, or a second
    ``todo`` card) firing a second developer onto the SAME per-issue branch /
    worktree while the first is still editing it — two agents clobbering one tree.
    The ``kanban.claim`` gate only protects re-claim of the *same running card*; it
    does NOT catch a detached delegate whose card was already blocked+unblocked, nor
    a distinct second developer card for the same issue. This guard closes all three:

      1. A live delegate / inner-agent PROCESS referencing this branch or its
         deterministic worktree path (``ps`` scan) — the authoritative liveness
         signal that survives card churn.
      2. Another RUNNING developer card for the same issue (a second card minted
         for one issue) — a board-level double-dispatch.
      3. A persistent cross-tick marker (#1404) showing a developer was dispatched
         for this issue within ``marker_ttl_secs`` — closes the gap (1)/(2) miss
         when the FIRST developer card already went terminal (done-without-PR)
         before the second dispatch: no live process, no running card, yet the
         first's branch/worktree still lingers and a second dispatch would collide
         with (or force-reset) it. TTL-bounded so a dead developer re-dispatches
         on a later tick.

    Fail-open (returns False) on any inspection error so a transient failure never
    permanently blocks the developer stage. ``ps_lines`` is injectable for tests.
    """
    if not branch or not issue:
        return False
    # (1) live process referencing this branch or its worktree path
    try:
        lines = _running_processes() if ps_lines is None else ps_lines
        worktree_frag = f".worktrees/dev-{issue}"
        for ln in lines:
            if branch in ln or worktree_frag in ln:
                logger.info(
                    "single-flight: live developer delegate for #%s detected in "
                    "process table — suppressing second dispatch (#1375): %s",
                    issue, ln[:160],
                )
                return True
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("single-flight: ps scan failed for #%s: %s", issue, exc)
    # (2) another running developer card for the same issue (board double-dispatch)
    if developer_profile:
        try:
            for t in kanban_mod.list_tasks(slug):
                tid = str(t.get("id") or t.get("task_id") or "")
                if not tid or tid == cid:
                    continue
                if (t.get("assignee") or "").strip() != developer_profile:
                    continue
                if extract_issue_number(t.get("title") or "") != issue:
                    continue
                if (t.get("status") or "").lower() == "running":
                    logger.info(
                        "single-flight: developer card %s for #%s already running — "
                        "suppressing second dispatch of %s (#1375)",
                        tid, issue, cid,
                    )
                    return True
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("single-flight: card scan failed for #%s: %s", issue, exc)
    # (3) persistent cross-tick marker (#1404): a developer was dispatched for this
    # issue within the grace window. Catches the done-without-PR race where the
    # first card is already terminal (so (1)/(2) see nothing) but its branch/worktree
    # still lingers and a second dispatch would collide with it.
    if workdir and marker_ttl_secs > 0:
        try:
            age = dispatch_state.get_developer_dispatch_age_secs(workdir, issue)
            if age is not None and age < marker_ttl_secs:
                logger.info(
                    "single-flight: developer for #%s dispatched %.0fs ago (< %.0fs "
                    "marker TTL) — suppressing second dispatch (#1404)",
                    issue, age, marker_ttl_secs,
                )
                return True
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("single-flight: marker check failed for #%s: %s", issue, exc)
    return False


def _default_spawn(
    *, card: str, board: str, cmd: str, role: str, taskf: str, outf: str,
    repo: str = "", branch: str = "", base: str = "", pr_grace_secs: int = 0,
) -> None:
    """Spawn the one-shot delegate wrapper detached (its own session), so it
    survives this dispatch process exiting. It heartbeats (refreshing the claim),
    relays the verdict (role-aware complete/block, #1330), and — because a delegated
    ``claude -p`` is NOT a Hermes session and so never fires the profile
    ``on_session_end`` advance hook — fires a scoped advance dispatch itself (needs
    ``--repo``) so the next stage starts in seconds instead of at the next cron tick."""
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
    if repo:
        argv += ["--repo", repo]
    if branch:
        argv += ["--branch", branch]
    if base:
        argv += ["--base", base]
    if pr_grace_secs:
        argv += ["--pr-grace-secs", str(pr_grace_secs)]
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
    # Repo path for the delegate's scoped advance dispatch (#1339). ``workdir`` is the
    # per-repo config field; fall back to the resolved ``repo`` path if present.
    repo_path = str((resolved or {}).get("workdir") or (resolved or {}).get("repo_path") or "")
    base_branch = str(((resolved or {}).get("vcs") or {}).get("target_branch") or "dev")
    # Developer PR grace window (#1375) — passed to delegate.sh so a slow-but-healthy
    # developer that opens its PR shortly after the inner agent exits is not declared
    # coding-agent-failed (which would trigger a concurrent re-spawn).
    _pipeline_cfg = (resolved or {}).get("pipeline") or {}
    try:
        pr_grace_secs = int(_pipeline_cfg.get("developer_pr_grace_secs", _DEFAULT_PR_GRACE_SECS))
    except (TypeError, ValueError):
        pr_grace_secs = _DEFAULT_PR_GRACE_SECS

    role_by_assignee = {
        (a or "").strip(): r
        for r, a in (profiles or _DEFAULT_PROFILES).items()
        if (a or "").strip()
    }
    spawn = spawn or _default_spawn
    spawned = 0
    seen: set = set()

    # Reset the per-tick list_tasks cache first (#1142): a card created earlier in
    # THIS tick is invisible to a cached read, and ``hermes kanban dispatch`` (a fresh
    # subprocess) would then grab it and spawn a qwen agent before we do. Reset forces
    # direct_dispatch to see freshly-created cards, so it claims them first.
    kanban.reset_tick_cache()
    cards = [c for st in _DISPATCHABLE_STATUSES for c in kanban.list_tasks(slug, status=st)]
    # id -> status across ALL cards, to gate on parent-completion (#1339). The gateway
    # dispatch path won't run a card whose upstream isn't done; direct_dispatch must
    # honor the same ordering or it fires QA/reviewer/security/docs (each created with
    # parents=[upstream]) the moment their `todo` cards exist — before the developer's PR.
    _status_by_id = {
        c.get("id"): (c.get("status") or "").lower() for c in kanban.list_tasks(slug)
    }
    for card in cards:
        if spawned >= max_spawns:
            break
        role = role_by_assignee.get((card.get("assignee") or "").strip())
        if role not in _DIRECT_ROLES:
            continue  # developer / unknown → normal path
        cid = card.get("id")
        if not cid or cid in seen:
            continue  # dedup across status buckets
        seen.add(cid)
        # show_card nests the card fields under a "task" key
        # ({"task": {...id,title,body...}, "children":..., "events":...}); the body is
        # NOT at the top level. Read from task, falling back to the list_tasks card.
        detail = kanban.show_card(slug, cid) or {}
        task = detail.get("task") or detail or card
        body = task.get("body") or card.get("body") or ""
        if _DELEGATION_MARKER not in body:
            continue  # not a delegated body → let the normal path handle it
        # Gate on parent-completion: a card whose upstream (developer → qa → reviewer …)
        # is not yet terminal must NOT be dispatched, or the pipeline ordering breaks
        # (QA reviews a PR that does not exist yet). Parents come back as id strings.
        parents = detail.get("parents") or task.get("parents") or []
        parent_ids = [
            p if isinstance(p, str) else (p.get("id") if isinstance(p, dict) else None)
            for p in parents
        ]
        if any(
            _status_by_id.get(pid, "") not in _TERMINAL_STATUSES
            for pid in parent_ids
            if pid
        ):
            continue  # upstream not done — respect pipeline ordering (#1339)
        issue = extract_issue_number(task.get("title") or card.get("title") or "") or 0
        is_dev = role == _DEVELOPER_ROLE
        inner = _inner_task_body(body) + (_DEV_MODE_OVERRIDE if is_dev else _RELAY_MODE_OVERRIDE)
        # The developer works on a deterministic per-issue branch in its own worktree
        # (delegate.sh creates it and detects the PR there); other roles have no branch.
        branch = f"fix/issue-{issue}" if is_dev else ""
        pfx = _ROLE_TMP_PREFIX.get(role, role)
        # Include the card id in the temp paths: the dispatcher can create more than one
        # card for the same (role, issue) — a retry after an empty completion, or a
        # concurrent tick — and a path keyed only on {pfx}-{issue} lets two delegate.sh
        # instances clobber each other's task/out files mid-run (#1329).
        taskf = f"/tmp/{pfx}-{issue}-{cid}-task.txt"
        outf = f"/tmp/{pfx}-{issue}-{cid}-out.txt"

        # Developer single-flight guard (#1375): never spawn a second developer
        # delegate for an issue while one is still live on its branch/worktree. A
        # crash-retry unblock (or a duplicate developer card) would otherwise fire a
        # concurrent agent onto the same `.worktrees/dev-<n>` checkout — a data-loss
        # hazard the `kanban.claim` gate below does NOT cover (it only guards the same
        # running card). Checked BEFORE claim so a suppressed re-spawn leaves the card
        # untouched for a clean re-dispatch once the live delegate finishes.
        if is_dev and _developer_delegate_in_flight(
            slug, issue, branch, str(cid),
            developer_profile=(card.get("assignee") or "").strip(),
            workdir=repo_path,
            marker_ttl_secs=(pr_grace_secs + _MARKER_TTL_BUFFER_SECS),
        ):
            logger.info(
                "direct-dispatch: developer delegate already in flight for #%s — "
                "skipping second dispatch of card %s (#1375)",
                issue, cid,
            )
            continue

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
            spawn(card=cid, board=slug, cmd=cmd, role=role, taskf=taskf, outf=outf,
                  repo=repo_path, branch=branch, base=base_branch,
                  pr_grace_secs=(pr_grace_secs if is_dev else 0))
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("direct-dispatch: spawn failed for %s (%s): %s", role, cid, exc)
            continue
        # Record the persistent single-flight marker (#1404) AFTER a successful
        # developer spawn so a later tick suppresses a second dispatch for this
        # issue even once this card goes terminal (done-without-PR). Best-effort:
        # a state-write failure must never abort the dispatch it just performed.
        if is_dev and issue and repo_path:
            try:
                dispatch_state.mark_developer_dispatch(repo_path, issue)
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("direct-dispatch: marker write failed for #%s: %s", issue, exc)
        logger.info(
            "direct-dispatch: spawned %s wrapper for #%s (card %s) — no local-model hop (#1329)",
            role, issue, cid,
        )
        spawned += 1

    return spawned
