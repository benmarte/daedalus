"""Tests for scripts/append_changelog.py (issue #1390).

Tests the idempotent append logic with shared format helper integration.
TDD: these tests define the contract before implementation.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load():
    path = ROOT / "scripts" / "append_changelog.py"
    spec = importlib.util.spec_from_file_location("append_changelog", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ac = _load()

REPO = "https://github.com/benmarte/daedalus"
PR_URL = f"{REPO}/pull/42"
ISSUE_URL = f"{REPO}/issues/100"


# ── idempotency ─────────────────────────────────────────────────────────────


def test_idempotent_same_pr_twice_writes_once(tmp_path):
    """Calling append_changelog twice with same PR# produces one entry."""
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text("", encoding="utf-8")
    
    r1 = ac.append_changelog(cl, title="fix: bug", pr_number=42, pr_url=PR_URL)
    r2 = ac.append_changelog(cl, title="fix: bug", pr_number=42, pr_url=PR_URL)
    
    assert r1 == True  # first write
    assert r2 == False  # second skipped
    assert cl.read_text().count("PR #42") == 1


def test_different_pr_both_written(tmp_path):
    """Different PR numbers both get written."""
    cl = tmp_path / "CHANGELOG.md"
    
    ac.append_changelog(cl, title="feat: a", pr_number=42, pr_url=PR_URL)
    ac.append_changelog(cl, title="feat: b", pr_number=43, pr_url=f"{REPO}/pull/43")
    
    text = cl.read_text()
    assert "PR #42" in text
    assert "PR #43" in text
    assert text.count("## [") == 2


# ── prepend order (newest first) ────────────────────────────────────────────


def test_newest_first_order(tmp_path):
    """New entry prepended before old entries."""
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text("## [old]({REPO}/issues/1) — [PR #1]({REPO}/pull/1)\n\n", encoding="utf-8")
    
    ac.append_changelog(cl, title="new", pr_number=2, pr_url=f"{REPO}/pull/2")
    
    text = cl.read_text()
    pos_new = text.find("PR #2")
    pos_old = text.find("PR #1")
    assert pos_new < pos_old


# ── URL derivation via shared helper ────────────────────────────────────────


def test_issue_number_derives_url(tmp_path):
    """issue_number parameter derives issue_url via shared helper."""
    cl = tmp_path / "CHANGELOG.md"
    
    ac.append_changelog(cl, title="fix: x", pr_number=42, pr_url=PR_URL, issue_number=100)
    
    text = cl.read_text()
    assert "/issues/100" in text
    assert "/pull/42" in text


def test_explicit_issue_url_overrides(tmp_path):
    """Explicit issue_url takes precedence."""
    cl = tmp_path / "CHANGELOG.md"
    custom_url = "https://custom.com/issue/999"
    
    ac.append_changelog(cl, title="fix: x", pr_number=42, pr_url=PR_URL, issue_url=custom_url)
    
    assert custom_url in cl.read_text()


# ── file handling ───────────────────────────────────────────────────────────


def test_creates_file_if_missing(tmp_path):
    """Creates CHANGELOG.md if it doesn't exist."""
    cl = tmp_path / "CHANGELOG.md"
    assert not cl.exists()
    
    ac.append_changelog(cl, title="fix: x", pr_number=1, pr_url=PR_URL)
    
    assert cl.exists()
    assert "PR #1" in cl.read_text()


def test_prepends_to_existing_content(tmp_path):
    """Existing content preserved after prepend."""
    cl = tmp_path / "CHANGELOG.md"
    original = "## [existing](u) — [PR #99](u)\n\n"
    cl.write_text(original, encoding="utf-8")
    
    ac.append_changelog(cl, title="new", pr_number=1, pr_url=PR_URL)
    
    text = cl.read_text()
    assert original in text
    assert text.startswith("## [new]")


# ── entry_present helper (word boundary) ───────────────────────────────────


def test_entry_present_word_boundary():
    """PR #12 should not match PR #123."""
    content = "## [x](u) — [PR #123](u)\n\n"
    
    assert ac._entry_present(content, 123) == True
    assert ac._entry_present(content, 12) == False
    assert ac._entry_present(content, 1) == False


# ── CLI interface ───────────────────────────────────────────────────────────


def test_cli_writes_to_file(tmp_path, capsys):
    """CLI writes entry via command-line args."""
    cl = tmp_path / "CHANGELOG.md"
    
    rc = ac.main([
        "--file", str(cl),
        "--title", "feat: cli test",
        "--pr-number", "55",
        "--pr-url", PR_URL,
        "--issue-number", "10",
    ])
    
    assert rc == 0
    text = cl.read_text()
    assert "PR #55" in text
    assert "/issues/10" in text
    out = capsys.readouterr().out
    assert "55" in out


def test_cli_rejects_duplicate(tmp_path, capsys):
    """CLI rejects duplicate PR# (idempotent)."""
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text("## [x](u) — [PR #55](u)\n\n", encoding="utf-8")
    
    rc = ac.main([
        "--file", str(cl),
        "--title", "skip me",
        "--pr-number", "55",
        "--pr-url", PR_URL,
    ])
    
    assert rc == 0  # still success, just skipped
    out = capsys.readouterr().out
    assert "skipped" in out.lower() or "already exists" in out.lower()


def test_cli_missing_required_args(capsys):
    """CLI exits non-zero when required args missing."""
    rc = ac.main(["--file", "/tmp/x.md"])
    
    assert rc != 0  # missing --title, --pr-number, --pr-url


def test_cli_reads_from_stdin(tmp_path, monkeypatch, capsys):
    """CLI can read pre-formatted entry from stdin."""
    cl = tmp_path / "CHANGELOG.md"
    entry = "## [manual](https://x) — [PR #77](https://y)\n"
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(entry))
    
    rc = ac.main(["--file", str(cl), "--stdin", "--pr-number", "77"])
    
    assert rc == 0
    assert "PR #77" in cl.read_text()


# ── format_entry helper (delegates to shared) ──────────────────────────────


def test_format_entry_uses_shared_helper():
    """format_entry() delegates to shared format_changelog_entry()."""
    result = ac.format_entry("fix: x", 42, pr_url=PR_URL, issue_number=10)
    
    assert "## [fix: x]" in result
    assert "/issues/10" in result
    assert "/pull/42" in result
    assert "—" in result


# ── standalone runner ───────────────────────────────────────────────────────

if __name__ == "__main__":
    import conftest
    from conftest import check
    
    with tempfile.TemporaryDirectory() as d:
        cl = Path(d) / "CHANGELOG.md"
        cl.write_text("", encoding="utf-8")
        
        # idempotency
        r1 = ac.append_changelog(cl, title="fix: bug", pr_number=42, pr_url=PR_URL)
        r2 = ac.append_changelog(cl, title="fix: bug", pr_number=42, pr_url=PR_URL)
        check("idempotent", r1 == True and r2 == False and cl.read_text().count("PR #42") == 1)
        
        # word boundary
        check("word boundary", ac._entry_present("PR #123", 12) == False)
        
        # file creation
        cl2 = Path(d) / "NEW.md"
        ac.append_changelog(cl2, title="t", pr_number=1, pr_url=PR_URL)
        check("creates file", cl2.exists() and "PR #1" in cl2.read_text())
    
    print(f"\n{conftest._passed} passed, {conftest._failed} failed")
    sys.exit(1 if conftest._failed else 0)
