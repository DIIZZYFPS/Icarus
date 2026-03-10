import os
import logging
import httpx
from fastapi import APIRouter, Request, BackgroundTasks
from backend.agent.processor import process_message

logger = logging.getLogger(__name__)
router = APIRouter()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "mock_token")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
ALLOWED_CHAT_ID = int(os.environ.get("ALLOWED_CHAT_ID", "0"))

def split_message(text: str, limit: int = 4000) -> list[str]:
    """Split text into chunks at newline boundaries where possible."""
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, limit)
        if split_at == -1:
            split_at = limit
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks

async def push_telegram_message(chat_id: int, text: str):
    """Send a message to a Telegram chat."""
    chunks = split_message(text)
    async with httpx.AsyncClient() as client:
        for chunk in chunks:
            try:
                await client.post(TELEGRAM_API_URL, json={"chat_id": chat_id, "text": chunk})
            except httpx.HTTPError as e:
                logger.error(f"Failed to push message back to Telegram: {e}")
                break
        logger.info(f"Sent response in {len(chunks)} message(s) to Telegram.")

async def handle_telegram_payload(chat_id: int, text: str):
    """Process Telegram message and send response."""
    response_text = await process_message("telegram", str(chat_id), text)
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
    username = user.get("username") or user.get("first_name") or "unknown"
    user_id = user.get("id")
    text = message.get("text")

    if chat_id and text:
        if chat_id != ALLOWED_CHAT_ID:
            return {"status": "ok"}  # Silently drop unknown senders
        
        # Standardize message format with identity prefix
        formatted_text = f"[User:{username} (id: {user_id})]: {text}"
        background_tasks.add_task(handle_telegram_payload, chat_id, formatted_text)

    return {"status": "ok"}
