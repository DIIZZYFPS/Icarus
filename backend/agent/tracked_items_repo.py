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
    thread_id: str | None = None,
) -> int:
    """Create or update a tracked item. A state change resets `notified` to 0
    — the operator was told about the *previous* state, not this one, so a
    fresh state change should be eligible to notify again.

    message_id links this item back to the TriageClassification row that
    created it (see triage_repo.py) — same "latest wins" treatment as
    summary/urgency/due_at below, so a later email about the same item
    re-points the link at whichever message most recently touched it.
    thread_id is the Gmail thread this item's emails belong to, if any — see
    find_by_thread()'s docstring for why this is the identity signal a
    caller should check *before* falling back to entity_key matching."""
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
                thread_id=thread_id, notified=0, notified_at=None, created_at=now, updated_at=now,
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
            if thread_id is not None:
                row.thread_id = thread_id
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


async def get_by_identity(
    platform: str, user_id: str, item_type: str, entity_key: str,
) -> TrackedItem | None:
    """Look up a single row by the same (platform, user_id, item_type,
    entity_key) tuple upsert_item() dedups on — but read-only, and callable
    with a *different* item_type than the one you're about to write. This is
    what lets worker_email_triage.py check "is there already a scored
    job_opportunity for this company+role" before deciding whether to
    upsert_item(item_type="job_application", ...) — which would never find
    it, since upsert_item's own lookup is scoped to the item_type you pass
    it — or promote_to_application() the existing row instead."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(TrackedItem).where(
                TrackedItem.platform == platform,
                TrackedItem.user_id == user_id,
                TrackedItem.item_type == item_type,
                TrackedItem.entity_key == entity_key,
            )
        )
        return result.scalar_one_or_none()


async def find_by_thread(
    platform: str, user_id: str, thread_id: str, item_types: tuple[str, ...] = ("job_application", "job_opportunity"),
) -> TrackedItem | None:
    """The strongest identity signal a follow-up email carries: a reply
    within the same Gmail thread as an email that already created/touched a
    tracked item is the same underlying job, independent of how *this*
    message's own company/role text reads. Entity-key matching (company+role
    normalized text) is what upsert_item()/get_by_identity() rely on
    instead, and it's fragile — an OA/interview/rejection email often drops
    the job title entirely, or a sender's display name drifts ("Netic" vs
    "Netic AI"), which used to fork a second tracked_items row per stage
    instead of updating the one that already existed. Check this BEFORE
    entity_key matching, not as a tiebreaker after.

    Most-recently-updated match wins if more than one row somehow shares a
    thread_id (shouldn't happen in practice — one thread is one application
    — but no need to guess which if it does). Returns None if thread_id is
    falsy or nothing matches, so callers can unconditionally check it first
    without a None-guard at every call site."""
    if not thread_id:
        return None
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(TrackedItem).where(
                TrackedItem.platform == platform,
                TrackedItem.user_id == user_id,
                TrackedItem.item_type.in_(item_types),
                TrackedItem.thread_id == thread_id,
            ).order_by(TrackedItem.updated_at.desc())
        )
        return result.scalars().first()


async def find_latest_job_by_company(
    platform: str, user_id: str, company_key: str,
) -> TrackedItem | None:
    """Fallback for when an application-update email doesn't state a role at
    all (and so has no usable entity_key of its own to match) and there's no
    thread_id to fall back on either — e.g. a plain-text confirmation with
    nothing Gmail can thread. Matches by company alone: entity_key is always
    `normalize(company + "-" + role)`, so `company_key + "-"` is a reliable
    prefix regardless of what role text (or "unknown-role") got appended.

    Deliberately narrower than find_by_thread(): only call this when the
    current email's own role extraction came back empty. A role that *was*
    extracted but doesn't match an existing row is quite possibly a genuine
    second posting at the same company (see tracked_items for two real,
    distinct Netic internships) — collapsing those together would be worse
    than the duplicate-row bug this exists to prevent. Most-recently-updated
    match wins when a company has more than one open row."""
    prefix = f"{company_key}-"
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(TrackedItem).where(
                TrackedItem.platform == platform,
                TrackedItem.user_id == user_id,
                TrackedItem.item_type.in_(("job_application", "job_opportunity")),
                (TrackedItem.entity_key == company_key) | (TrackedItem.entity_key.like(prefix + "%")),
            ).order_by(TrackedItem.updated_at.desc())
        )
        return result.scalars().first()


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


async def promote_to_application(
    item_id: int,
    state: str = "applied",
    summary: str | None = None,
    due_at: str | None = None,
    urgency: str | None = None,
    payload: dict | None = None,
    message_id: str | None = None,
    thread_id: str | None = None,
) -> bool:
    """Flip a scored-but-not-applied-to item (item_type="job_opportunity",
    worker_job_scout.py) into a real application (item_type="job_application",
    worker_email_triage.py's territory) in place — same row, same entity_key,
    not a new one. The optional fields are "latest wins" overrides, same
    semantics as upsert_item()'s — pass them when triage is the caller (a
    real confirmation email has a summary/state/due_at worth recording), omit
    them when the dashboard's "Mark applied" button is the caller (nothing
    new to say beyond the state change itself).

    Callers: the dashboard's "Mark applied" button calls this directly by
    item_id when the operator confirms manually. worker_email_triage.py calls
    it when an inbound application-update email's (company, role) matches an
    *existing* job_opportunity row — found via get_by_identity(), NOT via
    upsert_item(), because upsert_item's own dedup lookup is scoped to the
    item_type you pass it and item_type="job_application" will never match an
    existing item_type="job_opportunity" row, no matter how identical the
    entity_key is. That asymmetry is exactly why this function exists instead
    of leaving it to upsert_item to "just work" — it doesn't, across types.

    Returns False if the item doesn't exist or isn't currently a
    job_opportunity, so the route/caller can 404/fall back instead of
    silently no-op'ing."""
    now = _now_iso()
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(TrackedItem).where(TrackedItem.id == item_id))
        row = result.scalar_one_or_none()
        if row is None or row.item_type != "job_opportunity":
            return False
        row.item_type = "job_application"
        row.state = state
        if summary is not None:
            row.summary = summary
        if due_at is not None:
            row.due_at = due_at
        if urgency is not None:
            row.urgency = urgency
        if payload is not None:
            # MERGE, not replace — unlike upsert_item's payload overwrite
            # (safe there: same item_type before and after, so it's always
            # "the same kind of data, newer version"), a promotion combines
            # two DIFFERENT kinds of data: the opportunity's match_score/
            # tailoring_suggestions/link (still valuable after promotion —
            # arguably more so, it's "why I applied") and the application
            # email's company/role/status. A bare replace here silently
            # deletes the scouting data the operator was just looking at.
            # Confirmed live: an earlier version of this function replaced
            # instead of merged and the job link vanished on promotion.
            #
            # Within the merge itself, skip keys whose incoming value is
            # None rather than overlaying them unconditionally — an
            # application-update email's own extraction (worker_email_
            # triage.py's `details`) fills "role" with None whenever the
            # email just says "your application" without restating the job
            # title, and a bare {**existing, **payload} would blow away the
            # real role the original job_scout posting already recorded.
            # Confirmed live: exactly this wiped Netic's role on an OA-sent
            # promotion before this guard existed.
            existing_payload = {}
            if row.payload:
                try:
                    existing_payload = json.loads(row.payload)
                except Exception:
                    existing_payload = {}
            incoming = {k: v for k, v in payload.items() if v is not None}
            row.payload = json.dumps({**existing_payload, **incoming})
        if message_id is not None:
            row.message_id = message_id
        if thread_id is not None:
            row.thread_id = thread_id
        row.notified = 0
        row.notified_at = None
        row.dismissed = 0
        row.dismissed_at = None
        row.updated_at = now
        await session.commit()
    logger.info(f"[tracked_items] promoted job_opportunity id={item_id} -> job_application (state={state})")
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
