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

CREATE TABLE IF NOT EXISTS query_tasks (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword        TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'queued',
    fetched_count  INTEGER NOT NULL DEFAULT 0,
    inserted_count INTEGER NOT NULL DEFAULT 0,
    error          TEXT NOT NULL DEFAULT '',
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at     TIMESTAMP,
    finished_at    TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_query_tasks_status_id
ON query_tasks(status, id);

CREATE TABLE IF NOT EXISTS sync_tasks (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    remote_url     TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'queued',
    fetched_count  INTEGER NOT NULL DEFAULT 0,
    inserted_count INTEGER NOT NULL DEFAULT 0,
    error          TEXT NOT NULL DEFAULT '',
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at     TIMESTAMP,
    finished_at    TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_sync_tasks_status_id
ON sync_tasks(status, id);
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


def create_query_task(conn: sqlite3.Connection, keyword: str) -> int:
    """Create a query task and return its id."""
    with conn:
        cur = conn.execute(
            "INSERT INTO query_tasks (keyword, status) VALUES (?, 'queued')",
            (keyword,),
        )
    return int(cur.lastrowid)


def claim_next_query_task(conn: sqlite3.Connection) -> tuple[int, str] | None:
    """Claim the oldest queued task and mark it running."""
    with conn:
        row = conn.execute(
            "SELECT id, keyword FROM query_tasks WHERE status = 'queued' "
            "ORDER BY id ASC LIMIT 1"
        ).fetchone()
        if not row:
            return None

        task_id, keyword = int(row[0]), str(row[1])
        cur = conn.execute(
            "UPDATE query_tasks SET status = 'running', "
            "started_at = CURRENT_TIMESTAMP, error = '' "
            "WHERE id = ? AND status = 'queued'",
            (task_id,),
        )
        if cur.rowcount != 1:
            return None
    return task_id, keyword


def has_queued_query_task(conn: sqlite3.Connection) -> bool:
    """Return whether there is another queued task waiting."""
    row = conn.execute(
        "SELECT 1 FROM query_tasks WHERE status = 'queued' LIMIT 1"
    ).fetchone()
    return row is not None


def complete_query_task(
    conn: sqlite3.Connection, task_id: int, fetched_count: int, inserted_count: int
) -> None:
    """Mark a query task as completed."""
    with conn:
        conn.execute(
            "UPDATE query_tasks SET status = 'completed', fetched_count = ?, "
            "inserted_count = ?, finished_at = CURRENT_TIMESTAMP WHERE id = ?",
            (fetched_count, inserted_count, task_id),
        )


def fail_query_task(conn: sqlite3.Connection, task_id: int, error: str) -> None:
    """Mark a query task as failed."""
    with conn:
        conn.execute(
            "UPDATE query_tasks SET status = 'failed', error = ?, "
            "finished_at = CURRENT_TIMESTAMP WHERE id = ?",
            (error[:500], task_id),
        )


def list_query_tasks(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    """List recent query tasks for UI polling."""
    rows = conn.execute(
        "SELECT id, keyword, status, fetched_count, inserted_count, error, "
        "created_at, started_at, finished_at "
        "FROM query_tasks ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    tasks: list[dict] = []
    for row in rows:
        tasks.append({
            "id": int(row[0]),
            "keyword": str(row[1]),
            "status": str(row[2]),
            "fetched_count": int(row[3]),
            "inserted_count": int(row[4]),
            "error": str(row[5]) if row[5] is not None else "",
            "created_at": row[6],
            "started_at": row[7],
            "finished_at": row[8],
        })
    return tasks


def create_sync_task(conn: sqlite3.Connection, remote_url: str) -> int:
    """Create a sync task and return its id."""
    with conn:
        cur = conn.execute(
            "INSERT INTO sync_tasks (remote_url, status) VALUES (?, 'queued')",
            (remote_url,),
        )
    return int(cur.lastrowid)


def claim_next_sync_task(conn: sqlite3.Connection) -> tuple[int, str] | None:
    """Claim the oldest queued sync task and mark it running."""
    with conn:
        row = conn.execute(
            "SELECT id, remote_url FROM sync_tasks WHERE status = 'queued' "
            "ORDER BY id ASC LIMIT 1"
        ).fetchone()
        if not row:
            return None

        task_id, remote_url = int(row[0]), str(row[1])
        cur = conn.execute(
            "UPDATE sync_tasks SET status = 'running', "
            "started_at = CURRENT_TIMESTAMP, error = '' "
            "WHERE id = ? AND status = 'queued'",
            (task_id,),
        )
        if cur.rowcount != 1:
            return None
    return task_id, remote_url


def has_queued_sync_task(conn: sqlite3.Connection) -> bool:
    """Return whether there is another queued sync task waiting."""
    row = conn.execute(
        "SELECT 1 FROM sync_tasks WHERE status = 'queued' LIMIT 1"
    ).fetchone()
    return row is not None


def complete_sync_task(
    conn: sqlite3.Connection, task_id: int, fetched_count: int, inserted_count: int
) -> None:
    """Mark a sync task as completed."""
    with conn:
        conn.execute(
            "UPDATE sync_tasks SET status = 'completed', fetched_count = ?, "
            "inserted_count = ?, finished_at = CURRENT_TIMESTAMP WHERE id = ?",
            (fetched_count, inserted_count, task_id),
        )


def fail_sync_task(conn: sqlite3.Connection, task_id: int, error: str) -> None:
    """Mark a sync task as failed."""
    with conn:
        conn.execute(
            "UPDATE sync_tasks SET status = 'failed', error = ?, "
            "finished_at = CURRENT_TIMESTAMP WHERE id = ?",
            (error[:500], task_id),
        )


def list_sync_tasks(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    """List recent sync tasks for UI polling."""
    rows = conn.execute(
        "SELECT id, remote_url, status, fetched_count, inserted_count, error, "
        "created_at, started_at, finished_at "
        "FROM sync_tasks ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    tasks: list[dict] = []
    for row in rows:
        tasks.append({
            "id": int(row[0]),
            "remote_url": str(row[1]),
            "status": str(row[2]),
            "fetched_count": int(row[3]),
            "inserted_count": int(row[4]),
            "error": str(row[5]) if row[5] is not None else "",
            "created_at": row[6],
            "started_at": row[7],
            "finished_at": row[8],
        })
    return tasks
