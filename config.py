"""
MC Server Log Fetcher — Configuration

Sensitive values are read from environment variables.
Copy .env.example → .env and set your real credentials there.
"""

import os
from pathlib import Path


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
QUERY_TASK_STEP_INTERVAL = float(os.getenv("MC_QUERY_TASK_STEP_INTERVAL", "1"))

# JWT expiry buffer (seconds) — re-login this many seconds before token expires
TOKEN_EXPIRY_BUFFER = 60
