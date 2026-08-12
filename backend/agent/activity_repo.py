"""
activity_repo.py — Structured activity events for the dashboard's Activity page.

publish_activity() is the one call site instrumentation touches — it's
callable from icarus-api (in-container) *and* councilor.py (bare host,
its own venv) since both already talk to the same Redis instance for IPC.
It never touches SQLite directly: publishing and persisting are split so
there's exactly one writer to icarus.db (the activity_consumer task running
inside icarus-api), instead of two processes racing on the same SQLite file.

Flow: publish_activity() -> Redis pub/sub channel "icarus:activity" ->
  - activity_consumer.py (icarus-api) persists it via persist_activity()
  - any open dashboard WebSocket subscribed to the same channel gets it live

list_recent_activity() is the REST backfill path for a freshly loaded
dashboard page — the live WebSocket only carries what streams by after the
page connects.
"""

import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# sqlalchemy is intentionally NOT imported at module level: publish_activity()
# is called from councilor.py too, which runs in its own minimal host venv
# (redis + httpx only, no sqlalchemy/aiosqlite — see .venv at repo root).
# Only the DB-side functions below need it, so they import it lazily.

ACTIVITY_CHANNEL = "icarus:activity"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def publish_activity(
    actor: str,
    event_type: str,
    action: str,
    detail: str | None = None,
    thread_id: str | None = None,
    platform: str | None = None,
    user_id: str | None = None,
    severity: str = "info",
) -> None:
    """Publish one structured activity event. Best-effort: failures are
    logged and swallowed so a Redis hiccup never breaks the actual work
    being narrated (an escalation completing, an email being triaged)."""
    try:
        from backend.database.redis_connection import get_redis_client
        redis = get_redis_client()
        payload = json.dumps({
            "thread_id": thread_id,
            "actor": actor,
            "event_type": event_type,
            "action": action,
            "detail": detail,
            "severity": severity,
            "platform": platform,
            "user_id": user_id,
            "created_at": _now_iso(),
        })
        await redis.publish(ACTIVITY_CHANNEL, payload)
    except Exception as e:
        logger.warning(f"[activity] publish_activity failed (actor={actor}, event={event_type}): {e}")


async def persist_activity(event: dict) -> None:
    """Insert one already-published event into the durable activity_events
    table. Called only by activity_consumer.py — the single writer."""
    from backend.database.connection import AsyncSessionLocal
    from backend.database.models import ActivityEvent

    async with AsyncSessionLocal() as session:
        row = ActivityEvent(
            thread_id=event.get("thread_id"),
            actor=event.get("actor", "system"),
            event_type=event.get("event_type", "unknown"),
            action=event.get("action", ""),
            detail=event.get("detail"),
            severity=event.get("severity", "info"),
            platform=event.get("platform"),
            user_id=event.get("user_id"),
            created_at=event.get("created_at") or _now_iso(),
        )
        session.add(row)
        await session.commit()


async def list_recent_activity(limit: int = 150, actor: str | None = None) -> list[dict]:
    """Most recent activity events, oldest-first (natural reading/threading
    order for the dashboard timeline)."""
    from sqlalchemy import select
    from backend.database.connection import AsyncSessionLocal
    from backend.database.models import ActivityEvent

    async with AsyncSessionLocal() as session:
        query = select(ActivityEvent)
        if actor:
            query = query.where(ActivityEvent.actor == actor)
        query = query.order_by(ActivityEvent.id.desc()).limit(max(1, min(limit, 500)))
        result = await session.execute(query)
        rows = list(result.scalars().all())

    rows.reverse()
    return [_serialize(r) for r in rows]


async def latest_activity_for_actor(actor: str) -> dict | None:
    """Most recent event from one actor — used for dashboard ticker stats
    (e.g. "Councilor: idle, last consult 05:44")."""
    from sqlalchemy import select
    from backend.database.connection import AsyncSessionLocal
    from backend.database.models import ActivityEvent

    async with AsyncSessionLocal() as session:
        query = (
            select(ActivityEvent)
            .where(ActivityEvent.actor == actor)
            .order_by(ActivityEvent.id.desc())
            .limit(1)
        )
        result = await session.execute(query)
        row = result.scalar_one_or_none()

    return _serialize(row) if row else None


def _serialize(r) -> dict:
    return {
        "id": r.id,
        "thread_id": r.thread_id,
        "actor": r.actor,
        "event_type": r.event_type,
        "action": r.action,
        "detail": r.detail,
        "severity": r.severity,
        "platform": r.platform,
        "user_id": r.user_id,
        "created_at": r.created_at,
    }
