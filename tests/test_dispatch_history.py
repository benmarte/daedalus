"""Unit tests for dispatch-history append/rotation/read/format (issue #235)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import conftest  # noqa: E402
from conftest import check  # noqa: E402,F401

disp = conftest._load_dispatch()


# ── append ──────────────────────────────────────────────────────────────────


def test_append_creates_file_and_record(tmp_path):
    p = tmp_path / "sub" / "history.jsonl"  # parent dirs must be created
    disp._append_history({"mode": "github", "created": [1, 2]},
                         project="acme", path=p, timestamp="2026-06-27T00:00:00+00:00")
    check("file created", p.exists())
    rec = json.loads(p.read_text(encoding="utf-8").strip())
    check("timestamp recorded", rec["timestamp"] == "2026-06-27T00:00:00+00:00")
    check("project recorded", rec["project"] == "acme")
    check("summary fields merged", rec["mode"] == "github" and rec["created"] == [1, 2])


def test_append_adds_utc_timestamp_when_absent(tmp_path):
    p = tmp_path / "history.jsonl"
    disp._append_history({"mode": "github"}, project="acme", path=p)
    rec = json.loads(p.read_text(encoding="utf-8").strip())
    check("timestamp auto-added", isinstance(rec.get("timestamp"), str) and rec["timestamp"])


def test_append_is_one_line_per_tick(tmp_path):
    p = tmp_path / "history.jsonl"
    for i in range(3):
        disp._append_history({"created": [i]}, project="p", path=p, timestamp=f"t{i}")
    lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    check("one line per append", len(lines) == 3)
    check("each line valid json", all(json.loads(ln) for ln in lines))


# ── rotation ──────────────────────────────────────────────────────────────────


def test_rotation_caps_at_max_lines(tmp_path):
    p = tmp_path / "history.jsonl"
    total = disp._HISTORY_MAX_LINES + 50
    for i in range(total):
        disp._append_history({"n": i}, path=p, timestamp=f"t{i}")
    lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    check("capped at max", len(lines) == disp._HISTORY_MAX_LINES)
    first = json.loads(lines[0])
    last = json.loads(lines[-1])
    check("oldest rotated out", first["n"] == total - disp._HISTORY_MAX_LINES)
    check("newest retained", last["n"] == total - 1)


def test_append_never_raises_on_bad_path(tmp_path):
    # A path whose parent is an existing FILE cannot be mkdir'd → must not raise.
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    p = blocker / "history.jsonl"
    disp._append_history({"mode": "github"}, path=p)  # swallowed + logged
    check("no file created under file-parent", not p.exists())


# ── read ──────────────────────────────────────────────────────────────────────


def test_read_missing_file_returns_empty(tmp_path):
    check("missing → []", disp._read_history(10, path=tmp_path / "nope.jsonl") == [])


def test_read_returns_last_n_oldest_first(tmp_path):
    p = tmp_path / "history.jsonl"
    for i in range(5):
        disp._append_history({"n": i}, path=p, timestamp=f"t{i}")
    recs = disp._read_history(3, path=p)
    check("returns n entries", len(recs) == 3)
    check("oldest→newest order", [r["n"] for r in recs] == [2, 3, 4])


def test_read_skips_corrupt_lines(tmp_path):
    p = tmp_path / "history.jsonl"
    disp._append_history({"n": 1}, path=p, timestamp="t1")
    with p.open("a", encoding="utf-8") as f:
        f.write("not json\n")
    disp._append_history({"n": 2}, path=p, timestamp="t2")
    recs = disp._read_history(10, path=p)
    check("corrupt line skipped", [r["n"] for r in recs] == [1, 2])


# ── format ──────────────────────────────────────────────────────────────────────


def test_format_empty():
    check("empty message", disp._format_history([]) == "No dispatch history yet.")


def test_format_renders_table_with_counts():
    recs = [{"timestamp": "t1", "project": "acme", "mode": "github",
             "created": [1, 2, 3], "completed": [], "error": None}]
    out = disp._format_history(recs)
    check("has header", "TIMESTAMP" in out and "PROJECT" in out)
    check("list rendered as count", "3" in out)  # created → 3
    check("project value shown", "acme" in out)
    check("none rendered empty (no 'None')", "None" not in out)


if __name__ == "__main__":
    import tempfile

    print("dispatch-history tests (issue #235)")
    print("-" * 60)
    for _name, _fn in sorted(
        (n, f) for n, f in globals().items()
        if n.startswith("test_") and callable(f)
    ):
        import inspect
        if inspect.signature(_fn).parameters:
            with tempfile.TemporaryDirectory() as d:
                _fn(Path(d))
        else:
            _fn()
    print("-" * 60)
    print(f"Results: {conftest._passed} passed, {conftest._failed} failed")
    sys.exit(1 if conftest._failed else 0)
