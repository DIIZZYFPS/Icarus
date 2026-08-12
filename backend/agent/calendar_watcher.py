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
import logging
import asyncio

logger = logging.getLogger(__name__)

SYNC_INTERVAL_SECONDS = 15 * 60  # calendars don't change fast enough to warrant tighter polling

_known_event_ids: set[str] = set()
_seeded = False


async def _sync_once():
    global _seeded

    from backend.agent.calendar_tools import sync_upcoming_events_to_tracked_items

    platform = "discord"
    user_id = os.environ.get("DISCORD_OPERATOR_ID", "0")

    synced, events = await sync_upcoming_events_to_tracked_items(platform=platform, user_id=user_id)
    if synced == 0 and not events:
        return  # unconfigured, or nothing upcoming — either way, nothing to report

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


async def run_calendar_watcher():
    if not os.getenv("GOOGLE_CALENDAR_OAUTH_REFRESH_TOKEN", "").strip():
        logger.info("[calendar_watcher] GOOGLE_CALENDAR_OAUTH_REFRESH_TOKEN not set — watcher idle.")

    logger.info(f"[calendar_watcher] Started. Polling every {SYNC_INTERVAL_SECONDS}s.")
    while True:
        try:
            await _sync_once()
        except Exception as e:
            logger.error(f"[calendar_watcher] Sync error: {e}")
        await asyncio.sleep(SYNC_INTERVAL_SECONDS)
