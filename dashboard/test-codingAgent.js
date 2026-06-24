#!/usr/bin/env node
/**
 * Unit test for the coding-agent pure helpers (codingAgent.js).
 *
 * Guards issue #66: when the coding agent is switched, coding_agent_cmd must be
 * cleared so the new agent picks up its own default instead of inheriting the
 * previous agent's stale CLI command.
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

// shouldResetCmdOnAgentChange — the core of the bug fix.
check("claude-code → opencode resets cmd", ca.shouldResetCmdOnAgentChange("claude-code", "opencode"), true);
check("hermes → claude-code resets cmd", ca.shouldResetCmdOnAgentChange("hermes", "claude-code"), true);
check("claude-code → hermes resets cmd", ca.shouldResetCmdOnAgentChange("claude-code", "hermes"), true);
check("same agent does NOT reset cmd", ca.shouldResetCmdOnAgentChange("claude-code", "claude-code"), false);
check("hermes → hermes does NOT reset cmd", ca.shouldResetCmdOnAgentChange("hermes", "hermes"), false);

// isCliAgent — which agents expose the CLI Command field.
check("claude-code is a CLI agent", ca.isCliAgent("claude-code"), true);
check("codex is a CLI agent", ca.isCliAgent("codex"), true);
check("opencode is a CLI agent", ca.isCliAgent("opencode"), true);
check("hermes is NOT a CLI agent", ca.isCliAgent("hermes"), false);
check("unknown is NOT a CLI agent", ca.isCliAgent("nope"), false);

// defaultCmdFor — must match scripts/daedalus_dispatch.py _CODING_AGENT_DEFAULTS.
check("default for claude-code", ca.defaultCmdFor("claude-code"), "claude -p");
check("default for codex", ca.defaultCmdFor("codex"), "codex exec --full-auto");
check("default for opencode", ca.defaultCmdFor("opencode"), "opencode run");
check("default for hermes is ''", ca.defaultCmdFor("hermes"), "");
check("default for unknown is ''", ca.defaultCmdFor("nope"), "");

console.log("");
console.log(`All ${n} assertions passed.`);
