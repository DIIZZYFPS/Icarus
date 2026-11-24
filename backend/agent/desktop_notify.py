"""
desktop_notify.py — Icarus speaking first on the desktop (ADR icarus_desktop_avatar.md §6.7).

Notifications keep going to Discord as the written record. On top of that, the
important ones also become a spoken pop-up on the desktop avatar while its daemon
is connected:

  pop up   paused task asking a question · escalation results · delegated task that
           failed, or finished within the last hour, or was started from the desktop ·
           email with critical urgency, or high urgency that needs action
  quiet    everything else (routine subagent reports, lifecycle housekeeping,
           consultations, deploy checks, jobs): the orb's unread dot only

Pop-ups that arrive close together are bundled into one spoken update. Each pop-up
is recorded as a notification on the desktop conversation, with a delivery id the
daemon sends back with your spoken reply, so "read it" or an answer to a paused
task resolves exactly like replying to the Discord message does.

A pop-up is kept in Redis from the moment it is pushed until the daemon reports it spoken
({"type": "spoken", "id"}). If the daemon restarts or reconnects first, the unspoken ones are
sent again, so a daemon that was down for a moment doesn't swallow them. The daemon ignores
ids it already holds.
"""

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

ANNOUNCE_LIST = "icarus:desktop:announcements"     # Councilor (host) -> icarus-api
BUNDLE_SECONDS = float(os.getenv("DESKTOP_ANNOUNCE_BUNDLE_SECONDS", "4"))
UNSPOKEN_KEY = "icarus:desktop:unspoken"           # pop-ups pushed but not yet spoken: id -> payload
UNSPOKEN_MAX_AGE = float(os.getenv("DESKTOP_ANNOUNCE_MAX_AGE_HOURS", "3")) * 3600
RECENT_TASK_SECONDS = 60 * 60
MESSAGE_CHARS = 1500

STAMPS = {
    "pause_question": {"word": "INPUT NEEDED", "color": "amber", "wings": 1.2},
    "failed": {"word": "FAILED", "color": "magenta"},
    "complete": {"word": "COMPLETE", "color": "cyan", "wings": 1.8},
    "email": {"word": "INCOMING", "color": "cyan"},
    "calendar": {"word": "UP NEXT", "color": "cyan"},
    "alert": {"word": "ATTENTION", "color": "amber"},
}
PRIORITY = ["pause_question", "failed", "alert", "calendar", "complete", "email"]   # which stamp wins in a bundle


def _parse_iso(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


async def _task_row(task_id: str | None) -> dict | None:
    if not task_id:
        return None
    try:
        from backend.agent.subagent_registry import get_registry
        registry = get_registry()
        return await registry.get(task_id) if registry.exists() else None
    except Exception as e:
        logger.debug(f"[desktop-notify] registry lookup failed: {e}")
        return None


async def classify(data: dict) -> str | None:
    """The stamp kind for a pop-up, or None for quiet (orb dot only)."""
    kind = data.get("type")
    if kind == "pause_question":
        return "pause_question"
    if kind == "calendar_soon":
        return "calendar"
    if kind == "system_alert":
        return "alert"
    if kind == "email_notification":
        urgency = str(data.get("urgency") or "").lower()
        if urgency == "critical" or (urgency == "high" and data.get("action_needed")):
            return "email"
        return None
    if kind == "escalation":
        failed = str(data.get("status") or "").lower() == "failed" or "fail" in str(data.get("message", ""))[:200].lower()
        return "failed" if failed else "complete"
    if kind == "delegation":
        row = await _task_row(data.get("task_id"))
        status = str(data.get("status") or (row or {}).get("status") or "").lower()
        if status == "failed":
            return "failed"
        if data.get("platform") == "desktop":
            return "complete"
        created = _parse_iso((row or {}).get("created_at"))
        if created and time.time() - created < RECENT_TASK_SECONDS:
            return "complete"
        return None
    return None


def _describe(data: dict) -> str:
    kind = data.get("type")
    label = {
        "pause_question": f"A delegated task ({data.get('task_id')}) is paused and needs DIIZZY to decide something",
        "email_notification": "Important email",
        "escalation": "Councilor escalation result",
        "delegation": f"Delegated task {data.get('task_id') or ''} result".strip(),
        "calendar_soon": "Calendar reminder",
        "system_alert": "System problem DIIZZY needs to fix",
    }.get(kind, str(kind))
    body = data.get("question") if kind == "pause_question" else data.get("message")
    return f"{label}:\n{str(body or '')[:MESSAGE_CHARS]}"


async def remember_unspoken(payload: dict) -> None:
    from backend.database.redis_connection import get_redis_client
    try:
        await get_redis_client().hset(UNSPOKEN_KEY, payload["id"], json.dumps({**payload, "pushed": time.time()}))
    except Exception as e:
        logger.debug(f"[desktop-notify] couldn't keep {payload['id']} for resending: {e}")


async def mark_spoken(delivery_id: str) -> None:
    from backend.database.redis_connection import get_redis_client
    try:
        await get_redis_client().hdel(UNSPOKEN_KEY, delivery_id)
    except Exception as e:
        logger.debug(f"[desktop-notify] couldn't mark {delivery_id} spoken: {e}")


async def unspoken() -> list[dict]:
    """Pop-ups the daemon hasn't reported spoken yet, oldest first. Ones past the daemon's own
    age limit are dropped (it would discard them anyway; Discord has the record)."""
    from backend.database.redis_connection import get_redis_client
    try:
        redis = get_redis_client()
        raw = await redis.hgetall(UNSPOKEN_KEY)
        now, items, stale = time.time(), [], []
        for key, value in (raw or {}).items():
            key = key.decode() if isinstance(key, bytes) else key
            try:
                entry = json.loads(value)
                fresh = now - float(entry.get("pushed") or 0) < UNSPOKEN_MAX_AGE
            except (TypeError, ValueError):
                entry, fresh = None, False
            if fresh:
                items.append(entry)
            else:
                stale.append(key)
        if stale:
            await redis.hdel(UNSPOKEN_KEY, *stale)
        items.sort(key=lambda e: e.get("pushed") or 0)
        return [{k: v for k, v in e.items() if k != "pushed"} for e in items]
    except Exception as e:
        logger.debug(f"[desktop-notify] unspoken list unavailable: {e}")
        return []


class Announcer:
    def __init__(self):
        self.pending: list[tuple[str, dict]] = []
        self._flush_task: asyncio.Task | None = None

    async def consider(self, data: dict) -> None:
        """Called for every notification; pops up the important ones if the daemon is connected."""
        from backend.agent.desktop_session import get_active
        if get_active() is None:
            return
        try:
            stamp = await classify(data)
        except Exception:
            logger.exception("[desktop-notify] classify failed")
            return
        if stamp is None:
            return
        self.pending.append((stamp, data))
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_later())

    async def _flush_later(self) -> None:
        await asyncio.sleep(BUNDLE_SECONDS)
        items, self.pending = self.pending, []
        if items:
            try:
                await self.announce(items)
            except Exception:
                logger.exception("[desktop-notify] announcement failed")

    async def announce(self, items: list[tuple[str, dict]]) -> None:
        from backend.agent.conversation_context import build_conversation_id
        from backend.agent.desktop_session import get_active
        from backend.agent.notification_repo import bind_delivery, record_notification
        from backend.agent.processor import process_message
        from backend.routes.desktop import CHAT_ID, PLATFORM, _user_id

        user = _user_id()
        count = len(items)
        prompt = (
            f"[System: {count} update{'s' if count > 1 else ''} just came in while DIIZZY is at his desktop. "
            "Tell him out loud in one to three short sentences, most important first"
            + (f", starting with something like \"{count} things.\"" if count > 1 else "")
            + ". If one needs his decision, end with a short question. Don't read ids, links or paths aloud.]\n\n"
            + "\n\n".join(_describe(data) for _, data in items)
        )
        text = await process_message(PLATFORM, user, prompt, chat_id=CHAT_ID, skip_history=True)
        if not text or text == "[No response]":
            text = "Something came in from the Councilor. It's in your Discord DMs."

        delivery_id = f"desktop-{uuid.uuid4().hex[:10]}"
        conversation_id = build_conversation_id(platform=PLATFORM, user_id=user, chat_id=CHAT_ID)
        first = items[0][1]
        notification_id = await record_notification(
            conversation_id=conversation_id,
            notification_type="desktop_" + str(first.get("type")),
            source_id=delivery_id,
            content=text,
        )
        await bind_delivery(notification_id, [delivery_id])
        for stamp, data in items:
            if stamp == "pause_question" and data.get("task_id"):
                await link_pause(data["task_id"], delivery_id)

        best = min((stamp for stamp, _ in items), key=PRIORITY.index)
        session = get_active()
        if session is None:
            return
        payload = {
            "type": "notify", "id": delivery_id, "text": text,
            "stamp": {**STAMPS[best], "sub": f"ICARUS // {count} UPDATE{'S' if count > 1 else ''}"},
            "kinds": [data.get("type") for _, data in items],
        }
        await remember_unspoken(payload)
        await session.push(payload)
        logger.info(f"[desktop-notify] announced {count} item(s) as {delivery_id}")


async def link_pause(task_id: str, delivery_id: str) -> None:
    """Let a spoken reply to this pop-up resume the paused task, like a Discord reply would."""
    from backend.agent.subagent_resume import index_key, pause_key
    from backend.database.redis_connection import get_redis_client
    from backend.routes.desktop import CHAT_ID, PLATFORM
    redis = get_redis_client()
    raw = await redis.hget(pause_key(task_id), "delivery_message_ids")
    if raw is None:
        return
    try:
        ids = json.loads(raw.decode() if isinstance(raw, bytes) else raw or "[]")
    except (json.JSONDecodeError, AttributeError):
        ids = []
    ids.append(delivery_id)
    await redis.hset(pause_key(task_id), mapping={"delivery_message_ids": json.dumps(ids)})
    await redis.lpush(index_key(PLATFORM, CHAT_ID), task_id)
    await redis.ltrim(index_key(PLATFORM, CHAT_ID), 0, 49)


announcer = Announcer()


async def run_announcement_consumer() -> None:
    """Councilor-side events (paused-task questions) arrive through a Redis list."""
    from backend.database.redis_connection import get_redis_client
    redis = get_redis_client()
    while True:
        try:
            item = await redis.blpop(ANNOUNCE_LIST, timeout=5)
            if not item:
                continue
            data = json.loads(item[1])
            if time.time() - float(data.get("created", time.time())) > 30 * 60:
                continue
            await announcer.consider(data)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[desktop-notify] announcement consumer error: {e}")
            await asyncio.sleep(2)
