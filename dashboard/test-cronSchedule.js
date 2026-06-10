#!/usr/bin/env node
/**
 * Unit tests for cron schedule parse/build helpers.
 *
 * Guards the CronSchedule dropdown builder: parseSchedule must round-trip
 * through buildSchedule for every accepted format, and parse must handle
 * legacy bare intervals.
 *
 * Usage: node test-cronSchedule.js
 */
const assert = require("assert");
const { parseSchedule, buildSchedule } = require("./src/cronSchedule");

let n = 0;
function check(desc, got, want) {
  n++;
  assert.strictEqual(got, want, `${desc}: expected ${JSON.stringify(want)}, got ${JSON.stringify(got)}`);
  console.log("  OK:", desc);
}

function deepCheck(desc, got, want) {
  n++;
  assert.deepStrictEqual(got, want, `${desc}: expected ${JSON.stringify(want)}, got ${JSON.stringify(got)}`);
  console.log("  OK:", desc);
}

// ── parseSchedule: recurring intervals ──────────────────────────────────────

deepCheck("every 60m → Minutes, 60", parseSchedule("every 60m"), {
  unit: "Minutes", n: "60", dow: null, dom: null, hour: null, minute: null, raw: null
});

deepCheck("every 2h → Hours, 2", parseSchedule("every 2h"), {
  unit: "Hours", n: "2", dow: null, dom: null, hour: null, minute: null, raw: null
});

deepCheck("every 3d → Days, 3", parseSchedule("every 3d"), {
  unit: "Days", n: "3", dow: null, dom: null, hour: null, minute: null, raw: null
});

deepCheck("every 5m → Minutes, 5", parseSchedule("every 5m"), {
  unit: "Minutes", n: "5", dow: null, dom: null, hour: null, minute: null, raw: null
});

deepCheck("every 12h → Hours, 12", parseSchedule("every 12h"), {
  unit: "Hours", n: "12", dow: null, dom: null, hour: null, minute: null, raw: null
});

deepCheck("every 7d → Days, 7", parseSchedule("every 7d"), {
  unit: "Days", n: "7", dow: null, dom: null, hour: null, minute: null, raw: null
});

// ── parseSchedule: legacy bare intervals ────────────────────────────────────

deepCheck("bare 60m → Minutes, 60", parseSchedule("60m"), {
  unit: "Minutes", n: "60", dow: null, dom: null, hour: null, minute: null, raw: null
});

deepCheck("bare 2h → Hours, 2", parseSchedule("2h"), {
  unit: "Hours", n: "2", dow: null, dom: null, hour: null, minute: null, raw: null
});

deepCheck("bare 3d → Days, 3", parseSchedule("3d"), {
  unit: "Days", n: "3", dow: null, dom: null, hour: null, minute: null, raw: null
});

// ── parseSchedule: weekly cron expressions ──────────────────────────────────

deepCheck("0 9 * * 1 → Weekly, dow=1, hour=9", parseSchedule("0 9 * * 1"), {
  unit: "Weekly", n: null, dow: "1", dom: null, hour: "9", minute: "0", raw: null
});

deepCheck("30 14 * * 5 → Weekly, dow=5, hour=14, minute=30", parseSchedule("30 14 * * 5"), {
  unit: "Weekly", n: null, dow: "5", dom: null, hour: "14", minute: "30", raw: null
});

deepCheck("0 0 * * 0 → Weekly, Sunday", parseSchedule("0 0 * * 0"), {
  unit: "Weekly", n: null, dow: "0", dom: null, hour: "0", minute: "0", raw: null
});

deepCheck("0 23 * * 6 → Weekly, Saturday", parseSchedule("0 23 * * 6"), {
  unit: "Weekly", n: null, dow: "6", dom: null, hour: "23", minute: "0", raw: null
});

// ── parseSchedule: monthly cron expressions ─────────────────────────────────

deepCheck("0 9 15 * * → Monthly, dom=15, hour=9", parseSchedule("0 9 15 * *"), {
  unit: "Monthly", n: null, dow: null, dom: "15", hour: "9", minute: "0", raw: null
});

deepCheck("30 14 28 * * → Monthly, dom=28", parseSchedule("30 14 28 * *"), {
  unit: "Monthly", n: null, dow: null, dom: "28", hour: "14", minute: "30", raw: null
});

deepCheck("0 0 1 * * → Monthly, dom=1", parseSchedule("0 0 1 * *"), {
  unit: "Monthly", n: null, dow: null, dom: "1", hour: "0", minute: "0", raw: null
});

// ── parseSchedule: custom / unrecognised ────────────────────────────────────

var custom1 = parseSchedule("every 1w");
check("every 1w → Custom", custom1.unit, "Custom");
check("every 1w → raw preserved", custom1.raw, "every 1w");

var custom2 = parseSchedule("");
check("empty → Custom", custom2.unit, "Custom");
check("empty → raw empty", custom2.raw, "");

var custom3 = parseSchedule("*/5 * * * *");
check("complex cron → Custom", custom3.unit, "Custom");
check("complex cron → raw preserved", custom3.raw, "*/5 * * * *");

var custom4 = parseSchedule("daily");
check("human word → Custom", custom4.unit, "Custom");
check("human word → raw preserved", custom4.raw, "daily");

// ── buildSchedule ───────────────────────────────────────────────────────────

check("build Minutes, 60", buildSchedule({ unit: "Minutes", n: "60" }), "every 60m");
check("build Hours, 2", buildSchedule({ unit: "Hours", n: "2" }), "every 2h");
check("build Days, 3", buildSchedule({ unit: "Days", n: "3" }), "every 3d");
check("build Weekly, Monday 9:00", buildSchedule({ unit: "Weekly", dow: "1", hour: "9", minute: "0" }), "0 9 * * 1");
check("build Weekly, Friday 14:30", buildSchedule({ unit: "Weekly", dow: "5", hour: "14", minute: "30" }), "30 14 * * 5");
check("build Monthly, 15th 9:00", buildSchedule({ unit: "Monthly", dom: "15", hour: "9", minute: "0" }), "0 9 15 * *");
check("build Monthly, 28th 14:30", buildSchedule({ unit: "Monthly", dom: "28", hour: "14", minute: "30" }), "30 14 28 * *");
check("build Custom", buildSchedule({ unit: "Custom", raw: "*/5 * * * *" }), "*/5 * * * *");
check("build Custom empty", buildSchedule({ unit: "Custom", raw: "" }), "");

// ── Round-trip: parse → build = identity ────────────────────────────────────

var roundTrips = [
  "every 60m",
  "every 2h",
  "every 3d",
  "every 5m",
  "every 12h",
  "every 7d",
  "0 9 * * 1",
  "30 14 * * 5",
  "0 9 15 * *",
  "30 14 28 * *",
];
roundTrips.forEach(function (s) {
  var rebuilt = buildSchedule(parseSchedule(s));
  check("round-trip: " + s, rebuilt, s);
});

// ── Round-trip for legacy bare intervals (parsed then built as "every Nx") ──

check("round-trip bare 60m → every 60m", buildSchedule(parseSchedule("60m")), "every 60m");
check("round-trip bare 2h → every 2h", buildSchedule(parseSchedule("2h")), "every 2h");
check("round-trip bare 3d → every 3d", buildSchedule(parseSchedule("3d")), "every 3d");

console.log("");
console.log(`All ${n} assertions passed.`);
