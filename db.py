"""
SQLite storage for MC server logs.
"""

import sqlite3
from pathlib import Path

SCHEMA = '''
CREATE TABLE IF NOT EXISTS logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    log         TEXT    NOT NULL,
    name        TEXT    NOT NULL,
    time        INTEGER NOT NULL,
    "using"     TEXT    DEFAULT '',
    fetched_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(time, log)
);

CREATE INDEX IF NOT EXISTS idx_logs_time ON logs(time);
'''

INSERT_SQL = '''
INSERT OR IGNORE INTO logs (log, name, time, "using")
VALUES (:log, :name, :time, :using)
'''


def init_db(path: str | Path = "logs.db") -> sqlite3.Connection:
    """Initialise the database and return a connection."""
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def insert_logs(conn: sqlite3.Connection, entries: list[dict],
                since_time: int = 0) -> tuple[int, int]:
    """
    Insert log entries with `time > since_time`, skipping duplicates.
    Returns (newly_inserted_count, max_time_from_entries).
    """
    inserted = 0
    max_time = since_time

    with conn:
        for entry in entries:
            t = entry["time"]
            if t > max_time:
                max_time = t
            if t < since_time:
                continue  # 已知旧数据，跳过，避免浪费 AUTOINCREMENT id
            # t >= since_time 的条目都会尝试 INSERT，
            # 由 UNIQUE(time, log) 负责去重，防止丢失同一毫秒的不同日志
            cursor = conn.execute(INSERT_SQL, {
                "log": entry["log"],
                "name": entry["name"],
                "time": t,
                "using": entry.get("using", ""),
            })
            if cursor.rowcount:
                inserted += 1

    return inserted, max_time


def get_latest_time(conn: sqlite3.Connection) -> int | None:
    """Return the most recent `time` value in the DB, or None if empty."""
    row = conn.execute("SELECT MAX(time) FROM logs").fetchone()
    return row[0] if row else None


def count_logs(conn: sqlite3.Connection) -> int:
    """Return total number of log entries stored."""
    row = conn.execute("SELECT COUNT(*) FROM logs").fetchone()
    return row[0] if row else 0
