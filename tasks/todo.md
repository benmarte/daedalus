# Issue #1121 — Validator inner agent calls kanban complete without summary

## Tasks

- [ ] T1: Write failing test asserting `_validator_body()` with `coding_agent="claude-code"` contains no "Complete your card" or "Block your card" text and uses "Print to stdout:" for each outcome
- [ ] T2: Modify `_validator_body()` in `scripts/daedalus_dispatch.py` to conditionally emit stdout-only instructions when `coding_agent` is configured (inner agent mode)
- [ ] T3: Update `test_validator_kanban_readonly.py` to cover both modes (coding_agent=none keeps old text; coding_agent=claude-code uses new text)
- [ ] T4: Run full test suite; verify T1 passes, no regressions
- [ ] T5: Lint (ruff check + format)
- [ ] T6: Push branch, open PR into dev
