"""Cross-provider failover chains for the crash-retry reconciler (issue #1207).

Daedalus consumes AI providers at two independent layers:

* ``coding_agent`` — the external CLI that does the actual coding work
  (``execution.coding_agent`` / ``execution.coding_agent_cmd``), and
* ``brain`` — the Hermes profile model that drives each pipeline role
  (``model.default`` / ``model.provider``).

Either layer can be configured as an ordered chain (primary first, then
fallbacks). When the active provider fails with a *transient* trigger —
session/usage limit, quota, crash, timeout, API connection error — the
crash-retry reconciler (#1205) consults this module to pick the NEXT provider
in the chain instead of endlessly retrying the one that is down. Back-compat:
the existing single-value keys keep working and are treated as a one-element
chain, in which case failover is a structural no-op and #1205 semantics are
unchanged.

This module is deliberately pure (no kanban / dispatcher imports): chain and
knob resolution, trigger→layer mapping, and the provider-selection decision.
Cooldown persistence lives in ``core.dispatch_state``; application of a
switch (card-body delegation rewrite, profile resync) is supplied by the
dispatcher as callbacks.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger("daedalus.provider_failover")

# Coding-agent names accepted in an execution.coding_agents chain entry.
# "none" is excluded: a chain exists to hand work to a DIFFERENT agent, and
# "no delegation" is not a provider you can fail over to.
VALID_CODING_AGENTS = ("hermes", "claude-code", "codex", "opencode")

# Trigger classes produced by crash_retry.classify(). ``failover.triggers``
# entries are validated against this set.
TRIGGER_CLASSES = (
    "session_limit",
    "quota_exceeded",
    "crash",
    "timeout",
    "api_connection_error",
)

LAYER_CODING_AGENT = "coding_agent"
LAYER_BRAIN = "brain"

_FAILOVER_DEFAULTS: Dict[str, Any] = {
    "max_attempts_per_provider": 2,
    "cooldown_minutes": 30,
    "reset_to_primary": True,
    "triggers": list(TRIGGER_CLASSES),
}

# Evidence substrings that attribute a crash to the spawned CODING AGENT
# (the external CLI died / hit its limit). Anything else — notably
# APIConnectionError — is attributed to the orchestration BRAIN, because the
# worker whose process the breaker watches IS the brain session.
_CODING_AGENT_EVIDENCE = (
    "coding-agent-failed:",
    "coding_agent_died",
    "coding_agent_timeout",
    "pid not alive",
    "session limit",
    "usage limit",
    "rate limit",
    "quota",
)


def entry_name(entry: Dict[str, Any]) -> str:
    """Canonical provider name of a chain entry (either layer's shape)."""
    return str(entry.get("name") or entry.get("provider") or "").strip()


def provider_key(layer: str, name: str) -> str:
    """State-file key for a provider's global cooldown (``<layer>:<name>``)."""
    return f"{layer}:{name}"


def layer_for_evidence(evidence: str) -> str:
    """Attribute crash *evidence* to the coding-agent or brain layer."""
    s = (evidence or "").lower()
    if any(m in s for m in _CODING_AGENT_EVIDENCE):
        return LAYER_CODING_AGENT
    return LAYER_BRAIN


def resolve_failover_config(
    execution: Optional[Dict[str, Any]],
    model_cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Resolve the ``failover:`` knobs over built-in defaults.

    The block is accepted under both ``execution.failover`` and
    ``model.failover`` (the issue shows it under ``model:``); when both are
    present, ``execution.failover`` wins per-key. Invalid values fall back to
    the default. Returns a copy callers can mutate freely.
    """
    out = dict(_FAILOVER_DEFAULTS)
    out["triggers"] = list(_FAILOVER_DEFAULTS["triggers"])
    # model first, execution second → execution overrides on conflict.
    for raw in (
        (model_cfg or {}).get("failover"),
        (execution or {}).get("failover"),
    ):
        if not isinstance(raw, dict):
            continue
        for key in ("max_attempts_per_provider", "cooldown_minutes"):
            if key not in raw:
                continue
            try:
                iv = int(raw[key])
                if iv > 0:
                    out[key] = iv
            except (TypeError, ValueError):
                continue
        if "reset_to_primary" in raw:
            out["reset_to_primary"] = bool(raw["reset_to_primary"])
        trig = raw.get("triggers")
        if isinstance(trig, (list, tuple)):
            vals = [
                str(t).strip().lower()
                for t in trig
                if str(t).strip().lower() in TRIGGER_CLASSES
            ]
            if vals:
                out["triggers"] = vals
    return out


def resolve_coding_agent_chain(
    execution: Optional[Dict[str, Any]],
    defaults: Optional[Dict[str, str]] = None,
) -> List[Dict[str, str]]:
    """Ordered coding-agent chain: ``[{name, cmd}, …]`` (primary first).

    Reads ``execution.coding_agents``; entries with an unknown ``name`` are
    dropped with a warning, duplicates keep the first occurrence, and a
    missing ``cmd`` falls back to *defaults* (the dispatcher passes its
    ``_CODING_AGENT_DEFAULTS``). When the list is absent or yields nothing
    valid, the legacy single-value keys (``coding_agent`` /
    ``coding_agent_cmd``) are synthesized into a one-element chain — zero
    config change required (#1207 back-compat).
    """
    dmap = defaults or {}
    raw = (execution or {}).get("coding_agents")
    chain: List[Dict[str, str]] = []
    if isinstance(raw, (list, tuple)):
        for item in raw:
            if not isinstance(item, dict):
                logger.warning(
                    "provider-failover: coding_agents entry %r is not a mapping "
                    "— skipped",
                    item,
                )
                continue
            name = str(item.get("name") or "").strip().lower()
            if name not in VALID_CODING_AGENTS:
                logger.warning(
                    "provider-failover: invalid coding_agents name %r "
                    "(expected one of %s) — skipped",
                    item.get("name"),
                    ", ".join(VALID_CODING_AGENTS),
                )
                continue
            if any(e["name"] == name for e in chain):
                logger.warning(
                    "provider-failover: duplicate coding_agents entry %r — "
                    "keeping the first occurrence",
                    name,
                )
                continue
            cmd = item.get("cmd")
            cmd = cmd.strip() if isinstance(cmd, str) else ""
            chain.append({"name": name, "cmd": cmd or dmap.get(name, "")})
    if chain:
        return chain
    # Legacy one-element chain (same validation as _resolve_coding_agent).
    agent = (execution or {}).get("coding_agent")
    agent = agent.strip().lower() if isinstance(agent, str) and agent else "hermes"
    if agent not in VALID_CODING_AGENTS + ("none",):
        agent = "hermes"
    cmd = (execution or {}).get("coding_agent_cmd")
    cmd = cmd.strip() if isinstance(cmd, str) else ""
    return [{"name": agent, "cmd": cmd or dmap.get(agent, "")}]


def resolve_model_provider_chain(
    model_cfg: Optional[Dict[str, Any]],
    active: Optional[Dict[str, Optional[str]]] = None,
) -> List[Dict[str, str]]:
    """Ordered brain chain: ``[{provider, default}, …]`` (primary first).

    Reads ``model.providers`` from the resolved per-repo config; entries with
    no ``provider`` are dropped, duplicates keep the first occurrence. When
    the list is absent/empty, falls back to a one-element chain from *active*
    (the dispatcher passes ``_resolve_active_model_provider()`` — keys
    ``model``/``provider``). Returns ``[]`` when nothing is configured, in
    which case brain failover is disabled.
    """
    raw = (model_cfg or {}).get("providers")
    chain: List[Dict[str, str]] = []
    if isinstance(raw, (list, tuple)):
        for item in raw:
            if not isinstance(item, dict):
                logger.warning(
                    "provider-failover: model.providers entry %r is not a "
                    "mapping — skipped",
                    item,
                )
                continue
            provider = str(item.get("provider") or "").strip()
            if not provider:
                logger.warning(
                    "provider-failover: model.providers entry missing "
                    "'provider' — skipped: %r",
                    item,
                )
                continue
            if any(e["provider"] == provider for e in chain):
                logger.warning(
                    "provider-failover: duplicate model.providers entry %r — "
                    "keeping the first occurrence",
                    provider,
                )
                continue
            chain.append(
                {
                    "provider": provider,
                    "default": str(item.get("default") or "").strip(),
                }
            )
    if chain:
        return chain
    act = active or {}
    if act.get("provider") or act.get("model"):
        return [
            {
                "provider": str(act.get("provider") or ""),
                "default": str(act.get("model") or ""),
            }
        ]
    return []


def select_provider(
    chain: List[Dict[str, Any]],
    attempts: Dict[str, int],
    cooling: Set[str],
    cfg: Dict[str, Any],
    *,
    current_index: int = 0,
) -> Dict[str, Any]:
    """Decide which chain entry the next re-dispatch should use.

    *attempts* is the per-provider count already spent THIS episode (a
    dispatch on a provider = one attempt); *cooling* is the set of provider
    names currently inside their global cooldown window.

    Returns one of::

        {"action": "use", "index": i, "entry": chain[i]}
        {"action": "wait"}        # candidates remain but all are cooling —
                                  # stay blocked, re-evaluate next tick
        {"action": "exhausted"}   # every entry spent max_attempts_per_provider
                                  # → escalate

    ``reset_to_primary`` (default) prefers the lowest eligible index — the
    primary is chosen again the moment it recovers. Otherwise the current
    provider is kept while it remains eligible, advancing in chain order
    (with wrap) only when forced.
    """
    cap = int(cfg["max_attempts_per_provider"])
    names = [entry_name(e) for e in chain]
    open_idxs = [i for i, n in enumerate(names) if int(attempts.get(n, 0)) < cap]
    if not open_idxs:
        return {"action": "exhausted"}
    ready = [i for i in open_idxs if names[i] not in cooling]
    if not ready:
        return {"action": "wait"}
    if cfg.get("reset_to_primary", True):
        idx = ready[0]
    else:
        at_or_after = [i for i in ready if i >= current_index]
        idx = at_or_after[0] if at_or_after else ready[0]
    return {"action": "use", "index": idx, "entry": chain[idx]}


def validate_failover(resolved: Dict[str, Any]) -> List[str]:
    """Validate the failover-related config of a resolved per-repo config.

    Returns a list of human-readable errors (empty when valid). Mirrors
    ``config.validate_vcs``: absence of every key is valid (single-provider
    back-compat), and only *structurally* broken values error — unknown names
    are already warn-and-skip at resolution time, but a non-list
    ``coding_agents``/``providers`` or a wholly invalid entry is a config
    mistake worth surfacing.
    """
    errors: List[str] = []
    execution = resolved.get("execution") or {}
    model_cfg = resolved.get("model") or {}

    ca = execution.get("coding_agents")
    if ca is not None:
        if not isinstance(ca, (list, tuple)):
            errors.append("execution.coding_agents must be a list of {name, cmd}")
        else:
            for item in ca:
                name = item.get("name") if isinstance(item, dict) else None
                if (
                    not isinstance(item, dict)
                    or str(name or "").strip().lower() not in VALID_CODING_AGENTS
                ):
                    errors.append(
                        f"execution.coding_agents entry {item!r} is invalid "
                        f"(name must be one of: {', '.join(VALID_CODING_AGENTS)})"
                    )

    mp = model_cfg.get("providers")
    if mp is not None:
        if not isinstance(mp, (list, tuple)):
            errors.append("model.providers must be a list of {provider, default}")
        else:
            for item in mp:
                if not isinstance(item, dict) or not str(
                    (item.get("provider") if isinstance(item, dict) else "") or ""
                ).strip():
                    errors.append(
                        f"model.providers entry {item!r} is invalid "
                        "(needs a non-empty 'provider')"
                    )

    for section, block in (
        ("execution.failover", execution.get("failover")),
        ("model.failover", model_cfg.get("failover")),
    ):
        if block is None:
            continue
        if not isinstance(block, dict):
            errors.append(f"{section} must be a mapping")
            continue
        trig = block.get("triggers")
        if trig is not None:
            if not isinstance(trig, (list, tuple)):
                errors.append(f"{section}.triggers must be a list")
            else:
                unknown = [
                    str(t)
                    for t in trig
                    if str(t).strip().lower() not in TRIGGER_CLASSES
                ]
                if unknown:
                    errors.append(
                        f"{section}.triggers contains unknown trigger(s) "
                        f"{unknown} (valid: {', '.join(TRIGGER_CLASSES)})"
                    )
    return errors
