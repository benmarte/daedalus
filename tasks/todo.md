# Issue #134 — _reconcile_cron skips schedule conversion (one-shot crons)

## Problem
`_reconcile_cron()` passes the raw `cron.schedule` (e.g. `60m`) to `hermes cron
edit/create` without `_schedule_to_crontab()`. Hermes treats interval syntax as
a one-shot job → after one run it goes `[completed]` and the dispatcher stops.
`_ensure_dispatch_crons()` already converts; `_reconcile_cron()` (dashboard Save)
does not.

## Fix plan
- [ ] Add canonical `schedule_to_crontab()` to `core/util.py` (dep-free shared home,
      already imported by plugin_api for board_slug/parse_env_file).
- [ ] `__init__.py._schedule_to_crontab` delegates to `core.util` (lazy import → keeps
      plugin load import-safe; keeps the back-compat name the tests use).
- [ ] `_reconcile_cron(project_name, cron_cfg, cfg_path=None)`:
      - convert schedule to crontab before edit/create (both paths)
      - when conversion changed the value and cfg_path given, write the crontab
        schedule back to the YAML (mirror `_ensure_dispatch_crons`)
- [ ] Pass `cfg_path` from both callers (create + save endpoints).

## Tests
- [ ] Fix `test_updates_single_cron_in_place` (60m → "0 * * * *" in edit args).
- [ ] New: edit path converts interval → crontab.
- [ ] New: create path converts interval → crontab.
- [ ] New: crontab passthrough unchanged.
- [ ] New: write-back rewrites schedule in config file.
- [ ] core.util.schedule_to_crontab unit coverage.

## Verify
- [ ] pytest tests/ green
- [ ] ruff check/format on changed files
