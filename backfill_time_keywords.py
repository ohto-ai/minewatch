"""
Queue day-time keywords to backfill older Minecraft logs.

Run this on the server, then keep main.py running to process the queued tasks.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from config import DB_PATH
from db import ensure_query_task, init_db


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="批量创建全天时间关键词查询任务（默认每分钟一个关键词）",
    )
    parser.add_argument(
        "--db",
        default=DB_PATH,
        help=f"SQLite 数据库路径（默认: {DB_PATH}）",
    )
    parser.add_argument(
        "--from-hour",
        type=int,
        default=0,
        dest="from_hour",
        help="起始小时，范围 0-23（默认: 0）",
    )
    parser.add_argument(
        "--to-hour",
        type=int,
        default=23,
        dest="to_hour",
        help="结束小时，范围 0-23，包含该小时（默认: 23）",
    )
    return parser


def iter_minute_keywords(from_hour: int, to_hour: int) -> list[str]:
    if not 0 <= from_hour <= 23:
        raise ValueError("--from-hour must be between 0 and 23")
    if not 0 <= to_hour <= 23:
        raise ValueError("--to-hour must be between 0 and 23")
    if from_hour > to_hour:
        raise ValueError("--from-hour must be less than or equal to --to-hour")
    return [
        f"{hour:02d}:{minute:02d}"
        for hour in range(from_hour, to_hour + 1)
        for minute in range(60)
    ]


def main() -> int:
    args = build_parser().parse_args()
    keywords = iter_minute_keywords(args.from_hour, args.to_hour)
    db_path = Path(args.db)

    conn = init_db(db_path)
    try:
        created = 0
        reused = 0
        for keyword in keywords:
            _, is_new = ensure_query_task(conn, keyword)
            if is_new:
                created += 1
            else:
                reused += 1
    finally:
        conn.close()

    print(f"[ok] database={db_path}")
    print(
        f"[ok] queued minute keywords for {args.from_hour:02d}:00-{args.to_hour:02d}:59"
    )
    print(f"[ok] total={len(keywords)} created={created} reused={reused}")
    print("[tip] 运行 python main.py 让采集器按现有队列逐个回填")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
