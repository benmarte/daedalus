# Issue #1141 — Wire verify_signature into the ready-event dispatch path

## Acceptance criteria (from PM spec)
1. Bad sig + secret set → never reaches normalize(), no dispatch (exit 0).
2. Missing sig header + secret set → rejected (no normalize, no dispatch).
3. Valid sig → normalize() + dispatch proceed.
4. No secret → one-time warning (marker), dispatch proceeds; no repeat 2nd event.
5. Raw bytes passed to verify_signature() == exact stdin bytes (cat- byte-mangling regression).
6. `vcs.webhook_secret_env` accepted by validation (non-empty string when present; absence valid).
7. New dual-mode test covers 1-6; all existing tests pass.

## Tasks
- [ ] core/webhook_dispatch.py — handle_ready_event(raw_body, headers, *, secret, ...): infer provider → verify BEFORE normalize → resolve scope. Read raw stdin bytes ONCE in main(). Header extraction from CGI HTTP_* env. One-time no-secret warning via marker file. Secret resolved from vcs.webhook_secret_env across registered projects.
- [ ] config/__init__.py — validate vcs.webhook_secret_env (non-empty str when present).
- [ ] templates/daedalus.yaml — commented webhook_secret_env example.
- [ ] scripts/daedalus-ready.sh — reduce to thin `python3 -m core.webhook_dispatch` wrapper.
- [ ] tests/test_issue_1141_webhook_signature.py — dual-mode (unittest.main + pytest).
- [ ] CHANGELOG.md — entry.

## Out of scope
verify_signature() internals (#1140); standalone daedalus network receiver.
