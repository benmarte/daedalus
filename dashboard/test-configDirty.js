#!/usr/bin/env node
/**
 * Unit test for the config dirty-state helper (configDirty.js).
 *
 * Guards issue #66: the modal shows an "unsaved changes" badge when the edited
 * config diverges from the last-saved snapshot, so users don't lose edits to a
 * re-mount, reload, or second tab.
 *
 * Usage: node test-configDirty.js
 */
const assert = require("assert");
const cd = require("./src/configDirty");

let n = 0;
function check(desc, got, want) {
  n++;
  assert.strictEqual(got, want, `${desc}: expected ${JSON.stringify(want)}, got ${JSON.stringify(got)}`);
  console.log("  OK:", desc);
}

const base = { execution: { coding_agent: "claude-code", coding_agent_cmd: "claude -p" }, vcs: { target_branch: "main" } };

// Not dirty when nothing has changed (deep-equal, ignoring key order).
check("identical config is not dirty", cd.isDirty(base, JSON.parse(JSON.stringify(base))), false);
check("reordered keys are not dirty",
  cd.isDirty({ a: 1, b: 2 }, { b: 2, a: 1 }), false);

// Dirty when a value changes.
const edited = JSON.parse(JSON.stringify(base));
edited.execution.coding_agent_cmd = "cc-rizq";
check("changed cmd is dirty", cd.isDirty(base, edited), true);

// Dirty when the agent changes (and Fix 1 cleared the cmd).
const switched = JSON.parse(JSON.stringify(base));
switched.execution.coding_agent = "opencode";
switched.execution.coding_agent_cmd = "";
check("agent switch + cleared cmd is dirty", cd.isDirty(base, switched), true);

// Missing snapshots → never dirty (nothing loaded yet).
check("null pristine is not dirty", cd.isDirty(null, base), false);
check("null current is not dirty", cd.isDirty(base, null), false);

// Nested array order matters (lists are ordered).
check("reordered array IS dirty", cd.isDirty({ x: [1, 2] }, { x: [2, 1] }), true);

console.log("");
console.log(`All ${n} assertions passed.`);
