import json
import logging
from backend.database.redis_connection import get_redis_client

logger = logging.getLogger(__name__)

async def dispatch_worker_task(stream: str, data: dict):
    """Push a task to a Redis stream.

    Returns the stream entry id (task_id) which is the stable correlation key
    for retrieving the result via get_worker_result().
    """
    redis = get_redis_client()
    try:
        # Serialize non-primitive values to JSON so Redis XADD receives a flat
        # dict of strings (Redis XADD requires string field/values).
        task_data = {k: (json.dumps(v) if not isinstance(v, (str, bytes, int, float)) else v) for k, v in data.items()}
        task_id = await redis.xadd(stream, task_data)
        # Write the task_id back into the stream entry so the worker can read it.
        # Workers receive task_id as the stream entry id and store results under
        # icarus:email_score:{task_id} — always use the returned task_id for lookup.
        logger.info(f"Dispatched task {task_id} to {stream}")
        return task_id
    except Exception as e:
        logger.error(f"Failed to dispatch task to {stream}: {e}")
        return None

async def get_worker_result(task_id: str, timeout: int = 60):
    """Wait for a worker result in Redis, keyed by the stream entry id (task_id)
    returned by dispatch_worker_task().
    """
    redis = get_redis_client()
    import asyncio
    key = f"icarus:email_score:{task_id}"
    for _ in range(timeout):
        res = await redis.get(key)
        if res:
            return json.loads(res)
        await asyncio.sleep(1)
    return None
