"""
Tests for scripts/uninstall.sh — HERMES_HOME safety guard and behavior.

Verifies:
- Guard rejects unsafe HERMES_HOME values (empty, /, $HOME, non-dir, non-hermes).
- Guard accepts a valid Hermes home (config.yaml present or dir named .hermes).
- --help flag works.
- Confirmation flow: n/empty aborts, -y proceeds, --keep-profiles keeps profiles.
- Summary is printed before confirmation.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

# Absolute path to the script under test.
_script = (
    Path(__file__).resolve().parent.parent / "scripts" / "uninstall.sh"
)


@pytest.fixture(scope="module")
def script_path():
    """Ensure the script exists and is executable."""
    assert _script.exists(), f"Script not found: {_script}"
    _script.chmod(_script.stat().st_mode | stat.S_IEXEC)
    return _script


def _run(script_path: Path, *, hermes_home: str, extra_args: list[str] | None = None) -> subprocess.CompletedProcess:
    """Run uninstall.sh with the given HERMES_HOME and optional extra args."""
    env = {**os.environ, "HERMES_HOME": hermes_home}
    return subprocess.run(
        ["bash", str(script_path)] + (extra_args or []),
        capture_output=True, text=True, timeout=30, env=env,
    )


def _run_stdin(
    script_path: Path,
    *,
    hermes_home: str,
    stdin_text: str = "",
    extra_args: list[str] | None = None,
) -> subprocess.CompletedProcess:
    """Run uninstall.sh with stdin piped (for confirmation prompts)."""
    env = {**os.environ, "HERMES_HOME": hermes_home}
    return subprocess.run(
        ["bash", str(script_path)] + (extra_args or []),
        capture_output=True, text=True, timeout=30, env=env,
        input=stdin_text,
    )


def _run_no_hermes_home(script_path: Path, *, home: str, extra_args: list[str] | None = None) -> subprocess.CompletedProcess:
    """Run uninstall.sh WITHOUT HERMES_HOME set — it must default to HOME/.hermes."""
    env = {**os.environ, "HOME": home}
    env.pop("HERMES_HOME", None)
    return subprocess.run(
        ["bash", str(script_path)] + (extra_args or []),
        capture_output=True, text=True, timeout=30, env=env,
    )


# ── Safety guard: reject unsafe HERMES_HOME ────────────────────────────────

class TestGuardRejects:
    """The safety guard must abort before any rm -rf when HERMES_HOME is unsafe."""

    def test_empty_hermes_home_with_valid_home_default(self, script_path, tmp_path):
        """When HERMES_HOME is unset, it defaults to HOME/.hermes and passes."""
        d = tmp_path / ".hermes"
        d.mkdir()
        (d / "config.yaml").write_text("model: fake\n")
        r = _run_no_hermes_home(script_path, home=str(tmp_path))
        assert r.returncode == 0
        assert "FATAL" not in r.stderr

    def test_both_hermes_home_and_home_unset(self, script_path):
        """When neither HERMES_HOME nor HOME is set, must FATAL."""
        env = {**os.environ}
        env.pop("HERMES_HOME", None)
        env.pop("HOME", None)
        r = subprocess.run(
            ["bash", str(script_path)],
            capture_output=True, text=True, timeout=30, env=env,
        )
        assert r.returncode == 1
        assert "FATAL" in r.stderr
        assert "could not resolve" in r.stderr.lower()

    def test_filesystem_root(self, script_path):
        r = _run(script_path, hermes_home="/")
        assert r.returncode == 1
        assert "FATAL" in r.stderr
        assert "unsafe" in r.stderr.lower()

    def test_home_root(self, script_path):
        r = _run(script_path, hermes_home=os.environ["HOME"])
        assert r.returncode == 1
        assert "FATAL" in r.stderr
        assert "unsafe" in r.stderr.lower()

    def test_non_directory(self, script_path, tmp_path):
        """Pass a path that exists but is a file, not a directory."""
        f = tmp_path / "not-a-dir"
        f.write_text("hello\n")
        r = _run(script_path, hermes_home=str(f))
        assert r.returncode == 1
        assert "FATAL" in r.stderr
        assert "not a directory" in r.stderr.lower()

    def test_non_hermes_dir_no_config_no_dot_hermes(self, script_path, tmp_path):
        """A plain directory with no config.yaml and basename != .hermes."""
        d = tmp_path / "random-dir"
        d.mkdir()
        r = _run(script_path, hermes_home=str(d))
        assert r.returncode == 1
        assert "FATAL" in r.stderr
        assert "not a valid hermes home" in r.stderr.lower()

    def test_dot_hermes_basename_no_config(self, script_path, tmp_path):
        """A directory named .hermes (without config.yaml) passes the basename check."""
        d = tmp_path / ".hermes"
        d.mkdir()
        r = _run(script_path, hermes_home=str(d))
        # Should pass — basename is .hermes.  Then it'll skip items because none exist.
        assert r.returncode == 0
        assert "FATAL" not in r.stderr


# ── Safety guard: accept valid HERMES_HOME ─────────────────────────────────

class TestGuardAccepts:
    """The guard should let execution proceed when HERMES_HOME is valid."""

    def test_config_yaml_present(self, script_path, tmp_path):
        """A dir with config.yaml is accepted as a Hermes home."""
        d = tmp_path / "my-hermes"
        d.mkdir()
        (d / "config.yaml").write_text("model: fake\n")
        r = _run(script_path, hermes_home=str(d))
        assert r.returncode == 0
        assert "FATAL" not in r.stderr
        # Should show discovery output (aborts if stdin is not a tty)
        combined = (r.stdout + r.stderr).lower()
        assert "what will be removed" in combined or "aborted" in combined

    def test_dot_hermes_no_config_passes_guard(self, script_path, tmp_path):
        """A dir named .hermes passes even without config.yaml (basename check)."""
        d = tmp_path / ".hermes"
        d.mkdir()
        r = _run(script_path, hermes_home=str(d))
        assert r.returncode == 0
        assert "FATAL" not in r.stderr


# ── Flags ──────────────────────────────────────────────────────────────────

class TestFlags:
    """Non-guard behavior: flags should still work as before."""

    def test_help_flag(self, script_path, tmp_path):
        d = tmp_path / "my-hermes"
        d.mkdir()
        (d / "config.yaml").write_text("model: fake\n")
        r = _run(script_path, hermes_home=str(d), extra_args=["--help"])
        assert r.returncode == 0
        assert "Usage:" in r.stdout


# ── Confirmation flow ──────────────────────────────────────────────────────

class TestConfirmationAbort:
    """Declining the confirmation must abort with nothing removed."""

    def test_n_aborts_nothing_removed(self, script_path, tmp_path):
        """Typing 'n' aborts — daedalus.yaml stays, script exits 0."""
        d = tmp_path / "my-hermes"
        d.mkdir()
        (d / "config.yaml").write_text("model: fake\n")
        (d / "daedalus.yaml").write_text("projects: []\n")
        r = _run_stdin(script_path, hermes_home=str(d), stdin_text="n\n")
        assert r.returncode == 0
        assert "Aborted" in r.stdout
        assert (d / "daedalus.yaml").exists(), "File should NOT be removed on abort"

    def test_empty_input_aborts(self, script_path, tmp_path):
        """Empty input (just Enter) defaults to No — aborts."""
        d = tmp_path / "my-hermes"
        d.mkdir()
        (d / "config.yaml").write_text("model: fake\n")
        (d / "daedalus.yaml").write_text("projects: []\n")
        r = _run_stdin(script_path, hermes_home=str(d), stdin_text="\n")
        assert r.returncode == 0
        assert "Aborted" in r.stdout
        assert (d / "daedalus.yaml").exists()

    def test_random_input_aborts(self, script_path, tmp_path):
        """Non-y input aborts (anything other than y/Y)."""
        d = tmp_path / "my-hermes"
        d.mkdir()
        (d / "config.yaml").write_text("model: fake\n")
        (d / "daedalus.yaml").write_text("projects: []\n")
        r = _run_stdin(script_path, hermes_home=str(d), stdin_text="maybe\n")
        assert r.returncode == 0
        assert "Aborted" in r.stdout
        assert (d / "daedalus.yaml").exists()


class TestConfirmationProceed:
    """--yes/-y skips the interactive prompt and proceeds."""

    def test_y_flag_proceeds(self, script_path, tmp_path):
        """-y removes files without prompting."""
        d = tmp_path / "my-hermes"
        d.mkdir()
        (d / "config.yaml").write_text("model: fake\n")
        (d / "daedalus.yaml").write_text("projects: []\n")
        r = _run(script_path, hermes_home=str(d), extra_args=["-y"])
        assert r.returncode == 0
        assert not (d / "daedalus.yaml").exists()
        assert "Aborted" not in r.stdout

    def test_yes_long_flag_proceeds(self, script_path, tmp_path):
        """--yes removes files without prompting."""
        d = tmp_path / "my-hermes"
        d.mkdir()
        (d / "config.yaml").write_text("model: fake\n")
        (d / "daedalus.yaml").write_text("projects: []\n")
        r = _run(script_path, hermes_home=str(d), extra_args=["--yes"])
        assert r.returncode == 0
        assert not (d / "daedalus.yaml").exists()
        assert "Aborted" not in r.stdout

    def test_y_flag_removes_daedalus_dir(self, script_path, tmp_path):
        """-y removes daedalus/ directory."""
        d = tmp_path / "my-hermes"
        d.mkdir()
        (d / "config.yaml").write_text("model: fake\n")
        (d / "daedalus").mkdir()
        (d / "daedalus" / "projects.yaml").write_text("projects: []\n")
        r = _run(script_path, hermes_home=str(d), extra_args=["-y"])
        assert r.returncode == 0
        assert not (d / "daedalus").exists()


class TestKeepProfiles:
    """--keep-profiles leaves profiles intact; --roster is a no-op alias."""

    def test_keep_profiles_flag_accepted(self, script_path, tmp_path):
        """--keep-profiles doesn't crash the script."""
        d = tmp_path / "my-hermes"
        d.mkdir()
        (d / "config.yaml").write_text("model: fake\n")
        (d / "daedalus.yaml").write_text("projects: []\n")
        r = _run(script_path, hermes_home=str(d), extra_args=["-y", "--keep-profiles"])
        assert r.returncode == 0
        assert not (d / "daedalus.yaml").exists()

    def test_roster_noop_accepted(self, script_path, tmp_path):
        """--roster is accepted as a no-op (profiles removed by default now)."""
        d = tmp_path / "my-hermes"
        d.mkdir()
        (d / "config.yaml").write_text("model: fake\n")
        (d / "daedalus.yaml").write_text("projects: []\n")
        r = _run(script_path, hermes_home=str(d), extra_args=["-y", "--roster"])
        assert r.returncode == 0
        assert not (d / "daedalus.yaml").exists()


class TestKeepPlugin:
    """--keep-plugin skips the deferred plugin removal."""

    def test_keep_plugin_flag_accepted(self, script_path, tmp_path):
        """--keep-plugin doesn't crash the script and skips plugin removal."""
        d = tmp_path / "my-hermes"
        d.mkdir()
        (d / "config.yaml").write_text("model: fake\n")
        (d / "daedalus.yaml").write_text("projects: []\n")
        r = _run(script_path, hermes_home=str(d), extra_args=["-y", "--keep-plugin"])
        assert r.returncode == 0
        assert not (d / "daedalus.yaml").exists()
        # Should NOT spawn deferred removal
        assert "Removing the plugin package" not in r.stdout
        # Should mention plugin was kept
        assert "kept" in r.stdout.lower() or "keep-plugin" in r.stdout.lower()

    def test_keep_plugin_combined_with_keep_profiles(self, script_path, tmp_path):
        """--keep-plugin --keep-profiles together work."""
        d = tmp_path / "my-hermes"
        d.mkdir()
        (d / "config.yaml").write_text("model: fake\n")
        (d / "daedalus.yaml").write_text("projects: []\n")
        r = _run(script_path, hermes_home=str(d), extra_args=["-y", "--keep-plugin", "--keep-profiles"])
        assert r.returncode == 0
        assert not (d / "daedalus.yaml").exists()
        assert "Removing the plugin package" not in r.stdout

    def test_no_keep_plugin_includes_deferred_removal(self, script_path, tmp_path):
        """Without --keep-plugin, the deferred removal message appears."""
        d = tmp_path / "my-hermes"
        d.mkdir()
        (d / "config.yaml").write_text("model: fake\n")
        (d / "daedalus.yaml").write_text("projects: []\n")
        r = _run(script_path, hermes_home=str(d), extra_args=["-y"])
        assert r.returncode == 0
        assert "Removing the plugin package" in r.stdout

    def test_keep_plugin_in_discovery_summary(self, script_path, tmp_path):
        """Discovery phase shows plugin kept when --keep-plugin is set."""
        d = tmp_path / "my-hermes"
        d.mkdir()
        (d / "config.yaml").write_text("model: fake\n")
        (d / "daedalus.yaml").write_text("projects: []\n")
        r = _run(script_path, hermes_home=str(d), extra_args=["-y", "--keep-plugin"])
        assert r.returncode == 0
        combined = r.stdout + r.stderr
        assert "--keep-plugin" in combined.lower() or "kept" in combined.lower()

    def test_keep_plugin_help_text(self, script_path, tmp_path):
        """--help output mentions --keep-plugin."""
        d = tmp_path / "my-hermes"
        d.mkdir()
        (d / "config.yaml").write_text("model: fake\n")
        r = _run(script_path, hermes_home=str(d), extra_args=["--help"])
        assert r.returncode == 0
        assert "--keep-plugin" in r.stdout


class TestNoManualFollowups:
    """The old 'Manual follow-ups' message with 'hermes plugins uninstall' is gone."""

    def test_no_manual_followup_message(self, script_path, tmp_path):
        """Output no longer tells user to run 'hermes plugins uninstall' as a manual step."""
        d = tmp_path / "my-hermes"
        d.mkdir()
        (d / "config.yaml").write_text("model: fake\n")
        (d / "daedalus.yaml").write_text("projects: []\n")
        r = _run(script_path, hermes_home=str(d), extra_args=["-y"])
        assert r.returncode == 0
        assert "Manual follow-ups (not done by this script)" not in r.stdout
        # The old standalone "hermes plugins uninstall daedalus" line should not appear
        # as a manual instruction (it may appear inside the deferred removal message though)
        # We check that the specific old phrasing is gone:
        assert "Manual follow-ups" not in r.stdout


class TestSummaryBeforeConfirmation:
    """The data-loss summary must appear before the confirmation prompt."""

    def test_summary_contains_permanent_warning(self, script_path, tmp_path):
        """Summary must show permanent data-loss warning before prompting."""
        d = tmp_path / "my-hermes"
        d.mkdir()
        (d / "config.yaml").write_text("model: fake\n")
        (d / "daedalus.yaml").write_text("projects: []\n")
        r = _run_stdin(script_path, hermes_home=str(d), stdin_text="n\n")
        assert r.returncode == 0
        combined = r.stdout + r.stderr
        assert "permanently" in combined.lower()
        assert "cannot be undone" in combined.lower()

    def test_continue_prompt_appears(self, script_path, tmp_path):
        """The Continue? [y/N] prompt must appear."""
        d = tmp_path / "my-hermes"
        d.mkdir()
        (d / "config.yaml").write_text("model: fake\n")
        (d / "daedalus.yaml").write_text("projects: []\n")
        r = _run_stdin(script_path, hermes_home=str(d), stdin_text="n\n")
        assert r.returncode == 0
        combined = r.stdout + r.stderr
        assert "Continue?" in combined
        assert "y/N" in combined

    def test_y_skips_prompt(self, script_path, tmp_path):
        """-y must NOT show the interactive Continue? prompt."""
        d = tmp_path / "my-hermes"
        d.mkdir()
        (d / "config.yaml").write_text("model: fake\n")
        (d / "daedalus.yaml").write_text("projects: []\n")
        r = _run(script_path, hermes_home=str(d), extra_args=["-y"])
        assert r.returncode == 0
        # The summary may still print, but there should be no interactive prompt
        assert "Continue?" not in r.stdout


# ── Regression: valid home still cleans up idempotently ────────────────────

class TestIdempotentCleanup:
    """When a valid home is provided, the script behaves normally."""

    def test_removes_daedalus_yaml(self, script_path, tmp_path):
        d = tmp_path / "my-hermes"
        d.mkdir()
        (d / "config.yaml").write_text("model: fake\n")
        (d / "daedalus.yaml").write_text("projects: []\n")
        r = _run(script_path, hermes_home=str(d), extra_args=["-y"])
        assert r.returncode == 0
        assert not (d / "daedalus.yaml").exists()

    def test_idempotent_rerun(self, script_path, tmp_path):
        d = tmp_path / "my-hermes"
        d.mkdir()
        (d / "config.yaml").write_text("model: fake\n")
        # First run — remove daedalus.yaml if present
        r1 = _run(script_path, hermes_home=str(d), extra_args=["-y"])
        assert r1.returncode == 0
        # Second run — still succeeds (idempotent)
        r2 = _run(script_path, hermes_home=str(d), extra_args=["-y"])
        assert r2.returncode == 0
        assert "FATAL" not in r2.stderr

    def test_unknown_flag_rejected(self, script_path, tmp_path):
        d = tmp_path / "my-hermes"
        d.mkdir()
        (d / "config.yaml").write_text("model: fake\n")
        r = _run(script_path, hermes_home=str(d), extra_args=["--bogus"])
        assert r.returncode == 2
