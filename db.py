"""
SQLite storage for MC server logs.
"""

import re
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
CREATE INDEX IF NOT EXISTS idx_logs_name ON logs(name);

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

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE,
    password_hash TEXT    NOT NULL,
    role          TEXT    NOT NULL DEFAULT 'user',
    password_plain TEXT   NOT NULL DEFAULT '',
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
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

CREATE TABLE IF NOT EXISTS tag_rules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern     TEXT    NOT NULL,
    category    TEXT    NOT NULL,
    priority    INTEGER NOT NULL DEFAULT 0,
    description TEXT    NOT NULL DEFAULT '',
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS role_permissions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    role        TEXT    NOT NULL UNIQUE,
    mode        TEXT    NOT NULL DEFAULT 'all',
    categories  TEXT    NOT NULL DEFAULT '',
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
'''

INSERT_SQL = '''
INSERT OR IGNORE INTO logs (log, name, time, "using", category)
VALUES (:log, :name, :time, :using, :category)
'''


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply schema migrations for older databases."""
    # Migration: add password_plain column (added 2025-06 for xcon role support)
    try:
        conn.execute("ALTER TABLE users ADD COLUMN password_plain TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass  # column already exists

    # Migration: add category column for log classification (added 2026-06)
    try:
        conn.execute("ALTER TABLE logs ADD COLUMN category TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass  # column already exists

    # Migration: add xcon_level column for tracking API probe result (added 2026-06)
    try:
        conn.execute("ALTER TABLE users ADD COLUMN xcon_level TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass  # column already exists


def init_db(path: str | Path = "logs.db") -> sqlite3.Connection:
    """Initialise the database and return a connection."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    _migrate(conn)

    # Seed default tag rule if tag_rules table is empty
    try:
        row = conn.execute("SELECT COUNT(*) FROM tag_rules").fetchone()
        if row and row[0] == 0:
            conn.execute(
                "INSERT INTO tag_rules (pattern, category, priority, description) "
                "VALUES (?, ?, ?, ?)",
                (r'\[ServerChat\]', 'server_chat', 0, 'Server 聊天消息'),
            )
    except sqlite3.OperationalError:
        pass  # table may not exist yet on first-ever run

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
                "category": entry.get("category", ""),
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
    if conn.in_transaction:
        cur = conn.execute(
            "INSERT INTO query_tasks (keyword, status) VALUES (?, 'queued')",
            (keyword,),
        )
    else:
        with conn:
            cur = conn.execute(
                "INSERT INTO query_tasks (keyword, status) VALUES (?, 'queued')",
                (keyword,),
            )
    return int(cur.lastrowid)


def ensure_query_task(conn: sqlite3.Connection, keyword: str) -> tuple[int, bool]:
    """
    Ensure a query task exists for `keyword`.
    Returns (task_id, created_new).

    Existing queued/running/completed tasks are reused.
    Failed tasks are reset back to queued for retry.
    """
    row = conn.execute(
        "SELECT id, status FROM query_tasks WHERE keyword = ? "
        "ORDER BY CASE status "
        "WHEN 'queued' THEN 0 "
        "WHEN 'running' THEN 1 "
        "WHEN 'completed' THEN 2 "
        "WHEN 'failed' THEN 3 "
        "ELSE 4 END ASC, id DESC LIMIT 1",
        (keyword,),
    ).fetchone()
    if row:
        task_id, status = int(row[0]), str(row[1])
        if status == "failed":
            if conn.in_transaction:
                conn.execute(
                    "UPDATE query_tasks SET status = 'queued', fetched_count = 0, "
                    "inserted_count = 0, error = '', started_at = NULL, "
                    "finished_at = NULL WHERE id = ?",
                    (task_id,),
                )
            else:
                with conn:
                    conn.execute(
                        "UPDATE query_tasks SET status = 'queued', fetched_count = 0, "
                        "inserted_count = 0, error = '', started_at = NULL, "
                        "finished_at = NULL WHERE id = ?",
                        (task_id,),
                    )
            return task_id, False
        return task_id, False
    return create_query_task(conn, keyword), True


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


def list_query_tasks(conn: sqlite3.Connection, limit: int = 20,
                     status: str | None = None) -> list[dict]:
    """List recent query tasks for UI polling.

    When *status* is 'active', only queued + running tasks are returned,
    ordered by status (running first) then id.
    When *status* is None (default), recent tasks of any status are returned
    ordered by newest id first.
    """
    if status == "active":
        rows = conn.execute(
            "SELECT id, keyword, status, fetched_count, inserted_count, error, "
            "created_at, started_at, finished_at "
            "FROM query_tasks WHERE status IN ('queued', 'running') "
            "ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END, id ASC "
            "LIMIT ?",
            (limit,),
        ).fetchall()
    else:
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


def get_query_task_stats(conn: sqlite3.Connection) -> dict[str, int]:
    """Return counts of query tasks grouped by status."""
    rows = conn.execute(
        "SELECT status, COUNT(*) FROM query_tasks GROUP BY status"
    ).fetchall()
    stats = {"total": 0, "queued": 0, "running": 0, "completed": 0, "failed": 0}
    for status, cnt in rows:
        key = str(status) if str(status) in stats else None
        if key:
            stats[key] = int(cnt)
        stats["total"] += int(cnt)
    return stats


# ── User management ────────────────────────────────────────────

def create_user(conn: sqlite3.Connection, username: str, password_hash: str,
                role: str = "user", password_plain: str = "") -> int:
    """Create a new user and return its id."""
    with conn:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, role, password_plain) "
            "VALUES (?, ?, ?, ?)",
            (username, password_hash, role, password_plain),
        )
    return int(cur.lastrowid)


def create_sync_task(conn: sqlite3.Connection, remote_url: str) -> int:
    """Create a sync task and return its id."""
    with conn:
        cur = conn.execute(
            "INSERT INTO sync_tasks (remote_url, status) VALUES (?, 'queued')",
            (remote_url,),
        )
    return int(cur.lastrowid)


def get_user_by_username(conn: sqlite3.Connection, username: str) -> dict | None:
    """Return user dict for *username*, or None if not found."""
    row = conn.execute(
        "SELECT id, username, password_hash, role, password_plain, xcon_level, created_at "
        "FROM users WHERE username = ?",
        (username,),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": int(row[0]),
        "username": str(row[1]),
        "password_hash": str(row[2]),
        "role": str(row[3]),
        "password_plain": str(row[4]) if row[4] is not None else "",
        "xcon_level": str(row[5]) if row[5] else "",
        "created_at": row[6],
    }


def get_user_by_id(conn: sqlite3.Connection, user_id: int) -> dict | None:
    """Return user dict for *user_id*, or None if not found."""
    row = conn.execute(
        "SELECT id, username, password_hash, role, password_plain, xcon_level, created_at "
        "FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": int(row[0]),
        "username": str(row[1]),
        "password_hash": str(row[2]),
        "role": str(row[3]),
        "password_plain": str(row[4]) if row[4] is not None else "",
        "xcon_level": str(row[5]) if row[5] else "",
        "created_at": row[6],
    }


def update_user_role(conn: sqlite3.Connection, user_id: int, role: str) -> None:
    """Update the role of an existing user."""
    with conn:
        conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))


def update_user_password(conn: sqlite3.Connection, user_id: int,
                         password_hash: str, password_plain: str | None = None) -> None:
    """Update the password hash (and optionally plaintext) of an existing user."""
    with conn:
        if password_plain is not None:
            conn.execute(
                "UPDATE users SET password_hash = ?, password_plain = ? WHERE id = ?",
                (password_hash, password_plain, user_id),
            )
        else:
            conn.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (password_hash, user_id),
            )


def update_xcon_level(conn: sqlite3.Connection, user_id: int, level: str) -> None:
    """Update the xcon_level for a user (e.g. 'full' or 'restricted')."""
    with conn:
        conn.execute("UPDATE users SET xcon_level = ? WHERE id = ?", (level, user_id))


def delete_user(conn: sqlite3.Connection, user_id: int) -> None:
    """Delete a user by id."""
    with conn:
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))


def list_users(conn: sqlite3.Connection) -> list[dict]:
    """Return all users (without password hashes, with plaintext for xcon users)."""
    rows = conn.execute(
        "SELECT id, username, role, password_plain, xcon_level, created_at FROM users ORDER BY id ASC"
    ).fetchall()
    return [
        {
            "id": int(r[0]),
            "username": str(r[1]),
            "role": str(r[2]),
            "password_plain": str(r[3]) if r[3] is not None else "",
            "xcon_level": str(r[4]) if r[4] else "",
            "created_at": r[5],
        }
        for r in rows
    ]


def count_admins(conn: sqlite3.Connection) -> int:
    """Return the number of admin users."""
    row = conn.execute(
        "SELECT COUNT(*) FROM users WHERE role = 'admin'"
    ).fetchone()
    return int(row[0]) if row else 0


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


def delete_query_tasks(conn: sqlite3.Connection, statuses: list[str]) -> int:
    """Delete query tasks matching the given statuses. Returns count deleted."""
    if not statuses:
        return 0
    placeholders = ",".join("?" for _ in statuses)
    with conn:
        cur = conn.execute(
            f"DELETE FROM query_tasks WHERE status IN ({placeholders})",
            statuses,
        )
    return cur.rowcount


def delete_sync_tasks(conn: sqlite3.Connection, statuses: list[str]) -> int:
    """Delete sync tasks matching the given statuses. Returns count deleted."""
    if not statuses:
        return 0
    placeholders = ",".join("?" for _ in statuses)
    with conn:
        cur = conn.execute(
            f"DELETE FROM sync_tasks WHERE status IN ({placeholders})",
            statuses,
        )
    return cur.rowcount


def reset_sync_task(conn: sqlite3.Connection, task_id: int) -> bool:
    """Reset a failed sync task back to queued for retry.

    Returns True if the task was reset, False if it wasn't in a resettable state.
    """
    with conn:
        cur = conn.execute(
            "UPDATE sync_tasks SET status = 'queued', fetched_count = 0, "
            "inserted_count = 0, error = '', started_at = NULL, "
            "finished_at = NULL WHERE id = ? AND status = 'failed'",
            (task_id,),
        )
        return cur.rowcount == 1


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


# ── Tag rules ────────────────────────────────────────────────────

def load_compiled_tag_rules(
    conn: sqlite3.Connection,
) -> list[tuple[re.Pattern, str]]:
    """Load enabled tag rules from DB, compile regexes, sort by priority DESC.

    Returns a list of (compiled_regex, category) tuples.
    Invalid regex patterns are silently skipped.
    """
    rows = conn.execute(
        "SELECT pattern, category, priority FROM tag_rules "
        "WHERE enabled = 1 ORDER BY priority DESC, id ASC"
    ).fetchall()
    rules: list[tuple[re.Pattern, str]] = []
    for row in rows:
        try:
            compiled = re.compile(row["pattern"])
            rules.append((compiled, str(row["category"])))
        except re.error:
            continue  # skip invalid patterns (shouldn't happen, defensive)
    return rules


def list_distinct_categories(conn: sqlite3.Connection) -> list[str]:
    """Return all non-empty category values currently present in the logs table."""
    rows = conn.execute(
        "SELECT DISTINCT category FROM logs WHERE category != '' ORDER BY category"
    ).fetchall()
    return [str(r[0]) for r in rows]


def get_all_matching_categories(
    log_text: str,
    compiled_rules: list[tuple[re.Pattern, str]],
) -> list[str]:
    """Return ALL matching categories for a log line (not just the first).

    Unlike :func:`categorize_log_text` which returns only the highest-
    priority match, this returns every category whose pattern matches.
    """
    return [category for pattern, category in compiled_rules if pattern.search(log_text)]


def categorize_log_text(
    log_text: str,
    compiled_rules: list[tuple[re.Pattern, str]],
) -> str:
    """Return the first matching category, or '' if no rule matches."""
    for pattern, category in compiled_rules:
        if pattern.search(log_text):
            return category
    return ''


def create_tag_rule(
    conn: sqlite3.Connection,
    pattern: str,
    category: str,
    priority: int = 0,
    description: str = "",
) -> int:
    """Create a new tag rule and return its id."""
    with conn:
        cur = conn.execute(
            "INSERT INTO tag_rules (pattern, category, priority, description) "
            "VALUES (?, ?, ?, ?)",
            (pattern, category, priority, description),
        )
    return int(cur.lastrowid)


def update_tag_rule(
    conn: sqlite3.Connection,
    rule_id: int,
    pattern: str,
    category: str,
    priority: int,
    description: str,
    enabled: bool,
) -> None:
    """Update an existing tag rule."""
    with conn:
        conn.execute(
            "UPDATE tag_rules SET pattern=?, category=?, priority=?, "
            "description=?, enabled=? WHERE id=?",
            (pattern, category, priority, description, int(enabled), rule_id),
        )


def delete_tag_rule(conn: sqlite3.Connection, rule_id: int) -> None:
    """Delete a tag rule by id."""
    with conn:
        conn.execute("DELETE FROM tag_rules WHERE id = ?", (rule_id,))


def get_tag_rule(conn: sqlite3.Connection, rule_id: int) -> dict | None:
    """Return a tag rule dict by id, or None."""
    row = conn.execute(
        "SELECT id, pattern, category, priority, description, enabled, created_at "
        "FROM tag_rules WHERE id = ?",
        (rule_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": int(row[0]),
        "pattern": str(row[1]),
        "category": str(row[2]),
        "priority": int(row[3]),
        "description": str(row[4]) if row[4] else "",
        "enabled": bool(row[5]),
        "created_at": row[6],
    }


def list_tag_rules(conn: sqlite3.Connection) -> list[dict]:
    """Return all tag rules ordered by priority DESC, id ASC."""
    rows = conn.execute(
        "SELECT id, pattern, category, priority, description, enabled, created_at "
        "FROM tag_rules ORDER BY priority DESC, id ASC"
    ).fetchall()
    return [
        {
            "id": int(r[0]),
            "pattern": str(r[1]),
            "category": str(r[2]),
            "priority": int(r[3]),
            "description": str(r[4]) if r[4] else "",
            "enabled": bool(r[5]),
            "created_at": r[6],
        }
        for r in rows
    ]


def backfill_all_categories(
    conn: sqlite3.Connection,
    compiled_rules: list[tuple[re.Pattern, str]],
    batch_size: int = 500,
) -> tuple[int, int]:
    """Re-apply tag rules to all log rows.

    Iterates through the logs table in batches, computes the correct
    category for each row using the current rules, and updates rows
    that differ from their stored category.

    Returns (total_rows_scanned, rows_updated).
    """
    total = 0
    updated = 0
    offset = 0

    total_rows = conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
    if total_rows == 0:
        return 0, 0

    while offset < total_rows:
        rows = conn.execute(
            "SELECT id, log, category FROM logs ORDER BY id ASC LIMIT ? OFFSET ?",
            (batch_size, offset),
        ).fetchall()
        if not rows:
            break

        with conn:
            for row in rows:
                row_id = int(row["id"])
                old_cat = str(row["category"]) if row["category"] else ""
                new_cat = categorize_log_text(str(row["log"]), compiled_rules)
                if new_cat != old_cat:
                    conn.execute(
                        "UPDATE logs SET category = ? WHERE id = ?",
                        (new_cat, row_id),
                    )
                    updated += 1

        total += len(rows)
        offset += batch_size

    return total, updated


# ── Role permissions (tag visibility per role) ────────────────────

def get_role_permission(conn: sqlite3.Connection, role: str) -> dict | None:
    """Return the tag permission config for *role*, or None."""
    row = conn.execute(
        "SELECT id, role, mode, categories, created_at "
        "FROM role_permissions WHERE role = ?",
        (role,),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": int(row[0]),
        "role": str(row[1]),
        "mode": str(row[2]),
        "categories": str(row[3]) if row[3] else "",
        "created_at": row[4],
    }


def upsert_role_permission(
    conn: sqlite3.Connection,
    role: str,
    mode: str,
    categories: str,
) -> None:
    """Insert or update a role's tag permission config."""
    with conn:
        existing = conn.execute(
            "SELECT id FROM role_permissions WHERE role = ?", (role,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE role_permissions SET mode=?, categories=? WHERE role=?",
                (mode, categories, role),
            )
        else:
            conn.execute(
                "INSERT INTO role_permissions (role, mode, categories) "
                "VALUES (?, ?, ?)",
                (role, mode, categories),
            )


def delete_role_permission(conn: sqlite3.Connection, role: str) -> None:
    """Delete a role's tag permission config."""
    with conn:
        conn.execute("DELETE FROM role_permissions WHERE role = ?", (role,))


def list_role_permissions(conn: sqlite3.Connection) -> list[dict]:
    """Return all role permission configs ordered by role ASC."""
    rows = conn.execute(
        "SELECT id, role, mode, categories, created_at "
        "FROM role_permissions ORDER BY role ASC"
    ).fetchall()
    return [
        {
            "id": int(r[0]),
            "role": str(r[1]),
            "mode": str(r[2]),
            "categories": str(r[3]) if r[3] else "",
            "created_at": r[4],
        }
        for r in rows
    ]
