/**
 * Coding-agent metadata + pure helpers for the Coding Agent section of the
 * config modal (pure module so it can be unit-tested in plain node — see
 * test-codingAgent.js).
 *
 * Keep CODING_AGENT_DEFAULTS in sync with _CODING_AGENT_DEFAULTS in
 * scripts/daedalus_dispatch.py — the dispatcher falls back to these when
 * coding_agent_cmd is blank, so the placeholders shown here must match.
 */

// Agents that delegate to an external CLI subagent and therefore expose the
// "CLI Command" override field. "hermes" (and any other value) hides it.
var CLI_AGENTS = ["claude-code", "codex", "opencode"];

// Default CLI command per agent — shown as the input placeholder and used by
// the dispatcher when coding_agent_cmd is left blank.
var CODING_AGENT_DEFAULTS = {
  "claude-code": "claude -p",
  "codex": "codex exec --full-auto",
  "opencode": "opencode run",
};

// Whether the CLI Command override field should render for this agent.
function isCliAgent(agent) {
  return CLI_AGENTS.indexOf(agent) !== -1;
}

// Default CLI command for an agent, or "" when it has none.
function defaultCmdFor(agent) {
  return CODING_AGENT_DEFAULTS[agent] || "";
}

// Whether coding_agent_cmd must be cleared when the agent changes from
// prevAgent to nextAgent. Clearing on every real change lets the new agent
// pick up its own default (the dispatcher falls back to CODING_AGENT_DEFAULTS)
// instead of inheriting the previous agent's stale command. Returns false when
// the agent did not actually change, so a no-op selection never wipes a command
// the user just typed.
function shouldResetCmdOnAgentChange(prevAgent, nextAgent) {
  return prevAgent !== nextAgent;
}

module.exports = {
  CLI_AGENTS: CLI_AGENTS,
  CODING_AGENT_DEFAULTS: CODING_AGENT_DEFAULTS,
  isCliAgent: isCliAgent,
  defaultCmdFor: defaultCmdFor,
  shouldResetCmdOnAgentChange: shouldResetCmdOnAgentChange,
};
