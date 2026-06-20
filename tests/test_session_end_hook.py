"""Tests for the on_session_end plugin hook in __init__.py."""
import importlib.util
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Load __init__.py directly by path — the repo root IS the daedalus package,
# so adding its parent to sys.path and importing "daedalus" is the right call
# only if running from outside the repo. Inside the repo test runner we load
# the file directly to avoid path ambiguity.
_INIT_PATH = Path(__file__).resolve().parent.parent / "__init__.py"
_spec = importlib.util.spec_from_file_location("daedalus_plugin", _INIT_PATH)
_plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_plugin)

_on_session_end = _plugin._on_session_end
register = _plugin.register


class TestOnSessionEnd(unittest.TestCase):
    """Unit tests for _on_session_end callback."""

    def _call(self, kanban_task=None, cron_exists=True, cron_executable=True, **kwargs):
        """Helper: call _on_session_end with controlled env + filesystem."""
        env = {}
        if kanban_task:
            env["HERMES_KANBAN_TASK"] = kanban_task

        cron_path = "/fake/hermes/scripts/daedalus-cron.sh"

        fired = []

        def fake_run(cmd, **kw):
            fired.append(cmd)

        with patch.dict(os.environ, env, clear=True), \
             patch("os.path.isfile", return_value=cron_exists), \
             patch("os.access", return_value=cron_executable), \
             patch("subprocess.run", side_effect=fake_run) as mock_run, \
             patch.dict(os.environ, {"HERMES_HOME": "/fake/hermes"}, clear=False):
            _on_session_end(
                session_id="s1",
                completed=True,
                interrupted=False,
                model="gpt-4",
                platform="cli",
                **kwargs,
            )
            # Give the daemon thread a moment to fire
            time.sleep(0.1)

        return mock_run, fired

    def test_non_worker_session_does_not_fire(self):
        """No HERMES_KANBAN_TASK → cron script never called."""
        _, fired = self._call(kanban_task=None)
        self.assertEqual(fired, [])

    def test_worker_session_fires_cron_script(self):
        """HERMES_KANBAN_TASK set + script present → cron script called."""
        _, fired = self._call(kanban_task="t_abc123")
        self.assertEqual(len(fired), 1)
        self.assertIn("daedalus-cron.sh", fired[0][-1])

    def test_missing_cron_script_skipped_gracefully(self):
        """Cron script file absent → no subprocess call, no exception."""
        _, fired = self._call(kanban_task="t_abc123", cron_exists=False)
        self.assertEqual(fired, [])

    def test_non_executable_cron_script_skipped(self):
        """Cron script not executable → no subprocess call."""
        _, fired = self._call(kanban_task="t_abc123", cron_exists=True, cron_executable=False)
        self.assertEqual(fired, [])

    def test_subprocess_exception_does_not_propagate(self):
        """Exception inside the thread must not surface to the caller."""
        env = {"HERMES_KANBAN_TASK": "t_abc123", "HERMES_HOME": "/fake/hermes"}
        with patch.dict(os.environ, env, clear=True), \
             patch("os.path.isfile", return_value=True), \
             patch("os.access", return_value=True), \
             patch("subprocess.run", side_effect=RuntimeError("boom")):
            # Must not raise
            _on_session_end(
                session_id="s1", completed=True, interrupted=False,
                model="x", platform="cli",
            )
            time.sleep(0.1)  # let thread run

    def test_extra_kwargs_accepted(self):
        """Hook must accept arbitrary **kwargs without error."""
        _, fired = self._call(kanban_task=None, unknown_future_param="foo")
        self.assertEqual(fired, [])


class TestRegister(unittest.TestCase):
    """Tests for the register(ctx) entry point."""

    def test_register_calls_hook(self):
        """register() must call ctx.register_hook with on_session_end."""
        ctx = MagicMock()
        register(ctx)
        hook_calls = [
            call for call in ctx.register_hook.call_args_list
            if call.args and call.args[0] == "on_session_end"
        ]
        self.assertEqual(len(hook_calls), 1)
        self.assertIs(hook_calls[0].args[1], _on_session_end)

    def test_register_calls_auxiliary_task(self):
        """register() must still register the daedalus_dispatch auxiliary task."""
        ctx = MagicMock()
        register(ctx)
        ctx.register_auxiliary_task.assert_called_once()
        call_kwargs = ctx.register_auxiliary_task.call_args
        self.assertEqual(call_kwargs.kwargs.get("key") or call_kwargs.args[0], "daedalus_dispatch")

    def test_register_does_not_raise_on_ctx_error(self):
        """If ctx raises, register() must not propagate the exception."""
        ctx = MagicMock()
        ctx.register_auxiliary_task.side_effect = AttributeError("no such method")
        # Must not raise
        register(ctx)


if __name__ == "__main__":
    unittest.main()
