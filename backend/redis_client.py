"""
SmAttaker — Redis Connection
"""
import redis.asyncio as aioredis
from backend.config import settings

# ── Redis Client ────────────────────────────────────────
redis_client: aioredis.Redis | None = None


async def init_redis():
    """Initialize Redis connection pool."""
    global redis_client
    redis_client = aioredis.from_url(
        settings.REDIS_URL,
        encoding="utf-8",
        decode_responses=True,
        max_connections=20,
    )
    # Test connection
    await redis_client.ping()


async def close_redis():
    """Close Redis connection."""
    global redis_client
    if redis_client:
        await redis_client.close()
        redis_client = None


async def get_redis() -> aioredis.Redis:
    """Dependency: yields Redis client."""
    if redis_client is None:
        await init_redis()
    return redis_client
