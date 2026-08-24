"""
local_llm.py — OpenAI-compatible client for the local llama-server.

Talks to the bare-host llama-server serving Qwen3.6-35B-MTP over an
OpenAI-compatible /v1/chat/completions endpoint (llama.cpp server). This is
what llm_router.py routes consultation/scoring/escalation traffic to so none
of it leaves the box — replaces the Gemma/Gemini cloud calls.

Two entry points, mirroring the contract llm_router.py already exposed for
the Google GenAI backend so callers don't need to know which backend answered:
  - local_generate()    — single-shot generation.
  - local_agent_loop()  — multi-turn tool-calling loop.

Reachability: bare-host callers (councilor.py) use the default
http://127.0.0.1:8080/v1 — in-container callers (processor.py, worker
containers) need LOCAL_LLM_URL set to the host-gateway address (see
docker-compose.yml).
"""

import os
import re
import json
import base64
import asyncio
import inspect
import logging
import typing
from typing import Awaitable, Callable

import httpx

logger = logging.getLogger(__name__)

LOCAL_LLM_URL = os.getenv("LOCAL_LLM_URL", "http://127.0.0.1:8080/v1").rstrip("/")
LOCAL_LLM_MODEL = os.getenv("LOCAL_LLM_MODEL", "local")
LOCAL_LLM_TIMEOUT = float(os.getenv("LOCAL_LLM_TIMEOUT", "120"))

# The main model's llama-server was started with --mmproj (see
# start_qwen.sh), so it accepts OpenAI-style image_url content blocks. Two
# safety caps here: image fetches are capped in size (a Discord CDN URL is
# trusted-ish but there's no reason to pull down something huge), and the
# number of images per turn is capped since --parallel 1 means one image-
# heavy turn blocks every other request on the box until it's done.
_IMAGE_DOWNLOAD_TIMEOUT = 20.0
_MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGES_PER_TURN = 4

_THINK_TAG_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def _strip_reasoning(text: str) -> str:
    """Strip <think> blocks — Qwen3.6 emits reasoning separately in
    reasoning_content (already excluded below), but some serving configs
    inline it in content instead. Belt and suspenders."""
    if not text:
        return text
    return _THINK_TAG_RE.sub("", text).strip()


async def _fetch_image_as_data_uri(url: str) -> str | None:
    """Download an image (e.g. a Discord attachment URL) and return it as a
    base64 data: URI for the vision-capable /v1/chat/completions endpoint.
    Returns None on any failure — a bad/oversized/non-image attachment
    should degrade the turn to text-only, not fail it outright."""
    try:
        async with httpx.AsyncClient(timeout=_IMAGE_DOWNLOAD_TIMEOUT) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "").split(";")[0].strip()
            if not content_type.startswith("image/"):
                logger.warning(f"[local_llm] Skipping non-image attachment ({content_type or 'unknown'}): {url}")
                return None
            data = resp.content
            if len(data) > _MAX_IMAGE_BYTES:
                logger.warning(f"[local_llm] Skipping oversized image ({len(data)} bytes): {url}")
                return None
            b64 = base64.b64encode(data).decode("ascii")
            return f"data:{content_type};base64,{b64}"
    except Exception as e:
        logger.warning(f"[local_llm] Failed to fetch image {url}: {e}")
        return None


def _to_openai_messages(
    messages: list[dict],
    system_instruction: str | None,
    image_data_uris: list[str] | None = None,
) -> list[dict]:
    payload = []
    if system_instruction:
        payload.append({"role": "system", "content": system_instruction})
    last_index = len(messages) - 1
    for i, msg in enumerate(messages):
        role = "assistant" if msg["role"] == "model" else msg["role"]
        text = msg["text"]
        if image_data_uris and i == last_index and role == "user":
            content = [{"type": "text", "text": text}]
            content += [{"type": "image_url", "image_url": {"url": uri}} for uri in image_data_uris]
            payload.append({"role": role, "content": content})
        else:
            payload.append({"role": role, "content": text})
    return payload


async def local_generate(
    messages: list[dict],
    system_instruction: str | None = None,
    max_tokens: int = 8192,
    enable_thinking: bool = True,
) -> str:
    """Single-shot chat completion against the local llama-server.

    Args:
        messages: [{"role": "user"|"model", "text": "..."}, ...]
        system_instruction: optional system prompt.
        max_tokens: max output tokens.
        enable_thinking: Qwen3.6 reasons into reasoning_content before content
            by default, which can burn the whole max_tokens budget on
            reasoning and leave no room for the actual answer on short
            budgets. Set False for tasks that don't need it (e.g.
            structured-extraction scoring) — cheaper and faster.
    """
    body = {
        "model": LOCAL_LLM_MODEL,
        "messages": _to_openai_messages(messages, system_instruction),
        "max_tokens": max_tokens,
        "chat_template_kwargs": {"enable_thinking": enable_thinking},
    }

    try:
        async with httpx.AsyncClient(timeout=LOCAL_LLM_TIMEOUT) as client:
            resp = await client.post(f"{LOCAL_LLM_URL}/chat/completions", json=body)
            resp.raise_for_status()
            data = resp.json()
        content = data["choices"][0]["message"].get("content") or ""
        return _strip_reasoning(content) or "(Model returned no text)"
    except Exception as e:
        logger.error(f"[local_llm] generate() failed: {e}")
        return f"Local LLM error: {e}"


# ── JSON schema generation from Python callables ────────────────────────────
# We don't get ADK's or google-genai's automatic function-schema inference
# here, so this is a small best-effort replacement — good enough for the
# plain, simply-typed tool functions in this codebase (str/int/float/bool/
# list-of-str params with docstrings).

_PY_TYPE_MAP = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
}


def _json_type_for(annotation) -> dict:
    origin = typing.get_origin(annotation)
    if origin in (list, typing.List):
        args = typing.get_args(annotation)
        item_schema = _json_type_for(args[0]) if args else {"type": "string"}
        return {"type": "array", "items": item_schema}
    if origin is typing.Union:
        # Optional[X] == Union[X, None] — unwrap to X.
        non_none = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(non_none) == 1:
            return _json_type_for(non_none[0])
    if annotation in _PY_TYPE_MAP:
        return {"type": _PY_TYPE_MAP[annotation]}
    return {"type": "string"}


def _tool_schema(fn: Callable) -> dict:
    """Generate an OpenAI-format tool schema from a function's signature + docstring."""
    sig = inspect.signature(fn)
    try:
        hints = typing.get_type_hints(fn)
    except Exception:
        hints = {}

    properties, required = {}, []
    for name, param in sig.parameters.items():
        if name == "self":
            continue
        properties[name] = _json_type_for(hints.get(name, str))
        if param.default is inspect.Parameter.empty:
            required.append(name)

    description = (inspect.getdoc(fn) or fn.__name__).strip().split("\n\n")[0]

    return {
        "type": "function",
        "function": {
            "name": fn.__name__,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


# ── Human-readable activity descriptions ────────────────────────────────────
# Used by on_activity callbacks (e.g. a Discord status message) to show what
# the model is doing turn-by-turn, instead of the caller seeing nothing until
# the whole loop finishes. Falls back to a generic label for any tool not
# listed here, so new tools never need an update to stay covered.

_ACTIVITY_LABELS: dict[str, Callable[[dict], str]] = {
    "recall": lambda a: f"🔎 Searching your history for “{str(a.get('query', ''))[:60]}”…",
    "consult_councilor": lambda a: "🧠 Consulting the Councilor…",
    "escalate_to_councilor": lambda a: "📤 Escalating to the Councilor…",
    "list_tracked_items": lambda a: "📋 Checking tracked items…",
    "append_memory": lambda a: "🧠 Saving to memory…",
    "check_mailbox": lambda a: "📬 Checking the mailbox…",
    "read_file": lambda a: f"📁 Reading `{a.get('filepath', '')}`…",
    "list_directory": lambda a: f"📁 Listing `{a.get('directory', '')}`…",
    "request_create_file": lambda a: f"📝 Writing `{a.get('filepath', '')}`…",
    "replace_file_contents": lambda a: f"📝 Updating `{a.get('filepath', '')}`…",
    "dispatch_worker_task": lambda a: "⚙️ Dispatching a background task…",
    "get_worker_result": lambda a: "⏳ Waiting on a worker result…",
    "gmail_list_messages": lambda a: "📧 Checking Gmail…",
    "gmail_get_message": lambda a: "📧 Reading an email…",
    "calendar_list_upcoming_events": lambda a: "📅 Checking your calendar…",
    "get_telemetry_snapshot": lambda a: "📊 Checking system telemetry…",
    "github_get_repo_info": lambda a: f"🐙 Checking {a.get('owner', '')}/{a.get('repo', '')}…",
    "github_list_repos": lambda a: "🐙 Listing GitHub repos…",
    "github_read_file": lambda a: f"🐙 Reading `{a.get('path', '')}`…",
    "github_list_issues": lambda a: "🐙 Checking GitHub issues…",
    "github_read_issue": lambda a: "🐙 Reading a GitHub issue…",
    "github_create_issue": lambda a: "🐙 Creating a GitHub issue…",
    "github_create_branch": lambda a: "🐙 Creating a branch…",
    "github_write_file": lambda a: f"🐙 Writing `{a.get('path', '')}`…",
    "github_create_pr": lambda a: "🐙 Opening a PR…",
    "github_clone_repo": lambda a: "🐙 Cloning a repo…",
}


def _describe_activity(fn_name: str, args: dict) -> str:
    formatter = _ACTIVITY_LABELS.get(fn_name)
    if formatter:
        try:
            return formatter(args)
        except Exception:
            pass
    return f"🔧 Using `{fn_name}`…"


async def local_agent_loop(
    initial_prompt: str,
    tools: list[Callable],
    system_instruction: str | None = None,
    max_turns: int = 15,
    context_messages: list[dict] | None = None,
    enable_thinking: bool = True,
    on_activity: Callable[[str], Awaitable[None]] | None = None,
    image_urls: list[str] | None = None,
) -> str:
    """Multi-turn tool-calling loop against the local llama-server.

    Mirrors llm_router.agent_loop()'s signature/contract so the caller
    (councilor.py) doesn't need to know which backend answered.

    on_activity, if given, is awaited with a short human-readable string
    right before each tool call executes (e.g. "🧠 Consulting the
    Councilor…") — lets a caller show live progress (a Discord status
    message, say) instead of going silent until the whole loop finishes.
    Failures in the callback are logged and swallowed; they never interrupt
    the actual tool execution.

    image_urls: image URLs (e.g. Discord attachment CDN links) to attach to
    initial_prompt as vision content blocks. Fetched and base64-encoded here
    since the local llama-server has no way to reach out and fetch a remote
    URL itself. Capped at MAX_IMAGES_PER_TURN; a failed individual fetch is
    dropped rather than failing the whole turn. Requires the model's
    llama-server to have been started with --mmproj (see start_qwen.sh) —
    if it wasn't, the server will error on the image_url content block.
    """
    image_data_uris = None
    if image_urls:
        capped_urls = image_urls[:MAX_IMAGES_PER_TURN]
        if len(image_urls) > MAX_IMAGES_PER_TURN:
            logger.warning(
                f"[local_llm] {len(image_urls)} images attached, only using the first {MAX_IMAGES_PER_TURN}"
            )
        fetched = await asyncio.gather(*(_fetch_image_as_data_uri(u) for u in capped_urls))
        image_data_uris = [uri for uri in fetched if uri] or None

    messages = _to_openai_messages(
        (context_messages or []) + [{"role": "user", "text": initial_prompt}],
        system_instruction,
        image_data_uris=image_data_uris,
    )
    tool_schemas = [_tool_schema(fn) for fn in tools]
    tool_map = {fn.__name__: fn for fn in tools}

    # MTP speculative decoding can crash on requests carrying image content
    # (see start_qwen.sh) — disable it for every turn of a turn-carrying
    # image, since the image stays in `messages` across the whole loop, not
    # just turn 0. NOTE: as of the current llama-server build this override
    # was not observed to actually suppress MTP in a smoke test (draft
    # tokens were still accepted) — kept here as the documented mitigation,
    # but don't treat this as a confirmed fix if a real image ever crashes
    # the server; the fallback is disabling --spec-type on the server itself.
    has_images = bool(image_data_uris)

    async with httpx.AsyncClient(timeout=LOCAL_LLM_TIMEOUT) as client:
        for turn in range(max_turns):
            body = {
                "model": LOCAL_LLM_MODEL,
                "messages": messages,
                "tools": tool_schemas,
                "max_tokens": 8192,
                "chat_template_kwargs": {"enable_thinking": enable_thinking},
            }
            if has_images:
                body["speculative.n_max"] = 0
            try:
                resp = await client.post(f"{LOCAL_LLM_URL}/chat/completions", json=body)
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                logger.error(f"[local_llm] agent_loop turn {turn} failed: {e}")
                return f"Agent loop error on turn {turn}: {e}"

            message = data["choices"][0]["message"]
            tool_calls = message.get("tool_calls") or []

            if not tool_calls:
                final_text = _strip_reasoning(message.get("content") or "")
                logger.info(f"[local_llm] Agent loop completed after {turn + 1} turn(s)")
                return final_text or "(Agent produced no output)"

            messages.append({
                "role": "assistant",
                "content": message.get("content") or "",
                "tool_calls": tool_calls,
            })

            for call in tool_calls:
                fn_name = call["function"]["name"]
                fn = tool_map.get(fn_name)
                try:
                    args = json.loads(call["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}

                if on_activity:
                    try:
                        await on_activity(_describe_activity(fn_name, args))
                    except Exception as e:
                        logger.warning(f"[local_llm] on_activity callback failed: {e}")

                if fn is None:
                    logger.warning(f"[local_llm] Model requested unknown tool: {fn_name}")
                    result_text = json.dumps({"error": f"Unknown tool: {fn_name}"})
                else:
                    try:
                        logger.info(f"[local_llm] Executing tool: {fn_name}({list(args.keys())})")
                        result = fn(**args)
                        if inspect.isawaitable(result):
                            result = await result
                        result_text = result if isinstance(result, str) else json.dumps(result)
                    except Exception as e:
                        logger.error(f"[local_llm] Tool {fn_name} failed: {e}")
                        result_text = json.dumps({"error": str(e)})

                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", fn_name),
                    "content": result_text,
                })
            logger.info(f"[local_llm] Turn {turn}: executed {len(tool_calls)} tool call(s)")

    logger.warning(f"[local_llm] Agent loop hit max turns ({max_turns})")
    return "(Agent loop exhausted without final response)"


async def local_embed(text: str) -> list[float] | None:
    """Embed a string via the local embedding endpoint. Returns None if no
    embedding-capable server is configured/reachable — callers must degrade
    to FTS-only search rather than error out.

    Requires a server started with --embeddings (a separate lightweight
    instance from the chat-completion server, which does not serve
    embeddings). Set LOCAL_EMBEDDING_URL to point at it once one exists.
    """
    embedding_url = os.getenv("LOCAL_EMBEDDING_URL", "").rstrip("/")
    if not embedding_url:
        return None
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{embedding_url}/v1/embeddings", json={"input": text})
            resp.raise_for_status()
            data = resp.json()
        return data["data"][0]["embedding"]
    except Exception as e:
        logger.warning(f"[local_llm] embed() failed, degrading to FTS-only: {e}")
        return None
