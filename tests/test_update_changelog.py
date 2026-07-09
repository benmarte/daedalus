"""Unit tests for ``scripts/update_changelog.py`` (issue #1388).

Covers the four behaviours the sub-issue calls out:
  (a) correct entry format,
  (b) newest-first prepend,
  (c) idempotency (same PR twice → one entry),
  (d) no-op on an already-present PR.

Dual-mode: runs under pytest AND standalone (``python tests/test_update_changelog.py``)
via the shared ``check`` printer, matching the rest of the suite.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_module():
    """Load ``scripts/update_changelog.py`` as a module (it is not a package)."""
    path = ROOT / "scripts" / "update_changelog.py"
    spec = importlib.util.spec_from_file_location("update_changelog", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


uc = _load_module()

PR_URL = "https://github.com/benmarte/daedalus/pull/1234"
ISSUE_URL = "https://github.com/benmarte/daedalus/issues/1388"


# ── (a) format ──────────────────────────────────────────────────────────────
def test_format_delegates_to_shared_helper(tmp_path):
    """update_changelog uses scripts.lib.changelog_format — spot-check output."""
    cl = tmp_path / "CHANGELOG.md"
    uc.update_changelog(
        cl,
        title="fix: something",
        pr_number=1234,
        pr_url=PR_URL,
        entry_url=ISSUE_URL,
    )
    content = cl.read_text()
    assert "## [fix: something](https://github.com/benmarte/daedalus/issues/1388)" in content
    assert "[PR #1234](https://github.com/benmarte/daedalus/pull/1234)" in content


def test_format_entry_defaults_to_pr_url_via_update(tmp_path):
    """When --entry-url is omitted the title link uses the PR URL."""
    cl = tmp_path / "CHANGELOG.md"
    uc.update_changelog(cl, title="feat: x", pr_number=42, pr_url=PR_URL)
    assert cl.read_text() == f"## [feat: x]({PR_URL}) — [PR #42]({PR_URL})\n\n"


# ── (b) newest-first prepend ────────────────────────────────────────────────
def test_prepend_is_newest_first(tmp_path):
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text("## [old](u) — [PR #1]("+PR_URL+")\n\n", encoding="utf-8")
    uc.update_changelog(cl, title="new", pr_number=2, pr_url=PR_URL, entry_url=ISSUE_URL)
    text = cl.read_text()
    assert text.startswith(f"## [new]({ISSUE_URL}) — [PR #2]({PR_URL})")
    # old entry is still present, below the new one
    assert text.index("PR #2") < text.index("PR #1")


def test_prepend_helper_blank_line_separator():
    assert uc.prepend_entry("OLD\n", "NEW") == "NEW\n\nOLD\n"


def test_creates_file_when_absent(tmp_path):
    cl = tmp_path / "CHANGELOG.md"
    assert not cl.exists()
    wrote = uc.update_changelog(cl, title="t", pr_number=7, pr_url=PR_URL)
    assert wrote is True
    assert cl.exists()


# ── (c) idempotency: same PR twice → one entry ──────────────────────────────
def test_same_pr_twice_writes_once(tmp_path):
    cl = tmp_path / "CHANGELOG.md"
    first = uc.update_changelog(cl, title="t", pr_number=99, pr_url=PR_URL)
    second = uc.update_changelog(cl, title="t", pr_number=99, pr_url=PR_URL)
    assert first is True
    assert second is False
    assert cl.read_text().count("PR #99") == 1


# ── (d) no-op on already-present PR ─────────────────────────────────────────
def test_noop_on_existing_entry(tmp_path):
    cl = tmp_path / "CHANGELOG.md"
    original = f"## [x](u) — [PR #1234]({PR_URL})\n\n"
    cl.write_text(original, encoding="utf-8")
    wrote = uc.update_changelog(cl, title="x", pr_number=1234, pr_url=PR_URL)
    assert wrote is False
    assert cl.read_text() == original  # untouched


def test_entry_present_word_boundary():
    """PR #12 must not match PR #123 (word-boundary idempotency)."""
    content = f"## [x](u) — [PR #123]({PR_URL})\n\n"
    assert uc.entry_present(content, 123) is True
    assert uc.entry_present(content, 12) is False


def test_commit_message_carries_skip_ci():
    assert uc.COMMIT_MESSAGE == "docs: update CHANGELOG.md [skip ci]"
    assert "[skip ci]" in uc.COMMIT_MESSAGE


# ── CLI smoke ───────────────────────────────────────────────────────────────
def test_cli_writes_then_skips(tmp_path, capsys):
    cl = tmp_path / "CHANGELOG.md"
    args = ["--title", "t", "--pr-number", "55", "--pr-url", PR_URL, "--file", str(cl)]
    rc1 = uc.main(args)
    out1 = capsys.readouterr().out
    rc2 = uc.main(args)
    out2 = capsys.readouterr().out
    assert rc1 == 0 and rc2 == 0
    assert "wrote entry for PR #55" in out1
    assert "skipped PR #55" in out2
    assert cl.read_text().count("PR #55") == 1


# ── standalone runner ───────────────────────────────────────────────────────
if __name__ == "__main__":
    import tempfile

    import conftest
    from conftest import check

    def _tmp():
        return Path(tempfile.mkdtemp()) / "CHANGELOG.md"

    entry = uc.format_entry("fix: something", ISSUE_URL, 1234, PR_URL)
    check("format", entry == f"## [fix: something]({ISSUE_URL}) — [PR #1234]({PR_URL})")

    cl = _tmp()
    cl.write_text(f"## [old](u) — [PR #1]({PR_URL})\n\n", encoding="utf-8")
    uc.update_changelog(cl, title="new", pr_number=2, pr_url=PR_URL, entry_url=ISSUE_URL)
    t = cl.read_text()
    check("newest-first", t.index("PR #2") < t.index("PR #1"))

    cl = _tmp()
    uc.update_changelog(cl, title="t", pr_number=99, pr_url=PR_URL)
    second = uc.update_changelog(cl, title="t", pr_number=99, pr_url=PR_URL)
    check("idempotent same PR twice", second is False and cl.read_text().count("PR #99") == 1)

    cl = _tmp()
    orig = f"## [x](u) — [PR #1234]({PR_URL})\n\n"
    cl.write_text(orig, encoding="utf-8")
    check("noop on existing", uc.update_changelog(cl, title="x", pr_number=1234, pr_url=PR_URL) is False)

    check("word boundary", uc.entry_present(f"[PR #123]({PR_URL})", 12) is False)
    check("skip-ci commit msg", "[skip ci]" in uc.COMMIT_MESSAGE)

    print(f"\n{conftest._passed} passed, {conftest._failed} failed")
    sys.exit(1 if conftest._failed else 0)
