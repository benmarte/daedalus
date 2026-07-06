"""Tier promotion logic for dependency-based sub-issue re-evaluation.

Spec: docs/specs/tier-promotion-design.md

When an epic is decomposed into sub-issues with ``Depends on:`` dependencies,
only tier-0 (dependency-free) sub-issues are labelled Ready immediately. As
each sub-issue's PR merges, the dispatcher calls into this module to
re-evaluate the epic's siblings and label the next eligible tier whose
dependencies are all closed.

Public API
----------
- ``promote_waiting_tiers(provider, just_closed) -> PromotionResult``
    Main entry point used by the dispatcher.
- ``DependencySnapshot`` — immutable view of an epic's dependency DAG built
    from provider state. ``compute_tiers()`` returns the tier map;
    ``promotable(already_ready)`` returns open issues with no open blockers.
- ``compute_tiers(dep_map)`` — pure graph function computing the longest-path
    tier for each DAG node with cycle detection.
- ``detect_cycles(dep_map)`` — returns the list of cycles (each as a list of
    issue numbers) in the dependency graph via DFS.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from core.providers.base import VCSProvider, parse_depends_on

logger = logging.getLogger("daedalus.tier_promotion")


# Matches the parent-epic convention emitted by Phase-3 decomposer
# (``Part of epic #N`` / ``Part of: #N``) and the hand-written variant
# (``Epic: #N`` / ``Epic #N``). Captures the epic number.
EPIC_REF_RE = re.compile(
    r"(?im)^(?:part[\s-]+of(?:[\s-]+epic)?|epic)\s*:?\s*#(\d+)"
)


@dataclass
class PromotionResult:
    """Outcome of a single tier-promotion pass."""

    promoted: list[int] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    cycles: list[list[int]] = field(default_factory=list)
    epics_checked: set[int] = field(default_factory=set)


class DependencySnapshot:
    """Immutable-ish view of an epic's dependency DAG for one dispatch tick.

    ``provider`` is used lazily to derive the dep graph from
    ``provider.sub_issues_of`` / ``provider.blockers`` / ``provider.get_issue``
    and ``provider.get_issue_state``. ``just_closed`` is informational (the
    caller uses it to scope which epics to re-evaluate); the promotable
    computation looks at *current* open/closed state.
    """

    def __init__(
        self,
        *,
        epic_number: int,
        provider: VCSProvider,
        just_closed: frozenset = frozenset(),
    ) -> None:
        self.epic_number = epic_number
        self.provider = provider
        self.just_closed = frozenset(just_closed)
        # Lazily derived.
        self._sub_issues: list[int] | None = None
        self._computed_dep_map: dict[int, list[int]] | None = None

    # ── lazy providers ───────────────────────────────────────────────────────
    def _ensure_sub_issues(self) -> list[int]:
        if self._sub_issues is None:
            try:
                self._sub_issues = list(self.provider.sub_issues_of(self.epic_number) or [])
            except Exception as e:  # pragma: no cover — defensive
                logger.warning("sub_issues_of(%d) failed: %s", self.epic_number, e)
                self._sub_issues = []
        return self._sub_issues

    @property
    def sub_issues(self) -> list[int]:
        """Public access to the list of sub-issues discovered for this epic."""
        return self._ensure_sub_issues()

    def _dep_map(self) -> dict[int, list[int]]:
        """Build the *structural* dependency graph for the epic's sub-issues.

        Uses ``parse_depends_on(body)`` (not ``provider.blockers``) so the
        graph includes every declared dep — open or closed. Tiers describe
        the shape of the DAG, not its current blocked state. External deps
        (referenced numbers that are not siblings of this epic) are dropped
        so they don't perturb tier levels.
        """
        if self._computed_dep_map is not None:
            return self._computed_dep_map
        sub_issues = self._ensure_sub_issues()
        siblings = set(sub_issues)
        out: dict[int, list[int]] = {}
        for n in sub_issues:
            try:
                issue = self.provider.get_issue(n)
            except Exception as e:
                logger.warning("get_issue(%d) failed: %s", n, e)
                out[n] = []
                continue
            body = getattr(issue, "body", "") or ""
            all_refs = list(parse_depends_on(body))
            # Keep only internal (sibling) deps — external ones are
            # non-blocking from a tier perspective (they're handled by the
            # pull-based gate).
            out[n] = [d for d in all_refs if d in siblings]
        self._computed_dep_map = out
        return self._computed_dep_map

    # ── public surface used by tests & promote_waiting_tiers ─────────────────
    def compute_tiers(self) -> dict[int, int]:
        """Tier for every node in the DAG (cycle nodes excluded)."""
        return compute_tiers(self._dep_map())

    def promotable(self, *, already_ready: set[int] | None = None) -> list[int]:
        """Open sub-issues whose providers.blockers() is now empty.

        Excludes:
        • just-closed issue numbers (they just finished — not promotable)
        • already-Ready issue numbers (idempotency fast-path)
        • open issues with no open blockers that are tier-0 — tier 0 enters
          dispatch immediately during epic decomposition and does not need
          promotion. Only tier ≥ 1 issues are *promoted*.
        """
        already = set(already_ready or ())
        tiers = self.compute_tiers()
        out: list[int] = []
        for n in self._ensure_sub_issues():
            if n in self.just_closed or n in already:
                continue
            state = self.provider.get_issue_state(n)
            if state != "open":
                continue
            # Tier 0 is dispatched at creation — skip.
            if tiers.get(n, -1) == 0:
                continue
            try:
                open_deps = list(self.provider.blockers(n) or [])
            except Exception as e:  # pragma: no cover — defensive
                logger.warning("blockers(%d) failed: %s", n, e)
                continue
            if not open_deps:
                out.append(n)
        return out


# ---------------------------------------------------------------------------
# Pure-graph helpers
# ---------------------------------------------------------------------------
def detect_cycles(dep_map: dict[int, list[int]]) -> list[list[int]]:
    """Return cycles (each a list of nodes forming the loop) via DFS back-edges.

    Returns the empty list when the graph is acyclic. Cycles are *not*
    normalised — the same loop may appear multiple times if discovered from
    different entry points. Tests only check non-emptiness, so that's fine.
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[int, int] = {n: WHITE for n in dep_map}
    cycles: list[list[int]] = []

    def dfs(node: int, stack: list[int]) -> None:
        color[node] = GRAY
        stack.append(node)
        for dep in dep_map.get(node, []) or []:
            if dep not in dep_map:
                continue  # external dep
            if color[dep] == GRAY:
                idx = stack.index(dep)
                cycles.append(list(stack[idx:]))
            elif color[dep] == WHITE:
                dfs(dep, stack)
        stack.pop()
        color[node] = BLACK

    for n in dep_map:
        if color[n] == WHITE:
            dfs(n, [])
    return cycles


def compute_tiers(dep_map: dict[int, list[int]]) -> dict[int, int]:
    """Longest-path tier with cycle detection.

    Cycle participants are omitted from the result. External deps (numbers
    not present as keys in ``dep_map``) are treated as satisfied and do not
    contribute to the tier. A node whose every dep is external/cycle-trapped
    lands at tier 0.
    """
    if not dep_map:
        return {}

    cycles = detect_cycles(dep_map)
    cyclic_nodes: set[int] = set()
    for cycle in cycles:
        cyclic_nodes.update(cycle)

    tiers: dict[int, int] = {}
    computing: set[int] = set()

    def tier_of(node: int) -> int | None:
        if node in tiers:
            return tiers[node]
        if node in cyclic_nodes:
            return None
        if node in computing:  # pragma: no cover — cycle-guard
            return None
        if node not in dep_map:
            return None
        computing.add(node)
        deps = dep_map[node] or []
        max_dep_tier = -1
        for dep in deps:
            if dep not in dep_map:
                continue  # external
            dep_tier = tier_of(dep)
            if dep_tier is None:
                continue  # cycle-trapped
            if dep_tier > max_dep_tier:
                max_dep_tier = dep_tier
        tiers[node] = (max_dep_tier + 1) if max_dep_tier >= 0 else 0
        computing.discard(node)
        return tiers[node]

    for n in dep_map:
        if n not in cyclic_nodes:
            tier_of(n)
    return tiers


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def promote_waiting_tiers(
    provider: VCSProvider,
    just_closed: list[int],
) -> PromotionResult:
    """Run one tier-promotion pass over the epics owning the closed issues.

    For each just-closed issue, find its parent epic (via the body convention
    recognised by ``EPIC_REF_RE``) and re-examine that epic's siblings.
    Idempotent: an already-Ready issue is detected and skipped without making
    any provider label calls. Partial success: a per-issue provider error is
    recorded in ``result.errors`` but does not block the rest of the batch.
    """
    result = PromotionResult()
    if not just_closed:
        return result

    processed_epics: set[int] = set()

    for closed_issue in just_closed:
        try:
            issue = provider.get_issue(closed_issue)
        except Exception as e:
            logger.warning("get_issue(%d) failed: %s", closed_issue, e)
            continue
        if not issue:
            continue

        match = EPIC_REF_RE.search(issue.body or "")
        if not match:
            continue
        epic_number = int(match.group(1))

        if epic_number in processed_epics:
            continue
        processed_epics.add(epic_number)
        result.epics_checked.add(epic_number)

        snapshot = DependencySnapshot(
            epic_number=epic_number,
            provider=provider,
            just_closed=frozenset(just_closed),
        )

        # Detect cycles on the dep graph (built from provider.blockers).
        cycles = detect_cycles(snapshot._dep_map())
        if cycles:
            result.cycles.extend(cycles)
            logger.warning(
                "cyclic dependencies detected in epic #%d: %s",
                epic_number, cycles,
            )

        # Gather promotable issues, excluding already-Ready ones.
        already_ready: set[int] = set()
        for n in snapshot._ensure_sub_issues():
            try:
                if provider.has_label(n, "Ready"):
                    already_ready.add(n)
            except Exception:  # pragma: no cover — defensive
                pass

        promotable = snapshot.promotable(already_ready=already_ready)

        # Sequential-ordering guard: promote at most one tier per parent epic
        # per tick. If two sub-issue PRs merge between dispatcher ticks, a
        # still-open tier-1 sibling and a tier-2 issue (whose tier-1 dep just
        # closed) can both become unblocked at once. Promoting both would
        # violate the invariant that a tier only starts once every earlier
        # tier is closed. Keep only the lowest promotable tier; defer the rest
        # to the next tick (they re-surface once this tier's PRs merge).
        if promotable:
            tiers = snapshot.compute_tiers()
            min_tier = min(tiers.get(n, 0) for n in promotable)
            deferred = [n for n in promotable if tiers.get(n, 0) != min_tier]
            promotable = [n for n in promotable if tiers.get(n, 0) == min_tier]
            if deferred:
                logger.info(
                    "tier promotion: deferring %s in epic #%d to a later tick "
                    "(promoting tier %d first)",
                    deferred, epic_number, min_tier,
                )

        for n in promotable:
            try:
                ok = bool(provider.add_label(n, "Ready"))
            except Exception as e:
                logger.error("add_label(%d, Ready) failed: %s", n, e)
                result.errors.append({"issue": n, "error": str(e)})
                continue

            if not ok:
                logger.warning("add_label(%d, Ready) returned False", n)
                result.errors.append({"issue": n, "error": "add_label returned False"})
                continue

            # Also move the issue's board card to Ready so the normal dispatch
            # loop (which filters by board status, not label) picks it up on the
            # next tick. The label alone is not enough for board-mode dispatch.
            try:
                if provider.board_configured():
                    board_ok = bool(
                        provider.board_set_status(n, provider.status_name("ready"))
                    )
                    if not board_ok:
                        logger.warning(
                            "board_set_status(%d, Ready) returned False", n
                        )
            except Exception as e:  # pragma: no cover — defensive
                logger.warning("board_set_status(%d) failed: %s", n, e)

            result.promoted.append(n)
            logger.info("tier promotion: promoted #%d → Ready (epic #%d)", n, epic_number)

            try:
                provider.post_issue_comment(
                    n,
                    f"tier-promoted: all dependencies closed (epic #{epic_number})",
                )
            except Exception as e:  # pragma: no cover — non-promotional failure
                logger.warning("post_issue_comment(%d) failed: %s", n, e)

    return result


# ---------------------------------------------------------------------------
# Back-compat shim — some older call sites use this name.
# ---------------------------------------------------------------------------
def promote_next_tier(
    provider: VCSProvider,
    just_closed: list[int],
    epic_number: int | None = None,
) -> list[int]:
    """Legacy wrapper returning the promoted issue numbers only."""
    result = promote_waiting_tiers(provider, just_closed)
    return result.promoted
