"""
SmAttaker — Lightweight Rate Limiter
In-memory sliding-window rate limiter for sensitive endpoints (login,
etc). Deliberately dependency-free (no slowapi/redis requirement) —
given this project's history of dependency-resolution conflicts
breaking Render builds (aiohttp/ccxt, yfinance/curl_cffi), adding a new
pinned dependency for something this simple isn't worth the risk.

⚠️ Scope: this is per-process, in-memory state. Fine for a single
Render instance (which is what this project runs). If the app ever
scales to multiple instances behind a load balancer, this needs to
move to Redis (already a dependency, just not wired up for this yet —
see the "next steps" notes elsewhere) so limits are shared across
instances instead of each instance allowing its own separate quota.
"""
import time
import logging
from collections import defaultdict, deque
from fastapi import HTTPException, Request

logger = logging.getLogger("smattaker.rate_limit")

# key -> deque of request timestamps within the current window
_request_log: dict[str, deque] = defaultdict(deque)


def _client_key(request: Request, prefix: str) -> str:
    # X-Forwarded-For is set by Render's proxy; fall back to the direct
    # connection if it's ever missing (e.g. local testing).
    forwarded = request.headers.get("x-forwarded-for")
    client_ip = forwarded.split(",")[0].strip() if forwarded else (request.client.host if request.client else "unknown")
    return f"{prefix}:{client_ip}"


def rate_limiter(max_requests: int, window_seconds: int, prefix: str):
    """
    Returns a FastAPI dependency enforcing `max_requests` per
    `window_seconds` per client IP for the given `prefix` (so different
    endpoints can share this limiter with independent quotas).

    Usage:
        @router.post("/login", dependencies=[Depends(rate_limiter(10, 60, "login"))])
    """
    async def _dependency(request: Request):
        key = _client_key(request, prefix)
        now = time.time()
        log = _request_log[key]

        while log and now - log[0] > window_seconds:
            log.popleft()

        if len(log) >= max_requests:
            retry_after = int(window_seconds - (now - log[0])) + 1
            logger.warning(f"Rate limit exceeded for {key} ({len(log)}/{max_requests} in {window_seconds}s)")
            raise HTTPException(
                status_code=429,
                detail=f"Too many requests. Try again in {retry_after} seconds.",
                headers={"Retry-After": str(retry_after)},
            )

        log.append(now)

    return _dependency
