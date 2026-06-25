"""Tests for plugin registration — import safety, register() behaviour, manifest.

Run:  pytest tests/test_plugin_register.py -v
"""

import importlib.util
import os
import subprocess
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
    (isolate_home / ".hermes" / ".env").write_text("GITHUB_TOKEN=ghp_viaregister\n")
    env_file = _make_profile(isolate_home, "validator-daedalus", "")

    mod.register(FakeCtx())

    assert "GITHUB_TOKEN=ghp_viaregister" in env_file.read_text()


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


# ── 2d. dispatch cron self-heal (issue #80) ──────────────────────────────────

class _FakeCron:
    """Fake ``subprocess.run`` for ``hermes cron`` calls.

    Returns a configurable ``cron list --all`` listing and records every
    ``cron create`` command so tests can assert what was (re)created.
    """

    def __init__(self, existing_names=(), list_rc=0, create_rc=0, raise_on=None):
        # `cron list --all` output is parsed for `Name: <cron>` lines only.
        self.list_output = "".join(f"  Name: {n}\n" for n in existing_names)
        self.list_rc = list_rc
        self.create_rc = create_rc
        self.raise_on = raise_on  # optional callable(cmd) -> bool
        self.creates: list[list[str]] = []

    def __call__(self, cmd, *args, **kwargs):
        if self.raise_on and self.raise_on(cmd):
            raise OSError("boom")
        if cmd[:4] == ["hermes", "cron", "list", "--all"]:
            return subprocess.CompletedProcess(cmd, self.list_rc, self.list_output, "")
        if cmd[:3] == ["hermes", "cron", "create"]:
            self.creates.append(cmd)
            return subprocess.CompletedProcess(cmd, self.create_rc, "created", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")


def _register_repo(home, repo_name, cfg):
    """Scaffold ``<home>/<repo_name>/.hermes/daedalus.yaml`` from *cfg* and add
    the repo to the default daedalus registry. Returns the repo path."""
    repo = home / repo_name
    (repo / ".hermes").mkdir(parents=True, exist_ok=True)
    (repo / ".hermes" / "daedalus.yaml").write_text(yaml.safe_dump(cfg))
    reg = home / ".hermes" / "daedalus" / "projects"
    reg.parent.mkdir(parents=True, exist_ok=True)
    with open(reg, "a") as fh:
        fh.write(str(repo) + "\n")
    return repo


def test_ensure_dispatch_crons_recreates_missing(isolate_home):
    """A registered project with no live cron gets its job recreated."""
    mod = _load_package()
    _register_repo(isolate_home, "alpha", {"name": "alpha", "cron": {"schedule": "every 30m"}})
    fake = _FakeCron(existing_names=())  # nothing exists yet

    with mock.patch.object(mod.subprocess, "run", fake):
        mod._ensure_dispatch_crons()

    assert len(fake.creates) == 1, "expected exactly one cron create"
    cmd = fake.creates[0]
    assert cmd[:3] == ["hermes", "cron", "create"]
    assert "every 30m" in cmd
    assert "--name" in cmd and "alpha-daedalus" in cmd
    assert "--script" in cmd and "daedalus-cron.sh" in cmd
    assert "--no-agent" in cmd


def test_ensure_dispatch_crons_idempotent_when_present(isolate_home):
    """An existing ``<name>-daedalus`` job is left untouched (no duplicate)."""
    mod = _load_package()
    _register_repo(isolate_home, "beta", {"name": "beta", "cron": {"schedule": "every 60m"}})
    fake = _FakeCron(existing_names=("beta-daedalus",))

    with mock.patch.object(mod.subprocess, "run", fake):
        mod._ensure_dispatch_crons()

    assert fake.creates == [], "must not recreate an already-present cron"


def test_ensure_dispatch_crons_skips_disabled_empty_schedule(isolate_home):
    """An explicit empty schedule means 'disabled' — never resurrected."""
    mod = _load_package()
    _register_repo(isolate_home, "gamma", {"name": "gamma", "cron": {"schedule": ""}})
    fake = _FakeCron(existing_names=())

    with mock.patch.object(mod.subprocess, "run", fake):
        mod._ensure_dispatch_crons()

    assert fake.creates == [], "empty schedule must not be recreated"


def test_ensure_dispatch_crons_falls_back_to_template_schedule(isolate_home):
    """A config omitting cron.schedule uses the packaged template default."""
    mod = _load_package()
    _register_repo(isolate_home, "delta", {"name": "delta"})  # no cron key at all
    fake = _FakeCron(existing_names=())

    with mock.patch.object(mod.subprocess, "run", fake):
        mod._ensure_dispatch_crons()

    assert len(fake.creates) == 1
    # Template default is "every 60m" (templates/daedalus.yaml).
    assert "every 60m" in fake.creates[0]


def test_ensure_dispatch_crons_passes_deliver(isolate_home):
    """A non-empty deliver (and no notifications) is forwarded as --deliver."""
    mod = _load_package()
    _register_repo(isolate_home, "epsilon", {
        "name": "epsilon",
        "cron": {"schedule": "every 15m", "deliver": "slack:C123"},
    })
    fake = _FakeCron(existing_names=())

    with mock.patch.object(mod.subprocess, "run", fake):
        mod._ensure_dispatch_crons()

    cmd = fake.creates[0]
    assert "--deliver" in cmd and "slack:C123" in cmd


def test_ensure_dispatch_crons_omits_deliver_with_notifications(isolate_home):
    """When notifications[] is set the cron must NOT double-deliver via --deliver."""
    mod = _load_package()
    _register_repo(isolate_home, "zeta", {
        "name": "zeta",
        "cron": {
            "schedule": "every 15m",
            "deliver": "slack:C123",
            "notifications": [{"target": "slack:C999"}],
        },
    })
    fake = _FakeCron(existing_names=())

    with mock.patch.object(mod.subprocess, "run", fake):
        mod._ensure_dispatch_crons()

    assert "--deliver" not in fake.creates[0]


def test_ensure_dispatch_crons_noop_without_registry(isolate_home):
    """No registry file → no cron calls at all, and no crash."""
    mod = _load_package()
    fake = _FakeCron(existing_names=())

    with mock.patch.object(mod.subprocess, "run", fake):
        mod._ensure_dispatch_crons()  # must not raise

    assert fake.creates == []


def test_ensure_dispatch_crons_bails_when_list_fails(isolate_home):
    """If `cron list` fails we can't tell what's missing — create nothing."""
    mod = _load_package()
    _register_repo(isolate_home, "eta", {"name": "eta", "cron": {"schedule": "every 60m"}})
    fake = _FakeCron(existing_names=(), list_rc=1)

    with mock.patch.object(mod.subprocess, "run", fake):
        mod._ensure_dispatch_crons()

    assert fake.creates == [], "must not create when the cron list is unavailable"


def test_ensure_dispatch_crons_never_raises_on_subprocess_error(isolate_home):
    """A subprocess that raises must be swallowed — registration never breaks."""
    mod = _load_package()
    _register_repo(isolate_home, "theta", {"name": "theta", "cron": {"schedule": "every 60m"}})
    fake = _FakeCron(raise_on=lambda cmd: True)

    with mock.patch.object(mod.subprocess, "run", fake):
        mod._ensure_dispatch_crons()  # must not raise


def test_ensure_dispatch_crons_heals_only_missing_of_many(isolate_home):
    """With several projects, only the ones lacking a live cron are recreated."""
    mod = _load_package()
    _register_repo(isolate_home, "p1", {"name": "p1", "cron": {"schedule": "every 60m"}})
    _register_repo(isolate_home, "p2", {"name": "p2", "cron": {"schedule": "every 60m"}})
    _register_repo(isolate_home, "p3", {"name": "p3", "cron": {"schedule": "every 60m"}})
    fake = _FakeCron(existing_names=("p2-daedalus",))  # only p2 is alive

    with mock.patch.object(mod.subprocess, "run", fake):
        mod._ensure_dispatch_crons()

    created_names = {c[c.index("--name") + 1] for c in fake.creates}
    assert created_names == {"p1-daedalus", "p3-daedalus"}


def test_register_runs_ensure_dispatch_crons(isolate_home):
    """register() self-heals missing dispatch crons as part of plugin load."""
    mod = _load_package()
    _register_repo(isolate_home, "iota", {"name": "iota", "cron": {"schedule": "every 60m"}})
    fake = _FakeCron(existing_names=())

    with mock.patch.object(mod.subprocess, "run", fake):
        mod.register(FakeCtx())

    assert any("iota-daedalus" in c for c in fake.creates), \
        "register() did not recreate the missing dispatch cron"


# ── 2e. httpx dependency self-heal (issue #75) ───────────────────────────────


def test_ensure_dependencies_noop_when_httpx_present(isolate_home, monkeypatch):
    """When httpx is already importable, no pip install is spawned."""
    mod = _load_package()
    monkeypatch.setattr("importlib.util.find_spec", lambda name: object())
    calls = []
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: calls.append((a, k)))

    mod._ensure_dependencies()

    assert calls == [], "pip install should not run when httpx is present"


def test_ensure_dependencies_installs_when_httpx_missing(isolate_home, monkeypatch):
    """When httpx is missing, pip install -r requirements.txt is invoked."""
    mod = _load_package()
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    calls = []
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: calls.append((a, k)))

    mod._ensure_dependencies()

    assert len(calls) == 1, "expected exactly one pip install call"
    argv = calls[0][0][0]
    assert argv[:5] == [mod.sys.executable, "-m", "pip", "install", "-q"]
    assert argv[-1].endswith("requirements.txt")


def test_ensure_dependencies_never_raises(isolate_home, monkeypatch):
    """A pip/subprocess failure must be swallowed — registration never breaks."""
    mod = _load_package()
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)

    def _boom(*a, **k):
        raise RuntimeError("pip exploded")

    monkeypatch.setattr(mod.subprocess, "run", _boom)
    mod._ensure_dependencies()  # must not raise


def test_register_ensures_dependencies(isolate_home, monkeypatch):
    """register() runs the dependency self-heal as part of plugin load."""
    mod = _load_package()
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    calls = []
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: calls.append((a, k)))

    mod.register(FakeCtx())

    assert any(
        "pip" in c[0][0] and "install" in c[0][0] for c in calls
    ), "register() did not trigger the httpx dependency install"


def test_requirements_txt_exists_and_pins_httpx():
    """requirements.txt must exist at the repo root and declare httpx>=0.24."""
    req = ROOT / "requirements.txt"
    assert req.is_file(), f"requirements.txt not found at {req}"
    text = req.read_text()
    assert "httpx>=0.24" in text, "requirements.txt must pin httpx>=0.24"


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
