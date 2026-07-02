#!/usr/bin/env python3
"""Resolve the daedalus project repo-path for a just-finished kanban worker.

Prints the registry project path to stdout (or nothing if it can't resolve).
Used by daedalus-advance.sh to scope the dispatcher to ONE project so a
hook-triggered sweep can't leak another project's cards onto the wrong board.

Resolution order:
  1. task id from the hook payload (argv[1] JSON) — tries common keys
  2. fallback: walk parent processes for `kanban task t_XXXX`
Then: task id -> which board DB contains it -> board slug ->
      registry project whose daedalus.yaml `repo:` slugifies to that slug.
"""
import os, sys, re, glob, json, subprocess

# Make the plugin root importable so this standalone hook script can share the
# WAL connection helper with the rest of the codebase (issue #1134). This script
# is deployed to ~/.hermes/agent-hooks/ (whose parent has no ``core/``), so also
# try the installed plugin root and fall back to a plain sqlite3 connection if
# neither imports — a crash here made the on_session_end advance hook resolve
# nothing, silently stalling every pipeline handoff (issue #1202).
for _p in (
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    os.path.expanduser("~/.hermes/plugins/daedalus"),
):
    if _p not in sys.path:
        sys.path.insert(0, _p)
try:
    from core.db import connect_wal  # noqa: E402
except Exception:  # pragma: no cover - deployed-location import fallback
    import sqlite3

    def connect_wal(path):
        conn = sqlite3.connect(path, timeout=5)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
        return conn

HERMES = os.path.expanduser("~/.hermes")


def board_slug(repo: str) -> str:
    slug = repo.replace("/", "-") if repo else ""
    return re.sub(r"[^a-zA-Z0-9_-]", "-", slug).strip("-").lower()


def task_from_payload() -> str:
    if len(sys.argv) < 2:
        return ""
    try:
        d = json.loads(sys.argv[1])
    except Exception:
        return ""
    candidates = []
    if isinstance(d, dict):
        candidates.append(d)
        ex = d.get("extra")
        if isinstance(ex, dict):
            candidates.append(ex)
    for src in candidates:
        for k in ("task_id", "taskId", "task", "kanban_task", "id"):
            v = src.get(k)
            if isinstance(v, str) and v.startswith("t_"):
                return v
    return ""


def task_from_proctree() -> str:
    pid = os.getppid()
    for _ in range(10):
        if pid <= 1:
            break
        try:
            out = subprocess.run(
                ["ps", "-o", "ppid=,command=", "-p", str(pid)],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
        except Exception:
            break
        if not out:
            break
        m = re.search(r"kanban\s+task\s+(t_[A-Za-z0-9]+)", out)
        if m:
            return m.group(1)
        mp = re.match(r"\s*(\d+)\s", out)
        pid = int(mp.group(1)) if mp else 1
    return ""


def board_for_task(task_id: str) -> str:
    for db in glob.glob(os.path.join(HERMES, "kanban", "boards", "*", "kanban.db")):
        try:
            c = connect_wal(db)
            hit = c.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone()
            c.close()
            if hit:
                return os.path.basename(os.path.dirname(db))
        except Exception:
            continue
    return ""


def project_for_board(slug: str) -> str:
    reg = os.path.join(HERMES, "daedalus", "projects")
    try:
        paths = [l.strip() for l in open(reg) if l.strip()]
    except Exception:
        return ""
    for p in paths:
        cfg = os.path.join(p, ".hermes", "daedalus.yaml")
        try:
            repo = ""
            for line in open(cfg):
                m = re.match(r"\s*repo:\s*(.+?)\s*$", line)
                if m:
                    repo = m.group(1).strip().strip('"').strip("'")
                    break
            if repo and board_slug(repo) == slug:
                return p
        except Exception:
            continue
    return ""


def cwd_from_payload() -> str:
    """Registry project whose path matches the finishing worker's ``cwd``.

    The on_session_end payload carries ``cwd``; matching it against the registry
    scopes the sweep to exactly the worker's own project WITHOUT the fragile
    task-id -> board -> slug chain, which fails when the payload carries only a
    session id (not a ``t_`` kanban id) and the hook runs detached from the
    worker process. Still project-scoped (never a global sweep), so it cannot
    reintroduce the cross-project card leak the task-scoping guards against.
    """
    if len(sys.argv) < 2:
        return ""
    try:
        d = json.loads(sys.argv[1])
    except Exception:
        return ""
    cwd = (d.get("cwd") or "").rstrip("/") if isinstance(d, dict) else ""
    if not cwd:
        return ""
    reg = os.path.join(HERMES, "daedalus", "projects")
    try:
        paths = [l.strip() for l in open(reg) if l.strip()]
    except Exception:
        return ""
    for p in paths:
        if cwd == p.rstrip("/"):
            return p
    return ""


def main():
    # cwd-first: robust to the session-id payload shape and detached hook exec
    # (issue #1202). Falls back to the task-id -> board -> slug chain.
    proj = cwd_from_payload()
    if proj:
        print(proj)
        return
    tid = task_from_payload() or task_from_proctree()
    if not tid:
        return
    slug = board_for_task(tid)
    if not slug:
        return
    proj = project_for_board(slug)
    if proj:
        print(proj)


if __name__ == "__main__":
    main()
