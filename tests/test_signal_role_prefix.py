"""Stage-signal detection must tolerate a leading ``<role>:`` label.

Local models often prefix the SOUL completion signal with their role name
(e.g. ``VALIDATOR: CONFIRMED — …``). Without normalization the ``startswith``
signal checks fail and the pipeline stalls in a false "no summary" retry loop
(found by live dogfooding on a local qwen model, 2026-07-05).
"""

from __future__ import annotations

from core.dispatch.checks import _strip_role_label


def test_strips_leading_role_label():
    assert _strip_role_label("VALIDATOR: CONFIRMED — issue is valid") == "CONFIRMED — issue is valid"
    assert _strip_role_label("PM: SPEC: acceptance criteria") == "SPEC: acceptance criteria"
    assert _strip_role_label("QA: qa-passed: suite green") == "qa-passed: suite green"
    assert _strip_role_label("Documentation: docs posted") == "docs posted"
    assert _strip_role_label("Security-Analyst: escalate: threat") == "escalate: threat"


def test_noop_on_bare_signal():
    # Compliant output (Claude / SOUL-exact) is never altered.
    assert _strip_role_label("CONFIRMED: reproduced on main") == "CONFIRMED: reproduced on main"
    assert _strip_role_label("SPEC: done") == "SPEC: done"
    assert _strip_role_label("docs posted") == "docs posted"
    assert _strip_role_label("PLANNING COMPLETE") == "PLANNING COMPLETE"


def test_does_not_eat_qa_passed_signal():
    # 'qa-passed' has no colon after 'qa' → must NOT be stripped to 'passed'.
    assert _strip_role_label("qa-passed: suite green") == "qa-passed: suite green"


def test_only_strips_one_label():
    # count=1: a second role-looking token later in the line is untouched.
    assert _strip_role_label("VALIDATOR: developer should implement") == "developer should implement"
