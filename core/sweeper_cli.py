"""Manual invocation entry point for the stale-card sweeper.

Allows triggering the sweeper outside of the dispatcher tick (scheduled job
integration). Usage:

    python -m core.sweeper_cli <board> [--threshold-hours 48] [--archive] [--dry-run]
"""

import argparse
from typing import List, Optional
from core import sweeper


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="sweeper",
        description="Sweep stale blocked cards from a kanban board"
    )
    parser.add_argument("board", help="Board slug (e.g. 'daedalus')")
    parser.add_argument(
        "--threshold-hours",
        type=float,
        default=sweeper.DEFAULT_STALE_HOURS,
        help=f"Threshold in hours (default: {sweeper.DEFAULT_STALE_HOURS})"
    )
    parser.add_argument(
        "--archive",
        action="store_true",
        help="Archive stale cards off the active board"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report stale cards without archiving (implies --archive is ignored)"
    )
    return parser.parse_args(argv)


def run(argv: Optional[List[str]] = None) -> int:
    """Execute the sweeper CLI. Returns 0 on success, 1 on failure."""
    args = parse_args(argv)
    stale_ids = sweeper.sweep_stale_blocked(
        args.board,
        threshold_hours=args.threshold_hours,
        archive=args.archive,
        dry_run=args.dry_run
    )
    if stale_ids:
        print(f"Sweeper: found {len(stale_ids)} stale blocked card(s)")
        for tid in stale_ids:
            print(f"  - {tid}")
    else:
        print("Sweeper: no stale blocked cards found")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(run())
