"""Redis-backed context for automated notifications delivered to a conversation."""

from __future__ import annotations

import time
import uuid
from typing import Any


def get_redis_client():
    """Load the Redis client lazily so pure context helpers stay importable."""
    from backend.database.redis_connection import get_redis_client as _get_redis_client

    return _get_redis_client()

_PENDING_TTL_SECONDS = 7 * 24 * 60 * 60
_MAX_PENDING = 20


def _index_key(conversation_id: str) -> str:
    return f"icarus:notifications:index:{conversation_id}"


def _record_key(notification_id: str) -> str:
    return f"icarus:notifications:record:{notification_id}"


async def record_notification(
    *,
    conversation_id: str,
    notification_type: str,
    source_id: str | None,
    content: str,
) -> str:
    """Record a delivered-or-deliverable notification for later DM replies."""
    stable_source = str(source_id) if source_id else uuid.uuid4().hex
    notification_id = f"{conversation_id}:{notification_type}:{stable_source}"
    key = _record_key(notification_id)
    index_key = _index_key(conversation_id)
    redis = get_redis_client()

    exists = await redis.exists(key)
    fields: dict[str, Any] = {
        "notification_id": notification_id,
        "conversation_id": conversation_id,
        "notification_type": notification_type,
        "source_id": stable_source,
        "content": content,
        "created_at": str(int(time.time())),
        "consumed_at": "",
        "delivery_message_id": "",
    }
    await redis.hset(key, mapping=fields)
    await redis.expire(key, _PENDING_TTL_SECONDS)

    if not exists:
        await redis.lpush(index_key, notification_id)
        await redis.ltrim(index_key, 0, _MAX_PENDING - 1)
    await redis.expire(index_key, _PENDING_TTL_SECONDS)
    return notification_id


async def bind_delivery(notification_id: str, delivery_message_id: str) -> None:
    """Associate the Discord message ID so explicit replies can be resolved."""
    redis = get_redis_client()
    await redis.hset(
        _record_key(notification_id),
        mapping={"delivery_message_id": str(delivery_message_id)},
    )


async def get_pending_notification(
    *,
    conversation_id: str,
    reply_to_message_id: str | None = None,
) -> dict[str, str] | None:
    """Resolve one pending notification without crossing conversation scopes.

    An explicit Discord reply must match the notification's delivery message.
    For ordinary DM text, the newest pending notification is the intended
    context. Server callers should not call this function.
    """
    redis = get_redis_client()
    ids = await redis.lrange(_index_key(conversation_id), 0, _MAX_PENDING - 1)
    fallback: dict[str, str] | None = None

    for raw_id in ids:
        notification_id = raw_id.decode() if isinstance(raw_id, bytes) else str(raw_id)
        raw = await redis.hgetall(_record_key(notification_id))
        if not raw:
            continue
        record = {
            (key.decode() if isinstance(key, bytes) else str(key)): (
                value.decode() if isinstance(value, bytes) else str(value)
            )
            for key, value in raw.items()
        }
        if record.get("consumed_at"):
            continue
        if reply_to_message_id is not None:
            if record.get("delivery_message_id") == str(reply_to_message_id):
                return record
            continue
        if fallback is None:
            fallback = record

    return fallback


async def consume_notification(notification_id: str) -> None:
    """Mark a notification as consumed after a successful agent response."""
    redis = get_redis_client()
    await redis.hset(_record_key(notification_id), mapping={"consumed_at": str(int(time.time()))})
