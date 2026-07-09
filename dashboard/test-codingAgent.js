#!/usr/bin/env node
/**
 * Unit test for the coding-agent pure helpers (codingAgent.js).
 *
 * Guards issue #73: when the coding agent is switched, coding_agent_cmd is
 * auto-filled with the new agent's default command (Hermes clears it), so the
 * user doesn't have to look it up. A no-op selection (same agent) returns null
 * and leaves a typed command untouched (issue #66).
 *
 * Usage: node test-codingAgent.js
 */
const assert = require("assert");
const ca = require("./src/codingAgent");

let n = 0;
function check(desc, got, want) {
  n++;
  assert.strictEqual(got, want, `${desc}: expected ${JSON.stringify(want)}, got ${JSON.stringify(got)}`);
  console.log("  OK:", desc);
}

// cmdForAgentChange — auto-fill on switch (issue #73), null on no-op (issue #66).
check("hermes → claude-code fills full command", ca.cmdForAgentChange("hermes", "claude-code"),
  "CLAUDE_CONFIG_DIR=$HOME/.claude claude --dangerously-skip-permissions -p");
check("hermes → codex fills codex default", ca.cmdForAgentChange("hermes", "codex"), "codex exec --full-auto");
check("hermes → opencode fills opencode default", ca.cmdForAgentChange("hermes", "opencode"), "opencode run");
check("claude-code → opencode fills opencode default", ca.cmdForAgentChange("claude-code", "opencode"), "opencode run");
check("claude-code → hermes clears cmd", ca.cmdForAgentChange("claude-code", "hermes"), "");
check("same agent returns null (no overwrite)", ca.cmdForAgentChange("claude-code", "claude-code"), null);
check("hermes → hermes returns null", ca.cmdForAgentChange("hermes", "hermes"), null);

// isCliAgent — which agents expose the CLI Command field.
check("claude-code is a CLI agent", ca.isCliAgent("claude-code"), true);
check("codex is a CLI agent", ca.isCliAgent("codex"), true);
check("opencode is a CLI agent", ca.isCliAgent("opencode"), true);
check("antigravity is a CLI agent", ca.isCliAgent("antigravity"), true);
check("hermes is NOT a CLI agent", ca.isCliAgent("hermes"), false);
check("unknown is NOT a CLI agent", ca.isCliAgent("nope"), false);

// defaultCmdFor — must match scripts/daedalus_dispatch.py _CODING_AGENT_DEFAULTS.
check("default for claude-code", ca.defaultCmdFor("claude-code"),
  "CLAUDE_CONFIG_DIR=$HOME/.claude claude --dangerously-skip-permissions -p");
check("default for codex", ca.defaultCmdFor("codex"), "codex exec --full-auto");
check("default for opencode", ca.defaultCmdFor("opencode"), "opencode run");
check("default for antigravity", ca.defaultCmdFor("antigravity"),
  "agy --print --dangerously-skip-permissions --print-timeout 20m");
check("default for hermes is ''", ca.defaultCmdFor("hermes"), "");
check("default for unknown is ''", ca.defaultCmdFor("nope"), "");

console.log("");
console.log(`All ${n} assertions passed.`);
