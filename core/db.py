"""Shared SQLite connection helper (issue #1134).

Several sites read from / write to the Hermes kanban board's SQLite database
directly (``kanban.rename_task``, the stale-card sweeper, the dispatcher's
parent-issue lookup, and the advance-hook project resolver). The default
``sqlite3`` journal mode is ``DELETE``, under which a read during a concurrent
write can raise ``SQLITE_BUSY`` — a documented concurrent-dispatcher hazard.

``connect_wal`` centralises the mitigation: every direct connection is opened in
WAL journal mode (readers never block writers and vice versa) with
``synchronous=NORMAL`` (safe with WAL) and a ``busy_timeout`` so the rare
contention that remains waits briefly instead of erroring immediately.
"""
from __future__ import annotations

import sqlite3

# How long a blocked statement waits for a lock before raising SQLITE_BUSY.
_BUSY_TIMEOUT_MS = 5000


def connect_wal(path: str) -> sqlite3.Connection:
    """Open ``path`` and enable WAL journal mode, returning the connection.

    WAL is a persistent property of the database file, so re-applying it on every
    connection is idempotent and cheap. Callers own the returned connection
    (commit/close as before). Do not use on ``:memory:`` databases, which do not
    support WAL.
    """
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    return conn
