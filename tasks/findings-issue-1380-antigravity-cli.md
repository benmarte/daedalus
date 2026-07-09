# Findings — Antigravity CLI (`agy`) as a delegatable coding agent (#1380)

**Status:** enabled (user-directed) — selectable as `coding_agent: antigravity`
**Verdict:** ✅ **viable-with-caveats**
**Author:** developer agent · base branch `dev`

> **Update (enablement).** The two pre-flip gates below were previously kept
> inert via the resolver allow-list. Per an explicit operator decision, the
> resolver now accepts `antigravity` and the dashboard exposes it in the coding-
> agent dropdown, so it is selectable for any role. The two caveats below are
> **still empirically unverified** (no `agy` binary in the build/CI environment):
> keyring auth surviving into a headless worker, and prompt delivery over stdin.
> Both are only exercisable on a host where `agy` is installed and signed in as
> the same OS user the workers run as. Validate there before relying on it in
> production; local same-user spawns are the low-risk first target.

---

## TL;DR

Adding Antigravity (`agy`) as a fourth CLI coding-agent backend is a small,
pattern-following change — `agy` is explicitly "a coding-agent backend in the
same family as `codex` / `claude-code`" and exposes exactly the headless
one-shot mode Daedalus needs (`agy --print` / `agy -p`). The plumbing has been
wired in this PR following the exact `codex`/`opencode` pattern, but it is
**inert** (no role defaults to it) because two things could not be proven in
this environment and **must be confirmed against a real `agy` binary before
enabling it for any role**:

1. **Auth in a headless worker.** Antigravity auth is OS-keyring / browser
   based — there is **no API-key env-var path**. A delegated worker inherits it
   only if a human signed in once via `agy` under the **same OS user** the
   workers run as (analogous to the `~/.git-credentials` pre-seed in
   `provision_roster.sh`). This does **not** hard-block *local* Daedalus
   (workers are local same-user subprocess spawns) but it **does** block any
   SSH / CI / container worker.
2. **Prompt delivery is stdin, docs show positional.** Both worker spawn paths
   (`daedalus-worktree-spawn.sh` and `daedalus-delegate.sh`) pipe the task body
   to the agent's **stdin** (`< "$task"`). The docs only document `agy --print
   '<prompt>'` (positional) and a `--prompt` flag; whether `agy --print` reads
   the prompt from **stdin** when none is given positionally is **unverified**
   (the `agy` binary is not installed here).

Neither caveat is "it doesn't work" — the CLI-level headless mode demonstrably
exists and is designed for exactly this. They are **operational pre-flip
gates**. Because the wiring is additive and inert, it introduces **zero
behaviour change** until a user opts in with `coding_agent: antigravity`.

Sources: the installed Hermes skill at
`~/.hermes/hermes-agent/optional-skills/autonomous-ai-agents/antigravity-cli/`
(`SKILL.md` v0.2.0 + `references/cli-docs.md`), and inspection of the Daedalus
delegation machinery on `dev`.

---

## Research questions

### 1. Headless viability — does `agy --print` run non-interactively to completion?

**Yes at the CLI level.** `agy --print` / `agy -p` is the documented
non-interactive one-shot: "runs the prompt and exits". To make it safe in a
**non-TTY worker terminal** two launch-time overrides matter:

- `--dangerously-skip-permissions` — otherwise a permission prompt
  (`request-review` mode) would block a headless run. This mirrors why the
  `claude-code` default carries `--dangerously-skip-permissions`.
- `--print-timeout 20m` — `agy` has **no `--max-turns`**; a print run is bounded
  by `--print-timeout` (**default `5m`**). Left at 5m, a longer developer run is
  guillotined by the *inner* timeout before it can open a PR, while the *outer*
  wait (`coding_agent_max_wait`, default 3600s) keeps polling for a PR that
  never arrives → a spurious `CODING_AGENT_TIMEOUT` an hour later. Raising the
  inner bound avoids that.

**Wired default invocation** — a dedicated launcher, `daedalus-agy-run.sh`:

```
$HOME/.hermes/plugins/daedalus/scripts/daedalus-agy-run.sh
# which execs:  agy --print "$(cat)" --dangerously-skip-permissions --print-timeout 20m "$@"
```

**Gate #2 — RESOLVED.** The prompt reaches a delegated agent via **stdin** (the
spawn wrappers do `bash -c "$cmd" < task`), but agy takes the prompt
**positionally** (`agy --print '<prompt>'`) and stdin support is undocumented.
Baking a `"$(cat)"` substitution straight into `run_cmd` is unsafe — the
developer path interpolates the command through an *outer* pid-capturing
`bash -c '… exec …'` that would expand `$(cat)` before the `< task` redirect is
in effect (reading the wrong stdin), while the delegate path expands it once, so
no single quoted form is correct on both. The launcher sidesteps this: it is a
single-token command to the interpolation layer, and the substitution runs only
inside it, in a shell whose stdin *is* the piped task. It reads the task and
passes it positionally per the docs, forwarding any injected `--model` after —
so prompt delivery no longer depends on undocumented stdin behavior. Verified by
`test_agy_launcher_passes_piped_task_as_positional_print_prompt`.

### 2. Auth in a worker terminal — the biggest unknown

- Antigravity tries the **OS secure keyring first**, then falls back to
  **browser-based Google sign-in**. `/logout` clears saved creds.
- **No env-var / API-key path** exists (unlike token-in-env `claude-code` /
  `codex`). So the git-cred model is the right analogy: credentials must be
  **pre-seeded once, interactively, under the OS user the workers run as**, and
  the keyring then persists them for later headless runs.
- **Local Daedalus (the common case):** workers are local subprocess spawns
  under the operator's own OS user, so a one-time `agy` sign-in seeds the
  per-user keyring and headless `agy -p` runs reuse it — exactly like
  `provision_roster.sh` seeding `~/.git-credentials`. Practical requirement: the
  login keychain must be **unlocked** (true for a GUI-login session; a
  cron/launchd job under the same user generally inherits it, but this should be
  verified on the target host).
- **SSH / CI / container workers:** the fallback prints an authorization URL and
  expects the code pasted back — **not headless-viable**. On WSL, token storage
  is file-based, so issues there are local-file/session-state, not
  browser-only.

**Conclusion:** auth does **not** block autonomous delegation on a local,
same-user Daedalus host **provided** a human runs `agy` sign-in once and the
keyring is unlocked. It **does** block remote/headless-only hosts. This is the
primary caveat behind the "viable-with-caveats" verdict and must be documented
in `SETUP.md` / `provision_roster.sh` before any role is switched to
`antigravity`.

### 3. Output & completion contract

- `agy -p` writes files to the working tree and exits — same as the other
  agents. Git + PR creation is handled by the surrounding Daedalus flow / the
  inner agent following the task body, not by `agy` itself.
- Output is **plain text only** — **no `--output-format json`**, no result
  envelope (session_id / cost / turns). This is **fine for Daedalus**: the
  developer completion contract is the **PR handshake** (`daedalus-detect-pr.sh`
  polls `gh pr list`), not parsing agent stdout JSON. Non-developer roles relay
  a SOUL verdict line, also not JSON-from-agent.
- Timeout/turn flags: **no `--max-turns`** (no analog to `coding_agent_max_turns`
  — the outer wait bounds the run instead); **`--print-timeout`** (default 5m)
  is the inner bound and conceptually pairs with `coding_agent_max_wait` (raise
  both together for long tasks).

### 4. Model selection

Yes — `--model '<display string>'` maps cleanly to `coding_agent_model`. Exact
strings come from `agy models` (e.g. `'Gemini 3.1 Pro (High)'`,
`'Claude Opus 4.6 (Thinking)'`). A user sets `coding_agent_model` and it passes
through to the agent's `--model` flag, the same as the other backends.

### 5. Failover fit

Yes, with **no special-casing**. `core/provider_failover.py` is pure
name/cmd resolution — `resolve_coding_agent_chain()` validates each entry's
`name` against `VALID_CODING_AGENTS` and otherwise treats every agent
identically (identity, cooldown, dedup all operate on name/account/cmd). Once
`antigravity` is in `VALID_CODING_AGENTS` (done in this PR) it participates in a
`coding_agents:` chain like any other entry:

```yaml
coding_agents:
  - name: claude-code
    cmd: '…'
  - name: antigravity
    cmd: '$HOME/.hermes/plugins/daedalus/scripts/daedalus-agy-run.sh'
```

Transient-trigger attribution (`session limit`, `quota`, `crash`, `timeout`)
already keys off evidence substrings, not agent name, so `agy` failures rotate
correctly.

---

## Exact code touch-points for full integration

All wired in this PR (inert — no role default changed):

| # | Location | Change |
|---|----------|--------|
| 1 | `core/provider_failover.py` `VALID_CODING_AGENTS` | add `"antigravity"` |
| 2 | `core/dispatch/resolvers.py` `_CODING_AGENT_DEFAULTS` | add `"antigravity"` → `daedalus-agy-run.sh` launcher (positional-prompt bridge) |
| 3 | `scripts/daedalus_dispatch.py` `_build_delegation_instructions` | add `if agent == "antigravity":` delegation branch (mirrors `codex`/`opencode`) |
| 4 | `scripts/daedalus_dispatch.py` `_AGENT_SKILL` map | add `"antigravity": "autonomous-ai-agents/antigravity-cli"` |
| 5 | `core/dispatch/bodies.py` `_CLOUD_AGENT_LABELS` | add `"antigravity": "Antigravity"` (delegation-block label) |
| 6 | `templates/daedalus.yaml` `execution:` docs | document the `antigravity` option, its caveats, and the failover-chain enum |

**Default `run_cmd`:** `$HOME/.hermes/plugins/daedalus/scripts/daedalus-agy-run.sh`
(execs `agy --print "$(cat)" --dangerously-skip-permissions --print-timeout 20m "$@"` —
the positional-prompt bridge; see the "Gate #2 — RESOLVED" note above).

The `_CLOUD_AGENT_LABELS` entry (#5) is the touch-point the issue text missed —
without it the delegation block would render the raw `antigravity` string
instead of the `Antigravity` label; the PM flagged this.

---

## Auth-in-headless-worker risk — explicit call-out

**Does it block autonomous delegation?** — **Partially, host-dependent.**

- **Local, same-user host (typical Daedalus deployment): NO**, provided a human
  performs a one-time interactive `agy` sign-in and the OS keyring/login
  keychain is unlocked for the worker's session. Must be documented as a manual
  provisioning step (analogous to git creds).
- **SSH / CI / container / headless-only host: YES** — browser/paste-code auth
  cannot complete unattended and there is no API-key fallback.

The integration originally shipped **inert** for these reasons. It has since
been **enabled** (see the enablement note at the top) per operator decision, so
`antigravity` is now selectable. Before switching a *production* role to it,
still confirm (a) sign-in has been pre-seeded on the target host for the worker
OS user and (b) the stdin prompt-delivery behavior (Q1/Q3). Local same-user
spawns are the safe first target; SSH/CI/container workers remain blocked until
(a) is satisfied.

---

## Recommended next steps (to flip from inert → enabled)

1. Install `agy` on the Daedalus host: `hermes skills install
   official/autonomous-ai-agents/antigravity-cli`, then
   `command -v agy && agy --version`.
2. **Verify stdin delivery:** `printf 'Say hi in one word' | agy --print
   --dangerously-skip-permissions` — confirm it consumes the prompt from stdin
   and exits with output. If it does **not**, the delegation must pass the
   prompt positionally/`--prompt` (a small change to `_spawn_step3` / the spawn
   scripts) — do **not** enable until resolved.
3. **Pre-seed auth:** run `agy` interactively once under the workers' OS user to
   sign in; confirm a subsequent non-interactive `agy --print` reuses the
   keyring with no prompt. Document in `SETUP.md` / `provision_roster.sh`.
4. Only then set `coding_agent: antigravity` (or add it to a `coding_agents:`
   failover chain) for a single non-critical role and soak.

---

## Non-goals honored

- `antigravity` is **not** a default coding agent for any role (opt-in only).
- No refactor of the existing coding-agent delegation machinery — the new branch
  mirrors the `codex`/`opencode` branches verbatim.
