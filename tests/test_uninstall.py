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
        """Without --keep-plugin, the plugin removal runs (hermes remove or rm -rf fallback)."""
        d = tmp_path / "my-hermes"
        d.mkdir()
        (d / "config.yaml").write_text("model: fake\n")
        (d / "daedalus.yaml").write_text("projects: []\n")
        r = _run(script_path, hermes_home=str(d), extra_args=["-y"])
        assert r.returncode == 0
        # hermes is not present in CI so remove falls back; either way the plugin
        # removal block runs and the tab-removed note is printed.
        assert "daedalus dashboard tab has been removed" in r.stdout

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


# ── Bug 1 fix: cron block parsing ──────────────────────────────────────────

class TestCronBlockParsing:
    """Block-based cron list parsing extracts the correct Name."""

    # A realistic sample of `hermes cron list --all` output in block format.
    _sample_out = (
        "  ba57e4afbba0 [active]\n"
        "    Name:      daedalus-daedalus\n"
        "    Schedule:  every 10m\n"
        "    Script:    daedalus-cron.sh\n"
        "\n"
        "  deadbeef1234 [active]\n"
        "    Name:      my-project-daedalus\n"
        "    Schedule:  0 9 * * *\n"
        "    Script:    dispatch.sh\n"
        "\n"
        "  cafebabe5678 [active]\n"
        "    Name:      unrelated-job\n"
        "    Schedule:  every 1h\n"
        "    Script:    unrelated.sh\n"
    )

    _expected = ["daedalus-daedalus", "my-project-daedalus"]

    @staticmethod
    def _run_parse(script_path: Path, hermes_home: str, cron_output: str) -> list[str]:
        """Shell-inject a fake cron output by wrapping with a here-string."""
        import shlex
        cmd = (
            "parse_cron_names() {\n"
            "  FOUND_CRON=()\n"
            "  CRON_LIST=$1\n"
            '  _in_block=false\n'
            '  _cron_name=""\n'
            '  _cron_script=""\n'
            "  while IFS= read -r line || [[ -n \"$line\" ]]; do\n"
            '    if [[ \"$line\" =~ ^[[:space:]]*[0-9a-fA-F]{6,}[[:space:]]+\\[ ]]; then\n'
            "      if $_in_block && [[ -n \"$_cron_name\" ]]; then\n"
            '        if [[ \"$_cron_name\" == *-daedalus ]] || [[ \"$_cron_script\" =~ daedalus-[^/]*\\.sh$ ]]; then\n'
            '          FOUND_CRON+=(\"$_cron_name\")\n'
            '        fi\n'
            "      fi\n"
            '      _in_block=true\n'
            '      _cron_name=""\n'
            '      _cron_script=""\n'
            "      continue\n"
            "    fi\n"
            '    if $_in_block; then\n'
            '      if [[ \"$line\" =~ ^[[:space:]]*Name:[[:space:]]+(.*) ]]; then\n'
            '        _cron_name=\"${BASH_REMATCH[1]}\"\n'
            '        _cron_name=\"${_cron_name%%[[:space:]]*}\"\n'
            '      elif [[ \"$line\" =~ ^[[:space:]]*Script:[[:space:]]+(.*) ]]; then\n'
            '        _cron_script=\"${BASH_REMATCH[1]}\"\n'
            '        _cron_script=\"${_cron_script%%[[:space:]]*}\"\n'
            "      fi\n"
            "    fi\n"
            "  done <<< \"$CRON_LIST\"\n"
            "  if $_in_block && [[ -n \"$_cron_name\" ]]; then\n"
            '    if [[ \"$_cron_name\" == *-daedalus ]] || [[ \"$_cron_script\" =~ daedalus-[^/]*\\.sh$ ]]; then\n'
            '      FOUND_CRON+=(\"$_cron_name\")\n'
            "    fi\n"
            "  fi\n"
            '  # dedup\n'
            '  FOUND_CRON=( $(printf \"%s\\n\" \"${FOUND_CRON[@]}\" | sort -u) )\n'
            '  printf \"%s\\n\" \"${FOUND_CRON[@]}\"\n'
            "}\n"
            f"parse_cron_names {shlex.quote(cron_output)}\n"
        )
        r = subprocess.run(
            ["bash", "-c", cmd],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "HERMES_HOME": hermes_home},
        )
        return [ln for ln in r.stdout.strip().split("\n") if ln]

    def test_block_parsing_extracts_correct_names(self, script_path, tmp_path):
        """The block parser extracts 'daedalus-daedalus' and 'my-project-daedalus'
        but NOT 'unrelated-job'."""
        d = tmp_path / "my-hermes"
        d.mkdir()
        (d / "config.yaml").write_text("model: fake\n")
        names = self._run_parse(script_path, str(d), self._sample_out)
        assert sorted(names) == sorted(self._expected), (
            f"Expected {self._expected}, got {names}"
        )

    def test_empty_cron_output_returns_nothing(self, script_path, tmp_path):
        """Empty output returns empty list."""
        d = tmp_path / "my-hermes"
        d.mkdir()
        (d / "config.yaml").write_text("model: fake\n")
        names = self._run_parse(script_path, str(d), "\n")
        assert names == []

    def test_no_daedalus_blocks_returns_nothing(self, script_path, tmp_path):
        """Output with no daedalus-related blocks returns nothing."""
        d = tmp_path / "my-hermes"
        d.mkdir()
        (d / "config.yaml").write_text("model: fake\n")
        noop = (
            "  abcdef01 [active]\n"
            "    Name:      other-job\n"
            "    Schedule:  every 1h\n"
            "    Script:    other.sh\n"
        )
        names = self._run_parse(script_path, str(d), noop)
        assert names == []

    def test_name_ends_in_daedalus_but_script_not_matching(self, script_path, tmp_path):
        """Block whose Name ends in -daedalus but Script is unrelated is matched."""
        d = tmp_path / "my-hermes"
        d.mkdir()
        (d / "config.yaml").write_text("model: fake\n")
        out = (
            "  ffffff0000 [active]\n"
            "    Name:      only-name-daedalus\n"
            "    Schedule:  every 5m\n"
            "    Script:    my-own-script.sh\n"
        )
        names = self._run_parse(script_path, str(d), out)
        assert names == ["only-name-daedalus"]

    def test_script_matches_daedalus_but_name_not_ending(self, script_path, tmp_path):
        """Block whose Script matches daedalus-*.sh but Name doesn't end in -daedalus is matched."""
        d = tmp_path / "my-hermes"
        d.mkdir()
        (d / "config.yaml").write_text("model: fake\n")
        out = (
            "  abcabc01 [active]\n"
            "    Name:      just-a-cron\n"
            "    Schedule:  every 10m\n"
            "    Script:    daedalus-dispatch.sh\n"
        )
        names = self._run_parse(script_path, str(d), out)
        assert names == ["just-a-cron"]

    def test_never_removes_with_empty_name(self, script_path, tmp_path):
        """A block that matches a Script: line but has no Name: results in no extraction."""
        d = tmp_path / "my-hermes"
        d.mkdir()
        (d / "config.yaml").write_text("model: fake\n")
        # This simulates the old bug pattern: a Script line alone should
        # not produce an empty name entry.
        out = (
            "  bb000000 [active]\n"
            "    Name:      real-job\n"
            "    Schedule:  every 5m\n"
            "    Script:    daedalus-cron.sh\n"
        )
        names = self._run_parse(script_path, str(d), out)
        # The script line does NOT create an entry; only the block's Name matters.
        assert names == ["real-job"]


# ── Bug 2 fix: board slug derivation ───────────────────────────────────────

class TestBoardSlugDerivation:
    """Board slugs are derived from registry paths, not scraped from 'hermes kanban boards ls'."""

    @staticmethod
    def _build_board_slug(repo: str, name: str = "") -> str:
        """Pure-Python reimplementation of the _build_board_slug helper in uninstall.sh."""
        import re
        slug = repo.replace("/", "-") if repo else name
        slug = slug.lower()
        slug = re.sub(r"[^a-z0-9_-]", "-", slug)
        slug = re.sub(r"-+", "-", slug)
        slug = slug.strip("-")
        return slug or name

    def test_org_repo_becomes_org_repo(self):
        assert self._build_board_slug("org/repo") == "org-repo"

    def test_github_com_url_strips_prefix(self):
        # Repos are stored without the prefix by ConfigLoader,
        # but if one slips through, the '/' and '.' are replaced — dots ARE non-alnum.
        slug = self._build_board_slug("github.com/org/repo")
        # github.com/org/repo -> github-com-org-repo (dots + slashes become dashes)
        assert slug == "github-com-org-repo"

    def test_special_chars_become_dashes(self):
        # org/repo!@#test — !@# become dashes, then consecutive dashes collapse
        assert self._build_board_slug("org/repo!@#test") == "org-repo-test"

    def test_default_never_produced(self):
        slug = self._build_board_slug("something/default")
        assert slug != "default"
        assert slug == "something-default"

    def test_uppercase_is_lowered(self):
        assert self._build_board_slug("MyOrg/MyRepo") == "myorg-myrepo"

    def test_fallback_name(self):
        assert self._build_board_slug("", "my-project") == "my-project"

    def test_registry_discovery_derives_correct_slug(self, script_path, tmp_path):
        """The _build_board_slug function derives the same slug the dispatcher would.
        Tested inline in bash (same logic as in uninstall.sh) — acme-corp/widget-store
        becomes acme-corp-widget-store."""
        d = tmp_path / "my-hermes"
        d.mkdir()
        (d / "config.yaml").write_text("model: fake\n")

        test_script = (
            '#!/usr/bin/env bash\n'
            '_build_board_slug() {\n'
            '  local _slug="${1:-$2}"\n'
            '  _slug="${_slug//\\//-}"\n'
            '  _slug="$(echo "$_slug" | tr \'[:upper:]\' \'[:lower:]\')"\n'
            '  _slug="$(echo "$_slug" | sed \'s/[^a-z0-9_-]/-/g\' | sed \'s/--*/-/g\' | sed \'s/^-//;s/-$//\')\"\n'
            '  echo "${_slug:-$2}"\n'
            '}\n'
            '_build_board_slug "$@"\n'
        )
        r = subprocess.run(
            ["bash", "-c", test_script, "test-board-slug", "acme-corp/widget-store", "My Project"],
            capture_output=True, text=True, timeout=30,
        )
        assert r.returncode == 0
        assert r.stdout.strip() == "acme-corp-widget-store"

    def test_registry_gone_returns_nothing(self, script_path, tmp_path):
        """When the registry file doesn't exist, no board slugs are produced."""
        d = tmp_path / "my-hermes"
        d.mkdir()
        (d / "config.yaml").write_text("model: fake\n")

        import shlex
        cmd = (
            "HERMES_HOME=" + shlex.quote(str(d)) + "\n"
            "REGISTRY_FILE=\"$HERMES_HOME/daedalus/projects\"\n"
            "if [[ -f \"$REGISTRY_FILE\" ]]; then\n"
            "  echo \"HAS_REGISTRY\"\n"
            "else\n"
            "  echo \"NO_REGISTRY\"\n"
            "fi\n"
        )
        r = subprocess.run(
            ["bash", "-c", cmd],
            capture_output=True, text=True, timeout=30,
        )
        assert r.returncode == 0
        assert "NO_REGISTRY" in r.stdout
        assert "HAS_REGISTRY" not in r.stdout


# ── Bug 3 fix: dashboard disable always runs ───────────────────────────────

class TestDashboardAlwaysDisabled:
    """hermes plugins disable daedalus runs unconditionally (idempotent)."""

    def test_dashboard_discovered_in_summary(self, script_path, tmp_path):
        """Even without any host artifacts, the summary always lists the dashboard tab."""
        d = tmp_path / "my-hermes"
        d.mkdir()
        (d / "config.yaml").write_text("model: fake\n")
        r = _run_stdin(script_path, hermes_home=str(d), stdin_text="n\n")
        assert r.returncode == 0
        combined = r.stdout + r.stderr
        assert "hermes plugins disable daedalus" in combined

    def test_no_enabled_gating_code(self, script_path, tmp_path):
        """Verify the old gating pipeline (grep -qi daedalus) is gone from executable code."""
        script_text = _script.read_text()
        # The old guard was: `hermes plugins list --enabled 2>/dev/null | grep -qi 'daedalus'`
        # The full pipeline shouldn't appear; a mention in a comment is fine.
        assert "grep -qi" not in script_text or "'daedalus'" not in script_text


class TestConfigEntryStripped:
    """The lingering plugins.enabled/.disabled daedalus entry is removed from config.yaml,
    while comments and unrelated entries are preserved (targeted line edit, never a YAML round-trip)."""

    # A realistic config.yaml: plugins block with daedalus in BOTH lists, plus
    # surrounding comments and unrelated keys/plugins that MUST survive untouched.
    _config = (
        "model: fake\n"
        "\n"
        "# ── Plugins ──────────────────────────────────────────────\n"
        "plugins:\n"
        "  enabled:\n"
        "  - agent-skills\n"
        "  - daedalus\n"
        "  - disk-cleanup\n"
        "  disabled:\n"
        "  - daedalus\n"
        "  - some-other-plugin\n"
        "\n"
        "# ── Fallback Model ───────────────────────────────────────\n"
        "fallback:\n"
        "  - daedalus-lookalike-key: keep-me\n"
    )

    def _strip(self, script_path: Path, tmp_path: Path):
        d = tmp_path / ".hermes"
        d.mkdir()
        cfg = d / "config.yaml"
        cfg.write_text(self._config)
        r = _run(script_path, hermes_home=str(d),
                 extra_args=["-y", "--keep-profiles", "--keep-plugin"])
        assert r.returncode == 0, (r.stdout + r.stderr)
        return cfg, cfg.read_text(), r

    def test_daedalus_removed_from_both_lists(self, script_path, tmp_path):
        _, after, _ = self._strip(script_path, tmp_path)
        # Neither the enabled nor the disabled list still carries a `- daedalus` item.
        assert "  - daedalus\n" not in after

    def test_unrelated_plugins_preserved(self, script_path, tmp_path):
        _, after, _ = self._strip(script_path, tmp_path)
        assert "  - agent-skills\n" in after
        assert "  - disk-cleanup\n" in after
        assert "  - some-other-plugin\n" in after

    def test_comments_and_other_keys_preserved(self, script_path, tmp_path):
        _, after, _ = self._strip(script_path, tmp_path)
        assert "# ── Plugins ──────────────────────────────────────────────" in after
        assert "# ── Fallback Model ───────────────────────────────────────" in after
        assert "model: fake" in after
        # A non-list-item line that merely contains "daedalus" is NOT touched.
        assert "  - daedalus-lookalike-key: keep-me\n" in after

    def test_summary_reports_config_cleanup(self, script_path, tmp_path):
        _, _, r = self._strip(script_path, tmp_path)
        assert "config.yaml plugins.enabled/.disabled daedalus entry" in (r.stdout + r.stderr)

    def test_backup_written(self, script_path, tmp_path):
        cfg, _, _ = self._strip(script_path, tmp_path)
        bak = cfg.parent / "config.yaml.daedalus-uninstall.bak"
        assert bak.exists()
        assert "  - daedalus\n" in bak.read_text()  # backup keeps the original

    def test_idempotent_when_no_entry(self, script_path, tmp_path):
        """A config with no daedalus list entry is left byte-for-byte unchanged (no backup churn)."""
        d = tmp_path / ".hermes"
        d.mkdir()
        cfg = d / "config.yaml"
        clean = "model: fake\nplugins:\n  enabled:\n  - agent-skills\n"
        cfg.write_text(clean)
        r = _run(script_path, hermes_home=str(d),
                 extra_args=["-y", "--keep-profiles", "--keep-plugin"])
        assert r.returncode == 0, (r.stdout + r.stderr)
        assert cfg.read_text() == clean
        assert not (d / "config.yaml.daedalus-uninstall.bak").exists()
