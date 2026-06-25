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

// Default CLI command per agent — shown as the input placeholder, auto-filled
// into coding_agent_cmd when the agent is selected, and used by the dispatcher
// when coding_agent_cmd is left blank.
var CODING_AGENT_DEFAULTS = {
  "claude-code": "CLAUDE_CONFIG_DIR=$HOME/.claude claude --dangerously-skip-permissions -p",
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

// The value coding_agent_cmd should take when the agent changes from prevAgent
// to nextAgent: the new agent's default command, which auto-fills the CLI
// Command field (issue #73) so the user doesn't have to look it up. Hermes (and
// any agent without a default) yields "", which clears the field. Returns null
// when the agent did not actually change, so a no-op selection never overwrites
// a command the user just typed (issue #66).
function cmdForAgentChange(prevAgent, nextAgent) {
  if (prevAgent === nextAgent) return null;
  return defaultCmdFor(nextAgent);
}

module.exports = {
  CLI_AGENTS: CLI_AGENTS,
  CODING_AGENT_DEFAULTS: CODING_AGENT_DEFAULTS,
  isCliAgent: isCliAgent,
  defaultCmdFor: defaultCmdFor,
  cmdForAgentChange: cmdForAgentChange,
};
