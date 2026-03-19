<<<<<<< Updated upstream
"""
processor.py — Core message processing for Icarus L1 agent.

Handles incoming messages from Telegram/Discord, injects memory context
from the SQLite+FTS5 memory repository, runs the ADK agent, and persists
conversation history in Redis.
"""

=======
>>>>>>> Stashed changes
import re
import time
import json
import logging
<<<<<<< Updated upstream
from google.adk.runners import Runner
from google.genai.types import Content, Part
from backend.agent.engine import get_engine, get_readonly_engine
from backend.agent.tools import current_platform, current_user_id, current_chat_id
from backend.database.redis_connection import get_redis_client
=======
import inspect
from collections import OrderedDict
from google.adk.runners import Runner
from google.genai.types import Content, Part
from backend.agent.engine import get_engine, get_readonly_engine
from backend.agent.tools import (
    read_file, list_directory, replace_file_contents, request_create_file,
    append_memory, ICARUS_READONLY_TOOLS,
    current_platform, current_user_id, current_chat_id
)
from backend.agent.memory_repo import retrieve_relevant
from backend.agent.esc_tool import escalate_to_councilor, consult_councilor, check_mailbox
from backend.agent.github_tools import (
    github_get_repo_info, github_list_repos, github_read_file,
    github_list_issues, github_read_issue, github_create_issue, github_write_file,
    github_create_pr, github_create_branch
)
>>>>>>> Stashed changes

logger = logging.getLogger(__name__)

MAX_HISTORY_TURNS = 6
MEMORY_INJECT_LIMIT = 30
ICARUS_CONTEXT = "Conversation history:\n"
_CHAT_HISTORY_MAX_USERS = 256  # LRU cap — evict oldest user on overflow

<<<<<<< Updated upstream
=======
# Bounded LRU conversation history keyed by platform-specific ID (e.g., "telegram_123").
# Uses OrderedDict for O(1) move-to-end and O(1) popitem(last=False) eviction.
_chat_history: OrderedDict[str, list[tuple[str, str]]] = OrderedDict()
>>>>>>> Stashed changes

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

<<<<<<< Updated upstream
    Uses the current user message as a search query to find semantically
    relevant memories, ranked by FTS5 BM25 score + importance + recency.
    """
    try:
        from backend.agent.memory_repo import retrieve_relevant

=======
    for tool_name in _TOOL_SCAN_ORDER:
        fn, param_names = _TOOL_REGISTRY[tool_name]
        raw_kwargs: dict | None = None

        m = re.search(rf"\b{re.escape(tool_name)}\s*\(", text)
        if m:
            end = _find_call_end(text, m.end() - 1)
            if end != -1:
                call_str = text[m.start():end + 1]
                logger.info(f"[fallback] Detected Python call: {call_str[:120]}")
                try:
                    tree = ast.parse(call_str, mode="eval")
                    if isinstance(tree.body, ast.Call):
                        raw_kwargs = {}
                        for i, arg in enumerate(tree.body.args):
                            if i < len(param_names):
                                try:
                                    raw_kwargs[param_names[i]] = ast.literal_eval(arg)
                                except ValueError:
                                    pass
                        for kw in tree.body.keywords:
                            try:
                                raw_kwargs[kw.arg] = ast.literal_eval(kw.value)
                            except ValueError:
                                pass
                except SyntaxError as e:
                    logger.warning(f"[fallback] SyntaxError parsing {tool_name}(): {e}")

        if raw_kwargs is None:
            m_json = re.search(rf"\b{re.escape(tool_name)}\s*(\{{)", text)
            if m_json:
                brace_pos = m_json.start(1)
                try:
                    parsed, _ = json.JSONDecoder().raw_decode(text, brace_pos)
                    if isinstance(parsed, dict):
                        raw_kwargs = parsed
                except json.JSONDecodeError as e:
                    logger.warning(f"[fallback] JSONDecodeError parsing {tool_name}{{}}: {e}")

        if not raw_kwargs:
            continue

        if read_only and tool_name not in _READONLY_ALLOWED_TOOLS:
            logger.info(f"[fallback] Skipping disallowed tool '{tool_name}' in read-only mode.")
            continue

        known = set(param_names)
        kwargs = {k: v for k, v in raw_kwargs.items() if k in known}
        unknown_vals = [v for k, v in raw_kwargs.items() if k not in known]
        remaining = [p for p in param_names if p not in kwargs]
        for i, v in enumerate(unknown_vals):
            if i < len(remaining):
                kwargs[remaining[i]] = v

        for k, v in _TOOL_DEFAULTS.get(tool_name, {}).items():
            kwargs.setdefault(k, v)

        logger.info(f"[fallback] Executing {tool_name}(kwargs={list(kwargs.keys())})")
        try:
            if inspect.iscoroutinefunction(fn):
                result = await fn(**kwargs)
            else:
                result = fn(**kwargs)
            if isinstance(result, list):
                result = "\n".join(str(x) for x in result)
            logger.info(f"[fallback] {tool_name}() succeeded")
            return result or raw
        except Exception as e:
            logger.error(f"[fallback] {tool_name}() execution failed: {e}")
            continue

    return raw

async def _load_memory_context(platform: str, user_id: str, query: str, read_only: bool = False) -> str:
    """Query SQLite for memory entries relevant to the current user message.
    When read_only=True, only global entries are returned to prevent private
    memory leakage into untrusted (public server) contexts.
    """
    try:
>>>>>>> Stashed changes
        entries = await retrieve_relevant(
            platform=platform,
            user_id=user_id,
            query=query,
<<<<<<< Updated upstream
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

=======
            limit=MEMORY_INJECT_LINES,
            read_only=read_only,
        )
        if not entries:
            return ""
        lines = [f"[{e.created_at}] [{e.category.upper()}] {e.entry}" for e in entries]
>>>>>>> Stashed changes
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

<<<<<<< Updated upstream
        history = await get_chat_history(history_id)
        memory_context = await _load_memory_context(platform, user_id, query=text, read_only=read_only)

        # Identity and context headers
        context_header = (
            f"[CONTEXT: Platform={platform.upper()}, UserID={user_id}, "
            f"ChatID={chat_id}, Access={'ReadOnly' if read_only else 'Full'}]\n"
        )

=======
        history = _chat_history.get(history_id, [])
        memory_context = await _load_memory_context(platform, user_id, query=text, read_only=read_only)
        
        # Identity and context headers to ensure the model knows its environment
        context_header = f"[CONTEXT: Platform={platform.upper()}, UserID={user_id}, ChatID={chat_id}, Access={'ReadOnly' if read_only else 'Full'}]\n"
        
>>>>>>> Stashed changes
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
<<<<<<< Updated upstream
            await save_chat_history(history_id, (text, response_text))
=======
            history.append((text, response_text))
            _chat_history[history_id] = history[-MAX_HISTORY_TURNS:]
            _chat_history.move_to_end(history_id)
            while len(_chat_history) > _CHAT_HISTORY_MAX_USERS:
                _chat_history.popitem(last=False)  # evict least recently used
>>>>>>> Stashed changes

        return response_text or "[No response]"
    finally:
        current_platform.reset(token_p)
        current_user_id.reset(token_u)
        current_chat_id.reset(token_c)
