"""Hermes Daedalus — native issue/spec → reviewed-PR automation.

Package marker and plugin entry point. The installable-plugin entry (`register(ctx)` +
`plugin.yaml`) wires the dispatcher (`scripts/daedalus_dispatch.py`) as an
auxiliary task. The dashboard tab (`dashboard/`) and native-wrapper core (`core/`,
`config/`) remain available but are not wired through the plugin system.

We deliberately do NOT insert this dir onto the global sys.path: doing so shadows
Hermes's own top-level modules (cli, tools, config, …) for every agent process.
Entrypoints (scripts/, dashboard/plugin_api.py) set up their own path locally.
"""

import logging
import os
import re
import subprocess
import sys
import threading
from typing import Optional

logger = logging.getLogger(__name__)


def _registry_file() -> "os.PathLike[str]":
    """Path to the daedalus project registry (mirrors core.registry / _ensure_dispatch_crons).

    Read directly rather than importing ``core.registry`` so the plugin process
    never puts the plugin dir on ``sys.path`` (see module docstring).
    """
    from pathlib import Path
    hermes_home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    return Path(
        os.environ.get("HERMES_ORCH_REGISTRY")
        or os.path.join(hermes_home, "daedalus", "projects")
    )


def _resolve_project_for_task() -> Optional[str]:
    """Return the registered repo path containing the worker's cwd, or ``None``.

    A kanban worker runs in its project's workdir, so scoping the post-session
    dispatch to that path stops a single worker from sweeping every registered
    project (issues #137 / #133).
    """
    try:
        from pathlib import Path
        reg = _registry_file()
        if not os.path.exists(reg):
            return None
        cwd = Path.cwd().resolve()
        for raw in Path(reg).read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            rpath = Path(line).expanduser().resolve()
            if cwd == rpath or rpath in cwd.parents:
                return str(rpath)
    except Exception:
        return None
    return None


def _on_session_end(session_id, completed, interrupted, model, platform, **kwargs):
    """Fire the daedalus dispatcher immediately after any worker session ends.

    Only triggers when the session is a Hermes kanban worker (HERMES_KANBAN_TASK
    is set by the kanban dispatcher). Runs daedalus-cron.sh in a daemon thread
    so it never blocks agent teardown. Exceptions are caught and logged at DEBUG
    to satisfy the Hermes hook contract (hooks must not raise).
    """
    if not os.environ.get("HERMES_KANBAN_TASK"):
        return
    hermes_home = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
    cron_script = os.path.join(hermes_home, "scripts", "daedalus-cron.sh")
    if not (os.path.isfile(cron_script) and os.access(cron_script, os.X_OK)):
        logger.debug("daedalus on_session_end: cron script missing or not executable: %s", cron_script)
        return

    # Scope the dispatch to the worker's own project so one session-end doesn't
    # sweep (and re-notify) every registered repo (issues #137 / #133). Falls
    # back to a global sweep when cwd isn't a registered project.
    cmd = ["bash", cron_script]
    repo = _resolve_project_for_task()
    if repo:
        cmd += ["--repo", repo]

    def _run():
        try:
            subprocess.run(
                cmd,
                env=os.environ.copy(),
                timeout=120,
                check=False,
                capture_output=True,
            )
        except Exception as exc:
            logger.debug("daedalus on_session_end dispatch failed: %s", exc)

    threading.Thread(target=_run, name="daedalus-advance", daemon=True).start()


def _on_kanban_task_claimed(task_id, board, assignee, run_id, **kwargs):
    """Sync the global Hermes model config into the profile before it runs.

    Fires when any kanban task is claimed. For daedalus profiles (name ends
    with '-daedalus'), copies model/providers/fallback_providers/custom_providers
    from ~/.hermes/config.yaml into the profile's config.yaml so the profile
    always uses whatever model is selected in Hermes — no manual re-provisioning
    needed after a model switch.

    Per-profile override: set ``_daedalus_model_override: true`` in the
    profile's config.yaml to opt out and lock that profile to a specific model.
    """
    if not assignee or not str(assignee).endswith("-daedalus"):
        return
    try:
        import yaml
        hermes_home = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
        global_cfg_path = os.path.join(hermes_home, "config.yaml")
        profile_cfg_path = os.path.join(hermes_home, "profiles", str(assignee), "config.yaml")

        if not os.path.isfile(global_cfg_path) or not os.path.isfile(profile_cfg_path):
            return

        with open(global_cfg_path) as f:
            global_cfg = yaml.safe_load(f) or {}
        with open(profile_cfg_path) as f:
            profile_cfg = yaml.safe_load(f) or {}

        if profile_cfg.get("_daedalus_model_override"):
            return

        sync_keys = ("model", "providers", "fallback_providers", "custom_providers")
        changed = False
        for key in sync_keys:
            global_val = global_cfg.get(key)
            if profile_cfg.get(key) != global_val:
                if global_val is None:
                    profile_cfg.pop(key, None)
                else:
                    profile_cfg[key] = global_val
                changed = True

        # Strip "messaging" from toolsets arrays — Hermes emits "Unknown toolsets: messaging"
        # at startup because this toolset is not registered. Daedalus agents don't need it.
        for ts_list in (profile_cfg.get("toolsets") or [], profile_cfg.get("disabled_toolsets") or []):
            if "messaging" in ts_list:
                ts_list.remove("messaging")
                changed = True
        platform_toolsets = profile_cfg.get("platform_toolsets") or {}
        for _platform, ts_list in platform_toolsets.items():
            if isinstance(ts_list, list) and "messaging" in ts_list:
                ts_list.remove("messaging")
                changed = True

        if changed:
            with open(profile_cfg_path, "w") as f:
                yaml.safe_dump(profile_cfg, f, default_flow_style=False, sort_keys=False)
            logger.debug("daedalus: synced model config into profile %s", assignee)
    except Exception as exc:
        logger.debug("daedalus kanban_task_claimed sync failed: %s", exc)


def _read_env_value(env_path: str, key: str) -> Optional[str]:
    """Return the value of ``key`` from a dotenv-style file, or None.

    Parses ``KEY=value`` lines (ignoring blanks, comments, and ``export``
    prefixes). Surrounding quotes are stripped. Returns the first match.
    """
    try:
        with open(env_path) as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):].lstrip()
                name, sep, value = line.partition("=")
                if sep and name.strip() == key:
                    value = value.strip()
                    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                        value = value[1:-1]
                    return value
    except OSError:
        return None
    return None


def _sync_github_token() -> None:
    """Sync GITHUB_TOKEN from ~/.hermes/.env into every *-daedalus profile .env.

    provision_roster.sh only writes GITHUB_TOKEN into each profile's .env when a
    token is resolvable at provision time. On a fresh install where the token is
    only added to ~/.hermes/.env afterwards (or was absent during provisioning),
    the profiles are left permanently missing the token and agents fail with
    GitHub auth errors until someone re-provisions (issue #78).

    This runs on every plugin load. For each ``~/.hermes/profiles/*-daedalus``
    profile, it adds or updates the GITHUB_TOKEN to match ~/.hermes/.env so that
    token rotations are picked up automatically. Never raises.
    """
    try:
        hermes_home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
        token = _read_env_value(os.path.join(hermes_home, ".env"), "GITHUB_TOKEN")
        if not token:
            return

        profiles_dir = os.path.join(hermes_home, "profiles")
        if not os.path.isdir(profiles_dir):
            return

        for name in os.listdir(profiles_dir):
            if not name.endswith("-daedalus"):
                continue
            profile_dir = os.path.join(profiles_dir, name)
            if not os.path.isdir(profile_dir):
                continue
            env_file = os.path.join(profile_dir, ".env")

            existing = _read_env_value(env_file, "GITHUB_TOKEN")
            if existing is not None:
                continue  # profile already has a token — leave it alone

            try:
                # Key absent — append it.
                with open(env_file, "a") as f:
                    f.write(f"\nGITHUB_TOKEN={token}\n")
                os.chmod(env_file, 0o600)
                logger.debug("daedalus: synced GITHUB_TOKEN into profile %s", name)
            except OSError as exc:
                logger.debug("daedalus: token sync failed for %s: %s", name, exc)
    except Exception:
        logger.debug("daedalus: github token sync failed", exc_info=True)


def _ensure_cron_wrapper() -> None:
    """Make sure ~/.hermes/scripts/daedalus-cron.sh exists on every plugin load.

    Hermes has no ``post_install`` hook for plugins — it only clones the repo —
    so ``scripts/postinstall.py`` is never run automatically by ``hermes plugin
    add``/``hermes update``. Without this, fresh installs leave the cron job
    pointing at a script that does not exist, and the cron silently fails.

    We load ``_install_cron_wrapper()`` from ``scripts/postinstall.py`` by file
    path (the scripts dir is deliberately not on ``sys.path`` — see the module
    docstring) and run it on every ``register()``. The write is idempotent
    (writes the file + chmod +x), so repeating it on every load is safe and
    cheap. Failures are logged, never raised.
    """
    try:
        import importlib.util

        postinstall_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "scripts", "postinstall.py"
        )
        spec = importlib.util.spec_from_file_location(
            "daedalus_postinstall", postinstall_path
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        ok, msg = module._install_cron_wrapper()
        if not ok:
            logger.warning("daedalus: %s", msg)
    except Exception:
        logger.debug("daedalus: cron wrapper install failed", exc_info=True)


def _schedule_to_crontab(schedule: str) -> str:
    """Convert interval schedules to crontab syntax so recreated crons run forever (Repeat: ∞).

    Thin back-compat wrapper around ``core.util.schedule_to_crontab`` — the single
    source of truth now shared with ``dashboard.plugin_api._reconcile_cron`` (issue
    #134). Imported lazily so the plugin entry point stays import-safe even if
    ``core`` is not yet on ``sys.path`` at load time.
    """
    from core.util import schedule_to_crontab
    return schedule_to_crontab(schedule)


def _ensure_dispatch_crons() -> None:
    """Recreate any missing or dead ``<name>-daedalus`` dispatch crons on every load.

    The main dispatch cron lives in the global Hermes cron store
    (``~/.hermes/cron/jobs.json``). ``hermes update`` snapshots then migrates
    that store and does NOT restore the daedalus job afterwards, so the
    dispatcher silently stops running — the only recovery was a dashboard
    **Save** (which calls ``_reconcile_cron``) or a manual ``hermes cron
    create`` (issue #80).

    On every plugin load we list the existing cron jobs once, then for each repo
    in the daedalus registry (``~/.hermes/daedalus/projects``) recreate its
    ``<name>-daedalus`` job if it is missing — using the same schedule/deliver
    semantics the dashboard Save would (see ``dashboard.plugin_api._reconcile_cron``).

    Self-healing rules:
    - schedule resolves the repo's ``cron.schedule``; when that key is absent we
      fall back to the packaged template default, mirroring ConfigLoader's
      deep-merge. An *explicit empty* schedule means "dispatch disabled" and is
      never resurrected. Interval schedules like "60m" are converted to crontab
      syntax so the recreated job repeats forever (Repeat: ∞).
    - ``--deliver`` is passed only when the repo sets a non-empty ``deliver`` and
      has no ``notifications`` (which self-deliver), matching ``_reconcile_cron``.
    - Jobs in ``[completed]`` state (timed-out or one-shot) are treated as dead:
      the old job is deleted and a fresh one is created with crontab syntax.

    A cross-process file lock (fcntl.flock) serialises simultaneous plugin loads
    so that only one process runs the check+create per cron name at a time —
    preventing the TOCTOU race that caused duplicate crons when multiple Hermes
    worker processes started up concurrently (issue #95).

    Best-effort: every failure is logged at DEBUG/WARNING and never raised, so a
    broken ``hermes`` binary or malformed config can't break plugin registration.
    """
    try:
        import fcntl
        import yaml
        from pathlib import Path

        hermes_home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
        registry_file = Path(
            os.environ.get("HERMES_ORCH_REGISTRY")
            or os.path.join(hermes_home, "daedalus", "projects")
        )
        if not registry_file.exists():
            return

        # Acquire a cross-process exclusive lock so simultaneous plugin loads
        # don't each see the cron as missing and create duplicates.
        lock_path = Path(hermes_home) / "daedalus" / ".cron-heal.lock"
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_fd = open(lock_path, "w")
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
        except Exception as exc:
            logger.debug("daedalus: could not acquire cron-heal lock: %s", exc)
            return

        try:
            # Packaged template default — used only when a repo config omits
            # cron.schedule entirely (mirrors ConfigLoader's deep-merge over defaults).
            default_schedule = ""
            template_path = Path(__file__).resolve().parent / "templates" / "daedalus.yaml"
            try:
                tpl = yaml.safe_load(template_path.read_text()) or {}
                tpl = tpl.get("defaults", tpl)
                default_schedule = ((tpl.get("cron") or {}).get("schedule") or "").strip()
            except Exception:
                pass

            # List existing cron job NAMES once (a single subprocess for all projects).
            # Without the list we can't tell what's missing, so bail rather than risk
            # creating duplicates.
            try:
                res = subprocess.run(
                    ["hermes", "cron", "list", "--all"],
                    capture_output=True, text=True, timeout=10,
                )
            except Exception as exc:
                logger.debug("daedalus: cron list failed during self-heal: %s", exc)
                return
            if res.returncode != 0:
                logger.debug("daedalus: cron list returned %s during self-heal", res.returncode)
                return
            # Parse name, status, AND schedule so we can detect dead or
            # interval-format crons.  Only active crons with crontab-syntax
            # schedules block recreation.
            # NOTE: cron list output has Name: before Schedule:, so we must
            # accumulate all fields and flush when the next entry starts.
            cron_info: dict[str, tuple[str, str, str]] = {}  # name -> (cron_id, status, schedule)
            _cur_id: str | None = None
            _cur_status: str | None = None
            _cur_schedule: str | None = None
            _cur_name: str | None = None

            def _flush_cron_entry() -> None:
                if _cur_id and _cur_name:
                    cron_info[_cur_name] = (_cur_id, _cur_status or "", _cur_schedule or "")

            for line in res.stdout.splitlines():
                id_m = re.match(r"^\s+([0-9a-f]{8,})\s+\[(\w+)\]", line)
                if id_m:
                    _flush_cron_entry()
                    _cur_id, _cur_status, _cur_schedule, _cur_name = id_m.group(1), id_m.group(2), None, None
                    continue
                name_m = re.match(r"^\s*Name:\s+(.+)$", line)
                if name_m and _cur_id:
                    _cur_name = name_m.group(1).strip()
                    continue
                sched_m = re.match(r"^\s*Schedule:\s+(.+)$", line)
                if sched_m and _cur_id:
                    _cur_schedule = sched_m.group(1).strip()
            _flush_cron_entry()  # flush the last entry
            # Active crons: present and not completed AND already using crontab syntax.
            # Interval-format crons (e.g. "every 60m") are treated as stale and recreated.
            existing_names = {
                n for n, (_, s, sched) in cron_info.items()
                if s != "completed" and _schedule_to_crontab(sched) == sched
            }
            # Dead crons: [completed] — must delete before recreating
            dead_crons = {n: cid for n, (cid, s, _) in cron_info.items() if s == "completed"}
            # Interval-format active crons: stale, must delete and recreate with crontab syntax
            interval_crons = {
                n: cid for n, (cid, s, sched) in cron_info.items()
                if s != "completed" and _schedule_to_crontab(sched) != sched
            }

            for raw in registry_file.read_text().splitlines():
                repo_path = raw.strip()
                if not repo_path or repo_path.startswith("#"):
                    continue
                cfg_file = Path(repo_path) / ".hermes" / "daedalus.yaml"
                if not cfg_file.exists():
                    continue
                try:
                    cfg = yaml.safe_load(cfg_file.read_text()) or {}
                except Exception:
                    continue

                name = (cfg.get("name") or "").strip()
                if not name:
                    continue
                cron_cfg = cfg.get("cron") or {}
                schedule = (
                    (cron_cfg.get("schedule") or "").strip()
                    if "schedule" in cron_cfg
                    else default_schedule
                )
                if not schedule:
                    continue  # intentionally disabled — do not resurrect

                cron_name = f"{name}-daedalus"
                if cron_name in existing_names:
                    continue

                # Delete interval-format active crons so we can recreate with crontab syntax.
                if cron_name in interval_crons:
                    try:
                        subprocess.run(
                            ["hermes", "cron", "delete", interval_crons[cron_name]],
                            capture_output=True, text=True, timeout=10,
                        )
                        logger.info(
                            "daedalus: deleted interval-format dispatch cron: %s (%s) — will recreate with crontab syntax",
                            cron_name, interval_crons[cron_name],
                        )
                    except Exception as exc:
                        logger.debug("daedalus: could not delete interval cron %s: %s", cron_name, exc)

                # Delete the dead completed job so hermes lets us recreate the name.
                if cron_name in dead_crons:
                    try:
                        subprocess.run(
                            ["hermes", "cron", "delete", dead_crons[cron_name]],
                            capture_output=True, text=True, timeout=10,
                        )
                        logger.info(
                            "daedalus: deleted dead dispatch cron: %s (%s)",
                            cron_name, dead_crons[cron_name],
                        )
                    except Exception as exc:
                        logger.debug("daedalus: could not delete dead cron %s: %s", cron_name, exc)

                # Convert interval syntax to crontab so the job repeats forever.
                crontab_schedule = _schedule_to_crontab(schedule)
                cmd = [
                    "hermes", "cron", "create", crontab_schedule,
                    "--name", cron_name,
                    "--script", "daedalus-cron.sh",
                    "--no-agent",
                    # Run the dispatcher from this repo's root so it auto-scopes
                    # to this project instead of sweeping every repo (issue #137).
                    "--workdir", repo_path,
                ]
                deliver = (cron_cfg.get("deliver") or "").strip()
                if deliver and not cron_cfg.get("notifications"):
                    cmd += ["--deliver", deliver]

                try:
                    res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
                    if res.returncode == 0:
                        logger.info("daedalus: recreated missing dispatch cron: %s", cron_name)
                        existing_names.add(cron_name)  # prevent double-create within same load
                        # Write the normalised crontab schedule back to the config file so
                        # _reconcile_cron (dashboard) also uses crontab syntax and doesn't
                        # keep reverting the cron to interval format on the next save.
                        if crontab_schedule != schedule:
                            try:
                                raw_cfg = cfg_file.read_text()
                                import re as _re
                                new_cfg = _re.sub(
                                    r"(schedule\s*:\s*).*",
                                    lambda m: f'{m.group(1)}"{crontab_schedule}"',
                                    raw_cfg,
                                    count=1,
                                )
                                if new_cfg != raw_cfg:
                                    cfg_file.write_text(new_cfg)
                                    logger.info(
                                        "daedalus: normalised cron schedule in config: %s → %s",
                                        schedule, crontab_schedule,
                                    )
                            except Exception as exc:
                                logger.debug("daedalus: could not update schedule in config: %s", exc)
                    else:
                        logger.warning(
                            "daedalus: failed to recreate cron %s: %s",
                            cron_name, (res.stderr or res.stdout).strip()[:200],
                        )
                except Exception as exc:
                    logger.debug("daedalus: cron create failed for %s: %s", cron_name, exc)
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                lock_fd.close()
            except Exception:
                pass
    except Exception:
        logger.debug("daedalus: _ensure_dispatch_crons failed", exc_info=True)


def _ensure_dependencies() -> None:
    """Self-heal missing third-party deps (httpx) on every plugin load.

    ``core/providers/http.py`` imports ``httpx`` — the plugin's only
    third-party dependency. On a fresh OS, or a machine where the Hermes venv
    doesn't expose ``httpx`` to the interpreter running the dispatcher, the
    import fails at dispatch time with ``ModuleNotFoundError`` (issue #75).

    We check for ``httpx`` cheaply with ``importlib.util.find_spec`` first: in
    the common case it's already importable and we return immediately, adding
    zero pip overhead to the hot plugin-load path. Only when it's genuinely
    missing do we ``pip install`` ``requirements.txt``. Failures are logged at
    DEBUG, never raised — registration must never break on a dependency hiccup.
    """
    try:
        import importlib.util

        if importlib.util.find_spec("httpx") is not None:
            return

        req = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "requirements.txt"
        )
        if not os.path.isfile(req):
            return

        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-q", "-r", req],
            check=False,
            capture_output=True,
            timeout=120,
        )
    except Exception:
        logger.debug("daedalus: dependency install failed", exc_info=True)


def register(ctx) -> None:
    """Hermes plugin entry point — import-safe, never raises.

    Registers the daedalus dispatcher as an auxiliary LLM task so users
    can configure its provider/model independently of the main chat model.
    Also registers an on_session_end hook so any worker completion triggers
    dispatch immediately instead of waiting for the next 60-min cron tick,
    and a kanban_task_claimed hook to keep daedalus profile model configs
    in sync with the global Hermes model selection.

    Finally, it self-heals the host environment on every load: installs the
    httpx dependency if it's missing (Hermes provides no post-install hook —
    issue #75), (re)installs the daedalus-cron.sh wrapper so fresh installs and
    post-update environments always have the script the dispatcher cron job
    invokes, syncs GITHUB_TOKEN from ~/.hermes/.env into any *-daedalus profile
    .env that lacks it, and recreates any missing ``<name>-daedalus`` dispatch
    cron so the pipeline self-recovers after ``hermes update`` wipes the global
    cron store (#80).
    """
    try:
        ctx.register_auxiliary_task(
            key="daedalus_dispatch",
            display_name="Daedalus Dispatch",
            description="Issue/spec → reviewed-PR: scans GitHub Projects boards and kanban queues, "
                        "decomposes triage cards, dispatches worker agents.",
        )
        ctx.register_hook("on_session_end", _on_session_end)
        ctx.register_hook("kanban_task_claimed", _on_kanban_task_claimed)
    except Exception:
        logger.debug("daedalus register() failed", exc_info=True)

    # Idempotent — each runs in its own try/except so it never blocks registration.
    _ensure_dependencies()
    _ensure_cron_wrapper()
    _sync_github_token()
    _ensure_dispatch_crons()
