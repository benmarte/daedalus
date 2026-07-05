"""Goal-mode configuration for dispatcher-created kanban cards (#1296).

When enabled via ``execution.goal_mode: true``, machine-verifiable pipeline
stages (developer, qa, documentation) are created with ``--goal`` +
``--goal-max-turns`` so Hermes adjudicates completion against a ground-truth
goal string rather than relying solely on the worker's self-report.

DELEGATION BYPASS
  When the effective coding-agent for a role is not ``'none'`` or ``'hermes'``
  (e.g. ``'claude-code'``), the outer Hermes worker delegates to an external
  agent and has approximately one turn.  Goal-mode's multi-turn adjudication
  evaluates the OUTER worker's turns, not the inner agent's — so it cannot
  verify the external agent's artifact.  Goal-mode is automatically skipped
  for any role whose effective coding-agent is a delegation target.

JUDGE-LLM FALLBACK
  If ``hermes kanban create --goal`` returns non-zero (judge brain unavailable
  or misconfigured), the card must still be created in normal mode so the
  pipeline never stalls.  Use :func:`create_with_goal_fallback` at call sites
  to get this behaviour automatically — it retries without goal kwargs on
  ``None`` from the first attempt.

Config shape (flat keys under ``execution:``, mirroring the ``native_bounds``
pattern)::

    execution:
      goal_mode: false          # master switch (default off)
      goal_max_turns: 30        # judge turn budget per eligible card

Flag-off is byte-identical to pre-#1296: no ``--goal`` / ``--goal-max-turns``
args are emitted.  Never raises.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("daedalus.goal_mode")

# Default judge turn budget when execution.goal_max_turns is missing or invalid.
DEFAULT_MAX_TURNS = 30

# Roles whose completion can be checked against a machine-verifiable fact.
# validator / pm / planner / reviewer / security / accessibility are NOT
# included — their outputs are qualitative or require human judgment.
_ELIGIBLE_ROLES = frozenset({"developer", "qa", "documentation"})

# Coding-agent values that mean "the Hermes profile IS the executing worker".
# Any other value (claude-code, codex, opencode, …) means the outer worker
# delegates to an external agent and has ~1 turn — goal-mode is skipped.
_NATIVE_AGENTS = frozenset({"none", "hermes"})


def resolve_goal_mode(execution: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve the goal-mode policy from the ``execution:`` config block.

    Parameters
    ----------
    execution:
        The ``execution`` sub-dict from the resolved per-repo config.  May be
        empty or non-dict — handled gracefully.

    Returns
    -------
    dict with:

    * ``enabled`` — ``bool`` master switch (default ``False``).
    * ``max_turns`` — ``int`` judge turn budget (default :data:`DEFAULT_MAX_TURNS`).

    Never raises.
    """
    raw = execution if isinstance(execution, dict) else {}
    enabled = bool(raw.get("goal_mode", False))

    max_turns = DEFAULT_MAX_TURNS
    raw_turns = raw.get("goal_max_turns")
    if isinstance(raw_turns, (int, float)) and not isinstance(raw_turns, bool):
        cand = int(raw_turns)
        if cand > 0:
            max_turns = cand

    return {"enabled": enabled, "max_turns": max_turns}


def goal_string(role: str, issue_number: int) -> str:
    """Return a verifiable goal description for *role* and *issue_number*.

    Phrased as ground-truth facts a judge LLM can check against the worker's
    outputs and tool calls.  Derived from each SOUL's canonical completion
    signals so the judge evaluates the same facts the dispatcher watches for.

    Returns an empty string for non-eligible roles (callers should not request
    goal_kwargs for those, but this is safe to call regardless).  Never raises.
    """
    n = issue_number
    if role == "developer":
        return (
            f"An open pull request for issue #{n} exists in the repository. "
            f"The developer's kanban card has been blocked with a reason starting "
            f"with 'review-required: PR', indicating the PR was opened and handed "
            f"off for review."
        )
    if role == "qa":
        return (
            f"The QA card for issue #{n} has been blocked with a verdict. "
            f"The block reason starts with 'qa-passed' (tests pass, PR approved) "
            f"or 'qa-failed' (defects found), confirming a QA decision was reached."
        )
    if role == "documentation":
        return (
            f"A documentation report comment has been posted to issue #{n}. "
            f"The docs card has been completed with a summary starting with "
            f"'docs posted: issue #{n}', confirming documentation was delivered."
        )
    return ""


def goal_kwargs(
    goal_cfg: Optional[Dict[str, Any]],
    role: str,
    issue_number: int,
    effective_coding_agent: str = "none",
) -> Dict[str, Any]:
    """Return ``--goal`` kwargs for ``create_task``, or an empty dict when ineligible.

    An empty dict is returned (and no ``--goal`` args are emitted) when:

    * *goal_cfg* is falsy or ``enabled`` is ``False`` — byte-identical to
      pre-#1296 behaviour.
    * *role* is not in ``_ELIGIBLE_ROLES`` (validator, pm, planner, reviewer,
      security, accessibility).
    * *effective_coding_agent* is not ``'none'`` or ``'hermes'`` — the
      delegation wrapper owns completion; goal-mode's adjudication would
      evaluate the outer wrapper's ~1 turn, not the inner agent's artifact.

    Never raises.
    """
    if not goal_cfg or not goal_cfg.get("enabled"):
        return {}
    if role not in _ELIGIBLE_ROLES:
        return {}
    if effective_coding_agent not in _NATIVE_AGENTS:
        logger.debug(
            "goal_mode: skipping %s #%s — effective agent '%s' is a delegation "
            "target; outer worker has ~1 turn and cannot produce a verifiable artifact",
            role,
            issue_number,
            effective_coding_agent,
        )
        return {}
    return {
        "goal": True,
        "goal_max_turns": goal_cfg.get("max_turns", DEFAULT_MAX_TURNS),
    }


def create_with_goal_fallback(
    create_fn: Callable,
    goal_cfg: Optional[Dict[str, Any]],
    role: str,
    issue_number: int,
    effective_coding_agent: str,
    *args: Any,
    **kwargs: Any,
) -> Optional[str]:
    """Call *create_fn* with goal kwargs merged; on ``None``, retry without goal.

    This implements the judge-LLM-unavailable fallback: if
    ``hermes kanban create --goal`` returns non-zero because the judge brain is
    unavailable or misconfigured, the card is still created in normal mode so
    the pipeline never stalls.

    Parameters
    ----------
    create_fn:
        The ``kanban.create_task`` (or compatible) callable.
    goal_cfg:
        Resolved goal-mode config from :func:`resolve_goal_mode`.
    role:
        Pipeline role name (e.g. ``'developer'``, ``'qa'``, ``'documentation'``).
    issue_number:
        The GitHub issue number, used to build the goal string.
    effective_coding_agent:
        The per-role resolved coding agent string (``ra.get(role, coding_agent)``).
    *args:
        Positional arguments forwarded to *create_fn* (typically ``slug`` and
        ``title``).
    **kwargs:
        Keyword arguments forwarded to *create_fn*.  Goal kwargs are merged on
        top; base kwargs are passed unchanged on fallback.

    Returns
    -------
    The task-ID string from *create_fn*, or ``None`` if both attempts fail.
    Never raises.
    """
    gkw = goal_kwargs(goal_cfg, role, issue_number, effective_coding_agent)
    try:
        result = create_fn(*args, **{**kwargs, **gkw})
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("goal_mode: create raised unexpectedly: %s", exc)
        result = None
    if result is None and gkw:
        logger.warning(
            "goal_mode: %s #%s create failed with goal kwargs — retrying without "
            "(judge LLM unavailable or misconfigured?)",
            role,
            issue_number,
        )
        try:
            result = create_fn(*args, **kwargs)
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("goal_mode: %s #%s fallback create also raised: %s", role, issue_number, exc)
            result = None
    return result
