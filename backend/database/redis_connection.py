import os
import logging
import redis.asyncio as redis

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

_redis_client: redis.Redis | None = None

def get_redis_client() -> redis.Redis:
    global _redis_client
    if _redis_client is None:
        # socket_timeout=None: redis-py >=8 otherwise defaults this to 5s,
        # which races blocking calls like BLPOP(..., timeout=5) in
        # metrics_consumer.py — the client's own read deadline fires right
        # as the server would return nil, logging a spurious TimeoutError
        # every idle cycle. Let the server-side blocking timeout govern
        # instead. socket_connect_timeout stays bounded so a dead Redis is
        # still detected quickly.
        _redis_client = redis.from_url(
            REDIS_URL,
            decode_responses=True,
            socket_timeout=None,
            socket_connect_timeout=5,
        )
    return _redis_client

async def close_redis():
    global _redis_client
    if _redis_client:
        await _redis_client.close()
        _redis_client = None
