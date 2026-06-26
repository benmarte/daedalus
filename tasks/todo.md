# Issue #120 — extract reusable functions / reduce duplication

Branch: `fix/issue-120-refactor` (off `dev`). Pure refactor, no behaviour change
(except #6 fixes a latent guard inconsistency).

Priority order (lowest risk first): 2 → 3 → 4 → 5 → 7 → 8 → 9 → 1 → 10 → 6

- [x] #5  `core/util.py::extract_issue_number(text, *, prefer_qualified=False)`; iterate delegates, 10 dispatch sites use it
- [x] #2  dispatch `_resolve_howtos(provider_name, repo, issue_number=0)` — all sites
- [x] #3  dispatch `_unpack_issue(issue)` — all `_*_body()` sites
- [x] #4  dispatch `_get_task_summary(task, slug)` — 3 sites
- [x] #7  dispatch `_build_security_notify_cmds(repo, n, title, targets)` — 2 sites
- [x] #6  dispatch `_prepend_delegation(...)` — 9 body fns; unified guard to `not in ("none","hermes")`
- [x] #8  tests import `_load_dispatch` from conftest (test_dispatch/iterate/daedalus/e2e_smoke)
- [x] #9  `check` added to conftest; imported by 6 suites; `__main__` counters point at conftest
- [x] #1  `scripts/agent_comment.py` (post_comment/post_pr_comment, header enforced); 9 souls call it
- [x] #10 conftest `FakeProvider` is canonical base; test_iterate uses it directly, test_daedalus subclasses it
- [x] new unit tests: test_util.py, test_agent_comment.py, dispatch-helper tests in test_dispatch.py
- [x] full suite green (752 passed); zero new lint errors (json/E741/test_kanban F401 all pre-existing on dev)
