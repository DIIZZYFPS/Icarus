"""
activity_consumer.py — persists the icarus:activity Redis stream to SQLite.

The single writer to the activity_events table. Every dashboard-relevant
event, wherever it was published from (in-container tool calls, the
bare-host Councilor, the email triage worker), lands on the same Redis
channel; this task is what turns that ephemeral pub/sub stream into
durable history a freshly loaded dashboard can back-fill from. Live
dashboard clients don't go through here — they subscribe to the same
channel directly (see main.py's /ws/activity) so persistence latency never
adds delay to what's shown on screen.
"""

import json
import logging

logger = logging.getLogger(__name__)


async def run_activity_consumer():
    from backend.database.redis_connection import get_redis_client
    from backend.agent.activity_repo import ACTIVITY_CHANNEL, persist_activity

    redis = get_redis_client()
    pubsub = redis.pubsub()
    await pubsub.subscribe(ACTIVITY_CHANNEL)
    logger.info(f"[activity_consumer] Subscribed to {ACTIVITY_CHANNEL}")

    async for msg in pubsub.listen():
        if msg["type"] != "message":
            continue
        try:
            event = json.loads(msg["data"])
        except json.JSONDecodeError as e:
            logger.warning(f"[activity_consumer] Malformed event: {e}")
            continue
        try:
            await persist_activity(event)
        except Exception as e:
            logger.error(f"[activity_consumer] Failed to persist event: {e}")
