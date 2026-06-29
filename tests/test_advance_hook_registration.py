"""Regression tests for the per-profile advance-hook registration (issue #962).

The daedalus pipeline advances in near-real-time only when each Hermes profile's
config.yaml registers ``daedalus-advance.sh`` under ``hooks.on_session_end``.
``planner-daedalus`` (and other roles) shipped without that block, so the hook
never fired and the pipeline stalled until the next hourly cron tick.

These tests cover:
  * the pure mutation helper (``ensure_advance_hook``) — added-when-absent,
    idempotent, non-destructive;
  * the file round-trip (``register_in_file``);
  * a static check that ``provision_roster.sh`` wires the helper into the
    per-role setup so EVERY provisioned role gets the hook.

Dual-mode: runs under pytest and as ``python tests/test_advance_hook_registration.py``.
"""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_HELPER_PATH = _REPO_ROOT / "scripts" / "register_advance_hook.py"

_spec = importlib.util.spec_from_file_location("register_advance_hook", _HELPER_PATH)
assert _spec and _spec.loader, f"cannot load helper at {_HELPER_PATH}"
_helper = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_helper)

ensure_advance_hook = _helper.ensure_advance_hook
register_in_file = _helper.register_in_file

_HOOK = "/Users/example/.hermes/agent-hooks/daedalus-advance.sh"


class TestEnsureAdvanceHook(unittest.TestCase):
    """The pure dict mutation."""

    def test_adds_hook_when_absent(self):
        cfg = ensure_advance_hook({}, _HOOK)
        self.assertEqual(
            cfg["hooks"]["on_session_end"],
            [{"command": _HOOK, "timeout": 90}],
        )
        self.assertIs(cfg["hooks_auto_accept"], True)

    def test_custom_timeout(self):
        cfg = ensure_advance_hook({}, _HOOK, timeout=120)
        self.assertEqual(cfg["hooks"]["on_session_end"][0]["timeout"], 120)

    def test_idempotent_no_duplicate(self):
        cfg = ensure_advance_hook({}, _HOOK)
        cfg = ensure_advance_hook(cfg, _HOOK)
        cfg = ensure_advance_hook(cfg, _HOOK)
        entries = [
            e for e in cfg["hooks"]["on_session_end"] if e.get("command") == _HOOK
        ]
        self.assertEqual(len(entries), 1, "advance hook duplicated on re-apply")

    def test_preserves_unrelated_top_level_keys(self):
        cfg = {"model": "claude-opus", "terminal": {"env_passthrough": ["X"]}}
        ensure_advance_hook(cfg, _HOOK)
        self.assertEqual(cfg["model"], "claude-opus")
        self.assertEqual(cfg["terminal"], {"env_passthrough": ["X"]})

    def test_preserves_existing_hooks_and_other_session_end_entries(self):
        other = {"command": "/some/other-hook.sh", "timeout": 30}
        cfg = {
            "hooks": {
                "on_session_start": [{"command": "/start.sh"}],
                "on_session_end": [other],
            }
        }
        ensure_advance_hook(cfg, _HOOK)
        end = cfg["hooks"]["on_session_end"]
        self.assertIn(other, end, "pre-existing on_session_end entry was dropped")
        self.assertIn({"command": _HOOK, "timeout": 90}, end)
        self.assertEqual(cfg["hooks"]["on_session_start"], [{"command": "/start.sh"}])


class TestRegisterInFile(unittest.TestCase):
    """The config.yaml round-trip used by provision_roster.sh."""

    def _roundtrip(self, initial: dict | None) -> dict:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.yaml"
            if initial is not None:
                path.write_text(yaml.safe_dump(initial))
            register_in_file(str(path), _HOOK)
            return yaml.safe_load(path.read_text())

    def test_writes_hook_to_fresh_config(self):
        cfg = self._roundtrip({"model": "x"})
        self.assertEqual(
            cfg["hooks"]["on_session_end"], [{"command": _HOOK, "timeout": 90}]
        )
        self.assertIs(cfg["hooks_auto_accept"], True)
        self.assertEqual(cfg["model"], "x")

    def test_missing_file_is_created(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.yaml"
            register_in_file(str(path), _HOOK)
            self.assertTrue(path.exists())
            cfg = yaml.safe_load(path.read_text())
            self.assertEqual(cfg["hooks"]["on_session_end"][0]["command"], _HOOK)

    def test_file_idempotent_on_reapply(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.yaml"
            register_in_file(str(path), _HOOK)
            register_in_file(str(path), _HOOK)
            cfg = yaml.safe_load(path.read_text())
            self.assertEqual(len(cfg["hooks"]["on_session_end"]), 1)


class TestProvisionRosterWiring(unittest.TestCase):
    """Static checks: the provisioner wires the helper into per-role setup so
    EVERY role gets the hook (not just the ones that acquired it manually)."""

    @classmethod
    def setUpClass(cls):
        cls.script = (_REPO_ROOT / "scripts" / "provision_roster.sh").read_text()

    def test_calls_helper_inside_setup_role(self):
        # The call lives in setup_role(), which runs once per role — so a single
        # invocation in the script body covers all nine roles.
        idx = self.script.index("setup_role() {")
        end = self.script.index("\n}", idx)
        body = self.script[idx:end]
        self.assertIn("register_advance_hook.py", body)

    def test_uses_installed_advance_hook_path(self):
        self.assertIn(
            'ADVANCE_HOOK="$HERMES/agent-hooks/daedalus-advance.sh"', self.script
        )

    def test_all_nine_roles_provisioned(self):
        roles = (
            "validator-daedalus",
            "project-manager-daedalus",
            "planner-daedalus",
            "developer-daedalus",
            "reviewer-daedalus",
            "security-analyst-daedalus",
            "qa-daedalus",
            "accessibility-daedalus",
            "documentation-daedalus",
        )
        for role in roles:
            self.assertIn(f"setup_role {role}", self.script, f"{role} not provisioned")


if __name__ == "__main__":
    unittest.main()
