"""Deterministic Hermes-kanban tracking for the daedalus.

A thin, idempotent wrapper over the ``hermes kanban`` CLI: ensure a board exists,
list its tasks, and create exactly one task per issue (keyed by ``#<n>`` in the
title) so re-runs never duplicate. Hermes Kanban's own dispatch/worker then
executes each task and tracks status/runs/heartbeat/comments on the board
automatically — that is what makes tracking deterministic and live.

Every call degrades gracefully (logs + returns falsy) so tracking never breaks a
run.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path

from core.db import connect_wal

logger = logging.getLogger("daedalus.kanban")


def _guard_test_isolation() -> None:
    """Refuse to touch the real ``~/.hermes`` kanban board while under pytest.

    Defense-in-depth for issue #1209. If a test path reaches ``_hk`` without an
    isolated ``HERMES_HOME`` — the env override failed to propagate, as seen in
    pipeline QA/PM workers — running the real ``hermes kanban`` CLI would write
    cards to the LIVE board and trigger a runaway. This converts that silent leak
    into a loud failure. No-op in production: the ``PYTEST_CURRENT_TEST`` sentinel
    is only set by pytest, so real runs never enter this branch.
    """
    if not os.environ.get("PYTEST_CURRENT_TEST"):
        return
    home = os.environ.get("HERMES_HOME")
    real = (Path.home() / ".hermes").resolve()
    if not home or Path(home).expanduser().resolve() == real:
        raise RuntimeError(
            "core.kanban._hk refused to spawn the real 'hermes kanban' CLI during "
            f"a test: HERMES_HOME={home!r} points at the live board ({real}). "
            "Tests must stub core.kanban._hk (the autouse conftest fixture does "
            "this by default) or set an isolated tmp HERMES_HOME. See issue #1209."
        )


def _hk(args: list[str], timeout: int = 60) -> tuple[int, str, str]:
    """Run ``hermes kanban <args>``; return (rc, stdout, stderr). Patched in tests."""
    _guard_test_isolation()
    try:
        r = subprocess.run(["hermes", "kanban"] + args, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except Exception as e:
        return 1, "", str(e)


# ── per-tick list_tasks cache (issue #1142) ──────────────────────────────────
# The dispatcher calls list_tasks() at ~30 sites per tick, each spawning a
# hermes subprocess to read identical board state. This is an OPT-IN,
# process-local read cache keyed by (slug, status): disabled by default (so
# library callers and tests see unchanged behavior), enabled by the dispatcher
# for the duration of a single tick via a try/finally in run().
#
# ``None`` means disabled; a dict means enabled. It is invalidated (fully
# cleared) after every board mutation so a read never returns stale state within
# a tick. Non-persistent and NOT thread-safe — the dispatcher is single-process,
# single-threaded per tick (serialized by the main() FileLock).
_TICK_CACHE: dict | None = None


def enable_tick_cache() -> None:
    """Enable (and reset) the per-tick list_tasks cache. Idempotent."""
    global _TICK_CACHE
    _TICK_CACHE = {}


def reset_tick_cache() -> None:
    """Clear cached list_tasks results without disabling the cache. No-op if disabled."""
    if _TICK_CACHE is not None:
        _TICK_CACHE.clear()


def disable_tick_cache() -> None:
    """Disable the per-tick cache — subsequent list_tasks always hit the subprocess."""
    global _TICK_CACHE
    _TICK_CACHE = None


def _invalidate_tick_cache() -> None:
    """Drop all cached list_tasks results after a mutation. No-op if disabled."""
    if _TICK_CACHE is not None:
        _TICK_CACHE.clear()


def ensure_board(slug: str) -> bool:
    """Create the board if missing (idempotent). True if usable.

    In Hermes v0.16.0+ ``hermes kanban --board <slug> init`` REQUIRES the
    board to already exist, so it NEVER creates one.  The correct command is
    ``hermes kanban boards create <slug>``, which creates the board if absent
    and succeeds (exit 0) when it already exists.
    """
    rc, out, err = _hk(["boards", "create", slug])
    if rc == 0:
        return True
    # Some versions may return non-zero with an "already exists" message
    # (e.g. stderr).  Treat that as success — the board is usable.
    combined = ((out or "") + (err or "")).lower()
    if "already exists" in combined or "already exist" in combined:
        return True
    logger.warning("kanban: could not ensure board '%s': %s", slug, (err or "").strip())
    return False


_ISSUE_RE = re.compile(r"#(\d+)")


def list_issue_numbers(slug: str) -> set[int]:
    """Issue numbers that already have a task on the board (parsed from titles).

    Uses the structured JSON list (``hermes kanban list --json``) so that task
    title truncation in the human-readable ``ls`` table never causes an issue
    number — regardless of how many digits — to be missed.
    """
    tasks = list_tasks(slug)
    if not tasks:
        return set()
    nums: set[int] = set()
    for task in tasks:
        title = task.get("title") or ""
        if not title:
            continue
        for m in _ISSUE_RE.finditer(title):
            nums.add(int(m.group(1)))
    return nums


def create_triage(slug: str, issue_number: int | None, title: str, body: str,
                  idempotency_key: str | None = None,
                  workspace: str | None = None,
                  goal: bool = False,
                  goal_max_turns: int | None = None) -> str | None:
    """Create a TRIAGE card for an issue (to be fanned out by decompose()).

    Lands in the triage column rather than assigned to one profile, so the
    decomposer can split it into role sub-tasks (developer / reviewer /
    security-analyst / documentation). Returns the task id or None.

    ``issue_number`` can be None for non-issue cards (spec-file triggers,
    manual triage cards, etc.) — the card title is used as-is without a
    ``#<n>`` prefix.

    ``workspace`` (e.g. ``dir:/path/to/checkout`` or ``worktree:...``) pins where
    work happens. Hermes propagates it to every decomposed child, so a single pin
    here keeps the whole roster in the right checkout — without it, children land
    in a bare ``scratch`` workspace and a worker can wander into the wrong repo.

    ``goal`` spawns the worker in goal mode (multi-turn with adjudication).
    ``goal_max_turns`` sets the turn budget (only meaningful when goal=True).

    NOTE: The native ``decompose`` children do NOT inherit ``--goal`` because
    ``hermes kanban decompose`` does not support goal mode. If you need
    decomposed children in goal mode, create them individually with ``create_task(…, goal=True)``
    instead of using ``decompose()``.
    """
    card_title = f"#{issue_number} {title}" if issue_number is not None else title
    args = ["--board", slug, "create", card_title, "--body", body, "--triage"]
    if idempotency_key:
        args += ["--idempotency-key", idempotency_key]
    if workspace:
        args += ["--workspace", workspace]
    if goal:
        args += ["--goal"]
        if goal_max_turns is not None:
            args += ["--goal-max-turns", str(goal_max_turns)]
    rc, out, err = _hk(args)
    _invalidate_tick_cache()
    if rc != 0:
        logger.warning("kanban: create triage for #%s failed: %s", issue_number, (err or "").strip())
        return None
    m = re.search(r"\bt_[0-9a-f]+\b", out or "")
    tid = m.group(0) if m else None
    logger.info("kanban: created triage %s for #%s on board %s", tid, issue_number, slug)
    return tid


def decompose(slug: str, task_id: str) -> bool:
    """Fan a triage card out into role sub-tasks via the Hermes decomposer.

    The decomposer routes by each profile's description, so dev/review/security/
    documentation work lands on the right agents. Non-deterministic (LLM) — a
    terse triage body fans out broadly; a prescriptive one may stay single.
    """
    rc, out, err = _hk(["--board", slug, "decompose", task_id], timeout=180)
    _invalidate_tick_cache()
    if rc != 0:
        logger.warning("kanban: decompose %s failed: %s", task_id, (err or out or "").strip())
        return False
    logger.info("kanban: decomposed %s", task_id)
    return True


def swarm(
    slug: str,
    goal: str,
    workers: list[str],
    verifier: str,
    synthesizer: str,
    idempotency_key: str = "",
    priority: int | None = None,
    created_by: str = "",
) -> str | None:
    """Create a Kanban Swarm v1 graph (parallel workers → verifier → synthesizer).

    Thin wrapper over ``hermes kanban swarm``. Each ``workers`` entry is a
    ``PROFILE:TITLE[:SKILL,SKILL]`` string passed as a repeated ``--worker``.
    The ``verifier`` runs after all workers complete; the ``synthesizer`` runs
    after the verifier. ``idempotency_key`` dedups the root card so a re-tick
    re-roots zero duplicate swarms.

    Returns the root card id (``t_…``) on success, or ``None`` on failure.
    Never raises — logs a warning and returns ``None`` so the caller can fall
    back to its legacy per-role fan-out rather than stranding the pipeline.
    """
    args = ["--board", slug, "swarm"]
    for w in workers:
        args += ["--worker", w]
    args += ["--verifier", verifier, "--synthesizer", synthesizer]
    if idempotency_key:
        args += ["--idempotency-key", idempotency_key]
    if priority is not None:
        args += ["--priority", str(priority)]
    if created_by:
        args += ["--created-by", created_by]
    # positional goal LAST
    args += [goal]
    rc, out, err = _hk(args, timeout=180)
    _invalidate_tick_cache()
    if rc != 0:
        logger.warning("kanban: swarm '%s' failed: %s", goal, (err or out or "").strip())
        return None
    m = re.search(r"\bt_[0-9a-f]+\b", out or "")
    tid = m.group(0) if m else None
    logger.info("kanban: created swarm %s (goal=%s) on board %s", tid, goal, slug)
    return tid


def link(slug: str, parent_id: str, child_id: str) -> bool:
    """Attach ``child_id`` as a dependency child of ``parent_id`` post-hoc.

    Wraps ``hermes kanban link <parent> <child>``. Used to gate an
    already-created card (e.g. a swarm root) behind a predecessor after the
    fact, so a subsequent ``block_task(kind="dependency")`` auto-promotes it when
    the parent completes. Never raises — logs + returns False on failure.
    """
    rc, out, err = _hk(["--board", slug, "link", parent_id, child_id])
    _invalidate_tick_cache()
    if rc != 0:
        logger.warning("kanban: link %s -> %s failed: %s",
                       parent_id, child_id, (err or out or "").strip())
        return False
    logger.info("kanban: linked %s -> %s", parent_id, child_id)
    return True


def list_blocked(slug: str) -> list[dict]:
    """Cards currently in the 'blocked' column (full --json dicts)."""
    rc, out, _ = _hk(["--board", slug, "list", "--status", "blocked", "--json"])
    if rc != 0:
        return []
    try:
        return json.loads(out) or []
    except Exception:
        return []


def get_latest_summary(slug: str, task_id: str) -> str:
    """Return the latest_summary for a task (from show --json), or empty string."""
    rc, out, _ = _hk(["--board", slug, "show", task_id, "--json"])
    if rc != 0 or not out:
        return ""
    try:
        return json.loads(out).get("latest_summary") or ""
    except Exception:
        return ""


def review_handoff_pr(slug: str, task_id: str) -> int | None:
    """If task_id is a 'review-required' handoff, return the PR number it opened.

    The worker records the handoff (with `PR #<n>`) in its run summary/events, so
    we read the full card detail and parse it. Returns None if not a review-required
    handoff or no PR is referenced.
    """
    rc, out, _ = _hk(["--board", slug, "show", task_id, "--json"])
    if rc != 0:
        return None
    blob = out or ""
    if "review-required" not in blob:
        return None
    m = re.search(r"PR #(\d+)", blob)
    return int(m.group(1)) if m else None


def complete(
    slug: str,
    task_id: str,
    summary: str = "",
    metadata: dict | None = None,
) -> bool:
    """Mark a task complete (advances any children that were blocked on it).

    Args:
        slug: Board slug
        task_id: Task ID to complete
        summary: Optional summary message to record with the completion
        metadata: Optional dict of structured facts (a daedalus outcome record)
            stored on the CLOSING RUN via ``hermes kanban complete --metadata``
            (#1288). This is the native transport for completion handoffs — read
            back with :func:`run_outcome`. Blocked handoffs cannot carry metadata
            (``hermes kanban block`` has no ``--metadata``); they keep the
            free-text JSON fallback until #1290. Serialisation never raises: a
            non-serialisable dict is logged and the metadata is dropped rather
            than aborting the completion.
    """
    args = ["--board", slug, "complete", task_id]
    if summary:
        args += ["--summary", summary]
    if metadata:
        try:
            args += ["--metadata", json.dumps(metadata)]
        except (TypeError, ValueError) as exc:
            logger.warning(
                "kanban: complete %s — dropping unserialisable metadata: %s",
                task_id, exc,
            )
    rc, out, err = _hk(args)
    _invalidate_tick_cache()
    if rc != 0:
        logger.warning("kanban: complete %s failed: %s", task_id, (err or out or "").strip())
        return False
    logger.info("kanban: completed %s", task_id)
    return True


def heartbeat(slug: str, task_id: str, note: str = "") -> bool:
    """Emit a liveness heartbeat for a task (``hermes kanban heartbeat``).

    Keeps a long-running card from tripping the sweeper's stale-running
    detection while its worker is still active. Degrades gracefully (logs +
    returns False) — a failed heartbeat must never break a dispatch tick.
    """
    args = ["--board", slug, "heartbeat", task_id]
    if note:
        args += ["--note", note]
    rc, out, err = _hk(args)
    if rc != 0:
        logger.warning("kanban: heartbeat %s failed: %s", task_id, (err or out or "").strip())
        return False
    return True


def run_outcome(slug: str, task_id: str) -> dict | None:
    """Return the structured outcome metadata on a task's closing run, or None.

    Reads ``hermes kanban runs <task_id> --json`` and returns the ``metadata``
    dict of the most recent run that carries a ``daedalus_outcome`` record (the
    native transport written by :func:`complete` when ``metadata_transport`` is
    on — #1288). ``metadata`` is already a parsed dict in the CLI payload, but a
    JSON-string form is tolerated too. Parses defensively; returns None on any
    failure (no runs, no metadata, malformed JSON) — never raises.
    """
    rc, out, _ = _hk(["--board", slug, "runs", task_id, "--json"])
    if rc != 0 or not out:
        return None
    try:
        runs = json.loads(out or "[]")
    except Exception:
        return None
    if not isinstance(runs, list):
        return None
    # Scan newest-first so the closing run wins over earlier attempts.
    for run in reversed(runs):
        if not isinstance(run, dict):
            continue
        meta = run.get("metadata")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                continue
        if isinstance(meta, dict) and "daedalus_outcome" in meta:
            return meta
    return None


def worker_log_tail(slug: str, task_id: str, *, tail_bytes: int = 6000) -> str:
    """Return the tail of a task's worker log, or "" when unavailable.

    Reads ``hermes kanban log <task_id> --tail <bytes>`` (the inner agent's
    captured stdout/stderr under ``<kanban-root>/kanban/logs/``). Used by the
    crash-retry reconciler to capture *why* an inner agent died (#1372). Never
    raises — a missing log / CLI failure yields "".
    """
    rc, out, err = _hk(["--board", slug, "log", task_id, "--tail", str(int(tail_bytes))])
    if rc != 0:
        logger.debug(
            "kanban: worker_log_tail %s unavailable: %s",
            task_id,
            (err or out or "").strip()[:200],
        )
        return ""
    return (out or "").strip()


def edit_summary(slug: str, task_id: str, summary: str) -> bool:
    """Rewrite a task's recorded result summary (``hermes kanban edit --result``).

    Used by the dispatcher to self-heal a done PM card whose summary was lost to
    the hermes premature-completion bug when the spec survives as a GitHub issue
    comment (issue #1161) — the same recovery an operator performs manually.
    """
    args = ["--board", slug, "edit", task_id, "--result", "--summary", summary]
    rc, out, err = _hk(args)
    _invalidate_tick_cache()
    if rc != 0:
        logger.warning("kanban: edit_summary %s failed: %s", task_id, (err or out or "").strip())
        return False
    logger.info("kanban: edited result summary of %s", task_id)
    return True


def decompose_all_triage(slug: str) -> bool:
    """Decompose every card in the board's triage column (kanban-only mode).

    When there's no GitHub Project board, a human creates a triage card directly
    on the board; this fans every such card out across the roster. Idempotent —
    a decomposed card leaves the triage column, so re-running only touches new
    triage cards.
    """
    rc, out, err = _hk(["--board", slug, "decompose", "--all"], timeout=180)
    _invalidate_tick_cache()
    if rc != 0:
        logger.warning("kanban: decompose --all failed: %s", (err or out or "").strip())
        return False
    return True


def dispatch(slug: str, max_spawns: int = 5) -> bool:
    """Spawn workers for ready tasks on the board (Hermes tracks them live)."""
    rc, out, err = _hk(["--board", slug, "dispatch", "--max", str(max_spawns)])
    _invalidate_tick_cache()
    if rc != 0:
        logger.warning("kanban: dispatch failed: %s", (err or out or "").strip())
        return False
    return True


def claim(slug: str, task_id: str, *, ttl: int = 900) -> bool:
    """Claim a task, starting its run (moves it to ``running`` with a run_id) so a
    directly-spawned wrapper (direct_dispatch, #1329) has a run to complete/block.
    Returns False if the claim fails (e.g. already running/claimed)."""
    rc, out, err = _hk(["--board", slug, "claim", str(task_id), "--ttl", str(ttl)])
    _invalidate_tick_cache()
    if rc != 0:
        logger.info("kanban: claim %s failed: %s", task_id, (err or out or "").strip())
        return False
    return True


# ── iterate helpers ─────────────────────────────────────────────────────────


def show_card(slug: str, task_id: str) -> dict | None:
    """Return full card detail as a parsed JSON dict, or None."""
    rc, out, _ = _hk(["--board", slug, "show", task_id, "--json"])
    if rc != 0:
        return None
    try:
        return json.loads(out or "{}")
    except Exception:
        return None


def create_task(
    slug: str,
    title: str,
    *,
    body: str = "",
    assignee: str = "",
    workspace: str = "",
    idempotency_key: str = "",
    parents: list[str] | None = None,
    skills: list[str] | None = None,
    goal: bool = False,
    goal_max_turns: int | None = None,
    max_retries: int | None = None,
    max_runtime: str | None = None,
) -> str | None:
    """Create a regular (non-triage) task. Returns task id or None.

    ``skills`` attaches named Hermes skills to the task so the worker has them
    pre-loaded without needing to call skill_view() themselves.
    ``goal`` spawns the worker in goal mode (multi-turn with adjudication).
    ``goal_max_turns`` sets the turn budget (only meaningful when goal=True).
    ``max_retries`` overrides the default failure-before-block retry cap.
    ``max_runtime`` (e.g. ``"30m"``) sets a native wall-clock cap after which
    Hermes SIGTERM→SIGKILL→requeues the card (native self-bounding, #1289).
    Both are omitted from the CLI args when None, so a caller that does not
    pass them produces byte-identical args to the pre-#1289 behaviour.
    """
    args = ["--board", slug, "create", title]
    if body:
        args += ["--body", body]
    if assignee:
        args += ["--assignee", assignee]
    if workspace:
        args += ["--workspace", workspace]
    if idempotency_key:
        args += ["--idempotency-key", idempotency_key]
    if parents:
        for p in parents:
            args += ["--parent", p]
    if skills:
        for s in skills:
            args += ["--skill", s]
    if max_retries is not None:
        args += ["--max-retries", str(max_retries)]
    if max_runtime is not None:
        args += ["--max-runtime", str(max_runtime)]
    if goal:
        args += ["--goal"]
        if goal_max_turns is not None:
            args += ["--goal-max-turns", str(goal_max_turns)]
    rc, out, err = _hk(args)
    _invalidate_tick_cache()
    if rc != 0:
        logger.warning("kanban: create task failed: %s", (err or "").strip())
        return None
    m = re.search(r"\bt_[0-9a-f]+\b", out or "")
    tid = m.group(0) if m else None
    logger.info("kanban: created task %s (%s)", tid, title)
    return tid


def comment(slug: str, task_id: str, body: str) -> bool:
    """Append a comment to a task. Returns True on success."""
    rc, out, err = _hk(["--board", slug, "comment", task_id, body])
    _invalidate_tick_cache()
    if rc != 0:
        logger.warning("kanban: comment on %s failed: %s", task_id, (err or out or "").strip())
        return False
    return True


def unblock_task(slug: str, task_id: str, reason: str = "") -> bool:
    """Unblock a task, optionally with a reason comment. Returns True on success."""
    args = ["--board", slug, "unblock", task_id]
    if reason:
        args += ["--reason", reason]
    rc, out, err = _hk(args)
    _invalidate_tick_cache()
    if rc != 0:
        logger.warning("kanban: unblock %s failed: %s", task_id, (err or out or "").strip())
        return False
    return True


def block_task(
    slug: str,
    task_id: str,
    reason: str = "",
    *,
    kind: str | None = None,
) -> bool:
    """Block a task. Returns True on success.

    ``kind`` (optional, #1290) tags the block with a native Hermes block category
    so the framework knows how to treat it. Valid kinds: ``dependency`` (auto-
    promotes when ALL parent cards complete — the mechanism behind the upfront
    pipeline DAG), ``needs_input`` (human attention required), ``capability``,
    ``transient`` (flaky/retryable). When ``kind`` is None (the default) no
    ``--kind`` flag is emitted, so every existing caller produces byte-identical
    CLI args to the pre-#1290 behaviour. Never raises — degrades gracefully.
    """
    args = ["--board", slug, "block", task_id]
    if reason:
        args += [reason]  # hermes kanban block uses positional reason, not --reason
    if kind:
        args += ["--kind", kind]
    rc, out, err = _hk(args)
    _invalidate_tick_cache()
    if rc != 0:
        logger.warning("kanban: block %s failed: %s", task_id, (err or out or "").strip())
        return False
    return True


def archive_task(slug: str, task_id: str) -> bool:
    """Archive a task off the active board (``hermes kanban archive``).

    Used by the stale-blocked sweeper to move long-stuck cards out of the active
    columns. Returns True on success; degrades gracefully (logs + returns False).
    """
    rc, out, err = _hk(["--board", slug, "archive", task_id])
    _invalidate_tick_cache()
    if rc != 0:
        logger.warning("kanban: archive %s failed: %s", task_id, (err or out or "").strip())
        return False
    logger.info("kanban: archived %s", task_id)
    return True


def reassign_task(slug: str, task_id: str, profile: str, *, reclaim: bool = False) -> bool:
    """Reassign a task to a different profile. Returns True on success."""
    args = ["--board", slug, "reassign", task_id, profile]
    if reclaim:
        args.append("--reclaim")
    rc, out, err = _hk(args)
    _invalidate_tick_cache()
    if rc != 0:
        logger.warning("kanban: reassign %s → %s failed: %s", task_id, profile, (err or out or "").strip())
        return False
    return True


def rename_task(slug: str, task_id: str, new_title: str) -> bool:
    """Update a task's title via direct SQLite write. Returns True on success.

    There is no ``hermes kanban rename`` CLI command, so this writes directly to
    the board's SQLite database at $HERMES_HOME/kanban/boards/<slug>/kanban.db
    (falls back to ~/.hermes). Honoring HERMES_HOME keeps tests off the real
    board — a hardcoded ~/.hermes let test runs seed the live board and trigger a
    gateway execution loop (2026-07-02 incident).
    """
    _home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    db_path = os.path.join(_home, "kanban", "boards", slug, "kanban.db")
    if not os.path.exists(db_path):
        logger.warning("kanban: rename: DB not found for board %r", slug)
        return False
    try:
        conn = connect_wal(db_path)
        conn.execute("UPDATE tasks SET title = ? WHERE id = ?", (new_title, task_id))
        conn.commit()
        conn.close()
        _invalidate_tick_cache()
        return True
    except Exception as exc:
        logger.warning("kanban: rename %s failed: %s", task_id, exc)
        return False


def _board_db_path(slug: str) -> str:
    """Path to the board's SQLite database (honours HERMES_HOME — see rename_task)."""
    _home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    return os.path.join(_home, "kanban", "boards", slug, "kanban.db")


def get_body(slug: str, task_id: str) -> str | None:
    """Return a task's body via direct SQLite read, or None on any failure.

    There is no ``hermes kanban`` CLI command that prints the raw body, so this
    reads the board database directly (same pattern as ``rename_task``).
    """
    db_path = _board_db_path(slug)
    if not os.path.exists(db_path):
        logger.warning("kanban: get_body: DB not found for board %r", slug)
        return None
    try:
        conn = connect_wal(db_path)
        row = conn.execute(
            "SELECT body FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        conn.close()
        return row[0] if row and row[0] is not None else None
    except Exception as exc:
        logger.warning("kanban: get_body %s failed: %s", task_id, exc)
        return None


def edit_body(slug: str, task_id: str, body: str) -> bool:
    """Rewrite a task's body via direct SQLite write. Returns True on success.

    Used by the provider-failover path (issue #1207) to swap the injected
    ``⚠️ AGENT DELEGATION`` block to the fallback coding agent before the card
    is re-dispatched — ``hermes kanban edit`` only touches result/summary, so
    this writes the board database directly (same pattern as ``rename_task``).
    """
    db_path = _board_db_path(slug)
    if not os.path.exists(db_path):
        logger.warning("kanban: edit_body: DB not found for board %r", slug)
        return False
    try:
        conn = connect_wal(db_path)
        cur = conn.execute("UPDATE tasks SET body = ? WHERE id = ?", (body, task_id))
        conn.commit()
        conn.close()
        _invalidate_tick_cache()
        return cur.rowcount > 0
    except Exception as exc:
        logger.warning("kanban: edit_body %s failed: %s", task_id, exc)
        return False


def diagnostics(slug: str) -> list[dict]:
    """Run ``hermes kanban diagnostics --json`` and return parsed diagnostics.

    Identifies stuck-in-blocked cards and their severity. Degrades gracefully
    if the command is unavailable (returns empty list). The diagnostics CLI is
    a native Hermes Kanban command that scans for stale, blocked, or otherwise
    problematic cards.

    Returns a list of dicts, each with keys like ``task_id``, ``severity``,
    ``message``, ``status``, and ``assignee``.
    """
    rc, out, _ = _hk(["--board", slug, "diagnostics", "--json"])
    if rc != 0:
        return []
    try:
        data = json.loads(out or "[]")
        return data if isinstance(data, list) else []
    except Exception:
        return []


def list_tasks(slug: str, status: str = "") -> list[dict]:
    """List all tasks on a board, optionally filtered by status. Returns parsed JSON list.

    When the per-tick cache is enabled (see ``enable_tick_cache``), the parsed
    result is memoized by ``(slug, status)`` so repeated reads within one tick
    reuse a single subprocess. Only successful reads are cached (a failed/malformed
    read returns ``[]`` and is retried on the next call). Mutations invalidate the
    cache, so cached reads never go stale within a tick.
    """
    if _TICK_CACHE is not None:
        cached = _TICK_CACHE.get((slug, status))
        if cached is not None:
            return cached
    args = ["--board", slug, "list", "--json"]
    if status:
        args += ["--status", status]
    rc, out, _ = _hk(args)
    if rc != 0:
        return []
    try:
        result = json.loads(out or "[]")
    except Exception:
        return []
    if _TICK_CACHE is not None:
        _TICK_CACHE[(slug, status)] = result
    return result


def close_non_blocked_issue_tasks(slug: str, issue_number: int) -> list[str]:
    """Complete pending/in-progress tasks for issue_number, skipping blocked ones.

    Used when the validator has blocked an issue — downstream tasks (developer,
    reviewer, etc.) are completed immediately so they can't be dispatched, but
    the validator's blocked card is left intact so humans can see the reason.
    """
    tasks = list_tasks(slug)
    pattern = f"#{issue_number}"
    completed_ids: list[str] = []
    for t in tasks:
        if pattern not in (t.get("title") or ""):
            continue
        status = (t.get("status") or "").lower()
        if status in ("done", "complete", "completed", "blocked"):
            continue  # leave the blocked validator card; skip already-done tasks
        tid = t.get("id") or t.get("task_id")
        if tid and complete(slug, str(tid)):
            completed_ids.append(str(tid))
    return completed_ids


def close_issue_tasks(slug: str, issue_number: int, *, summary: str = "", dry_run: bool = False) -> list[str]:
    """Complete all non-done kanban tasks that reference #issue_number in their title.
    Uses word-boundary regex matching (#957 does not match #9571/#9570).

    Also walks the task tree to find any blocked children with review-required
    summaries and completes them with the provided summary message.
    Returns the list of task IDs that were completed.
    
    Args:
        slug: Board slug
        issue_number: Issue number to close tasks for
        summary: Optional summary message for completed blocked/review-required children
        dry_run: If True, log what would be completed without acting
    """
    tasks = list_tasks(slug)
    pattern = re.compile(rf"(?<!\d)#{issue_number}(?!\d)")
    completed_ids: list[str] = []
    
    # First pass: complete all non-done tasks whose title or body/handoff references the issue
    for t in tasks:
        title = t.get("title") or ""
        body = t.get("body") or ""
        if not (pattern.search(title) or pattern.search(body)):
            continue
        status = (t.get("status") or "").lower()
        if status in ("done", "complete", "completed", "cancelled"):
            continue
        tid = t.get("id") or t.get("task_id")
        if not tid:
            continue
        if dry_run:
            logger.info("[dry-run] would complete task %s (status: %s)", tid, status)
            completed_ids.append(str(tid))
        elif complete(slug, str(tid)):
            completed_ids.append(str(tid))
    
    # Second pass: walk task trees and complete blocked/review-required children.
    # Memoize show_card results within this call so each card is fetched via at
    # most one subprocess. Previously (#1136) a card was fetched once as a
    # parent-candidate here and again as a child of another matching task —
    # children of an issue's cards typically reference the same #issue, so they
    # matched the loop below AND appeared in a parent's children list, producing
    # the N + N*M subprocess explosion that blocked the dispatch tick.
    if summary:
        card_cache: dict[str, dict | None] = {}

        def _get_card(task_id: str) -> dict | None:
            if task_id not in card_cache:
                card_cache[task_id] = show_card(slug, task_id)
            return card_cache[task_id]

        for t in tasks:
            title = t.get("title") or ""
            body = t.get("body") or ""
            if not (pattern.search(title) or pattern.search(body)):
                continue
            tid = t.get("id") or t.get("task_id")
            if not tid:
                continue
            # Get full card details to find children (cached — see above)
            card = _get_card(str(tid))
            if not card:
                continue
            children = card.get("children") or []
            for child_id in children:
                child_card = _get_card(child_id)
                if not child_card:
                    continue
                child_task = child_card.get("task", child_card)
                child_status = (child_task.get("status") or "").lower()
                if child_status in ("done", "complete", "completed"):
                    continue
                # Complete blocked/review-required children
                if child_status == "blocked":
                    latest_summary = child_card.get("latest_summary") or ""
                    if latest_summary.startswith("review-required:"):
                        if dry_run:
                            logger.info("[dry-run] would complete blocked/review-required child %s with summary: %s", 
                                      child_id, summary)
                            completed_ids.append(child_id)
                        elif complete(slug, child_id, summary=summary):
                            completed_ids.append(child_id)
    
    return completed_ids
