"""
Tests for scripts/postinstall.py — prerequisite checks and provision invocation.

Verifies:
- Each prereq check fails loudly with actionable guidance when the thing is missing.
- When all prereqs pass, provision_roster.sh is invoked via subprocess.
- Isolated from real ~/.hermes (mock filesystem and subprocess calls).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

# Make the repo root importable so we can load postinstall functions.
_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))


# ── load postinstall module ──────────────────────────────────────────────────

@pytest.fixture(scope="module")
def postinstall():
    """Import scripts/postinstall.py safely."""
    import importlib.util
    path = _repo_root / "scripts" / "postinstall.py"
    spec = importlib.util.spec_from_file_location("postinstall", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── helpers ──────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_hermes_home(tmp_path):
    """Patch _HERMES_HOME to a temp dir so tests never touch real ~/.hermes."""
    home = tmp_path / ".hermes"
    home.mkdir()
    # Create a default profile
    (home / "profiles" / "default").mkdir(parents=True)
    (home / "profiles" / "default" / "config.yaml").write_text("model: fake\n")
    # Create agent-skills plugin
    (home / "plugins" / "agent-skills" / "skills" / "using-agent-skills").mkdir(parents=True)
    return home


# ── prereq check tests ───────────────────────────────────────────────────────

class TestCheckDefaultProfile:
    """_check_default_profile returns clear messages."""

    def test_default_present(self, postinstall, tmp_path):
        home = tmp_path / ".hermes"
        (home / "profiles" / "default" / "config.yaml").parent.mkdir(parents=True, exist_ok=True)
        (home / "profiles" / "default" / "config.yaml").write_text("model: test\n")
        with mock.patch.object(postinstall, "_HERMES_HOME", home):
            ok, msg = postinstall._check_default_profile()
        assert ok is True
        assert "OK:" in msg

    def test_default_missing(self, postinstall, tmp_path):
        home = tmp_path / ".hermes_empty"
        home.mkdir()
        with mock.patch.object(postinstall, "_HERMES_HOME", home):
            ok, msg = postinstall._check_default_profile()
        assert ok is False
        assert "MISSING" in msg
        assert "hermes profile create default" in msg.lower() or "hermes setup" in msg.lower()

    def test_default_no_config_yaml(self, postinstall, tmp_path):
        home = tmp_path / ".hermes_noconfig"
        (home / "profiles" / "default").mkdir(parents=True)
        with mock.patch.object(postinstall, "_HERMES_HOME", home):
            ok, msg = postinstall._check_default_profile()
        assert ok is False
        assert "MISSING" in msg

    def test_default_root_config_only(self, postinstall, tmp_path):
        """Root config.yaml (standard install default) passes — no profiles/default/."""
        home = tmp_path / ".hermes_root"
        home.mkdir()
        (home / "config.yaml").write_text("model: root-only\n")
        with mock.patch.object(postinstall, "_HERMES_HOME", home):
            ok, msg = postinstall._check_default_profile()
        assert ok is True
        assert "OK:" in msg
        assert str(home / "config.yaml") in msg


class TestEnsureAgentSkills:
    """_ensure_agent_skills reports installed skills or auto-installs them."""

    def test_agent_skills_present(self, postinstall, tmp_path):
        home = tmp_path / ".hermes"
        (home / "plugins" / "agent-skills" / "skills" / "test-skill").mkdir(parents=True)
        with mock.patch.object(postinstall, "_HERMES_HOME", home):
            ok, msg = postinstall._ensure_agent_skills()
        assert ok is True
        assert "OK:" in msg

    def test_agent_skills_missing_auto_installs(self, postinstall, tmp_path):
        home = tmp_path / ".hermes_no_skills"
        home.mkdir()
        skills_dir = home / "plugins" / "agent-skills" / "skills"

        def fake_install(cmd, **kwargs):
            assert "agent-skills" in " ".join(cmd)
            skills_dir.mkdir(parents=True)
            return mock.Mock(returncode=0, stdout="installed", stderr="")

        with mock.patch.object(postinstall, "_HERMES_HOME", home), \
             mock.patch.object(postinstall.subprocess, "run", side_effect=fake_install):
            ok, msg = postinstall._ensure_agent_skills()
        assert ok is True
        assert "automatically" in msg

    def test_agent_skills_install_fails(self, postinstall, tmp_path):
        home = tmp_path / ".hermes_noplugins"
        home.mkdir()
        (home / "plugins").mkdir()
        failed = mock.Mock(returncode=1, stdout="", stderr="clone error")
        with mock.patch.object(postinstall, "_HERMES_HOME", home), \
             mock.patch.object(postinstall.subprocess, "run", return_value=failed):
            ok, msg = postinstall._ensure_agent_skills()
        assert ok is False
        assert "FAIL" in msg
        assert "hermes plugins install" in msg.lower()

    def test_agent_skills_hermes_cli_missing(self, postinstall, tmp_path):
        home = tmp_path / ".hermes_nocli"
        home.mkdir()
        with mock.patch.object(postinstall, "_HERMES_HOME", home), \
             mock.patch.object(postinstall.subprocess, "run", side_effect=FileNotFoundError):
            ok, msg = postinstall._ensure_agent_skills()
        assert ok is False
        assert "hermes" in msg.lower()


class TestCheckVcsTokens:
    """_check_vcs_tokens is advisory — never blocks install."""

    def test_token_present(self, postinstall):
        with mock.patch.dict("os.environ", {"GITHUB_TOKEN": "tok"}, clear=False):
            ok, msg = postinstall._check_vcs_tokens()
        assert ok is True
        assert "OK:" in msg and "GITHUB_TOKEN" in msg

    def test_no_tokens_is_advisory(self, postinstall):
        """No tokens must NOT block install — kanban-only setups need none."""
        with mock.patch.dict("os.environ", {}, clear=True):
            ok, msg = postinstall._check_vcs_tokens()
        assert ok is True
        assert "WARN" in msg
        assert "GITLAB_TOKEN" in msg and "AZURE_DEVOPS_PAT" in msg

    def test_multiple_tokens_listed(self, postinstall):
        with mock.patch.dict("os.environ",
                             {"GITLAB_TOKEN": "a", "AZURE_DEVOPS_PAT": "b"},
                             clear=True):
            ok, msg = postinstall._check_vcs_tokens()
        assert ok is True
        assert "GITLAB_TOKEN" in msg and "AZURE_DEVOPS_PAT" in msg


class TestRunProvision:
    """_run_provision invokes the shell script and reports results."""

    def test_provision_success(self, postinstall, tmp_path):
        script_dir = tmp_path / "scripts"
        script_dir.mkdir()
        (script_dir / "provision_roster.sh").write_text(
            "#!/bin/bash\necho '=== project-manager-daedalus ==='\necho '=== developer-daedalus ==='\necho 'roster done'\n"
        )
        (script_dir / "provision_roster.sh").chmod(0o755)

        ok, output = postinstall._run_provision(script_dir)
        assert ok is True
        assert "roster done" in output

    def test_provision_failure(self, postinstall, tmp_path):
        script_dir = tmp_path / "scripts"
        script_dir.mkdir()
        (script_dir / "provision_roster.sh").write_text(
            "#!/bin/bash\necho 'something broke' >&2\nexit 1\n"
        )
        (script_dir / "provision_roster.sh").chmod(0o755)

        ok, output = postinstall._run_provision(script_dir)
        assert ok is False
        assert "Provision failed" in output

    def test_provision_script_missing(self, postinstall, tmp_path):
        script_dir = tmp_path / "scripts"
        script_dir.mkdir()
        ok, output = postinstall._run_provision(script_dir)
        assert ok is False
        assert "MISSING" in output

    def test_extract_profiles(self, postinstall):
        output = """
=== project-manager-daedalus ===
  skills: spec-driven-development ...
=== developer-daedalus ===
  skills: context-engineering ...
=== roster provisioned ===
"""
        profiles = postinstall._extract_profiles_from_output(output)
        assert profiles == ["project-manager-daedalus", "developer-daedalus"]


# ── main() integration tests ─────────────────────────────────────────────────

class TestMain:
    """main() orchestrates checks and provision, returns correct exit codes."""

    def test_all_pass_provision(self, postinstall, tmp_path):
        """All checks pass + provision succeeds → exit 0."""
        home = tmp_path / ".hermes"
        (home / "profiles" / "default" / "config.yaml").parent.mkdir(parents=True, exist_ok=True)
        (home / "profiles" / "default" / "config.yaml").write_text("model: ok\n")
        (home / "plugins" / "agent-skills" / "skills" / "x").mkdir(parents=True)

        script_dir = tmp_path / "scripts"
        script_dir.mkdir()
        (script_dir / "provision_roster.sh").write_text(
            "#!/bin/bash\necho '=== developer-daedalus ==='\necho 'done'\n"
        )
        (script_dir / "provision_roster.sh").chmod(0o755)

        with mock.patch.object(postinstall, "_HERMES_HOME", home):
            with mock.patch("subprocess.run") as mock_run:
                mock_run.side_effect = [
                    # vcs token check
                    mock.Mock(returncode=0, stderr="Logged in as test\n"),
                    # provision_roster.sh
                    mock.Mock(
                        returncode=0,
                        stdout="=== developer-daedalus ===\ndone\n",
                        stderr="",
                    ),
                ]
                with mock.patch.object(postinstall.Path, "__init__", lambda s, *a, **kw: None):
                    # Patch Path.__init__ so the script_dir resolution inside main() still works.
                    # Actually, main() resolves script_dir = Path(__file__).resolve().parent
                    # We need to mock that to point at our tmp script_dir.
                    pass

        # Easier approach: mock _run_provision and _check_* directly
        with mock.patch.object(postinstall, "_HERMES_HOME", home):
            with mock.patch.object(postinstall, "_check_vcs_tokens", return_value=(True, "OK")):
                with mock.patch.object(postinstall, "_run_provision", return_value=(True, "=== dev ===\ndone\n")):
                    rc = postinstall.main()
        assert rc == 0

    def test_one_prereq_fails(self, postinstall, tmp_path):
        """One prereq fails → exit 1, no provision invoked."""
        home = tmp_path / ".hermes"
        home.mkdir()

        with mock.patch.object(postinstall, "_HERMES_HOME", home):
            rc = postinstall.main()
        assert rc == 1

    def test_check_only(self, postinstall, tmp_path):
        """--check mode exits 0 after checks, never calls provision."""
        home = tmp_path / ".hermes"
        (home / "profiles" / "default" / "config.yaml").parent.mkdir(parents=True, exist_ok=True)
        (home / "profiles" / "default" / "config.yaml").write_text("model: ok\n")
        (home / "plugins" / "agent-skills" / "skills" / "x").mkdir(parents=True)

        with mock.patch.object(postinstall, "_HERMES_HOME", home):
            with mock.patch.object(postinstall, "_check_vcs_tokens", return_value=(True, "OK")):
                with mock.patch.object(postinstall, "_run_provision") as mock_prov:
                    rc = postinstall.main(check_only=True)
        assert rc == 0
        mock_prov.assert_not_called()

    def test_provision_failure_exit_code(self, postinstall, tmp_path):
        """Provision failure → exit 2."""
        home = tmp_path / ".hermes"
        (home / "profiles" / "default" / "config.yaml").parent.mkdir(parents=True, exist_ok=True)
        (home / "profiles" / "default" / "config.yaml").write_text("model: ok\n")
        (home / "plugins" / "agent-skills" / "skills" / "x").mkdir(parents=True)

        with mock.patch.object(postinstall, "_HERMES_HOME", home):
            with mock.patch.object(postinstall, "_check_vcs_tokens", return_value=(True, "OK")):
                with mock.patch.object(postinstall, "_run_provision",
                                       return_value=(False, "Provision failed")):
                    rc = postinstall.main()
        assert rc == 2

    def test_import_safe(self, postinstall):
        """Module imports without side effects (the fixture already did it)."""
        assert postinstall is not None
        assert hasattr(postinstall, "main")
        assert hasattr(postinstall, "_check_default_profile")
        assert hasattr(postinstall, "_ensure_agent_skills")
        assert hasattr(postinstall, "_check_vcs_tokens")


# ── webhook handler install tests ────────────────────────────────────────────


class TestInstallWebhookHandler:
    """_install_webhook_handler copies daedalus-ready.sh to ~/.hermes/agent-hooks/."""

    def test_install_success(self, postinstall, tmp_path):
        """Installs handler to ~/.hermes/agent-hooks/daedalus-ready.sh, makes executable."""
        # Source script must exist for the install to work.
        source = _repo_root / "scripts" / "daedalus-ready.sh"
        if not source.is_file():
            pytest.skip("scripts/daedalus-ready.sh not present")

        fake_home = tmp_path / "fake_home"
        fake_home.mkdir()

        with mock.patch.dict("os.environ", {"HOME": str(fake_home)}):
            ok, msg = postinstall._install_webhook_handler()

        assert ok is True
        assert "OK:" in msg
        installed = fake_home / ".hermes" / "agent-hooks" / "daedalus-ready.sh"
        assert installed.is_file()
        # Check executable bit
        assert installed.stat().st_mode & 0o111

    def test_install_source_missing(self, postinstall, tmp_path, monkeypatch):
        """Returns FAIL when source script is missing."""
        fake_home = tmp_path / "fake_home"
        fake_home.mkdir()

        # Create a fake scripts dir that lacks daedalus-ready.sh
        fake_scripts = tmp_path / "scripts"
        fake_scripts.mkdir()
        fake_postinstall = fake_scripts / "postinstall.py"
        real_postinstall = _repo_root / "scripts" / "postinstall.py"
        fake_postinstall.write_text(real_postinstall.read_text())

        # Monkeypatch __file__ resolution so the function finds an empty scripts dir
        import importlib.util
        spec = importlib.util.spec_from_file_location("postinstall_nomock", str(fake_postinstall))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        with mock.patch.dict("os.environ", {"HOME": str(fake_home)}):
            ok, msg = mod._install_webhook_handler()

        assert ok is False
        assert "MISSING" in msg


# ── cron wrapper / gateway watchdog tests (#187) ─────────────────────────────


class TestInstallCronWrapper:
    """_install_cron_wrapper writes daedalus-cron.sh with the gateway watchdog."""

    def test_wrapper_content(self, postinstall, tmp_path):
        """Generated wrapper preserves existing behavior AND adds the watchdog block."""
        fake_home = tmp_path / "fake_home"
        fake_home.mkdir()

        with mock.patch.dict("os.environ", {"HOME": str(fake_home)}):
            ok, msg = postinstall._install_cron_wrapper()

        assert ok is True
        assert "OK:" in msg
        wrapper = fake_home / ".hermes" / "scripts" / "daedalus-cron.sh"
        assert wrapper.is_file()
        assert wrapper.stat().st_mode & 0o111  # executable
        text = wrapper.read_text()

        # Existing behavior preserved.
        assert ".hermes/.env" in text
        assert "daedalus_dispatch.py" in text
        assert "python3" in text

        # Watchdog: Python-based detection with rate limiting and backoff (#799).
        assert "gateway_watchdog.py" in text
        assert "python3" in text
        assert "--no-dispatch" in text
        # Overlap protection via mkdir-based lock.
        assert "mkdir" in text
        # The watchdog must sit BEFORE the dispatcher invocation.
        # Check that the actual python3 invocation of dispatch.py comes after watchdog invocations.
        # Use rfind to get the last occurrence (the actual invocation), not comments.
        dispatch_idx = text.rfind('python3 "$DISPATCH_HOME/scripts/daedalus_dispatch.py"')
        watchdog_idx = text.find('python3 "$WATCHDOG')
        assert dispatch_idx > 0, "dispatcher invocation not found"
        assert watchdog_idx > 0, "watchdog invocation not found"
        assert watchdog_idx < dispatch_idx, "watchdog must run before dispatcher"

    def test_wrapper_documents_and_parses_plugin_dir(self, postinstall, tmp_path):
        """Generated wrapper documents --plugin-dir and parses it before the exec (#233)."""
        fake_home = tmp_path / "fake_home"
        fake_home.mkdir()

        with mock.patch.dict("os.environ", {"HOME": str(fake_home)}):
            postinstall._install_cron_wrapper()
        text = (fake_home / ".hermes" / "scripts" / "daedalus-cron.sh").read_text()

        # Documented at the top of the script.
        assert "--plugin-dir <path>" in text
        # Parsed (both spaced and =form) and consumed into PLUGIN_DIR.
        assert "--plugin-dir)" in text
        assert "--plugin-dir=*)" in text
        # Override prepends PYTHONPATH and redirects the dispatcher path.
        assert 'export PYTHONPATH="$PLUGIN_DIR' in text
        assert 'DISPATCH_HOME="$PLUGIN_DIR"' in text
        # The dispatcher invocation resolves through DISPATCH_HOME, forwarding only the kept ARGS.
        assert 'python3 "$DISPATCH_HOME/scripts/daedalus_dispatch.py" "${ARGS[@]}"' in text
        # Parsing happens before the dispatcher is reached.
        # The actual dispatcher invocation (not in comments) should be near the end.
        dispatch_idx = text.rfind('python3 "$DISPATCH_HOME/scripts/daedalus_dispatch.py"')
        assert dispatch_idx > 0, "dispatcher invocation not found"
        # The dispatcher should be after all watchdog invocations (in the last third of the script)
        assert dispatch_idx > len(text) * 0.66, "dispatcher should run after setup and watchdogs"


def _build_fake_bin(tmp_path, *, with_hermes: bool):
    """Create a bin dir with stub `sleep`/`python3` (+ optional `hermes`).

    The `hermes` stub logs each invocation and reports liveness from a flag
    file so the post-restart status check can flip to 'running'. `sleep` is a
    no-op so the wrapper's `sleep 5` doesn't slow the test.
    """
    import sys

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()

    # No-op sleep so the wrapper's `sleep 5` returns instantly.
    sleep_stub = fake_bin / "sleep"
    sleep_stub.write_text("#!/usr/bin/env bash\nexit 0\n")
    sleep_stub.chmod(0o755)

    # `python3` must resolve to the running interpreter.
    py = fake_bin / "python3"
    py.write_text(f'#!/usr/bin/env bash\nexec "{sys.executable}" "$@"\n')
    py.chmod(0o755)

    log = tmp_path / "hermes.log"
    restart_flag = tmp_path / "restarted"

    if with_hermes:
        hermes = fake_bin / "hermes"
        hermes.write_text(
            "#!/usr/bin/env bash\n"
            f'echo "$1 $2" >> "{log}"\n'
            'if [ "$1" = "gateway" ] && [ "$2" = "status" ]; then\n'
            f'  if [ "$GW_MODE" = "down" ] && [ ! -f "{restart_flag}" ]; then\n'
            '    echo "✗ Gateway is not running"\n'
            "  else\n"
            '    echo "✓ Gateway is running — PID 4242"\n'
            "  fi\n"
            'elif [ "$1" = "gateway" ] && [ "$2" = "restart" ]; then\n'
            f'  touch "{restart_flag}"\n'
            "fi\n"
            "exit 0\n"  # status always exits 0, just like the real CLI
        )
        hermes.chmod(0o755)

    return fake_bin, log, restart_flag


class TestCronWrapperIntegration:
    """Run the generated wrapper end-to-end with a stubbed gateway_watchdog.py."""

    def _install_stub_watchdog(self, dispatch_home: Path, exit_code: int = 0):
        """Place a stub gateway_watchdog.py that logs its invocation and exit code."""
        wd = dispatch_home / "scripts" / "gateway_watchdog.py"
        wd.parent.mkdir(parents=True, exist_ok=True)
        wd.write_text(
            "import sys\n"
            f"print('watchdog invoked', file=sys.stderr)\n"
            f"sys.exit({exit_code})\n"
        )
        return wd

    def _install_stub_dispatcher(self, fake_home: Path, marker):
        """Place a stub daedalus_dispatch.py that touches `marker`."""
        disp = fake_home / ".hermes" / "plugins" / "daedalus" / "scripts" / "daedalus_dispatch.py"
        disp.parent.mkdir(parents=True, exist_ok=True)
        disp.write_text(
            "import os\n"
            f'open(r"{marker}", "w").close()\n'
        )
        return disp

    def _run(self, postinstall, tmp_path, *, with_watchdog: bool = True,
             wd_exit_code: int = 0, extra_args: str = ""):
        fake_home = tmp_path / "fake_home"
        fake_home.mkdir()
        with mock.patch.dict("os.environ", {"HOME": str(fake_home)}):
            ok, _ = postinstall._install_cron_wrapper()
        assert ok is True
        wrapper = fake_home / ".hermes" / "scripts" / "daedalus-cron.sh"

        marker = tmp_path / "dispatched"
        self._install_stub_dispatcher(fake_home, marker)

        if with_watchdog:
            dispatch_home = fake_home / ".hermes" / "plugins" / "daedalus"
            self._install_stub_watchdog(dispatch_home, exit_code=wd_exit_code)

        env = {
            "HOME": str(fake_home),
            "PATH": f"/usr/bin:/bin",
        }
        cmd = ["bash", str(wrapper)] + (extra_args.split() if extra_args else [])
        result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=30)
        return marker, result

    def test_watchdog_invoked_before_dispatcher(self, postinstall, tmp_path):
        """The watchdog script runs before the dispatcher and dispatcher still runs."""
        marker, result = self._run(postinstall, tmp_path)
        assert marker.exists(), "dispatcher must be reached after the watchdog"
        assert "watchdog invoked" in result.stderr

    def test_watchdog_missing_still_dispatches(self, postinstall, tmp_path):
        """If gateway_watchdog.py is missing, dispatcher still runs (best-effort)."""
        marker, result = self._run(postinstall, tmp_path, with_watchdog=False)
        assert marker.exists(), "dispatcher must run even without the watchdog script"
        assert "watchdog invoked" not in result.stderr

    def test_watchdog_failure_is_best_effort(self, postinstall, tmp_path):
        """A non-zero watchdog exit does not block the dispatcher (best-effort)."""
        marker, result = self._run(postinstall, tmp_path, wd_exit_code=1)
        assert marker.exists(), "dispatcher must run even if the watchdog fails"

    def test_flock_prevents_concurrent_watchdog(self, postinstall, tmp_path):
        """Non-blocking flock: if lock is held, watchdog block exits silently."""
        fake_home = tmp_path / "fake_home"
        fake_home.mkdir()
        with mock.patch.dict("os.environ", {"HOME": str(fake_home)}):
            postinstall._install_cron_wrapper()
        wrapper = fake_home / ".hermes" / "scripts" / "daedalus-cron.sh"

        # Pre-create the lock file and hold the lock for the duration of the run.
        lock_path = fake_home / ".hermes" / "gateway-watchdog.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.touch()

        marker = tmp_path / "dispatched"
        self._install_stub_dispatcher(fake_home, marker)
        dispatch_home = fake_home / ".hermes" / "plugins" / "daedalus"
        self._install_stub_watchdog(dispatch_home)

        # Hold the lock by exec-ing a background flock, then run the wrapper.
        env = {"HOME": str(fake_home), "PATH": "/usr/bin:/bin"}
        # Hold the lock via a subshell that sleeps with flock held
        hold_lock_cmd = f"exec 9>{lock_path}; flock -n 9; sleep 30"
        lock_holder = subprocess.Popen(
            ["bash", "-c", hold_lock_cmd],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        import time; time.sleep(0.3)  # let flock settle

        try:
            result = subprocess.run(
                ["bash", str(wrapper)], env=env,
                capture_output=True, text=True, timeout=10,
            )
            # Watchdog must NOT have been invoked (lock held).
            assert "watchdog invoked" not in result.stderr, (
                "concurrent watchdog should be skipped under flock"
            )
            # Dispatcher must still run.
            assert marker.exists(), "dispatcher must run even if watchdog is locked out"
        finally:
            lock_holder.terminate()
            lock_holder.wait(timeout=5)


def _install_recording_dispatcher(plugin_root, marker, record):
    """Stub daedalus_dispatch.py that touches `marker` and records argv + PYTHONPATH.

    `record` gets two lines: repr(sys.argv[1:]) then the PYTHONPATH env value,
    so a test can assert which args were forwarded and whether the dev checkout
    was prepended to the import path.
    """
    disp = plugin_root / "scripts" / "daedalus_dispatch.py"
    disp.parent.mkdir(parents=True, exist_ok=True)
    disp.write_text(
        "import os, sys\n"
        f'open(r"{marker}", "w").close()\n'
        f'with open(r"{record}", "w") as f:\n'
        '    f.write(repr(sys.argv[1:]) + "\\n")\n'
        '    f.write((os.environ.get("PYTHONPATH") or "") + "\\n")\n'
    )
    return disp


class TestPluginDirFlag:
    """--plugin-dir redirects the dispatcher to a local dev checkout (#233)."""

    def _run(self, postinstall, tmp_path, extra_args):
        fake_home = tmp_path / "fake_home"
        fake_home.mkdir()
        with mock.patch.dict("os.environ", {"HOME": str(fake_home)}):
            ok, _ = postinstall._install_cron_wrapper()
        assert ok is True
        wrapper = fake_home / ".hermes" / "scripts" / "daedalus-cron.sh"

        # Installed plugin location (the default source).
        installed_marker = tmp_path / "installed_ran"
        installed_rec = tmp_path / "installed_argv"
        _install_recording_dispatcher(
            fake_home / ".hermes" / "plugins" / "daedalus",
            installed_marker, installed_rec)

        # Local dev checkout (the override target).
        local_dir = tmp_path / "localdev"
        local_marker = tmp_path / "local_ran"
        local_rec = tmp_path / "local_argv"
        _install_recording_dispatcher(local_dir, local_marker, local_rec)

        fake_bin, _, _ = _build_fake_bin(tmp_path, with_hermes=False)
        env = {"HOME": str(fake_home), "PATH": f"{fake_bin}:/usr/bin:/bin"}
        result = subprocess.run(
            ["bash", str(wrapper), *extra_args],
            env=env, capture_output=True, text=True, timeout=30)
        return {
            "stderr": result.stderr,
            "local": (local_dir, local_marker, local_rec),
            "installed": (installed_marker, installed_rec),
        }

    def test_plugin_dir_runs_local_checkout(self, postinstall, tmp_path):
        """--plugin-dir runs the local dispatcher, warns, prepends PYTHONPATH, and
        does NOT forward the flag while passing the remaining args through."""
        local_dir = tmp_path / "localdev"
        r = self._run(postinstall, tmp_path,
                      ["--plugin-dir", str(local_dir), "--deliver", "slack"])

        _, local_marker, local_rec = r["local"]
        installed_marker, _ = r["installed"]
        assert local_marker.exists(), "local dev dispatcher must run"
        assert not installed_marker.exists(), "installed dispatcher must be bypassed"

        assert "WARNING --plugin-dir active" in r["stderr"]

        argv_line, pythonpath_line = local_rec.read_text().splitlines()[:2]
        forwarded = eval(argv_line)
        assert "--plugin-dir" not in forwarded, "flag must be consumed, not forwarded"
        assert str(local_dir) not in forwarded
        assert forwarded == ["--deliver", "slack"], "remaining args pass through verbatim"
        assert pythonpath_line.split(":")[0] == str(local_dir), \
            "local checkout must be prepended to PYTHONPATH"

    def test_plugin_dir_accepts_equals_form(self, postinstall, tmp_path):
        """--plugin-dir=<path> form is also honoured."""
        local_dir = tmp_path / "localdev"
        r = self._run(postinstall, tmp_path, [f"--plugin-dir={local_dir}"])
        _, local_marker, _ = r["local"]
        installed_marker, _ = r["installed"]
        assert local_marker.exists()
        assert not installed_marker.exists()

    def test_plugin_dir_without_value_falls_back(self, postinstall, tmp_path):
        """A trailing --plugin-dir with no path must not hang; falls back to installed."""
        r = self._run(postinstall, tmp_path, ["--plugin-dir"])
        _, local_marker, _ = r["local"]
        installed_marker, _ = r["installed"]
        assert installed_marker.exists(), "empty --plugin-dir must use the installed plugin"
        assert not local_marker.exists()

    def test_no_flag_uses_installed_plugin(self, postinstall, tmp_path):
        """Without --plugin-dir, the installed plugin runs and nothing is warned."""
        r = self._run(postinstall, tmp_path, ["--deliver", "slack"])
        _, local_marker, _ = r["local"]
        installed_marker, installed_rec = r["installed"]
        assert installed_marker.exists(), "installed dispatcher must run by default"
        assert not local_marker.exists()
        assert "--plugin-dir active" not in r["stderr"]
        forwarded = eval(installed_rec.read_text().splitlines()[0])
        assert forwarded == ["--deliver", "slack"]
