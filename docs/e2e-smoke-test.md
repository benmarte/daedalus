# E2E Smoke Test — Daedalus Plugin

## Overview

This document describes how to run the end-to-end smoke test for the Daedalus
plugin in a fresh Hermes environment. The test validates that all recent
fresh-install fixes work correctly and that the plugin can be set up without
manual intervention.

## Test Structure

The e2e test has two tiers:

| Tier | File | Scope | Automation |
|------|------|-------|------------|
| 1 | `tests/test_e2e_smoke.py` | Plugin load, cron creation, CI retry dedup | Fully automated (pytest) |
| 2 | `scripts/e2e_smoke_test.sh` | Full live environment (env, plugin, dashboard, dispatch, cron) | Steps 1-3 automated; steps 4-5 gated |

## Prerequisites

- Hermes CLI installed and configured
- Daedalus plugin installed (`hermes plugins install <path> --enable`)
- Python 3.9+ with `httpx` and `pyyaml` available
- `GITHUB_TOKEN` exported in the environment (for dispatch checks)

## Running Tier 1 (Automated)

```bash
# From the daedalus repo root:
python3 -m pytest tests/test_e2e_smoke.py -v
```

Expected output: all tests pass (no skip guards).

## Running Tier 2 (Shell Script)

```bash
# From the daedalus repo root:
export GITHUB_TOKEN=ghp_xxx
bash scripts/e2e_smoke_test.sh
```

Expected output:
- Steps 1-3: all PASS
- Steps 4-5: all SKIP (gated behind #79, #80, #81)

## Pass/Fail Checklist

### Tier 1 (Automated)

- [ ] `test_cron_wrapper_installed_on_register` — PASS
- [ ] `test_cron_wrapper_idempotent` — PASS
- [ ] `test_httpx_importable` — PASS
- [ ] `test_github_token_synced_to_daedalus_profiles` — PASS
- [ ] `test_github_token_synced_to_all_daedalus_profiles` — PASS
- [ ] `test_github_token_sync_secure_permissions` — PASS
- [ ] `test_cron_recreated_after_missing` — PASS
- [ ] `test_ci_retry_dedup_on_concurrent_invocations` — PASS
- [ ] `test_ci_retry_dedup_same_slug_different_pending_count` — PASS
- [ ] `test_ci_retry_dedup_different_slugs` — PASS
- [ ] `test_cancel_ci_retry_removes_cron` — PASS
- [ ] `test_cancel_ci_retry_not_found_is_benign` — PASS
- [ ] `test_ci_retry_slug_sanitized` — PASS
- [ ] `test_cancel_ci_retry_slug_sanitized` — PASS
- [ ] `test_schedule_ci_retry_subprocess_failure` — PASS
- [ ] `test_cancel_ci_retry_subprocess_failure` — PASS
- [ ] `test_ci_retry_post_fire_recreation_then_cancel` — PASS
- [ ] `test_register_registers_hooks` — PASS
- [ ] `test_requirements_txt_has_httpx` — PASS

### Tier 2 (Shell Script — Steps 1-3)

- [ ] hermes CLI is available
- [ ] Daedalus plugin directory exists
- [ ] plugin.yaml exists
- [ ] daedalus-cron.sh exists and is executable
- [ ] daedalus-cron.sh references daedalus_dispatch.py
- [ ] httpx is importable
- [ ] GITHUB_TOKEN synced to all *-daedalus profiles
- [ ] Dashboard plugin_api is importable
- [ ] daedalus_dispatch module is importable
- [ ] ConfigLoader is importable
- [ ] All core modules are importable

### Tier 2 (Shell Script — Steps 4-5, Manual)

- [ ] Create test GitHub issue, move to Ready, run daedalus-cron.sh
- [ ] Verify issue picked up, assigned, PR opened, kanban_block called
- [ ] Run hermes update — verify daedalus-daedalus cron auto-recreates
- [ ] Run dispatch twice concurrently — verify no duplicate CI-retry crons

## Re-running Before Each Release

1. Set up a fresh Hermes environment (or clean `~/.hermes`)
2. Install the Daedalus plugin
3. Export `GITHUB_TOKEN`
4. Run Tier 1: `python3 -m pytest tests/test_e2e_smoke.py -v`
5. Run Tier 2: `bash scripts/e2e_smoke_test.sh`
6. Verify all active checks pass
7. For manual steps 4-5, follow the checklist above
