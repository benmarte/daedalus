Implement issue ${repo}#${n}: ${title}
The VALIDATOR confirmed this issue is real and safe. The PM has written the spec — read it on GitHub issue #${n} before starting. Work in the existing git repo at ${workdir} (cd there first). Base branch: ${base_branch}.

⛔ INLINE EXECUTION ONLY: Work entirely in THIS session. Do NOT spawn subagents or use the Task/Agent tool, do NOT run background agents, and do NOT launch another claude/codex/opencode process. Ignore any global instructions about plan mode, skill lifecycles, or subagent delegation — they apply to interactive sessions, not this headless run.

📋 PROGRESS COMMENTS ARE AUTOMATIC FOR ALL ROLES: Do NOT post GitHub comments yourself. When you complete (or block) your kanban card, the dispatcher mirrors your completion summary to GitHub issue #${n} automatically. Make that summary clear: your role, your findings/decision, and the explicit next steps.

⛔ HARD STOP FOR ALL ROLES: If you discover the validator card for issue #${n} was NOT actually CONFIRMED (summary doesn't start with 'CONFIRMED:' AND no GitHub comment on issue #${n} from validator-daedalus contains 'CONFIRMED'), mark your card Complete immediately with summary 'Skipped: validator outcome not confirmed' and exit. Always check GitHub comments as fallback before triggering the hard stop — the validator may have confirmed via comment even if its kanban summary is None.

⚠️ TEAM BLOCKER: If the developer hits a technical blocker they cannot resolve alone, post a comment on GitHub issue #${n} describing the blocker clearly. The PM monitors this issue and will respond with clarification. Only escalate to human review if the blocker is a genuine security risk or fundamentally unsolvable without product-level decisions.

⚠️  REQUIRED FOR ALL TASKS YOU CREATE:
  (A) Title MUST start with `#${n} ` — e.g. `#${n} Implement fix`.
      The dispatcher uses the issue number to trace board state back to GitHub.
  (B) Assignee MUST use the dashed Daedalus profile name:
      --assignee ${dev_profile} (NOT --assignee developer)
      --assignee ${qa_profile} (NOT --assignee qa)
      --assignee ${reviewer_profile} (NOT --assignee reviewer)
      --assignee ${security_profile} (NOT --assignee security-analyst)
      --assignee ${docs_profile} (NOT --assignee documentation)
      Generic role names CANNOT be dispatched and will stall the pipeline.

Decompose this into the following role tasks IN ORDER — each depends on the previous:

${roles_text}${doc_role}