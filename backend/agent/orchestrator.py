import json
import logging
from backend.database.redis_connection import get_redis_client

logger = logging.getLogger(__name__)

EMAIL_TRIAGE_STREAM = "tasks:email_triage"
JOB_SCOUT_STREAM = "tasks:job_scout"
EMAIL_PRIORITY_STREAM = "tasks:email_priority"
_STREAM_ALIASES = {
    "tasks:email_triage":   EMAIL_TRIAGE_STREAM,
    "email_triage":         EMAIL_TRIAGE_STREAM,
    "emails":               EMAIL_TRIAGE_STREAM,
    # email_priority is NOT an alias for email_triage — they're two distinct,
    # independently-running workers (worker_email_priority.py vs.
    # worker_email_triage.py), with different consumer groups and different
    # result-storage conventions (icarus:email_score:* vs.
    # icarus:email_triage:*). Rewriting one onto the other silently orphans
    # whichever worker doesn't get the traffic — see the incident this
    # comment replaced. Normalize spelling only; never merge the two.
    "tasks:email_priority": EMAIL_PRIORITY_STREAM,
    "email_priority":       EMAIL_PRIORITY_STREAM,
    "email priority":       EMAIL_PRIORITY_STREAM,
    "tasks:job_scout":      JOB_SCOUT_STREAM,
    "job_scout":            JOB_SCOUT_STREAM,
    "job scout":            JOB_SCOUT_STREAM,
}

# Every stream an actual worker consumes from — the only "not non-standard"
# targets. Add a new entry here alongside a new worker's own stream_name.
_KNOWN_STREAMS = {EMAIL_TRIAGE_STREAM, EMAIL_PRIORITY_STREAM, JOB_SCOUT_STREAM}


def _normalize_stream(stream: str) -> str:
    raw = (stream or "").strip()
    if not raw:
        logger.warning(
            "dispatch_worker_task received an empty stream name; defaulting to %s",
            EMAIL_TRIAGE_STREAM,
        )
        return EMAIL_TRIAGE_STREAM

    normalized = _STREAM_ALIASES.get(raw.lower(), raw)
    if normalized != raw:
        logger.info("Normalizing worker stream '%s' -> '%s'", raw, normalized)
    elif normalized not in _KNOWN_STREAMS:
        logger.warning(
            "Dispatching to non-standard stream '%s'. Known streams: %s.",
            normalized,
            ", ".join(sorted(_KNOWN_STREAMS)),
        )
    return normalized

async def dispatch_worker_task(stream: str, data: dict):
    """Push a task to a Redis stream.

    Returns the stream entry id (task_id) which is the stable correlation key
    for retrieving the result via get_worker_result().
    """
    redis = get_redis_client()
    stream = _normalize_stream(stream)
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

    Uses Redis BLPOP for efficient blocking instead of polling.
    Falls back to GET on the result key if BLPOP times out (result may
    have been stored before we started listening).
    """
    redis = get_redis_client()
    key = f"icarus:email_score:{task_id}"

    # First check if result already exists
    res = await redis.get(key)
    if res:
        return json.loads(res)

    # Wait using a notification channel
    notify_key = f"icarus:worker:notify:{task_id}"
    result = await redis.blpop(notify_key, timeout=timeout)
    if result:
        # BLPOP returns (key, value) tuple
        return json.loads(result[1])

    # Final fallback — check the result key one more time
    res = await redis.get(key)
    if res:
        return json.loads(res)
    return None

