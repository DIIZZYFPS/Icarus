"""
gmail_watcher.py — Gmail push notification listener.

Background task that:
1. Registers a Gmail watch (push notifications via GCP Pub/Sub) on startup
2. Pulls messages from the Pub/Sub subscription
3. Fetches new message IDs via the Gmail History API
4. Dispatches each new message to the `tasks:email_triage` Redis stream
5. Re-registers the watch every 6 days (Google requires renewal every 7)
"""

import os
import json
import base64
import asyncio
import logging
import time

logger = logging.getLogger(__name__)

# Watch renewal interval: 6 days (Google expires after 7)
WATCH_RENEWAL_SECONDS = 6 * 24 * 3600

# Pub/Sub pull interval when no messages
PULL_INTERVAL_SECONDS = 5

# Track the last known historyId to avoid reprocessing
_last_history_id: str | None = None


async def _register_watch() -> str | None:
    """Register Gmail push notifications. Returns the initial historyId."""
    from backend.agent.gmail_tools import gmail_setup_watch

    topic = os.getenv("GMAIL_PUBSUB_TOPIC", "").strip()
    if not topic:
        logger.error("[gmail_watcher] GMAIL_PUBSUB_TOPIC not set — cannot register watch")
        return None

    result = await gmail_setup_watch(topic)
    if result:
        history_id = result.get("historyId")
        logger.info(f"[gmail_watcher] Watch registered, historyId={history_id}")
        return str(history_id)
    return None


async def _dispatch_to_triage(message_ids: list[str]):
    """Push message IDs to the email triage Redis stream."""
    if not message_ids:
        return

    from backend.database.redis_connection import get_redis_client
    redis = get_redis_client()

    for msg_id in message_ids:
        await redis.xadd(
            "tasks:email_triage",
            {"message_id": msg_id},
        )
    logger.info(f"[gmail_watcher] Dispatched {len(message_ids)} message(s) to triage stream")


async def _pull_pubsub():
    """Pull messages from the GCP Pub/Sub subscription (async).

    Uses the REST API directly to avoid the heavy google-cloud-pubsub
    dependency. Falls back gracefully if credentials aren't set up.
    """
    import httpx
    from google.oauth2.credentials import Credentials as UserCredentials

    project = os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
    subscription = os.getenv("GMAIL_PUBSUB_SUBSCRIPTION", "").strip()

    if not project or not subscription:
        return []

    # Build OAuth credentials for Pub/Sub access
    # Re-use the Gmail OAuth credentials (they need pubsub scope too)
    client_id = (os.getenv("GMAIL_OAUTH_CLIENT_ID") or "").strip()
    client_secret = (os.getenv("GMAIL_OAUTH_CLIENT_SECRET") or "").strip()
    refresh_token = (os.getenv("GMAIL_OAUTH_REFRESH_TOKEN") or "").strip()

    if not (client_id and client_secret and refresh_token):
        return []

    creds = UserCredentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=[
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/pubsub",
        ],
    )
    # Force refresh to get an access token
    import google.auth.transport.requests
    creds.refresh(google.auth.transport.requests.Request())

    url = f"https://pubsub.googleapis.com/v1/{subscription}:pull"
    headers = {
        "Authorization": f"Bearer {creds.token}",
        "Content-Type": "application/json",
    }
    body = {"maxMessages": 20}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        messages = data.get("receivedMessages", [])
        if not messages:
            return []

        # Acknowledge all received messages
        ack_ids = [m["ackId"] for m in messages]
        ack_url = f"https://pubsub.googleapis.com/v1/{subscription}:acknowledge"
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(ack_url, json={"ackIds": ack_ids}, headers=headers)

        # Parse notification payloads
        notifications = []
        for msg in messages:
            pubsub_data = msg.get("message", {}).get("data", "")
            if pubsub_data:
                try:
                    decoded = json.loads(base64.b64decode(pubsub_data))
                    notifications.append(decoded)
                except (json.JSONDecodeError, Exception) as e:
                    logger.warning(f"[gmail_watcher] Failed to decode pubsub message: {e}")

        return notifications
    except Exception as e:
        logger.warning(f"[gmail_watcher] Pub/Sub pull failed: {e}")
        return []


async def run_gmail_watcher():
    """Main loop: register watch, pull Pub/Sub, dispatch to triage."""
    global _last_history_id

    logger.info("[gmail_watcher] Starting Gmail watcher...")

    # Verify required env vars
    topic = os.getenv("GMAIL_PUBSUB_TOPIC", "").strip()
    if not topic:
        logger.warning("[gmail_watcher] GMAIL_PUBSUB_TOPIC not set — watcher disabled")
        return

    # Register initial watch
    _last_history_id = await _register_watch()
    if not _last_history_id:
        logger.error("[gmail_watcher] Failed to register watch — will retry in 60s")
        await asyncio.sleep(60)
        _last_history_id = await _register_watch()
        if not _last_history_id:
            logger.error("[gmail_watcher] Watch registration failed — watcher disabled")
            return

    last_watch_time = time.monotonic()
    logger.info(f"[gmail_watcher] Watcher active, polling Pub/Sub (historyId={_last_history_id})")

    while True:
        try:
            # Re-register watch every 6 days
            if time.monotonic() - last_watch_time > WATCH_RENEWAL_SECONDS:
                new_history_id = await _register_watch()
                if new_history_id:
                    _last_history_id = new_history_id
                    last_watch_time = time.monotonic()
                    logger.info("[gmail_watcher] Watch renewed")

            # Pull from Pub/Sub
            notifications = await _pull_pubsub()

            if notifications and _last_history_id:
                from backend.agent.gmail_tools import gmail_get_history

                # Each notification tells us new mail arrived since some historyId
                # Use our tracked historyId to fetch all new messages
                new_ids = await gmail_get_history(_last_history_id)

                if new_ids:
                    # Deduplicate
                    unique_ids = list(set(new_ids))
                    await _dispatch_to_triage(unique_ids)
                    logger.info(
                        f"[gmail_watcher] {len(unique_ids)} new message(s) dispatched"
                    )

                # Update historyId to the latest from any notification
                for notif in notifications:
                    hid = notif.get("historyId")
                    if hid and (not _last_history_id or int(hid) > int(_last_history_id)):
                        _last_history_id = str(hid)

        except Exception as e:
            logger.error(f"[gmail_watcher] Error in main loop: {e}")

        await asyncio.sleep(PULL_INTERVAL_SECONDS)
