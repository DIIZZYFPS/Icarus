import os
import re
import ast
import json
import time
import httpx
import logging
from fastapi import APIRouter, Request, BackgroundTasks
from google.adk.runners import Runner
from google.genai.types import Content, Part
from backend.agent.engine import get_engine
from backend.agent.tools import _respond_ctx, respond, read_file, list_directory, replace_file_contents
from backend.agent.esc_tool import escalate_to_councilor

logger = logging.getLogger(__name__)
router = APIRouter()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "mock_token")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
ALLOWED_CHAT_ID = int(os.environ.get("ALLOWED_CHAT_ID", "0"))
MAX_HISTORY_TURNS = 6  # plain-text turns included in every prompt

# Minimal context header — identity and tool law are in LlmAgent.instruction (engine.py).
# This only carries the conversation history preamble.
ICARUS_CONTEXT = "Conversation history:\n"

# Plain-text conversation history keyed by chat_id.
# Stored separately from ADK sessions to avoid tool-role accumulation.
_chat_history: dict[int, list[tuple[str, str]]] = {}

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


# Maps tool names → (function, ordered param names for positional arg fallback)
_TOOL_REGISTRY = {
    "respond":               (respond,               ["message"]),
    "escalate_to_councilor": (escalate_to_councilor, ["intent_description", "target_files"]),
    "replace_file_contents": (replace_file_contents, ["filepath", "new_contents"]),
    "read_file":             (read_file,              ["filepath"]),
    "list_directory":        (list_directory,         ["directory"]),
}
# Default values for optional/commonly-omitted parameters
_TOOL_DEFAULTS = {
    "escalate_to_councilor": {"target_files": []},
}
# Scan in priority order — respond first, then terminals, then reads
_TOOL_SCAN_ORDER = ["respond", "escalate_to_councilor", "replace_file_contents", "read_file", "list_directory"]

def _find_call_end(text: str, paren_open: int) -> int:
    """Return the index of the closing ')' matching text[paren_open]='('."""
    depth = 0
    in_str = False
    str_char = None
    i = paren_open
    while i < len(text):
        c = text[i]
        if in_str:
            if c == "\\" :
                i += 2
                continue
            if text[i:i+len(str_char)] == str_char:
                in_str = False
                i += len(str_char)
                continue
        else:
            for q in ('"""', "'''", '"', "'"):
                if text[i:i+len(q)] == q:
                    in_str = True
                    str_char = q
                    i += len(q)
                    break
            else:
                if c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                    if depth == 0:
                        return i
                i += 1
                continue
            continue
        i += 1
    return -1

def _dispatch_tool_call(raw: str) -> str:
    """Fallback: scan raw model output for any known tool call, execute it,
    and return the result. Handles all 5 tools via ast.parse for robustness.
    Falls back to the raw text if nothing is parseable."""
    # Strip markdown code fences and 'tool_call' prefix labels the model emits.
    # Model uses both "tool_call: name(...)" and "tool_call name{...}" formats.
    text = re.sub(r"```(?:python)?\s*", "", raw)
    text = re.sub(r"\btool_call[\s:]+", "", text).strip()

    for tool_name in _TOOL_SCAN_ORDER:
        fn, param_names = _TOOL_REGISTRY[tool_name]
        raw_kwargs: dict | None = None

        # --- Path A: Python call format  tool_name(key=val, ...) ---
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

        # --- Path B: JSON call format  tool_name{...}  (Gemma native output) ---
        if raw_kwargs is None:
            m_json = re.search(rf"\b{re.escape(tool_name)}\s*(\{{)", text)
            if m_json:
                brace_pos = m_json.start(1)
                logger.info(f"[fallback] Detected JSON call: {text[m_json.start():brace_pos + 40]!r}")
                try:
                    parsed, _ = json.JSONDecoder().raw_decode(text, brace_pos)
                    if isinstance(parsed, dict):
                        raw_kwargs = parsed
                    else:
                        logger.warning(f"[fallback] {tool_name} JSON payload is not a dict")
                except json.JSONDecodeError as e:
                    logger.warning(f"[fallback] JSONDecodeError parsing {tool_name}{{}}: {e}")

        if not raw_kwargs:
            continue

        # Normalize: remap any wrong kwarg names positionally to expected params
        known = set(param_names)
        kwargs = {k: v for k, v in raw_kwargs.items() if k in known}
        unknown_vals = [v for k, v in raw_kwargs.items() if k not in known]
        remaining = [p for p in param_names if p not in kwargs]
        for i, v in enumerate(unknown_vals):
            if i < len(remaining):
                logger.info(f"[fallback] Remapped unknown kwarg to '{remaining[i]}'")
                kwargs[remaining[i]] = v

        # Fill in defaults for any still-missing parameters
        for k, v in _TOOL_DEFAULTS.get(tool_name, {}).items():
            if kwargs.setdefault(k, v) == v:
                logger.info(f"[fallback] Applied default for '{k}'")

        logger.info(f"[fallback] Executing {tool_name}(kwargs={list(kwargs.keys())})")
        try:
            result = fn(**kwargs)
            if isinstance(result, list):
                result = "\n".join(str(x) for x in result)
            logger.info(f"[fallback] {tool_name}() succeeded")
            return result or raw
        except Exception as e:
            logger.error(f"[fallback] {tool_name}() execution failed: {e}")
            continue

    logger.debug("[fallback] No parseable tool call found in model output")
    return raw

async def process_telegram_payload(chat_id: int, text: str):
    runner: Runner = get_engine()

    # Build context: system prompt + last N plain-text turns + current message
    history = _chat_history.get(chat_id, [])
    history_text = "".join(f"User: {u}\nIcarus: {a}\n\n" for u, a in history)
    full_prompt = ICARUS_CONTEXT + history_text + f"User: {text}"

    # Fresh session every call — prevents tool-role accumulation in ADK history
    session_id = f"telegram_{chat_id}_{int(time.time())}"

    _respond_ctx.set('')
    message = Content(role="user", parts=[Part(text=full_prompt)])

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

    # Store clean user/response pair in plain-text history
    if response_text:
        # If respond() wasn't dispatched as a real tool call (pythonic parser
        # produced finish_reason=stop), extract the message from the raw text.
        if not _respond_ctx.get(''):
            response_text = _dispatch_tool_call(response_text)
        # Strip any leaked <think>...</think> blocks from Qwen3.5 thinking mode
        response_text = re.sub(r"<think>.*?</think>", "", response_text, flags=re.DOTALL).strip()
        history.append((text, response_text))
        _chat_history[chat_id] = history[-MAX_HISTORY_TURNS:]

    chunks = split_message(response_text or "[No response]")
    async with httpx.AsyncClient() as client:
        for chunk in chunks:
            try:
                await client.post(TELEGRAM_API_URL, json={"chat_id": chat_id, "text": chunk})
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
