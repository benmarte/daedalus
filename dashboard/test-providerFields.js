/**
 * Unit tests for src/providerFields.js — run with `node test-providerFields.js`.
 */
var pf = require("./src/providerFields");

var passed = 0, failed = 0;
function check(name, cond) {
  if (cond) { passed++; console.log("  OK: " + name); }
  else { failed++; console.error("  FAIL: " + name); }
}

check("three providers", pf.PROVIDERS.length === 3 && pf.PROVIDERS[0] === "github");
check("labels cover every provider",
  pf.PROVIDERS.every(function (p) { return !!pf.PROVIDER_LABELS[p]; }));
check("github repo label", pf.repoLabelForProvider("github").indexOf("Org/Repo") === 0);
check("gitlab repo label", pf.repoLabelForProvider("gitlab").indexOf("GitLab project path") === 0);
check("azure repo label", pf.repoLabelForProvider("azuredevops").indexOf("Repository") === 0);
check("unknown provider falls back to github label",
  pf.repoLabelForProvider("bogus") === pf.repoLabelForProvider("github"));
check("placeholders per provider",
  pf.repoPlaceholderForProvider("gitlab") === "group/project"
  && pf.repoPlaceholderForProvider("azuredevops") === "my-repo"
  && pf.repoPlaceholderForProvider("github") === "org/my-repo");
check("notify events", pf.NOTIFY_EVENTS.length === 4
  && pf.NOTIFY_EVENTS.indexOf("doc-report") !== -1
  && pf.NOTIFY_EVENTS.indexOf("pr-ready") !== -1);

console.log("\n" + (failed === 0 ? "All " + passed + " assertions passed." : failed + " FAILED"));
process.exit(failed === 0 ? 0 : 1);
