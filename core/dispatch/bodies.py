"""core.dispatch.bodies — agent task-body helpers and how-to constants.

Groups all "what to tell an agent" building blocks that do NOT depend on the
live kanban board, delegation stack, or mutable dispatcher globals:

  how-to string constants  — provider-specific comment / PR / issue-close
                             snippets surfaced in every role body
  template engine          — _load_agent_body_template / _render_agent_body,
                             backed by templates/agent_bodies/*.md
  delegation helpers       — _spawn_step3, _wait_for_agent_cmd, _ROLE_AFTER_SPAWN,
                             _CLOUD_AGENT_LABELS, _INNER_BODY_SEPARATOR,
                             _AGENT_FAILED_NOTE (the spawn + wait building blocks
                             consumed by _build_delegation_instructions in the
                             dispatcher; extracted here because they carry no
                             mutable-global dependency)
  delegation body helpers  — _DELEGATION_MARKER, _ROLE_BODY_MARKER, _ROLE_TMP_PREFIX,
                             _role_from_card, _inner_task_body, _rewrite_delegation_block
                             (pure string-inspection helpers for body structure and
                             marker detection; no kanban, no mutable globals) (PR 4/4)
  _resolve_howtos          — assembles the dict each role body requests
  _pm_consultation_body    — PM consultation body builder (calls _resolve_howtos +
                             _unpack_issue; no kanban, no mutable globals)

Functions that call _build_delegation_instructions or read _CODING_AGENT_MAX_WAIT
(a mutable dispatcher global) STAY in scripts/daedalus_dispatch.py.

Moved from scripts/daedalus_dispatch.py (issue #1153 PR 2/4, PR 4/4).
The dispatcher re-exports every symbol so the public surface is unchanged.
"""

from __future__ import annotations

import logging
from pathlib import Path
from string import Template
from typing import Any, Dict, Optional

from core.dispatch.resolvers import _unpack_issue  # noqa: E402

logger = logging.getLogger("daedalus.dispatch")

# ── How-to string constants ───────────────────────────────────────────────────
# Agents no longer post their own GitHub comments (#894). GITHUB_TOKEN is NOT
# exported into the cron worker environment, so the old urllib/token snippets
# raised KeyError and the progress comment was silently dropped. Instead, the
# dispatcher mirrors each role's kanban completion summary to the issue via its
# already-authenticated provider (see ``_post_completion_comments``). The
# how-to string below therefore just tells the agent NOT to post — and to write
# a clear kanban summary, which the dispatcher posts on its behalf.
_AGENT_COMMENT_NOTE = (
    "the dispatcher — it automatically posts your completion summary to the "
    "issue when your kanban card completes, so you do NOT post GitHub comments "
    "yourself. Just write a clear kanban completion summary stating your role, "
    "your findings/decision, and the next steps"
)
_PR_COMMENT_HOWTO = {
    "github": _AGENT_COMMENT_NOTE,
    "gitlab": _AGENT_COMMENT_NOTE,
    "azuredevops": _AGENT_COMMENT_NOTE,
}

_CLOSE_ISSUE_HOWTO = {
    "github": (
        "PATCH https://api.github.com/repos/{repo}/issues/{n} "
        "-H 'Authorization: Bearer $GITHUB_TOKEN' "
        "-H 'Accept: application/vnd.github+json' "
        '-d \'{{"state":"closed","state_reason":"{reason}"}}\''
    ),
    "gitlab": (
        "PUT /api/v4/projects/<project-id>/issues/{n} "
        "-H 'PRIVATE-TOKEN: $GITLAB_TOKEN' "
        '-d \'{{"state_event":"close"}}\''
    ),
    "azuredevops": (
        "PATCH .../workitems/{n} (Basic auth with $AZURE_DEVOPS_PAT) "
        '-d \'[{{"op":"add","path":"/fields/System.State","value":"Done"}}]\''
    ),
}

_PR_CREATE_HOWTO = {
    "github": (
        "the GitHub API. "
        "IMPORTANT: use execute_code(language='python') — do NOT use curl/terminal for this, "
        "as PR body markdown breaks shell escaping. "
        "WARNING: pr_body below is PLAIN MARKDOWN TEXT — do NOT set it to JSON or a dict. "
        "Python example:\n"
        "```python\n"
        "import os, urllib.request, json\n"
        "# pr_body is PLAIN MARKDOWN — not JSON, not a dict, just text\n"
        "pr_body = 'Closes #<issue_number>\\n\\n## Problem\\n<describe>\\n\\n## Fix\\n<what changed>\\n\\n## How to test\\n<steps>'\n"
        "payload = {{'title': '<title>', 'head': '<branch>', 'base': '<base>', 'body': pr_body}}\n"
        "req = urllib.request.Request(\n"
        "    'https://api.github.com/repos/{repo}/pulls',\n"
        "    data=json.dumps(payload).encode(),\n"
        "    headers={{'Authorization': f'Bearer {{os.environ[\"GITHUB_TOKEN\"]}}',\n"
        "             'Accept': 'application/vnd.github+json'}}, method='POST')\n"
        "resp = json.loads(urllib.request.urlopen(req).read())\n"
        "print('PR URL:', resp['html_url'], 'PR number:', resp['number'])\n"
        "```\n"
        "— pr_body MUST be a plain markdown string starting with 'Closes #<issue_number>' on its own line; "
        "NEVER set pr_body to json.dumps(...) or a dict"
    ),
    "gitlab": (
        "the GitLab API. "
        "IMPORTANT: use execute_code(language='python') — do NOT use curl/terminal. "
        "WARNING: description below is PLAIN MARKDOWN TEXT — do NOT set it to JSON or a dict. "
        "Python example:\n"
        "```python\n"
        "import os, urllib.request, json\n"
        "# description is PLAIN MARKDOWN — not JSON, not a dict, just text\n"
        "description = 'Closes #<issue_number>\\n\\n## Problem\\n<describe>\\n\\n## Fix\\n<what changed>\\n\\n## How to test\\n<steps>'\n"
        "payload = {{'source_branch': '<branch>', 'target_branch': '<base>',\n"
        "            'title': '<title>', 'description': description}}\n"
        "req = urllib.request.Request(\n"
        "    'https://gitlab.com/api/v4/projects/<project-id>/merge_requests',\n"
        "    data=json.dumps(payload).encode(),\n"
        "    headers={{'PRIVATE-TOKEN': os.environ['GITLAB_TOKEN'],\n"
        "             'Content-Type': 'application/json'}}, method='POST')\n"
        "resp = json.loads(urllib.request.urlopen(req).read())\n"
        "print('MR URL:', resp['web_url'], 'MR number:', resp['iid'])\n"
        "```\n"
        "— description MUST be a plain markdown string starting with 'Closes #<issue_number>' on its own line; "
        "NEVER set description to json.dumps(...) or a dict"
    ),
    "azuredevops": (
        "the Azure DevOps API. "
        "IMPORTANT: use execute_code(language='python') — do NOT use curl/terminal. "
        "WARNING: description below is PLAIN MARKDOWN TEXT — do NOT set it to JSON or a dict. "
        "Python example:\n"
        "```python\n"
        "import os, urllib.request, json, base64\n"
        "pat = os.environ['AZURE_DEVOPS_PAT']\n"
        "auth = base64.b64encode(f':{pat}'.encode()).decode()\n"
        "# description is PLAIN MARKDOWN — not JSON, not a dict, just text\n"
        "description = 'Fixes #<issue_number>\\n\\n## Problem\\n<describe>\\n\\n## Fix\\n<what changed>\\n\\n## How to test\\n<steps>'\n"
        "payload = {{'title': '<title>', 'sourceRefName': 'refs/heads/<branch>',\n"
        "            'targetRefName': 'refs/heads/<base>', 'description': description}}\n"
        "req = urllib.request.Request(\n"
        "    'https://dev.azure.com/<org>/<project>/_apis/git/repositories/<repo>/pullrequests?api-version=7.1',\n"
        "    data=json.dumps(payload).encode(),\n"
        "    headers={{'Authorization': f'Basic {{auth}}', 'Content-Type': 'application/json'}}, method='POST')\n"
        "resp = json.loads(urllib.request.urlopen(req).read())\n"
        "print('PR URL:', resp.get('url'), 'PR ID:', resp.get('pullRequestId'))\n"
        "```\n"
        "— description MUST be a plain markdown string starting with 'Fixes #<issue_number>' on its own line; "
        "NEVER set description to json.dumps(...) or a dict"
    ),
}


# ── Template engine ───────────────────────────────────────────────────────────
# Locate templates/agent_bodies/ relative to the plugin root.
# bodies.py lives at <plugin_root>/core/dispatch/bodies.py, so:
#   parent     = core/dispatch/
#   parent.parent = core/
#   parent.parent.parent = <plugin_root>/
_AGENT_BODY_TEMPLATE_DIR = (
    Path(__file__).resolve().parent.parent.parent / "templates" / "agent_bodies"
)
_AGENT_BODY_CACHE: Dict[str, str] = {}


def _load_agent_body_template(name: str) -> str:
    """Load and cache the raw text of ``templates/agent_bodies/<name>.md``."""
    if name in _AGENT_BODY_CACHE:
        return _AGENT_BODY_CACHE[name]
    path = _AGENT_BODY_TEMPLATE_DIR / f"{name}.md"
    if not path.is_file():
        raise FileNotFoundError(f"agent-body template not found: {path}")
    text = path.read_text(encoding="utf-8")
    _AGENT_BODY_CACHE[name] = text
    return text


def _render_agent_body(name: str, **variables: Any) -> str:
    """Render ``templates/agent_bodies/<name>.md`` with ``string.Template``.

    Uses ``$placeholder`` syntax (``$$`` for a literal ``$``).  Literal braces
    are safe — they are NOT special in ``string.Template``.  Missing
    placeholders raise ``KeyError`` (via ``.substitute``), never silently
    leave ``$name`` in the output.
    """
    tmpl = _load_agent_body_template(name)
    return Template(tmpl).substitute(variables)


# ── Delegation helpers ────────────────────────────────────────────────────────
_AGENT_FAILED_NOTE = (
    "If that output contains 'CODING_AGENT_DIED' or 'CODING_AGENT_TIMEOUT', the coding agent "
    "failed to produce a result — do NOT proceed, do NOT attempt to implement or investigate "
    "the issue yourself, and do NOT complete your card. "
    'Block it with kanban_block("coding-agent-failed: <CODING_AGENT_DIED|CODING_AGENT_TIMEOUT> — see stderr above") '
    "and STOP. The dispatcher will retry automatically on your next session end."
)


def _wait_for_agent_cmd(
    pfx: str, issue_number: int, max_wait: int, detect_pr: bool = False
) -> str:
    """Build the bounded, liveness-guarded wait command for a spawned coding agent.

    Polls for the agent's output file, but unlike the old ``until [ -s out ]``
    loop it ALSO (a) checks the spawned PID with ``kill -0`` and bails the moment
    the process is gone with no output, and (b) enforces a ``max_wait`` wall-clock
    ceiling. On either failure it prints a ``CODING_AGENT_DIED`` /
    ``CODING_AGENT_TIMEOUT`` marker plus the stderr tail so the death reason
    (OOM / auth / crash) is visible (issue #141). The whole command is a single
    line so it drops straight into a ``terminal("...")`` call.

    When ``detect_pr`` is set (developer role only), each poll also runs
    ``daedalus-detect-pr.sh``: if the coding agent has already opened a PR for its
    branch but hasn't exited/emitted the handshake line, the helper writes that
    line to ``out`` and kills the agent, so the card advances to review instead of
    sitting ``running`` until the timeout and then retrying into a duplicate PR
    (issue #146). The helper is a quiet no-op when no PR exists yet, so the
    liveness/timeout backstop below is unchanged for every other case.
    """
    out = f"/tmp/{pfx}-{issue_number}-out.txt"
    err = f"/tmp/{pfx}-{issue_number}-err.txt"
    pid = f"/tmp/{pfx}-{issue_number}-pid.txt"
    detect = "$HOME/.hermes/plugins/daedalus/scripts/daedalus-detect-pr.sh"
    # Pass the deterministic branch so detection is race-free (the developer runs
    # in an isolated worktree on fix/issue-<N>; reading the shared HEAD would
    # report another concurrent agent's branch — the #1131 cross-wire loop).
    detect_branch = f"fix/issue-{issue_number}" if issue_number else ""
    # No double quotes anywhere — this whole string is embedded inside a
    # terminal("...") call, so a literal " would terminate it early. An empty or
    # stale PID makes ``kill -0`` exit non-zero (treated as dead), which is the
    # behavior we want, so the unquoted $P needs no -z guard.
    #
    # The PR-detection step runs FIRST each iteration and may populate {out}
    # (and kill the agent). The ``[ -s {out} ] && break`` right after it exits the
    # loop without consuming a 30s sleep when a PR was just found. {out} is a
    # space-free /tmp path so it needs no quoting.
    detect_step = (
        f"bash {detect} {out} {pid} {detect_branch} 2>/dev/null; [ -s {out} ] && break; "
        if detect_pr
        else ""
    )
    return (
        f"P=$(cat {pid} 2>/dev/null); S=$SECONDS; "
        f"while [ ! -s {out} ]; do "
        f"{detect_step}"
        f"if ! kill -0 $P 2>/dev/null; then "
        f"echo CODING_AGENT_DIED: agent exited without writing output. stderr tail:; "
        f"tail -n 40 {err} 2>/dev/null; break; fi; "
        f"if [ $((SECONDS-S)) -ge {max_wait} ]; then "
        f"echo CODING_AGENT_TIMEOUT: exceeded {max_wait}s with no output. stderr tail:; "
        f"tail -n 40 {err} 2>/dev/null; break; fi; "
        f"sleep 30; done; cat {out} 2>/dev/null"
    )


# Templates keyed by role. ``{wait_cmd}`` (the bounded liveness-guarded wait) and
# ``{failed_note}`` are filled in by ``_build_delegation_instructions`` along with
# ``{pfx}``/``{issue_number}`` so each concurrent task reads/writes an isolated
# /tmp pair (issue #114) and fails fast on a dead agent (issue #141).
_ROLE_AFTER_SPAWN: Dict[str, str] = {
    "developer": (
        '  4. Wait for the coding agent to finish: terminal("{wait_cmd}")\n'
        "  4b. {failed_note}\n"
        "  5. On success the agent will have opened a PR and output: 'PR URL: ... PR number: <n>'\n"
        "  5b. If the output does NOT contain 'PR URL:', the inner agent ran but failed to open a PR — "
        'block the card with kanban_block("coding-agent-failed: inner agent produced no PR URL") and STOP. '
        "Do NOT attempt to implement or fix the issue yourself.\n"
        '  6. Block your card: kanban_block("review-required: PR #<n> — <branch>")\n'
        "  STOP — do NOT open the PR yourself and do NOT attempt the implementation yourself. "
        "Wait for coding agent output, then block with the real PR number. "
        "The task body below is for the INNER coding agent only.\n"
    ),
    "validator": (
        '  4. Wait for the coding agent: terminal("{wait_cmd}")\n'
        "  4b. {failed_note}\n"
        "  5. On success the inner agent will have printed its verdict as the last line of stdout.\n"
        "  6. Complete YOUR kanban card with the exact verdict line from the output: 'CONFIRMED: <reason>' or 'BLOCKED: <reason>' or 'ALREADY_FIXED: <reason>'\n"
        "  STOP — do NOT investigate the issue yourself. Do NOT call kanban_block unless the inner agent failed.\n"
    ),
    "pm": (
        '  4. Wait for the coding agent: terminal("{wait_cmd}")\n'
        "  4b. {failed_note}\n"
        '  5. On success the agent will have posted the spec to GitHub and output "spec: <summary>".\n'
        "  6. Complete your card with: 'spec: <one-line summary from the output>'\n"
        "  STOP — do not write the spec yourself.\n"
    ),
    "qa": (
        '  4. Wait for the coding agent: terminal("{wait_cmd}")\n'
        "  4b. {failed_note}\n"
        "  5. On success the agent will have posted a QA report to GitHub and output its verdict.\n"
        "  6. Complete your card: 'qa-passed: PR #N' or block with 'qa-failed: <reason>'\n"
        "  STOP — do not run the tests yourself.\n"
    ),
    "reviewer": (
        '  4. Wait for the coding agent: terminal("{wait_cmd}")\n'
        "  4b. {failed_note}\n"
        "  5. On success the agent will have posted review findings to GitHub and output its verdict.\n"
        "  6. Complete your card: 'reviewed: approved' or 'reviewed: changes-requested: <reason>'\n"
        "  STOP — do not review the PR yourself.\n"
    ),
    "security": (
        '  4. Wait for the coding agent: terminal("{wait_cmd}")\n'
        "  4b. {failed_note}\n"
        "  5. On success the agent will have posted security findings to GitHub and output its verdict.\n"
        "  6. Complete your card: 'security: cleared' or 'security: flagged: <finding>'\n"
        "  STOP — do not audit the PR yourself.\n"
    ),
    "documentation": (
        '  4. Wait for the coding agent: terminal("{wait_cmd}")\n'
        "  4b. {failed_note}\n"
        "  5. On success the agent will have posted the completion report to GitHub.\n"
        "  6. Complete your card: 'docs: posted completion report for PR #N'\n"
        "  STOP — do not write the report yourself.\n"
    ),
}

_CLOUD_AGENT_LABELS: Dict[str, str] = {
    "claude-code": "Claude Code",
    "codex": "Codex",
    "opencode": "OpenCode",
}

# Boundary between the outer delegation wrapper and the inner coding-agent
# prompt (#1241). Block-first bodies end the delegation block with this line
# and the wrapper's step 1 says "copy ONLY the text below it"; body-first
# bodies use the "⚠️  AGENT DELEGATION" marker line itself as the boundary.
# Copying the wrapper into the inner agent's stdin makes it re-delegate
# (spawn a background subagent and exit with no output).
_INNER_BODY_SEPARATOR = (
    "━━━ INNER TASK BODY — write ONLY the text BELOW this line to the task file ━━━"
)


def _spawn_step3(
    pfx: str, issue_number: int, run_cmd: str, role: str, base_branch: str
) -> str:
    """Build the step-3 ``terminal(...)`` spawn line for the delegated coding agent.

    For the developer role the agent is launched inside a dedicated per-issue git
    worktree (branch ``fix/issue-<N>`` forked off freshly-fetched ``origin/<base>``)
    via ``daedalus-worktree-spawn.sh``. This isolates concurrent developers so they
    never share a working tree — the fix for the shared-workdir branch/PR cross-wire
    race (a #1131-style CODING_AGENT_DIED loop). Every worktree forks off *current*
    ``base`` (the wrapper fetches first) to minimise merge conflicts.

    Other roles keep the original in-place spawn (they run against an existing PR
    and do not create branches).
    """
    tmp = f"/tmp/{pfx}-{issue_number}-task.txt"
    outf = f"/tmp/{pfx}-{issue_number}-out.txt"
    errf = f"/tmp/{pfx}-{issue_number}-err.txt"
    pidf = f"/tmp/{pfx}-{issue_number}-pid.txt"
    if role == "developer":
        spawn = (
            "$HOME/.hermes/plugins/daedalus/scripts/daedalus-worktree-spawn.sh "
            f"{issue_number} {base_branch} {tmp} {outf} {errf} {run_cmd}"
        )
        return f"  3. terminal(\"bash -c 'echo $$ > {pidf}; exec {spawn}'\", background=True)\n"
    # Non-developer roles (validator/pm/qa/reviewer/security/accessibility/docs):
    # spawn the SAME one-shot delegate wrapper the developer uses, in
    # `--relay-verdict` mode. The wrapper spawns the coding-agent CLI, waits, and
    # transitions YOUR card by relaying the verdict the agent emits (SOUL signal
    # line + JSON OutcomeRecord) — so the outer model never has to wait/parse/
    # complete the card itself (which weak local models fail at). Substitute
    # <CARD_ID> with your own kanban card id and <BOARD_SLUG> with the board slug.
    return (
        "  3. Spawn the one-shot delegate wrapper in the BACKGROUND, then your\n"
        "     session ENDS (the wrapper owns spawn→wait→transition). Substitute\n"
        "     <CARD_ID> with your kanban card id and <BOARD_SLUG> with the board slug:\n"
        "       terminal('$HOME/.hermes/plugins/daedalus/scripts/daedalus-delegate.sh "
        f"--task-file {tmp} --cmd \"{run_cmd}\" --card <CARD_ID> --board <BOARD_SLUG> "
        f"--out {outf} --relay-verdict', background=True)\n"
        "     Do NOT wait, read the output, or complete the card yourself — "
        "--relay-verdict does it.\n"
    )


# ── How-to resolution ─────────────────────────────────────────────────────────


def _resolve_howtos(
    provider_name: str, repo: str, issue_number: int = 0
) -> Dict[str, str]:
    """Resolve provider-appropriate how-to instruction strings for a role body.

    Returns ``{"comment", "pr_create", "close_completed", "close_wontfix"}`` —
    the same strings each ``_*_body()`` previously built inline. Callers pick the
    keys they need; unused keys are cheap to compute and never emitted.
    """
    comment = _PR_COMMENT_HOWTO.get(provider_name, _PR_COMMENT_HOWTO["github"]).format(
        repo=repo
    )
    pr_create = _PR_CREATE_HOWTO.get(provider_name, _PR_CREATE_HOWTO["github"]).format(
        repo=repo
    )
    close_tmpl = _CLOSE_ISSUE_HOWTO.get(provider_name, _CLOSE_ISSUE_HOWTO["github"])
    return {
        "comment": comment,
        "pr_create": pr_create,
        "close_completed": close_tmpl.format(
            repo=repo, n=issue_number, reason="completed"
        ),
        "close_wontfix": close_tmpl.format(
            repo=repo, n=issue_number, reason="not_planned"
        ),
    }


# ── PM consultation body ──────────────────────────────────────────────────────


def _pm_consultation_body(
    repo: str,
    issue: Dict[str, Any],
    blocker_summary: str,
    workdir: str,
    provider_name: str,
) -> str:
    """Task body for a PM consultation when a team member hits a technical blocker."""
    n, title, _, _ = _unpack_issue(issue)
    comment_howto = _resolve_howtos(provider_name, repo, n)["comment"]
    return (
        f"You are the PRODUCT MANAGER responding to a TEAM BLOCKER on issue {repo}#{n}: {title}\n"
        f"Work in the existing git repo at {workdir}.\n\n"
        f"A team member has been blocked and cannot proceed without PM clarification.\n"
        f"Blocker reported: {blocker_summary}\n\n"
        f"⛔ DO NOT write code. Your role is to unblock the team with product/design decisions.\n\n"
        f"Steps:\n"
        f"   a) Read the blocker summary and the original issue #{n} carefully.\n"
        f"   b) Post a clarification comment on issue #{n} via: {comment_howto}\n"
        f"      Your comment must:\n"
        f"      - Address the specific blocker described above\n"
        f"      - Make a concrete product decision (not 'it depends')\n"
        f"      - Reference acceptance criteria from the spec if applicable\n"
        f"   c) If the blocker reveals a product-level ambiguity: update the spec comment "
        f"on issue #{n} with the new decision.\n"
        f"   d) Complete your card with summary starting 'CLARIFIED: ' followed by a "
        f"1-sentence description of the decision made.\n\n"
        f"If this blocker cannot be resolved without human input (requires legal, compliance, "
        f"or C-level sign-off), complete your card with 'ESCALATED: ' and explain why.\n"
    )


# ── Delegation body helpers (PR 4/4) ─────────────────────────────────────────
# Pure string-inspection helpers for body structure and marker detection.
# These carry no kanban dependency and no mutable dispatcher globals, so they
# sit here alongside the other delegation building blocks.
#
# _build_delegation_instructions, _prepend_delegation, _apply_coding_agent_failover,
# and _build_failover_context STAY in scripts/daedalus_dispatch.py because they read
# _CODING_AGENT_MAX_WAIT (mutable dispatcher global) or call _build_delegation_instructions
# (creating a circular import if moved).

# Sentinel string that begins every agent-delegation block injected into task bodies.
_DELEGATION_MARKER = "⚠️  AGENT DELEGATION — USE"

# Sentinel string present at the start of every role task body ("You are the ...").
_ROLE_BODY_MARKER = "You are the "

# Per-role tmp-file prefix used by _build_delegation_instructions when naming the
# /tmp/<pfx>-<issue>-{task,out,err}.txt files for a spawned coding agent.
_ROLE_TMP_PREFIX: Dict[str, str] = {
    "pm": "pm",
    "developer": "dev",
    "validator": "validator",
    "qa": "qa",
    "reviewer": "rev",
    "security": "sec",
    "documentation": "docs",
    "accessibility": "a11y",
    "planner": "planner",
}


def _role_from_card(card: Dict[str, Any]) -> str:
    """Best-effort pipeline role of a kanban card (title prefix, then assignee)."""
    title = (card.get("title") or "").lower()
    assignee = (card.get("assignee") or "").lower()
    for role in _ROLE_TMP_PREFIX:
        if f"{role}:" in title or role in assignee:
            return role
    return "developer"


def _inner_task_body(body: str) -> str:
    """Extract the inner coding-agent prompt from a full card body (#1241).

    Mirrors the copy instruction in the delegation block's step 1: block-first
    bodies yield everything below the ``_INNER_BODY_SEPARATOR`` line; body-first
    bodies (appended block) yield everything above the ``_DELEGATION_MARKER``
    line. A body without a delegation block is returned unchanged. This is the
    single place the boundary contract is encoded — golden tests assert the
    result never contains the delegation wrapper.
    """
    marker_idx = body.find(_DELEGATION_MARKER)
    if marker_idx == -1:
        return body
    sep_idx = body.find(_INNER_BODY_SEPARATOR)
    if sep_idx > marker_idx:  # block first — inner body sits below the separator
        return body[sep_idx + len(_INNER_BODY_SEPARATOR):].lstrip("\n")
    return body[:marker_idx].rstrip("\n")  # body first — block appended after it


def _rewrite_delegation_block(body: str, block: str) -> Optional[str]:
    """Replace (or insert) the delegation block in *body* with *block*.

    Handles both compositions ``_prepend_delegation`` produces (block before
    the role body, or appended after it). An empty *block* strips delegation
    entirely (fallback agent is hermes — the brain codes directly). Returns
    None when the body shape is unrecognized — refuse rather than corrupt.
    """
    role_idx = body.find(_ROLE_BODY_MARKER)
    if role_idx == -1:
        return None
    marker_idx = body.find(_DELEGATION_MARKER)
    if marker_idx == -1 or marker_idx < role_idx:
        rest = body[role_idx:]
        return (block + "\n\n" + rest) if block else rest
    head = body[:marker_idx].rstrip("\n")
    return (head + block + "\n\n") if block else head + "\n"
