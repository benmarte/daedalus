"""Tests for plugin registration — import safety, register() behaviour, manifest.

Run:  pytest tests/test_plugin_register.py -v
"""

import importlib.util
import os
from pathlib import Path
from unittest import mock

import pytest
import yaml

# Package root (the dir containing __init__.py).
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def isolate_home(tmp_path):
    """Point HOME at a temp dir so register()'s cron-wrapper install never
    touches the real ~/.hermes during tests.  Also remove HERMES_HOME so
    _sync_github_token uses the HOME-based fallback path."""
    with mock.patch.dict("os.environ", {"HOME": str(tmp_path)}, clear=False):
        os.environ.pop("HERMES_HOME", None)
        yield tmp_path


def _load_package():
    """Load the daedalus package by path — the directory has a hyphen so
    normal import won't work.  Mirrors how Hermes's plugin system loads modules."""
    spec = importlib.util.spec_from_file_location(
        "daedalus_plugin", str(ROOT / "__init__.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── 1. Import safety ─────────────────────────────────────────────────────────

def test_package_imports_without_error():
    """Loading the package module must never raise."""
    mod = _load_package()
    assert mod is not None


def test_package_has_register_function():
    """The package exposes a register() callable."""
    mod = _load_package()
    assert callable(mod.register)


# ── 2. register() behaviour ─────────────────────────────────────────────────

class FakeCtx:
    """Fake PluginContext that records calls."""

    def __init__(self):
        self.calls = []

    def register_auxiliary_task(self, key, *, display_name, description, defaults=None):
        self.calls.append({
            "method": "register_auxiliary_task",
            "key": key,
            "display_name": display_name,
            "description": description,
            "defaults": defaults,
        })

    # Deliberately missing methods — register() must not crash if they're absent.


def test_register_calls_register_auxiliary_task_exactly_once():
    """register(ctx) invokes register_auxiliary_task exactly once."""
    mod = _load_package()
    ctx = FakeCtx()
    mod.register(ctx)
    assert len(ctx.calls) == 1, f"Expected 1 call, got {len(ctx.calls)}"
    assert ctx.calls[0]["method"] == "register_auxiliary_task"


def test_register_auxiliary_task_key_and_metadata():
    """The auxiliary task key and display metadata are correct."""
    mod = _load_package()
    ctx = FakeCtx()
    mod.register(ctx)
    call = ctx.calls[0]
    assert call["key"] == "daedalus_dispatch"
    assert call["display_name"] == "Daedalus Dispatch"
    assert "description" in call
    assert len(call["description"]) > 0


def test_register_never_raises():
    """register() must not raise, even with a completely bare ctx (missing methods)."""
    mod = _load_package()

    class BareCtx:
        pass

    # Must not raise — wrapped in try/except inside register()
    mod.register(BareCtx())


def test_register_never_raises_with_none_ctx(isolate_home):
    """register() called with None must not raise (just no-op)."""
    mod = _load_package()
    # Must not raise
    mod.register(None)


# ── 2b. cron wrapper auto-install (issue #74) ────────────────────────────────

def test_register_installs_cron_wrapper(isolate_home):
    """register(ctx) writes ~/.hermes/scripts/daedalus-cron.sh on every load.

    Hermes has no post_install hook, so the wrapper must be (re)installed each
    time the plugin loads — otherwise fresh installs leave the dispatcher cron
    pointing at a non-existent script (issue #74)."""
    mod = _load_package()
    mod.register(FakeCtx())

    wrapper = isolate_home / ".hermes" / "scripts" / "daedalus-cron.sh"
    assert wrapper.is_file(), "register() did not install daedalus-cron.sh"
    assert wrapper.stat().st_mode & 0o111, "cron wrapper is not executable"
    assert "daedalus_dispatch.py" in wrapper.read_text()


def test_register_cron_install_is_idempotent(isolate_home):
    """Calling register() repeatedly is safe — the wrapper write is idempotent."""
    mod = _load_package()
    mod.register(FakeCtx())
    mod.register(FakeCtx())

    wrapper = isolate_home / ".hermes" / "scripts" / "daedalus-cron.sh"
    assert wrapper.is_file()


def test_ensure_cron_wrapper_never_raises(isolate_home, monkeypatch):
    """_ensure_cron_wrapper swallows failures so registration never breaks."""
    mod = _load_package()
    # Force the underlying install to blow up; the wrapper must still not raise.
    monkeypatch.setattr("os.path.dirname", lambda *_: "/nonexistent/path")
    mod._ensure_cron_wrapper()  # must not raise


# ── 2c. GITHUB_TOKEN profile sync (issue #78) ────────────────────────────────

def _make_profile(home, name, env_contents=None):
    """Create ~/.hermes/profiles/<name>/.env with optional contents; return path."""
    profile_dir = home / ".hermes" / "profiles" / name
    profile_dir.mkdir(parents=True, exist_ok=True)
    env_file = profile_dir / ".env"
    env_file.write_text(env_contents if env_contents is not None else "")
    return env_file


def test_sync_github_token_adds_missing_token(isolate_home):
    """A *-daedalus profile lacking GITHUB_TOKEN gets it from ~/.hermes/.env."""
    mod = _load_package()
    (isolate_home / ".hermes").mkdir(parents=True, exist_ok=True)
    (isolate_home / ".hermes" / ".env").write_text("GITHUB_TOKEN=ghp_fresh123\n")
    env_file = _make_profile(isolate_home, "developer-daedalus", "OTHER_KEY=1\n")

    mod._sync_github_token()

    assert "GITHUB_TOKEN=ghp_fresh123" in env_file.read_text()


def test_sync_github_token_is_idempotent(isolate_home):
    """Profiles that already have GITHUB_TOKEN are untouched (no duplicate)."""
    mod = _load_package()
    (isolate_home / ".hermes").mkdir(parents=True, exist_ok=True)
    (isolate_home / ".hermes" / ".env").write_text("GITHUB_TOKEN=ghp_fresh123\n")
    env_file = _make_profile(
        isolate_home, "reviewer-daedalus", "GITHUB_TOKEN=ghp_existing999\n"
    )

    mod._sync_github_token()
    mod._sync_github_token()  # twice — must remain stable

    text = env_file.read_text()
    assert text.count("GITHUB_TOKEN=") == 1
    assert "ghp_existing999" in text
    assert "ghp_fresh123" not in text


def test_sync_github_token_skips_non_daedalus_profiles(isolate_home):
    """Profiles whose name does not end with -daedalus are not modified."""
    mod = _load_package()
    (isolate_home / ".hermes").mkdir(parents=True, exist_ok=True)
    (isolate_home / ".hermes" / ".env").write_text("GITHUB_TOKEN=ghp_fresh123\n")
    env_file = _make_profile(isolate_home, "some-other-profile", "")

    mod._sync_github_token()

    assert "GITHUB_TOKEN" not in env_file.read_text()


def test_sync_github_token_noop_without_source_token(isolate_home):
    """No GITHUB_TOKEN in ~/.hermes/.env → profiles are left unchanged."""
    mod = _load_package()
    (isolate_home / ".hermes").mkdir(parents=True, exist_ok=True)
    (isolate_home / ".hermes" / ".env").write_text("SOMETHING_ELSE=x\n")
    env_file = _make_profile(isolate_home, "planner-daedalus", "")

    mod._sync_github_token()

    assert "GITHUB_TOKEN" not in env_file.read_text()


def test_sync_github_token_never_raises_without_hermes_home(isolate_home):
    """Missing ~/.hermes entirely must be a safe no-op, not a crash."""
    mod = _load_package()
    mod._sync_github_token()  # no ~/.hermes/.env, no profiles dir — must not raise


def test_register_syncs_github_token(isolate_home):
    """register() runs the token sync as part of plugin load."""
    mod = _load_package()
    (isolate_home / ".hermes").mkdir(parents=True, exist_ok=True)
    (isolate_home / ".hermes" / ".env").write_text("GITHUB_TOKEN=***\n")
    env_file = _make_profile(isolate_home, "validator-daedalus", "")

    mod.register(FakeCtx())

    assert "GITHUB_TOKEN=***" in env_file.read_text()


def test_sync_github_token_syncs_multiple_profiles_in_one_pass(isolate_home):
    """Every *-daedalus profile lacking the token is healed in a single call."""
    mod = _load_package()
    (isolate_home / ".hermes").mkdir(parents=True, exist_ok=True)
    (isolate_home / ".hermes" / ".env").write_text("GITHUB_TOKEN=ghp_multi\n")
    a = _make_profile(isolate_home, "developer-daedalus", "")
    b = _make_profile(isolate_home, "reviewer-daedalus", "OTHER=1\n")
    c = _make_profile(isolate_home, "planner-daedalus", "GITHUB_TOKEN=ghp_keep\n")

    mod._sync_github_token()

    assert "GITHUB_TOKEN=ghp_multi" in a.read_text()
    assert "GITHUB_TOKEN=ghp_multi" in b.read_text()
    # Profile that already had a token keeps its own value.
    assert "ghp_keep" in c.read_text()
    assert "ghp_multi" not in c.read_text()


def test_sync_github_token_prefers_hermes_home_env(isolate_home, monkeypatch):
    """HERMES_HOME overrides the ~/.hermes default for the source and profiles."""
    mod = _load_package()
    alt_home = isolate_home / "custom-hermes"
    (alt_home).mkdir(parents=True, exist_ok=True)
    (alt_home / ".env").write_text("GITHUB_TOKEN=ghp_fromenv\n")
    profile_dir = alt_home / "profiles" / "developer-daedalus"
    profile_dir.mkdir(parents=True, exist_ok=True)
    env_file = profile_dir / ".env"
    env_file.write_text("")
    monkeypatch.setenv("HERMES_HOME", str(alt_home))

    mod._sync_github_token()

    assert "GITHUB_TOKEN=ghp_fromenv" in env_file.read_text()


def test_sync_github_token_sets_secure_permissions(isolate_home):
    """A synced profile .env must be chmod 0o600 (token is a secret)."""
    mod = _load_package()
    (isolate_home / ".hermes").mkdir(parents=True, exist_ok=True)
    (isolate_home / ".hermes" / ".env").write_text("GITHUB_TOKEN=ghp_secret\n")
    env_file = _make_profile(isolate_home, "developer-daedalus", "")
    os.chmod(env_file, 0o644)

    mod._sync_github_token()

    assert (os.stat(env_file).st_mode & 0o777) == 0o600


def test_read_env_value_strips_quotes_and_export(isolate_home):
    """_read_env_value handles `export KEY="quoted"` dotenv lines."""
    mod = _load_package()
    env = isolate_home / "sample.env"
    env.write_text('# comment\nexport GITHUB_TOKEN="ghp_quoted"\n')

    assert mod._read_env_value(str(env), "GITHUB_TOKEN") == "ghp_quoted"


# ── 2d. Dispatch cron auto-recovery (issue #80) ──────────────────────────────


def _make_project(home, name, schedule="60m"):
    """Create a fake daedalus project with .hermes/daedalus.yaml and return the
    repo path and the daedalus.yaml path."""
    repo_path = home / "repos" / name
    (repo_path / ".hermes").mkdir(parents=True, exist_ok=True)
    cfg = {
        "name": name,
        "cron": {"schedule": schedule},
    }
    cfg_file = repo_path / ".hermes" / "daedalus.yaml"
    cfg_file.write_text(yaml.safe_dump(cfg))
    return str(repo_path), cfg_file


def _make_registry(home, *repo_paths):
    """Create ~/.hermes/daedalus/projects with the given repo paths."""
    registry_dir = home / ".hermes" / "daedalus"
    registry_dir.mkdir(parents=True, exist_ok=True)
    registry_file = registry_dir / "projects"
    registry_file.write_text("\n".join(repo_paths) + "\n")
    return registry_file


def test_ensure_dispatch_crons_noop_without_registry(isolate_home):
    """No ~/.hermes/daedalus/projects → no-op, never raises."""
    mod = _load_package()
    mod._ensure_dispatch_crons()  # must not raise


def test_ensure_dispatch_crons_creates_missing_cron(isolate_home, monkeypatch):
    """A project whose <name>-daedalus cron is missing gets created."""
    mod = _load_package()
    repo_path, _ = _make_project(isolate_home, "daedalus", "60m")
    _make_registry(isolate_home, repo_path)

    # Mock hermes cron list to show no existing crons.
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["hermes", "cron", "list"]:
            return mock.Mock(stdout="", returncode=0)
        if cmd[:3] == ["hermes", "cron", "create"]:
            return mock.Mock(stdout="", returncode=0)
        return mock.Mock(stdout="", returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)

    mod._ensure_dispatch_crons()

    # Should have called list then create.
    create_calls = [c for c in calls if c[:3] == ["hermes", "cron", "create"]]
    assert len(create_calls) == 1
    assert create_calls[0][3] == "60m"
    assert "--name" in create_calls[0]
    assert "daedalus-daedalus" in create_calls[0]
    assert "--no-agent" in create_calls[0]
    assert "--script" in create_calls[0]
    assert "daedalus-cron.sh" in create_calls[0]


def test_ensure_dispatch_crons_skips_existing_cron(isolate_home, monkeypatch):
    """A project whose cron already exists is not recreated."""
    mod = _load_package()
    repo_path, _ = _make_project(isolate_home, "daedalus", "60m")
    _make_registry(isolate_home, repo_path)

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["hermes", "cron", "list"]:
            return mock.Mock(stdout="daedalus-daedalus", returncode=0)
        return mock.Mock(stdout="", returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)

    mod._ensure_dispatch_crons()

    create_calls = [c for c in calls if c[:3] == ["hermes", "cron", "create"]]
    assert len(create_calls) == 0


def test_ensure_dispatch_crons_skips_project_without_name(isolate_home, monkeypatch):
    """A project whose daedalus.yaml lacks 'name' is skipped."""
    mod = _load_package()
    repo_path = str(isolate_home / "repos" / "unnamed")
    (Path(repo_path) / ".hermes").mkdir(parents=True, exist_ok=True)
    cfg_file = Path(repo_path) / ".hermes" / "daedalus.yaml"
    cfg_file.write_text(yaml.safe_dump({"cron": {"schedule": "60m"}}))
    _make_registry(isolate_home, repo_path)

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return mock.Mock(stdout="", returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)

    mod._ensure_dispatch_crons()

    create_calls = [c for c in calls if c[:3] == ["hermes", "cron", "create"]]
    assert len(create_calls) == 0


def test_ensure_dispatch_crons_skips_project_without_schedule(isolate_home, monkeypatch):
    """A project whose daedalus.yaml lacks cron.schedule is skipped."""
    mod = _load_package()
    repo_path = str(isolate_home / "repos" / "nosched")
    (Path(repo_path) / ".hermes").mkdir(parents=True, exist_ok=True)
    cfg_file = Path(repo_path) / ".hermes" / "daedalus.yaml"
    cfg_file.write_text(yaml.safe_dump({"name": "nosched"}))
    _make_registry(isolate_home, repo_path)

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return mock.Mock(stdout="", returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)

    mod._ensure_dispatch_crons()

    create_calls = [c for c in calls if c[:3] == ["hermes", "cron", "create"]]
    assert len(create_calls) == 0


def test_ensure_dispatch_crons_skips_missing_daedalus_yaml(isolate_home, monkeypatch):
    """A project without .hermes/daedalus.yaml is skipped."""
    mod = _load_package()
    repo_path = str(isolate_home / "repos" / "noconfig")
    (Path(repo_path) / ".hermes").mkdir(parents=True, exist_ok=True)
    _make_registry(isolate_home, repo_path)

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return mock.Mock(stdout="", returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)

    mod._ensure_dispatch_crons()

    create_calls = [c for c in calls if c[:3] == ["hermes", "cron", "create"]]
    assert len(create_calls) == 0


def test_ensure_dispatch_crons_skips_blank_and_comment_lines(isolate_home, monkeypatch):
    """Blank lines and comment lines in the registry are ignored."""
    mod = _load_package()
    repo_path, _ = _make_project(isolate_home, "daedalus", "60m")
    registry_dir = isolate_home / ".hermes" / "daedalus"
    registry_dir.mkdir(parents=True, exist_ok=True)
    registry_file = registry_dir / "projects"
    registry_file.write_text(f"\n# this is a comment\n\n{repo_path}\n\n")

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["hermes", "cron", "list"]:
            return mock.Mock(stdout="", returncode=0)
        return mock.Mock(stdout="", returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)

    mod._ensure_dispatch_crons()

    create_calls = [c for c in calls if c[:3] == ["hermes", "cron", "create"]]
    assert len(create_calls) == 1


def test_ensure_dispatch_crons_never_raises(isolate_home, monkeypatch):
    """Any exception is caught and logged, never propagated."""
    mod = _load_package()

    def fake_run(cmd, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("subprocess.run", fake_run)

    mod._ensure_dispatch_crons()  # must not raise


def test_register_ensures_dispatch_crons(isolate_home, monkeypatch):
    """register() calls _ensure_dispatch_crons as part of plugin load."""
    mod = _load_package()
    (isolate_home / ".hermes").mkdir(parents=True, exist_ok=True)
    (isolate_home / ".hermes" / ".env").write_text("GITHUB_TOKEN=***\n")

    called = False

    def fake_ensure():
        nonlocal called
        called = True

    monkeypatch.setattr(mod, "_ensure_dispatch_crons", fake_ensure)

    mod.register(FakeCtx())

    assert called, "_ensure_dispatch_crons was not called from register()"


# ── 3. plugin.yaml manifest ──────────────────────────────────────────────────

def test_plugin_yaml_exists_and_parses():
    """plugin.yaml must exist, parse as YAML, and have name == 'daedalus'."""
    manifest_path = ROOT / "plugin.yaml"
    assert manifest_path.exists(), f"plugin.yaml not found at {manifest_path}"
    content = manifest_path.read_text()
    data = yaml.safe_load(content)
    assert data is not None, "plugin.yaml parsed to None"
    assert data.get("name") == "daedalus", (
        f"Expected name='daedalus', got {data.get('name')!r}"
    )
    assert "version" in data
    assert "description" in data
    assert "author" in data
