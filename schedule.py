"""
Cron-inspired schedule — determines the poll interval based on current time.

Rules are evaluated top-to-bottom; the first match wins.
"""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Rule:
    hour: str       # "2-7", "*", etc.  (24-hour)
    dow: str        # "0-4", "5,6", "*"  (0=Mon … 6=Sun, Python weekday)
    interval: int   # seconds
    label: str      # human-readable

    def matches(self, dt: datetime) -> bool:
        return _match_field(self.hour, dt.hour) and _match_field(self.dow, dt.weekday())


# ── Schedule table ────────────────────────────────────────────
#  hour   dow     interval   label
#  ─────  ──────  ────────   ──────────────────
SCHEDULE = [
    Rule("2-7",  "*",    180, "夜间 02:00-07:59"),
    Rule("*",    "0-4",   60, "工作日"),
    Rule("*",    "5,6",   10, "周末"),
]


def _match_field(spec: str, value: int) -> bool:
    """Match a cron-style field (hour or dow) against an integer value."""
    if spec == "*":
        return True
    for part in spec.split(","):
        if "-" in part:
            lo, hi = part.split("-", 1)
            if int(lo) <= value <= int(hi):
                return True
        elif int(part) == value:
            return True
    return False


def get_interval(now: datetime | None = None) -> int:
    """Return the poll interval (seconds) for the current moment."""
    if now is None:
        now = datetime.now()
    for rule in SCHEDULE:
        if rule.matches(now):
            return rule.interval
    return 60  # fallback


def describe(now: datetime | None = None) -> str:
    """Return a human-readable description of the current schedule."""
    if now is None:
        now = datetime.now()
    for rule in SCHEDULE:
        if rule.matches(now):
            return f"{rule.label} → 每 {rule.interval}s"
    return "未知时段 → 每 60s"
