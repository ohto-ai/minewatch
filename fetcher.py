"""
Log fetching — poll the API and persist to SQLite.
"""

import re
import time as _time
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from config import LOG_URL, LOG_REFERER, QUERY_TASK_CONCURRENCY, DB_PATH
from auth import get_auth_headers
from db import (
    insert_logs, count_logs, get_latest_time, claim_next_query_task,
    complete_query_task, ensure_query_task, fail_query_task, has_queued_query_task,
)
from schedule import get_interval, describe

# ESC character (0x1B) — appears as  in JSON, decoded by json.loads
ESC = "\x1b"

# Regex to strip Minecraft ANSI CSI sequences: ESC[ <params> <letter>
_ANSI_CSI = re.compile(ESC + r"\[[0-9;]*[a-zA-Z]")
_ANSI_LINE_CLEAR = re.compile(ESC + r"\[K")
_MINUTE_KEYWORD = re.compile(r"^\d{2}:\d{2}$")


def clean_log(raw: str) -> str:
    """Remove ANSI codes and control chars from a log line, returning clean text."""
    text = _ANSI_CSI.sub("", raw)          # colour / formatting codes
    text = _ANSI_LINE_CLEAR.sub("", text)  # line-clear sequences
    text = text.replace("\r", "")           # carriage return
    text = text.replace(ESC, "")            # any stray bare ESC
    return text.strip()


def fetch_logs(session: requests.Session, search: str = "") -> list[dict]:
    """
    Fetch the latest 100 log entries from the API.
    Returns a list of raw entry dicts.
    """
    resp = session.get(
        LOG_URL,
        params={"log": search},
        headers={
            **get_auth_headers(),
            "Referer": LOG_REFERER,
        },
        timeout=10,
    )
    resp.raise_for_status()
    body = resp.json()
    data = body.get("data")
    return data if isinstance(data, list) else []


def expand_second_query_tasks(conn: sqlite3.Connection, keyword: str, fetched_count: int) -> tuple[int, int]:
    """Queue second-level tasks when a minute keyword hits the remote 100-log cap."""
    if fetched_count < 100 or not _MINUTE_KEYWORD.fullmatch(keyword):
        return 0, 0

    created = 0
    reused = 0
    with conn:
        for second in range(60):
            _, is_new = ensure_query_task(conn, f"{keyword}:{second:02d}")
            if is_new:
                created += 1
            else:
                reused += 1
    return created, reused


def process_one_query_task(conn: sqlite3.Connection, session: requests.Session) -> bool:
    """Process one queued query task, if available."""
    task = claim_next_query_task(conn)
    if not task:
        return False

    task_id, keyword = task
    print(f"[task#{task_id}] running keyword={keyword!r}")

    try:
        entries = fetch_logs(session, search=keyword)
        for entry in entries:
            entry["log"] = clean_log(entry["log"])
        inserted_count, _ = insert_logs(conn, entries, since_time=0)
        second_created, second_reused = expand_second_query_tasks(conn, keyword, len(entries))
        complete_query_task(conn, task_id, fetched_count=len(entries),
                            inserted_count=inserted_count)
        print(f"[task#{task_id}] completed fetched={len(entries)} inserted={inserted_count}")
        if second_created or second_reused:
            print(
                f"[task#{task_id}] refined to seconds "
                f"created={second_created} reused={second_reused}"
            )
    except Exception as e:
        fail_query_task(conn, task_id, str(e))
        print(f"[task#{task_id}] failed: {e}")
    return True


def _query_worker(db_path: str) -> int:
    """Worker thread: claim and process query tasks until the queue is empty.

    Each worker opens its own DB connection and HTTP session so that
    multiple workers can run concurrently without contention on a single
    connection object (SQLite connections are not thread-safe).
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    session = requests.Session()
    processed = 0
    try:
        while process_one_query_task(conn, session):
            processed += 1
    finally:
        session.close()
        conn.close()
    return processed


def process_queued_query_tasks(db_path: str, max_concurrency: int) -> int:
    """Process queued query tasks using up to *max_concurrency* parallel workers.

    Each worker independently claims tasks from the database queue via
    atomic ``UPDATE … WHERE status='queued'``, so tasks are never
    processed twice.

    When *max_concurrency* is 1 the work is done in the calling thread
    (no thread-pool overhead).
    """
    # Quick exit when the queue is empty — avoids spawning threads uselessly
    check_conn = sqlite3.connect(db_path)
    try:
        if not has_queued_query_task(check_conn):
            return 0
    finally:
        check_conn.close()

    # Single-worker path: no thread overhead, preserves task_interval delay
    if max_concurrency <= 1:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        session = requests.Session()
        try:
            processed = 0
            while process_one_query_task(conn, session):
                processed += 1
            return processed
        finally:
            session.close()
            conn.close()

    # Multi-worker path: launch N threads, each draining the queue
    with ThreadPoolExecutor(max_workers=max_concurrency) as executor:
        futures = [
            executor.submit(_query_worker, db_path)
            for _ in range(max_concurrency)
        ]
        total = 0
        for f in as_completed(futures):
            total += f.result()
        return total


def poll_loop(conn: sqlite3.Connection) -> None:
    """Main polling loop — fetch, clean, store, repeat."""
    session = requests.Session()
    total_stored = count_logs(conn)
    watermark = get_latest_time(conn) or 0
    prev_interval = get_interval()

    print(f"[init] DB contains {total_stored} existing entries")
    print(f"[init] 调度: {describe()}")
    print(f"[init] 查询并发数: {QUERY_TASK_CONCURRENCY}")
    print(f"[init] Ctrl+C to stop\n")

    consecutive_errors = 0

    try:
        while True:
            loop_start = _time.monotonic()

            try:
                entries = fetch_logs(session)
                consecutive_errors = 0

                # Clean ANSI codes from each log line
                for entry in entries:
                    entry["log"] = clean_log(entry["log"])

                new_count, watermark = insert_logs(conn, entries,
                                                    since_time=watermark)
                if new_count > 0:
                    total_stored += new_count
                    latest = entries[0]["log"] if entries else ""
                    preview = latest[:90] + "..." if len(latest) > 90 else latest
                    print(f"[+{new_count:>3}] total={total_stored:<8} | {preview}")

                # Dynamic interval — may change across time boundaries
                interval = get_interval()
                if interval != prev_interval:
                    print(f"[sch] 时段切换 → {describe()}")
                    prev_interval = interval
                _process_queued = process_queued_query_tasks(DB_PATH, QUERY_TASK_CONCURRENCY)
                if _process_queued:
                    print(f"[task] batch done — {_process_queued} tasks processed")

                # Sleep precisely, accounting for request latency
                elapsed = _time.monotonic() - loop_start
                remaining = interval - elapsed
                if remaining > 0:
                    _time.sleep(remaining)

            except requests.RequestException as e:
                consecutive_errors += 1
                wait = min(consecutive_errors * 2, 30)
                print(f"[err] HTTP error (retry in {wait}s): {e}")
                _time.sleep(wait)

            except Exception as e:
                consecutive_errors += 1
                wait = min(consecutive_errors * 2, 30)
                print(f"[err] Unexpected error (retry in {wait}s): {e}")
                _time.sleep(wait)

    except KeyboardInterrupt:
        print(f"\n[stop] Interrupted — {total_stored} total entries stored")
        session.close()
