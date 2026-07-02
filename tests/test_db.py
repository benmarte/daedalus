"""Tests for core/db.py — the shared WAL connection helper (issue #1134).

Verifies that connect_wal() returns a live connection with WAL journal mode and
synchronous=NORMAL active, and that all four direct sqlite3.connect sites have
been routed through the helper (no bare sqlite3.connect remains in core/ and
scripts/).

Runs under pytest and as a standalone script.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# Ensure project root is on sys.path BEFORE importing core.db
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.path.insert(0, str(Path(__file__).resolve().parent))

import conftest  # noqa: E402
from conftest import check  # noqa: E402,F401
from core.db import connect_wal  # noqa: E402

_ROOT = Path(__file__).resolve().parent.parent


def test_connect_wal_enables_wal(tmp_path):
    """A file-backed DB opened via the helper reports journal_mode == 'wal'."""
    db_path = str(Path(tmp_path) / "wal_helper_test.db")
    conn = connect_wal(db_path)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        check("journal_mode is wal", mode.lower() == "wal")
    finally:
        conn.close()


def test_connect_wal_synchronous_normal(tmp_path):
    """synchronous is set to NORMAL (1)."""
    db_path = str(Path(tmp_path) / "wal_helper_sync.db")
    conn = connect_wal(db_path)
    try:
        sync = conn.execute("PRAGMA synchronous").fetchone()[0]
        check("synchronous is NORMAL (1)", int(sync) == 1)
    finally:
        conn.close()


def test_connect_wal_returns_usable_connection(tmp_path):
    """The returned connection can run ordinary read/write statements."""
    db_path = str(Path(tmp_path) / "wal_helper_usable.db")
    conn = connect_wal(db_path)
    try:
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.execute("INSERT INTO t (id) VALUES (?)", (7,))
        conn.commit()
        row = conn.execute("SELECT id FROM t").fetchone()
        check("round-trips a row", row is not None and row[0] == 7)
    finally:
        conn.close()


def test_no_bare_sqlite3_connect_in_source():
    """All direct connection sites go through the helper — none open bare."""
    offenders = []
    for sub in ("core", "scripts"):
        for py in (_ROOT / sub).rglob("*.py"):
            if py.name == "db.py":
                continue
            text = py.read_text()
            if re.search(r"sqlite3\.connect\(", text):
                offenders.append(str(py.relative_to(_ROOT)))
    check(f"no bare sqlite3.connect in core/ or scripts/ ({offenders})", not offenders)


if __name__ == "__main__":
    import tempfile

    tests = [
        test_connect_wal_enables_wal,
        test_connect_wal_synchronous_normal,
        test_connect_wal_returns_usable_connection,
        test_no_bare_sqlite3_connect_in_source,
    ]
    for t in tests:
        print(f"\n--- {t.__name__} ---")
        try:
            with tempfile.TemporaryDirectory() as d:
                if t.__code__.co_argcount:
                    t(Path(d))
                else:
                    t()
        except Exception as e:
            conftest._failed += 1
            print(f"  FAIL  (raised {type(e).__name__}: {e})")

    print(f"\n{'=' * 60}")
    print(f"Results: {conftest._passed} passed, {conftest._failed} failed")
    if conftest._failed:
        sys.exit(1)
