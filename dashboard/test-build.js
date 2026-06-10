#!/usr/bin/env node
/**
 * Test the content-hashed dashboard bundle.
 *
 * Runs the build and asserts:
 *   (a) manifest.json `entry` matches  ^dist/index-[0-9a-f]+\.js$
 *   (b) the file referenced by `entry` exists on disk
 *   (c) exactly one .js file is present in dist/
 *   (d) re-running the build is deterministic (same source → same hash)
 *
 * Usage: node test-build.js
 */

const fs = require("fs");
const path = require("path");
const { execSync } = require("child_process");

const dashboardDir = __dirname;
const distDir = path.join(dashboardDir, "dist");
const manifestPath = path.join(dashboardDir, "manifest.json");
const buildCmd = "npm run build";

let failures = 0;

function fail(msg) {
  console.error("FAIL: " + msg);
  failures++;
}

function ok(msg) {
  console.log("  OK: " + msg);
}

// ── Run build ────────────────────────────────────────────────────────────
console.log("Running npm run build ...");
try {
  execSync(buildCmd, { cwd: dashboardDir, stdio: "pipe" });
  ok("Build succeeded");
} catch (err) {
  fail("Build failed: " + String(err.stderr || err));
  process.exit(1);
}

// ── Read manifest ────────────────────────────────────────────────────────
let manifest;
try {
  manifest = JSON.parse(fs.readFileSync(manifestPath, "utf-8"));
  ok("manifest.json is valid JSON");
} catch (err) {
  fail("manifest.json not readable or invalid JSON: " + err.message);
  process.exit(1);
}

// ── (a) entry matches pattern ────────────────────────────────────────────
const entryPattern = /^dist\/index-[0-9a-zA-Z]+\.js$/;
if (manifest.entry && entryPattern.test(manifest.entry)) {
  ok("manifest.entry matches ^dist/index-[0-9a-zA-Z]+\\.js$: " + manifest.entry);
} else {
  fail(
    "manifest.entry " + JSON.stringify(manifest.entry) +
    " does not match " + String(entryPattern),
  );
}

// ── (b) entry file exists ────────────────────────────────────────────────
const entryPath = path.join(dashboardDir, manifest.entry);
if (fs.existsSync(entryPath)) {
  ok("entry file exists: " + manifest.entry);
} else {
  fail("entry file does not exist: " + manifest.entry);
}

// ── (c) exactly one bundle in dist/ ───────────────────────────────────────
const distFiles = fs.readdirSync(distDir);
const jsFiles = distFiles.filter(function (f) {
  return f.endsWith(".js");
});

if (jsFiles.length === 1) {
  ok("Exactly one .js file in dist/");
} else {
  fail(
    "Expected 1 .js file in dist/, found " + jsFiles.length +
    ": " + jsFiles.join(", "),
  );
}

// ── (d) determinism: rebuild must produce same hash ──────────────────────
const firstEntry = manifest.entry;
execSync(buildCmd, { cwd: dashboardDir, stdio: "pipe" });

let secondManifest;
try {
  secondManifest = JSON.parse(fs.readFileSync(manifestPath, "utf-8"));
} catch (err) {
  fail("Second build: manifest.json not readable: " + err.message);
  process.exit(1);
}

if (secondManifest.entry === firstEntry) {
  ok("Rebuild is deterministic: same entry " + firstEntry);
} else {
  fail(
    "Rebuild NOT deterministic: " + firstEntry +
    " → " + secondManifest.entry,
  );
}

// ═══════════════════════════════════════════════════════════════════════════
console.log("");
if (failures === 0) {
  console.log("All assertions passed.");
  process.exit(0);
} else {
  console.error(failures + " assertion(s) failed.");
  process.exit(1);
}
