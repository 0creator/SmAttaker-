"""
SmAttaker — Redis Response Cache
Generic get-or-compute caching for expensive read-heavy endpoints
(analytics aggregations, rankings). Redis has been a listed dependency
and initialized on startup (backend/redis_client.py) since the start of
this project but was never actually used anywhere — this is the first
real consumer of it.

Fails open by design: if Redis is unreachable for any reason, every
function here falls back to computing fresh data rather than raising —
caching must never be the reason a page breaks.
"""
import json
import logging
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("smattaker.cache")


async def cached_json(
    key: str,
    ttl_seconds: int,
    compute_fn: Callable[[], Awaitable[Any]],
) -> Any:
    """
    Returns cached JSON-serializable data for `key` if present and
    fresh; otherwise calls `compute_fn()`, caches the result for
    `ttl_seconds`, and returns it.

    `compute_fn` must return something JSON-serializable (plain dicts/
    lists/primitives — e.g. a Pydantic model's `.model_dump(mode="json")`,
    not the model instance itself).
    """
    try:
        from backend.redis_client import get_redis
        redis = await get_redis()
        if redis is not None:
            cached = await redis.get(key)
            if cached is not None:
                return json.loads(cached)
    except Exception as e:
        logger.warning(f"Cache read failed for '{key}' (falling back to live compute): {e}")
        redis = None

    result = await compute_fn()

    try:
        if redis is not None:
            await redis.setex(key, ttl_seconds, json.dumps(result))
    except Exception as e:
        logger.warning(f"Cache write failed for '{key}' (result still returned fresh): {e}")

    return result


async def invalidate(key_prefix: str) -> None:
    """
    Delete every cached key starting with `key_prefix`. Call this when
    underlying data changes in a way that should bust the cache
    immediately rather than waiting out the TTL (e.g. after a new trade
    completes, if instant freshness matters more than the TTL window).
    """
    try:
        from backend.redis_client import get_redis
        redis = await get_redis()
        if redis is None:
            return
        cursor = 0
        while True:
            cursor, keys = await redis.scan(cursor, match=f"{key_prefix}*", count=100)
            if keys:
                await redis.delete(*keys)
            if cursor == 0:
                break
    except Exception as e:
        logger.warning(f"Cache invalidation failed for prefix '{key_prefix}': {e}")
