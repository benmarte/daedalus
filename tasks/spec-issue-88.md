# Spec — Issue #88: Hermes plugin lifecycle CI

## Goal
A GitHub Actions workflow that exercises the full Hermes + Daedalus plugin
lifecycle (install → dispatch → upgrade → uninstall → reinstall) on every PR and
nightly, catching install-path regressions like #75 (missing deps), #79
(per-profile cron), and #80 (cron lost after `hermes update`) before users hit them.

## Key constraint (drives the whole design)
**Hermes is a private tool — not on PyPI, no public installer.** CI cannot run a
real `hermes` binary. BUT every regression #88 targets lives in the plugin's *own*
scripts (`postinstall.py`, `provision_roster.sh`, `__init__.py`'s
`_ensure_dispatch_crons`, `uninstall.sh`) — not in Hermes itself. So we ship a
small `hermes` CLI **stub** that records cron/profile/plugin/kanban state under an
isolated `$HERMES_HOME`, and run the *real* plugin scripts against it. The stub is
the regression surface for the cron-handling bugs.

## Acceptance criteria → implementation mapping
- Runs on push→`dev`, PR→`main`, nightly schedule — `on:` block.
- Stage 1 Fresh install: isolated `HERMES_HOME`, `pip install -r requirements.txt`
  resolves, run real `postinstall.py`, assert cron wrapper + roster profiles +
  webhook handler; `postinstall.py --check` is the health/smoke check (there is no
  `hermes doctor` — documented deviation).
- Stage 2 Dispatch smoke: `daedalus_dispatch.py --dry-run` (flag already exists),
  assert exit 0 + "DRY RUN"/"registry is empty" log.
- Stage 3 Upgrade: register a sample project, create its cron, simulate
  `hermes update` by wiping the cron store, re-run `register()`, assert the
  `<name>-daedalus` cron is recreated (#80 guard); re-run dispatch smoke.
- Stage 4 Uninstall + reinstall: real `uninstall.sh -y`, assert clean state (no
  `*-daedalus` cron, registry dir gone, plugin dir gone), reinstall, assert healthy.
- Stage 5 Reinstall dispatch smoke: dispatch `--dry-run` exit 0.
- All stages block merge to `main` (required check).

## Deviations from the spec (documented, not PM-blocking)
- `hermes doctor` → `postinstall.py --check` (the actual health check; no `doctor` exists).
- dispatch `--dry-run` already implemented; reused as-is.
- "Install Hermes" → seed an isolated `$HERMES_HOME` + a `hermes` stub on PATH,
  because Hermes is not publicly installable.

## Non-goals
- Live GitHub/Slack/Discord calls (all stubbed; VCS tokens unset → hermetic).
- Reimplementing Hermes. The stub only records state + emits parseable stdout.
