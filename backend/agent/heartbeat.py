"""
heartbeat.py — Polls the Redis-based Councilor mailbox and delivers responses.

Replaces the old filesystem-based mailbox polling with Redis LPOP from
the 'icarus:councilor:responses' list.
"""

import json
import asyncio
import logging

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL = 15  # seconds between mailbox polls
async def _poll_mailbox():
    """Pop undelivered Councilor responses from Redis and deliver them."""
    from backend.database.redis_connection import get_redis_client
    redis = get_redis_client()

    # Pop up to 10 responses per cycle
    for _ in range(10):
        raw = await redis.rpop("icarus:councilor:responses")
        if raw is None:
            break

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.warning(f"[heartbeat] Malformed response in Redis: {e}")
            continue

        message = data.get("message", "(no message)")
        resp_type = data.get("type", "unknown")
        platform = data.get("platform")
        chat_id = data.get("chat_id")

        # ── Email notification — run through Qwen for Icarus's voice ──────
        if resp_type == "email_notification":
            import os
            from backend.agent.discord_bot import relay_email_notification_to_operator
            user_id = data.get("user_id") or os.environ.get("DISCORD_OPERATOR_ID", "0")

            try:
                # Use the ADK-based relay logic for Icarus's voice
                await relay_email_notification_to_operator(message, str(user_id))
                logger.info(f"[heartbeat] Email notification delivered via ADK relay")
            except Exception as e:
                logger.error(f"[heartbeat] Email notification delivery failed: {e}")
            continue

        if resp_type == "escalation":
            prefix = "[MAILBOX] Councilor escalation complete."
        else:
            prefix = "[MAILBOX] Councilor consultation response."

        prompt = f"{prefix}\n\n{message}"
        logger.info(f"[heartbeat] Delivering {resp_type} response to {platform}:{chat_id}")

        # Lazy imports to avoid circular import at module load time
        from backend.routes.webhook import push_telegram_message
        from backend.agent.discord_bot import relay_councilor_mailbox_to_operator, push_discord_message

        if platform == "telegram" and chat_id:
            try:
                await push_telegram_message(int(chat_id), prompt)
            except Exception as e:
                logger.error(f"[heartbeat] Telegram delivery failed: {e}")
        elif platform == "discord":
            try:
                import os
                discord_operator_id = os.environ.get("DISCORD_OPERATOR_ID", "0")
                user_id = data.get("user_id") or discord_operator_id

                if resp_type == "consultation":
                    await relay_councilor_mailbox_to_operator(
                        prompt, str(user_id),
                        chat_id=str(chat_id) if chat_id else None,
                    )
                else:
                    await push_discord_message(None, prompt, user_id=str(user_id))
            except Exception as e:
                logger.error(f"[heartbeat] Discord delivery failed: {e}")
        else:
            # Fallback: try Telegram if configured
            import os
            telegram_chat_id = os.environ.get("ALLOWED_CHAT_ID", "0")
            try:
                tid = int(telegram_chat_id)
                if tid != 0:
                    await push_telegram_message(tid, prompt)
            except Exception as e:
                logger.error(f"[heartbeat] Fallback Telegram delivery failed: {e}")


async def run_heartbeat():
    """Background loop — polls the Redis mailbox every HEARTBEAT_INTERVAL seconds."""
    logger.info(f"[heartbeat] Started. Polling every {HEARTBEAT_INTERVAL}s.")
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL)
        try:
            await _poll_mailbox()
        except Exception as e:
            logger.error(f"[heartbeat] Unexpected poll error: {e}")
