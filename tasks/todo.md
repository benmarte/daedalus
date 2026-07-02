# Issue #1160 — advance-hook dispatch silently dropped on FileLock contention

## Spec (PM spec on GH issue #1160; combines all three fix options)

1. **Marker + holder rerun:** on lock `Timeout`, append the dispatch's scope to a
   rerun marker file next to the lock; the lock holder consumes the marker before
   releasing and runs one scoped `_main_inner(["--repo", scope])` pass per queued
   scope (bounded passes, watchdog still armed).
2. **Race closure:** after writing the marker, retry `acquire(timeout=0)` once — if
   the holder released between our timeout and the marker write, we become the
   holder and our own rerun loop consumes the marker.
3. **Bounded wait:** acquire timeout configurable via `DAEDALUS_LOCK_WAIT` env
   (default 0); the advance hook sets 120 so session-end bursts serialize.
4. **Visibility:** hook logs dispatch output to
   `~/.hermes/logs/daedalus-advance-dispatch.log` instead of /dev/null.

## Acceptance criteria
- [x] Timeout with resolvable scope → marker written, "FileLock already held"
      warning still logged (existing subprocess test depends on it), rc 0.
- [x] Timeout on --history/--self-test/--dry-run invocations → no marker.
- [x] Holder consumes marker before release; scopes deduped; marker removed.
- [x] Rerun passes capped so a re-appearing marker cannot spin forever.
- [x] `DAEDALUS_LOCK_WAIT` respected; invalid/negative values fall back to 0.
- [x] Hook exports DAEDALUS_LOCK_WAIT=120 and captures dispatch output in a log.
- [x] Regression tests cover marker write, holder rerun, dedupe, cap, race retry,
      env parse, hook script content.

## Tasks
- [x] T1: Failing tests in tests/test_issue_1160_lock_rerun.py (10 tests, failed first)
- [x] T2: `_main_inner(argv=None)` pure refactor
- [x] T3: Marker helpers + `_lock_wait_secs()` + rewired `main()`
- [x] T4: scripts/daedalus-advance.sh — lock wait + dispatch log + mkdir -p
- [x] T5: Full test suite green (exit 0); ruff clean on new code (3 pre-existing
      findings in daedalus_dispatch.py confirmed present on origin/dev, untouched)
- [x] T6: Push, PR into dev
