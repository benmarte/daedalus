"""Concurrent-duplicate detection (#1413).

Two dedup gates for same-root-cause issues that are processed **in parallel** —
a scenario the validator's sequential open-PR/issue check structurally cannot
catch, because at validation time for issue *X* a concurrently-processing
sibling *Y* has no PR yet, so there is nothing to match against.

Layer 1 — dispatch-time overlap gate
    Before spawning a validator for issue *X*, :func:`find_inflight_duplicate`
    checks file-reference / keyword overlap (via :mod:`core.file_overlap`)
    against other **in-flight** issues.  On a high-confidence hit the dispatcher
    holds *X* until the sibling resolves; the validator then re-checks and can
    legitimately emit ``DUPLICATE``.

Layer 2 — pre-merge PR file-overlap gate
    Once a PR is open its changed-file set is finally known.
    :func:`find_pr_file_overlap` checks it against other open PRs
    (suffix-aware path match).  On overlap the dispatcher flags the *newer* PR
    as a suspected duplicate for human review — the reliable backstop the
    validator cannot make at validation time.

This module is pure and import-light — it depends only on
:mod:`core.file_overlap`, matching the convention of :mod:`core.util`.  No
network, no subprocess, so it is trivially unit-testable and usable from both
the dispatcher and the dashboard.
"""
from __future__ import annotations

from typing import Iterable

from core.file_overlap import _paths_overlap, detect_file_overlap

# ─────────────────────────────────────────────────────────────────────────────
# Defaults (overridable via pipeline.concurrent_dedup in daedalus.yaml)
# ─────────────────────────────────────────────────────────────────────────────

# Minimum overlap confidence for the Layer-1 dispatch-time gate. 0.8 is the
# file-reference match threshold in core.file_overlap — an explicit shared file
# (or a full keyword+file match at 1.0) trips the hold; a bare keyword-only
# overlap (below FILE_REF_THRESHOLD) does not, keeping the gate conservative.
DEFAULT_MIN_CONFIDENCE: float = 0.8

# Minimum number of shared changed files for the Layer-2 pre-merge gate.
DEFAULT_MIN_SHARED_FILES: int = 1


# ─────────────────────────────────────────────────────────────────────────────
# Layer 1 — dispatch-time overlap gate
# ─────────────────────────────────────────────────────────────────────────────

def gather_inflight_siblings(
    candidate_number: "int | None",
    managed: Iterable[int],
    created: Iterable[int],
    issues_map: dict,
) -> list[dict]:
    """Return the issue dicts in-flight alongside *candidate_number*.

    In-flight = an issue that already has a kanban card (*managed*) OR one
    created earlier this same tick (*created*), that is still present in
    *issues_map* (i.e. still an open, fetched issue), excluding the candidate
    itself.  Ordered by issue number so the caller's overlap scan is
    deterministic.  A number absent from *issues_map* (e.g. a sibling whose
    issue was closed when its PR merged) is dropped, which is exactly how the
    hold releases once the sibling resolves.
    """
    nums = set(managed) | set(created)
    return [
        issues_map[m]
        for m in sorted(nums)
        if m != candidate_number and m in issues_map
    ]


def find_inflight_duplicate(
    candidate: dict,
    siblings: Iterable[dict],
    *,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> "tuple[int, dict] | None":
    """Return the in-flight sibling most likely to duplicate *candidate*.

    Parameters
    ----------
    candidate
        The issue about to be dispatched — a dict with at least ``number`` and
        optional ``title`` / ``body`` keys.
    siblings
        Other **in-flight** issues (same dict shape). The caller supplies only
        genuinely-concurrent issues (managed or created this tick, still open).
    min_confidence
        Minimum :func:`core.file_overlap.detect_file_overlap` confidence for a
        hit.  Defaults to :data:`DEFAULT_MIN_CONFIDENCE`.

    Returns
    -------
    ``(sibling_number, overlap_result)`` for the highest-confidence sibling
    whose overlap with *candidate* is ``>= min_confidence``, or ``None`` when no
    sibling clears the bar.  ``overlap_result`` is the raw
    :func:`detect_file_overlap` dict (``overlaps`` / ``confidence`` /
    ``matched_files`` / ``matched_keywords``).

    Ties (equal confidence) break to the **lowest** sibling number so the result
    is deterministic and the older sibling wins — the newer candidate is the one
    held back.
    """
    cand_no = candidate.get("number") if isinstance(candidate, dict) else None
    best_key: "tuple[float, int] | None" = None
    best: "tuple[int, dict] | None" = None
    for sib in siblings:
        if not isinstance(sib, dict):
            continue
        sib_no = sib.get("number")
        if sib_no is None or sib_no == cand_no:
            continue
        result = detect_file_overlap(candidate, sib)
        if not result.get("overlaps"):
            continue
        conf = float(result.get("confidence", 0.0))
        if conf < min_confidence:
            continue
        # Max on (confidence, -number) → highest confidence, then lowest number.
        key = (conf, -int(sib_no))
        if best_key is None or key > best_key:
            best_key = key
            best = (int(sib_no), result)
    return best


# ─────────────────────────────────────────────────────────────────────────────
# Layer 2 — pre-merge PR file-overlap gate
# ─────────────────────────────────────────────────────────────────────────────

def shared_changed_files(
    files_a: Iterable[str],
    files_b: Iterable[str],
) -> list[str]:
    """Return files from *files_a* that overlap (suffix-aware) with *files_b*.

    Uses the same component-wise suffix match as the planner's file-overlap
    detection, so ``src/foo/bar.py`` matches a bare ``foo/bar.py``. The returned
    list preserves *files_a* order and is de-duplicated.
    """
    b_list = [f for f in files_b if f]
    shared: list[str] = []
    for fa in files_a:
        if not fa or fa in shared:
            continue
        if any(_paths_overlap(fa, fb) for fb in b_list):
            shared.append(fa)
    return shared


def find_pr_file_overlap(
    candidate_files: Iterable[str],
    others: Iterable["tuple[int, Iterable[str]]"],
    *,
    min_shared_files: int = DEFAULT_MIN_SHARED_FILES,
) -> "tuple[int, list[str]] | None":
    """Return an existing open PR that duplicates *candidate_files*.

    Parameters
    ----------
    candidate_files
        Changed-file paths of the PR under inspection (the newer one).
    others
        Iterable of ``(pr_number, files)`` for the OTHER open PRs to compare
        against.
    min_shared_files
        Minimum count of suffix-aware shared files for a hit.  Defaults to
        :data:`DEFAULT_MIN_SHARED_FILES`.

    Returns
    -------
    ``(pr_number, shared_files)`` for the best-matching existing PR, or ``None``.
    The PR sharing the **most** files wins; ties break to the **lowest**
    pr_number (the older PR), so the caller flags the newer PR as the duplicate.
    """
    cand = [f for f in candidate_files if f]
    if not cand or min_shared_files < 1:
        return None
    best_key: "tuple[int, int] | None" = None
    best: "tuple[int, list[str]] | None" = None
    for pr_no, files in others:
        if pr_no is None or not files:
            continue
        shared = shared_changed_files(cand, files)
        if len(shared) < min_shared_files:
            continue
        key = (len(shared), -int(pr_no))
        if best_key is None or key > best_key:
            best_key = key
            best = (int(pr_no), shared)
    return best
