# Plugin lifecycle CI

`.github/workflows/plugin-lifecycle.yml` runs the full Hermes + Daedalus plugin
lifecycle on every push to `dev`, every PR targeting `main`, and nightly. It is the
regression guard for install-path bugs that only surface on a fresh install or
after `hermes update` — e.g. #75 (undeclared deps), #79 (per-profile cron dedup),
#80 (dispatch cron lost after upgrade).

## Why a `hermes` stub?

Hermes is a private tool — it is not on PyPI and has no public installer, so CI
cannot run the real binary. But the regressions above all live in **the plugin's
own scripts** (`postinstall.py`, `provision_roster.sh`, `__init__.py`'s
`_ensure_dispatch_crons`, `uninstall.sh`), not in Hermes itself. So we ship a
tiny `hermes` CLI stub (`tests/ci/hermes`) that records cron / profile / plugin /
kanban state under an isolated `$HERMES_HOME`, and run the **real** plugin scripts
against it. The stub is the surface the cron-handling regressions are asserted on.

## What runs

`tests/ci/lifecycle_smoke.sh` (also runnable locally — see below) drives five stages:

1. **Fresh install** — `requirements.txt` resolves (#75), real `postinstall.py`
   installs the cron wrapper, webhook handler, and 9-role roster; `postinstall.py
   --check` is the health check (there is no `hermes doctor`).
2. **Dispatch smoke** — `daedalus_dispatch.py --dry-run` exits 0, no mutations.
3. **Upgrade** — register a sample project + its cron, wipe the cron store to mimic
   `hermes update`, reload via `register()`, assert the `<name>-daedalus` cron is
   recreated (#80), re-run dispatch.
4. **Uninstall + reinstall** — real `uninstall.sh -y`, assert clean state (no stale
   cron, registry, or plugin dir), reinstall + re-provision.
5. **Reinstall dispatch smoke** — `--dry-run` exits 0 on the clean install.

`tests/test_plugin_lifecycle_workflow.py` is a fast guard on the harness shape
(triggers present, all 5 stages wired, stub keeps its subcommands).

## Run it locally

```bash
python3 -m venv /tmp/venv && source /tmp/venv/bin/activate
pip install -r requirements.txt
bash tests/ci/lifecycle_smoke.sh        # uses an isolated /tmp/hermes-ci-home
```

The driver sets its own `HOME`/`HERMES_HOME` and never touches your real Hermes
install. Override the work dir with `HERMES_CI_WORK=...`.
