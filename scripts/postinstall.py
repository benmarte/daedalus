#!/usr/bin/env python3
"""
postinstall.py — Prerequisite installer + roster provisioner for the daedalus plugin.

Ensures the host environment is ready (default profile, agent-skills plugin),
installing agent-skills automatically if missing, then runs
scripts/provision_roster.sh to seed the 9-agent lifecycle roster.
No gh CLI involved — VCS access is via provider APIs with tokens from env.

Usage:
    python3 scripts/postinstall.py          # ensure prereqs + provision
    python3 scripts/postinstall.py --check  # check only, don't install or provision

Exit codes:
    0 — all prereqs met (and provision succeeded if not --check)
    1 — one or more prereqs failed
    2 — provision failed

Import-safe: the module does nothing at import time. Call main() to run checks.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_HERMES_HOME = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))


# ── prerequisite checks ──────────────────────────────────────────────────────

def _check_default_profile() -> tuple[bool, str]:
    """Verify a usable 'default' profile exists (per-profile dir OR root config)."""
    prof = _HERMES_HOME / "profiles" / "default" / "config.yaml"
    root = _HERMES_HOME / "config.yaml"
    if prof.is_file():
        return True, f"OK: default profile found at {prof}"
    if root.is_file():
        return True, f"OK: default profile (root config) found at {root}"
    return False, (
        f"MISSING: no default profile ({prof} or {root})\n"
        f"  Fix: run 'hermes setup' first."
    )


def _ensure_agent_skills() -> tuple[bool, str]:
    """Ensure the agent-skills plugin is installed; auto-installs if missing."""
    skills_dir = _HERMES_HOME / "plugins" / "agent-skills" / "skills"
    if skills_dir.is_dir():
        return True, f"OK: agent-skills plugin installed at {skills_dir}"
    print("  agent-skills not found — installing automatically...")
    try:
        result = subprocess.run(
            ["hermes", "plugins", "install", "addyosmani/agent-skills", "--enable"],
            capture_output=True, text=True, timeout=90,
        )
    except FileNotFoundError:
        return False, "FAIL: 'hermes' CLI not found — is Hermes installed?"
    except subprocess.TimeoutExpired:
        return False, "FAIL: hermes plugins install timed out after 90s"
    if result.returncode == 0 and skills_dir.is_dir():
        return True, "OK: agent-skills plugin installed automatically"
    detail = (result.stderr or result.stdout or "").strip()[:200]
    return False, (
        f"FAIL: could not auto-install agent-skills\n"
        f"  {detail}\n"
        f"  Manual fix: hermes plugins install addyosmani/agent-skills --enable"
    )




def _check_vcs_tokens() -> tuple[bool, str]:
    """Advisory check: report which VCS provider tokens are present in the env.

    The plugin (dispatcher, dashboard, and worker provisioning) talks to
    GitHub/GitLab/Azure DevOps exclusively via their HTTPS APIs with tokens
    from the environment. Never a blocker — kanban-only setups need no token.
    """
    found = [name for name in ("GITHUB_TOKEN", "GH_TOKEN", "GITLAB_TOKEN",
                               "AZURE_DEVOPS_PAT")
             if (os.environ.get(name) or "").strip()]
    if found:
        return True, f"OK: VCS token(s) in env: {', '.join(found)}"
    return True, (
        "WARN: no VCS tokens in env (GITHUB_TOKEN / GITLAB_TOKEN / AZURE_DEVOPS_PAT) "
        "— fine for kanban-only/spec-file projects.\n"
        "  Export the token for each provider your projects use before running the dispatcher."
    )


# ── cron wrapper ─────────────────────────────────────────────────────────────


def _install_cron_wrapper() -> tuple[bool, str]:
    """Write ~/.hermes/scripts/daedalus-cron.sh (idempotent, chmod +x)."""
    real_home = Path(os.environ.get("HOME", os.path.expanduser("~")))
    scripts_dir = real_home / ".hermes" / "scripts"
    wrapper = scripts_dir / "daedalus-cron.sh"

    script_content = (
        "#!/usr/bin/env bash\n"
        "# daedalus-cron.sh — wrapper invoked by the Hermes dispatch cron.\n"
        "#\n"
        "# Usage: daedalus-cron.sh [--plugin-dir <path>] [dispatcher args...]\n"
        "#\n"
        "#   --plugin-dir <path>   Load the dispatcher from a LOCAL development\n"
        "#                         checkout at <path> instead of the installed plugin\n"
        "#                         (~/.hermes/plugins/daedalus). Prepends <path> to\n"
        "#                         PYTHONPATH and runs <path>/scripts/daedalus_dispatch.py\n"
        "#                         so code changes take effect without a\n"
        "#                         'hermes plugins update daedalus'. A warning is logged\n"
        "#                         whenever it is active — never leave it in a production\n"
        "#                         cron command. (#233)\n"
        "#\n"
        "# All other arguments are forwarded to daedalus_dispatch.py unchanged.\n"
        "\n"
        "# Source Hermes environment variables (including GITHUB_TOKEN)\n"
        "if [ -f \"$HOME/.hermes/.env\" ]; then\n"
        "  export $(grep -v '^#' \"$HOME/.hermes/.env\" | xargs)\n"
        "fi\n"
        "\n"
        "# --- Parse --plugin-dir (consumed here, NOT forwarded to the dispatcher) ----\n"
        "# The installed plugin is the default source. --plugin-dir overrides it with a\n"
        "# local dev checkout so the development loop is write → test, not\n"
        "# write → install → test. Remaining args are collected and forwarded verbatim.\n"
        "PLUGIN_DIR=\"\"\n"
        "ARGS=()\n"
        "while [ $# -gt 0 ]; do\n"
        "  case \"$1\" in\n"
        "    --plugin-dir) PLUGIN_DIR=\"$2\"; shift; shift || true ;;\n"
        "    --plugin-dir=*) PLUGIN_DIR=\"${1#*=}\"; shift ;;\n"
        "    *) ARGS+=(\"$1\"); shift ;;\n"
        "  esac\n"
        "done\n"
        "DISPATCH_HOME=\"$HOME/.hermes/plugins/daedalus\"\n"
        "if [ -n \"$PLUGIN_DIR\" ]; then\n"
        "  echo \"daedalus-cron: WARNING --plugin-dir active — loading dispatcher from\" \\\n"
        "       \"$PLUGIN_DIR (local dev checkout, NOT the installed plugin)\" >&2\n"
        "  export PYTHONPATH=\"$PLUGIN_DIR${PYTHONPATH:+:$PYTHONPATH}\"\n"
        "  DISPATCH_HOME=\"$PLUGIN_DIR\"\n"
        "fi\n"
        "# ---------------------------------------------------------------------------\n"
        "\n"
        "# --- Gateway watchdog (#383) ---\n"
        "# HTTP health probe + rate-limited restart + silent-death detection\n"
        "# Overlap protection: mkdir-based lock\n"
        'WATCHDOG_HTTP_SCRIPT="$DISPATCH_HOME/scripts/watchdog.py"\n'
        'WATCHDOG_HTTP_LOCK="$HOME/.hermes/gateway-watchdog-http.lock"\n'
        "if [ -f \"$WATCHDOG_HTTP_SCRIPT\" ]; then\n"
        "  if mkdir \"$WATCHDOG_HTTP_LOCK\" 2>/dev/null; then\n"
        "    echo \"daedalus-cron: running HTTP watchdog (#383)\" >&2\n"
        "    python3 \"$WATCHDOG_HTTP_SCRIPT\" || \\\n"
        "      echo \"daedalus-cron: HTTP watchdog exited with code $?\" >&2\n"
        "  fi\n"
        "fi\n"
        "# ---------------------------------------------------------------------------\n"
        "\n"
        "# --- Gateway watchdog (#799) ---\n"
        "# STOP-marker + exponential backoff + crash-log detection\n"
        "# Overlap protection: mkdir-based lock\n"
        'WATCHDOG_SCRIPT="$DISPATCH_HOME/scripts/gateway_watchdog.py"\n'
        'WATCHDOG_LOCK="$HOME/.hermes/gateway-watchdog.lock"\n'
        "if [ -f \"$WATCHDOG_SCRIPT\" ]; then\n"
        "  if mkdir \"$WATCHDOG_LOCK\" 2>/dev/null; then\n"
        "    echo \"daedalus-cron: running gateway watchdog (#799)\" >&2\n"
        "    python3 \"$WATCHDOG_SCRIPT\" --no-dispatch || \\\n"
        "      echo \"daedalus-cron: gateway watchdog exited with code $?\" >&2\n"
        "  fi\n"
        "fi\n"
        "# ---------------------------------------------------------------------------\n"
        "\n"
        "# Combined EXIT trap to clean up BOTH watchdog locks\n"
        'trap \'rmdir "$WATCHDOG_HTTP_LOCK" 2>/dev/null; rmdir "$WATCHDOG_LOCK" 2>/dev/null\' EXIT\n'
        "# ---------------------------------------------------------------------------\n"
        "\n"
        'exec python3 "$DISPATCH_HOME/scripts/daedalus_dispatch.py" "${ARGS[@]}"\n'
    )

    try:
        scripts_dir.mkdir(parents=True, exist_ok=True)
        wrapper.write_text(script_content)
        wrapper.chmod(0o755)
        return True, f"OK: cron wrapper installed at {wrapper}"
    except OSError as exc:
        return False, f"FAIL: could not write cron wrapper at {wrapper}: {exc}"


def _install_webhook_handler() -> tuple[bool, str]:
    """Install the webhook handler to ~/.hermes/agent-hooks/daedalus-ready.sh (idempotent).

    Copies scripts/daedalus-ready.sh from the repo to the user's agent-hooks dir
    and makes it executable. The handler reads webhook payloads from stdin,
    normalizes them via core.webhook_normalizer, and fires the dispatcher
    if the item moved to the Ready column.
    """
    real_home = Path(os.environ.get("HOME", os.path.expanduser("~")))
    hooks_dir = real_home / ".hermes" / "agent-hooks"
    target = hooks_dir / "daedalus-ready.sh"
    source = Path(__file__).resolve().parent / "daedalus-ready.sh"

    if not source.is_file():
        return False, f"MISSING: source script not found at {source}"

    try:
        hooks_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(source.read_text())
        target.chmod(0o755)
        return True, f"OK: webhook handler installed at {target}"
    except OSError as exc:
        return False, f"FAIL: could not install webhook handler at {target}: {exc}"


def _install_advance_hook() -> tuple[bool, str]:
    """Install the session-end advance hook to ~/.hermes/agent-hooks/ (idempotent).

    Copies BOTH scripts/daedalus-advance.sh and scripts/daedalus_resolve_project.py
    from the repo to the user's agent-hooks dir; the shell script is made
    executable. Hermes runs scripts in ~/.hermes/agent-hooks/ on session end, so
    copying daedalus-advance.sh there is all the registration it needs — when a
    daedalus pipeline worker finishes, the hook resolves the worker's project via
    daedalus_resolve_project.py and fires the dispatcher scoped to that project so
    the pipeline advances immediately instead of waiting for the next cron tick.
    """
    real_home = Path(os.environ.get("HOME", os.path.expanduser("~")))
    hooks_dir = real_home / ".hermes" / "agent-hooks"
    source_dir = Path(__file__).resolve().parent
    sh_source = source_dir / "daedalus-advance.sh"
    py_source = source_dir / "daedalus_resolve_project.py"

    for source in (sh_source, py_source):
        if not source.is_file():
            return False, f"MISSING: source script not found at {source}"

    try:
        hooks_dir.mkdir(parents=True, exist_ok=True)
        sh_target = hooks_dir / "daedalus-advance.sh"
        sh_target.write_text(sh_source.read_text())
        sh_target.chmod(0o755)
        py_target = hooks_dir / "daedalus_resolve_project.py"
        py_target.write_text(py_source.read_text())
        py_target.chmod(0o644)
        return True, f"OK: advance hook installed at {sh_target} (+ {py_target.name})"
    except OSError as exc:
        return False, f"FAIL: could not install advance hook in {hooks_dir}: {exc}"


def _install_watchdog_script() -> tuple[bool, str]:
    """Install the gateway watchdog script to ~/.hermes/plugins/daedalus/scripts/ (idempotent).

    Copies scripts/gateway_watchdog.py from the repo to the plugin's scripts dir.
    The watchdog detects silent gateway death and restarts with safeguards.
    """
    source_dir = Path(__file__).resolve().parent
    source = source_dir / "gateway_watchdog.py"

    if not source.is_file():
        return False, f"MISSING: watchdog script not found at {source}"

    target = _HERMES_HOME / "plugins" / "daedalus" / "scripts"

    try:
        target.mkdir(parents=True, exist_ok=True)
        target_file = target / "gateway_watchdog.py"
        target_file.write_text(source.read_text())
        target_file.chmod(0o755)
        return True, f"OK: watchdog script installed at {target_file}"
    except OSError as exc:
        return False, f"FAIL: could not install watchdog script at {target}: {exc}"


def _install_watchdog_http_script() -> tuple[bool, str]:
    """Install scripts/watchdog.py (#383 HTTP /health probe) idempotently.

    Source: scripts/watchdog.py (resolved via Path(__file__).parent).
    Target: <HERMES_HOME>/plugins/daedalus/scripts/watchdog.py.

    This watchdog complements gateway_watchdog.py — it probes the gateway's HTTP
    /health endpoint on localhost, detects silent deaths (process alive but
    goroutine stuck), and tracks restarts with rate limiting + cooldown. All
    configuration is via DAEDALUS_GW_* env vars.
    """
    hermes_home = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
    source_dir = Path(__file__).resolve().parent
    source = source_dir / "watchdog.py"

    if not source.is_file():
        return False, f"MISSING: HTTP watchdog script not found at {source}"

    target = hermes_home / "plugins" / "daedalus" / "scripts"

    try:
        target.mkdir(parents=True, exist_ok=True)
        target_file = target / "watchdog.py"
        target_file.write_text(source.read_text())
        target_file.chmod(0o755)
        return True, f"OK: HTTP watchdog script installed at {target_file}"
    except OSError as exc:
        return False, f"FAIL: could not install HTTP watchdog script at {target}: {exc}"


# ── provision ────────────────────────────────────────────────────────────────

def _run_provision(script_dir: Path) -> tuple[bool, str]:
    """Invoke provision_roster.sh and return (success, stdout+stderr)."""
    provision_script = script_dir / "provision_roster.sh"
    if not provision_script.is_file():
        return False, f"MISSING: provision script not found at {provision_script}"

    try:
        result = subprocess.run(
            ["bash", str(provision_script)],
            capture_output=True, text=True, timeout=120,
            cwd=str(script_dir),
        )
        output = result.stdout + result.stderr
        if result.returncode == 0:
            return True, output
        return False, f"Provision failed (exit code {result.returncode}):\n{output}"
    except subprocess.TimeoutExpired:
        return False, "Provision timed out after 120s."
    except Exception as exc:
        return False, f"Provision crashed: {exc}"


def _extract_profiles_from_output(output: str) -> list[str]:
    """Pull profile names from provision_roster.sh output (e.g. '=== developer ===').
    Filter out non-profile lines like '=== roster provisioned ==='."""
    _SKIP = {"roster provisioned", "roster"}
    profiles = []
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("=== ") and stripped.endswith(" ==="):
            name = stripped[4:-4].strip()
            if name.lower() not in _SKIP:
                profiles.append(name)
    return profiles


# ── main ─────────────────────────────────────────────────────────────────────

def main(check_only: bool = False) -> int:
    """Run prerequisite checks, then (optionally) provision the roster.

    Returns 0 on success, 1 on prereq failure, 2 on provision failure.
    """
    script_dir = Path(__file__).resolve().parent

    checks = [
        ("default profile", _check_default_profile),
        ("agent-skills plugin", _ensure_agent_skills),
("vcs tokens", _check_vcs_tokens),
    ]

    all_ok = True
    for label, check_fn in checks:
        ok, msg = check_fn()
        print(msg)
        if not ok:
            all_ok = False
        print()

    if not all_ok:
        print("\u2717 Prerequisites NOT met. Fix the issues above and re-run.")
        return 1

    # Install the cron wrapper script (idempotent, non-fatal)
    ok, msg = _install_cron_wrapper()
    print(msg)
    print()
    # Note: non-fatal — a wrapper failure is logged but doesn't block setup.
    
    # Install the webhook handler script (idempotent, non-fatal)
    ok, msg = _install_webhook_handler()
    print(msg)
    print()

    # Install the session-end advance hook (idempotent, non-fatal)
    ok, msg = _install_advance_hook()
    print(msg)
    print()

    # Install the enhanced gateway watchdog script (idempotent, non-fatal)
    ok, msg = _install_watchdog_script()
    print(msg)
    print()

    # Install the HTTP /health probe watchdog (idempotent, non-fatal)
    ok, msg = _install_watchdog_http_script()
    print(msg)
    print()

    # on_session_end plugin hook — no install needed, auto-registered via __init__.py
    print("OK: on_session_end plugin hook registered via __init__.py (fires dispatcher after every worker session)")
    print()

    if check_only:
        print("\u2713 All prerequisites met (--check only, skipping provision).")
        return 0

    print("\u2713 All prerequisites met. Running provision...\n")
    ok, output = _run_provision(script_dir)
    print(output)

    if not ok:
        print("\n\u2717 Provision failed.")
        return 2

    profiles = _extract_profiles_from_output(output)
    if profiles:
        print(f"\n\u2713 Roster provisioned successfully. Created/updated profiles: {', '.join(profiles)}")
    else:
        print("\n\u2713 Roster provisioned successfully.")

    return 0


if __name__ == "__main__":
    sys.exit(main(check_only="--check" in sys.argv))
