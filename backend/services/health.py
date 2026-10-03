"""
SmAttaker — Component Health Checks
=====================================
Answers "is the database actually reachable, right now, and how slow
is it?" the same way scheduler-status already answers "is the
scheduler alive?" — a direct HTTP call instead of guessing from a
moving log window (see main.py's own comments on why that guessing
game doesn't work on Render).

Each check is deliberately isolated with its own try/except: a Redis
outage must never make the health endpoint fail to report that the
database is fine, and vice versa. A health endpoint that itself throws
500s during an incident is worse than useless.
"""
import time
from typing import Optional


async def check_database() -> dict:
    """Round-trip a trivial query and time it. This is the same engine
    every request already depends on, so a failure here means the
    whole platform is down — not just this diagnostic."""
    from sqlalchemy import text
    from backend.database import async_session_factory

    start = time.monotonic()
    try:
        async with async_session_factory() as db:
            await db.execute(text("SELECT 1"))
        latency_ms = round((time.monotonic() - start) * 1000, 1)
        return {"status": "ok", "latency_ms": latency_ms}
    except Exception as e:
        return {"status": "down", "latency_ms": None, "error": str(e)[:200]}


async def check_redis() -> dict:
    """Ping the shared Redis connection (rate limiting, caching)."""
    from backend.redis_client import get_redis

    start = time.monotonic()
    try:
        client = await get_redis()
        await client.ping()
        latency_ms = round((time.monotonic() - start) * 1000, 1)
        return {"status": "ok", "latency_ms": latency_ms}
    except Exception as e:
        return {"status": "down", "latency_ms": None, "error": str(e)[:200]}


def overall_status(components: dict) -> str:
    """'ok' if everything is up, 'degraded' if a non-critical piece is
    down, 'down' if the database itself is unreachable — nothing else
    matters if the database is down."""
    if components.get("database", {}).get("status") != "ok":
        return "down"
    if any(c.get("status") != "ok" for k, c in components.items() if k != "database"):
        return "degraded"
    return "ok"
