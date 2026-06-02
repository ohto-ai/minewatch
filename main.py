"""
MC Server Log Fetcher — entry point.
"""

import sys

from config import DB_PATH
from db import init_db, count_logs
from fetcher import poll_loop


def main() -> None:
    print("=" * 52)
    print("  MC Server Log Fetcher")
    print(f"  DB: {DB_PATH}")
    print("=" * 52)

    try:
        conn = init_db(DB_PATH)
    except Exception as e:
        print(f"[fatal] Cannot open database: {e}")
        sys.exit(1)

    try:
        poll_loop(conn)
    finally:
        conn.close()
        print(f"[exit] Database closed — {count_logs(conn)} entries saved")


if __name__ == "__main__":
    main()
