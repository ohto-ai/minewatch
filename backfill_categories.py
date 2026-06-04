"""
Backfill the ``category`` column on existing log rows.

Scans the ``logs`` table for rows where ``category = ''`` and classifies
them based on content markers (e.g. ``[ServerChat]``).  Uses batched
updates to keep transactions short.

Usage:
    python backfill_categories.py          # dry-run (report only)
    python backfill_categories.py --apply  # actually update
    python backfill_categories.py --apply --db path/to/logs.db
    python backfill_categories.py --apply --batch 200
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


def categorize_log(log_text: str) -> str:
    """Classify a log line into a category (mirrors fetcher.categorize_log)."""
    if "[ServerChat]" in log_text:
        return "server_chat"
    return ""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backfill category column on existing log rows",
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
    parser.add_argument(
        "--batch",
        default=500,
        type=int,
        dest="batch_size",
        help="Rows per transaction (default: 500)",
    )
    return parser


def backfill(db_path: str, *, dry_run: bool = True, batch_size: int = 500) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")

    # Count total uncategorized rows
    total_uncategorized = conn.execute(
        "SELECT COUNT(*) FROM logs WHERE category = ''"
    ).fetchone()[0]

    if total_uncategorized == 0:
        print("All rows already categorized — nothing to do.")
        conn.close()
        return

    print(f"Found {total_uncategorized:,} uncategorized log rows")

    # Sample what we'd find
    samples = conn.execute(
        "SELECT id, log FROM logs WHERE category = '' AND log LIKE '%[ServerChat]%' LIMIT 5"
    ).fetchall()
    server_chat_count = conn.execute(
        "SELECT COUNT(*) FROM logs WHERE category = '' AND log LIKE '%[ServerChat]%'"
    ).fetchone()[0]

    print(f"  → {server_chat_count:,} would be tagged as 'server_chat'")
    print(f"  → {total_uncategorized - server_chat_count:,} would stay uncategorized (general)")
    print()

    if samples:
        print("Sample [ServerChat] entries:")
        for row in samples:
            preview = row["log"][:100] + "..." if len(row["log"]) > 100 else row["log"]
            print(f"  #{row['id']}  {preview!r}")
        print()

    if dry_run:
        print(f"[dry-run] {total_uncategorized:,} rows would be processed.")
        print("Run with --apply to actually update them.")
        conn.close()
        return

    # ── Apply: batch-update server_chat rows ──
    updated_chat = 0
    offset = 0

    while True:
        with conn:
            rows = conn.execute(
                "SELECT id FROM logs WHERE category = '' AND log LIKE '%[ServerChat]%' "
                "LIMIT ? OFFSET ?",
                (batch_size, offset),
            ).fetchall()

            if not rows:
                break

            ids = [r["id"] for r in rows]
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"UPDATE logs SET category = 'server_chat' "
                f"WHERE id IN ({placeholders})",
                ids,
            )
            updated_chat += len(ids)
            offset += batch_size
            print(f"  Updated {updated_chat}/{server_chat_count} server_chat rows...")

    # Mark remaining uncategorized as general (leave as '' — that IS the default)
    # No need to update them; empty string already means "general/uncategorized".

    print(f"\nBackfill complete: {updated_chat:,} rows tagged as 'server_chat'")
    remaining = conn.execute(
        "SELECT COUNT(*) FROM logs WHERE category = ''"
    ).fetchone()[0]
    print(f"Remaining uncategorized (general): {remaining:,}")
    conn.close()


def main() -> int:
    args = build_parser().parse_args()
    db_path = args.db_path or str(Path(__file__).parent / "logs.db")

    if not Path(db_path).exists():
        print(f"Database not found: {db_path}")
        return 1

    backfill(db_path, dry_run=not args.apply, batch_size=args.batch_size)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
