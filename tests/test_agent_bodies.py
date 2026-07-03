"""Golden tests for the nine agent-body template renderers (issue #1147).

Each of the nine role-prompt builders in ``scripts/daedalus_dispatch.py`` now
renders its prose from ``templates/agent_bodies/<role>.md`` via a brace-safe
``string.Template`` helper. These tests lock the rendered output byte-for-byte
against committed golden fixtures so a prompt-copy-only change (edit the ``.md``,
touch no ``.py``) diffs purely as markdown and is caught here until the fixture
is regenerated.

Regenerate fixtures after an intentional prompt edit::

    python tests/test_agent_bodies.py --regen

Dual-mode: also runs standalone (``python tests/test_agent_bodies.py``) via the
shared ``check`` printer, matching the rest of the suite.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

import pytest  # noqa: E402

from conftest import _load_dispatch, check  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "agent_bodies"

# ── canonical, deterministic inputs (no filesystem / network access) ──────────
REPO = "acme/widgets"
WORKDIR = "/tmp/work"
BASE = "dev"
PROVIDER = "github"

ISSUE = {
    "number": 1234,
    "title": "Fix the widget crash",
    "body": "The widget crashes when clicked.\n\n- [ ] repro\n- [ ] fix",
    "url": "https://github.com/acme/widgets/issues/1234",
    "labels": ["bug"],
}

# Epic input crafted to fire deterministic detection reasons under legacy
# defaults (size 1000 / checklist 5 / label 'epic'): decomposition language +
# 5-item checklist + epic-label. ``workdir=""`` skips source-context injection
# so the render stays deterministic (no filesystem reads).
EPIC_ISSUE = {
    "number": 1234,
    "title": "Rework the widget subsystem",
    "body": (
        "We should decompose into smaller pieces.\n\n"
        "- [ ] one\n- [ ] two\n- [ ] three\n- [ ] four\n- [ ] five\n"
    ),
    "url": "https://github.com/acme/widgets/issues/1234",
    "labels": ["epic"],
}

CASE_NAMES = [
    "planner",
    "validator_plain",
    "validator_delegated",
    "pm",
    "downstream_default",
    "downstream_security_first",
    "downstream_skip_dev",
    "dev",
    "qa",
    "reviewer",
    "security",
    "docs",
]


def _render_case(disp, name: str) -> str:
    """Render one builder with canonical inputs. Kept in sync with CASE_NAMES."""
    if name == "planner":
        return disp._planner_body(REPO, EPIC_ISSUE, "", BASE, PROVIDER)
    if name == "validator_plain":
        return disp._validator_body(
            REPO, ISSUE, WORKDIR, BASE, PROVIDER,
            security_notify_targets=["slack"],
        )
    if name == "validator_delegated":
        return disp._validator_body(
            REPO, ISSUE, WORKDIR, BASE, PROVIDER,
            security_notify_targets=["slack"],
            coding_agent="claude-code", coding_agent_cmd="claude -p",
        )
    if name == "pm":
        return disp._pm_body(
            REPO, ISSUE, "CONFIRMED: reproduced on dev", WORKDIR, BASE, PROVIDER,
        )
    if name == "downstream_default":
        return disp._downstream_body(
            REPO, ISSUE, 3, WORKDIR, "slack", BASE, PROVIDER,
            security_notify_targets=["slack"],
        )
    if name == "downstream_security_first":
        return disp._downstream_body(
            REPO, ISSUE, 3, WORKDIR, "slack", BASE, PROVIDER,
            security_notify_targets=["slack"],
            label_overrides={"bug": {"security_first": True}},
        )
    if name == "downstream_skip_dev":
        return disp._downstream_body(
            REPO, ISSUE, 3, WORKDIR, "slack", BASE, PROVIDER,
            security_notify_targets=["slack"],
            label_overrides={"bug": {"skip_developer": True}},
        )
    if name == "dev":
        return disp._dev_task_body(REPO, ISSUE, 3, WORKDIR, BASE, PROVIDER)
    if name == "qa":
        return disp._qa_task_body(REPO, ISSUE, WORKDIR, PROVIDER)
    if name == "reviewer":
        return disp._reviewer_task_body(REPO, ISSUE, WORKDIR, PROVIDER)
    if name == "security":
        return disp._security_task_body(REPO, ISSUE, WORKDIR, PROVIDER)
    if name == "docs":
        return disp._docs_task_body(REPO, ISSUE, WORKDIR, PROVIDER, "slack")
    raise KeyError(name)  # pragma: no cover


@pytest.mark.parametrize("name", CASE_NAMES)
def test_agent_body_matches_golden(name):
    disp = _load_dispatch()
    out = _render_case(disp, name)
    fixture = FIXTURES / f"{name}.txt"
    assert fixture.exists(), (
        f"missing golden fixture {fixture} — run `python tests/test_agent_bodies.py --regen`"
    )
    assert out == fixture.read_text(encoding="utf-8"), (
        f"{name} body drifted from golden fixture — if intentional, regenerate with "
        f"`python tests/test_agent_bodies.py --regen`"
    )


def test_missing_template_fails_loudly():
    """A missing template raises rather than yielding a silent empty prompt."""
    disp = _load_dispatch()
    with pytest.raises((FileNotFoundError, KeyError)):
        disp._render_agent_body("does-not-exist")


def _regen() -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    disp = _load_dispatch()
    for name in CASE_NAMES:
        out = _render_case(disp, name)
        (FIXTURES / f"{name}.txt").write_text(out, encoding="utf-8")
        print(f"  wrote {name}.txt ({len(out)} bytes)")


if __name__ == "__main__":
    if "--regen" in sys.argv:
        _regen()
    else:
        disp = _load_dispatch()
        for name in CASE_NAMES:
            fixture = FIXTURES / f"{name}.txt"
            if not fixture.exists():
                check(f"{name} fixture exists", False)
                continue
            check(
                f"{name} matches golden",
                _render_case(disp, name) == fixture.read_text(encoding="utf-8"),
            )
        import conftest

        print(f"\n{conftest._passed} passed, {conftest._failed} failed")
        sys.exit(1 if conftest._failed else 0)
