## [feat: native QA swarm fan-out behind planner.native_decompose](https://github.com/benmarte/daedalus/issues/1294) — [PR #1312](https://github.com/benmarte/daedalus/pull/1312)

Phase 5 of the structured-outcome epic (#1276). A new opt-in `planner.native_decompose` flag (default `false`) replaces the custom post-developer reviewer/security/accessibility(+docs) fan-out with ONE native `hermes kanban swarm` (parallel reviewer/security/accessibility workers → `qa` verifier → `documentation` synthesizer). The QA gate is preserved: the `qa-{n}` card is still created parented to the developer card, then the swarm root is linked under it and dependency-blocked (`hermes kanban block --kind dependency`) so Hermes auto-promotes the whole swarm only once QA completes. Idempotent via `qa-{n}` and `swarm-{n}` keys; any swarm failure logs and falls back to the legacy per-role fan-out so a hiccup degrades to today's behaviour rather than stranding the issue. Adds never-raise `kanban.swarm()` and `kanban.link()` wrappers. Only consulted when `pipeline.upfront_dag` is OFF (upfront_dag no-ops the per-tick fan-out entirely). With the flag off (the default) the per-tick fan-out runs exactly as before — byte-identical. Native planner epic decomposition through `hermes kanban decompose` (D1: kanban child cards instead of GitHub sub-issues) is the remaining half of #1294, tracked as a follow-up. See `tasks/spec-issue-1294.md`.

## [feat: native human gates via needs_input behind pipeline.native_gates](https://github.com/benmarte/daedalus/issues/1291) — [PR #1302](https://github.com/benmarte/daedalus/pull/1302)

Phase 3 of the structured-outcome epic (#1276). A new opt-in `pipeline.native_gates` flag (default `false`) tags the in-pipeline human gate — the developer `review-required: PR #N` re-block emitted once the PR is open and awaiting human review/merge (QA/reviewer/security run in the meantime) — with the native Hermes block category `hermes kanban block --kind needs_input`, so an awaiting-human card is natively distinguishable on the board from a machine-wait. The `kind` is advisory metadata only: `classify_blocked()` still routes off the `review-required:` reason string (→ ADVANCE), so flag-on adds the tag without changing flow and flag-off omits `--kind` entirely (plain block, byte-identical). Machine-wait blocks (`awaiting-fix:`, `awaiting-pr`) are deliberately NOT tagged. The 3 GitHub-side gates (move-to-Ready, merge action, close-issue) stay human/assert-only — they are never card blocks. The native pending-merge (`auto_merge=false`) `needs_input` marker is a documented follow-up, since the docs card is terminal by merge-time and there is no non-fragile card to block. With the flag off (the default) behaviour is byte-identical to before.

## [feat: upfront pipeline DAG at Ready-time behind pipeline.upfront_dag](https://github.com/benmarte/daedalus/issues/1290) — [PR #1300](https://github.com/benmarte/daedalus/pull/1300)

Phase 2 (the KEYSTONE) of the structured-outcome epic (#1276). A new opt-in `pipeline.upfront_dag` flag (default `false`) builds the ENTIRE stage graph in one shot at Ready-time — validator → PM → developer → QA → [reviewer, security, accessibility] → docs — with every non-root stage created dependency-blocked (`hermes kanban block --kind dependency`) so Hermes auto-promotes it the moment ALL of its parents complete (docs is multi-parented to reviewer + security + accessibility). A 6-outcome arbiter then prunes the pre-built DAG by the validator's structured verdict, read from native run metadata first (#1288 transport) with free-text-JSON and legacy-prefix fallback: CONFIRMED keeps the DAG (Hermes promotes PM); ALREADY_FIXED / DUPLICATE cancel every downstream branch silently; NEEDS_MORE_INFO / BLOCK_FOR_REVIEW block for human input (`--kind needs_input`); SECURITY_THREAT escalates + cancels; anything unparseable safe-parks as NEEDS_MORE_INFO (never auto-proceeds on ambiguity). While the flag is on, the per-tick post-developer downstream creation no-ops so the upfront DAG owns those cards; re-ticks are idempotent via stable `<role>-{n}` keys. `kanban.block_task` gains an additive `kind=` parameter (dependency / needs_input / capability / transient). With the flag off (the default) the dispatcher creates only the validator card and fans out per-tick exactly as before — byte-identical. Highest-risk toggle: ships inert; downstream work (#1291 gates, #1292 labels, #1294 decompose) builds on this. Keep `protocol.metadata_transport: true` alongside so the arbiter can read the validator verdict natively.

## [feat: native notification delivery behind notify.native_send](https://github.com/benmarte/daedalus/issues/1293) — [PR #1299](https://github.com/benmarte/daedalus/pull/1299)

A new opt-in `notify.native_send` flag (default `false`) makes native `hermes send` the sole delivery transport for escalation notifications (`retry-cap-exhausted`, `crash-retries-exhausted`). With the flag off (the default), behaviour is byte-identical to before: those escalations fire on BOTH transports — the native `hermes send` path (`cron.notifications` targets, with threading) AND the legacy raw-webhook path (`core/notification_sender.py` → `SLACK_WEBHOOK_URL` / `DISCORD_WEBHOOK_URL` env vars, Block Kit / Discord embeds). With the flag on, the redundant legacy raw-webhook calls are skipped since the native path already delivers the same notifications. Tradeoff: `hermes send` delivers plain markdown only (no Block Kit / embeds) and follows `cron.notifications` targets rather than the env webhooks, so configure native targets before flipping it. Delivery-only — inbound intake (the native webhook receiver) is tracked separately in #1298.

## [feat: native retry/runtime bounds behind execution.native_bounds](https://github.com/benmarte/daedalus/issues/1289) — [PR #1297](https://github.com/benmarte/daedalus/pull/1297)

An opt-in `execution.native_bounds` flag (default `false`) gives every dispatcher-created role card Hermes-native per-task circuit breakers: `--max-retries` (a consecutive-worker-failure breaker, default 2 = `kanban.failure_limit`, distinct from the CI/review fix-loop `MAX_FIX_ATTEMPTS = 3`) and `--max-runtime` (a wall-clock cap after which Hermes SIGTERM→SIGKILL→requeues the card). Per-role overrides come from `execution.native_max_retries`, `execution.native_max_runtime`, and `execution.native_bounds_by_role` (the developer role gets a generous default cap). With the flag on, native `--max-runtime` requeue is authoritative for wall-clock timeouts, so `crash_retry` skips `timeout`-class cards to avoid double-handling; all other crash classes flow through unchanged. Bounds-only — goal-mode (`--goal` judge adjudication) is a deliberate follow-up. With the flag off (the default) call sites emit byte-identical CLI args, so behaviour is unchanged.

## [feat: outcome metadata transport via native complete --metadata](https://github.com/benmarte/daedalus/issues/1288) — [PR #1295](https://github.com/benmarte/daedalus/pull/1295)

Phase 1 of the structured-outcome epic (#1276). A new opt-in `protocol.metadata_transport` flag (default `false`) lets **completion** handoffs additionally emit their structured outcome as native run metadata via `hermes kanban complete --metadata`, stored on the closing run and read back with `hermes kanban runs --json`. `classify_blocked()` reads the native closing-run outcome FIRST (telemetry source `metadata`), then falls through to the free-text JSON block and finally the legacy prefix, so no routing behaviour is lost; `_guard_prefix_on_done` also accepts a metadata-only completion. **Blocked** handoffs (review-required, awaiting-pr, changes-requested, needs_input) still carry their outcome as free-text JSON because `hermes kanban block` has no `--metadata` option — a blocked card has no closing run to attach metadata to (full elimination awaits the #1290 DAG work, Phase 2). With the flag off (the default) the caller passes `native_outcome=None`, making routing byte-identical to before. Keep `prefix_fallback: true` while soaking this flag.

## [refactor: extract agent prompt bodies to templates/agent_bodies/](https://github.com/benmarte/daedalus/issues/1147) — [PR #1240](https://github.com/benmarte/daedalus/pull/1240)

## [refactor: extract agent prompt bodies to templates/agent_bodies/](https://github.com/benmarte/daedalus/issues/1147) — [PR #1240](https://github.com/benmarte/daedalus/pull/1240)

## [perf: cache kanban list_tasks per dispatch tick](https://github.com/benmarte/daedalus/issues/1142) — [PR #1238](https://github.com/benmarte/daedalus/pull/1238)

## [perf: cache notification platform probes with a short TTL](https://github.com/benmarte/daedalus/issues/1144) — [PR #1232](https://github.com/benmarte/daedalus/pull/1232)

## [perf: pass pre-fetched task list into QA/reviewer/security gate checks](https://github.com/benmarte/daedalus/issues/1135) — [PR #1234](https://github.com/benmarte/daedalus/pull/1234)

## [feat: QA agent should skip local test suite when CI is already green on the PR](https://github.com/benmarte/daedalus/issues/1118) — [PR #1225](https://github.com/benmarte/daedalus/pull/1225)

## [perf: batch PR CI status lookups in dashboard open-PRs list](https://github.com/benmarte/daedalus/issues/1143) — [PR #1219](https://github.com/benmarte/daedalus/pull/1219)

## [docs: add CLAUDE.md — module map, invariants, and agent-maintainer guide](https://github.com/benmarte/daedalus/issues/1137) — [PR #1221](https://github.com/benmarte/daedalus/pull/1221)

## [ci: align CI Python version with development (3.12 vs 3.14)](https://github.com/benmarte/daedalus/issues/1138) — [PR #1223](https://github.com/benmarte/daedalus/pull/1223)

## [fix: follow-up issue dedup guard bypassed on API error — duplicate follow-up issues created silently](https://github.com/benmarte/daedalus/issues/1111) — [PR #1213](https://github.com/benmarte/daedalus/pull/1213)

## [feat: QA agent runs full 'pytest -n auto' suite so QA-green matches CI-green](https://github.com/benmarte/daedalus/issues/1201) — [PR #1212](https://github.com/benmarte/daedalus/pull/1212)

## [feat: auto-merge sweep re-runs failed CI (bounded) for pipeline-complete PRs, then escalates](https://github.com/benmarte/daedalus/issues/1199) — [PR #1208](https://github.com/benmarte/daedalus/pull/1208)

## [fix: advance-hook resolver crashes (ModuleNotFoundError: core) — pipeline never advances autonomously](https://github.com/benmarte/daedalus/issues/1202) — [PR #1204](https://github.com/benmarte/daedalus/pull/1204)

## [fix: CHANGELOG.md prepend causes merge conflicts between concurrent PRs](https://github.com/benmarte/daedalus/issues/1179) — [PR #1197](https://github.com/benmarte/daedalus/pull/1197)

## [fix: paginate _board_item_for_issue projectItems + deduplicate board item fallback logic](https://github.com/benmarte/daedalus/issues/1171) — [PR #1196](https://github.com/benmarte/daedalus/pull/1196)

## [fix: paginate _board_item_for_issue projectItems + deduplicate board item fallback logic](https://github.com/benmarte/daedalus/issues/1171) — [PR #1196](https://github.com/benmarte/daedalus/pull/1196)

`_board_item_for_issue()` queried an issue's `projectItems` edge with `first:20` and no `hasNextPage` pagination, so an issue enrolled in more than 20 GitHub Projects would miss the configured board's item and trigger a spurious (idempotent) re-enroll — the same silent-truncation class that #1158 had already hardened `_items()` against. The fix paginates `projectItems(first:100, after:$cursor)` with a cursor loop and a 50-page (~5000-item) safety cap mirroring `_items()`, returns on the first matching board item, and logs a truncation warning if the cap is hit. A new `_resolve_board_item(issue_number)` helper consolidates the verbatim listing-then-direct-lookup fallback blocks that were duplicated in `board_ensure_backlog` and `board_set_status`; it tries the cached listing first (no extra round-trip) and falls back to the edge only on a miss. Status extraction is deduplicated via a single `_status_of()` static helper reused by both `_items()` and `_board_item_for_issue()`.

## [fix: silent exception swallowing hides failures — add logging to bare except blocks](https://github.com/benmarte/daedalus/issues/1133) — [PR #1195](https://github.com/benmarte/daedalus/pull/1195)

## [fix: enable SQLite WAL mode at all direct connection sites](https://github.com/benmarte/daedalus/issues/1134) — [PR #1193](https://github.com/benmarte/daedalus/pull/1193)

## [fix(security): refuse verify_ssl:false outside dev mode](https://github.com/benmarte/daedalus/issues/1132) — [PR #1189](https://github.com/benmarte/daedalus/pull/1189)

## [fix(security): webhook signature verification is never invoked — wire verify_signature into the ready-event path](https://github.com/benmarte/daedalus/issues/1141) — [PR #1188](https://github.com/benmarte/daedalus/pull/1188)

## [fix(security): webhook signature verification is never invoked — wire verify_signature into the ready-event path](https://github.com/benmarte/daedalus/issues/1141)

`verify_signature()` in `core/webhook_normalizer.py` was dead code — the ready-event hook (`scripts/daedalus-ready.sh`) read the payload from stdin via `payload=$(cat -)` and dispatched straight through `normalize()` with no signature check, so anyone who could reach the webhook endpoint could trigger a full pipeline dispatch of agents with repo write access. The `cat -` capture also stripped trailing newlines and re-`echo`ed the body per heredoc, so any HMAC computed over it would have mismatched even with the right secret. The inline python is now extracted into the importable, unit-tested `core/webhook_dispatch.py`, which reads the raw stdin bytes exactly ONCE and verifies the HMAC signature BEFORE `normalize()`. Decision rule is fail-safe: with a secret configured (`vcs.webhook_secret_env`, an env-var name — never a raw secret in YAML) an invalid or missing signature is logged and dropped with no dispatch (exit 0); a valid signature proceeds; with no secret configured a one-time warning is logged (marker file) and dispatch proceeds so existing deployments keep working. Signatures are reconstructed from forwarded CGI-style `HTTP_*` headers for reverse-proxied receivers (the native hermes gateway already HMAC-verifies fail-closed upstream and forwards no headers). `scripts/daedalus-ready.sh` is reduced to a thin `python3 -m core.webhook_dispatch` wrapper, `validate_vcs` accepts `vcs.webhook_secret_env` (non-empty string when present; absence valid), and `templates/daedalus.yaml` documents the key.

## [fix: PM consultation trigger races APPROVE_ADVANCE — approved gate cards don't auto-advance](https://github.com/benmarte/daedalus/issues/1182) — [PR #1186](https://github.com/benmarte/daedalus/pull/1186)

## [fix(security): Azure webhook verification fails open when token header is missing](https://github.com/benmarte/daedalus/issues/1140) — [PR #1181](https://github.com/benmarte/daedalus/pull/1181)

## [fix: durable dedup + stage-recovery suppression for retry-cap notifications](https://github.com/benmarte/daedalus/issues/1167) — [PR #1172](https://github.com/benmarte/daedalus/pull/1172)

Role-scoped markers (`<!-- daedalus:retry-cap-notified:<role> -->`) replace the bare marker so distinct stall episodes for developer/PM/validator on the same issue don't collide. `_mark_notified_block` returns bool, logs warnings on failure, and falls back to stamping the triggering card when no validator card is found. New `_retry_cap_stage_recovered()` helper checks whether the stage has recovered (running card, open PR, downstream role active) before sending a notification; provider errors fail open to "not recovered" (better one duplicate than a swallowed real alert). All five cap paths (developer, validator x2, PM x2) updated with guard order: dedup-check → recovery-check → send → mark.

## [fix(security): dashboard API has no authentication — gate all daedalus plugin endpoints](https://github.com/benmarte/daedalus/issues/1130) — [PR #1173](https://github.com/benmarte/daedalus/pull/1173)

The daedalus dashboard plugin API exposed all 21 backend routes with no authentication, allowing any local process to read project data and trigger install/uninstall/notification actions. The fix adds a fail-closed shared-secret gate (`require_dashboard_auth`) applied to every sub-router at include time. When no secret is configured and the explicit opt-in is unset, requests are rejected with HTTP 403. When a secret is configured (`DAEDALUS_DASHBOARD_TOKEN` or `HERMES_DASHBOARD_SESSION_TOKEN`), missing/mismatched credentials return HTTP 401, compared in constant time via `hmac.compare_digest`. `DAEDALUS_DASHBOARD_AUTH_DISABLED=1` provides a local-dev escape hatch with a loud once-per-process warning.

## [fix(security): use timing-safe comparison for GitLab webhook token](https://github.com/benmarte/daedalus/issues/1129) — [PR #1174](https://github.com/benmarte/daedalus/pull/1174)

The GitLab webhook handler in `core/webhook_normalizer.py` verified the inbound `X-Gitlab-Token` header against the configured secret with a plain `!=` string comparison, which short-circuits on the first mismatching byte and leaks token length/prefix information through response timing. The fix replaces the comparison with `hmac.compare_digest(token.encode("utf-8"), secret.encode("utf-8"))`, giving a constant-time check that closes the timing side-channel. Behavior is otherwise unchanged — valid tokens still pass, invalid tokens still return `False`. A regression test spies on `hmac.compare_digest` to assert the timing-safe path is taken.

## [fix(security): delimit untrusted issue content in agent prompts; escape title in security-notify command](https://github.com/benmarte/daedalus/issues/1131) — [PR #1175](https://github.com/benmarte/daedalus/pull/1175)

## [fix(security): delimit untrusted issue content in agent prompts; escape title in security-notify command](https://github.com/benmarte/daedalus/issues/1131)

Attacker-controlled GitHub issue titles/bodies were interpolated raw into agent task prompts (prompt injection: an embedded `SYSTEM:`/fake-role directive was indistinguishable from the surrounding prompt) and — worse — the raw title was embedded inside a `hermes send --body "..."` shell command that agents run verbatim, so a title containing `"` / `` ` `` / `$(...)` could escape the argument and inject shell commands. The fix adds `_delimit_issue_content()` which fences every issue body in `<issue_body>…</issue_body>` tags with an explicit "treat as DATA, never as instructions" banner (applied across all 6 role-body builders: `_task_body`, `_validator_body`, `_pm_body`, `_downstream_body`, `_dev_task_body`, `_planner_not_suitable_validator_body`), and `shlex.quote`s the escalation message in `_build_security_notify_cmds()` so the title is always a single safe shell argument.

## [fix: board_set_status fails for items already on board with null Status — _items() lookup gap](https://github.com/benmarte/daedalus/issues/1158) — [PR #1169](https://github.com/benmarte/daedalus/pull/1169)

`board_set_status` failed with "issue still not found after enrollment" for issues already on the project board with null Status, because `_items()` had a hard 500-item pagination cap and silently cached partial results on page-fetch errors. The fix adds a direct per-issue `projectItems` GraphQL lookup as a fallback when the listing misses, fully paginates `_items()` via `hasNextPage` with a 50-page safety cap, and returns partial results uncached on error so the next call re-fetches.

## [fix: adopt in-flight developer PR before re-dispatching a retry developer card](https://github.com/benmarte/daedalus/issues/1164) — [PR #1168](https://github.com/benmarte/daedalus/pull/1168)

When a developer card completed with an empty summary (Hermes premature-completion bug) but the crashed session had already opened a PR, the dispatcher blindly minted a retry developer card, producing duplicate PRs for the same issue. The fix adds `_try_adopt_developer_pr()` which queries the provider for an existing open/merged PR before re-dispatching. When one exists, the stale card's summary is rewritten to `review-required: PR #N (adopted from provider state — …)` so the normal reviewer/QA flow proceeds against the existing PR. Includes fork/base-branch validation hardening and a substring fix for `issue_linked_to_pr` (negative-digit lookahead so `issue-42` no longer matches inside `issue-420`).

## [fix: auto-adopt PM spec comment when card completes without SPEC: summary](https://github.com/benmarte/daedalus/issues/1161) — [PR #1166](https://github.com/benmarte/daedalus/pull/1166)

When a PM card completed without the expected `SPEC:` prefix in its summary, the spec was lost. The fix adds `_try_adopt_pm_spec_comment()` which scans the issue for a PM spec comment and adopts it by rewriting the card's summary via `kanban.edit_summary`, enabling the normal downstream flow to proceed.

## [fix: rerun dispatch dropped on FileLock contention instead of silently discarding it](https://github.com/benmarte/daedalus/issues/1160) — [PR #1162](https://github.com/benmarte/daedalus/pull/1162)

When the dispatcher's FileLock was contended (another tick already running), the rerun marker was drained but the dispatch was silently discarded, causing the tick's work to be lost. The fix drains the rerun marker and logs the hook output so the next tick picks up the work.

## [fix: rescue gate cards re-promoted by block-loop detection when their verdict already passed](https://github.com/benmarte/daedalus/issues/1119) — [PR #1159](https://github.com/benmarte/daedalus/pull/1159)

Gate cards (qa-passed/review-approved/security-approved) re-promoted by block-loop detection were re-running the gate forever instead of being recognized as already passed. The fix adds a block-loop rescue scan that completes gate cards with passing verdicts instead of re-queuing them.

## [chore: repo hygiene — .gitignore gaps, untrack kanban.db, remove stray artifacts, archive closed specs](https://github.com/benmarte/daedalus/issues/1128) — [PR #1157](https://github.com/benmarte/daedalus/pull/1157)

## [fix: _resolve_repo_arg() swallows config-load failures with no diagnostic — wrong-project dispatch possible](https://github.com/benmarte/daedalus/issues/1110) — [PR #1122](https://github.com/benmarte/daedalus/pull/1122)

## [fix: developer task body does too much — strip /review, /code-simplify; add clean fallback on inner-agent failure](https://github.com/benmarte/daedalus/issues/1123) — [PR #1127](https://github.com/benmarte/daedalus/pull/1127)

## [fix: validator inner agent completes kanban card without summary → infinite retry loop](https://github.com/benmarte/daedalus/issues/1121) — [PR #1124](https://github.com/benmarte/daedalus/pull/1124)

When `coding_agent` is set to `claude-code` (or any non-hermes value), `_validator_body()` now emits "Print to stdout: 'CONFIRMED: ...'" instructions instead of "Complete your card..." for every outcome block, and explicitly prohibits `hermes kanban complete` calls from the inner subprocess. The `_ROLE_AFTER_SPAWN["validator"]` delegation block received the same fix. The outer `validator-daedalus` agent (SOUL.md step 6) remains the sole caller of `kanban complete`. A fallback guard in `validator-daedalus.md` covers the edge case where the inner agent somehow still marks the card done with `summary: None`.

## [fix: gate epic-level QA dispatch until at least one sub-issue PR is open](https://github.com/benmarte/daedalus/issues/1098) — [PR #1106](https://github.com/benmarte/daedalus/pull/1106)

## [fix: validator agent must not create kanban tasks or write board state (read-only enforcement)](https://github.com/benmarte/daedalus/issues/1105) — [PR #1107](https://github.com/benmarte/daedalus/pull/1107)

## [fix: epic-detection heuristic misclassifies large-body bug reports as epics — route to validator instead](https://github.com/benmarte/daedalus/issues/1100) — [PR #1103](https://github.com/benmarte/daedalus/pull/1103)

## [feat: dispatch QA/reviewer/security immediately on PR open — move CI gate to merge only](https://github.com/benmarte/daedalus/issues/1074) — [PR #1095](https://github.com/benmarte/daedalus/pull/1095)

## [fix: trigger ADVANCE immediately on review-required without waiting for CI](https://github.com/benmarte/daedalus/issues/1075) — [PR #1090](https://github.com/benmarte/daedalus/pull/1090)

- Developer cards with `review-required: PR #N` now ADVANCE immediately regardless of CI state (per epic #1074). CI gating moved from ADVANCE-time to merge-time only. QA/reviewer/security dispatch happens as soon as the PR is opened.
- Auto-merge gate enforces CI green before merging (issue #1085, PR #1092). If CI is not green when docs completes, the merge is deferred to the next cron tick.
- Reviewer and security merge gates added (issue #1085, PR #1093): `_reviewer_passed_for_issue()` and `_security_passed_for_issue()` check that the reviewer and security-analyst have approved the PR before auto-merge. A `skip-qa` label bypasses both gates (issue #1074 non-regression, PR #1094).
- Downstream review task body text updated to reflect that CI may still be running (issue #1082, PR #1091).

## [feat: dev_mode YAML flag to redirect dispatcher to local dev checkout](https://github.com/benmarte/daedalus/issues/1071) — [PR #1089](https://github.com/benmarte/daedalus/pull/1089)

## [fix: _check_completed_planner silently drops done planner tasks with non-PLANNING-COMPLETE summary](https://github.com/benmarte/daedalus/issues/1072) — [PR #1073](https://github.com/benmarte/daedalus/pull/1073)

# Changelog

All notable changes to Daedalus are documented here. The format loosely follows
[Keep a Changelog](https://keepachangelog.com/), and the project uses
`1.0.0-beta.N` pre-release versioning.

## [Unreleased]

---

## [1.0.0-beta.34] — 2026-06-30

### Tests

- Add explicit tests confirming `auto_merge=False` and absent `auto_merge` config do not trigger a merge even when QA has passed (closes test coverage gap identified post-beta.33)

---

## [1.0.0-beta.33] — 2026-06-30

### Bug Fixes

- Gate `qa_failed_cards` on executor `ok=True` — prevents spurious QA-failed notifications when kanban is unreachable ([#1002])
- Add `enrollment_failures` key to kanban-only dispatch summary — prevents `KeyError` for callers reading the summary dict
- Combine `merge_pr()` PUT + fallback GET failure into a single warning log entry with both errors visible ([#1034])
- Extend `_redact()` to cover URL-percent-encoded token variants — tokens encoded via `urllib.parse.quote` no longer leak in transport error messages
- `ensure_labels()` calls `list_labels()` exactly once — eliminated a redundant API round-trip on every label-ensure path
- `_resolve_web_path()` lazy-fetch + log injection hardening — `path_with_namespace` only fetched when needed; raw API response sanitized via `unicode_escape` before logging
- `VCSProvider.enrollment_failures` moved to instance `__init__` — eliminates shared mutable class-level default
- Document `_execute_qa_fix` `True`/`False` return semantics in docstring

### New Features

- **`max-fix-attempts` notification event** — operators can subscribe to receive an alert when a QA fix card exhausts `MAX_FIX_ATTEMPTS` and requires manual intervention (see `docs/adr/adr-006-max-fix-attempts-notification.md`)
- **`run_iterate()` 5-tuple return** — new 5th slot `escalated_cards` carries cards at the attempt ceiling, distinct from `qa_failed_cards` (first failure)
- **Per-process notification dedup** — `_notify_qa_failed` and `_notify_max_fix_attempts` each maintain a module-level set keyed on `(issue_n, pr)`; eliminates per-tick notification spam while a blocked card persists (see `docs/adr/adr-005-qa-failed-notification-dedup.md`)

### Tests

- 5-scenario E2E QA gate smoke suite (`tests/test_e2e_qa_gate_filelock_smoke.py`) — covers happy path, FileLock mutex (deterministic via `threading.Event` barriers), auto-merge blocked without QA signal, auto-merge fires on qa-passed, skip-qa bypass ([#1038])
- 19 new unit tests: `ok=False` kanban-down path, `ensure_labels` single-call dedup, `enrollment_failures` in kanban summary, combined PUT+GET warning log, notification dedup for both events, `max-fix-attempts` subscriber delivery, escalated 5th-slot assertion
- Total: 2551 tests passing

---

## [1.0.0-beta.32] — 2026-06-29

### Model & Profile Sync

- Profile resync: when `coding_agent` or global Hermes model changes, all `*-daedalus` profiles are updated automatically ([#1066], closes [#1053] [#1054] [#1055] [#1057])
- Config fingerprint stored per-workdir; first tick seeds baseline without resyncing; subsequent ticks with changed fingerprint trigger resync ([#1063], closes [#1052])
- Model injection: `--model ${resolved-model}` injected into `coding_agent_cmd` when absent ([#1049])

### Planner Intelligence

- Planner detects sub-tasks touching the same file(s) and merges them into one issue ([#1065], closes [#1058])
- Overlap-based blocking chains: sub-tasks sharing files are serialized with `depends_on` edges ([#1067], closes [#1059] [#1060] [#1061] [#1062])

### Pipeline Reliability

- `merge_pr` verifies actual GitHub state before reporting MERGE FAILED; worktree branch-cleanup errors no longer masked as merge failures ([#1068], closes [#1034])
- Validator-blocked idempotency key increments per block cycle — no more silent re-notifications ([#1033], closes [#994])
- Monotonic idempotency key for planner-fallback validator path ([#1031])
- Status-blind guard applied to task-existence queries ([#1027], closes [#1008])
- Gate auto-merge on QA pass signal ([#1006])
- Gate downstream review roles behind QA in fallback path ([#985], closes [#955])
- Safety nets for orphaned kanban cards ([#1005], closes [#957])
- Reconcile dev cards when PR merged outside pipeline ([#984], closes [#957])
- QA no longer races developer mid-edit on shared working tree ([#954], closes [#953])
- Dispatcher handler for planner `NOT SUITABLE` signal ([#941], closes [#931])

[#1068]: https://github.com/benmarte/daedalus/pull/1068
[#1067]: https://github.com/benmarte/daedalus/pull/1067
[#1066]: https://github.com/benmarte/daedalus/pull/1066
[#1065]: https://github.com/benmarte/daedalus/pull/1065
[#1063]: https://github.com/benmarte/daedalus/pull/1063
[#1049]: https://github.com/benmarte/daedalus/pull/1049
[#1033]: https://github.com/benmarte/daedalus/pull/1033
[#1031]: https://github.com/benmarte/daedalus/pull/1031
[#1027]: https://github.com/benmarte/daedalus/pull/1027
[#1006]: https://github.com/benmarte/daedalus/pull/1006
[#985]: https://github.com/benmarte/daedalus/pull/985
[#1005]: https://github.com/benmarte/daedalus/pull/1005
[#984]: https://github.com/benmarte/daedalus/pull/984
[#954]: https://github.com/benmarte/daedalus/pull/954
[#941]: https://github.com/benmarte/daedalus/pull/941
[#1034]: https://github.com/benmarte/daedalus/issues/1034
[#1053]: https://github.com/benmarte/daedalus/issues/1053
[#1054]: https://github.com/benmarte/daedalus/issues/1054
[#1055]: https://github.com/benmarte/daedalus/issues/1055
[#1057]: https://github.com/benmarte/daedalus/issues/1057
[#1052]: https://github.com/benmarte/daedalus/issues/1052
[#1058]: https://github.com/benmarte/daedalus/issues/1058
[#1059]: https://github.com/benmarte/daedalus/issues/1059
[#1060]: https://github.com/benmarte/daedalus/issues/1060
[#1061]: https://github.com/benmarte/daedalus/issues/1061
[#1062]: https://github.com/benmarte/daedalus/issues/1062
[#994]: https://github.com/benmarte/daedalus/issues/994
[#1008]: https://github.com/benmarte/daedalus/issues/1008
[#955]: https://github.com/benmarte/daedalus/issues/955
[#957]: https://github.com/benmarte/daedalus/issues/957
[#953]: https://github.com/benmarte/daedalus/issues/953
[#931]: https://github.com/benmarte/daedalus/issues/931

---

### Pipeline Reliability (legacy, rolled into prior releases)

- Validator no longer completes silently with `summary=None` when Claude Code delegation fails ([#952], closes [#916])
- `advance hook` registered to postinstall so it ships with the plugin ([#950], closes [#936])
- Auto-advance sub-issues to Ready after planner decomposition ([#937], closes [#915])
- E2E regression assertions for #891 and #894 ([#917], closes [#902])
- Dry-run mode flag for dispatcher ([#914], closes [#900])
- Multi-tick pipeline harness ([#913], closes [#901])
- Agents no longer silently fail when `GITHUB_TOKEN` not set in cron env ([#895], closes [#894])
- Concurrent dispatcher ticks no longer re-decompose epics ([#893], closes [#891])

[#954]: https://github.com/benmarte/daedalus/pull/954
[#952]: https://github.com/benmarte/daedalus/pull/952
[#950]: https://github.com/benmarte/daedalus/pull/950
[#941]: https://github.com/benmarte/daedalus/pull/941
[#937]: https://github.com/benmarte/daedalus/pull/937
[#917]: https://github.com/benmarte/daedalus/pull/917
[#914]: https://github.com/benmarte/daedalus/pull/914
[#913]: https://github.com/benmarte/daedalus/pull/913
[#895]: https://github.com/benmarte/daedalus/pull/895
[#893]: https://github.com/benmarte/daedalus/pull/893

---

## [1.0.0-beta.31] — 2026-06-29

### ⚠️ Notable behavior changes

- **QA gate on auto-merge**: The auto-merge monitor now requires an explicit
  QA-passed signal before merging. PRs must have either the `qa-passed` label
  OR a successful `daedalus/qa` status check. PRs with the `skip-qa` label
  bypass the gate (for docs-only changes and emergency hotfixes).
- **Concurrent dispatcher protection**: A process-level `FileLock` mutex at
  dispatcher entry prevents concurrent cron + advance-hook invocations from
  racing. The second invocation exits cleanly instead of corrupting state.
- **New dependency**: `filelock>=3.0` added to requirements.txt and pyproject.toml.

### Added

- **Concurrent dispatch stress tests** (#1029) — regression tests verifying
  concurrent dispatcher invocations produce exactly one task.
- **Monotonic idempotency keys** (#1031) — planner-fallback validator path now
  uses monotonic idempotency keys to prevent duplicate validator creation.
- **QA auto-merge gate** (#1006, closes #998, #999, #1001) — auto-merge monitor
  requires explicit QA-passed signal before merging. Signal: `qa-passed` label
  OR `daedalus/qa` status check = success.
- **Skip-QA label bypass** (#1026, closes #1000, #1003) — PRs with `skip-qa`
  label bypass the QA gate (for docs-only changes and emergency hotfixes).
- **Orphaned card safety nets** (#1005, closes #957) — cards cleaned up if
  GitHub issue moves to Done before pipeline completes.

### Changed

- **Process-level FileLock mutex** (#1025, #1028, closes #1015) — `FileLock`
  acquired at `main()` entry with `timeout=0`; second invocation logs and exits
  cleanly instead of racing.
- **Status-blind guards** (#1027, closes #1008, #988, #993, #994, #995, #996, #997) —
  guards on `_has_downstream_tasks`, `_has_active_pm_consultation`,
  `_count_active_issue_tasks` now ignore task status when checking existence,
  preventing race conditions where tasks exist but are in terminal states.

### Fixed

- **Test quality issues** (#1030) — fixes from PR #1028 review addressing test
  isolation and assertion clarity.
- **Downstream review roles gated behind QA** (#985, closes #955) — fallback path
  no longer bypasses QA gate by parenting all review roles to dev card.
- **Ambiguous 'pass' in approve_signals** (#983, closes #956) — removed ambiguous
  'pass' substring from approve_signals to prevent false approvals on
  reviewer/security cards.
- **Advance hook registration** (#981, closes #962) — advance hook now registered
  per-profile so planner sessions properly advance the pipeline.
- **Validator retry deduplication** (#980, closes #961) — dispatcher processes
  validator retry logic per-issue instead of per-task, eliminating O(N) redundant
  API calls.

### Tests

- **FileLock mutex tests** (#1028) — comprehensive tests for concurrent dispatcher
  protection.
- **Skip-QA bypass tests** (#1026) — tests verifying `skip-qa` label correctly
  bypasses the QA gate.
- **Concurrent dispatch tests** (#1029) — stress tests verifying exactly one task
  produced under concurrent dispatcher invocations.

---

## [1.0.0-beta.30] — 2026-06-26

### ⚠️ Notable behavior change — automatic comment-mirror threading

Daedalus now mirrors every agent comment into a **persistent per-issue thread**
on each notification target (see Added → Notification threading). This is
delivered as a new `comment-mirror` event, and **any catch-all
`cron.notifications` entry** — one with no `events` filter or `events: []` —
**receives it automatically, with no opt-in.**

If you have existing Slack/Discord targets without an `events` filter, they will
start receiving threaded comment mirrors after upgrading. On an active board this
can be a significant increase in message volume. **To exclude it,** list the
events you *do* want on that entry and leave `comment-mirror` out:

```yaml
cron:
  notifications:
    - platform: "Slack"
      target: "slack:C0CHANNEL1"
      events: ["dispatch-summary", "pipeline-failure"]  # no comment-mirror
```

### Added

- **Notification threading** (#126, closes #121) — every daedalus-managed issue
  gets one persistent thread per configured `cron.notifications` target. Agent
  comments on the issue and its linked PR (spec posts, progress, review feedback)
  are mirrored into that thread as replies; PR-open and merge events post replies
  too. Threading is **platform-agnostic**: Slack anchors on `thread_ts`, Discord
  on `message_id`, both captured via `hermes send --json`. No new config keys are
  required — it works automatically with existing notification targets.
  - `daedalus_dispatch_state.json` gains a `threads` key per issue:
    `{\"<issue_number>\": {\"threads\": {\"<target>\": \"<anchor_id>\"},
    \"thread_events\": {\"<target>\": [\"<event_key>\"]}}}`.
  - Cross-tick duplicate suppression — the same event is never posted twice to a
    target.
  - Self-healing anchor — if a thread's root message is deleted, the next event
    posts a fresh root and updates the stored anchor.
  - See [docs/notification-threading.md](docs/notification-threading.md).

### Changed

- **Agent comment header enforcement** (#122, closes #120) — all soul files now
  route comments through `scripts/agent_comment.py`, which enforces the mandatory
  `**Agent: <name>**` header as the first line of every comment. Previously souls
  could omit it; it is now structural. Tooling that parses or filters daedalus
  comments by author can rely on this header always being present.

### Fixed

PR #244 — implement `provider.has_label()` so tier promotion no longer re-labels and re-comments already-Ready issues on every dispatch tick. `sub_issues_of` regex in `core/providers/base.py` widened to accept the same format variants as `EPIC_REF_RE` (with/without colon, hyphens, `part of epic #N`). 16 regression tests added.

- **Standalone test runner** (#124, closes #123) — internal fix to the standalone
  `__main__` test runner. No user-facing impact.

### Caveats / follow-ups (from PR #126 review)

- **Silent opt-in** — catch-all notification entries receive `comment-mirror`
  with no explicit opt-in (documented above). A future opt-out config key is
  under consideration.
- **Per-tick API cost** — `_mirror_issue_threads` fetches each open issue's issue
  and PR comments every tick before dedup. Fine for small boards; may be
  noticeable on large boards with many open issues.
- **Root-failure edge** — if the initial root post fails but a later reply
  succeeds, that reply becomes the thread anchor. Self-heals; rare; not a blocker.

---

## [1.0.0-beta.29] — 2026-06-25

### Added

- **Epic sub-issue creation (Phase 3)** (#151, PR #179) — when the planner agent
  completes its kanban card with `PLANNING COMPLETE:`, the dispatcher automatically
  decomposes the parent epic into sub-issues, posts an idempotency marker
  (`<!-- daedalus:sub-issues:[N1,N2,...] -->`) on the parent, applies the `epic`
  label, and creates triage cards so each sub-issue enters the validator pipeline
  on the next tick. Two decomposition strategies: Case A (checklist items in the
  epic body → one sub-issue per item, capped at 10) and Case B (no checklist →
  three fixed default sub-issues: Research & Scoping, Implementation, Testing &
  Documentation). Sub-issues inherit parent labels (minus `epic`) and add
  `subtask`. `VCSProvider.add_label()` added to base with GitHub implementation
  via the Issues API; no-op on GitLab/Azure DevOps. 18 new tests in
  `tests/test_subissue_creation.py`.

- **Dependency-aware ready-gating** (#139, PR #148) — the dispatch sweep now refuses
  to start a `Ready` issue while any of its blockers are still open, re-checking
  every tick so a dependent auto-unblocks once its blockers' PRs merge. Blockers
  are resolved per-provider (GitHub native dependencies via `blocked_by`, GitLab
  `is_blocked_by` issue links, Azure DevOps `Predecessor` work-item links) all
  merged with a portable `Depends on: #N` body fallback that works on any
  provider. Dispatch summaries gain a new **⛓ Waiting on Dependencies** section
  that surfaces *why* an issue is being held back.

- **Epic-issue detection (Phase 1)** (#138, PR #155) — new `is_epic()` helper in
  `core.providers.base` flags issues as "epic-sized" using three disjunct
  heuristics (≥4 markdown checklist items, `epic` label case-insensitive, or
  body ≥2000 chars). Accepts provider dicts and `IssueSummary` objects; never
  raises. 34 tests in `tests/test_epic_detection.py` cover all three heuristics,
  boundary values, mixed input shapes, and the OR-combination contract. This is
  detection only — dispatcher wiring ships in Phase 2.

### Changed

- **Tier promotion applies Ready board status** (#208, PR #227) — when a sub-issue's
  tier level drops to 0 (all blockers closed), the promotion pass now applies both
  the `Ready` label *and* sets the issue's board status to `Ready`, triggering the
  normal dispatch flow (validator → PM → dev team) on the next tick. Previously only
  the label was applied, requiring manual board-status updates.

- **Planner reads source files for sub-issue context** (#138, PR #242) — when
  decomposing epics into sub-issues, the planner now reads relevant source files
  from the codebase and injects their content into sub-issue bodies to provide
  implementation context. Up to 10 files per sub-issue, 50KB per file, with binary
  detection and path-traversal prevention.

### Fixed

- **Promotion idempotency: implement `has_label` to prevent duplicate labels/comments**
  (#220, PR #244) — `VCSProvider.has_label()` was a no-op returning `False`; the
  GitHub provider now implements it by inspecting the `labels` field from
  `get_issue()`. Tier promotion is now idempotent: already-Ready issues are excluded
  from re-promotion, preventing duplicate `add_label` and `post_issue_comment` calls
  on every dispatcher tick. `sub_issues_of` base-class regex widened to match all
  `EPIC_REF_RE` formats (`Epic #N`, `Epic: #N`, `Part of: #N`, `Part of #N`,
  `Part of epic: #N`, `Part of epic #N`, hyphenated variants). Case-insensitive
  throughout. 16 new regression tests in `tests/test_bugfixes_regex_and_haslabel.py`.

- **GitHub Projects enrollment node-ID retry with backoff** (#236, PR #245) — when
  the GitHub Projects GraphQL API returns a transient failure resolving the node ID
  for an issue, the dispatcher now retries with exponential backoff instead of
  failing the enrollment outright.

- **Separate blocked/stop handlers** (#2075, PR #222) — the dispatcher previously
  handled `blocked:` and `stop:` signals through the same code path. They are now
  separate: `blocked:` cards stay in the pipeline (awaiting human intervention),
  while `stop:` cards trigger the dedicated auto-close path that archives the card
  and closes the issue.

- **Validator retry-cap exhaustion notification deduplication** (#183, PR #226) —
  when a validator exhausts its retry cap, the dispatcher now posts a single
  `validator-retry-cap-exhausted` notification instead of posting once per
  subsequent tick.

- **PM/validator retry-cap → Slack/Discord dual-channel notification** (#378, epics
  #181, PRs #813, #818, #820, #823, #842, #849, #850) — when the PM or validator
  retry cap is exhausted, the dispatcher now fires a `retry-cap-exhausted` alert on
  two independent channels: (a) structured `NotificationPayload` to Slack/Discord
  webhooks via `core/notification_sender.py` (severity=critical,
  non-blocking daemon thread, 10s HTTP timeout), and (b) message-bubble to every
  `hermes send` target subscribed to the `retry-cap-exhausted` event. An
  idempotency marker (`<!-- daedalus:retry-cap-notified -->`) on the issue
  guarantees only one notification per cap-exhaustion. A GitHub comment with role,
  retry-count, error classification, and recovery instructions is also posted.
  Configured via `SLACK_WEBHOOK_URL` / `DISCORD_WEBHOOK_URL` environment variables.
  Notification failures are logged but never block the dispatcher.

- **`_fetch_issues` default limit raised to 100** (#203, PR #224) — the default
  limit for `_fetch_issues()` was 20, which silently truncated boards with >20 open
  issues. Default raised to 100 so larger boards are not silently skipped.

- **`issues_map` miss → fallback `get_issue()` with retry** (#185, PR #221) — when
  the dispatcher's `issues_map` cache misses an issue number, it now falls back to
  a direct `get_issue()` call with retry on transient failure, instead of failing
  outright.
