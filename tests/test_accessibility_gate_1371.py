"""Tests for the accessibility-stage UI gate (#1371).

Gate accessibility-card creation on real UI changes: a backend-only PR (no file
matches the UI fileset) creates NO accessibility card and spawns no accessibility
agent, while a UI-touching PR is unaffected. Docs then degrades to
``(reviewer, security)`` automatically. Follows the plain-Python + ``check()``
pattern of ``test_iterate.py`` and is runnable both under pytest and standalone.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

# Make the package root importable (config/, core/) and the tests dir (conftest).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import conftest  # noqa: E402
from conftest import check  # noqa: E402
from core import iterate  # noqa: E402
from core import kanban  # noqa: E402
from core.file_overlap import is_ui_file, pr_touches_ui  # noqa: E402
from core.iterate import executors  # noqa: E402

# Representative filesets.
BACKEND_FILES = ["core/iterate/executors.py", "__init__.py", "tests/test_x.py"]
UI_FILES = ["dashboard/src/App.jsx", "core/iterate/executors.py"]


class _FilesProvider:
    """Minimal provider double exposing only get_pr_files (#1371)."""

    def __init__(self, files: list[str] | None, *, raises: bool = False) -> None:
        self._files = files
        self._raises = raises
        self.calls: list[int] = []

    def get_pr_files(self, pr_number: int) -> list[dict]:
        self.calls.append(pr_number)
        if self._raises:
            raise RuntimeError("boom")
        return [{"filename": f} for f in (self._files or [])]


# ── unit: is_ui_file / pr_touches_ui ─────────────────────────────────────────


def test_is_ui_file_extensions():
    check("jsx is UI", is_ui_file("dashboard/src/App.jsx"))
    check("scss is UI", is_ui_file("styles/main.scss"))
    check("html is UI", is_ui_file("index.html"))
    check("py is NOT UI", not is_ui_file("core/iterate/executors.py"))
    check("md is NOT UI", not is_ui_file("README.md"))
    check("extension match is case-insensitive", is_ui_file("Main.CSS"))


def test_is_ui_file_globs():
    check("dashboard/src glob", is_ui_file("dashboard/src/components/Btn.py"))
    check("nested components glob", is_ui_file("app/ui/components/Widget.py"))
    check("root components glob", is_ui_file("components/Btn.py"))
    check("stories glob", is_ui_file("foo/bar/Widget.stories.tsx"))
    check("unrelated path not UI", not is_ui_file("core/db.py"))


def test_is_ui_file_empty_and_custom():
    check("empty filename is not UI", not is_ui_file(""))
    check(
        "custom extensions honoured",
        is_ui_file("a.py", extensions=[".py"], globs=[]),
    )
    check(
        "disabling globs drops components match",
        not is_ui_file("components/x.py", extensions=[], globs=[]),
    )


def test_pr_touches_ui():
    check("backend-only PR does not touch UI", not pr_touches_ui(BACKEND_FILES))
    check("UI PR touches UI", pr_touches_ui(UI_FILES))
    check("empty list does not touch UI", not pr_touches_ui([]))
    check("None does not touch UI", not pr_touches_ui(None))


# ── _accessibility_needed: fail-open semantics ───────────────────────────────


def test_accessibility_needed_fail_open():
    check("no provider → keep a11y", executors._accessibility_needed(None, 42))
    check(
        "no pr number → keep a11y",
        executors._accessibility_needed(_FilesProvider(UI_FILES), None),
    )
    check(
        "empty file list → keep a11y",
        executors._accessibility_needed(_FilesProvider([]), 42),
    )
    check(
        "provider error → keep a11y",
        executors._accessibility_needed(_FilesProvider(None, raises=True), 42),
    )


def test_accessibility_needed_true_for_ui():
    prov = _FilesProvider(UI_FILES)
    check("UI PR needs a11y", executors._accessibility_needed(prov, 42) is True)
    check("provider was queried", prov.calls == [42])


def test_accessibility_needed_false_for_backend():
    prov = _FilesProvider(BACKEND_FILES)
    check("backend PR does not need a11y", executors._accessibility_needed(prov, 42) is False)


def test_accessibility_needed_custom_fileset():
    # A .py-only fileset makes a backend PR "need" a11y; UI extensions ignored.
    prov = _FilesProvider(["core/db.py"])
    check(
        "custom ui_extensions detects py",
        executors._accessibility_needed(prov, 42, ui_extensions=[".py"], ui_globs=[]),
    )


# ── _create_downstream_review_tasks: gating ──────────────────────────────────


def test_backend_pr_skips_accessibility_card():
    """AC: backend-only PR creates NO accessibility card; docs degrades."""
    card = {"id": "t_dev", "body": "benmarte/daedalus#19", "workspace": "dir:/w"}
    prov = _FilesProvider(BACKEND_FILES)
    with mock.patch.object(kanban, "list_tasks", return_value=[]):
        with mock.patch.object(
            kanban, "create_task", side_effect=["t_qa", "t_rev", "t_sec", "t_doc"]
        ) as mk_create:
            with mock.patch.object(kanban, "comment", return_value=True):
                created = iterate._create_downstream_review_tasks(
                    "slug", 19, card, pr_number=22, provider=prov,
                )
    keys = [c.kwargs["idempotency_key"] for c in mk_create.call_args_list]
    check("no accessibility card created", "accessibility-19" not in keys)
    check("qa/reviewer/security/docs created", keys == ["qa-19", "reviewer-19", "security-19", "docs-19"])
    check("4 tasks created (a11y skipped)", len(created) == 4)
    by_key = {c.kwargs["idempotency_key"]: c.kwargs["parents"] for c in mk_create.call_args_list}
    check(
        "docs degrades to reviewer+security only",
        by_key["docs-19"] == ["t_rev", "t_sec"],
    )
    check("provider was consulted", prov.calls == [22])


def test_ui_pr_creates_accessibility_card():
    """AC: UI-touching PR still creates the accessibility card (unchanged)."""
    card = {"id": "t_dev", "body": "benmarte/daedalus#19", "workspace": "dir:/w"}
    prov = _FilesProvider(UI_FILES)
    with mock.patch.object(kanban, "list_tasks", return_value=[]):
        with mock.patch.object(
            kanban, "create_task", side_effect=["t_qa", "t_rev", "t_sec", "t_acc", "t_doc"]
        ) as mk_create:
            with mock.patch.object(kanban, "comment", return_value=True):
                created = iterate._create_downstream_review_tasks(
                    "slug", 19, card, pr_number=22, provider=prov,
                )
    keys = [c.kwargs["idempotency_key"] for c in mk_create.call_args_list]
    check("accessibility card created", "accessibility-19" in keys)
    check("5 tasks created", len(created) == 5)
    by_key = {c.kwargs["idempotency_key"]: c.kwargs["parents"] for c in mk_create.call_args_list}
    check(
        "docs parented to reviewer/security/accessibility",
        by_key["docs-19"] == ["t_rev", "t_sec", "t_acc"],
    )


def test_no_provider_keeps_accessibility_card():
    """Fail-open: no provider means a11y is still created (byte-identical default)."""
    card = {"id": "t_dev", "body": "benmarte/daedalus#19", "workspace": "dir:/w"}
    with mock.patch.object(kanban, "list_tasks", return_value=[]):
        with mock.patch.object(
            kanban, "create_task", side_effect=["t_qa", "t_rev", "t_sec", "t_acc", "t_doc"]
        ) as mk_create:
            with mock.patch.object(kanban, "comment", return_value=True):
                iterate._create_downstream_review_tasks("slug", 19, card, pr_number=22)
    keys = [c.kwargs["idempotency_key"] for c in mk_create.call_args_list]
    check("a11y kept when provider absent", "accessibility-19" in keys)


def test_explicit_skip_accessibility_flag():
    """An explicit skip_accessibility=True short-circuits the diff check."""
    card = {"id": "t_dev", "body": "benmarte/daedalus#19", "workspace": "dir:/w"}
    with mock.patch.object(kanban, "list_tasks", return_value=[]):
        with mock.patch.object(
            kanban, "create_task", side_effect=["t_qa", "t_rev", "t_sec", "t_doc"]
        ) as mk_create:
            with mock.patch.object(kanban, "comment", return_value=True):
                iterate._create_downstream_review_tasks(
                    "slug", 19, card, pr_number=22, skip_accessibility=True,
                )
    keys = [c.kwargs["idempotency_key"] for c in mk_create.call_args_list]
    check("explicit skip drops a11y", "accessibility-19" not in keys)


# ── swarm path (#1294 native_decompose) ──────────────────────────────────────


def test_swarm_skips_accessibility_worker():
    """The native swarm omits the accessibility worker when gated out."""
    card = {"id": "t_dev", "body": "benmarte/daedalus#19", "workspace": "dir:/w"}
    with mock.patch.object(kanban, "list_tasks", return_value=[]):
        with mock.patch.object(kanban, "swarm", return_value="t_root") as mk_swarm:
            with mock.patch.object(kanban, "comment", return_value=True):
                executors._create_downstream_swarm(
                    "slug", 19, card, pr_number=22, skip_accessibility=True,
                )
    workers = mk_swarm.call_args.kwargs["workers"]
    check("swarm has 2 workers (no a11y)", len(workers) == 2)
    check(
        "no accessibility worker in swarm",
        not any("accessibility" in w for w in workers),
    )


def test_swarm_includes_accessibility_worker_for_ui():
    """The native swarm keeps the accessibility worker for UI PRs."""
    card = {"id": "t_dev", "body": "benmarte/daedalus#19", "workspace": "dir:/w"}
    with mock.patch.object(kanban, "list_tasks", return_value=[]):
        with mock.patch.object(kanban, "swarm", return_value="t_root") as mk_swarm:
            with mock.patch.object(kanban, "comment", return_value=True):
                executors._create_downstream_swarm(
                    "slug", 19, card, pr_number=22, skip_accessibility=False,
                )
    workers = mk_swarm.call_args.kwargs["workers"]
    check("swarm has 3 workers", len(workers) == 3)
    check(
        "accessibility worker present",
        any("accessibility" in w for w in workers),
    )


if __name__ == "__main__":
    print("Accessibility UI-gate tests (#1371)")
    print("-" * 60)
    for fn in (
        test_is_ui_file_extensions,
        test_is_ui_file_globs,
        test_is_ui_file_empty_and_custom,
        test_pr_touches_ui,
        test_accessibility_needed_fail_open,
        test_accessibility_needed_true_for_ui,
        test_accessibility_needed_false_for_backend,
        test_accessibility_needed_custom_fileset,
        test_backend_pr_skips_accessibility_card,
        test_ui_pr_creates_accessibility_card,
        test_no_provider_keeps_accessibility_card,
        test_explicit_skip_accessibility_flag,
        test_swarm_skips_accessibility_worker,
        test_swarm_includes_accessibility_worker_for_ui,
    ):
        fn()
    print("-" * 60)
    print(f"Results: {conftest._passed} passed, {conftest._failed} failed")
    sys.exit(1 if conftest._failed else 0)
