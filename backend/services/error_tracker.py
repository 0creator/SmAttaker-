"""
SmAttaker — Recent Errors Tracker
====================================
A tiny logging.Handler that keeps the last N ERROR+ log records in
memory, so the admin panel can show "what broke recently" without
needing a third-party log aggregator (Sentry, Datadog, etc.) wired up.
This is deliberately NOT a replacement for one — it has no persistence
(a restart clears it) and no cross-instance aggregation — but it's a
real, free improvement over "someone has to remember to go read Render
logs" for a solo/small-team operator, which is exactly the gap RUNBOOK.md
describes every past incident falling into.

Thread/async-safety: a plain list with maxlen-style trimming is safe
here because CPython's GIL makes `list.append` + trim atomic enough
for a best-effort diagnostic feed — this is not a source of truth,
so we don't take a lock for it.
"""
import logging
import time
from collections import deque
from typing import Optional

_MAX_ERRORS = 200
_recent_errors: deque = deque(maxlen=_MAX_ERRORS)


class RecentErrorsHandler(logging.Handler):
    """Attach to the root logger to capture ERROR+ records app-wide —
    including ones raised deep inside the strategy runner, the bot, or
    exchange connectors, without those modules needing to know this
    tracker exists."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            _recent_errors.append({
                "timestamp": time.time(),
                "logger": record.name,
                "level": record.levelname,
                "message": self.format(record) if self.formatter else record.getMessage(),
            })
        except Exception:
            # A broken formatter must never break logging itself.
            pass


def install_error_tracker(min_level: int = logging.ERROR) -> None:
    """Call once at startup (main.py, right after logging.basicConfig)."""
    handler = RecentErrorsHandler(level=min_level)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger().addHandler(handler)


def get_recent_errors(limit: int = 50, since_seconds: Optional[int] = None) -> list[dict]:
    """Most recent errors first. `since_seconds` filters to a rolling
    window (e.g. last hour) instead of always returning the full buffer."""
    items = list(_recent_errors)[::-1]
    if since_seconds is not None:
        cutoff = time.time() - since_seconds
        items = [e for e in items if e["timestamp"] >= cutoff]
    return items[:limit]


def error_rate_last_hour() -> int:
    """Count of ERROR+ records in the last 60 minutes — the single
    number the admin overview card shows."""
    return len(get_recent_errors(limit=_MAX_ERRORS, since_seconds=3600))
