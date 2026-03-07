import os
import httpx
import logging
from fastapi import APIRouter, Request, BackgroundTasks
from google.adk.runners import Runner
from google.genai.types import Content, Part
from backend.agent.engine import get_engine, get_session_service
from backend.agent.tools import _respond_ctx

logger = logging.getLogger(__name__)
router = APIRouter()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "mock_token")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
ALLOWED_CHAT_ID = int(os.environ.get("ALLOWED_CHAT_ID", "0"))
MAX_SESSION_EVENTS = 20  # ~10 turns before oldest are dropped

ICARUS_CONTEXT = (
    "You are Icarus, an autonomous AI daemon. You MUST call a tool on every response — no exceptions.\n"
    "- Plain text reply → call respond(message='your reply')\n"
    "- File/directory task → call read_file or list_directory (never describe what they return)\n"
    "- Task beyond your capabilities → call escalate_to_councilor\n\n"
)

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

async def process_telegram_payload(chat_id: int, text: str):
    runner: Runner = get_engine()
    session_service = get_session_service()
    session_id = f"telegram_{chat_id}"

    _respond_ctx.set('')  # reset for this task
    message = Content(role="user", parts=[Part(text=ICARUS_CONTEXT + text)])

    response_text = ""
    async for event in runner.run_async(
        user_id=str(chat_id),
        session_id=session_id,
        new_message=message
    ):
        val = _respond_ctx.get('')
        if val:
            response_text = val
            break
        if event.is_final_response() and event.content:
            response_text = event.content.parts[0].text
            break

    # Sliding window: drop oldest events once session exceeds MAX_SESSION_EVENTS
    try:
        session = await session_service.get_session(
            app_name="icarus",
            user_id=str(chat_id),
            session_id=session_id
        )
        if session and len(session.events) > MAX_SESSION_EVENTS:
            session.events = session.events[-MAX_SESSION_EVENTS:]
    except Exception as e:
        logger.warning(f"Session trim failed (non-critical): {e}")

    chunks = split_message(response_text or "[No response]")
    async with httpx.AsyncClient() as client:
        for chunk in chunks:
            try:
                await client.post(TELEGRAM_API_URL, json={
                    "chat_id": chat_id,
                    "text": chunk
                })
            except httpx.HTTPError as e:
                logger.error(f"Failed to push message back to Telegram: {e}")
                break
        logger.info(f"Sent response in {len(chunks)} message(s) to Telegram.")

@router.post("/webhook/telegram")
async def telegram_webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        payload = await request.json()
    except Exception:
        return {"status": "error", "message": "Invalid JSON"}

    message = payload.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    text = message.get("text")

    if chat_id and text:
        if chat_id != ALLOWED_CHAT_ID:
            return {"status": "ok"}  # Silently drop unknown senders
        background_tasks.add_task(process_telegram_payload, chat_id, text)

    return {"status": "ok"}
