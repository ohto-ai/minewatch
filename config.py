"""
MC Server Log Fetcher — Configuration

Sensitive values are read from environment variables.
Copy .env.example → .env and set your real credentials there.
"""

import os
import secrets as _secrets
from pathlib import Path


def _get_env_float(name: str, default: float) -> float:
    """Read a float environment variable with a safe fallback."""
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _load_dotenv() -> None:
    """Load .env file from project root (no external dependency needed)."""
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key and key not in os.environ:  # don't override existing env vars
            os.environ[key] = value


_load_dotenv()

BASE_URL = os.getenv("MC_BASE_URL", "http://xcon.top:8585")
LOGIN_URL = f"{BASE_URL}/api/login"
LOG_URL = f"{BASE_URL}/api/gpm/process/log"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/148.0.0.0 Safari/537.36"
)

BASE_HEADERS = {
    "Referer": f"{BASE_URL}/login",
    "User-Agent": USER_AGENT,
}

LOG_REFERER = f"{BASE_URL}/manager/gpm"

CREDENTIALS = {
    "username": os.getenv("MC_USERNAME", ""),
    "password": os.getenv("MC_PASSWORD", ""),
}

DB_PATH = "logs.db"
POLL_INTERVAL = 3.0
QUERY_TASK_STEP_INTERVAL = _get_env_float("MC_QUERY_TASK_STEP_INTERVAL", 1.0)

# JWT expiry buffer (seconds) — re-login this many seconds before token expires
TOKEN_EXPIRY_BUFFER = 60

# Flask secret key for session signing — set via FLASK_SECRET_KEY env var.
# A random key is generated at startup when the env var is absent, which means
# sessions are invalidated on every server restart in that case.
FLASK_SECRET_KEY: str = os.getenv("FLASK_SECRET_KEY") or _secrets.token_hex(32)

# Default admin credentials (used only when no users exist in the database).
ADMIN_USERNAME: str = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD: str = os.getenv("ADMIN_PASSWORD", "admin")

# Shared token used by inter-server sync requests to /api/logs/export.
SYNC_SHARED_TOKEN: str = os.getenv("SYNC_SHARED_TOKEN", "")
