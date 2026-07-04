"""core.iterate — CI-aware auto-advance routing and self-healing loop.

Package layout (issue #1154):
  classify   — action constants, _parse_handoff, classify_blocked  (PR 1/3)
  __init__   — package root: re-exports classify layer + all executors,
               decompose helpers, gate checkers, and run_iterate

For every blocked card on the board, classify its blocked state into an action,
then execute that action (complete, create fix-up tasks, unblock, escalate).
Runs as part of the daedalus dispatcher auto-advance block.

Pure helpers are unit-testable; the executors call ``core.kanban`` and the
configured VCS provider (``core.providers``) and are guarded so failures log
and continue.

``kanban`` is bound at package level (``from core import kanban``) so that
``mock.patch("core.iterate.kanban")`` and
``mock.patch("core.iterate.kanban.X")`` continue to work unchanged for
existing test suites.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core import kanban
from core.providers.base import CIStatus, issue_linked_to_pr as issue_linked_to_pr
from core.util import extract_issue_number

logger = logging.getLogger("daedalus.iterate")

# ── classify layer (extracted to core/iterate/classify.py, PR 1/3) ───────────
# Re-exported here so ``from core.iterate import X`` and
# ``mock.patch("core.iterate.X")`` / ``mock.patch("core.iterate.classify_blocked")``
# continue to resolve unchanged.
from core.iterate.classify import (  # noqa: E402
    ADVANCE,
    APPROVE_ADVANCE,
    ESCALATE,
    MAX_FIX_ATTEMPTS,
    PENDING_PR,
    PENDING_SIGNAL,
    PLANNER_DECOMPOSE,
    PM_ROUTE,
    QA_FIX,
    RECONCILE_MERGED,
    _parse_handoff as _parse_handoff,
    classify_blocked,
)

# Source-reading fallback counter for observability
_source_reading_fallback_count: int = 0


def get_source_reading_fallback_count() -> int:
    """Return the count of Phase 4 fallback events (for testing/monitoring)."""
    return _source_reading_fallback_count


def reset_source_reading_fallback_count() -> None:
    """Reset the source-reading fallback counter to zero (for tests)."""
    global _source_reading_fallback_count
    _source_reading_fallback_count = 0


# ── executors layer (extracted to core/iterate/executors.py, PR 2/3) ──────────
# Every symbol re-exported here so ``from core.iterate import X`` and
# ``mock.patch("core.iterate.X")`` continue to resolve unchanged for callers
# in __init__.py and for the test suite.
from core.iterate.executors import (  # noqa: E402
    _ACTION_EXECUTORS as _ACTION_EXECUTORS,
    _CHECKLIST_RE as _CHECKLIST_RE,
    _CODE_BLOCK_RE as _CODE_BLOCK_RE,
    _DECOMPOSE_LOCK_STALE_SECONDS as _DECOMPOSE_LOCK_STALE_SECONDS,
    _DECOMPOSE_MARKER_PREFIX as _DECOMPOSE_MARKER_PREFIX,
    _DECOMPOSED_MARKER_RE as _DECOMPOSED_MARKER_RE,
    _DOWNSTREAM_REVIEW_ROLES as _DOWNSTREAM_REVIEW_ROLES,
    _ESCALATION_STAMP_PREFIX as _ESCALATION_STAMP_PREFIX,
    _FILE_SYMBOL_CAP as _FILE_SYMBOL_CAP,
    _LEGACY_DECOMPOSED_MARKER_RE as _LEGACY_DECOMPOSED_MARKER_RE,
    _MAX_SUB_ISSUES as _MAX_SUB_ISSUES,
    _ROLE_ASSIGNEE_PREFIX as _ROLE_ASSIGNEE_PREFIX,
    _acquire_decompose_lock as _acquire_decompose_lock,
    _build_decomposed_marker as _build_decomposed_marker,
    _check_and_maybe_escalate as _check_and_maybe_escalate,
    _count_fix_attempts as _count_fix_attempts,
    _create_downstream_review_tasks as _create_downstream_review_tasks,
    _default_sub_issue_titles as _default_sub_issue_titles,
    _downstream_parents as _downstream_parents,
    _execute_advance as _execute_advance,
    _execute_approve_advance as _execute_approve_advance,
    _execute_escalate as _execute_escalate,
    _execute_legacy_dev_fix_review as _execute_legacy_dev_fix_review,
    _execute_pending_pr as _execute_pending_pr,
    _execute_planner_decompose as _execute_planner_decompose,
    _execute_planner_decompose_inner as _execute_planner_decompose_inner,
    _execute_pm_route as _execute_pm_route,
    _execute_qa_fix as _execute_qa_fix,
    _execute_reconcile_merged as _execute_reconcile_merged,
    _extract_issue_number_from_card as _extract_issue_number_from_card,
    _extract_sub_issues_from_body as _extract_sub_issues_from_body,
    _fix_attempts_path as _fix_attempts_path,
    _handoff_from_card as _handoff_from_card,
    _increment_fix_attempts as _increment_fix_attempts,
    _is_card_already_escalated as _is_card_already_escalated,
    _lock_file_path as _lock_file_path,
    _parse_pr_number as _parse_pr_number,
    _qa_passed_for_issue as _qa_passed_for_issue,
    _read_fix_attempts as _read_fix_attempts,
    _release_decompose_lock as _release_decompose_lock,
    _render_affected_files_section as _render_affected_files_section,
    _reviewer_passed_for_issue as _reviewer_passed_for_issue,
    _role_cards_for_issue as _role_cards_for_issue,
    _role_gate_passed as _role_gate_passed,
    _security_passed_for_issue as _security_passed_for_issue,
    _strip_code_blocks as _strip_code_blocks,
    _sub_issue_body as _sub_issue_body,
    _write_fix_attempts as _write_fix_attempts,
    has_decomposed_marker as has_decomposed_marker,
)


# ── Phase 4: source file reading & context injection ────────────────────────


def _extract_keywords(text: str, max_keywords: int = 10) -> list[str]:
    """Extract meaningful identifiers from *text*, skipping stop-words."""
    stop_words = {
        "the", "a", "an", "and", "or", "in", "on", "at", "to", "for", "of",
        "with", "by", "from", "is", "are", "was", "were", "be", "been",
        "being", "have", "has", "had", "do", "does", "did", "will", "would",
        "could", "should", "may", "might", "must", "shall", "can", "need",
        "dare", "ought", "used", "as", "if", "then", "than", "when", "where",
        "while", "how", "what", "which", "who", "whom", "this", "that",
        "these", "those", "i", "me", "my", "we", "our", "you", "your", "he",
        "him", "his", "she", "her", "it", "its", "they", "them", "their",
        "not", "no", "nor", "but", "about", "up", "down", "out", "off",
        "over", "under", "again", "further", "into", "through", "during",
        "before", "after", "above", "below", "any", "all", "each", "every",
        "both", "few", "more", "most", "other", "some", "such", "only",
        "own", "same", "so", "just", "new", "add", "update", "fix", "part",
    }
    words = re.findall(r"\b[a-zA-Z_][a-zA-Z0-9_]+\b", text)
    keywords: list[str] = []
    for w in words:
        lw = w.lower()
        if len(lw) > 3 and lw not in stop_words and lw not in keywords:
            keywords.append(lw)
            if len(keywords) >= max_keywords:
                break
    return keywords


# ── Phase 4b: epic-context-informed source reading ────────────────────────────


@dataclass
class EpicContext:
    """Structured extraction of context signals from a single sub-issue scope."""
    scope: str = ""
    file_paths: list[str] = field(default_factory=list)
    identifiers: list[str] = field(default_factory=list)
    component_names: list[str] = field(default_factory=list)
    dir_tags: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)


@dataclass
class AggregateEpicContext:
    """Aggregated context across all sub-issues in an epic."""
    per_sub_issues: list[EpicContext] = field(default_factory=list)
    all_file_paths: set[str] = field(default_factory=set)
    all_identifiers: set[str] = field(default_factory=set)
    all_component_names: set[str] = field(default_factory=set)
    all_dir_tags: set[str] = field(default_factory=set)


# ── Same-file task merging ────────────────────────────────────────────────────

@dataclass
class _MergedTask:
    """Result of merging one or more sub-issue tasks that share the same files."""
    title: str
    scope: str
    context: EpicContext


def _merge_same_file_tasks(
    titles: list[str],
    scopes: list[str],
    contexts: list[EpicContext],
) -> tuple:
    """Consolidate tasks that touch exactly the same set of files into one task.

    Groups tasks by the frozenset of their ``file_paths``.  Tasks with no
    file_paths, or whose file_path sets are unique, pass through unchanged.
    Tasks with overlapping but non-identical file_path sets are NOT merged —
    only exact set equality triggers consolidation.

    Returns ``(merged_titles, merged_scopes, merged_contexts)`` with the same
    relative ordering as the input (first occurrence of each group determines
    its position).
    """
    if not titles:
        return [], [], []
    if len(contexts) != len(titles):
        return titles, scopes, contexts
    groups: dict[Any, list[int]] = {}
    for idx, ctx in enumerate(contexts):
        key = frozenset(ctx.file_paths) if ctx.file_paths else ("__nofiles__", idx)
        groups.setdefault(key, []).append(idx)
    merged_titles: list[str] = []
    merged_scopes: list[str] = []
    merged_contexts: list[EpicContext] = []
    seen: set = set()
    for idx in range(len(titles)):
        ctx = contexts[idx]
        key = frozenset(ctx.file_paths) if ctx.file_paths else ("__nofiles__", idx)
        if key in seen:
            continue
        seen.add(key)
        group_indices = groups.get(key, [idx])
        if len(group_indices) == 1:
            merged_titles.append(titles[idx])
            merged_scopes.append(scopes[idx])
            merged_contexts.append(ctx)
        else:
            g_titles = [titles[i] for i in group_indices]
            g_scopes = [scopes[i] for i in group_indices]
            g_ctxs = [contexts[i] for i in group_indices]
            combined_title = " + ".join(g_titles)
            combined_scope = "\n".join(g_scopes)
            seen_fps: list[str] = []
            seen_fps_set: set = set()
            seen_ids: list[str] = []
            seen_ids_set: set = set()
            for c in g_ctxs:
                for fp in (c.file_paths or []):
                    if fp not in seen_fps_set:
                        seen_fps.append(fp)
                        seen_fps_set.add(fp)
                for ident in (c.identifiers or []):
                    if ident not in seen_ids_set:
                        seen_ids.append(ident)
                        seen_ids_set.add(ident)
            combined_ctx = EpicContext(
                scope=combined_scope,
                file_paths=seen_fps,
                identifiers=seen_ids,
            )
            merged_titles.append(combined_title)
            merged_scopes.append(combined_scope)
            merged_contexts.append(combined_ctx)
    return merged_titles, merged_scopes, merged_contexts


# ── Overlap-based blocking chain helpers ─────────────────────────────────────

def detect_file_overlap(contexts: list[EpicContext]) -> dict[str, set[int]]:
    """Group sub-issue contexts by shared file paths.

    Returns a mapping of ``{file_path: {context_indices}}`` for every file
    that appears in two or more contexts.  Files that appear in only one
    context are omitted (they produce no blocking chain).
    """
    file_to_indices: dict[str, set[int]] = {}
    for idx, ctx in enumerate(contexts):
        for fp in (ctx.file_paths or []):
            file_to_indices.setdefault(fp, set()).add(idx)
    return {fp: idxs for fp, idxs in file_to_indices.items() if len(idxs) > 1}


def build_blocking_edges(
    overlap: dict[str, set[int]],
    total_tasks: int,
    existing: dict[int, list[int]] | None = None,
) -> dict[int, list[int]]:
    """Build a blocking-edge map from an overlap group dict.

    For each file that two or more tasks share, the tasks that touch it are
    sorted and chained sequentially (task N+1 blocked by task N).  No cycles
    are produced since edges always point from lower to higher index.

    *existing* may contain pre-existing edges that are merged rather than
    replaced.  Duplicate edges are deduplicated.

    Returns ``{task_index: [blocking_task_indices]}``.
    """
    edges: dict[int, list[int]] = {k: list(v) for k, v in (existing or {}).items()}
    for fp, idxs in overlap.items():
        sorted_idxs = sorted(idxs)
        for i in range(1, len(sorted_idxs)):
            blocked = sorted_idxs[i]
            blocker = sorted_idxs[i - 1]
            lst = edges.setdefault(blocked, [])
            if blocker not in lst:
                lst.append(blocker)
    return edges


def _file_paths_overlap(paths_a: list[str], paths_b: list[str]) -> bool:
    """Return True if paths_a and paths_b share at least one file path."""
    if not paths_a or not paths_b:
        return False
    set_a = set(paths_a)
    return any(p in set_a for p in paths_b)


def _compute_sub_issue_dependencies(
    contexts: list[EpicContext],
    index: int,
    created_numbers: list[int],
    existing_deps: list[int] | None = None,
) -> list[int]:
    """Return the depends_on list for the sub-issue at *index*.

    Compares the sub-issue at *index* against all prior contexts using direct
    file_paths comparison.  To avoid redundant transitive deps, only the most
    recently created overlapping predecessor is chained — earlier predecessors
    are already reachable through the chain.

    Contexts without file_paths never create dependencies (keyword similarity
    alone is not used here — only explicit file-path overlap).

    *created_numbers* maps prior context indices to their GitHub issue numbers.
    *existing_deps* may contain pre-existing dependencies (deduplicated).
    """
    if index == 0:
        return list(existing_deps or [])
    # When no per-sub-issue context is available, fall back to fully sequential
    # ordering (each task depends on all prior) to avoid accidental parallelism.
    if not contexts or index >= len(contexts):
        deps = list(existing_deps or [])
        for n in created_numbers:
            if n not in deps:
                deps.append(n)
        return deps
    current_ctx = contexts[index]
    # When the current sub-issue has no file_paths, we cannot determine which
    # prior tasks it might conflict with.  Default to sequential (all prior
    # tasks as dependencies) so we don't accidentally parallelize unknown work.
    if not current_ctx.file_paths:
        deps = list(existing_deps or [])
        for n in created_numbers:
            if n not in deps:
                deps.append(n)
        return deps
    latest_overlapping_n: int | None = None
    for prior_idx, prior_n in enumerate(created_numbers):
        if prior_idx >= len(contexts):
            break
        if _file_paths_overlap(current_ctx.file_paths, contexts[prior_idx].file_paths):
            latest_overlapping_n = prior_n  # keep updating: want the most recent
    deps: list[int] = list(existing_deps or [])
    if latest_overlapping_n is not None and latest_overlapping_n not in deps:
        deps.append(latest_overlapping_n)
    return deps


# Well-known directories for dir-tag extraction
_KNOWN_DIRS = frozenset({"src", "lib", "app", "core", "tests", "scripts", "providers"})


def extract_epic_context(
    scope_text: str,
    known_components: set[str] | None = None,
) -> EpicContext:
    """Extract structured context signals from a scope/checklist item.

    Pure function — no filesystem access.

    Args:
        scope_text: Single sub-issue scope text.
        known_components: Optional set of known component names.
    """
    scope_text = scope_text or ""
    scope_lower = scope_text.lower()

    # 1. File paths — reuse path_re regex
    path_re = re.compile(
        r"(?:^|(?<=\s)|(?<=[\"'`(]))"
        r"([a-zA-Z0-9_][\w\-./]*[a-zA-Z0-9_\-]"
        r"\.(?:py|js|ts|jsx|tsx|java|go|rs|rb|c|cpp|h|md|yaml|yml|json|toml|sh))"
        r"(?:\b|$)"
    )
    file_paths: list[str] = []
    for m in path_re.finditer(scope_text):
        p = m.group(1)
        if p not in file_paths:
            file_paths.append(p)

    # 2. Identifiers — def/class names mentioned
    func_re = re.compile(r"\b(?:def|class)\s+([a-zA-Z_][a-zA-Z0-9_]*)\b")
    identifiers: list[str] = []
    for m in func_re.finditer(scope_text):
        name = m.group(1)
        if name not in identifiers:
            identifiers.append(name)

    # 3. Component names — cross-reference scope words against known_components
    component_names: list[str] = []
    if known_components:
        words = re.findall(r"\b[a-zA-Z_][a-zA-Z0-9_]+\b", scope_text)
        for w in words:
            lw = w.lower()
            if lw in {c.lower() for c in known_components} and lw not in component_names:
                component_names.append(lw)

    # 4. Dir tags — known directory names mentioned in scope
    dir_tags: list[str] = []
    for d in _KNOWN_DIRS:
        if re.search(rf"\b{re.escape(d)}\b", scope_lower) and d not in dir_tags:
            dir_tags.append(d)

    # 5. Keywords — significant tokens via _extract_keywords
    keywords = _extract_keywords(scope_text)

    return EpicContext(
        scope=scope_text,
        file_paths=file_paths,
        identifiers=identifiers,
        component_names=component_names,
        dir_tags=dir_tags,
        keywords=keywords,
    )


def load_known_components(workdir: str) -> set[str]:
    """Derive known component names from project structure.

    Sources:
    1. config/souls/*.md filenames — strip -daedalus.md
    2. Top-level Python package dirs
    3. core/*.py module basenames
    """
    components: set[str] = set()
    try:
        workdir_path = Path(workdir)
        if not workdir_path.exists():
            return components

        # 1. SOUL profile names
        souls_dir = workdir_path / "config" / "souls"
        if souls_dir.exists() and souls_dir.is_dir():
            for f in souls_dir.glob("*-daedalus.md"):
                name = f.stem
                if name.endswith("-daedalus"):
                    components.add(name[: -len("-daedalus")])

        # 2. Top-level package dirs
        for entry in workdir_path.iterdir():
            if entry.is_dir() and (entry / "__init__.py").exists():
                components.add(entry.name)

        # 3. core/*.py module basenames
        core_dir = workdir_path / "core"
        if core_dir.exists() and core_dir.is_dir():
            for f in core_dir.glob("*.py"):
                if f.stem != "__init__":
                    components.add(f.stem)

    except (OSError, ValueError) as exc:
        logger.debug("load_known_components: failed to scan workdir %s: %s", workdir, exc)

    return components


def filter_context_for_sub(
    file_contents: dict[str, str],
    sub_context: EpicContext,
    file_metadata: dict[str, str],
) -> dict[str, str]:
    """Filter file_contents to files relevant to this sub-issue.

    A file is relevant if its path, directory, or content matches
    sub_context signals. When no files match, returns all unchanged
    (graceful degradation).
    """
    if not file_contents:
        return file_contents

    # If the sub-context has NO signals at all, return everything (graceful)
    has_any_signal = (
        sub_context.file_paths
        or sub_context.identifiers
        or sub_context.component_names
        or sub_context.dir_tags
    )
    if not has_any_signal:
        return file_contents

    relevant: dict[str, str] = {}
    for file_path, content in file_contents.items():
        # Check signal match
        if _file_matches_sub_context(file_path, content, sub_context):
            relevant[file_path] = content

    # Graceful degradation: if no match, return all
    return relevant if relevant else file_contents


def _file_matches_sub_context(
    file_path: str,
    content: str,
    ctx: EpicContext,
) -> bool:
    """Return True when *file_path*/*content* match any sub-context signal."""
    path_lower = file_path.lower()

    # File path matches explicit paths mentioned
    for p in ctx.file_paths:
        if p.lower() in path_lower or path_lower.endswith(p.lower()):
            return True

    # Directory matches dir_tags
    for d in ctx.dir_tags:
        if f"/{d}/" in path_lower or path_lower.startswith(f"{d}/"):
            return True

    # Content contains identifiers
    for ident in ctx.identifiers:
        if ident in content:
            return True

    # Content contains component names
    for comp in ctx.component_names:
        if comp in content.lower():
            return True

    return False


def _build_aggregate_context(
    checklist_items: list[str],
    known_components: set[str] | None = None,
) -> AggregateEpicContext:
    """Build AggregateEpicContext from per-checklist-item scopes."""
    per_sub = [extract_epic_context(item, known_components) for item in checklist_items]
    agg_file_paths: set[str] = set()
    agg_identifiers: set[str] = set()
    agg_component_names: set[str] = set()
    agg_dir_tags: set[str] = set()
    for ctx in per_sub:
        agg_file_paths.update(ctx.file_paths)
        agg_identifiers.update(ctx.identifiers)
        agg_component_names.update(ctx.component_names)
        agg_dir_tags.update(ctx.dir_tags)
    return AggregateEpicContext(
        per_sub_issues=per_sub,
        all_file_paths=agg_file_paths,
        all_identifiers=agg_identifiers,
        all_component_names=agg_component_names,
        all_dir_tags=agg_dir_tags,
    )


def _grep_py_definitions(name: str, workdir: str, *, timeout: int = 5) -> list[str]:
    """Grep *workdir* for Python files defining ``def name`` or ``class name``.

    Returns the matching file paths (one per stdout line), or ``[]`` on any
    grep failure — no match, timeout, or missing binary (graceful degradation).
    """
    try:
        res = subprocess.run(
            ["grep", "-rl", "--include=*.py", "-e", f"def {name}", "-e", f"class {name}", workdir],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.SubprocessError, subprocess.TimeoutExpired, OSError):
        return []
    if res.returncode != 0:
        return []
    return [line.strip() for line in res.stdout.splitlines() if line.strip()]


def identify_relevant_files(
    scope_text: str,
    workdir: str,
    max_files: int = 10,
    max_depth: int = 5,
    epic_context: AggregateEpicContext | None = None,
) -> tuple[list[Path], dict]:
    """Identify source files in *workdir* relevant to *scope_text*.

    Four strategies, each gated so they only fire when the scope actually
    provides a signal — otherwise we return nothing (graceful degradation).

    1. **Path extraction** — explicit ``src/foo.py`` mentions in the scope.
    2. **Function/class scan** — ``def X`` / ``class Y`` patterns, grepped.
    3. **Directory heuristic** — scan common dirs (src/lib/app/core/tests)
       only when the scope mentions one of those names.
    4. **Extension fallback** — only if earlier strategies already found
       candidates and we're below *max_files*.
    """
    workdir_path = Path(workdir)
    if not workdir_path.exists():
        logger.warning("identify_relevant_files: workdir %s does not exist", workdir)
        return ([], {})

    candidates: set[Path] = set()
    metadata: dict[str, str] = {}

    def _add(p: Path, why: str) -> bool:
        if p in candidates:
            return False
        candidates.add(p)
        metadata[str(p)] = why
        return len(candidates) >= max_files

    # ── Epic-context priority boost (Strategy 0) ─────────────────────────
    # When an AggregateEpicContext is provided, files mentioned in its
    # all_file_paths are added FIRST with the highest strategy tag, so they
    # appear before scope-only matches. All aggregated identifiers are also
    # grepped directly, giving the planner a wider signal window.
    if epic_context is not None:
        for p in sorted(epic_context.all_file_paths):
            try:
                fp = workdir_path / p
                if fp.exists() and fp.is_file() and fp.resolve().is_relative_to(workdir_path.resolve()):
                    if _add(fp, "epic_context:path"):
                        break
            except (OSError, ValueError):
                continue
        # Grep for aggregated identifiers directly (more precise than
        # re-extracting from raw scope text).
        if not (len(candidates) >= max_files):
            for ident in sorted(epic_context.all_identifiers):
                for line in _grep_py_definitions(ident, workdir):
                    fp = Path(line)
                    try:
                        if fp.exists() and fp.resolve().is_relative_to(workdir_path.resolve()):
                            if _add(fp, f"epic_context:ident:{ident}"):
                                break
                    except (OSError, ValueError):
                        continue
                if len(candidates) >= max_files:
                    break

    # Strategy 1 — explicit file paths mentioned in scope text.
    # Matches things like ``src/foo.py``, ``./lib/utils.js``, ``core/iterate.py``.
    path_re = re.compile(
        r"(?:^|(?<=\s)|(?<=[\"'`(]))"
        r"([a-zA-Z0-9_][\w\-./]*[a-zA-Z0-9_\-]"
        r"\.(?:py|js|ts|jsx|tsx|java|go|rs|rb|c|cpp|h|md|yaml|yml|json|toml|sh))"
        r"(?:\b|$)",
    )
    for m in path_re.finditer(scope_text or ""):
        fp = workdir_path / m.group(1)
        try:
            if fp.exists() and fp.is_file() and fp.resolve().is_relative_to(workdir_path.resolve()):
                if _add(fp, "path_extraction"):
                    break
        except (OSError, ValueError):
            continue

    # Strategy 2 — grep for def/class declarations named in the scope.
    func_re = re.compile(r"\b(?:def|class)\s+([a-zA-Z_][a-zA-Z0-9_]*)\b")
    for m in func_re.finditer(scope_text or ""):
        name = m.group(1)
        for line in _grep_py_definitions(name, workdir):
            fp = Path(line)
            try:
                if fp.exists() and fp.resolve().is_relative_to(workdir_path.resolve()):
                    if _add(fp, f"definition_scan:{name}"):
                        break
            except (OSError, ValueError):
                continue
        if len(candidates) >= max_files:
            break

    # Strategy 3 — directory heuristic. Only fires when scope mentions
    # one of the common directory names, so "Add new feature" returns
    # empty rather than dumping the whole repo.
    common_dirs = ["src", "lib", "app", "core", "tests"]
    scope_lower = (scope_text or "").lower()
    scope_mentions_dir = any(
        re.search(rf"\b{re.escape(d)}\b", scope_lower) for d in common_dirs
    )
    if scope_mentions_dir:
        code_exts = ("*.py", "*.js", "*.ts", "*.jsx", "*.tsx", "*.java", "*.go", "*.rs")
        for dir_name in common_dirs:
            if not re.search(rf"\b{re.escape(dir_name)}\b", scope_lower):
                continue
            dir_path = workdir_path / dir_name
            if not (dir_path.exists() and dir_path.is_dir()):
                continue
            # BFS up to max_depth, but cap total files aggressively.
            for ext in code_exts:
                for fp in dir_path.rglob(ext):
                    # Respect max_depth
                    try:
                        rel = fp.resolve().relative_to(dir_path.resolve())
                        if len(rel.parts) > max_depth:
                            continue
                    except (OSError, ValueError):
                        continue
                    if fp.is_file():
                        if _add(fp, f"directory_scan:{dir_name}"):
                            break
                if len(candidates) >= max_files:
                    break
            if len(candidates) >= max_files:
                break

    # Strategy 4 — extension fallback. Only if we already have signal AND
    # haven't hit max_files yet. Walks the repo once.
    if 0 < len(candidates) < max_files:
        code_exts = ("*.py", "*.js", "*.ts", "*.jsx", "*.tsx", "*.java", "*.go", "*.rs")
        for ext in code_exts:
            if len(candidates) >= max_files:
                break
            for fp in workdir_path.rglob(ext):
                try:
                    if not fp.is_file() or not fp.resolve().is_relative_to(workdir_path.resolve()):
                        continue
                    # Skip common non-source dirs
                    rel = str(fp.relative_to(workdir_path))
                    if any(seg in rel for seg in ("node_modules", ".git", "__pycache__", ".venv", "venv", ".tox")):
                        continue
                except (OSError, ValueError):
                    continue
                if _add(fp, f"extension_fallback:{ext}"):
                    break
            if len(candidates) >= max_files:
                break

    return (list(candidates), metadata)


def read_source_files(
    file_paths: list[Path],
    workdir: str,
    max_size: int = 50_000,
) -> dict[str, str]:
    """Read source files with safety checks.

    - **Binary detection** — skip files whose first 1 KiB contains a NUL byte.
    - **Size limit** — truncate UTF-8 output to *max_size* bytes.
    - **Symlink safety** — resolve symlinks before reading.
    - **Path traversal** — refuse files outside *workdir*.
    """
    contents: dict[str, str] = {}
    try:
        workdir_path = Path(workdir).resolve()
    except (OSError, ValueError):
        return contents

    for file_path_obj in file_paths:
        try:
            resolved = file_path_obj.resolve()
            # Path-traversal guard
            if not resolved.is_relative_to(workdir_path):
                logger.warning("read_source_files: path traversal blocked: %s", file_path_obj)
                continue
            if not resolved.exists() or not resolved.is_file():
                logger.warning("read_source_files: file not found: %s", file_path_obj)
                continue
            # Binary detection — NUL byte in first 1 KiB
            with open(resolved, "rb") as fh:
                head = fh.read(1024)
                if b"\x00" in head:
                    logger.info("read_source_files: skipping binary file: %s", file_path_obj)
                    continue
            raw = resolved.read_text(encoding="utf-8", errors="ignore")
            # Truncate to max_size bytes (not chars)
            encoded_len = len(raw.encode("utf-8"))
            if encoded_len > max_size:
                # Binary-search the char cutoff that produces ≤ max_size bytes
                cutoff = max_size
                raw = raw[:cutoff]
                while len(raw.encode("utf-8")) > max_size and cutoff > 0:
                    cutoff = max(0, cutoff - max(1, (len(raw.encode("utf-8")) - max_size)))
                    raw = raw[:cutoff]
                logger.info(
                    "read_source_files: truncated %s from %d to %d bytes",
                    file_path_obj, encoded_len, len(raw.encode("utf-8")),
                )
            # Dict keys use the original string form for stable lookups.
            contents[str(file_path_obj)] = raw
        except Exception as exc:  # noqa: BLE001
            logger.warning("read_source_files: error reading %s: %s", file_path_obj, exc)
    return contents


def build_sub_issue_context(file_contents: dict[str, str]) -> str:
    """Format file contents into a markdown context block."""
    if not file_contents:
        return ""
    lines = ["## Relevant Source Context", ""]
    for file_path, content in file_contents.items():
        lines.append(f"### `{file_path}`")
        lines.append("")
        lines.append("```")
        lines.append(content)
        lines.append("```")
        lines.append("")
    return "\n".join(lines)


def build_enhanced_scope(original_scope: str, source_context: str) -> str:
    """Combine *original_scope* with *source_context*.

    If *source_context* is empty, return the original unchanged (graceful
    degradation). Otherwise append the context as an additional section.
    """
    if not source_context:
        return original_scope
    return f"{original_scope}\n\n{source_context}"



# ── block-loop rescue (issue #1119) ──────────────────────────────────────────

# Gate profiles whose block reason can carry a terminal passing verdict, and
# the verdict prefixes that mean "the gate passed — complete, don't re-run".
# Matched with startswith on the lowercased reason so prose that merely
# contains the word ("tests pass") can't false-positive (same rationale as
# the removed "pass" signal in _parse_handoff).
_BLOCK_LOOP_PASS_PREFIXES: dict[str, tuple] = {
    "qa-daedalus": ("qa-passed",),
    "reviewer-daedalus": ("review-approved", "approved", "lgtm"),
    "security-analyst-daedalus": ("security-approved", "security-passed"),
}

# Statuses the rescue scan never touches: terminal states, plus 'blocked'
# (a card still in the blocked column is owned by the main blocked scan).
_RESCUE_SKIP_STATUSES = ("done", "complete", "completed", "archived",
                         "cancelled", "blocked")


def _latest_block_loop_reason(detail: dict) -> str | None:
    """Reason of the most recent ``block_loop_detected`` event, or None.

    ``detail`` is a ``kanban.show_card`` dict; its ``events`` list carries the
    framework's loop-detection events with ``payload.reason`` set to the block
    reason that kept recurring. Returns None when the task never hit a block
    loop (the empty string when the event has no reason).
    """
    for ev in reversed(detail.get("events") or []):
        if (ev.get("kind") or "") != "block_loop_detected":
            continue
        payload = ev.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}
        if not isinstance(payload, dict):
            payload = {}
        return str(payload.get("reason") or "")
    return None


def _rescue_block_loop_gate_cards(
    slug: str,
    repo: str,
    *,
    exclude_ids: set[str] | None = None,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """Complete gate cards the framework re-promoted despite a passing verdict.

    When ``kanban.complete()`` fails transiently (rate limit) on a gate card
    blocked with a passing verdict (``qa-passed:`` / ``review-approved:`` /
    ``security-approved:``), the Hermes framework's loop detection fires
    ``block_loop_detected`` and auto-resolves by posting ``specified`` +
    ``promoted`` — putting the task back into running and re-running the whole
    gate (#1119). Those cards leave the blocked column, so the main blocked
    scan in ``run_iterate`` never sees them.

    This scan finds active gate-profile tasks whose most recent
    ``block_loop_detected`` event carries a passing verdict and routes them to
    the same executors the blocked-card path uses: QA cards to
    ``_execute_advance`` (complete + downstream review tasks) and
    reviewer/security cards to ``_execute_approve_advance`` (complete). If
    ``complete()`` fails again the card stays active and the next tick
    retries — degrade gracefully, never crash the tick.

    Returns a list of ``{tid, action, pr, ok}`` dicts for attempted rescues.
    """
    exclude = exclude_ids or set()
    try:
        tasks = kanban.list_tasks(slug)
    except Exception as e:
        logger.error("iterate: block-loop rescue — list_tasks failed for %s: %s", slug, e)
        return []

    entries: list[dict[str, Any]] = []
    for t in tasks or []:
        tid = str(t.get("id") or "")
        assignee = (t.get("assignee") or "").lower().strip()
        status = (t.get("status") or "").lower().strip()
        if not tid or tid in exclude or status in _RESCUE_SKIP_STATUSES:
            continue
        prefixes = _BLOCK_LOOP_PASS_PREFIXES.get(assignee)
        if not prefixes:
            continue
        detail = kanban.show_card(slug, tid)
        if not detail:
            continue
        reason = _latest_block_loop_reason(detail)
        if reason is None:
            continue  # never hit a block loop — the blocked-card path owns it
        verdict = (reason or detail.get("latest_summary") or "").strip()
        if not verdict.lower().startswith(prefixes):
            continue
        card = dict(detail.get("task") or {})
        card.setdefault("id", tid)
        pr = _parse_pr_number(verdict)
        try:
            if assignee == "qa-daedalus":
                action = ADVANCE
                ok = _execute_advance(slug, card, repo, verdict,
                                      dry_run=dry_run, pr_number=pr)
            else:
                action = APPROVE_ADVANCE
                ok = _execute_approve_advance(slug, card, repo, verdict,
                                              dry_run=dry_run)
        except Exception as e:
            logger.error("iterate: block-loop rescue executor failed for %s: %s", tid, e)
            continue
        logger.info(
            "iterate: block-loop rescue — %s %s (%s), verdict %r → %s",
            action, tid, assignee, verdict[:80],
            "ok" if ok else "failed (retry next tick)")
        entries.append({"tid": tid, "action": action, "pr": pr, "ok": bool(ok)})
    return entries


# ── main loop ───────────────────────────────────────────────────────────────


def _try_merge_if_gates_pass(
    slug: str,
    issue_n: int | None,
    pr: int | None,
    provider: Any,
    *,
    merge_method: str,
    skip_qa: bool,
    ci_status: str,
    dry_run: bool = False,
    active_tasks: list[dict[str, Any]] | None = None,
    archived_tasks: list[dict[str, Any]] | None = None,
) -> bool:
    """Merge ``pr`` iff every pipeline gate passes. Returns True only on an actual
    merge; idempotent and safe to call repeatedly.

    Called from two places: the docs-card-completion path (below) AND the per-tick
    deferred-merge sweep (``sweep_deferred_merges``). A failed ``merge_pr`` — e.g.
    the PR is momentarily un-mergeable due to a conflict, or CI hadn't gone green
    at docs-completion — returns False instead of consuming the only merge attempt,
    so a later tick retries. This is what makes auto-merge no longer one-shot (#1178).

    Gates (all bypassed by the ``skip-qa`` label, per #1074): QA passed, reviewer
    approved, security cleared, CI green, and the PR not already merged.

    ``active_tasks``/``archived_tasks`` let the deferred-merge sweep pass a
    once-per-tick board snapshot so the three gate checks don't each re-run
    ``list_tasks`` per PR (#1135). Both default to ``None`` (self-fetch).
    """
    if pr is None or provider is None:
        return False
    if not skip_qa and not _qa_passed_for_issue(
            slug, issue_n, active_tasks=active_tasks, archived_tasks=archived_tasks):
        logger.warning(
            "iterate: Skipping merge: QA has not passed for PR #%s (issue #%s).", pr, issue_n)
        return False
    if skip_qa:
        logger.info(
            "iterate: skip-qa label present on PR #%s — bypassing QA/reviewer/security gates", pr)
    if not skip_qa and not _reviewer_passed_for_issue(
            slug, issue_n, active_tasks=active_tasks, archived_tasks=archived_tasks):
        logger.warning(
            "iterate: Skipping merge: reviewer has not approved PR #%s (issue #%s).", pr, issue_n)
        return False
    if not skip_qa and not _security_passed_for_issue(
            slug, issue_n, active_tasks=active_tasks, archived_tasks=archived_tasks):
        logger.warning(
            "iterate: Skipping merge: security has not cleared PR #%s (issue #%s).", pr, issue_n)
        return False
    # CI gate: green required when the provider supports CI checks; UNKNOWN (no CI
    # configured) is treated as green so CI-less repos aren't blocked.
    provider_supports_ci = getattr(provider, "supports_ci_status", False)
    if provider_supports_ci and ci_status != CIStatus.GREEN:
        logger.warning(
            "iterate: Skipping merge: CI not green for PR #%s (status: %s).", pr, ci_status)
        return False
    # Idempotency: never double-merge.
    if hasattr(provider, "is_pr_merged"):
        try:
            if provider.is_pr_merged(pr):
                logger.info(
                    "iterate: Skipping merge: PR #%s already merged (idempotent skip)", pr)
                return False
        except Exception as e:
            logger.warning(
                "iterate: is_pr_merged check failed for PR #%s: %s — proceeding", pr, e)
    logger.info(
        "iterate: all gates passed for PR #%s (QA/reviewer/security: %s, CI: %s) — merging",
        pr, "skip-qa" if skip_qa else "passed",
        ci_status if provider_supports_ci else "n/a",
    )
    if dry_run:
        logger.info("[dry-run] auto_merge=true: would merge PR #%s (%s)", pr, merge_method)
        return False
    merged = provider.merge_pr(pr, merge_method=merge_method)
    if merged:
        logger.info("iterate: auto-merged PR #%s (%s)", pr, merge_method)
        return True
    logger.warning(
        "iterate: auto_merge failed for PR #%s — leaving open; a later tick will retry", pr)
    return False


# ── bounded CI re-run for transiently-red pipeline-complete PRs (#1199) ───────
# Max automatic CI re-runs per PR head SHA before we escalate. A persistent red
# after this many re-runs is a real failure, not a flake, so we stop and notify.
CI_RERUN_MAX = 2
# Per-SHA marker comments posted on the PR make the retry idempotent across ticks
# and same-tick re-invocations (the PR itself is the source of truth). A new head
# SHA (branch pushed) yields a fresh budget — new code deserves fresh attempts.
_CI_RERUN_MARKER_PREFIX = "<!-- daedalus:ci-rerun:"
_CI_ESCALATED_MARKER_PREFIX = "<!-- daedalus:ci-escalated:"


def _ci_rerun_attempts(comments: list[Any], sha: str) -> int:
    """Count re-run marker comments already posted for ``sha``."""
    marker = f"{_CI_RERUN_MARKER_PREFIX}{sha}:"
    return sum(1 for c in comments if marker in (getattr(c, "body", "") or ""))


def _ci_already_escalated(comments: list[Any], sha: str) -> bool:
    """True if this SHA has already been escalated — the loop stop."""
    marker = f"{_CI_ESCALATED_MARKER_PREFIX}{sha} -->"
    return any(marker in (getattr(c, "body", "") or "") for c in comments)


def _rerun_or_escalate_red_ci(
    slug: str,
    issue_n: int | None,
    pr: int,
    provider: Any,
    *,
    dry_run: bool = False,
) -> str:
    """Handle a pipeline-complete PR whose required CI is genuinely RED (#1199).

    Bounded-retry the failed CI run (``CI_RERUN_MAX`` per head SHA); once the
    budget is spent and CI is still red, escalate with the failing-run URL
    instead of looping. Idempotent via per-SHA marker comments on the PR.

    Natural inter-tick backoff: issuing a re-run flips CI to PENDING, so the
    sweep won't act again until it settles back to RED — no timer needed.

    Returns one of ``"rerun"``, ``"escalated"``, or ``""`` (no-op this tick).
    """
    if provider is None or not getattr(provider, "supports_ci_rerun", False):
        return ""
    try:
        sha = provider.get_pr_head_sha(pr)
    except Exception as e:
        logger.warning("iterate: CI-rerun: get_pr_head_sha failed for PR #%s: %s", pr, e)
        return ""
    if not sha:
        logger.warning("iterate: CI-rerun: no head SHA for PR #%s — skipping", pr)
        return ""
    try:
        comments = provider.list_pr_comments(pr)
    except Exception:
        comments = []
    if _ci_already_escalated(comments, sha):
        return ""  # already escalated for this SHA — never loop
    attempts = _ci_rerun_attempts(comments, sha)

    if attempts < CI_RERUN_MAX:
        n = attempts + 1
        if dry_run:
            logger.info(
                "[dry-run] would re-run failed CI for PR #%s (attempt %d/%d, sha %s)",
                pr, n, CI_RERUN_MAX, sha[:8])
            return "rerun"
        ok = False
        try:
            ok = bool(provider.rerun_failed_ci(pr))
        except Exception as e:
            logger.warning("iterate: CI-rerun failed for PR #%s: %s", pr, e)
        if not ok:
            logger.warning(
                "iterate: CI-rerun no-op for PR #%s (attempt %d/%d) — no failed run or API error",
                pr, n, CI_RERUN_MAX)
            return ""
        # Persist the marker only after a successful re-run so a failed request
        # doesn't silently burn an attempt.
        provider.post_pr_comment(
            pr,
            f"{_CI_RERUN_MARKER_PREFIX}{sha}:{n} -->\n\n"
            f"♻️ Auto re-ran failed CI (attempt {n}/{CI_RERUN_MAX}) — "
            f"transient failure suspected; will merge automatically once green.")
        logger.info(
            "iterate: re-ran failed CI for PR #%s (issue #%s, attempt %d/%d, sha %s)",
            pr, issue_n, n, CI_RERUN_MAX, sha[:8])
        return "rerun"

    # Budget spent and still red → escalate once (no loop).
    try:
        run_url = provider.failed_ci_run_url(pr) or ""
    except Exception:
        run_url = ""
    msg = (
        f"⚠️ ESCALATE: PR #{pr} (issue #{issue_n}) — required CI is still RED after "
        f"{CI_RERUN_MAX} automatic re-runs. A persistent red is a real failure, not a "
        f"flake — manual intervention required.")
    if run_url:
        msg += f"\n\nFailing run: {run_url}"
    if dry_run:
        logger.info("[dry-run] %s", msg)
        return "escalated"
    provider.post_pr_comment(pr, f"{_CI_ESCALATED_MARKER_PREFIX}{sha} -->\n\n{msg}")
    logger.warning("iterate: %s", msg)
    return "escalated"


def sweep_deferred_merges(
    slug: str,
    repo: str,
    provider: Any,
    resolved: dict[str, Any] | None,
    *,
    dry_run: bool = False,
) -> list[int]:
    """Retry auto-merge for PRs whose pipeline finished but that weren't merged at
    docs-card completion (#1178).

    The docs-completion merge is one-shot: if the PR was momentarily un-mergeable
    (e.g. a CHANGELOG conflict) or CI hadn't gone green yet, the docs card still
    completes and drops out of ``list_blocked``, so the merge never retries. This
    sweep runs every tick: for each DONE ``docs-<n>`` card whose issue is still open
    with an open PR, it re-checks the gates and merges. Idempotent. Returns the PR
    numbers merged this tick.
    """
    execution = (resolved or {}).get("execution") or {}
    if not bool(execution.get("auto_merge", False)) or provider is None:
        return []
    merge_method = str(execution.get("merge_method", "squash")).lower()
    try:
        tasks = kanban.list_tasks(slug) or []
    except Exception as e:
        logger.error("iterate: deferred-merge sweep failed to list tasks: %s", e)
        return []
    # Pre-fetch the archived list once too, so the per-PR gate checks
    # (_qa/_reviewer/_security_passed_for_issue) reuse both board snapshots
    # instead of each re-running list_tasks (active + archived) per PR (#1135).
    # Done gate cards archive quickly, so the archived list is the common
    # match path. On fetch failure fall back to None → gate helpers self-fetch
    # (prior behaviour) rather than silently seeing zero archived cards.
    try:
        archived_tasks: list[dict[str, Any]] | None = kanban.list_tasks(slug, status="archived") or []
    except Exception as e:
        logger.error("iterate: deferred-merge sweep failed to list archived tasks: %s", e)
        archived_tasks = None
    merged: list[int] = []
    ci_cache: dict[int, str] = {}
    seen_issues: set[int] = set()
    # Match documentation cards by ASSIGNEE (title formats vary; idempotency_key
    # is no longer returned by the kanban API). Extract the issue number from the
    # title via the canonical helper.
    #
    # Scan BOTH the active board AND the archived list, and accept a docs card in
    # either the DONE or ARCHIVED state. Completed gate cards archive quickly
    # (#1141) — the documentation card (terminal stage) is frequently already
    # archived by the time this every-tick sweep runs. Scanning only active DONE
    # cards therefore never even *considered* a pipeline-complete PR once its docs
    # card archived, stranding it open until a manual merge (#1226). Archiving only
    # happens post-completion, so an archived docs card is a valid completion
    # signal; the gate/CI/mergeability checks below still re-verify before merging.
    candidate_tasks = list(tasks)
    if archived_tasks:
        candidate_tasks += archived_tasks
    for task in candidate_tasks:
        if (task.get("status") or "").lower() not in ("done", "archived"):
            continue
        if not (task.get("assignee") or "").strip().lower().startswith("documentation-"):
            continue
        issue_n = extract_issue_number(task.get("title") or "")
        if issue_n is None or issue_n in seen_issues:
            continue
        seen_issues.add(issue_n)
        # A closed issue already landed; only still-open issues need merging.
        if hasattr(provider, "is_issue_open"):
            try:
                if not provider.is_issue_open(issue_n):
                    continue
            except Exception:
                pass
        # Deterministic branch from worktree isolation (#1176): fix/issue-<n>.
        try:
            pr = provider.find_pr_for_branch(f"fix/issue-{issue_n}")
        except Exception:
            pr = None
        if pr is None:
            logger.debug(
                "iterate: deferred-merge sweep: no open PR for issue #%s "
                "(branch fix/issue-%s) — skipping", issue_n, issue_n)
            continue
        if pr not in ci_cache:
            try:
                ci_cache[pr] = provider.get_pr_ci_status(pr)
            except Exception:
                ci_cache[pr] = CIStatus.UNKNOWN
        skip_qa = False
        if hasattr(provider, "has_label"):
            try:
                skip_qa = bool(provider.has_label(pr, "skip-qa"))
            except Exception:
                skip_qa = False
        if _try_merge_if_gates_pass(
            slug, issue_n, pr, provider,
            merge_method=merge_method, skip_qa=skip_qa,
            ci_status=ci_cache[pr], dry_run=dry_run,
            active_tasks=tasks, archived_tasks=archived_tasks,
        ):
            merged.append(pr)
        elif ci_cache[pr] == CIStatus.RED:
            # Pipeline-complete PR with a genuinely RED (not pending/unknown)
            # required CI: bounded-retry the failed run, then escalate (#1199).
            # A re-run flips CI to PENDING, so the merge path picks it up on a
            # later tick once it goes green.
            _rerun_or_escalate_red_ci(
                slug, issue_n, pr, provider, dry_run=dry_run)
    if merged:
        logger.info("iterate: deferred-merge sweep merged PR(s): %s", merged)
    return merged


def run_iterate(
    slug: str,
    repo: str,
    *,
    resolved: dict[str, Any] | None = None,
    provider: Any | None = None,
    dry_run: bool = False,
    max_fix_attempts: int = MAX_FIX_ATTEMPTS,
) -> tuple[dict[str, int], list[int], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Run the auto-advance routing and self-healing loop.

    For every blocked card on the board, classify its state and execute the
    appropriate action. Returns (counts, advance_prs, pending_signal_cards,
    qa_failed_cards, escalated_cards) where advance_prs lists PR numbers for
    cards that were successfully advanced, pending_signal_cards lists cards skipped
    because QA/a11y signal was unrecognized, qa_failed_cards lists dicts with
    {issue_n, pr, reason} for QA cards that created a developer fix card, and
    escalated_cards lists dicts with {issue_n, pr, reason} for QA cards that
    exhausted MAX_FIX_ATTEMPTS and triggered escalation.

    Args:
        slug: Kanban board slug.
        repo: Repo identifier (org/name) — used in card bodies only.
        resolved: Optional resolved project config (for workdir, notify_target).
        provider: Optional VCS provider (core.providers.VCSProvider) for PR/CI
            lookups. Without one, branch→PR resolution is skipped and CI is
            treated as not-green.
        dry_run: If True, log intentions without mutating anything.
        max_fix_attempts: Escalation cap for developer/reviewer/security fix
            cycles. Defaults to the module constant ``MAX_FIX_ATTEMPTS`` (3);
            the dispatcher resolves ``execution.max_fix_attempts`` and threads
            the per-project override in here.

    Returns:
        (counts, advance_prs, pending_signal_cards, qa_failed_cards, escalated_cards) tuple.
    """
    counts: dict[str, int] = {
        ADVANCE: 0,
        QA_FIX: 0,
        PENDING_SIGNAL: 0,
        PENDING_PR: 0,
        PM_ROUTE: 0,
        APPROVE_ADVANCE: 0,
        ESCALATE: 0,
        PLANNER_DECOMPOSE: 0,
        RECONCILE_MERGED: 0,
    }
    advance_prs: list[int] = []  # PR numbers for cards that were advanced
    pending_signal_cards: list[dict[str, Any]] = []  # Cards with unrecognized QA/a11y signal
    qa_failed_cards: list[dict[str, Any]] = []  # QA cards that created a fix card
    escalated_cards: list[dict[str, Any]] = []  # QA cards that hit MAX_FIX_ATTEMPTS

    workdir = (resolved or {}).get("workdir", "")
    notify_target = (resolved or {}).get("cron", {}).get("deliver", "")
    router_profile = (resolved or {}).get("router_profile", "project-manager-daedalus")
    execution = (resolved or {}).get("execution") or {}
    auto_merge = bool(execution.get("auto_merge", False))
    merge_method = str(execution.get("merge_method", "squash")).lower()

    blocked_cards = kanban.list_blocked(slug)

    # ── block-loop rescue (issue #1119) ──────────────────────────────────
    # Gate cards whose complete() failed transiently get auto-promoted out
    # of the blocked column by the framework's loop detection, so the
    # blocked scan below never sees them. Rescue them here — this must run
    # even when the blocked column is empty (the re-promoted card is the
    # only sign anything is wrong).
    blocked_ids = {str(c.get("id")) for c in blocked_cards if c.get("id")}
    for entry in _rescue_block_loop_gate_cards(
            slug, repo, exclude_ids=blocked_ids, dry_run=dry_run):
        if not entry.get("ok"):
            continue
        counts[entry["action"]] = counts.get(entry["action"], 0) + 1
        if entry["action"] == ADVANCE and entry.get("pr") is not None:
            advance_prs.append(entry["pr"])

    # Deferred auto-merge sweep (#1178): retry merges the one-shot docs-completion
    # path missed (PR became mergeable / CI-green only after the docs card completed).
    # Runs every tick — including when nothing is blocked — since a merge-ready PR
    # leaves no blocked card behind to re-trigger the merge.
    try:
        advance_prs.extend(
            sweep_deferred_merges(slug, repo, provider, resolved, dry_run=dry_run)
        )
    except Exception as e:  # never let the merge sweep break a dispatch tick
        logger.error("iterate: deferred-merge sweep error: %s", e)

    if not blocked_cards:
        return counts, advance_prs, pending_signal_cards, qa_failed_cards, escalated_cards

    # Collect PR→CI cache so we don't call the provider for the same PR twice.
    # Stores the raw CIStatus string (not bool) so UNKNOWN/PENDING are distinguishable.
    ci_cache: dict[int, str] = {}

    # Per-tick escalation dedup: tracks which issue numbers have already been
    # escalated this tick. Maps issue number → first card's tid that escalated.
    escalated_issues: dict[int, str] = {}

    for card in blocked_cards:
        tid = card.get("id")
        if not tid:
            continue

        assignee = (card.get("assignee") or "").strip()
        handoff = _handoff_from_card(card)

        # Fallback: list_blocked returns minimal dicts without runs/reasons.
        # Fetch the full card detail via show_card and use latest_summary.
        if not handoff and tid:
            detail = kanban.show_card(slug, tid)
            if detail:
                handoff = (detail.get("latest_summary") or "").strip()

        fix_attempts = _count_fix_attempts(card)

        pr = _parse_pr_number(handoff)

        # Fallback: if handoff has no PR #, try the card's branch_name.
        if pr is None:
            branch_name = (card.get("branch_name") or "").strip()
            if branch_name and provider is not None:
                pr = provider.find_pr_for_branch(branch_name)
                if pr is not None:
                    logger.info("iterate: %s resolved PR #%s via branch %s",
                                tid, pr, branch_name)

        ci_green = False
        raw_ci = CIStatus.UNKNOWN
        if pr is not None and provider is not None:
            if pr not in ci_cache:
                ci_cache[pr] = provider.get_pr_ci_status(pr)
            raw_ci = ci_cache[pr]

            # No CI configured → no gate: treat UNKNOWN as green when the
            # provider doesn't support CI status checks (e.g. no check runs).
            if not getattr(provider, "supports_ci_status", False) and raw_ci == CIStatus.UNKNOWN:
                logger.info("iterate: %s provider has no CI support — treating as green", tid)
                ci_green = True
            else:
                ci_green = (raw_ci == CIStatus.GREEN)

        # #953: verify the resolved PR is a real, open PR before a developer
        # card can advance and release its QA child. Only checked for developer
        # cards (the only branch that gates on it) to avoid extra provider
        # calls. Unverifiable (provider lacks the capability or errors) stays
        # None → prior behaviour; only an affirmative "not open" blocks advance.
        pr_is_open: bool | None = None
        pr_is_merged: bool | None = None
        if (pr is not None and provider is not None
                and assignee.lower().strip() == "developer-daedalus"
                and hasattr(provider, "is_pr_open")):
            try:
                pr_is_open = bool(provider.is_pr_open(pr))
            except Exception:
                pr_is_open = None
            # #957: only when the PR is not open is "merged" interesting — a
            # merged PR means the work landed; reconcile the issue's cards
            # instead of holding in PENDING_PR. Skip the extra provider call
            # when the PR is open or its state is unverifiable.
            if pr_is_open is False and hasattr(provider, "is_pr_merged"):
                try:
                    pr_is_merged = bool(provider.is_pr_merged(pr))
                except Exception:
                    pr_is_merged = None

        # Detect skip-qa label on PR (bypass QA gate)
        skip_qa = False
        if pr is not None and provider is not None:
            skip_qa = bool(provider.has_label(pr, "skip-qa"))

        action = classify_blocked(assignee, handoff, ci_green,
                                  fix_attempts=fix_attempts, pr_number=pr,
                                  raw_ci=raw_ci, pr_is_open=pr_is_open,
                                  pr_is_merged=pr_is_merged,
                                  skip_qa=skip_qa,
                                  max_fix_attempts=max_fix_attempts)

        # ── Escalation dedup (issue #35) ─────────────────────────────────
        # Before executing ESCALATE, check two layers of dedup:
        #   1. Cross-tick stamp: card already has "escalated: issue #N" comment.
        #   2. Per-tick sentinel: another card already escalated for this issue.
        # Both layers skip the card silently (or complete duplicates).
        if action == ESCALATE:
            issue_n = _extract_issue_number_from_card(card)

            # Layer 2: per-tick dedup (different card, same issue, same tick)
            if issue_n is not None and issue_n in escalated_issues:
                first_tid = escalated_issues[issue_n]
                if dry_run:
                    logger.info(
                        "[dry-run] would skip duplicate ESCALATE for %s "
                        "(already escalated by %s)", tid, first_tid)
                else:
                    logger.info(
                        "iterate: %s skipping duplicate ESCALATE for "
                        "issue #%s (already escalated by %s)",
                        tid, issue_n, first_tid)
                    kanban.complete(
                        slug, tid,
                        summary=f"skipped: escalated by {first_tid}")
                continue

            # Layer 1: cross-tick stamp (same card, previous tick already escalated)
            if issue_n is not None and _is_card_already_escalated(slug, tid, issue_n):
                logger.info(
                    "iterate: %s already stamped escalated: issue #%s — skipping",
                    tid, issue_n)
                continue

            # Record this card as the escalation owner for this issue/tick
            if issue_n is not None:
                escalated_issues[issue_n] = tid

        # PENDING_SIGNAL is a skip-action: card goes to pending_signal_cards
        # because the QA/a11y agent posted an unrecognized signal (still running,
        # crash, typo). No executor needed — the next cron tick re-evaluates.
        if action == PENDING_SIGNAL:
            pending_signal_cards.append({"tid": tid, "pr": pr, "card": card})
            counts[PENDING_SIGNAL] += 1
            logger.info("iterate: %s unrecognized QA/a11y signal — deferred to next tick", tid)
            continue

        # PENDING_PR: run the executor inline (it updates the block reason when
        # a PR is found; if no PR yet it's a no-op). Count and continue.
        if action == PENDING_PR:
            _execute_pending_pr(slug, card, repo, handoff, provider=provider, dry_run=dry_run)
            counts[PENDING_PR] += 1
            logger.info("iterate: %s awaiting PR for issue #%s", tid,
                        _extract_issue_number_from_card(card))
            continue

        if not action:
            continue  # nothing to do for this card

        executor = _ACTION_EXECUTORS.get(action)
        if not executor:
            logger.warning("iterate: unknown action '%s' for card %s", action, tid)
            continue

        # ── Pre-executor CI gate for docs auto-merge (issue #1085) ──────────
        # When the docs card is about to be completed (APPROVE_ADVANCE) and
        # auto_merge is enabled, CI must be green BEFORE we complete the card.
        # If CI is not green, we skip the executor entirely so the card stays
        # blocked — the next cron tick will re-evaluate and merge when CI
        # turns green. Without this gate, the card would be completed and
        # disappear from list_blocked, making the deferred merge impossible.
        if (
            action == APPROVE_ADVANCE
            and assignee == "documentation-daedalus"
            and auto_merge
            and pr is not None
            and provider is not None
        ):
            ci_status_for_merge = ci_cache.get(pr, CIStatus.UNKNOWN)
            provider_supports_ci = getattr(provider, "supports_ci_status", False)
            if provider_supports_ci and ci_status_for_merge != CIStatus.GREEN:
                logger.info(
                    "iterate: deferring docs card %s — CI not green for PR #%s (status: %s). "
                    "Card stays blocked; next tick will retry when CI passes.",
                    tid, pr, ci_status_for_merge,
                )
                counts[action] = counts.get(action, 0)  # no increment — nothing executed
                continue

        try:
            ok = executor(
                slug, card, repo, handoff,
                workdir=workdir,
                notify_target=notify_target,
                router_profile=router_profile,
                dry_run=dry_run,
                pr_number=pr,
                provider=provider,
                max_fix_attempts=max_fix_attempts,
            )

            # Gate on ok=True: prevents notification when the executor fails
            # (no PR number found, or kanban.create_task returned None/False).
            # Distinguish fix-card creation from escalation so callers can send
            # the right notification for each case.
            if action == QA_FIX and assignee == "qa-daedalus" and ok:
                issue_n = _extract_issue_number_from_card(card)
                entry = {"issue_n": issue_n, "pr": pr, "reason": handoff}
                # Escalation: fix_attempts file counter already at MAX before this
                # tick's increment (executor called _execute_escalate, not create_task).
                # Use file-only counter to avoid a second kanban.list_tasks round-trip.
                _tid = card.get("id", "")
                _file_count = _read_fix_attempts(workdir).get(_tid, 0) if workdir and _tid else 0
                _escalated = (_file_count >= max_fix_attempts)
                if _escalated:
                    escalated_cards.append(entry)
                else:
                    qa_failed_cards.append(entry)

            if ok:
                counts[action] += 1
                # Track PR number for advance actions so the human summary can
                # report which PRs were advanced (not just a count tuple).
                if action == ADVANCE and pr is not None:
                    advance_prs.append(pr)

                # Auto-merge: when the docs card completes and auto_merge is enabled,
                # the dispatcher merges the PR via the VCS API. This is the ONLY path
                # that can trigger a merge — agents never merge directly.
                if (
                    action == APPROVE_ADVANCE
                    and assignee == "documentation-daedalus"
                    and auto_merge
                    and pr is not None
                    and provider is not None
                ):
                    # Merge now if every gate passes. If not (CI still pending, PR
                    # momentarily un-mergeable), the docs card is already done — but
                    # sweep_deferred_merges() retries on later ticks, so the merge is
                    # no longer one-shot (#1178).
                    issue_n = _extract_issue_number_from_card(card)
                    _try_merge_if_gates_pass(
                        slug, issue_n, pr, provider,
                        merge_method=merge_method, skip_qa=skip_qa,
                        ci_status=ci_cache.get(pr, CIStatus.UNKNOWN), dry_run=dry_run,
                    )
        except Exception as e:
            logger.error("iterate: executor %s failed for card %s: %s", action, tid, e)

    return counts, advance_prs, pending_signal_cards, qa_failed_cards, escalated_cards

