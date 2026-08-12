"""
processor.py — Core message processing for Icarus L1 agent.

Handles incoming messages from Telegram/Discord, injects memory context
from the SQLite+FTS5 memory repository, runs the L1 agent loop (local model,
no ADK — see engine.py), and persists conversation history in Redis.
"""

import re
import json
import logging
from typing import Awaitable, Callable
from backend.agent.engine import run_icarus
from backend.agent.telemetry_tools import fetch_latest_telemetry_snapshot
from backend.agent.tools import current_platform, current_user_id, current_chat_id
from backend.database.redis_connection import get_redis_client

logger = logging.getLogger(__name__)

MAX_HISTORY_TURNS = 6
MEMORY_INJECT_LIMIT = 30
ICARUS_CONTEXT = "Conversation history:\n"
HEALTH_QUERY_PATTERNS = (
    "how are you feeling",
    "how are you doing",
    "system health",
    "simulation readiness",
    "resource usage",
    "telemetry",
)


def _is_health_status_query(text: str) -> bool:
    lowered = (text or "").lower()
    if any(pattern in lowered for pattern in HEALTH_QUERY_PATTERNS):
        return True

    return bool(re.search(r"\b(status|health|readiness)\b", lowered))


def _fmt_metric(value: float | None, suffix: str = "") -> str:
    if value is None:
        return "n/a"
    return f"{value:.1f}{suffix}"


async def _build_proactive_health_note(user_text: str) -> str:
    """Inject a short operational warning only for non-health prompts in red state."""
    if _is_health_status_query(user_text):
        return ""

    snapshot = await fetch_latest_telemetry_snapshot()
    if not snapshot:
        return ""

    readiness = (snapshot.get("simulation_readiness") or "").lower()
    if readiness != "red":
        return ""

    cpu = _fmt_metric(snapshot.get("cpu_percent"), "%")
    mem_used = _fmt_metric(snapshot.get("memory_used_mb"), "MB")
    mem_total = _fmt_metric(snapshot.get("memory_total_mb"), "MB")
    disk = _fmt_metric(snapshot.get("disk_used_percent"), "%")
    pressure = _fmt_metric(snapshot.get("pressure_score"))
    gpu = _fmt_metric(snapshot.get("gpu_util_percent"), "%")

    return (
        "[SYSTEM HEALTH NOTE]\n"
        "Latest telemetry indicates high pressure. "
        f"readiness=red, pressure_score={pressure}, cpu={cpu}, "
        f"memory={mem_used}/{mem_total}, disk={disk}, gpu={gpu}. "
        "Keep the primary response focused; append one brief operational warning.\n"
    )


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
    on_activity: Callable[[str], Awaitable[None]] | None = None,
    skip_history: bool = False,
) -> str:
    """Core message processing logic — runs the L1 agent loop against the local model.

    on_activity, if given, is called with a short status string before each
    tool call executes, so a caller (e.g. Discord) can show live progress
    instead of going silent until the whole turn finishes.

    skip_history — for system-generated hydration prompts (a Councilor
    mailbox relay, an email-notification relay): these are self-contained,
    single-turn analyses, not a continuation of the user's own conversation.
    Injecting recent chat turns ahead of them is actively harmful, not just
    unnecessary — found via a real incident where two unrelated prior
    "smoke test" exchanges sat immediately before a spam-notification
    hydration prompt in the injected history, and the model pattern-matched
    onto that instead of the actual new content, producing a response about
    the wrong thing entirely while still "succeeding" mechanically. Doesn't
    affect transcript logging (still unconditional) or the Redis session
    history for the user's own turns (still read/written normally elsewhere)
    — it only skips history for *this* call's own prompt assembly and skips
    writing this call's (prompt, response) pair into that history, so a
    hydration prompt can't itself become confusing context for the next
    real user turn either.
    """
    token_p = current_platform.set(platform)
    token_u = current_user_id.set(user_id)
    token_c = current_chat_id.set(str(chat_id))

    try:
        history_id = f"{platform}_{user_id}"

        history = [] if skip_history else await get_chat_history(history_id)
        memory_context = await _load_memory_context(platform, user_id, query=text, read_only=read_only)
        health_note_context = await _build_proactive_health_note(text)

        # Log the incoming turn to the permanent transcript unconditionally —
        # before the run, not after, so it's captured even if the agent call
        # below fails. This is what closes the "does head A know what head B
        # did" gap: notifications and hydration prompts flow through this same
        # path, so they're findable later via the recall tool regardless of
        # whether any agent chose to append_memory about them.
        try:
            from backend.agent.transcript_repo import log_message
            await log_message(platform, user_id, "user", text)
        except Exception as e:
            logger.warning(f"[transcript] Failed to log incoming message: {e}")

        # Identity and context headers
        context_header = (
            f"[CONTEXT: Platform={platform.upper()}, UserID={user_id}, "
            f"ChatID={chat_id}, Access={'ReadOnly' if read_only else 'Full'}]\n"
        )

        history_text = "".join(f"User: {u}\nIcarus: {a}\n\n" for u, a in history)
        full_prompt = (
            context_header
            + memory_context
            + health_note_context
            + ICARUS_CONTEXT
            + history_text
            + f"User: {text}"
        )

        response_text = await run_icarus(full_prompt, read_only=read_only, on_activity=on_activity)

        if response_text:
            # Strip think tags if the model produces them
            response_text = re.sub(r"<think>.*?</think>", "", response_text, flags=re.DOTALL).strip()
            if not skip_history:
                await save_chat_history(history_id, (text, response_text))
            try:
                from backend.agent.transcript_repo import log_message
                await log_message(platform, user_id, "assistant", response_text)
            except Exception as e:
                logger.warning(f"[transcript] Failed to log response: {e}")

        return response_text or "[No response]"
    finally:
        current_platform.reset(token_p)
        current_user_id.reset(token_u)
        current_chat_id.reset(token_c)
