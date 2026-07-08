"""Tests for issue #1367 — Hermes model-change detection investigation.

The deliverable of #1367 is a documented decision (ADR-007) plus code
annotations that pin the three profile-sync paths to their roles:

1. ADR-007 exists and documents findings + the recommended integration path.
2. The canonical automatic path (``kanban_task_claimed`` JIT hook) is registered.
3. Each of the three sync sites cross-references ADR-007 so the "demote"
   decision cannot silently rot or be re-duplicated.

Run: pytest tests/test_adr_1367_model_sync.py -v
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ADR = ROOT / "docs" / "adr" / "adr-007-hermes-model-change-detection.md"


def _load_plugin():
    """Load the daedalus package by path (dir has a hyphen so normal import fails)."""
    spec = importlib.util.spec_from_file_location(
        "daedalus_plugin_adr1367", str(ROOT / "__init__.py")
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_adr_exists_with_required_sections():
    """ADR-007 documents what exists today and the recommended path (AC #1)."""
    assert ADR.is_file(), f"missing ADR: {ADR}"
    text = ADR.read_text()
    for heading in ("## Context", "## Decision", "## Consequences"):
        assert heading in text, f"ADR missing section: {heading}"
    # Findings: no upstream model-change hook exists today.
    assert "on_model_change" in text
    # Recommended canonical path is the JIT hook.
    assert "kanban_task_claimed" in text
    # Cross-links to the investigation issue and the follow-up implementation issue.
    assert "#1367" in text
    assert "#1368" in text


def test_adr_names_all_three_sync_paths():
    """The ADR must enumerate the three existing detection paths (AC: document today)."""
    text = ADR.read_text()
    assert "_on_kanban_task_claimed" in text
    assert "_resync_profiles_to_model" in text
    assert "sync_profiles" in text


def test_canonical_hook_is_registered():
    """register() wires the canonical kanban_task_claimed sync hook."""
    plugin = _load_plugin()

    class _Ctx:
        def __init__(self):
            self.hooks = []

        def register_auxiliary_task(self, **kwargs):
            pass

        def register_hook(self, name, handler):
            self.hooks.append(name)

    ctx = _Ctx()
    plugin.register(ctx)
    assert "kanban_task_claimed" in ctx.hooks


def test_sync_sites_cross_reference_adr():
    """Each sync site's docstring points at ADR-007 so the roles stay documented."""
    init_src = (ROOT / "__init__.py").read_text()
    resolvers_src = (ROOT / "core" / "dispatch" / "resolvers.py").read_text()
    standalone_src = (ROOT / "core" / "sync_profiles.py").read_text()

    # Canonical JIT hook.
    assert "CANONICAL" in init_src and "ADR-007" in init_src
    # Demoted poll-fingerprint fallback.
    assert "DEMOTED" in resolvers_src and "ADR-007" in resolvers_src
    # Manual operator escape hatch.
    assert "MANUAL" in standalone_src and "ADR-007" in standalone_src
