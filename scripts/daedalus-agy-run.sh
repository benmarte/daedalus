#!/usr/bin/env bash
#
# daedalus-agy-run.sh — Antigravity (`agy`) launcher for delegated Daedalus tasks.
#
# WHY THIS SHIM EXISTS
#   The Daedalus spawn wrappers (daedalus-worktree-spawn.sh / daedalus-delegate.sh)
#   deliver the task body to a coding agent over STDIN — the innermost invocation
#   is always `bash -c "$cmd" < task`. claude-code (`claude -p`), codex
#   (`codex exec`) and opencode (`opencode run`) all READ the prompt from stdin,
#   so they need no wrapper.
#
#   Antigravity is different: per its skill docs the prompt is a POSITIONAL
#   argument — `agy --print '<prompt>'` — and stdin support is NOT documented.
#   Embedding a `"$(cat)"` command-substitution directly in the configured
#   coding_agent_cmd does NOT work reliably: the developer path interpolates the
#   command through an OUTER pid-capturing `bash -c '… exec …'` that would expand
#   `$(cat)` before the `< task` redirect is even in effect (reading the wrong
#   stdin), while the delegate path expands it once — so no single quoted form is
#   correct on both paths. A dedicated launcher sidesteps that entirely: it is a
#   single-token command to the interpolation layer, and the substitution runs
#   only here, in a shell whose stdin IS the piped task.
#
# BEHAVIOUR
#   Reads the whole task from stdin and passes it as the positional `--print`
#   prompt, exactly as the docs prescribe. Extra args (e.g. an injected
#   `--model <engine>`) are forwarded verbatim after the prompt, matching the
#   documented `agy -p '<prompt>' --model '<engine>'` shape.
#
#   --dangerously-skip-permissions keeps a non-TTY worker from hanging on a
#   permission prompt; --print-timeout 20m overrides agy's 5m default so a long
#   dev run isn't guillotined mid-PR (agy has no --max-turns). `exec` hands our
#   PID to agy so the outer wait-loop's `kill -0` liveness check tracks the agent
#   itself, not this shim.
set -uo pipefail
exec agy --print "$(cat)" --dangerously-skip-permissions --print-timeout 20m "$@"
