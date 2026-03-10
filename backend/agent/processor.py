import os
import re
import ast
import json
import time
import logging
import inspect
from google.adk.runners import Runner
from google.genai.types import Content, Part
from backend.agent.engine import get_engine, get_readonly_engine
from backend.agent.tools import (
    read_file, list_directory, replace_file_contents, request_create_file,
    append_memory, MEMORY_LOG_PATH, ICARUS_READONLY_TOOLS
)
from backend.agent.esc_tool import escalate_to_councilor, consult_councilor, check_mailbox
from backend.agent.github_tools import (
    github_get_repo_info, github_list_repos, github_read_file,
    github_list_issues, github_create_issue, github_write_file,
    github_create_pr, github_create_branch
)

logger = logging.getLogger(__name__)

MAX_HISTORY_TURNS = 6  # plain-text turns included in every prompt
MEMORY_INJECT_LINES = 30  # number of recent memory entries to inject per session
ICARUS_CONTEXT = "Conversation history:\n"

# Plain-text conversation history keyed by platform-specific ID (e.g., "telegram_123" or "discord_456").
_chat_history: dict[str, list[tuple[str, str]]] = {}

# Maps tool names → (function, ordered param names for positional arg fallback)
_TOOL_REGISTRY = {
    "replace_file_contents": (replace_file_contents, ["filepath", "new_contents"]),
    "read_file":             (read_file,              ["filepath"]),
    "list_directory":        (list_directory,         ["directory"]),
    "request_create_file":   (request_create_file,    ["filepath", "contents"]),
    "append_memory":         (append_memory,          ["entry"]),
    "escalate_to_councilor": (escalate_to_councilor,  ["intent_description", "target_files"]),
    "consult_councilor":     (consult_councilor,      ["question"]),
    "check_mailbox":         (check_mailbox,          []),
    "github_get_repo_info":  (github_get_repo_info,   ["owner", "repo"]),
    "github_list_repos":     (github_list_repos,      ["user"]),
    "github_read_file":      (github_read_file,       ["owner", "repo", "path", "branch"]),
    "github_list_issues":    (github_list_issues,     ["owner", "repo", "state"]),
    "github_create_issue":   (github_create_issue,    ["owner", "repo", "title", "body"]),
    "github_write_file":     (github_write_file,      ["owner", "repo", "path", "content", "message", "branch"]),
    "github_create_pr":      (github_create_pr,       ["owner", "repo", "title", "head", "base", "body"]),
    "github_create_branch":  (github_create_branch,   ["owner", "repo", "branch", "from_branch"]),
}

# Derive the read-only allowlist from the single source of truth in tools.py so the
# two sets can never drift apart.
_READONLY_ALLOWED_TOOLS = {fn.__name__ for fn in ICARUS_READONLY_TOOLS}

_TOOL_DEFAULTS = {}
_TOOL_SCAN_ORDER = list(_TOOL_REGISTRY.keys())

_COERCE_MAX_LEN = 10_240  # 10 KB guard against DoS via deeply nested structures

def _coerce_param(v: str):
    """Try to parse a raw string parameter value as JSON or a Python literal.
    Falls back to the original stripped string if neither succeeds."""
    v = v.strip()
    if len(v) > _COERCE_MAX_LEN:
        return v
    try:
        return json.loads(v)
    except (json.JSONDecodeError, ValueError):
        pass
    try:
        return ast.literal_eval(v)
    except (ValueError, SyntaxError, RecursionError):
        pass
    return v

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

async def _dispatch_tool_call(raw: str, read_only: bool = False) -> str:
    """Fallback: scan raw model output for any known tool call, execute it,
    and return the result. Handles XML, Python-like, and JSON formats.
    Respects read_only flag for untrusted contexts."""
    text = re.sub(r"```(?:python)?\s*", "", raw)
    text = re.sub(r"\btool_call[\s:]+", "", text).strip()

    # --- XML format ---
    xml_m = re.search(r"<tool_call>\s*<function=(\w+)>(.*?)</function>\s*</tool_call>", text, re.DOTALL)
    if xml_m:
        tool_name = xml_m.group(1)
        inner = xml_m.group(2)
        logger.info(f"[fallback] Detected XML tool_call format: {tool_name}")
        if tool_name in _TOOL_REGISTRY:
            if read_only and tool_name not in _READONLY_ALLOWED_TOOLS:
                logger.info(f"[fallback] Skipping disallowed tool '{tool_name}' in read-only mode.")
            else:
                fn, param_names = _TOOL_REGISTRY[tool_name]
                params = dict(re.findall(r"<parameter=(\w+)>(.*?)</parameter>", inner, re.DOTALL))
                for k, v in _TOOL_DEFAULTS.get(tool_name, {}).items():
                    params.setdefault(k, v)
                known = set(param_names)
                kwargs = {k: _coerce_param(v) for k, v in params.items() if k in known}
                try:
                    if inspect.iscoroutinefunction(fn):
                        result = await fn(**kwargs)
                    else:
                        result = fn(**kwargs)
                    if isinstance(result, list):
                        result = "\n".join(str(x) for x in result)
                    logger.info(f"[fallback] XML path: {tool_name}() succeeded")
                    return result or raw
                except Exception as e:
                    logger.error(f"[fallback] XML path: {tool_name}() failed: {e}")

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

def _load_memory_context() -> str:
    """Read recent memory entries from the persistent memory log."""
    try:
        with open(MEMORY_LOG_PATH, 'r') as f:
            lines = f.readlines()
        recent = lines[-MEMORY_INJECT_LINES:]
        if not recent:
            return ""
        return "[PERSISTENT MEMORY — recent entries]\n" + "".join(recent) + "\n"
    except Exception as e:
        logger.warning(f"[memory] Failed to load memory context: {e}")
        return ""

async def process_message(platform: str, user_id: str, text: str, read_only: bool = False) -> str:
    """Core message processing logic using ADK Runner."""
    runner: Runner = get_readonly_engine() if read_only else get_engine()
    history_id = f"{platform}_{user_id}"

    history = _chat_history.get(history_id, [])
    memory_context = _load_memory_context()
    history_text = "".join(f"User: {u}\nIcarus: {a}\n\n" for u, a in history)
    full_prompt = memory_context + ICARUS_CONTEXT + history_text + f"User: {text}"

    session_id = f"{platform}_{user_id}_{int(time.time())}"

    message = Content(role="user", parts=[Part(text=full_prompt)])
    response_text = ""
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session_id,
        new_message=message
    ):
        if event.is_final_response() and event.content:
            text_parts = [p.text for p in event.content.parts if hasattr(p, 'text') and p.text]
            response_text = text_parts[-1] if text_parts else ""
            break

    if response_text:
        response_text = await _dispatch_tool_call(response_text, read_only=read_only)
        response_text = re.sub(r"<think>.*?</think>", "", response_text, flags=re.DOTALL).strip()
        history.append((text, response_text))
        _chat_history[history_id] = history[-MAX_HISTORY_TURNS:]

    return response_text or "[No response]"

