"""
llm_router.py — Tiered model routing for Icarus.

Every task type routes to the local llama-server by default — consultation,
scoring (email triage), and escalation (code changes) never leave the box.
This replaces the old Gemma 27B / Gemini Flash cloud routing: those were the
two live privacy leaks (silent per-email classification, and occasional but
real code-content exposure on escalation) that motivated the move to local.

ROUTING_TABLE still exists as an explicit policy table rather than a hardcode
so a future "cloud" overflow tier — for tasks that genuinely exceed local
capability — has a documented place to plug in per task_type, without
touching call sites. The Google GenAI implementation is kept below as
_cloud_generate()/_cloud_agent_loop(), dormant, not wired into the table.
If it's ever reintroduced, it must stay an explicit, logged, last-resort
path — never a silent default.

Provides two modes:
  - generate()    — single-shot generation (consultations, scoring)
  - agent_loop()  — multi-turn tool-calling loop (escalations)
"""

import os
import logging
from typing import Callable

from backend.agent.local_llm import local_generate, local_agent_loop

logger = logging.getLogger(__name__)

# ── Model routing table ─────────────────────────────────────────────────────
# task_type -> tier. "local" is the only tier wired up today.
ROUTING_TABLE = {
    "consultation": "local",
    "scoring":      "local",
    "escalation":   "local",
}

DEFAULT_TIER = "local"


def _resolve_tier(task_type: str) -> str:
    tier = ROUTING_TABLE.get(task_type, DEFAULT_TIER)
    logger.info(f"[llm_router] Routing task_type={task_type!r} -> tier={tier!r}")
    return tier


# ── Single-shot generation ──────────────────────────────────────────────────

async def generate(
    task_type: str,
    messages: list[dict],
    system_instruction: str | None = None,
    max_tokens: int = 8192,
) -> str:
    """Single-shot generation. No tool calling, just prompt -> response.

    Args:
        task_type: Key into ROUTING_TABLE (e.g., "consultation", "scoring").
        messages: List of {"role": "user"|"model", "text": "..."} dicts.
        system_instruction: Optional system prompt.
        max_tokens: Maximum output tokens.

    Returns:
        The model's text response.
    """
    tier = _resolve_tier(task_type)
    if tier == "cloud":
        return await _cloud_generate(task_type, messages, system_instruction, max_tokens)
    # Scoring (structured extraction, e.g. email triage) doesn't benefit from
    # reasoning and pays for it in latency/tokens — keep thinking on elsewhere.
    enable_thinking = task_type != "scoring"
    return await local_generate(
        messages, system_instruction=system_instruction, max_tokens=max_tokens,
        enable_thinking=enable_thinking,
    )


# ── Multi-turn agent loop with tool calling ─────────────────────────────────

async def agent_loop(
    task_type: str,
    initial_prompt: str,
    tools: list[Callable],
    system_instruction: str | None = None,
    max_turns: int = 15,
    context_messages: list[dict] | None = None,
) -> str:
    """Multi-turn agent loop with function calling.

    The model can call tools, receive results, and iterate until it produces
    a final text response or hits the turn limit.
    """
    tier = _resolve_tier(task_type)
    if tier == "cloud":
        return await _cloud_agent_loop(
            task_type, initial_prompt, tools, system_instruction, max_turns, context_messages
        )
    return await local_agent_loop(
        initial_prompt, tools,
        system_instruction=system_instruction,
        max_turns=max_turns,
        context_messages=context_messages,
    )


# ── Dormant cloud overflow path (Google GenAI) ──────────────────────────────
# Not wired into ROUTING_TABLE. Kept so a deliberate, visible, last-resort
# overflow tier can be reintroduced later without rebuilding this from
# scratch — see the task-routing discussion in project history. If you wire
# this back in, keep it opt-in per task and make sure the escalation happens
# somewhere the operator actually sees it (memory/notification), not silently.

_CLOUD_ROUTING_TABLE = {
    "consultation": "gemma-3-27b-it",
    "scoring":      "gemma-3-27b-it",
    "escalation":   "gemini-3.1-flash-lite-preview",  # stale — verify current model names before reuse
}
_CLOUD_DEFAULT_MODEL = "gemma-3-27b-it"
_NO_SYSTEM_INSTRUCTION = {"gemma-3-27b-it", "gemma-3-12b-it", "gemma-3-4b-it"}

_cloud_client = None


def _get_cloud_client():
    global _cloud_client
    if _cloud_client is None:
        from google import genai
        api_key = os.getenv("GOOGLE_API_KEY")
        if not api_key:
            raise RuntimeError(
                "GOOGLE_API_KEY is not set. Get one from https://aistudio.google.com/apikey"
            )
        _cloud_client = genai.Client(api_key=api_key)
        logger.info("[llm_router] Initialized google-genai client (cloud overflow)")
    return _cloud_client


async def _cloud_generate(
    task_type: str,
    messages: list[dict],
    system_instruction: str | None,
    max_tokens: int,
) -> str:
    from google.genai import types

    client = _get_cloud_client()
    model = _CLOUD_ROUTING_TABLE.get(task_type, _CLOUD_DEFAULT_MODEL)
    logger.warning(f"[llm_router] CLOUD OVERFLOW: task_type={task_type!r} -> model={model!r}")

    contents = []
    if system_instruction and model in _NO_SYSTEM_INSTRUCTION:
        contents.append(types.Content(role="user", parts=[types.Part.from_text(text=f"[SYSTEM INSTRUCTIONS]\n{system_instruction}")]))
        contents.append(types.Content(role="model", parts=[types.Part.from_text(text="Understood. I will follow these instructions.")]))
        effective_system = None
    else:
        effective_system = system_instruction

    contents.extend([
        types.Content(role=msg["role"], parts=[types.Part.from_text(text=msg["text"])])
        for msg in messages
    ])

    config = types.GenerateContentConfig(max_output_tokens=max_tokens, system_instruction=effective_system)

    try:
        response = client.models.generate_content(model=model, contents=contents, config=config)
        return response.text or "(Model returned no text)"
    except Exception as e:
        logger.error(f"[llm_router] cloud generate() failed for {model}: {e}")
        return f"LLM routing error: {e}"


async def _cloud_agent_loop(
    task_type: str,
    initial_prompt: str,
    tools: list[Callable],
    system_instruction: str | None,
    max_turns: int,
    context_messages: list[dict] | None,
) -> str:
    from google.genai import types

    client = _get_cloud_client()
    model = _CLOUD_ROUTING_TABLE.get(task_type, _CLOUD_DEFAULT_MODEL)
    logger.warning(f"[llm_router] CLOUD OVERFLOW: task_type={task_type!r} -> model={model!r}")

    contents: list[types.Content] = []
    if system_instruction and model in _NO_SYSTEM_INSTRUCTION:
        contents.append(types.Content(role="user", parts=[types.Part.from_text(text=f"[SYSTEM INSTRUCTIONS]\n{system_instruction}")]))
        contents.append(types.Content(role="model", parts=[types.Part.from_text(text="Understood. I will follow these instructions.")]))
        effective_system = None
    else:
        effective_system = system_instruction

    if context_messages:
        for msg in context_messages:
            contents.append(types.Content(role=msg["role"], parts=[types.Part.from_text(text=msg["text"])]))
    contents.append(types.Content(role="user", parts=[types.Part.from_text(text=initial_prompt)]))

    config = types.GenerateContentConfig(tools=tools, system_instruction=effective_system, max_output_tokens=8192)

    for turn in range(max_turns):
        try:
            response = client.models.generate_content(model=model, contents=contents, config=config)
        except Exception as e:
            logger.error(f"[llm_router] cloud agent_loop turn {turn} failed: {e}")
            return f"Agent loop error on turn {turn}: {e}"

        if response.function_calls:
            tool_map = {fn.__name__: fn for fn in tools}
            function_responses = []
            for fc in response.function_calls:
                fn = tool_map.get(fc.name)
                if fn is None:
                    function_responses.append(types.Part.from_function_response(name=fc.name, response={"error": f"Unknown tool: {fc.name}"}))
                    continue
                try:
                    args = dict(fc.args) if fc.args else {}
                    result = fn(**args)
                    if isinstance(result, str):
                        result = {"result": result}
                    elif not isinstance(result, dict):
                        result = {"result": str(result)}
                    function_responses.append(types.Part.from_function_response(name=fc.name, response=result))
                except Exception as e:
                    function_responses.append(types.Part.from_function_response(name=fc.name, response={"error": str(e)}))

            contents.append(response.candidates[0].content)
            contents.append(types.Content(role="user", parts=function_responses))
        else:
            return response.text or "(Agent produced no output)"

    try:
        return response.text or "(Agent loop exhausted without final response)"
    except Exception:
        return "(Agent loop exhausted without final response)"
