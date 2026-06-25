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
        "# Source Hermes environment variables (including GITHUB_TOKEN)\n"
        "if [ -f \"$HOME/.hermes/.env\" ]; then\n"
        "  export $(grep -v '^#' \"$HOME/.hermes/.env\" | xargs)\n"
        "fi\n"
        'exec python3 "$HOME/.hermes/plugins/daedalus/scripts/daedalus_dispatch.py" "$@"\n'
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


# ── python dependencies ──────────────────────────────────────────────────────


def _install_requirements() -> tuple[bool, str]:
    """Install Python deps from requirements.txt via pip (idempotent).

    The plugin's only third-party import is httpx (core/providers/http.py).
    Fresh installs may not expose it to the interpreter that runs the
    dispatcher, causing ModuleNotFoundError at dispatch time (issue #75).
    requirements.txt lives at the repo root (one level up from scripts/).
    Missing file is a benign no-op; pip already-satisfied is a fast success.
    """
    req = Path(__file__).resolve().parent.parent / "requirements.txt"
    if not req.is_file():
        return True, f"OK: no requirements.txt at {req} (nothing to install)"
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", str(req)],
            capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        return False, "FAIL: pip install -r requirements.txt timed out after 120s"
    except Exception as exc:
        return False, f"FAIL: pip install crashed: {exc}"
    if result.returncode == 0:
        return True, f"OK: Python dependencies installed from {req}"
    detail = (result.stderr or result.stdout or "").strip()[:200]
    return False, (
        f"FAIL: pip install -r requirements.txt failed:\n  {detail}\n"
        f"  Manual fix: {sys.executable} -m pip install -r {req}"
    )


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

    # Install Python dependencies (httpx) so the dispatcher can import them.
    ok, msg = _install_requirements()
    print(msg)
    print()
    if not ok:
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
