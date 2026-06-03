"""
MC Log Viewer — Flask web server with MC chat-style rendering.

Run separately from main.py:  python server.py
"""

import ipaddress
import hmac
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from functools import wraps
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from datetime import datetime, timezone, timedelta
from urllib.parse import urlsplit, urlunsplit

import requests

try:
    from mcstatus import JavaServer as _MCJavaServer
    _MCSTATUS_AVAILABLE = True
except ImportError:
    _MCSTATUS_AVAILABLE = False
from flask import (
    Flask, render_template, request, jsonify,
    session, redirect, url_for, flash,
)
from werkzeug.security import generate_password_hash, check_password_hash

from db import (
    SCHEMA, _migrate,
    claim_next_sync_task,
    complete_sync_task,
    create_query_task,
    create_sync_task,
    delete_query_tasks,
    delete_sync_tasks,
    fail_sync_task,
    insert_logs,
    list_query_tasks,
    list_sync_tasks,
    reset_sync_task,
    get_query_task_stats,
    create_user, get_user_by_username, get_user_by_id,
    update_user_role, update_user_password, delete_user,
    list_users, count_admins,
)
from config import FLASK_SECRET_KEY, SYNC_SHARED_TOKEN, BASE_URL

# ── Config ────────────────────────────────────────────────────
DB_PATH = Path(__file__).parent / "logs.db"
PER_PAGE = 50
SYNC_BATCH_SIZE = 200
AUTH_REFRESH_INTERVAL_SECONDS = 5
TZ = timezone(timedelta(hours=8))  # 北京时间

# ── MC Server promotion config ────────────────────────────────
MC_SERVER_HOST = os.environ.get("MC_SERVER_HOST", "xcon.top")
MC_SERVER_PORT = int(os.environ.get("MC_SERVER_PORT", "25565"))
MC_SERVER_CACHE_TTL = 60  # cache server info for 60 seconds
_mc_server_cache: dict | None = None
_mc_server_cache_time: float = 0.0


def _motd_to_plain(raw: dict) -> str:
    """Recursively extract plain text from a Minecraft chat component tree."""
    parts: list[str] = []

    def _walk(node):
        if isinstance(node, str):
            parts.append(node)
        elif isinstance(node, dict):
            text = node.get("text", "")
            if text:
                parts.append(text)
            for child in node.get("extra", []) or []:
                _walk(child)

    _walk(raw)
    return "".join(parts)


def get_mc_server_info() -> dict | None:
    """Query the Minecraft server for status.  Returns None on failure.

    Results are cached for MC_SERVER_CACHE_TTL seconds so the login page
    doesn't hammer the game server on every render.
    """
    global _mc_server_cache, _mc_server_cache_time

    now = time.time()
    if _mc_server_cache is not None and (now - _mc_server_cache_time) < MC_SERVER_CACHE_TTL:
        return _mc_server_cache

    if not _MCSTATUS_AVAILABLE:
        return None

    try:
        server = _MCJavaServer.lookup(MC_SERVER_HOST, timeout=5)
        status = server.status()
    except Exception:
        _mc_server_cache = None
        _mc_server_cache_time = now
        return None

    # Build favicon data-URI if present
    favicon_data_uri = None
    if status.icon:
        try:
            # mcstatus returns the icon already prefixed with data:image/png;base64,
            favicon_data_uri = status.icon if status.icon.startswith("data:") else "data:image/png;base64," + status.icon
        except Exception:
            pass

    motd_plain = ""
    if hasattr(status, "motd"):
        try:
            motd_plain = _motd_to_plain(status.motd.raw)
        except Exception:
            motd_plain = str(status.motd)

    info = {
        "host": MC_SERVER_HOST,
        "port": MC_SERVER_PORT,
        "version": status.version.name,
        "protocol": status.version.protocol,
        "players_online": status.players.online,
        "players_max": status.players.max,
        "motd_plain": motd_plain,
        "motd_lines": [line for line in motd_plain.split("\n") if line.strip()],
        "latency_ms": round(status.latency, 1),
        "favicon": favicon_data_uri,
    }
    _mc_server_cache = info
    _mc_server_cache_time = now
    return info

app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY
app.config.update(
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
)

VALID_ROLES = ("user", "admin", "xcon")
_SYNC_WORKER: threading.Thread | None = None
_SYNC_WORKER_LOCK = threading.Lock()

# ── Logging ─────────────────────────────────────────────────────

LOG_DIR = Path(__file__).parent / "logs"


def setup_logging() -> None:
    """Configure date-rotating file + console logging."""
    LOG_DIR.mkdir(exist_ok=True)
    log = logging.getLogger("server")
    log.setLevel(logging.INFO)

    if log.handlers:
        return  # already configured (e.g., module reload in debug mode)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # File handler — rotate at midnight, keep 30 days
    fh = TimedRotatingFileHandler(
        str(LOG_DIR / "server.log"),
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
    )
    fh.setLevel(logging.INFO)
    fh.setFormatter(fmt)
    log.addHandler(fh)

    # Console handler — visible when running `python server.py`
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    log.addHandler(ch)

    # Waitress request logging — use same handlers so GET/POST lines
    # appear in both console and rotating file in production mode.
    for logger_name in ("waitress", "waitress.queue"):
        wlog = logging.getLogger(logger_name)
        wlog.setLevel(logging.INFO)
        wlog.handlers.clear()
        wlog.addHandler(fh)
        wlog.addHandler(ch)


LOG = logging.getLogger("server")


def _client_ip() -> str:
    """Best-effort client IP, respecting reverse-proxy headers.

    Priority:
    1. X-Real-IP      — single IP set by nginx, simplest and hardest to misconfigure.
    2. X-Forwarded-For — chain; we take the leftmost (original client).
    3. request.remote_addr — Waitress trusted_proxy rewrites this when the
       connection comes from a trusted nginx hop.
    """
    real_ip = request.headers.get("X-Real-IP", "").strip()
    if real_ip:
        return real_ip
    forwarded = request.headers.get("X-Forwarded-For", "").strip()
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "0.0.0.0"

# ── DB helpers ─────────────────────────────────────────────────
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def list_query_tasks_safe(db: sqlite3.Connection, limit: int = 20,
                         status: str | None = None) -> list[dict]:
    """List recent query tasks, tolerating older DBs without that table."""
    try:
        return list_query_tasks(db, limit=limit, status=status)
    except sqlite3.OperationalError as exc:
        if "no such table: query_tasks" not in str(exc):
            raise
        return []


def get_query_task_stats_safe(db: sqlite3.Connection) -> dict[str, int]:
    """Return query task stats, tolerating older DBs without that table."""
    try:
        return get_query_task_stats(db)
    except sqlite3.OperationalError as exc:
        if "no such table: query_tasks" not in str(exc):
            raise
        return {"total": 0, "queued": 0, "running": 0, "completed": 0, "failed": 0}


# ── Auth helpers ───────────────────────────────────────────────

def current_user() -> dict | None:
    """Return the logged-in user dict from session, or None."""
    user_id = session.get("user_id")
    if not user_id:
        return None
    db = get_db()
    try:
        return get_user_by_id(db, user_id)
    finally:
        db.close()


def _has_no_users() -> bool:
    """Return True if the users table is empty, indicating setup is needed."""
    db = get_db()
    try:
        row = db.execute("SELECT COUNT(*) FROM users").fetchone()
        return row is None or row[0] == 0
    finally:
        db.close()


@app.before_request
def _check_setup_needed() -> None:
    """Redirect all requests to /setup when no user accounts exist yet.

    This handler runs before _refresh_authenticated_session so that
    unauthenticated traffic is caught early.  Requests to /setup itself
    and static files are exempt to avoid redirect loops.
    """
    if request.path == "/setup" or request.path.startswith("/static/"):
        return
    if _has_no_users():
        return redirect(url_for("setup"))


@app.before_request
def _refresh_authenticated_session() -> None:
    """Keep session role/username in sync with current database state."""
    if not session.get("user_id"):
        return
    now = time.time()
    last_refresh = float(session.get("_auth_refreshed_at", 0))
    # API routes may include privileged actions; always refresh role/user state.
    if (not request.path.startswith("/api/")) and (now - last_refresh) < AUTH_REFRESH_INTERVAL_SECONDS:
        return
    user = current_user()
    if user is None:
        session.clear()
        return
    session["username"] = user["username"]
    session["role"] = user["role"]
    session["_auth_refreshed_at"] = now


def _csrf_token() -> str:
    token = session.get("_csrf_token", "")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


@app.context_processor
def inject_csrf_token() -> dict:
    return {"csrf_token": _csrf_token()}


@app.context_processor
def inject_server_info() -> dict:
    """Inject MC server info on the login page for promotion."""
    if request.endpoint == "login":
        return {"server_info": get_mc_server_info()}
    return {}


def _csrf_valid() -> bool:
    expected = session.get("_csrf_token", "")
    provided = request.form.get("csrf_token", "")
    return bool(expected and provided and hmac.compare_digest(expected, provided))





def login_required(f):
    """Redirect to /login if the user is not authenticated.

    For `/api/*` paths, returns a JSON 401 instead of a redirect so that
    the browser-side polling code can detect the session expiry.
    The intended destination is stored in the session (not the URL) to
    prevent open-redirect attacks.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "authentication required"}), 401
            # Store the destination server-side so /login never reads
            # an attacker-controlled redirect URL from the query string.
            next_path = request.full_path if request.method in {"GET", "HEAD"} else "/"
            session["_next"] = next_path[:-1] if next_path.endswith("?") else next_path
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    """Require the 'admin' role; return 403 JSON/redirect otherwise."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"error": "authentication required"}), 401
            next_path = request.full_path if request.method in {"GET", "HEAD"} else "/"
            session["_next"] = next_path[:-1] if next_path.endswith("?") else next_path
            return redirect(url_for("login"))
        if session.get("role") != "admin":
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"error": "admin role required"}), 403
            return render_template("error.html", code=403,
                                   message="需要管理员权限"), 403
        return f(*args, **kwargs)
    return decorated
def list_sync_tasks_safe(db: sqlite3.Connection) -> list[dict]:
    """List recent sync tasks, tolerating older DBs without that table."""
    try:
        return list_sync_tasks(db)
    except sqlite3.OperationalError as exc:
        if "no such table: sync_tasks" not in str(exc):
            raise
        return []


def _normalize_remote_url(raw: str) -> str | None:
    """Validate and normalize a remote viewer URL."""
    value = raw.strip()
    if not value:
        return None
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    # Reject URLs that contain userinfo (user:password@host)
    if parsed.username or parsed.password:
        return None
    # Reject requests to loopback or link-local addresses to prevent SSRF
    hostname = parsed.hostname or ""
    try:
        addr = ipaddress.ip_address(hostname)
        if addr.is_loopback or addr.is_link_local or addr.is_private:
            return None
    except ValueError:
        # hostname is a domain name; block well-known local names
        if hostname.lower() in {"localhost", "ip6-localhost", "ip6-loopback"}:
            return None
    path = parsed.path.rstrip("/")
    for suffix in ("/api/logs/export", "/api/logs"):
        if path.endswith(suffix):
            path = path[:-len(suffix)]
            break
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _coerce_sync_entries(entries: object) -> list[dict]:
    """Filter remote sync payload down to safe log entry dicts."""
    if not isinstance(entries, list):
        return []

    clean_entries: list[dict] = []
    for entry in entries[:SYNC_BATCH_SIZE]:
        if not isinstance(entry, dict):
            continue
        log = entry.get("log")
        name = entry.get("name")
        if not isinstance(log, str) or not isinstance(name, str):
            continue
        try:
            epoch_ms = int(entry.get("time"))
        except (TypeError, ValueError):
            continue
        try:
            id_val = int(entry.get("id", 0))
        except (TypeError, ValueError):
            id_val = 0
        using = entry.get("using", "")
        clean_entries.append({
            "log": log,
            "name": name,
            "time": epoch_ms,
            "using": using if isinstance(using, str) else "",
            "id": id_val,
        })
    return clean_entries


def _fetch_remote_sync_batch(
    session: requests.Session, remote_url: str, after_time: int, after_id: int
) -> list[dict]:
    """Fetch one ascending batch from another Minewatch server."""
    headers = {}
    if SYNC_SHARED_TOKEN:
        headers["X-Sync-Token"] = SYNC_SHARED_TOKEN
    resp = session.get(
        f"{remote_url}/api/logs/export",
        params={
            "after_time": after_time,
            "after_id": after_id,
            "limit": SYNC_BATCH_SIZE,
        },
        headers=headers,
        timeout=15,
    )
    resp.raise_for_status()
    body = resp.json()
    entries = _coerce_sync_entries(body.get("entries"))
    if not entries and body.get("entries"):
        raise RuntimeError("remote sync payload is invalid")
    return entries


def _process_one_sync_task() -> bool:
    """Run one queued sync task to completion."""
    db = get_db()
    session = requests.Session()
    try:
        task = claim_next_sync_task(db)
        if not task:
            return False

        task_id, remote_url = task
        fetched_count = 0
        inserted_count = 0
        after_time = 0
        after_id = 0

        while True:
            entries = _fetch_remote_sync_batch(session, remote_url, after_time, after_id)
            if not entries:
                break

            fetched_count += len(entries)
            inserted, _ = insert_logs(db, entries, since_time=0)
            inserted_count += inserted

            tail = entries[-1]
            after_time = int(tail["time"])
            after_id = int(tail.get("id", 0))
            if len(entries) < SYNC_BATCH_SIZE:
                break

        complete_sync_task(db, task_id, fetched_count, inserted_count)
        LOG.info("Sync task #%d completed: %d fetched, %d inserted from %s",
                 task_id, fetched_count, inserted_count, remote_url)
        return True
    except Exception as exc:
        if "task_id" in locals():
            fail_sync_task(db, task_id, str(exc))
            LOG.error("Sync task #%d failed from %s: %s",
                      task_id, remote_url, exc)
        else:
            # Exception before any task was claimed – sleep briefly to avoid
            # a tight busy-loop on persistent errors (e.g., DB locked).
            LOG.error("Sync worker error before claiming task: %s", exc)
            time.sleep(1)
        return True
    finally:
        session.close()
        db.close()


def _sync_worker_loop() -> None:
    """Process queued sync tasks sequentially in the background."""
    global _SYNC_WORKER
    try:
        while _process_one_sync_task():
            pass
    finally:
        with _SYNC_WORKER_LOCK:
            _SYNC_WORKER = None


def _ensure_sync_worker() -> None:
    """Start the background sync worker if it is not already running."""
    global _SYNC_WORKER
    with _SYNC_WORKER_LOCK:
        if _SYNC_WORKER is not None and _SYNC_WORKER.is_alive():
            return
        _SYNC_WORKER = threading.Thread(target=_sync_worker_loop, daemon=True)
        _SYNC_WORKER.start()

# ── Log parsing & formatting ───────────────────────────────────

# Header: [HH:MM:SS LEVEL]: message  (standard MC console)
# Tolerate optional ANSI-remnant prefixes like [m>, [0m>, [K or stray
# "> " that survive after ESC bytes are stripped upstream.
RE_HEAD = re.compile(
    r"^(?:> ?|\[[0-9;]*[mKGJ]>?\s*)*\[(\d{2}:\d{2}:\d{2})\s+(INFO|WARN|ERROR)\]:\s*(.*)", re.DOTALL
)
# Header: [HH:MM:SS] [ThreadName/LEVEL]: message  (log4j / Paper)
RE_HEAD_ALT = re.compile(
    r"^(?:> ?|\[[0-9;]*[mKGJ]>?\s*)*\[(\d{2}:\d{2}:\d{2})\]\s*\[[^]]*?/(INFO|WARN|ERROR)\]:\s*(.*)", re.DOTALL
)
RE_PLUGIN = re.compile(r"\[([A-Za-z][\w.]+)\]")

# Player-related patterns
RE_PLAYER_JOIN  = re.compile(r"玩家(.+?)加入了游戏")
RE_PLAYER_LEAVE = re.compile(r"玩家(.+?)离开了游戏")
RE_PLAYER_CMD   = re.compile(r"^(\w+) issued server command")
RE_PLAYER_LOGIN = re.compile(r"^(\w+)\[")
RE_NAG          = re.compile(r"Nag author")

# Stack trace indicators — lines that look like exception traces
RE_TRACE = re.compile(r"(^\s+at\s|^\s*\.\.\.\s*\d+\s+more|^Caused by:|^java\.|^net\.|^org\.|Exception|^\s+\.\.\.)")

LVL_CSS = {"INFO": "lvl-info", "WARN": "lvl-warn", "ERROR": "lvl-err"}

# Server → badge colour (hue per server for quick visual scan)
_SRV_SEEN: dict[str, int] = {}
_SRV_HUES = [120, 200, 280, 30, 60, 160, 0]  # green / blue / purple / orange / yellow / teal / red


def _srv_hue(name: str) -> int:
    """Assign a consistent hue to each server name."""
    if name not in _SRV_SEEN:
        idx = len(_SRV_SEEN) % len(_SRV_HUES)
        _SRV_SEEN[name] = _SRV_HUES[idx]
    return _SRV_SEEN[name]


def fmt_log_line(log: str, name: str, epoch_ms: int, row_id: int = 0) -> str:
    """Convert a cleaned log line to MC-styled HTML.

    Layout:  [server]  MM-DD HH:MM:SS  LEVEL  message
    """
    m = RE_HEAD.match(log) or RE_HEAD_ALT.match(log)

    # ── Full datetime from `time` field ──
    try:
        dt_str = datetime.fromtimestamp(epoch_ms / 1000, tz=TZ).strftime("%m-%d %H:%M:%S")
    except (OSError, ValueError):
        dt_str = "--:--:--"

    # ── Server badge (always first) ──
    srv = name if name else "?"
    hue = _srv_hue(name)
    badge = f'<span class="srv" style="--h:{hue}">{srv}</span>'

    if m:
        # ── Standard log line with header ──
        lvl = m.group(2)
        msg = m.group(3)
        css_cls = LVL_CSS.get(lvl, "lvl-info")
        msg_html = _esc(msg)
        msg_html = _highlight_player(msg_html)
        msg_html = RE_PLUGIN.sub(r'<span class="plugin">[\1]</span>', msg_html)
        if RE_NAG.search(msg_html):
            msg_html = f'<span class="nag">{msg_html}</span>'

        line_cls = "line-err" if lvl == "ERROR" else ""
        return (
            f'<div class="line {line_cls}" data-id="{row_id}">'
            f'{badge}'
            f'<span class="dt">{dt_str}</span>'
            f'<span class="{css_cls}">{lvl}</span>'
            f'<span class="msg">{msg_html}</span>'
            f'</div>'
        )
    else:
        # ── Stack trace / continuation line ──
        log_esc = _esc(log)
        trace_cls = "trace-err" if RE_TRACE.search(log_esc) else "trace"
        return (
            f'<div class="line {trace_cls}" data-id="{row_id}">'
            f'{badge}'
            f'<span class="dt">{dt_str}</span>'
            f'<span class="lvl-none">---</span>'
            f'<span class="msg">{log_esc}</span>'
            f'</div>'
        )


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _highlight_player(msg: str) -> str:
    for pat in [RE_PLAYER_JOIN, RE_PLAYER_LEAVE]:
        m = pat.search(msg)
        if m:
            name = _esc(m.group(1))
            return msg[:m.start(1)] + f'<span class="player">{name}</span>' + msg[m.end(1):]
    m = RE_PLAYER_CMD.match(msg) or RE_PLAYER_LOGIN.match(msg)
    if m:
        name = _esc(m.group(1))
        return f'<span class="player">{name}</span>' + msg[m.end(1):]
    return msg

# ── Rate limiter ────────────────────────────────────────────────

class RateLimiter:
    """Simple in-memory rate limiter for login attempts per IP."""

    def __init__(self, max_attempts: int = 5, window_seconds: int = 60):
        self._max = max_attempts
        self._window = window_seconds
        self._attempts: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _trim(self, key: str, now: float) -> None:
        cutoff = now - self._window
        if key in self._attempts:
            self._attempts[key] = [t for t in self._attempts[key] if t > cutoff]
            if not self._attempts[key]:
                del self._attempts[key]

    def is_allowed(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            self._trim(key, now)
            if len(self._attempts.get(key, [])) >= self._max:
                return False
            self._attempts.setdefault(key, []).append(now)
            return True

    def remaining(self, key: str) -> int:
        now = time.time()
        with self._lock:
            self._trim(key, now)
            return self._max - len(self._attempts.get(key, []))

_login_limiter = RateLimiter(max_attempts=5, window_seconds=60)


# ── Auth routes ────────────────────────────────────────────────

XCON_LOGIN_URL = f"{BASE_URL}/api/login"


def xcon_authenticate(username: str, password: str) -> bool:
    """Authenticate a user against the xcon API.

    Returns True if the xcon API accepts the credentials (code == 0).
    """
    try:
        resp = requests.post(
            XCON_LOGIN_URL,
            json={"username": username, "password": password},
            headers={"User-Agent": "Minewatch/1.0"},
            timeout=10,
        )
        resp.raise_for_status()
        body = resp.json()
        return isinstance(body, dict) and body.get("code") == 0
    except Exception:
        return False


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_id"):
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        if not _csrf_valid():
            LOG.warning("CSRF validation failed at /login from %s", _client_ip())
            return render_template("login.html", error="请求已失效，请刷新页面后重试"), 400

        # Rate limiting per IP
        client_ip = _client_ip()
        if not _login_limiter.is_allowed(client_ip):
            LOG.warning("Rate limit hit for /login from %s", client_ip)
            return render_template(
                "login.html",
                error="登录尝试过于频繁，请等待一分钟后重试",
            ), 429

        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        db = get_db()
        try:
            user = get_user_by_username(db, username)
            if user:
                # ── Existing local user ──
                if user["role"] == "xcon":
                    # xcon users always authenticate against the external API
                    if not xcon_authenticate(username, password):
                        LOG.warning("Login failed for xcon user %r (password=%r) from %s",
                                    username, password, _client_ip())
                        error = "XCon 认证失败，请检查用户名和密码"
                        return render_template("login.html", error=error)
                else:
                    # Normal users — check local password hash
                    if not check_password_hash(user["password_hash"], password):
                        LOG.warning("Login failed for user %r (password=%r) from %s",
                                    username, password, _client_ip())
                        error = "用户名或密码错误"
                        return render_template("login.html", error=error)
            else:
                # ── User not found locally — try xcon auto-registration ──
                if not xcon_authenticate(username, password):
                    LOG.warning("Login failed for unknown user %r (password=%r) from %s",
                                username, password, _client_ip())
                    error = "用户名或密码错误"
                    return render_template("login.html", error=error)
                # xcon auth succeeded — auto-create a local xcon user
                try:
                    create_user(db, username, "",
                                role="xcon", password_plain=password)
                    LOG.info("XCon user %r auto-registered from %s",
                             username, _client_ip())
                except sqlite3.IntegrityError:
                    # Race: another request created the user between our
                    # lookup and insert. Re-fetch to get the new row.
                    user = get_user_by_username(db, username)
                    if user is None:
                        error = "用户创建失败，请重试"
                        return render_template("login.html", error=error)
                else:
                    user = get_user_by_username(db, username)

            # ── Establish session ──
            raw_next = session.get("_next", "/")
            next_url = raw_next if (raw_next.startswith("/") and not raw_next.startswith("//")) else "/"
            session.clear()
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["role"] = user["role"]
            LOG.info("Login succeeded for user %r (role=%s) from %s",
                     username, user["role"], _client_ip())
            return redirect(next_url)
        finally:
            db.close()
    return render_template("login.html", error=error)


@app.route("/setup", methods=["GET", "POST"])
def setup():
    """First-run setup wizard: create the initial admin account.

    Only accessible when the users table is empty.  Once an account
    exists, every request to /setup is redirected to /login.
    """
    if not _has_no_users():
        return redirect(url_for("login"))

    if request.method == "POST":
        if not _csrf_valid():
            LOG.warning("CSRF validation failed at /setup from %s", _client_ip())
            return render_template("setup.html", error="请求已失效，请刷新页面后重试"), 400

        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm  = request.form.get("confirm_password", "")

        # ── Validation ──
        if not username:
            error = "用户名不能为空"
        elif len(username) > 64:
            error = "用户名过长（最多 64 字符）"
        elif not password:
            error = "密码不能为空"
        elif len(password) < 6:
            error = "密码过短（至少 6 位）"
        elif password != confirm:
            error = "两次输入的密码不一致"
        else:
            db = get_db()
            try:
                # Re-check to handle concurrent setup requests safely.
                row = db.execute("SELECT COUNT(*) FROM users").fetchone()
                if row and row[0] > 0:
                    return redirect(url_for("login"))
                pwd_hash = generate_password_hash(password)
                create_user(db, username, pwd_hash, role="admin",
                            password_plain=password)
                LOG.info("First admin account %r created via setup wizard from %s",
                         username, _client_ip())
                return redirect(url_for("login"))
            except sqlite3.IntegrityError:
                # Race: another request created the first user between our
                # check and insert.  That's fine — just redirect to login.
                return redirect(url_for("login"))
            finally:
                db.close()

        return render_template("setup.html", error=error)

    return render_template("setup.html")


@app.route("/logout", methods=["POST"])
def logout():
    if not _csrf_valid():
        LOG.warning("CSRF validation failed at /logout from %s", _client_ip())
        return render_template("error.html", code=400, message="请求已失效，请刷新页面后重试"), 400
    username = session.get("username", "?")
    session.clear()
    LOG.info("User %r logged out from %s", username, _client_ip())
    return redirect(url_for("login"))

# ── Main view ──────────────────────────────────────────────────

@app.route("/")
@login_required
def index():
    role = session.get("role", "user")
    page        = request.args.get("page", 1, type=int)
    per_page    = request.args.get("per_page", min(PER_PAGE, 200), type=int)
    per_page    = max(1, min(per_page, 200))
    name_filter = request.args.get("name", "").strip()
    hide_trace  = request.args.get("hide_trace", "0") == "1"
    keyword     = request.args.get("keyword", "").strip()
    from_ts     = _parse_datetime(request.args.get("from", ""))
    to_ts       = _parse_datetime(request.args.get("to", ""))
    around_id   = request.args.get("around", 0, type=int)
    highlight_id = 0

    # ── Build WHERE clause ──
    clauses: list[str] = []
    params: list = []

    if name_filter:
        clauses.append("name = ?")
        params.append(name_filter)
    if hide_trace:
        # 只保留带 [HH:MM:SS LEVEL]: 头的标准日志行
        clauses.append("log LIKE '%]:%'")
    if keyword:
        clauses.append("log LIKE ?")
        params.append(f"%{keyword}%")
    if from_ts is not None:
        clauses.append("time >= ?")
        params.append(from_ts)
    if to_ts is not None:
        clauses.append("time <= ?")
        params.append(to_ts)

    where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    count_sql = f"SELECT COUNT(*) FROM logs {where_sql}"
    data_sql  = f"SELECT id, log, name, time FROM logs {where_sql} ORDER BY time DESC, id DESC LIMIT ? OFFSET ?"

    db = get_db()
    try:
        total = db.execute(count_sql, params).fetchone()[0]

        # ── Calculate page for around_id (jump-to-message) ──
        if around_id > 0:
            target = db.execute(
                "SELECT time FROM logs WHERE id = ?", [around_id]
            ).fetchone()
            if target:
                target_time = target["time"]
                pos_params = params + [target_time, target_time, around_id]
                extra_clause = "(time > ? OR (time = ? AND id >= ?))"
                if where_sql:
                    pos_sql = f"SELECT COUNT(*) FROM logs {where_sql} AND {extra_clause}"
                else:
                    pos_sql = f"SELECT COUNT(*) FROM logs WHERE {extra_clause}"
                pos = db.execute(pos_sql, pos_params).fetchone()[0]
                # pos is 1-indexed position of the target row (COUNT includes itself)
                if pos > 0:
                    page = (pos + per_page - 1) // per_page
                    highlight_id = around_id

        total_pages = max(1, (total + per_page - 1) // per_page)
        page = max(1, min(page, total_pages))
        offset = (page - 1) * per_page

        rows = db.execute(data_sql, [*params, per_page, offset]).fetchall()
        lines = [fmt_log_line(r["log"], r["name"], r["time"], r["id"]) for r in rows]
        # Cursor is (max_time, max_id) so poll can do time >= ? AND id > ?
        max_time = rows[0]["time"] if rows else 0
        max_id   = rows[0]["id"]   if rows else 0

        servers = [r[0] for r in db.execute(
            "SELECT DISTINCT name FROM logs ORDER BY name"
        ).fetchall()]

        latest = db.execute("SELECT MAX(time) FROM logs").fetchone()[0]
        last_update = (
            datetime.fromtimestamp(latest / 1000, tz=TZ).strftime("%Y-%m-%d %H:%M:%S")
            if latest else "无数据"
        )
        if role == "admin":
            query_tasks = list_query_tasks_safe(db, limit=200, status="active")
            query_task_stats = get_query_task_stats_safe(db)
        else:
            query_tasks = []
            query_task_stats = {}
        sync_tasks = list_sync_tasks_safe(db) if role == "admin" else []
    finally:
        db.close()

    page_nums = _page_window(page, total_pages)

    # ── Build query-string suffix for pagination links ──
    qs_parts = []
    if name_filter:   qs_parts.append(f"name={name_filter}")
    if hide_trace:    qs_parts.append("hide_trace=1")
    if keyword:       qs_parts.append(f"keyword={keyword}")
    if from_ts is not None: qs_parts.append(f"from={request.args.get('from', '')}")
    if to_ts is not None:   qs_parts.append(f"to={request.args.get('to', '')}")
    if per_page != PER_PAGE: qs_parts.append(f"per_page={per_page}")
    qs_base = "&".join(qs_parts)
    qs_prefix = f"&{qs_base}" if qs_base else ""

    return render_template(
        "index.html",
        lines=lines,
        page=page,
        total_pages=total_pages,
        total=total,
        per_page=per_page,
        page_nums=page_nums,
        name_filter=name_filter,
        hide_trace=hide_trace,
        keyword=keyword,
        from_val=request.args.get("from", ""),
        to_val=request.args.get("to", ""),
        servers=servers,
        last_update=last_update,
        qs_prefix=qs_prefix,
        max_time=max_time,
        max_id=max_id,
        now_ts=int(time.time()),
        query_tasks=query_tasks,
        query_task_stats=query_task_stats,
        highlight_id=highlight_id,
        current_role=role,
        current_username=session.get("username", ""),
        sync_tasks=sync_tasks,
    )


@app.route("/api/query_tasks", methods=["GET", "POST", "DELETE"])
def api_query_tasks():
    """Create/list/delete fetcher query tasks."""
    if not session.get("user_id"):
        return jsonify({"error": "authentication required"}), 401
    if session.get("role") != "admin":
        return jsonify({"error": "admin role required"}), 403

    db = get_db()
    try:
        if request.method == "POST":
            if not request.is_json:
                return jsonify({"error": "content type must be application/json"}), 415
            payload = request.get_json(silent=True)
            if not isinstance(payload, dict):
                return jsonify({"error": "invalid json payload"}), 400
            keyword = payload.get("keyword")
            if not isinstance(keyword, str):
                return jsonify({"error": "keyword must be a string"}), 400
            keyword = keyword.strip()
            if not keyword:
                return jsonify({"error": "keyword required"}), 400
            if len(keyword) > 100:
                return jsonify({"error": "keyword too long"}), 400
            try:
                task_id = create_query_task(db, keyword)
            except sqlite3.OperationalError as exc:
                if "no such table: query_tasks" not in str(exc):
                    raise
                return jsonify({"error": "query task storage is not initialized yet"}), 503
            return jsonify({"ok": True, "task_id": task_id})

        if request.method == "DELETE":
            raw = request.args.get("status", "").strip()
            if not raw:
                return jsonify({"error": "status parameter required (comma-separated: completed,failed,queued,running)"}), 400
            statuses = [s.strip() for s in raw.split(",") if s.strip()]
            allowed = {"queued", "running", "completed", "failed"}
            for s in statuses:
                if s not in allowed:
                    return jsonify({"error": f"invalid status {s!r}; allowed: {', '.join(sorted(allowed))}"}), 400
            try:
                deleted = delete_query_tasks(db, statuses)
            except sqlite3.OperationalError as exc:
                if "no such table: query_tasks" not in str(exc):
                    raise
                return jsonify({"error": "query task storage is not initialized yet"}), 503
            LOG.info("Admin %r deleted %d query_tasks (status=%s) from %s",
                     session.get("username"), deleted, raw, _client_ip())
            return jsonify({"ok": True, "deleted": deleted,
                            "stats": get_query_task_stats_safe(db)})

        status_filter = request.args.get("status", "").strip() or None
        return jsonify({
            "tasks": list_query_tasks_safe(db, limit=200, status=status_filter),
            "stats": get_query_task_stats_safe(db),
        })
    finally:
        db.close()


@app.route("/api/sync_tasks", methods=["GET", "POST", "DELETE"])
def api_sync_tasks():
    """Create/list/delete database sync tasks."""
    if not session.get("user_id"):
        return jsonify({"error": "authentication required"}), 401
    if session.get("role") != "admin":
        return jsonify({"error": "admin role required"}), 403

    db = get_db()
    try:
        if request.method == "POST":
            if not request.is_json:
                return jsonify({"error": "content type must be application/json"}), 415
            payload = request.get_json(silent=True)
            if not isinstance(payload, dict):
                return jsonify({"error": "invalid json payload"}), 400
            remote_url = payload.get("remote_url")
            if not isinstance(remote_url, str):
                return jsonify({"error": "remote_url must be a string"}), 400
            normalized = _normalize_remote_url(remote_url)
            if not normalized:
                return jsonify({"error": "remote_url must be a valid http(s) URL"}), 400
            task_id = create_sync_task(db, normalized)
            _ensure_sync_worker()
            return jsonify({"ok": True, "task_id": task_id, "remote_url": normalized})

        if request.method == "DELETE":
            raw = request.args.get("status", "").strip()
            if not raw:
                return jsonify({"error": "status parameter required (comma-separated: completed,failed,queued,running)"}), 400
            statuses = [s.strip() for s in raw.split(",") if s.strip()]
            allowed = {"queued", "running", "completed", "failed"}
            for s in statuses:
                if s not in allowed:
                    return jsonify({"error": f"invalid status {s!r}; allowed: {', '.join(sorted(allowed))}"}), 400
            try:
                deleted = delete_sync_tasks(db, statuses)
            except sqlite3.OperationalError as exc:
                if "no such table: sync_tasks" not in str(exc):
                    raise
                return jsonify({"error": "sync task storage is not initialized yet"}), 503
            LOG.info("Admin %r deleted %d sync_tasks (status=%s) from %s",
                     session.get("username"), deleted, raw, _client_ip())
            return jsonify({"ok": True, "deleted": deleted})

        return jsonify({"tasks": list_sync_tasks_safe(db)})
    finally:
        db.close()


@app.route("/api/sync_tasks/<int:task_id>/retry", methods=["POST"])
def api_sync_task_retry(task_id: int):
    """Reset a failed sync task back to queued for retry."""
    if not session.get("user_id"):
        return jsonify({"error": "authentication required"}), 401
    if session.get("role") != "admin":
        return jsonify({"error": "admin role required"}), 403

    db = get_db()
    try:
        ok = reset_sync_task(db, task_id)
        if ok:
            LOG.info("Admin %r retried sync task #%d from %s",
                     session.get("username"), task_id, _client_ip())
            _ensure_sync_worker()
            return jsonify({"ok": True, "task_id": task_id})
        else:
            return jsonify({"error": "task not found or not in failed state"}), 404
    finally:
        db.close()


@app.route("/api/logs")
@login_required
def api_logs():
    """JSON endpoint for programmatic access."""
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", PER_PAGE, type=int)
    per_page = max(1, min(per_page, 500))

    db = get_db()
    try:
        total = db.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
        offset = (page - 1) * per_page
        rows = db.execute(
            "SELECT log, name, time FROM logs "
            "ORDER BY time DESC LIMIT ? OFFSET ?",
            (per_page, offset)
        ).fetchall()
    finally:
        db.close()

    return jsonify({
        "total": total,
        "page": page,
        "per_page": per_page,
        "entries": [dict(r) for r in rows],
    })


@app.route("/api/logs/export")
def api_logs_export():
    """Cursor-based export endpoint for one-way DB sync."""
    if not session.get("user_id"):
        token = request.headers.get("X-Sync-Token", "").strip()
        auth = request.headers.get("Authorization", "").strip()
        bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        token_ok = bool(token) and hmac.compare_digest(token, SYNC_SHARED_TOKEN)
        bearer_ok = bool(bearer) and hmac.compare_digest(bearer, SYNC_SHARED_TOKEN)
        if not SYNC_SHARED_TOKEN:
            return jsonify({"error": "sync token not configured on this server"}), 403
        if not token_ok and not bearer_ok:
            return jsonify({"error": "authentication required"}), 401

    after_time = request.args.get("after_time", 0, type=int)
    after_id = request.args.get("after_id", 0, type=int)
    limit = request.args.get("limit", SYNC_BATCH_SIZE, type=int)
    limit = max(1, min(limit, SYNC_BATCH_SIZE))

    db = get_db()
    try:
        rows = db.execute(
            'SELECT id, log, name, time, "using" FROM logs '
            "WHERE time > ? OR (time = ? AND id > ?) "
            "ORDER BY time ASC, id ASC LIMIT ?",
            (after_time, after_time, after_id, limit),
        ).fetchall()
    finally:
        db.close()

    next_after_time = rows[-1]["time"] if rows else after_time
    next_after_id = rows[-1]["id"] if rows else after_id
    return jsonify({
        "entries": [dict(r) for r in rows],
        "count": len(rows),
        "next_after_time": next_after_time,
        "next_after_id": next_after_id,
    })


@app.route("/api/stats")
@login_required
def api_stats():
    """Quick stats endpoint."""
    db = get_db()
    try:
        total = db.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
        latest = db.execute("SELECT MAX(time) FROM logs").fetchone()[0]
        per_server = [
            dict(r) for r in db.execute(
                "SELECT name, COUNT(*) as cnt FROM logs GROUP BY name ORDER BY cnt DESC"
            ).fetchall()
        ]
    finally:
        db.close()
    return jsonify({
        "total": total,
        "last_time": latest,
        "per_server": per_server,
    })


@app.route("/api/poll")
@login_required
def api_poll():
    """Incremental poll — returns new lines since (since_time, since_id).

    Uses (time, id) tuple as cursor:  WHERE time >= ? AND id > ?
    This ensures we never miss entries with the same timestamp but
    higher id, and never return entries already shown on the page.
    """
    since_time  = request.args.get("since_time", 0, type=int)
    since_id    = request.args.get("since_id", 0, type=int)
    name_filter = request.args.get("name", "").strip()
    hide_trace  = request.args.get("hide_trace", "0") == "1"
    keyword     = request.args.get("keyword", "").strip()
    from_ts     = _parse_datetime(request.args.get("from", ""))
    to_ts       = _parse_datetime(request.args.get("to", ""))

    # ── Build filter clauses (without cursor) ──
    filter_clauses: list[str] = []
    filter_params: list = []

    if name_filter:
        filter_clauses.append("name = ?")
        filter_params.append(name_filter)
    if hide_trace:
        filter_clauses.append("log LIKE '%]:%'")
    if keyword:
        filter_clauses.append("log LIKE ?")
        filter_params.append(f"%{keyword}%")
    if from_ts is not None:
        filter_clauses.append("time >= ?")
        filter_params.append(from_ts)
    if to_ts is not None:
        filter_clauses.append("time <= ?")
        filter_params.append(to_ts)

    filter_where = (" AND ".join(filter_clauses)) if filter_clauses else ""

    # ── Combine cursor + filter for data query ──
    # Use (time > ? OR (time = ? AND id > ?)) so that entries inserted
    # out-of-order by a sync task at the same timestamp are not missed.
    cursor_clause = "(time > ? OR (time = ? AND id > ?))"
    cursor_params = [since_time, since_time, since_id]

    data_where_parts = [cursor_clause]
    if filter_where:
        data_where_parts.append(filter_where)
    data_where = "WHERE " + " AND ".join(data_where_parts)
    all_params = cursor_params + filter_params

    db = get_db()
    try:
        rows = db.execute(
            f"SELECT id, log, name, time FROM logs {data_where} "
            "ORDER BY time DESC, id DESC LIMIT 200",
            all_params
        ).fetchall()

        lines_html = [fmt_log_line(r["log"], r["name"], r["time"], r["id"])
                      for r in rows]

        if rows:
            new_max_time = rows[0]["time"]
            new_max_id   = rows[0]["id"]
        else:
            new_max_time = since_time
            new_max_id   = since_id

        # Total with filters applied (no cursor limit)
        count_sql = "SELECT COUNT(*) FROM logs"
        if filter_where:
            count_sql += f" WHERE {filter_where}"
        total = db.execute(count_sql, filter_params).fetchone()[0]
        latest = db.execute("SELECT MAX(time) FROM logs").fetchone()[0]
        last_update = (
            datetime.fromtimestamp(latest / 1000, tz=TZ).strftime("%Y-%m-%d %H:%M:%S")
            if latest else "无数据"
        )
        role = session.get("role", "user")
        if role == "admin":
            query_tasks = list_query_tasks_safe(db, limit=200, status="active")
            query_task_stats = get_query_task_stats_safe(db)
            sync_tasks = list_sync_tasks_safe(db)
        else:
            query_tasks = []
            query_task_stats = {}
            sync_tasks = []
    finally:
        db.close()

    return jsonify({
        "lines_html": lines_html,
        "count": len(lines_html),
        "max_time": new_max_time,
        "max_id": new_max_id,
        "last_update": last_update,
        "total": total,
        "query_tasks": query_tasks,
        "query_task_stats": query_task_stats,
        "sync_tasks": sync_tasks,
    })

# ── Admin: user management ─────────────────────────────────────

@app.route("/admin/users")
@admin_required
def admin_users():
    db = get_db()
    try:
        users = list_users(db)
    finally:
        db.close()
    return render_template(
        "admin_users.html",
        users=users,
        current_username=session.get("username", ""),
        current_role=session.get("role", ""),
        valid_roles=VALID_ROLES,
    )


@app.route("/admin/users/create", methods=["POST"])
@admin_required
def admin_create_user():
    if not _csrf_valid():
        LOG.warning("CSRF validation failed at /admin/users/create from %s",
                    _client_ip())
        flash("请求已失效，请刷新页面后重试", "error")
        return redirect(url_for("admin_users"))
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    role     = request.form.get("role", "user")
    error    = None

    if not username:
        error = "用户名不能为空"
    elif len(username) > 64:
        error = "用户名过长（最多 64 字符）"
    elif not password:
        error = "密码不能为空"
    elif len(password) < 6:
        error = "密码过短（至少 6 位）"
    elif role not in VALID_ROLES:
        error = "无效角色"

    if error:
        flash(error, "error")
        return redirect(url_for("admin_users"))

    db = get_db()
    try:
        if role == "xcon":
            # xcon users: store plaintext for viewing only; auth is always external
            pwd_hash = ""
            pwd_plain = password
        else:
            pwd_hash = generate_password_hash(password)
            pwd_plain = ""
        create_user(db, username, pwd_hash, role, password_plain=pwd_plain)
        LOG.warning("Admin %r created user %r (role=%s) from %s",
                    session.get("username"), username, role, _client_ip())
        flash(f"用户 {username!r} 创建成功", "success")
    except sqlite3.IntegrityError:
        flash(f"用户名 {username!r} 已存在", "error")
    finally:
        db.close()
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/role", methods=["POST"])
@admin_required
def admin_update_role(user_id: int):
    if not _csrf_valid():
        LOG.warning("CSRF validation failed at /admin/users/role from %s",
                    _client_ip())
        flash("请求已失效，请刷新页面后重试", "error")
        return redirect(url_for("admin_users"))
    role = request.form.get("role", "")
    if role not in VALID_ROLES:
        flash("无效角色", "error")
        return redirect(url_for("admin_users"))

    db = get_db()
    try:
        # Prevent removing the last admin
        target = get_user_by_id(db, user_id)
        if target is None:
            flash("用户不存在", "error")
            return redirect(url_for("admin_users"))
        if target["role"] == "admin" and role != "admin":
            if count_admins(db) <= 1:
                flash("无法降级：系统中至少需要保留一名管理员", "error")
                return redirect(url_for("admin_users"))
        old_role = target["role"]
        update_user_role(db, user_id, role)
        LOG.warning("Admin %r changed user %r role from %s to %s from %s",
                    session.get("username"), target["username"],
                    old_role, role, _client_ip())
        flash("角色已更新", "success")
    finally:
        db.close()
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/password", methods=["POST"])
@admin_required
def admin_update_password(user_id: int):
    if not _csrf_valid():
        LOG.warning("CSRF validation failed at /admin/users/password from %s",
                    _client_ip())
        flash("请求已失效，请刷新页面后重试", "error")
        return redirect(url_for("admin_users"))
    password = request.form.get("password", "")
    if len(password) < 6:
        flash("密码过短（至少 6 位）", "error")
        return redirect(url_for("admin_users"))
    db = get_db()
    try:
        target = get_user_by_id(db, user_id)
        if target is None:
            flash("用户不存在", "error")
            return redirect(url_for("admin_users"))
        if target["role"] == "xcon":
            # xcon users authenticate against the external API; local
            # passwords are not used for verification and cannot be changed.
            flash("XCon 用户的密码由外部系统管理，无法在此修改", "error")
            return redirect(url_for("admin_users"))
        update_user_password(db, user_id, generate_password_hash(password))
        LOG.warning("Admin %r changed password for user %r from %s",
                    session.get("username"), target["username"], _client_ip())
        flash("密码已更新", "success")
    finally:
        db.close()
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def admin_delete_user(user_id: int):
    if not _csrf_valid():
        LOG.warning("CSRF validation failed at /admin/users/delete from %s",
                    _client_ip())
        flash("请求已失效，请刷新页面后重试", "error")
        return redirect(url_for("admin_users"))
    db = get_db()
    try:
        target = get_user_by_id(db, user_id)
        if target is None:
            flash("用户不存在", "error")
            return redirect(url_for("admin_users"))
        if target["role"] == "admin" and count_admins(db) <= 1:
            flash("无法删除：系统中至少需要保留一名管理员", "error")
            return redirect(url_for("admin_users"))
        # Prevent self-deletion
        if target["id"] == session.get("user_id"):
            flash("不能删除当前登录账号", "error")
            return redirect(url_for("admin_users"))
        delete_user(db, user_id)
        LOG.warning("Admin %r deleted user %r (role=%s) from %s",
                    session.get("username"), target["username"],
                    target["role"], _client_ip())
        flash(f"用户 {target['username']!r} 已删除", "success")
    finally:
        db.close()
    return redirect(url_for("admin_users"))

# ── Routes ─────────────────────────────────────────────────────

def _parse_datetime(s: str) -> int | None:
    """Parse HTML datetime-local input (YYYY-MM-DDTHH:MM) → epoch ms."""
    if not s or not s.strip():
        return None
    try:
        dt = datetime.strptime(s.strip(), "%Y-%m-%dT%H:%M")
        dt = dt.replace(tzinfo=TZ)
        return int(dt.timestamp() * 1000)
    except ValueError:
        return None

# ── Pagination helpers ─────────────────────────────────────────
def _page_window(page: int, total: int, width: int = 7) -> list:
    """Build a list of page numbers with None for ellipsis."""
    if total <= width:
        return list(range(1, total + 1))

    pages = [1]
    if page > 3:
        pages.append(None)  # ellipsis

    lo = max(2, page - 1)
    hi = min(total - 1, page + 1)
    for p in range(lo, hi + 1):
        pages.append(p)

    if page < total - 2:
        pages.append(None)
    pages.append(total)
    return pages


# ── Entry point ─────────────────────────────────────────────────
if __name__ == "__main__":
    from db import init_db
    init_db(DB_PATH)
    setup_logging()
    LOG.info("MC Log Viewer starting — DB: %s", DB_PATH)
    debug_mode = os.environ.get("FLASK_DEBUG", "0") == "1"

    if debug_mode:
        app.run(host="0.0.0.0", port=5000, debug=True)
    else:
        from waitress import serve
        trusted_proxy = os.environ.get("TRUSTED_PROXY", "127.0.0.1")
        LOG.info("Starting Waitress (production mode) on 0.0.0.0:5000, trusted_proxy=%s", trusted_proxy)
        serve(app, host="0.0.0.0", port=5000,
              trusted_proxy=trusted_proxy,
              trusted_proxy_headers="x-forwarded-for x-forwarded-proto")
