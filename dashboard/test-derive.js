#!/usr/bin/env node
/**
 * Unit test for the deliver-cascade pure helper (deriveMethodFromDeliver).
 *
 * Guards the channel-dropdown bug: the config modal's derive effect must map a
 * saved cron.deliver value to its method, and return "" (never reset) when
 * there is no usable value — so an in-progress method selection is never wiped.
 *
 * Usage: node test-derive.js
 */
const assert = require("assert");
const { deriveMethodFromDeliver } = require("./src/deriveMethod");

const NOTIF = {
  Slack: ["slack:tasks", "slack:dycotomic"],
  Discord: ["discord:#general"],
};

let n = 0;
function check(desc, got, want) {
  n++;
  assert.strictEqual(got, want, `${desc}: expected ${JSON.stringify(want)}, got ${JSON.stringify(got)}`);
  console.log("  OK:", desc);
}

check("slack:tasks → Slack", deriveMethodFromDeliver("slack:tasks", NOTIF), "Slack");
check("discord:#general → Discord", deriveMethodFromDeliver("discord:#general", NOTIF), "Discord");
check("case-insensitive prefix (SLACK:tasks → Slack)", deriveMethodFromDeliver("SLACK:tasks", NOTIF), "Slack");
check("empty deliver → '' (never reset)", deriveMethodFromDeliver("", NOTIF), "");
check("no notifications yet → ''", deriveMethodFromDeliver("slack:tasks", {}), "");
check("unknown method → ''", deriveMethodFromDeliver("telegram:foo", NOTIF), "");
check("bare method key match → Slack", deriveMethodFromDeliver("Slack", NOTIF), "Slack");

console.log("");
console.log(`All ${n} assertions passed.`);
