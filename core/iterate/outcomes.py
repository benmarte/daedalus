"""core.iterate.outcomes — structured outcome record schema and parser.

Phase 1 of #1170 (dual-write, prefer-structured-read).

Agents append a fenced JSON block to their completion summary in addition to
the legacy prefix line.  This module extracts, validates, and returns a typed
``OutcomeRecord`` so ``classify_blocked`` can route by ``(role, verdict)``
instead of byte-level ``startswith`` matching.

Parser contract
---------------
- Extract the **last** fenced JSON block (``` ```json…``` ``` or ``` ```…``` ```)
  whose content contains ``"daedalus_outcome"``.  Fall back to the last bare
  JSON object in the summary if no fenced block is found.
- Validate ``daedalus_outcome == 1``, ``role`` in ``VERDICT_TABLE``,
  ``verdict`` in ``VERDICT_TABLE[role]``, and ``refs`` types.
- Return a frozen ``OutcomeRecord`` dataclass on success, or ``None`` on any
  failure.  **Never raises.**
- Malformed / absent JSON → ``None``; caller falls back to prefix routing.

Verdict table
-------------
Per-role verdict enums as specified in the #1170 proposal:

  validator   confirmed | already_fixed | duplicate | needs_more_info
              | security_threat | block_for_review
  qa          passed | failed
  reviewer    approved | changes_requested
  security    approved | changes_requested
  a11y        approved | na | skipped | changes_requested
  docs        posted
  planner     plan | not_suitable
  pm          spec | assigned | clarified | escalated
  developer   pr_opened | blocked
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("daedalus.iterate.outcomes")

# ── schema constants ──────────────────────────────────────────────────────────

SCHEMA_VERSION: int = 1

# Per-role verdict enums.  Kept as frozensets for O(1) membership checks.
VERDICT_TABLE: dict[str, frozenset[str]] = {
    "validator": frozenset({
        "confirmed", "already_fixed", "duplicate",
        "needs_more_info", "security_threat", "block_for_review",
    }),
    "qa":        frozenset({"passed", "failed"}),
    "reviewer":  frozenset({"approved", "changes_requested"}),
    "security":  frozenset({"approved", "changes_requested"}),
    "a11y":      frozenset({"approved", "na", "skipped", "changes_requested"}),
    "docs":      frozenset({"posted"}),
    "planner":   frozenset({"plan", "not_suitable"}),
    "pm":        frozenset({"spec", "assigned", "clarified", "escalated"}),
    "developer": frozenset({"pr_opened", "blocked"}),
}

# ── compiled regexes ──────────────────────────────────────────────────────────

# Fenced code block: ```json...``` or ```...```  (captures block interior)
_FENCED_RE: re.Pattern[str] = re.compile(
    r"```(?:json)?\s*(.*?)\s*```",
    re.DOTALL,
)


# ── public dataclass ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class OutcomeRecord:
    """Validated, immutable structured outcome record from an agent summary.

    Fields mirror the JSON shape defined in #1170 §2a:

        {"daedalus_outcome": 1,
         "role": "qa", "verdict": "passed",
         "refs": {"issue": 123, "pr": 456},
         "evidence": {"ci": "green", "tests": "3389 passed"},
         "note": "free text"}

    ``issue_ref`` and ``pr_ref`` are ``None`` when the agent omitted or
    null-ed the corresponding ``refs`` key.
    """

    schema_version: int
    role: str
    verdict: str
    issue_ref: int | None   # refs.issue
    pr_ref: int | None       # refs.pr
    evidence: dict[str, Any]
    note: str


# ── internal helpers ──────────────────────────────────────────────────────────


def _parse_raw(text: str) -> dict[str, Any] | None:
    """Attempt JSON parse; return dict or None (never raises)."""
    try:
        obj = json.loads(text.strip())
        return obj if isinstance(obj, dict) else None
    except (json.JSONDecodeError, ValueError):
        return None


def _find_json_candidates(text: str) -> list[str]:
    """Return all JSON object strings containing ``"daedalus_outcome"`` in *text*.

    Scans for every ``"daedalus_outcome"`` occurrence, then walks backwards to
    the enclosing ``{`` and forwards to the balanced ``}``, handling arbitrary
    nesting.  Returns strings in text-order; the caller takes the last one.
    """
    candidates: list[str] = []
    search_from = 0
    while True:
        idx = text.find('"daedalus_outcome"', search_from)
        if idx == -1:
            break
        # Find the outermost opening brace enclosing this key.
        brace_start = text.rfind("{", 0, idx)
        if brace_start == -1:
            search_from = idx + 1
            continue
        # Walk forward to find the matching closing brace.
        depth = 0
        end = -1
        for i in range(brace_start, len(text)):
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end != -1:
            candidates.append(text[brace_start : end + 1])
        search_from = idx + 1
    return candidates


def _validate(obj: dict[str, Any]) -> OutcomeRecord | None:
    """Validate a raw parsed dict against the outcome schema.

    Returns a frozen ``OutcomeRecord`` on success, ``None`` on any violation.
    """
    # ── schema version ────────────────────────────────────────────────────────
    sv = obj.get("daedalus_outcome")
    if sv != SCHEMA_VERSION:
        logger.debug("outcomes: schema_version mismatch: %r (expected %d)", sv, SCHEMA_VERSION)
        return None

    # ── role ──────────────────────────────────────────────────────────────────
    role = obj.get("role")
    if not isinstance(role, str) or role not in VERDICT_TABLE:
        logger.debug("outcomes: unknown role %r", role)
        return None

    # ── verdict ───────────────────────────────────────────────────────────────
    verdict = obj.get("verdict")
    if not isinstance(verdict, str) or verdict not in VERDICT_TABLE[role]:
        logger.debug("outcomes: invalid verdict %r for role %r", verdict, role)
        return None

    # ── refs (optional; each element int|None) ────────────────────────────────
    refs = obj.get("refs", {})
    if not isinstance(refs, dict):
        logger.debug("outcomes: refs must be a dict, got %r", type(refs))
        return None
    issue_raw = refs.get("issue")
    pr_raw = refs.get("pr")
    if issue_raw is not None and not isinstance(issue_raw, int):
        logger.debug("outcomes: refs.issue must be int|None, got %r", issue_raw)
        return None
    if pr_raw is not None and not isinstance(pr_raw, int):
        logger.debug("outcomes: refs.pr must be int|None, got %r", pr_raw)
        return None
    issue_ref: int | None = issue_raw
    pr_ref: int | None = pr_raw

    # ── evidence (optional dict) ──────────────────────────────────────────────
    evidence_raw = obj.get("evidence", {})
    evidence: dict[str, Any] = evidence_raw if isinstance(evidence_raw, dict) else {}

    # ── note (optional str) ───────────────────────────────────────────────────
    note_raw = obj.get("note", "")
    note: str = note_raw if isinstance(note_raw, str) else str(note_raw)

    return OutcomeRecord(
        schema_version=SCHEMA_VERSION,
        role=role,
        verdict=verdict,
        issue_ref=issue_ref,
        pr_ref=pr_ref,
        evidence=evidence,
        note=note,
    )


# ── public API ────────────────────────────────────────────────────────────────


def parse(summary: str) -> OutcomeRecord | None:
    """Extract and validate the outcome record from an agent summary string.

    Strategy (in order of preference):

    1. Find all fenced JSON blocks (``` ```json…``` ``` or ``` ```…``` ```) whose
       content contains ``"daedalus_outcome"``; take the *last* one.
    2. If no fenced block qualifies, scan the full text for bare JSON objects
       containing ``"daedalus_outcome"``; take the *last* one.

    Validates the extracted object against the schema.
    Returns ``None`` — without raising — on any failure.
    """
    if not summary:
        return None

    try:
        # ── step 1: fenced blocks ─────────────────────────────────────────────
        fenced_candidates: list[str] = []
        for m in _FENCED_RE.finditer(summary):
            block_content = m.group(1)
            if '"daedalus_outcome"' in block_content:
                fenced_candidates.extend(_find_json_candidates(block_content))

        if fenced_candidates:
            # Take the last fenced candidate.
            obj = _parse_raw(fenced_candidates[-1])
            if obj is not None:
                rec = _validate(obj)
                if rec is not None:
                    return rec
            # Fenced block found but failed validation — do NOT fall back to
            # bare scan; the agent clearly attempted to emit a record and the
            # caller should fall back to prefix matching.
            logger.debug(
                "outcomes: fenced block present but failed validation — "
                "returning None (caller falls back to prefix)"
            )
            return None

        # ── step 2: bare JSON objects anywhere in the text ───────────────────
        bare_candidates = _find_json_candidates(summary)
        for raw in reversed(bare_candidates):  # last candidate wins
            obj = _parse_raw(raw)
            if obj is not None:
                rec = _validate(obj)
                if rec is not None:
                    return rec

    except Exception as exc:  # noqa: BLE001 — must never raise from parse()
        logger.debug("outcomes: unexpected error parsing summary: %s", exc)

    return None
