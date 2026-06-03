"""
MC Log Viewer — Flask web server with MC chat-style rendering.

Run separately from main.py:  python server.py
"""

import re
import sqlite3
import time
from functools import wraps
from pathlib import Path
from datetime import datetime, timezone, timedelta

from flask import (
    Flask, render_template, request, jsonify,
    session, redirect, url_for, flash,
)
from werkzeug.security import generate_password_hash, check_password_hash

from db import (
    create_query_task, list_query_tasks,
    create_user, get_user_by_username, get_user_by_id,
    update_user_role, update_user_password, delete_user,
    list_users, count_admins,
)
from config import FLASK_SECRET_KEY, ADMIN_USERNAME, ADMIN_PASSWORD

# ── Config ────────────────────────────────────────────────────
DB_PATH = Path(__file__).parent / "logs.db"
PER_PAGE = 50
TZ = timezone(timedelta(hours=8))  # 北京时间

app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY

VALID_ROLES = ("user", "admin")

# ── DB helpers ─────────────────────────────────────────────────
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def list_query_tasks_safe(db: sqlite3.Connection) -> list[dict]:
    """List recent query tasks, tolerating older DBs without that table."""
    try:
        return list_query_tasks(db)
    except sqlite3.OperationalError as exc:
        if "no such table: query_tasks" not in str(exc):
            raise
        return []


def _ensure_default_admin() -> None:
    """Create the default admin account if no users exist yet."""
    db = get_db()
    try:
        row = db.execute("SELECT COUNT(*) FROM users").fetchone()
        if row and row[0] == 0:
            pwd_hash = generate_password_hash(ADMIN_PASSWORD)
            create_user(db, ADMIN_USERNAME, pwd_hash, role="admin")
            print(f"[auth] Default admin created: username={ADMIN_USERNAME!r}")
    except sqlite3.OperationalError:
        pass  # users table may not exist yet (init_db called separately)
    finally:
        db.close()

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
            session["_next"] = request.path
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def user_required(f):
    """Require at least the 'user' role (or 'admin')."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            return jsonify({"error": "authentication required"}), 401
        role = session.get("role", "")
        if role not in ("user", "admin"):
            return jsonify({"error": "permission denied"}), 403
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    """Require the 'admin' role; return 403 JSON/redirect otherwise."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"error": "authentication required"}), 401
            return redirect(url_for("login", next=request.path))
        if session.get("role") != "admin":
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"error": "admin role required"}), 403
            return render_template("error.html", code=403,
                                   message="需要管理员权限"), 403
        return f(*args, **kwargs)
    return decorated

# ── Log parsing & formatting ───────────────────────────────────

# Header: [HH:MM:SS LEVEL]: message  (standard MC console)
RE_HEAD = re.compile(
    r"^>?\s*\[(\d{2}:\d{2}:\d{2})\s+(INFO|WARN|ERROR)\]:\s*(.*)", re.DOTALL
)
# Header: [HH:MM:SS] [ThreadName/LEVEL]: message  (log4j / Paper)
RE_HEAD_ALT = re.compile(
    r"^>?\s*\[(\d{2}:\d{2}:\d{2})\]\s*\[[^]]*?/(INFO|WARN|ERROR)\]:\s*(.*)", re.DOTALL
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

# ── Auth routes ────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_id"):
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        db = get_db()
        try:
            user = get_user_by_username(db, username)
        finally:
            db.close()
        if user and check_password_hash(user["password_hash"], password):
            # Read the intended destination stored server-side before clearing session
            raw_next = session.get("_next", "/")
            next_url = raw_next if (raw_next.startswith("/") and not raw_next.startswith("//")) else "/"
            session.clear()
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["role"] = user["role"]
            return redirect(next_url)
        error = "用户名或密码错误"
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ── Main view ──────────────────────────────────────────────────

@app.route("/")
@login_required
def index():
    page        = request.args.get("page", 1, type=int)
    per_page    = request.args.get("per_page", min(PER_PAGE, 200), type=int)
    per_page    = min(per_page, 200)
    name_filter = request.args.get("name", "").strip()
    hide_trace  = request.args.get("hide_trace", "0") == "1"
    keyword     = request.args.get("keyword", "").strip()
    from_ts     = _parse_datetime(request.args.get("from", ""))
    to_ts       = _parse_datetime(request.args.get("to", ""))

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
        query_tasks = list_query_tasks_safe(db)
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

    role = session.get("role", "user")

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
        current_role=role,
        current_username=session.get("username", ""),
    )


@app.route("/api/query_tasks", methods=["GET", "POST"])
def api_query_tasks():
    """Create/list fetcher query tasks."""
    # GET is read-only → login required; POST mutates → user/admin required
    if not session.get("user_id"):
        return jsonify({"error": "authentication required"}), 401
    if request.method == "POST" and session.get("role") not in ("user", "admin"):
        return jsonify({"error": "permission denied"}), 403

    db = get_db()
    try:
        if request.method == "POST":
            payload = request.get_json(silent=True) or {}
            keyword = payload.get("keyword") if isinstance(payload, dict) else ""
            if not keyword:
                keyword = request.form.get("keyword", "")
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

        return jsonify({"tasks": list_query_tasks_safe(db)})
    finally:
        db.close()


@app.route("/api/logs")
@login_required
def api_logs():
    """JSON endpoint for programmatic access."""
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", PER_PAGE, type=int)
    per_page = min(per_page, 500)

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
    cursor_clause = "(time >= ? AND id > ?)"
    cursor_params = [since_time, since_id]

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
        query_tasks = list_query_tasks_safe(db)
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
        pwd_hash = generate_password_hash(password)
        create_user(db, username, pwd_hash, role)
        flash(f"用户 {username!r} 创建成功", "success")
    except sqlite3.IntegrityError:
        flash(f"用户名 {username!r} 已存在", "error")
    finally:
        db.close()
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/role", methods=["POST"])
@admin_required
def admin_update_role(user_id: int):
    role = request.form.get("role", "")
    if role not in VALID_ROLES:
        flash("无效角色", "error")
        return redirect(url_for("admin_users"))

    db = get_db()
    try:
        # Prevent removing the last admin
        target = get_user_by_id(db, user_id)
        if target and target["role"] == "admin" and role != "admin":
            if count_admins(db) <= 1:
                flash("无法降级：系统中至少需要保留一名管理员", "error")
                return redirect(url_for("admin_users"))
        update_user_role(db, user_id, role)
        flash("角色已更新", "success")
    finally:
        db.close()
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/password", methods=["POST"])
@admin_required
def admin_update_password(user_id: int):
    password = request.form.get("password", "")
    if len(password) < 6:
        flash("密码过短（至少 6 位）", "error")
        return redirect(url_for("admin_users"))
    db = get_db()
    try:
        update_user_password(db, user_id, generate_password_hash(password))
        flash("密码已更新", "success")
    finally:
        db.close()
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def admin_delete_user(user_id: int):
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
    _ensure_default_admin()
    print("=" * 52)
    print("  MC Log Viewer")
    print(f"  DB: {DB_PATH}")
    print("=" * 52)
    app.run(host="0.0.0.0", port=5000, debug=True)
