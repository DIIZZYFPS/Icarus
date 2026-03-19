"""
llm_router.py — Tiered model routing for Icarus.

Routes tasks to different LLM backends based on task type:
  - "consultation" / "scoring" → Gemma 3 27B (free tier)
  - "escalation"               → Gemini Flash (paid, high-quality coding)

Provides two modes:
  - generate()    — single-shot generation (consultations, scoring)
  - agent_loop()  — multi-turn tool-calling loop (escalations)
"""

import os
import logging
from typing import Callable

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

# ── Model routing table ─────────────────────────────────────────────────────
ROUTING_TABLE = {
    "consultation": "gemma-3-27b-it",
    "scoring":      "gemma-3-27b-it",
    "escalation":   "gemini-3-flash",
}

DEFAULT_MODEL = "gemma-3-27b-it"

# Models that do NOT support the system_instruction API parameter.
# For these, we prepend system instructions as a user message instead.
_NO_SYSTEM_INSTRUCTION = {"gemma-3-27b-it", "gemma-3-12b-it", "gemma-3-4b-it"}

# ── Client singleton ────────────────────────────────────────────────────────
_client: genai.Client | None = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        api_key = os.getenv("GOOGLE_API_KEY")
        if not api_key:
            raise RuntimeError(
                "GOOGLE_API_KEY is not set. Get one from https://aistudio.google.com/apikey"
            )
        _client = genai.Client(api_key=api_key)
        logger.info("[llm_router] Initialized google-genai client")
    return _client


def _resolve_model(task_type: str) -> str:
    model = ROUTING_TABLE.get(task_type, DEFAULT_MODEL)
    logger.info(f"[llm_router] Routing task_type={task_type!r} → model={model!r}")
    return model


# ── Single-shot generation ──────────────────────────────────────────────────

async def generate(
    task_type: str,
    messages: list[dict],
    system_instruction: str | None = None,
    max_tokens: int = 8192,
) -> str:
    """Single-shot generation. No tool calling, just prompt → response.

    Args:
        task_type: Key into ROUTING_TABLE (e.g., "consultation", "scoring").
        messages: List of {"role": "user"|"model", "text": "..."} dicts.
        system_instruction: Optional system prompt.
        max_tokens: Maximum output tokens.

    Returns:
        The model's text response.
    """
    client = _get_client()
    model = _resolve_model(task_type)

    contents = []

    # Gemma models don't support system_instruction — prepend as a user message
    if system_instruction and model in _NO_SYSTEM_INSTRUCTION:
        contents.append(
            types.Content(
                role="user",
                parts=[types.Part.from_text(text=f"[SYSTEM INSTRUCTIONS]\n{system_instruction}")],
            )
        )
        contents.append(
            types.Content(
                role="model",
                parts=[types.Part.from_text(text="Understood. I will follow these instructions.")],
            )
        )
        effective_system = None
    else:
        effective_system = system_instruction

    contents.extend([
        types.Content(
            role=msg["role"],
            parts=[types.Part.from_text(text=msg["text"])],
        )
        for msg in messages
    ])

    config = types.GenerateContentConfig(
        max_output_tokens=max_tokens,
        system_instruction=effective_system,
    )

    try:
        response = client.models.generate_content(
            model=model,
            contents=contents,
            config=config,
        )
        return response.text or "(Model returned no text)"
    except Exception as e:
        logger.error(f"[llm_router] generate() failed for {model}: {e}")
        return f"LLM routing error: {e}"


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

    Args:
        task_type: Key into ROUTING_TABLE.
        initial_prompt: The initial user prompt.
        tools: List of Python callables to expose as tools.
        system_instruction: Optional system prompt.
        max_turns: Maximum number of tool-call rounds.
        context_messages: Optional prior conversation context.

    Returns:
        The model's final text response.
    """
    client = _get_client()
    model = _resolve_model(task_type)

    # Build initial contents
    contents: list[types.Content] = []

    # Gemma models don't support system_instruction — prepend as a user message
    if system_instruction and model in _NO_SYSTEM_INSTRUCTION:
        contents.append(
            types.Content(
                role="user",
                parts=[types.Part.from_text(text=f"[SYSTEM INSTRUCTIONS]\n{system_instruction}")],
            )
        )
        contents.append(
            types.Content(
                role="model",
                parts=[types.Part.from_text(text="Understood. I will follow these instructions.")],
            )
        )
        effective_system = None
    else:
        effective_system = system_instruction

    if context_messages:
        for msg in context_messages:
            contents.append(
                types.Content(
                    role=msg["role"],
                    parts=[types.Part.from_text(text=msg["text"])],
                )
            )
    contents.append(
        types.Content(
            role="user",
            parts=[types.Part.from_text(text=initial_prompt)],
        )
    )

    config = types.GenerateContentConfig(
        tools=tools,
        system_instruction=effective_system,
        max_output_tokens=8192,
    )

    for turn in range(max_turns):
        try:
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )
        except Exception as e:
            logger.error(f"[llm_router] agent_loop turn {turn} failed: {e}")
            return f"Agent loop error on turn {turn}: {e}"

        # Check if the model wants to call functions
        if response.function_calls:
            # Execute each function call
            tool_map = {fn.__name__: fn for fn in tools}
            function_responses = []

            for fc in response.function_calls:
                fn = tool_map.get(fc.name)
                if fn is None:
                    logger.warning(f"[llm_router] Model requested unknown tool: {fc.name}")
                    function_responses.append(
                        types.Part.from_function_response(
                            name=fc.name,
                            response={"error": f"Unknown tool: {fc.name}"},
                        )
                    )
                    continue

                try:
                    args = dict(fc.args) if fc.args else {}
                    logger.info(f"[llm_router] Executing tool: {fc.name}({list(args.keys())})")
                    result = fn(**args)
                    # Ensure result is a dict for function_response
                    if isinstance(result, str):
                        result = {"result": result}
                    elif not isinstance(result, dict):
                        result = {"result": str(result)}
                    function_responses.append(
                        types.Part.from_function_response(
                            name=fc.name,
                            response=result,
                        )
                    )
                except Exception as e:
                    logger.error(f"[llm_router] Tool {fc.name} failed: {e}")
                    function_responses.append(
                        types.Part.from_function_response(
                            name=fc.name,
                            response={"error": str(e)},
                        )
                    )

            # Append model's response (with function calls) and our function results
            contents.append(response.candidates[0].content)
            contents.append(
                types.Content(role="user", parts=function_responses)
            )
            logger.info(f"[llm_router] Turn {turn}: executed {len(function_responses)} tool call(s)")
        else:
            # No function calls — this is the final response
            final_text = response.text or "(Agent produced no output)"
            logger.info(f"[llm_router] Agent loop completed after {turn + 1} turn(s)")
            return final_text

    logger.warning(f"[llm_router] Agent loop hit max turns ({max_turns})")
    # Try to extract any text from the last response
    try:
        return response.text or "(Agent loop exhausted without final response)"
    except Exception:
        return "(Agent loop exhausted without final response)"
