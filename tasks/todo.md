# Todo — Issue #88: Hermes plugin lifecycle CI

- [x] Research the real install/upgrade/uninstall scripts + the `hermes` CLI surface
- [x] Build `tests/ci/hermes` — lightweight stub CLI (cron/profile/plugins/kanban)
- [x] Build `tests/ci/lifecycle_smoke.sh` — 5-stage driver against an isolated `$HERMES_HOME`
- [x] Add `.github/workflows/plugin-lifecycle.yml` (push→dev, PR→main, nightly, dispatch)
- [x] Add `tests/test_plugin_lifecycle_workflow.py` — fast harness guard
- [x] Run driver locally end-to-end (all 5 stages green, python3.14 in a venv)
- [x] Run new guard + full pytest suite — no regressions
- [ ] Lint, push, open PR into `dev`, comment on #88, block kanban card, run dispatcher
