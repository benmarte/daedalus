/**
 * Provider-aware field metadata for the VCS sections (pure module so it can
 * be unit-tested in plain node — see test-providerFields.js).
 */
var PROVIDERS = ["github", "gitlab", "azuredevops"];

var PROVIDER_LABELS = {
  github: "GitHub",
  gitlab: "GitLab",
  azuredevops: "Azure DevOps",
};

// Notification event types — keep in sync with NOTIFY_EVENTS in
// scripts/daedalus_dispatch.py and dashboard/plugin_api.py.
var NOTIFY_EVENTS = ["doc-report", "dispatch-summary", "pipeline-failure", "pr-ready"];

function repoLabelForProvider(provider) {
  if (provider === "gitlab") return "GitLab project path (e.g. group/project)";
  if (provider === "azuredevops") return "Repository (org/project set below)";
  return "Org/Repo (e.g. org/my-repo)";
}

function repoPlaceholderForProvider(provider) {
  if (provider === "gitlab") return "group/project";
  if (provider === "azuredevops") return "my-repo";
  return "org/my-repo";
}

module.exports = {
  PROVIDERS: PROVIDERS,
  PROVIDER_LABELS: PROVIDER_LABELS,
  NOTIFY_EVENTS: NOTIFY_EVENTS,
  repoLabelForProvider: repoLabelForProvider,
  repoPlaceholderForProvider: repoPlaceholderForProvider,
};
