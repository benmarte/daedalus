"""
Cron-reconciliation helpers for the Daedalus dashboard plugin API.

Owns notification validation and the ``hermes cron`` reconcile machinery that
a project Save routes through. Extracted from ``dashboard/plugin_api.py``
(issue #1155, PR 1/3) with NO behaviour change — every symbol is re-exported
from ``plugin_api`` so existing import paths keep resolving.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from dashboard._shared import (
    _hermes_cli,
    _schedule_to_crontab,
    parse_cron_jobs,
)


# Notification event types a cron.notifications[] entry can subscribe to.
# Keep in sync with NOTIFY_EVENTS in scripts/daedalus_dispatch.py.
NOTIFY_EVENTS = ("doc-report", "dispatch-summary", "pipeline-failure", "pr-ready")


def _validate_notifications(value: Any) -> list[str]:
    """Validate a cron.notifications payload. Returns human-readable errors."""
    if not isinstance(value, list):
        return ["cron.notifications must be a list"]
    errors: list[str] = []
    for i, entry in enumerate(value):
        if not isinstance(entry, dict):
            errors.append(f"cron.notifications[{i}] must be a mapping")
            continue
        target = entry.get("target")
        if not isinstance(target, str) or not target.strip():
            errors.append(f"cron.notifications[{i}].target must be a non-empty string "
                          "(e.g. 'slack:C123', 'discord:#general')")
        platform = entry.get("platform")
        if platform is not None and not isinstance(platform, str):
            errors.append(f"cron.notifications[{i}].platform must be a string")
        events = entry.get("events")
        if events is not None and (
            not isinstance(events, list)
            or any(e not in NOTIFY_EVENTS for e in events)
        ):
            errors.append(f"cron.notifications[{i}].events must be a list drawn from: "
                          + ", ".join(NOTIFY_EVENTS))
    return errors


# Canonical parser lives in core/cron_parser.py (issue #1148); this alias keeps
# the historical private name used by call sites and tests.
_parse_cron_jobs = parse_cron_jobs


def _cron_cli(args: list[str]) -> tuple[int, str]:
    """Run a ``hermes cron`` subcommand via the shared CLI wrapper."""
    return _hermes_cli(["cron"] + args, timeout=10)


def _write_schedule_to_config(cfg_path: Path, crontab_schedule: str) -> None:
    """Rewrite the ``cron.schedule`` value in a daedalus.yaml in place.

    Used after ``_reconcile_cron`` normalises an interval schedule to crontab
    syntax so the persisted YAML matches the live cron (mirrors the write-back
    in ``_ensure_dispatch_crons``). Never raises — a write failure just leaves
    the YAML on the interval value, which the plugin-load self-heal corrects.
    """
    try:
        raw_cfg = cfg_path.read_text()
        new_cfg = re.sub(
            r"(schedule\s*:\s*).*",
            lambda m: f'{m.group(1)}"{crontab_schedule}"',
            raw_cfg,
            count=1,
        )
        if new_cfg != raw_cfg:
            cfg_path.write_text(new_cfg)
    except OSError:
        pass


def _reconcile_cron(
    project_name: str, cron_cfg: dict, cfg_path: Path | None = None
) -> dict:
    """Reconcile the real ``hermes cron`` job with the config on save.

    Cron job name = ``f"{project_name}-daedalus"``. Each project owns exactly
    one job. Editing a project UPDATES the existing job in place via the
    native ``hermes cron edit <id>`` — it never stacks a duplicate:

    - one existing job  → ``hermes cron edit <id> --schedule <s>``
      (falls back to remove+create if the installed hermes lacks ``edit``)
    - no existing job   → ``hermes cron create``
    - duplicates found  → keep none, remove all by hex ID, create fresh
    - empty schedule    → remove all matches

    The schedule is normalised to crontab syntax via ``_schedule_to_crontab``
    BEFORE it reaches hermes (issue #134). Hermes treats interval syntax like
    ``60m`` as a *one-shot* job — it runs once, moves to ``[completed]`` and the
    dispatcher silently stops. Crontab syntax (``0 * * * *``) repeats forever.
    This mirrors what ``_ensure_dispatch_crons`` already does on plugin load, so
    a dashboard Save can never produce a one-shot cron.

    A cron CLI failure is captured as an error string; this function NEVER
    raises, so a broken ``hermes`` binary cannot fail the config save.

    Args:
        project_name: The project name from the config.
        cron_cfg: The ``cron`` dict from the resolved project config.
            Keys used: ``schedule`` (str), ``deliver`` (str, optional),
            ``notifications`` (list, optional — when set, the dispatcher
            self-delivers and the cron gets NO --deliver target).
        cfg_path: Optional path to the project's ``daedalus.yaml``. When given
            and the schedule was normalised (interval → crontab), the new
            crontab schedule is written back so the YAML stays consistent with
            the live cron (mirrors ``_ensure_dispatch_crons``).

    Returns:
        ``{"cron": "<created|updated|removed|skipped>", "name": "<cron_name>",
        "error": <str|None>}``
    """
    cron_name = f"{project_name}-daedalus"
    result: dict[str, Any] = {
        "cron": "skipped",
        "name": cron_name,
        "error": None,
    }

    raw_schedule = cron_cfg.get("schedule", "").strip() if cron_cfg else ""
    # Convert interval syntax ("60m", "every 2h") to crontab so the job repeats
    # forever — otherwise hermes creates a one-shot job (issue #134).
    schedule = _schedule_to_crontab(raw_schedule) if raw_schedule else ""
    # Keep the YAML in step with the live cron when we normalised the schedule.
    if cfg_path is not None and schedule and schedule != raw_schedule:
        _write_schedule_to_config(cfg_path, schedule)
    # With notifications[] the dispatcher fans out itself — the cron job must
    # not double-deliver its stdout.
    has_notifications = bool(cron_cfg.get("notifications")) if cron_cfg else False
    deliver = "" if has_notifications else (cron_cfg.get("deliver", "").strip() if cron_cfg else "")

    # Run the dispatcher from this repo's root so it auto-scopes to this project
    # instead of sweeping every registered repo (issue #137). The repo root is the
    # parent of the project's ``.hermes/`` dir, where daedalus.yaml lives.
    workdir = str(cfg_path.parent.parent.resolve()) if cfg_path is not None else ""

    # 1. Find existing jobs by name.
    matching_ids: list[str] = []
    rc, out = _cron_cli(["list", "--all"])
    if rc == 0:
        matching_ids = [j["job_id"] for j in _parse_cron_jobs(out) if j.get("name") == cron_name]

    # 2. Empty schedule → remove all matches.
    if not schedule:
        for job_id in matching_ids:
            _cron_cli(["remove", job_id])
        result["cron"] = "removed"
        return result

    # 3. Exactly one job → update it in place (native `hermes cron edit`).
    if len(matching_ids) == 1:
        edit_args = ["edit", matching_ids[0], "--schedule", schedule]
        if workdir:
            edit_args += ["--workdir", workdir]
        if deliver:
            edit_args += ["--deliver", deliver]
        rc, out = _cron_cli(edit_args)
        if rc == 0:
            result["cron"] = "updated"
            return result
        # Older hermes without `cron edit` (or edit failure): fall through to
        # remove+create so the save still converges on one correct job.

    # 4. Zero, several, or un-editable → remove all matches, create fresh.
    for job_id in matching_ids:
        _cron_cli(["remove", job_id])

    cmd = [
        "create", schedule,
        "--name", cron_name,
        "--script", "daedalus-cron.sh",
        "--no-agent",
    ]
    if workdir:
        cmd += ["--workdir", workdir]
    if deliver:
        cmd += ["--deliver", deliver]

    rc, out = _cron_cli(cmd)
    if rc != 0:
        result["error"] = out.strip()[:500] or f"exit code {rc}"
    else:
        result["cron"] = "created" if "created" in out.lower() else "updated"
    return result
