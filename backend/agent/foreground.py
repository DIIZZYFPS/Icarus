"""
foreground.py — keep background model calls out of the way of a live conversation.

The local model server has one request slot (--parallel 1), so a background
call that starts while DIIZZY is talking to the desktop avatar makes every
step of that conversation wait behind it. The desktop route marks the
conversation as live with hold(); background workers (email triage) call
wait_until_idle() before starting a model call.

The mark is a Redis key with a TTL, so a crashed holder can't block
background work for longer than one TTL, and wait_until_idle() gives up
after max_wait seconds so background work is delayed, never starved.
"""

import asyncio
import logging
import os
import time

from backend.database.redis_connection import get_redis_client

logger = logging.getLogger(__name__)

KEY = "icarus:llm:foreground"
MAX_DEFER_SECONDS = float(os.getenv("BACKGROUND_LLM_MAX_DEFER_SECONDS", "300"))


async def hold(seconds: float, reason: str = "desktop") -> None:
    """Mark a live conversation for the next `seconds` (extends, never shortens, a later expiry)."""
    try:
        redis = get_redis_client()
        ttl_ms = int(seconds * 1000)
        current = await redis.pttl(KEY)
        if current is None or current < ttl_ms:
            await redis.set(KEY, reason, px=ttl_ms)
    except Exception as e:
        logger.debug(f"[foreground] hold failed: {e}")


async def is_busy() -> bool:
    try:
        return bool(await get_redis_client().exists(KEY))
    except Exception:
        return False


async def wait_until_idle(max_wait: float | None = None, poll: float = 0.5, label: str = "background") -> float:
    """Wait while a conversation is live. Returns the seconds waited."""
    limit = MAX_DEFER_SECONDS if max_wait is None else max_wait
    start = time.monotonic()
    logged = False
    while await is_busy():
        waited = time.monotonic() - start
        if waited >= limit:
            logger.info(f"[foreground] {label} waited {waited:.0f}s; going ahead anyway")
            break
        if not logged:
            logger.info(f"[foreground] {label} deferring: a desktop conversation is live")
            logged = True
        await asyncio.sleep(poll)
    return time.monotonic() - start
