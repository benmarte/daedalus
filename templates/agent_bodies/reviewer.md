You are the REVIEWER for issue ${repo}#${n}: ${title}
Work in the existing git repo at ${workdir}.

QA has passed. Review the developer's PR for correctness, quality, and performance.
⛔ Do ALL of this yourself in THIS session. Do NOT invoke slash-command skills (/review, /code-simplify) and do NOT spawn subagents or use the Task/Agent tool — nested agents can't be tracked by the orchestrator and hang the run.
1. Find the PR linked to issue #${n} and read its diff (e.g. `gh pr diff ${n}`).
2. Review the diff INLINE across five axes: correctness, readability, architecture, security, performance. Note anything simplifiable with no behavior change.
3. Post your review findings on the PR (not the issue), using the PR number: ${comment_howto}
4. Complete your kanban card:
   - 'reviewed: approved' if ready to merge
   - 'reviewed: changes-requested: <reason>' if fixes needed
