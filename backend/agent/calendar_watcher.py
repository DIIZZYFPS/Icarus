"""
calendar_watcher.py — periodic Calendar → tracked_items sync.

Sibling to gmail_watcher.py, much simpler: Calendar has no push-notification
path in use here, so this just polls sync_upcoming_events_to_tracked_items()
on an interval. Degrades to a no-op loop (checks every interval, does
nothing) if GOOGLE_CALENDAR_OAUTH_REFRESH_TOKEN isn't set — same
"unconfigured is not an error" posture as the rest of the Calendar/Gmail
integration.

Diffs the synced event IDs against what it's already seen so it only
raises an activity event for genuinely new events — sync_upcoming_events_
to_tracked_items() re-upserts the whole window every poll (that's correct,
it keeps state fresh), but re-announcing the same 10 events every 15
minutes would just be noise on the dashboard.
"""

import os
import time
import logging
import asyncio
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

SYNC_INTERVAL_SECONDS = 5 * 60   # also how quickly a newly added event becomes remindable
REMINDER_CHECK_SECONDS = 60
REMINDER_MINUTES = float(os.getenv("CALENDAR_REMINDER_MINUTES", "10"))
REMINDER_DISCORD = os.getenv("CALENDAR_REMINDER_DISCORD", "1") != "0"

_known_event_ids: set[str] = set()
_seeded = False
_upcoming: list[dict] = []        # the last sync's events, for reminders
_reminded: set[str] = set()       # event id + start already reminded


async def _sync_once():
    global _seeded

    from backend.agent.calendar_tools import sync_upcoming_events_to_tracked_items

    platform = "discord"
    user_id = os.environ.get("DISCORD_OPERATOR_ID", "0")

    synced, events = await sync_upcoming_events_to_tracked_items(platform=platform, user_id=user_id)
    if synced == 0 and not events:
        return  # unconfigured, or nothing upcoming — either way, nothing to report
    _upcoming[:] = events

    current_ids = {e["id"] for e in events}

    if not _seeded:
        # First successful sync after startup — seed silently instead of
        # announcing every pre-existing event as "new".
        _known_event_ids.update(current_ids)
        _seeded = True
        logger.info(f"[calendar_watcher] Seeded with {len(current_ids)} existing event(s)")
        return

    new_events = [e for e in events if e["id"] not in _known_event_ids]
    _known_event_ids.update(current_ids)

    if new_events:
        from backend.agent.activity_repo import publish_activity
        summary = "; ".join(f"{e['summary']} ({e['start']})" for e in new_events[:5])
        await publish_activity(
            actor="system", event_type="calendar_synced",
            action=f"{len(new_events)} new calendar event(s)", detail=summary,
            platform=platform, user_id=user_id,
        )
        logger.info(f"[calendar_watcher] {len(new_events)} new event(s): {summary}")


def _start_of(event: dict) -> datetime | None:
    """Timed events only: all-day events (a bare date) don't get a 'starting soon'."""
    start = event.get("start") or ""
    if "T" not in start:
        return None
    try:
        return datetime.fromisoformat(start.replace("Z", "+00:00"))
    except ValueError:
        return None


def due_reminders(events: list[dict], now: datetime, minutes: float = REMINDER_MINUTES) -> list[tuple[dict, int]]:
    """Events starting within `minutes` that haven't been reminded yet; marks them reminded."""
    due = []
    for event in events:
        start = _start_of(event)
        if start is None:
            continue
        seconds = (start - now).total_seconds()
        key = f"{event.get('id')}@{event.get('start')}"
        if 0 < seconds <= minutes * 60 and key not in _reminded:
            _reminded.add(key)
            due.append((event, max(1, round(seconds / 60))))
    return due


def reminder_text(event: dict, minutes_left: int) -> str:
    start = _start_of(event)
    when = start.strftime("%-I:%M %p").lower() if start else ""
    where = f" ({event['location']})" if event.get("location") else ""
    return f"📅 {event.get('summary', '(no title)')} starts in {minutes_left} min, at {when}{where}."


async def _send_reminders():
    for event, minutes_left in due_reminders(_upcoming, datetime.now(timezone.utc)):
        text = reminder_text(event, minutes_left)
        logger.info(f"[calendar_watcher] reminder: {text}")
        if REMINDER_DISCORD:
            try:
                from backend.agent.operator_notify import send_operator_dm
                await asyncio.to_thread(send_operator_dm, text)
            except Exception as e:
                logger.warning(f"[calendar_watcher] Discord reminder failed: {e}")
        try:
            from backend.agent.desktop_notify import announcer
            await announcer.consider({"type": "calendar_soon", "message": text, "event_id": event.get("id")})
        except Exception as e:
            logger.debug(f"[calendar_watcher] desktop reminder skipped: {e}")


async def run_calendar_watcher():
    if not os.getenv("GOOGLE_CALENDAR_OAUTH_REFRESH_TOKEN", "").strip():
        logger.info("[calendar_watcher] GOOGLE_CALENDAR_OAUTH_REFRESH_TOKEN not set — watcher idle.")

    logger.info(f"[calendar_watcher] Started. Syncing every {SYNC_INTERVAL_SECONDS}s, "
                f"reminding {REMINDER_MINUTES:g} min before events.")
    last_sync = 0.0
    while True:
        if time.monotonic() - last_sync >= SYNC_INTERVAL_SECONDS:
            last_sync = time.monotonic()
            try:
                await _sync_once()
            except Exception as e:
                logger.error(f"[calendar_watcher] Sync error: {e}")
        try:
            await _send_reminders()
        except Exception as e:
            logger.error(f"[calendar_watcher] Reminder error: {e}")
        await asyncio.sleep(REMINDER_CHECK_SECONDS)
