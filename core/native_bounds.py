"""Native per-task retry / runtime bounds for dispatcher-created cards (#1289).

Hermes-core kanban tasks accept two mechanical circuit breakers:

* ``--max-retries N`` — the per-task *consecutive-failure* breaker. After ``N``
  consecutive worker failures the core parks the card. Defaults to
  ``kanban.failure_limit`` (2). This is a DIFFERENT mechanism from the daedalus
  CI/review fix-loop (``MAX_FIX_ATTEMPTS = 3`` in ``core/iterate.py``): the
  fix-loop retries a *completed* card whose artifact failed review; the breaker
  retries a card whose *worker* died. Neither code path conflates the two.
* ``--max-runtime <dur>`` (e.g. ``30m``) — a wall-clock cap after which the
  Hermes dispatcher SIGTERM→SIGKILL→**requeues** the card, self-bounding a
  runaway worker.

This module resolves an off-by-default ``execution.native_bounds`` policy into a
per-role ``{max_retries, max_runtime}`` map. When the flag is disabled the
resolver still returns a fully-populated dict but ``enabled`` is ``False`` and
``bounds_kwargs`` returns an empty mapping, so dispatcher call sites emit
BYTE-IDENTICAL CLI args to the pre-#1289 behaviour (no ``--max-retries`` /
``--max-runtime``, no changed control flow).

Goal-mode (``--goal``, judge-LLM adjudication) is deliberately OUT of scope here
and tracked in the #1289 follow-up: it adds per-card LLM cost/latency and stalls
cards when the judge brain is unavailable, so it is not bundled with these
deterministic, LLM-free bounds.

Config shape (flat keys under ``execution:``, mirroring the ``crash_retry_*``
pattern)::

    execution:
      native_bounds: false            # master switch (default off)
      native_max_retries: 2           # global default breaker
      native_max_runtime: "30m"       # global default wall-clock cap
      native_bounds_by_role:          # optional per-role overrides
        developer: {max_runtime: "1h"}

All values are validated; missing / malformed values fall back to the built-in
defaults. Never raises.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("daedalus.native_bounds")

# Default per-task failure breaker. Explicitly 2 to match ``kanban.failure_limit``
# and stay DISTINCT from ``MAX_FIX_ATTEMPTS = 3`` (the CI/review fix-loop).
DEFAULT_MAX_RETRIES = 2

# Default wall-clock cap for short-lived roles (validator, QA, reviewer, …).
DEFAULT_MAX_RUNTIME = "30m"

# Per-role runtime overrides baked in as sensible defaults. Long-running roles
# (the developer actually edits code + runs tests) get a generous cap; every
# other role uses ``DEFAULT_MAX_RUNTIME``.
_ROLE_RUNTIME_DEFAULTS: Dict[str, str] = {
    "developer": "1h",
}

# Every role a dispatcher call site may tag. ``pm`` / ``planner`` are included so
# their cards inherit the defaults even though they are not UI-configured.
_ROLES = (
    "validator",
    "pm",
    "planner",
    "developer",
    "qa",
    "reviewer",
    "security",
    "documentation",
)


def _valid_runtime(value: Any) -> Optional[str]:
    """Return a non-empty runtime string, or None when *value* is unusable."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _valid_retries(value: Any) -> Optional[int]:
    """Return a positive int, or None when *value* is unusable."""
    if isinstance(value, bool):  # bools are ints — reject explicitly
        return None
    try:
        iv = int(value)
    except (TypeError, ValueError):
        return None
    return iv if iv > 0 else None


def resolve_bounds(execution: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve the native-bounds policy from ``execution:`` over built-in defaults.

    Returns a dict with:

    * ``enabled`` — ``bool`` master switch (default ``False``).
    * ``default_max_retries`` / ``default_max_runtime`` — the resolved globals.
    * ``by_role`` — ``{role: {"max_retries": int, "max_runtime": str}}`` for
      every known role, with per-role overrides applied.

    Missing, non-numeric, non-positive, or empty values fall back to defaults.
    Never raises.
    """
    raw = execution if isinstance(execution, dict) else {}
    enabled = bool(raw.get("native_bounds", False))

    base_retries = _valid_retries(raw.get("native_max_retries")) or DEFAULT_MAX_RETRIES
    base_runtime = _valid_runtime(raw.get("native_max_runtime")) or DEFAULT_MAX_RUNTIME

    by_role_raw = raw.get("native_bounds_by_role")
    if not isinstance(by_role_raw, dict):
        by_role_raw = {}

    by_role: Dict[str, Dict[str, Any]] = {}
    for role in _ROLES:
        retries = base_retries
        runtime = _ROLE_RUNTIME_DEFAULTS.get(role, base_runtime)
        override = by_role_raw.get(role)
        if isinstance(override, dict):
            retries = _valid_retries(override.get("max_retries")) or retries
            runtime = _valid_runtime(override.get("max_runtime")) or runtime
        by_role[role] = {"max_retries": retries, "max_runtime": runtime}

    return {
        "enabled": enabled,
        "default_max_retries": base_retries,
        "default_max_runtime": base_runtime,
        "by_role": by_role,
    }


def bounds_kwargs(bounds: Optional[Dict[str, Any]], role: str) -> Dict[str, Any]:
    """CLI-arg kwargs (``max_retries`` / ``max_runtime``) for *role*.

    Returns an EMPTY dict when *bounds* is falsy or disabled — so a call site
    that splats ``**bounds_kwargs(...)`` emits byte-identical args to the
    pre-#1289 behaviour when the flag is off. Never raises.
    """
    if not bounds or not bounds.get("enabled"):
        return {}
    rb = (bounds.get("by_role") or {}).get(role)
    if not isinstance(rb, dict):
        return {
            "max_retries": bounds.get("default_max_retries", DEFAULT_MAX_RETRIES),
            "max_runtime": bounds.get("default_max_runtime", DEFAULT_MAX_RUNTIME),
        }
    return {"max_retries": rb["max_retries"], "max_runtime": rb["max_runtime"]}
