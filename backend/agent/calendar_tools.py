"""
calendar_tools.py — Google Calendar integration for Icarus.

Scaffold: mirrors gmail_tools.py's OAuth pattern (personal refresh-token
auth, no service-account fallback needed here). No live credentials exist
yet — GOOGLE_CALENDAR_OAUTH_REFRESH_TOKEN is unset until you generate one, so
every tool here degrades to a clear "not configured" message rather than
erroring. Uses the same GCP OAuth client as Gmail (GMAIL_OAUTH_CLIENT_ID/
_SECRET) — Calendar just needs its own consented scope/refresh token. Extend
backend/scripts/generate_gmail_refresh_token.py's scope list to include
calendar.readonly and rerun it to get one, or run a separate flow.

Read-only scope by default (calendar.readonly) — this is an awareness
feature, not a scheduling one. Only upgrade to a write scope if Icarus is
ever asked to create/modify events on your behalf.

sync_upcoming_events_to_tracked_items() is the intended entry point for a
future calendar watcher (sibling to gmail_watcher.py) — it feeds
tracked_items_repo the same way worker_email_triage.py already does, so
calendar events land in the same structured, queryable substrate as jobs
and bills rather than becoming another isolated silo.
"""

import os
import re
import logging
import asyncio
from datetime import datetime, timezone

from google.oauth2.credentials import Credentials as UserCredentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']

_NOT_CONFIGURED_MSG = (
    "Calendar service not configured — set GOOGLE_CALENDAR_OAUTH_REFRESH_TOKEN "
    "(reuses GMAIL_OAUTH_CLIENT_ID/_SECRET with a separate consented scope; "
    "see backend/scripts/generate_gmail_refresh_token.py)."
)


def get_calendar_service():
    """Authenticate and return a Calendar API service, or None if not configured."""
    client_id = (os.getenv("GMAIL_OAUTH_CLIENT_ID") or "").strip()
    client_secret = (os.getenv("GMAIL_OAUTH_CLIENT_SECRET") or "").strip()
    refresh_token = (os.getenv("GOOGLE_CALENDAR_OAUTH_REFRESH_TOKEN") or "").strip()

    if not (client_id and client_secret and refresh_token):
        logger.info("[calendar] Not configured — GOOGLE_CALENDAR_OAUTH_REFRESH_TOKEN unset.")
        return None

    try:
        creds = UserCredentials(
            token=None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=SCOPES,
        )
        return build("calendar", "v3", credentials=creds, cache_discovery=False)
    except Exception as e:
        logger.error(f"[calendar] Authentication failed: {e}")
        return None


def _run_sync(fn):
    loop = asyncio.get_running_loop()
    return loop.run_in_executor(None, fn)


def _normalize_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "unknown"


async def calendar_list_upcoming_events(max_results: int = 10, calendar_id: str = "primary") -> str:
    """List upcoming events on the calendar. Read-only.

    Args:
        max_results: Max events to return (default 10).
        calendar_id: Which calendar to read (default "primary").
    """
    service = get_calendar_service()
    if not service:
        return _NOT_CONFIGURED_MSG
    try:
        now = datetime.now(timezone.utc).isoformat()
        results = await _run_sync(
            lambda: service.events().list(
                calendarId=calendar_id, timeMin=now, maxResults=max_results,
                singleEvents=True, orderBy='startTime',
            ).execute()
        )
        events = results.get('items', [])
        if not events:
            return "No upcoming events."
        lines = []
        for e in events:
            start = e.get('start', {}).get('dateTime') or e.get('start', {}).get('date')
            lines.append(f"- {start}: {e.get('summary', '(no title)')}")
        return "Upcoming events:\n" + "\n".join(lines)
    except HttpError as e:
        logger.error(f"[calendar] Failed to list events: {e}")
        return f"Error: {e}"
    except Exception as e:
        logger.error(f"[calendar] Failed to list events: {e}")
        return f"Error: {e}"


async def sync_upcoming_events_to_tracked_items(
    platform: str, user_id: str, max_results: int = 25, calendar_id: str = "primary"
) -> int:
    """Upsert upcoming calendar events into tracked_items, same substrate as
    email-triage jobs/bills. Intended to be called periodically by a future
    calendar watcher — not wired to run automatically yet, since no
    credentials exist. Returns the number of events synced (0 if
    unconfigured)."""
    service = get_calendar_service()
    if not service:
        return 0

    from backend.agent.tracked_items_repo import upsert_item

    try:
        now = datetime.now(timezone.utc).isoformat()
        results = await _run_sync(
            lambda: service.events().list(
                calendarId=calendar_id, timeMin=now, maxResults=max_results,
                singleEvents=True, orderBy='startTime',
            ).execute()
        )
    except Exception as e:
        logger.error(f"[calendar] Sync failed to list events: {e}")
        return 0

    events = results.get('items', [])
    synced = 0
    for e in events:
        event_id = e.get('id')
        if not event_id:
            continue
        start = e.get('start', {}).get('dateTime') or e.get('start', {}).get('date')
        await upsert_item(
            platform=platform, user_id=user_id,
            item_type="calendar_event", entity_key=_normalize_key(event_id),
            state="upcoming",
            summary=e.get('summary', '(no title)'),
            due_at=start,
            payload={"location": e.get("location"), "html_link": e.get("htmlLink")},
            source="calendar",
        )
        synced += 1

    logger.info(f"[calendar] Synced {synced} upcoming events to tracked_items")
    return synced


CALENDAR_TOOLS = [calendar_list_upcoming_events]
