"""
Repair log entries that have ANSI-remnant prefixes (e.g. [m>) obscuring
the real [HH:MM:SS LEVEL] timestamp header.

Run once to clean up historical data after the clean_log fix in fetcher.py.
Safe to run multiple times — it only touches rows that still have the prefix.

Usage:
    python repair_log_prefixes.py          # dry-run (report only)
    python repair_log_prefixes.py --apply  # actually repair
    python repair_log_prefixes.py --apply --db path/to/logs.db
"""

from __future__ import annotations

import argparse
import re
import sqlite3
from pathlib import Path

# ── Same pattern as _RE_ANSI_REMNANT in fetcher.py ───────────────
# Matches ANSI-code remnants like [m>, [0m>, [38;2;255;255;255m>, [K
# and stray "> " console prompt artifacts that survive after the ESC
# byte has been stripped upstream.
_RE_ANSI_REMNANT = re.compile(r"^(?:> ?|\[[0-9;]*[mKGJ]>?\s*)+")

# GLOB pre-filters: fast indexed-ish scan; the regex does the precise match.
# Catch lines starting with:
#   "> ..."          — stray console prompt artifact
#   "[...K..." early — ANSI line-clear remnant ([K)
#   "[...m..." early — ANSI SGR remnant ([0m, [32m, etc.)
_GLOB_PREFILTERS = [
    ">*",        # leading >
    "[[]*K*",    # [K remnant
    "[[]*m*",    # [0m, [32m etc.
]


def needs_repair(log: str) -> bool:
    """Return True if *log* starts with an ANSI remnant prefix."""
    return bool(_RE_ANSI_REMNANT.match(log))


def strip_remnant(log: str) -> str:
    """Remove the ANSI-remnant prefix, returning the cleaned log line."""
    return _RE_ANSI_REMNANT.sub("", log)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Repair ANSI-remnant prefixes in stored log entries",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        dest="apply",
        help="Actually write changes (omit for dry-run)",
    )
    parser.add_argument(
        "--db",
        default=None,
        dest="db_path",
        help="Path to logs.db (default: logs.db next to this script)",
    )
    return parser


def repair(db_path: str, *, dry_run: bool = True) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")

    # ── Phase 1: find candidate rows ──
    # Build OR-ed GLOB query from the pre-filter list
    where_clause = " OR ".join(["log GLOB ?"] * len(_GLOB_PREFILTERS))
    candidates = conn.execute(
        f"SELECT id, log, name, time FROM logs WHERE {where_clause}",
        tuple(_GLOB_PREFILTERS),
    ).fetchall()

    dirty: list[dict] = []
    for r in candidates:
        if needs_repair(r["log"]):
            dirty.append({
                "id": r["id"],
                "log": r["log"],
                "name": r["name"],
                "time": r["time"],
            })

    print(f"Scanned {len(candidates):,} candidate rows (GLOB pre-filter)")
    print(f"Found  {len(dirty):,} rows with ANSI-remnant prefixes")

    if not dirty:
        print("Nothing to repair.")
        conn.close()
        return

    # Show a few samples
    print("\nSample entries to repair:")
    for row in dirty[:5]:
        cleaned = strip_remnant(row["log"])
        print(f"  #{row['id']}  {row['log'][:80]!r}  →  {cleaned[:80]!r}")
    if len(dirty) > 5:
        print(f"  ... and {len(dirty) - 5} more")

    if dry_run:
        print(f"\n[dry-run] {len(dirty)} rows would be repaired.")
        print("Run with --apply to actually fix them.")
        conn.close()
        return

    # ── Phase 2: apply repairs ──
    cleaned_count = 0
    deleted_count = 0
    error_count = 0

    with conn:  # single transaction
        for row in dirty:
            cleaned = strip_remnant(row["log"])
            if cleaned == row["log"]:
                continue  # shouldn't happen, but be safe

            # Check if a clean version already exists with the same timestamp
            dup = conn.execute(
                "SELECT id FROM logs WHERE time = ? AND log = ? AND id != ?",
                (row["time"], cleaned, row["id"]),
            ).fetchone()

            if dup:
                # Clean version already exists — delete this dirty duplicate
                conn.execute("DELETE FROM logs WHERE id = ?", (row["id"],))
                deleted_count += 1
            else:
                try:
                    conn.execute(
                        "UPDATE logs SET log = ? WHERE id = ?",
                        (cleaned, row["id"]),
                    )
                    cleaned_count += 1
                except sqlite3.IntegrityError:
                    # UNIQUE constraint — another row got there first
                    conn.execute("DELETE FROM logs WHERE id = ?", (row["id"],))
                    deleted_count += 1

    print(f"\nRepair complete: {cleaned_count} cleaned, "
          f"{deleted_count} duplicates deleted, "
          f"{error_count} errors")

    # Verify
    where_clause = " OR ".join(["log GLOB ?"] * len(_GLOB_PREFILTERS))
    remaining = conn.execute(
        f"SELECT COUNT(*) FROM logs WHERE {where_clause}",
        tuple(_GLOB_PREFILTERS),
    ).fetchone()[0]
    print(f"Rows matching GLOB pre-filters after repair: {remaining}")
    conn.close()


def main() -> int:
    args = build_parser().parse_args()
    db_path = args.db_path or str(Path(__file__).parent / "logs.db")

    if not Path(db_path).exists():
        print(f"Database not found: {db_path}")
        return 1

    repair(db_path, dry_run=not args.apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
