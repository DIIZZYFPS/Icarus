"""
tracked_items_repo.py — Structured, stateful entity tracking.

Sibling to memory_repo.py, not a replacement: MemoryEntry holds narrative
facts ("what did I learn/decide"); TrackedItem holds things with an actual
lifecycle ("what state is this application/bill/event in right now"). This
is what a future "what am I missing" digest queries directly, instead of
trying to re-derive current state from a pile of free-text log lines — LLMs
are bad at reconstructing mutable state from scattered mentions over any real
time horizon, and good at narrating clean structured input.

Every write is an upsert keyed on (platform, user_id, item_type, entity_key)
— three emails about the same job application update one row, they don't
create three disconnected mentions.
"""

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from backend.database.connection import AsyncSessionLocal
from backend.database.models import TrackedItem

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def upsert_item(
    platform: str,
    user_id: str,
    item_type: str,
    entity_key: str,
    state: str,
    summary: str | None = None,
    next_action: str | None = None,
    due_at: str | None = None,
    urgency: str | None = None,
    payload: dict | None = None,
    source: str = "agent",
    message_id: str | None = None,
) -> int:
    """Create or update a tracked item. A state change resets `notified` to 0
    — the operator was told about the *previous* state, not this one, so a
    fresh state change should be eligible to notify again.

    message_id links this item back to the TriageClassification row that
    created it (see triage_repo.py) — same "latest wins" treatment as
    summary/urgency/due_at below, so a later email about the same item
    re-points the link at whichever message most recently touched it."""
    now = _now_iso()
    payload_json = json.dumps(payload) if payload is not None else None

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(TrackedItem).where(
                TrackedItem.platform == platform,
                TrackedItem.user_id == user_id,
                TrackedItem.item_type == item_type,
                TrackedItem.entity_key == entity_key,
            )
        )
        row = result.scalar_one_or_none()

        if row is None:
            row = TrackedItem(
                platform=platform, user_id=user_id, item_type=item_type, entity_key=entity_key,
                state=state, summary=summary, next_action=next_action, due_at=due_at,
                urgency=urgency, payload=payload_json, source=source, message_id=message_id,
                notified=0, notified_at=None, created_at=now, updated_at=now,
            )
            session.add(row)
        else:
            state_changed = row.state != state
            row.state = state
            if summary is not None:
                row.summary = summary
            if next_action is not None:
                row.next_action = next_action
            if due_at is not None:
                row.due_at = due_at
            if urgency is not None:
                row.urgency = urgency
            if payload_json is not None:
                row.payload = payload_json
            if message_id is not None:
                row.message_id = message_id
            row.updated_at = now
            if state_changed:
                row.notified = 0
                row.notified_at = None
                # A state change is a new development on this item — an
                # earlier dismissal was about the state as it stood then,
                # not this one, so it shouldn't keep hiding it from the
                # dashboard's attention list.
                row.dismissed = 0
                row.dismissed_at = None

        await session.commit()
        await session.refresh(row)
        row_id = row.id

    logger.info(f"[tracked_items] upserted {item_type}:{entity_key} -> state={state} (id={row_id})")
    return row_id


async def mark_notified(item_id: int) -> None:
    """Stamp an item as having been notified about in its current state —
    this is the durable fact that closes the 'did you already tell me about
    this' gap, separate from the classification itself."""
    now = _now_iso()
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(TrackedItem).where(TrackedItem.id == item_id))
        row = result.scalar_one_or_none()
        if row is None:
            return
        row.notified = 1
        row.notified_at = now
        await session.commit()


async def set_dismissed(item_id: int, dismissed: bool) -> bool:
    """Mark a tracked item handled/unhandled from the dashboard. Returns
    False if the item doesn't exist so the route can 404 instead of
    silently no-op'ing."""
    now = _now_iso()
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(TrackedItem).where(TrackedItem.id == item_id))
        row = result.scalar_one_or_none()
        if row is None:
            return False
        row.dismissed = 1 if dismissed else 0
        row.dismissed_at = now if dismissed else None
        await session.commit()
    return True


async def get_by_id(item_id: int) -> TrackedItem | None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(TrackedItem).where(TrackedItem.id == item_id))
        return result.scalar_one_or_none()


async def set_urgency(item_id: int, urgency: str) -> bool:
    """Adjust a tracked item's urgency from the dashboard — the "lower
    importance / mark important" correction. Returns False if the item
    doesn't exist so the route can 404 instead of silently no-op'ing.
    Doesn't touch dismissed/notified state — an urgency correction and a
    "handled" state are independent facts about the item."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(TrackedItem).where(TrackedItem.id == item_id))
        row = result.scalar_one_or_none()
        if row is None:
            return False
        row.urgency = urgency
        row.updated_at = _now_iso()
        await session.commit()
    return True


async def list_items(
    platform: str,
    user_id: str,
    item_type: str | None = None,
) -> list[TrackedItem]:
    """List tracked items for a user, optionally filtered to one item_type,
    most recently updated first. Returns everything regardless of lifecycle
    state — this repo doesn't know what counts as "terminal" for a given
    item_type (paid vs unpaid, offer vs rejected, past vs upcoming), so that
    filtering belongs to the caller, not here."""
    async with AsyncSessionLocal() as session:
        query = select(TrackedItem).where(
            TrackedItem.platform == platform,
            TrackedItem.user_id == user_id,
        )
        if item_type:
            query = query.where(TrackedItem.item_type == item_type)
        query = query.order_by(TrackedItem.updated_at.desc())
        result = await session.execute(query)
        return list(result.scalars().all())
