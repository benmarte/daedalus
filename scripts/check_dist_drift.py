#!/usr/bin/env python3
"""
Check that dashboard src/dist/manifest changes stay in sync on every PR.

Usage (GitHub Actions — pass changed files as positional arguments):
    python3 scripts/check_dist_drift.py file1 file2 ...

Usage (piped from git diff):
    git diff --name-only origin/dev...HEAD | python3 scripts/check_dist_drift.py

Decision table
--------------
src changed  | dist changed | manifest changed | Result
-------------|--------------|------------------|-------
no           | no           | no               | skip   (no dashboard changes)
yes          | yes          | yes              | pass   (rebuild committed)
yes          | yes          | no               | FAIL   (manifest missing)
yes          | no           | yes              | FAIL   (dist missing)
yes          | no           | no               | FAIL   (both missing)
no           | yes          | *                | FAIL   (hand-edited bundle)
no           | no           | yes              | FAIL   (hand-edited manifest)

"src changed" means any file under dashboard/src/ OR dashboard/build.js itself.
"dist changed" means any file under dashboard/dist/.
"manifest changed" means dashboard/manifest.json exactly.

Exit codes:
    0  Pass or skip (no relevant drift, or all three areas updated together)
    1  Fail (drift detected — PR should not merge until fixed)
"""
from __future__ import annotations

import sys

# ── path classifiers ──────────────────────────────────────────────────────────

_SRC_PREFIXES: tuple[str, ...] = ("dashboard/src/", "dashboard/build.js")
_DIST_PREFIX = "dashboard/dist/"
_MANIFEST = "dashboard/manifest.json"

_FIX_HINT = (
    "Fix: cd dashboard && npm install && npm run build\n"
    "Then commit dashboard/dist/ and dashboard/manifest.json "
    "together with your src changes."
)


def classify_files(changed_files: list[str]) -> dict[str, bool]:
    """Classify a list of changed file paths into the three dashboard areas.

    Returns a dict with boolean keys 'src', 'dist', 'manifest'.
    """
    return {
        "src": any(f.startswith(_SRC_PREFIXES) for f in changed_files),
        "dist": any(f.startswith(_DIST_PREFIX) for f in changed_files),
        "manifest": any(f == _MANIFEST for f in changed_files),
    }


def check_drift(changed_files: list[str]) -> tuple[int, str]:
    """Apply the drift decision table to a list of changed file paths.

    Returns (exit_code, message) where exit_code is 0 (pass/skip) or 1 (fail).
    """
    c = classify_files(changed_files)

    # Nothing dashboard-related changed — nothing to check.
    if not c["src"] and not c["dist"] and not c["manifest"]:
        return 0, "dist-drift: skip — no dashboard/src, dist, or manifest files changed"

    # All three areas updated together — the only acceptable state after a rebuild.
    if c["src"] and c["dist"] and c["manifest"]:
        return 0, "dist-drift: pass — dashboard/src, dist, and manifest all updated together"

    # src changed but rebuild artefacts are incomplete.
    if c["src"]:
        missing: list[str] = []
        if not c["dist"]:
            missing.append("dashboard/dist/")
        if not c["manifest"]:
            missing.append("dashboard/manifest.json")
        joined = " and ".join(missing)
        return 1, (
            f"dist-drift: FAIL — dashboard/src or build.js changed "
            f"but {joined} not updated.\n{_FIX_HINT}"
        )

    # dist changed without a src change — hand-edited bundle.
    if c["dist"]:
        return 1, (
            "dist-drift: FAIL — dashboard/dist/ changed without any "
            "dashboard/src/ or build.js changes.\n"
            "Do not hand-edit the bundle. Edit dashboard/src/App.jsx and rebuild:\n"
            "  cd dashboard && npm install && npm run build"
        )

    # manifest changed alone — hand-edited manifest (or accidental edit).
    if c["manifest"]:
        return 1, (
            "dist-drift: FAIL — dashboard/manifest.json changed without "
            "dashboard/src/ or dist/ changes.\n"
            "Do not hand-edit the manifest. It is updated automatically by the build:\n"
            "  cd dashboard && npm install && npm run build"
        )

    # Unreachable given the exhaustive table above, but be safe.
    return 0, "dist-drift: pass — no drift detected"


# ── CLI entry point ───────────────────────────────────────────────────────────

def main() -> int:
    # Accept files from positional args; also read stdin if it is piped.
    args = sys.argv[1:]
    if not sys.stdin.isatty():
        args = sys.stdin.read().split() + args

    # De-duplicate and strip blanks.
    files = list(dict.fromkeys(f for f in args if f))

    exit_code, message = check_drift(files)
    print(message)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
