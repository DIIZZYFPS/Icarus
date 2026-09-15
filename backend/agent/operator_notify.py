"""
operator_notify.py — Push a message to the operator on Telegram or Discord
and hand back the ids of the messages actually delivered.

councilor.py had these as private helpers that returned nothing. Two
things needed them shared and id-returning:

  - A persistent subagent (worker_subagent.py, in a container) has to be
    able to reach the operator the same way the Councilor does, without
    importing councilor.py.
  - Phase 5's pause/resume needs to know WHICH message asked the question,
    so an operator reply-to that message resolves to exactly one waiting
    subagent (backend/agent/subagent_resume.py).

Synchronous urllib, same as before — callers on an event loop wrap this in
asyncio.to_thread. stdlib only, so it imports in the Councilor's minimal
host venv and inside the worker image alike.
"""

import os
import json
import logging
import urllib.request
from dataclasses import dataclass, field

from backend.utils import split_message

logger = logging.getLogger(__name__)

USER_AGENT = "Icarus-Councilor-v2.0.0"


@dataclass
class NotifyResult:
    platform: str
    target_id: str | None                              # chat id / channel id the message went to
    message_ids: list[str] = field(default_factory=list)

    @property
    def delivered(self) -> bool:
        return bool(self.message_ids)


def send_telegram(message: str, chat_id: str | None = None) -> NotifyResult:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or os.getenv("ALLOWED_CHAT_ID")
    result = NotifyResult("telegram", str(chat_id) if chat_id else None)
    if not token or not chat_id:
        return result
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for chunk in split_message(message, 4000):
        try:
            data = json.dumps({"chat_id": int(chat_id), "text": chunk}).encode()
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read().decode() or "{}")
            message_id = (body.get("result") or {}).get("message_id")
            if message_id is not None:
                result.message_ids.append(str(message_id))
        except Exception as e:
            logger.warning(f"Failed to send Telegram notification: {e}")
            break
    return result


def send_discord(message: str, channel_id: str | None = None) -> NotifyResult:
    token = os.getenv("DISCORD_BOT_TOKEN")
    channel_id = channel_id or os.getenv("DISCORD_ALLOWED_CHANNEL_ID")
    result = NotifyResult("discord", str(channel_id) if channel_id else None)
    if not token or not channel_id:
        return result
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
    for chunk in split_message(message, 2000):
        try:
            data = json.dumps({"content": chunk}).encode()
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read().decode() or "{}")
            if body.get("id"):
                result.message_ids.append(str(body["id"]))
        except Exception as e:
            logger.warning(f"Failed to send Discord notification: {e}")
            break
    return result


def notify(platform: str | None, chat_id: str | None, message: str) -> NotifyResult:
    """Send to the originating platform; Telegram is the fallback, as before."""
    if platform == "discord":
        return send_discord(message, chat_id)
    return send_telegram(message, chat_id)
