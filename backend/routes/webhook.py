import os
import re
import logging
import httpx
from fastapi import APIRouter, Request, BackgroundTasks
from backend.agent.processor import process_message
from backend.utils import split_message

logger = logging.getLogger(__name__)
router = APIRouter()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "mock_token")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
ALLOWED_CHAT_ID = int(os.environ.get("ALLOWED_CHAT_ID", "0"))

_USERNAME_MAX_LEN = 64
_SANITIZE_PATTERN = re.compile(r'[\r\n\t:\[\]]')

# split_message imported from backend.utils

async def push_telegram_message(chat_id: int, text: str):
    """Send a message to a Telegram chat."""
    chunks = split_message(text, limit=4000)
    async with httpx.AsyncClient() as client:
        for chunk in chunks:
            try:
                await client.post(TELEGRAM_API_URL, json={"chat_id": chat_id, "text": chunk})
            except httpx.HTTPError as e:
                logger.error(f"Failed to push message back to Telegram: {e}")
                break
        logger.info(f"Sent response in {len(chunks)} message(s) to Telegram.")

def _sanitize_identifier(value: str) -> str:
    """Normalize a user-provided identifier to prevent prompt injection.
    Removes control characters and bracket/colon characters that could spoof
    the [User:...] header, then truncates to a safe maximum length.
    Falls back to 'anonymous' if all characters are stripped.
    """
    value = _SANITIZE_PATTERN.sub(' ', value)
    value = value.strip()[:_USERNAME_MAX_LEN]
    return value if value else "anonymous"

async def handle_telegram_payload(chat_id: int, user_id: str, text: str):
    """Process Telegram message and send response."""
    response_text = await process_message("telegram", user_id, text, chat_id=str(chat_id))
    await push_telegram_message(chat_id, response_text)

@router.post("/webhook/telegram")
async def telegram_webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        payload = await request.json()
    except Exception:
        return {"status": "error", "message": "Invalid JSON"}

    message = payload.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    user = message.get("from", {})
    username = _sanitize_identifier(user.get("username") or user.get("first_name") or "unknown")
    # Use the sender's Telegram user ID for per-user memory/history isolation.
    # Fall back to chat_id only if from.id is absent (e.g., channel posts).
    user_id = str(user.get("id") or chat_id)
    text = message.get("text")

    if chat_id and text:
        if chat_id != ALLOWED_CHAT_ID:
            return {"status": "ok"}  # Silently drop unknown senders

        # Standardize message format with sanitized identity prefix
        formatted_text = f"[User:{username} (id: {user_id})]: {text}"
        background_tasks.add_task(handle_telegram_payload, chat_id, user_id, formatted_text)

    return {"status": "ok"}
