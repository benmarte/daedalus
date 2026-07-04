"""Tests for core.iterate.outcomes — schema validation, parser, edge cases.

Covers:
  - Valid fenced JSON block extraction (last-block-wins, multiple blocks)
  - Valid bare JSON object extraction
  - Schema validation (version, role, verdict, refs types)
  - Malformed inputs returning None without raising
  - Per-role verdict enum completeness
  - Large summaries and surrounding prose
  - NEVER-raises contract
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.iterate.outcomes import (
    OutcomeRecord,
    SCHEMA_VERSION,
    VERDICT_TABLE,
    parse,
)

# ── helpers ───────────────────────────────────────────────────────────────────


def _wrap(payload: str) -> str:
    """Wrap a JSON payload in a fenced block as an agent would emit it."""
    return f"```json\n{payload}\n```"


def _minimal(role: str, verdict: str, issue: int | None = 42, pr: int | None = None) -> str:
    """Build a minimal valid JSON payload string."""
    issue_val = str(issue) if issue is not None else "null"
    pr_val = str(pr) if pr is not None else "null"
    return (
        f'{{"daedalus_outcome": {SCHEMA_VERSION}, "role": "{role}", '
        f'"verdict": "{verdict}", '
        f'"refs": {{"issue": {issue_val}, "pr": {pr_val}}}, '
        f'"evidence": {{}}, "note": ""}}'
    )


# ── VERDICT_TABLE completeness ────────────────────────────────────────────────


def test_verdict_table_has_all_roles():
    """All pipeline roles must appear in VERDICT_TABLE."""
    expected_roles = {
        "validator", "qa", "reviewer", "security",
        "a11y", "docs", "planner", "pm", "developer",
    }
    assert expected_roles == set(VERDICT_TABLE.keys())


def test_verdict_table_no_empty_verdict_sets():
    """Every role must have at least one valid verdict."""
    for role, verdicts in VERDICT_TABLE.items():
        assert verdicts, f"role {role!r} has no verdicts"


# ── happy path: fenced JSON ───────────────────────────────────────────────────


@pytest.mark.parametrize("role,verdict", [
    ("validator", "confirmed"),
    ("validator", "already_fixed"),
    ("validator", "duplicate"),
    ("validator", "needs_more_info"),
    ("validator", "security_threat"),
    ("validator", "block_for_review"),
    ("qa", "passed"),
    ("qa", "failed"),
    ("reviewer", "approved"),
    ("reviewer", "changes_requested"),
    ("security", "approved"),
    ("security", "changes_requested"),
    ("a11y", "approved"),
    ("a11y", "na"),
    ("a11y", "skipped"),
    ("a11y", "changes_requested"),
    ("docs", "posted"),
    ("planner", "plan"),
    ("planner", "not_suitable"),
    ("pm", "spec"),
    ("pm", "assigned"),
    ("pm", "clarified"),
    ("pm", "escalated"),
    ("developer", "pr_opened"),
    ("developer", "blocked"),
])
def test_parse_valid_fenced_block_all_verdicts(role, verdict):
    """Every valid (role, verdict) pair parses successfully from a fenced block."""
    summary = _wrap(_minimal(role, verdict))
    rec = parse(summary)
    assert rec is not None, f"expected OutcomeRecord for ({role!r}, {verdict!r})"
    assert isinstance(rec, OutcomeRecord)
    assert rec.role == role
    assert rec.verdict == verdict
    assert rec.schema_version == SCHEMA_VERSION


def test_parse_fenced_block_with_evidence():
    """Evidence dict is preserved."""
    payload = (
        '{"daedalus_outcome": 1, "role": "qa", "verdict": "passed", '
        '"refs": {"issue": 99, "pr": 55}, '
        '"evidence": {"ci": "green", "tests": "3389 passed"}, '
        '"note": "all good"}'
    )
    rec = parse(_wrap(payload))
    assert rec is not None
    assert rec.evidence == {"ci": "green", "tests": "3389 passed"}
    assert rec.note == "all good"
    assert rec.issue_ref == 99
    assert rec.pr_ref == 55


def test_parse_fenced_block_null_refs():
    """Null refs are returned as None, not the integer 0."""
    payload = '{"daedalus_outcome": 1, "role": "docs", "verdict": "posted", "refs": {"issue": null, "pr": null}}'
    rec = parse(_wrap(payload))
    assert rec is not None
    assert rec.issue_ref is None
    assert rec.pr_ref is None


def test_parse_fenced_block_missing_refs_key():
    """Absent 'refs' key is treated as empty refs (both None)."""
    payload = '{"daedalus_outcome": 1, "role": "docs", "verdict": "posted"}'
    rec = parse(_wrap(payload))
    assert rec is not None
    assert rec.issue_ref is None
    assert rec.pr_ref is None


def test_parse_last_fenced_block_wins():
    """When multiple fenced blocks are present, the last valid one is returned."""
    first = _wrap(_minimal("qa", "failed"))
    second = _wrap(_minimal("qa", "passed"))
    summary = f"QA run 1:\n{first}\n\nQA run 2 (retry):\n{second}"
    rec = parse(summary)
    assert rec is not None
    assert rec.verdict == "passed"  # last block wins


def test_parse_block_surrounded_by_prose():
    """JSON block embedded in prose is found regardless of surrounding text."""
    prefix = "qa-passed: PR #42\n\nDetailed analysis: all tests green.\n\n"
    suffix = "\n\nThe suite ran 3389 tests with 0 failures."
    payload = _minimal("qa", "passed", issue=5, pr=42)
    summary = prefix + _wrap(payload) + suffix
    rec = parse(summary)
    assert rec is not None
    assert rec.role == "qa"
    assert rec.verdict == "passed"
    assert rec.pr_ref == 42


def test_parse_large_summary():
    """Parser handles a large summary (many KB of prose around a small JSON block)."""
    prose = "Lorem ipsum dolor sit amet. " * 500  # ~14KB
    payload = _minimal("reviewer", "approved", pr=99)
    summary = prose + _wrap(payload) + prose
    rec = parse(summary)
    assert rec is not None
    assert rec.verdict == "approved"


# ── happy path: bare JSON ─────────────────────────────────────────────────────


def test_parse_bare_json_object():
    """A bare JSON object (not fenced) is found when no fenced block exists."""
    payload = _minimal("developer", "pr_opened", pr=77)
    summary = f"review-required: PR #77\n\n{payload}"
    rec = parse(summary)
    assert rec is not None
    assert rec.verdict == "pr_opened"
    assert rec.pr_ref == 77


def test_parse_bare_json_last_wins():
    """When multiple bare JSON objects are present, the last is used."""
    first = _minimal("developer", "blocked")
    second = _minimal("developer", "pr_opened", pr=77)
    summary = f"{first}\n\n{second}"
    rec = parse(summary)
    assert rec is not None
    assert rec.verdict == "pr_opened"


def test_parse_fenced_beats_bare_when_both_present():
    """Fenced block is preferred over bare JSON (fenced is checked first)."""
    bare = _minimal("qa", "failed")  # would give "failed"
    fenced = _wrap(_minimal("qa", "passed"))  # gives "passed"
    summary = f"{bare}\n\n{fenced}"
    rec = parse(summary)
    assert rec is not None
    assert rec.verdict == "passed", "fenced block should take priority over bare JSON"


# ── validation failures → None ────────────────────────────────────────────────


def test_parse_empty_string():
    """Empty summary returns None without raising."""
    assert parse("") is None


def test_parse_no_json():
    """Plain text with no JSON returns None."""
    assert parse("qa-passed: PR #42 all green") is None


def test_parse_wrong_schema_version():
    """Wrong schema_version (not 1) returns None."""
    payload = '{"daedalus_outcome": 2, "role": "qa", "verdict": "passed"}'
    assert parse(_wrap(payload)) is None


def test_parse_unknown_role():
    """Unknown role returns None."""
    payload = '{"daedalus_outcome": 1, "role": "unknown-agent", "verdict": "passed"}'
    assert parse(_wrap(payload)) is None


def test_parse_invalid_verdict_for_role():
    """Verdict not in the role's enum returns None."""
    payload = '{"daedalus_outcome": 1, "role": "qa", "verdict": "confirmed"}'
    assert parse(_wrap(payload)) is None


def test_parse_refs_issue_is_string_not_int():
    """refs.issue as a string (not int) returns None."""
    payload = '{"daedalus_outcome": 1, "role": "qa", "verdict": "passed", "refs": {"issue": "42", "pr": null}}'
    assert parse(_wrap(payload)) is None


def test_parse_refs_pr_is_string_not_int():
    """refs.pr as a string (not int) returns None."""
    payload = '{"daedalus_outcome": 1, "role": "qa", "verdict": "passed", "refs": {"issue": 42, "pr": "77"}}'
    assert parse(_wrap(payload)) is None


def test_parse_refs_not_a_dict():
    """refs as a non-dict (array) returns None."""
    payload = '{"daedalus_outcome": 1, "role": "qa", "verdict": "passed", "refs": [42, 77]}'
    assert parse(_wrap(payload)) is None


def test_parse_broken_json():
    """Syntactically invalid JSON returns None."""
    assert parse("```json\n{not: valid json}\n```") is None


def test_parse_valid_outer_but_missing_daedalus_outcome_key():
    """Valid JSON without 'daedalus_outcome' key returns None."""
    payload = '{"role": "qa", "verdict": "passed"}'
    assert parse(_wrap(payload)) is None


def test_parse_daedalus_outcome_zero():
    """daedalus_outcome: 0 (wrong version) returns None."""
    payload = '{"daedalus_outcome": 0, "role": "qa", "verdict": "passed"}'
    assert parse(_wrap(payload)) is None


def test_parse_daedalus_outcome_null():
    """daedalus_outcome: null returns None."""
    payload = '{"daedalus_outcome": null, "role": "qa", "verdict": "passed"}'
    assert parse(_wrap(payload)) is None


def test_parse_missing_role_key():
    """Missing 'role' key returns None."""
    payload = '{"daedalus_outcome": 1, "verdict": "passed"}'
    assert parse(_wrap(payload)) is None


def test_parse_missing_verdict_key():
    """Missing 'verdict' key returns None."""
    payload = '{"daedalus_outcome": 1, "role": "qa"}'
    assert parse(_wrap(payload)) is None


def test_parse_fenced_block_invalid_fails_fast_no_bare_fallback():
    """A fenced block that fails validation stops extraction (no bare-JSON fallback).

    If the agent emitted a fenced block (even malformed), we don't search for
    a bare JSON object elsewhere — the agent intended the fenced block.
    """
    bad_fenced = "```json\n{\"daedalus_outcome\": 1, \"role\": \"qa\", \"verdict\": \"WRONG_VERDICT\"}\n```"
    bare = _minimal("qa", "passed")
    summary = f"{bad_fenced}\n\n{bare}"
    # Fenced block validation fails → None; bare object is NOT used as fallback.
    assert parse(summary) is None


# ── never-raises contract ─────────────────────────────────────────────────────


def test_parse_never_raises_on_garbage_input():
    """parse() must not raise on any garbage input."""
    garbage_inputs = [
        None,  # type: ignore[arg-type]
        42,  # type: ignore[arg-type]
        "```json\n{```",
        "x" * 100_000,
        "```json\n" + ("}" * 1000) + "\n```",
        '{"daedalus_outcome": ' + "1" * 1000 + "}",
    ]
    for inp in garbage_inputs:
        try:
            result = parse(inp)  # type: ignore[arg-type]
            # None is the expected result; anything other than an exception is fine.
        except Exception as exc:  # pragma: no cover
            pytest.fail(f"parse() raised {exc!r} on input {inp!r}")


# ── OutcomeRecord immutability ────────────────────────────────────────────────


def test_outcome_record_is_frozen():
    """OutcomeRecord is a frozen dataclass — assignment must raise FrozenInstanceError."""
    rec = parse(_wrap(_minimal("qa", "passed")))
    assert rec is not None
    with pytest.raises((AttributeError, TypeError)):
        rec.role = "developer"  # type: ignore[misc]


# ── standalone runner (dual-mode parity) ─────────────────────────────────────


if __name__ == "__main__":
    import sys
    import traceback

    failures = 0
    tests = [
        name
        for name, obj in list(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    for name in sorted(tests):
        fn = globals()[name]
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception:
            failures += 1
            print(f"  FAIL  {name}")
            traceback.print_exc()
    print(f"\n{len(tests) - failures} passed, {failures} failed")
    sys.exit(1 if failures else 0)
