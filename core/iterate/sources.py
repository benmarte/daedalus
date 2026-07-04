"""core.iterate.sources — Epic-context extraction and source-file reading.

This is the third extracted layer of core/iterate (PR 3/3, issue #1154).

Package layout after this PR:

  classify.py   — action constants, _parse_handoff, classify_blocked  (PR 1/3)
  executors.py  — decompose lock, fix-attempt tracking, role-gate helpers,
                  all _execute_* functions, planner helpers, _ACTION_EXECUTORS (PR 2/3)
  sources.py    — Phase 4 epic-context extraction + source-file reading
                  infrastructure (PR 3/3, this file)
  gates.py      — block-loop rescue scan, merge-gate, CI-rerun logic,
                  sweep_deferred_merges (PR 3/3)
  __init__.py   — package root: re-exports all layers + kanban binding,
                  _source_reading_fallback_count, run_iterate

PATCH SEMANTICS
---------------
All functions here are PURE (no kanban, no provider calls). Tests that want
to intercept these functions patch them by string:

    mock.patch("core.iterate.identify_relevant_files")
    mock.patch("core.iterate.load_known_components")
    mock.patch("core.iterate.read_source_files")

Because every function here is re-exported via ``core.iterate.__init__``,
``mock.patch("core.iterate.X")`` replaces the name in ``__init__``'s namespace.
Callers in executors.py access these through ``_pkg().X`` (where _pkg() returns
``sys.modules["core.iterate"]``), so they pick up the patched version. ✓
"""
from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("daedalus.iterate")


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


# ── Epic-context dataclasses ──────────────────────────────────────────────────


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
    deps: list[int] = list(existing_deps or [])  # type: ignore[no-redef]  # earlier defs are in always-returning branches
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
                    rel_s = str(fp.relative_to(workdir_path))
                    if any(seg in rel_s for seg in ("node_modules", ".git", "__pycache__", ".venv", "venv", ".tox")):
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
