# Runbook — Native Hermes autonomous issue→PR pipeline (dycotomic)

Status: **A (plugin removed) ✅ · C (roster) ✅ · B1 (spike) ⏸ held — awaiting issue pick ·
B4 (cron) ⏳ after B1.** This runbook makes B1 copy-paste resumable.

## Verified facts
- **Repo:** `RIZQ-TECH/dycotomic.app` · **local workdir:** `/Users/benmarte/Documents/github/rizq/dycotomic`
- **GitHub Project:** #1 "Dycotomic Web App" · node id `PVT_kwDODzyWm84BST_-`
- **Status field id:** `PVTSSF_lADODzyWm84BST_-zg_4mQU` · options:
  `Backlog=f75ad846 · Ready=61e4505c · In progress=47fc9ee4 · In review=df73e18b · Done=98236657`
- **Hermes Kanban board slug:** `rizq-tech-dycotomic-app` (empty)
- **Roster profiles:** project-manager, planner, developer, reviewer, security-analyst, documentation
  (each has matrix agent-skills + cloned keys + `GITHUB_TOKEN`; provisioned by
  `scripts/provision_roster.sh`, re-runnable).
- **Decision ceiling:** agents stop at a green, conflict-free, reviewer+security-approved PR →
  GitHub status `In review`; **a human merges** (sets `Done`).

## B1 — spike one issue (set `ISSUE=<n>` after you promote it to Ready)
```bash
ISSUE=233                      # whichever you pick
WORKDIR=/Users/benmarte/Documents/github/rizq/dycotomic
gh issue view "$ISSUE" --repo RIZQ-TECH/dycotomic.app --json title,body   # sanity

# 1) point Hermes at the dycotomic board
hermes kanban boards switch rizq-tech-dycotomic-app

# 2) create the triage card, worktree pinned to the PROJECT repo (Phase-0 blocker #1)
hermes kanban create "issue #$ISSUE: <title>" \
  --triage \
  --body "$(gh issue view "$ISSUE" --repo RIZQ-TECH/dycotomic.app --json title,body \
            --jq '.title + "\n\n" + .body')  \n\nAcceptance: PR with green CI, no conflicts, reviewer+security approved." \
  --workspace "worktree:$WORKDIR/.worktrees/issue-$ISSUE" \
  --branch "feat/issue-$ISSUE" \
  --tenant dycotomic \
  --max-runtime 2h --max-retries 2
# --triage → the specifier fleshes out the spec and promotes to todo; the decomposer then routes
# child tasks to the roster BY PROFILE DESCRIPTION. If Auto routing doesn't fan out across the 6
# roles (Phase-0 open Q1), fall back to a pre-shaped chain: create planner→developer→reviewer→
# security-analyst→documentation cards with --parent + --assignee + the same --workspace.

# 3) mirror up: GitHub Project Ready → In progress  (ITEM_ID = project item id, not issue #)
ITEM_ID=$(gh project item-list 1 --owner RIZQ-TECH --format json -L 200 \
  | python3 -c "import sys,json;print(next(i['id'] for i in json.load(sys.stdin)['items'] if (i.get('content') or {}).get('number')==$ISSUE))")
gh project item-edit --id "$ITEM_ID" --project-id PVT_kwDODzyWm84BST_- \
  --field-id PVTSSF_lADODzyWm84BST_-zg_4mQU --single-select-option-id 47fc9ee4   # In progress

# 4) ensure a gateway/dispatcher is running so cards get claimed
hermes gateway status   # or: hermes gateway (foreground) / hermes gateway install

# 5) watch: hermes kanban list / show; tail ~/.hermes/logs/gateway.log
#    On PR open → mirror In review (option df73e18b). STOP there; human merges → Done (98236657).
```

### Exit criteria (capture into PLAN Phase-0 findings)
One issue → green-checks, approved, **ready-to-merge** PR with zero daedalus-plugin code in the
loop; Project #1 shows `In review`. Record: did `--triage`+decomposer fan out across 6 roles or did
we pre-shape? did `developer` reliably drive CI green (`gh pr checks --watch` loop)? worktree landed
under `$WORKDIR/.worktrees` (not the gateway cwd)?

### B1 RESULTS — validated 2026-06-08
Two runs on issue #292 ("Session timeout"):
- **Run 1 (base main):** triage card **auto-decomposed → developer → reviewer → security-analyst**;
  developer built it (33 tests), reviewer submitted **APPROVE**. Surfaced 4 issues: gh-per-profile
  auth (developer blocked at `gh pr create`), worktree path pin ignored, bundled-skill re-seed
  collision, and **PR opened against `main` → no CI** (ci.yml only runs on PRs to `dev`).
- **Run 2 (base dev, corrected):** pre-built worktree from `origin/dev` + `--workspace dir:<path>`;
  conventions seeded into profile memories. Result: **PR #354 → base `dev`, CI ALL GREEN**
  (frontend-check + frontend-test pass; python-lint/test correctly skipped), **pre-commit clean**,
  **533/533 tests**, MERGEABLE → mirrored GitHub #292 to **In review** for human merge. ✅

**Validated mechanics for B4:**
- Pre-create the worktree from `origin/dev` and pass `--workspace dir:<path>` (do NOT trust
  `worktree:<path>`). Branch off `dev`; PR `--base dev`.
- Authenticate gh per profile HOME (`gh auth login --with-token`) — done in `provision_roster.sh`.
- Conventions live in each profile's `memories/MEMORY.md` (dev-base, PR into dev, run pre-commit).
- The worker tends to `kanban_block` ("review-required") instead of `kanban_complete`; to chain to
  reviewer/security (when decomposed) or close out, run `hermes kanban complete <id>`.
- Decomposer is non-deterministic: a terse triage body fans out to dev→reviewer→security (run 1);
  a prescriptive body gets specified-in-place as a single developer task (run 2). For B4, decide
  whether to pre-shape reviewer/security cards or rely on decomposition.

## B4 — automate (only after B1 passes)
Root-store cron (no `--profile`), every Nm:
1. `gh project item-list 1 --owner RIZQ-TECH` → items with `status == Ready`.
2. For each, idempotently create the triage card (use `--idempotency-key issue-<n>`) + flip the
   GitHub item to `In progress` (option `47fc9ee4`).
3. Reconcile pass: mirror `In review`/`Done` up from Hermes card/PR state.
Build it from the IDs above once B1 confirms the routing + CI-green behavior.

## One-way sync map (Hermes → GitHub, locked)
`card created` → In progress (47fc9ee4) · `PR opened` → In review (df73e18b) ·
`human merge` → Done (98236657). Hermes Kanban = team source of truth; GitHub = issue-level mirror.
