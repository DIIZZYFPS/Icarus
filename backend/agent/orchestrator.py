import json
import logging
from backend.database.redis_connection import get_redis_client

logger = logging.getLogger(__name__)

async def dispatch_worker_task(stream: str, data: dict):
    """Push a task to a Redis stream."""
    redis = get_redis_client()
    try:
        # We need to serialize the data to JSON to ensure it's a flat dict of strings
        # because Redis XADD expects key-value pairs of strings.
        task_data = {k: (json.dumps(v) if not isinstance(v, (str, bytes, int, float)) else v) for k, v in data.items()}
        msg_id = await redis.xadd(stream, task_data)
        logger.info(f"Dispatched task {msg_id} to {stream}")
        return msg_id
    except Exception as e:
        logger.error(f"Failed to dispatch task to {stream}: {e}")
        return None

async def get_worker_result(message_id: str, timeout: int = 60):
    """Wait for a worker result in Redis."""
    redis = get_redis_client()
    key = f"icarus:email_score:{message_id}"
    import asyncio
    for _ in range(timeout):
        res = await redis.get(key)
        if res:
            return json.loads(res)
        await asyncio.sleep(1)
    return None
