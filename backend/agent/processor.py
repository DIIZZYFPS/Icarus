"""
processor.py — Core message processing for Icarus L1 agent.

Handles incoming messages from Telegram/Discord, injects memory context
from the SQLite+FTS5 memory repository, runs the ADK agent, and persists
conversation history in Redis.
"""

import re
import time
import json
import logging
from google.adk.runners import Runner
from google.genai.types import Content, Part
from backend.agent.engine import get_engine, get_readonly_engine
from backend.agent.tools import current_platform, current_user_id, current_chat_id
from backend.database.redis_connection import get_redis_client

logger = logging.getLogger(__name__)

MAX_HISTORY_TURNS = 6
MEMORY_INJECT_LIMIT = 30
ICARUS_CONTEXT = "Conversation history:\n"


# ── Chat History (Redis-backed) ─────────────────────────────────────────────

async def get_chat_history(history_id: str) -> list[tuple[str, str]]:
    """Retrieve chat history from Redis."""
    redis = get_redis_client()
    key = f"icarus:session:{history_id}"
    try:
        raw_history = await redis.lrange(key, 0, MAX_HISTORY_TURNS - 1)
        history = []
        for item in reversed(raw_history):
            try:
                history.append(json.loads(item))
            except json.JSONDecodeError:
                continue
        return history
    except Exception as e:
        logger.error(f"Failed to retrieve chat history: {e}")
        return []


async def save_chat_history(history_id: str, history_item: tuple[str, str]):
    """Save chat history item to Redis."""
    redis = get_redis_client()
    key = f"icarus:session:{history_id}"
    try:
        await redis.lpush(key, json.dumps(history_item))
        await redis.ltrim(key, 0, MAX_HISTORY_TURNS - 1)
        await redis.expire(key, 3600 * 24 * 7)
    except Exception as e:
        logger.error(f"Failed to save chat history: {e}")


# ── Memory Context (SQLite+FTS5) ────────────────────────────────────────────

async def _load_memory_context(platform: str, user_id: str, query: str, read_only: bool = False) -> str:
    """Retrieve relevant memory entries from the SQLite+FTS5 repository.

    Uses the current user message as a search query to find semantically
    relevant memories, ranked by FTS5 BM25 score + importance + recency.
    """
    try:
        from backend.agent.memory_repo import retrieve_relevant

        entries = await retrieve_relevant(
            platform=platform,
            user_id=user_id,
            query=query,
            limit=MEMORY_INJECT_LIMIT,
            read_only=read_only,
        )

        if not entries:
            return ""

        lines = []
        for e in entries:
            visibility_tag = "[SYSTEM]" if e.visibility == "global" else ""
            category_tag = f"[{e.category.upper()}]" if e.category else ""
            lines.append(f"[{e.created_at}] {visibility_tag}{category_tag} {e.entry}")

        return "[PERSISTENT MEMORY — relevant entries]\n" + "\n".join(lines) + "\n"
    except Exception as e:
        logger.warning(f"[memory] Failed to load memory context: {e}")
        return ""


# ── Message Processing ──────────────────────────────────────────────────────

async def process_message(
    platform: str,
    user_id: str,
    text: str,
    chat_id: str = "0",
    read_only: bool = False,
) -> str:
    """Core message processing logic using ADK Runner."""
    token_p = current_platform.set(platform)
    token_u = current_user_id.set(user_id)
    token_c = current_chat_id.set(str(chat_id))

    try:
        runner: Runner = get_readonly_engine() if read_only else get_engine()
        history_id = f"{platform}_{user_id}"

        history = await get_chat_history(history_id)
        memory_context = await _load_memory_context(platform, user_id, query=text, read_only=read_only)

        # Identity and context headers
        context_header = (
            f"[CONTEXT: Platform={platform.upper()}, UserID={user_id}, "
            f"ChatID={chat_id}, Access={'ReadOnly' if read_only else 'Full'}]\n"
        )

        history_text = "".join(f"User: {u}\nIcarus: {a}\n\n" for u, a in history)
        full_prompt = context_header + memory_context + ICARUS_CONTEXT + history_text + f"User: {text}"

        session_id = f"{platform}_{user_id}_{int(time.time())}"

        message = Content(role="user", parts=[Part(text=full_prompt)])
        response_text = ""
        async for event in runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=message,
        ):
            if event.is_final_response() and event.content:
                text_parts = [p.text for p in event.content.parts if hasattr(p, "text") and p.text]
                response_text = text_parts[-1] if text_parts else ""
                break

        if response_text:
            # Strip think tags if the model produces them
            response_text = re.sub(r"<think>.*?</think>", "", response_text, flags=re.DOTALL).strip()
            await save_chat_history(history_id, (text, response_text))

        return response_text or "[No response]"
    finally:
        current_platform.reset(token_p)
        current_user_id.reset(token_u)
        current_chat_id.reset(token_c)
