"""
google_auth_health.py — say so when Google revokes a refresh token, instead of failing quietly.

Gmail and Calendar run on OAuth refresh tokens. When Google revokes one (an app left
in "Testing" mode expires them after 7 days; password changes and manual revokes do
too), every call fails with invalid_grant and the watchers just log errors. This sends
one alert per service per ALERT_TTL to the operator's Discord DM and, if the desktop
avatar is connected, as a spoken pop-up.
"""

import asyncio
import logging

logger = logging.getLogger(__name__)

ALERT_TTL = 12 * 3600
SCRIPTS = {
    "Gmail": ("generate_gmail_refresh_token", "email triage and Gmail tools"),
    "Calendar": ("generate_calendar_refresh_token", "calendar sync and reminders"),
}


def is_revoked(error) -> bool:
    return "invalid_grant" in str(error)


async def report_google_auth_failure(service: str, error) -> None:
    if not is_revoked(error):
        return
    try:
        from backend.database.redis_connection import get_redis_client
        if not await get_redis_client().set(f"icarus:alerts:google_auth:{service.lower()}", "1", ex=ALERT_TTL, nx=True):
            return
    except Exception as e:
        logger.debug(f"[google-auth] alert dedupe unavailable: {e}")
    script, what = SCRIPTS.get(service, ("generate_gmail_refresh_token", service))
    text = (
        f"⚠️ {service} access expired: Google revoked Icarus's refresh token, so {what} are down.\n"
        f"Fix: on the host, from the repo root run `.venv/bin/python -m backend.scripts.{script}`, "
        "then `docker compose up -d --no-deps icarus-api`.\n"
        "If this happens about every 7 days, the Google OAuth app is still in Testing mode: "
        "publish it to Production in Google Cloud Console (Google Auth Platform → Audience)."
    )
    logger.error(f"[google-auth] {service} refresh token revoked; alerting the operator")
    try:
        from backend.agent.operator_notify import send_operator_dm
        await asyncio.to_thread(send_operator_dm, text)
    except Exception as e:
        logger.warning(f"[google-auth] Discord alert failed: {e}")
    try:
        from backend.agent.desktop_notify import announcer
        await announcer.consider({"type": "system_alert", "message": text})
    except Exception as e:
        logger.debug(f"[google-auth] desktop alert skipped: {e}")
