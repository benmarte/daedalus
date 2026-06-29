"""File-reference extraction and task-overlap detection utility.

Extracts explicit file references from task descriptions/bodies and computes
a similarity score between two task descriptions to detect when they are
likely to touch the same file(s).  The planner uses this to decide whether to
create a blocking dependency chain between sibling sub-tasks instead of
running them in parallel.

Usage::

    from core.file_overlap import extract_file_refs, detect_file_overlap

    refs = extract_file_refs(task_body)
    result = detect_file_overlap(task_a, task_b)
    if result["overlaps"]:
        # create blocking chain ...

This module is import-free from third-party packages, matching the convention
of ``core.util`` — it is usable by both the dashboard and the dispatch script.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Set, Union

# ─────────────────────────────────────────────────────────────────────────────
# Constants / thresholds
# ─────────────────────────────────────────────────────────────────────────────

# Minimum keyword similarity (overlap coefficient) for keyword-based overlap
# without file refs.  Overlap coefficient = |A∩B| / min(|A|,|B|) — measures how
# much of the smaller task's vocabulary is shared.
KEYWORD_HIGH_THRESHOLD: float = 0.4

# Confidence assigned when one or more file references match exactly.
FILE_REF_THRESHOLD: float = 0.8

# File extensions we recognise as file-path indicators when scanning prose.
_FILE_EXTENSIONS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".rs", ".go", ".java", ".rb",
    ".cpp", ".c", ".h", ".hpp", ".cs", ".php", ".swift", ".kt", ".scala",
    ".sh", ".bash", ".zsh", ".fish", ".yml", ".yaml", ".toml", ".json",
    ".xml", ".html", ".css", ".scss", ".sass", ".less", ".vue", ".svelte",
    ".md", ".rst", ".txt", ".sql", ".proto", ".graphql", ".gql", ".env",
    ".cfg", ".ini", ".conf", ".dockerfile", ".makefile", ".lock",
}

# Stop-words excluded from keyword tokens — common glue words only.
# Domain-relevant verbs (fix, update, implement, etc.) are intentionally NOT
# stop-words because they carry signal about what a task touches.
_STOP_WORDS: frozenset[str] = frozenset({
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "up", "about", "into", "through", "during",
    "before", "after", "above", "below", "between", "is", "are", "was",
    "were", "be", "been", "being", "have", "has", "had", "do", "does",
    "did", "will", "would", "should", "could", "may", "might", "must",
    "shall", "can", "need", "this", "that", "these", "those", "it", "its",
    "they", "them", "their", "there", "here", "where", "when", "how",
    "what", "which", "who", "whom", "if", "then", "else", "not", "no",
    "so", "than", "too", "very", "just", "also", "as", "such", "all",
    "any", "each", "few", "more", "most", "other", "some", "only", "own",
    "same", "new", "now", "one", "two",
    "task", "issue", "code", "module",
    "ensure", "handle", "check", "verify", "return",
    "via", "per", "etc", "see", "like",
})

# ─────────────────────────────────────────────────────────────────────────────
# Regex patterns
# ─────────────────────────────────────────────────────────────────────────────

# Backtick-quoted or inline-code span: `...`  (captures the inner text)
_BACKTICK_RE = re.compile(r"`([^`]+)`")

# Bare file path in prose: a path-like token with a recognised extension.
# Matches paths with at least one ``/`` and a known extension, or absolute paths.
_BARE_PATH_RE = re.compile(
    r"(?:^|\s|[,()])((?:/[A-Za-z0-9_.\-]+)+|[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)+\.[A-Za-z0-9]+)"
)

# Kebab/snake/slash boundary splitter
_KEBAB_SNAKE_RE = re.compile(r"[-_/]+")


# ─────────────────────────────────────────────────────────────────────────────
# File-reference extraction
# ─────────────────────────────────────────────────────────────────────────────

def _is_file_path(token: str) -> bool:
    """Return True if *token* looks like a file path (has a known extension)."""
    lower = token.lower()
    return any(lower.endswith(ext) for ext in _FILE_EXTENSIONS)


def extract_file_refs(task_body: Optional[str]) -> List[str]:
    """Extract explicit file references from a task body.

    Scans for:
    - Backtick-quoted or inline-code spans containing file paths.
    - Bare file paths in prose (relative paths with ``/`` and an extension,
      or absolute paths with an extension).

    Returns a de-duplicated list preserving first-occurrence order.
    """
    if not task_body:
        return []

    text = str(task_body)
    seen: Set[str] = set()
    refs: List[str] = []

    # 1. Backtick-quoted / code-span paths
    for m in _BACKTICK_RE.finditer(text):
        inner = m.group(1).strip()
        if _is_file_path(inner):
            if inner not in seen:
                seen.add(inner)
                refs.append(inner)

    # 2. Bare paths in prose (not already captured by backticks)
    for m in _BARE_PATH_RE.finditer(text):
        path = m.group(1).strip().rstrip(".,;:!?")
        if _is_file_path(path) and path not in seen:
            seen.add(path)
            refs.append(path)

    return refs


# ─────────────────────────────────────────────────────────────────────────────
# Keyword tokenization & normalization
# ─────────────────────────────────────────────────────────────────────────────

def _split_camel_case(word: str) -> List[str]:
    """Split a CamelCase or PascalCase word into lower-case parts.

    ``FileOverlapDetection`` → ``["file", "overlap", "detection"]``
    ``dispatchState`` → ``["dispatch", "state"]``
    """
    # Insert a space before each capital that follows a lowercase letter or digit
    s1 = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", word)
    # Insert a space before each capital that starts a new word when preceded
    # by another capital followed by lowercase: "IOError" → "IO Error"
    s2 = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", s1)
    return [w.lower() for w in s2.split() if w]


def _normalize_keyword(word: str) -> str:
    """Normalize a keyword using a lightweight suffix-stripping approach.

    Strips common English suffixes so that ``running`` and ``run`` map to the
    same token.  Not a full Porter stemmer, but sufficient for task-overlap
    heuristics.
    """
    if not word:
        return ""
    w = word.lower()
    # Order matters: longest/most-specific suffixes first.
    # Note: we deliberately do NOT strip "tion" (detection → detec is useless)
    # or "ion" — those are too aggressive and produce non-recognisable stems.
    for suffix in (
        "izations", "ization", "ations", "ation",
        "ements", "ement", "ments", "ment",
        "tions", "ings", "ing",
        "ied", "ies",
        "ers", "er",
        "ed",
        "es",
        "s",
        "ly",
    ):
        if w.endswith(suffix) and len(w) > len(suffix) + 2:
            w = w[: -len(suffix)]
            break
    # Handle doubled trailing consonant from "-ing" stripping: "running" → "runn" → "run"
    if len(w) >= 4 and w[-1] == w[-2] and w[-1] not in "aeiou":
        w = w[:-1]
    return w


def _tokenize(text: Optional[str]) -> List[str]:
    """Tokenize text into a list of normalized keywords.

    Splits on whitespace and punctuation, splits camelCase, removes stop-words,
    and applies suffix-stripping normalization.
    """
    if not text:
        return []

    # Extract word-like tokens (letters, digits, underscores, hyphens, slashes)
    raw_tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9_\-/]*", str(text))

    tokens: List[str] = []
    for raw in raw_tokens:
        # Split on kebab/snake/slash boundaries
        parts = _KEBAB_SNAKE_RE.split(raw)
        for part in parts:
            if not part:
                continue
            # Split camelCase
            for sub in _split_camel_case(part):
                if len(sub) < 3:
                    continue
                if sub in _STOP_WORDS:
                    continue
                normalized = _normalize_keyword(sub)
                if normalized and len(normalized) >= 3 and normalized not in _STOP_WORDS:
                    tokens.append(normalized)
    return tokens


# ─────────────────────────────────────────────────────────────────────────────
# Overlap detection
# ─────────────────────────────────────────────────────────────────────────────

def _get_text(task: Union[str, Dict]) -> str:
    """Extract the combined title+body text from a task dict or string."""
    if isinstance(task, str):
        return task
    parts: List[str] = []
    title = task.get("title")
    if title:
        parts.append(str(title))
    body = task.get("body")
    if body:
        parts.append(str(body))
    return " ".join(parts)


def _paths_overlap(path_a: str, path_b: str) -> bool:
    """Check if two file paths overlap (one is a suffix of the other).

    ``src/foo/bar.ts`` overlaps with ``foo/bar.ts`` because the shorter
    path is a suffix of the longer one.
    """
    if path_a == path_b:
        return True
    # Normalise: strip leading slashes for comparison
    a = path_a.lstrip("/")
    b = path_b.lstrip("/")
    if a == b:
        return True
    # Check if the shorter is a suffix of the longer (component-wise)
    parts_a = a.split("/")
    parts_b = b.split("/")
    if len(parts_a) > len(parts_b):
        return parts_a[-len(parts_b):] == parts_b
    else:
        return parts_b[-len(parts_a):] == parts_a


def _overlap_coefficient(set_a: Set[str], set_b: Set[str]) -> float:
    """Compute the overlap coefficient between two sets: |A∩B| / min(|A|,|B|).

    This is more suitable than Jaccard for comparing a short task description
    against a longer one — it measures what fraction of the *smaller* set's
    vocabulary is shared, which better reflects whether two tasks talk about
    the same things.
    """
    if not set_a or not set_b:
        return 0.0
    intersection = set_a & set_b
    return len(intersection) / min(len(set_a), len(set_b))


def detect_file_overlap(
    task_a: Union[str, Dict],
    task_b: Union[str, Dict],
) -> Dict:
    """Detect whether two tasks are likely to touch the same file(s).

    Parameters
    ----------
    task_a, task_b
        Either a plain string (treated as the task body) or a dict with
        optional ``title`` and ``body`` keys.

    Returns
    -------
    dict
        ``{"overlaps": bool, "confidence": float,
           "matched_files": list[str], "matched_keywords": list[str]}``

    The confidence is:
    - 1.0 when both file refs and keywords fully match.
    - ``FILE_REF_THRESHOLD`` (0.8) when file refs match but keywords don't.
    - The Jaccard keyword similarity when no file refs match (but above
      ``KEYWORD_HIGH_THRESHOLD`` for overlap).
    - 0.0 when nothing matches.
    """
    text_a = _get_text(task_a)
    text_b = _get_text(task_b)

    # ── File-reference overlap ──────────────────────────────────────────────
    refs_a = extract_file_refs(text_a)
    refs_b = extract_file_refs(text_b)
    matched_files: List[str] = []

    for ra in refs_a:
        for rb in refs_b:
            if _paths_overlap(ra, rb):
                if ra not in matched_files:
                    matched_files.append(ra)

    # ── Keyword similarity ───────────────────────────────────────────────────
    tokens_a = set(_tokenize(text_a))
    tokens_b = set(_tokenize(text_b))
    shared = tokens_a & tokens_b
    keyword_sim = _overlap_coefficient(tokens_a, tokens_b)
    matched_keywords = sorted(shared)

    # ── Compute result ──────────────────────────────────────────────────────
    has_file_overlap = len(matched_files) > 0
    has_keyword_overlap = keyword_sim >= KEYWORD_HIGH_THRESHOLD

    if has_file_overlap and has_keyword_overlap:
        confidence = 1.0
    elif has_file_overlap:
        confidence = FILE_REF_THRESHOLD
    elif has_keyword_overlap:
        confidence = keyword_sim
    else:
        confidence = 0.0

    overlaps = has_file_overlap or has_keyword_overlap

    return {
        "overlaps": overlaps,
        "confidence": round(confidence, 4),
        "matched_files": matched_files,
        "matched_keywords": matched_keywords if overlaps else [],
    }