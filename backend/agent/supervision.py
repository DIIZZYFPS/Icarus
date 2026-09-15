"""
supervision.py — Step-level supervision of a subagent's tool use.

local_llm.local_agent_loop() accepts an optional `supervisor` hook. When a
subagent's model produces a tool call that can't be executed as written —
arguments that aren't valid JSON, a tool name that doesn't exist, or a tool
that raised — the loop hands the failure to the hook BEFORE it's serialized
into the transcript as plain text for the subagent's own model to flail at.
The hook answers with a verdict (repair / redirect / retry / note /
give_up) or None ("do what you always did").

Supervisor is the policy behind that hook, in two layers:

  1. Deterministic, free: the usual small-model slips — code fences around
     the JSON, a trailing comma, Python-style quotes/booleans, prose around
     the object, a near-miss tool name — are fixed locally, no model call.
  2. A `consult`-shaped call to the local model, task_type "supervision",
     for what layer 1 can't fix. This is the Councilor supervising itself,
     not a new model tier. It is rate-limited two ways: execution errors
     only reach it after SUPERVISION_FAILURE_THRESHOLD consecutive failures
     of the same tool (mirroring WorkerBase's retry pattern — a one-off
     transient error isn't worth a model round-trip), and a single loop run
     gets at most SUPERVISION_MAX_CONSULTS of them.

Boundary (see icarus_overhaul.md §2): this is only ever passed into loops
that run *subagents* — councilor.process_delegation and
worker_subagent.PersistentSubagent. engine.run_icarus (L1's own loop) never
gets a supervisor; that's enforced by construction, not configuration.
"""

import os
import re
import ast
import json
import difflib
import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

SUPERVISION_FAILURE_THRESHOLD = int(os.getenv("SUPERVISION_FAILURE_THRESHOLD", "2"))
SUPERVISION_MAX_CONSULTS = int(os.getenv("SUPERVISION_MAX_CONSULTS", "6"))

ACTION_REPAIR = "repair"
ACTION_REDIRECT = "redirect"
ACTION_RETRY = "retry"
ACTION_NOTE = "note"
ACTION_GIVE_UP = "give_up"

_FENCE_RE = re.compile(r"^\s*```(?:json|python)?\s*|\s*```\s*$", re.IGNORECASE)
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")
_SMART_QUOTES = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'"})


# ── Layer 1: deterministic repairs ───────────────────────────────────────────

def _outermost_object(text: str) -> str | None:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    return text[start:end + 1]


def _matches_schema(args: dict, schema: dict | None) -> bool:
    if not schema:
        return True
    params = schema.get("function", schema).get("parameters", {}) if isinstance(schema, dict) else {}
    properties = params.get("properties") or {}
    required = params.get("required") or []
    if properties and any(k not in properties for k in args):
        return False
    return all(r in args for r in required)


def repair_json_args(raw: str | None, schema: dict | None = None) -> dict | None:
    """Try the cheap fixes. Returns the repaired dict, or None if nothing
    parses into an object that fits the tool's schema."""
    if not raw or not isinstance(raw, str):
        return None
    text = _FENCE_RE.sub("", raw.strip()).translate(_SMART_QUOTES)
    candidates = [text]
    inner = _outermost_object(text)
    if inner and inner != text:
        candidates.append(inner)
    candidates += [_TRAILING_COMMA_RE.sub(r"\1", c) for c in list(candidates)]

    for candidate in candidates:
        for parser in (json.loads, ast.literal_eval):
            try:
                value = parser(candidate)
            except Exception:
                continue
            if isinstance(value, dict) and all(isinstance(k, str) for k in value) and _matches_schema(value, schema):
                return value
    return None


def closest_tool(name: str, available: list[str], cutoff: float = 0.6) -> str | None:
    if not name or not available:
        return None
    lowered = {a.lower(): a for a in available}
    if name.lower() in lowered:
        return lowered[name.lower()]
    matches = difflib.get_close_matches(name, list(available), n=1, cutoff=cutoff)
    return matches[0] if matches else None


# ── Layer 2: the consult ─────────────────────────────────────────────────────

SUPERVISOR_SYSTEM_PROMPT = """You are The Councilor, supervising a subagent's tool use for Project Icarus.
The subagent made one tool call that could not be executed as written. Decide
what should happen next and answer with ONE JSON object and nothing else — no
prose, no code fences.

Allowed shapes (use exactly one; "args" must match the tool's parameter schema):
  {"action": "repair",   "args": {...}}                       — run the same tool with these corrected arguments
  {"action": "redirect", "tool": "<existing tool>", "args": {...}} — the subagent meant a different tool
  {"action": "retry",    "args": {...} or null}               — try the same call again (optionally with fixed args)
  {"action": "note",     "note": "<one sentence>"}            — let the error stand, but attach this guidance for the subagent
  {"action": "give_up",  "reason": "<one sentence>"}          — this task cannot proceed; stop it

Only "redirect" to a tool in the available list. Prefer the least drastic
action that plausibly works. Give up only when the failure is structural
(missing credentials, the task needs something no available tool can do).
"""


def _format_event(event: dict, intent: str) -> str:
    kind = event.get("kind")
    lines = [f"Task the subagent is working on:\n{intent[:1200]}", ""]
    lines.append(f"Failure kind: {kind}")
    lines.append(f"Tool called: {event.get('tool')}")
    schema = event.get("schema")
    if schema:
        params = schema.get("function", schema).get("parameters") if isinstance(schema, dict) else None
        lines.append(f"Tool parameter schema: {json.dumps(params or schema)[:1500]}")
    if kind == "malformed_args":
        lines.append(f"Raw arguments the subagent produced (not valid JSON): {str(event.get('raw_arguments'))[:1500]}")
    else:
        lines.append(f"Arguments: {json.dumps(event.get('args') or {})[:1500]}")
    if kind == "exec_error":
        lines.append(f"Error raised: {str(event.get('error'))[:1500]}")
        lines.append(f"Consecutive failures of this tool: {event.get('consecutive_failures')}")
    lines.append(f"Available tools: {', '.join(event.get('available_tools') or [])}")
    lines.append(f"Allowed actions here: {', '.join(sorted(event.get('_allowed') or []))}")
    lines.append("")
    lines.append("Answer with the JSON object only.")
    return "\n".join(lines)


def parse_verdict(text: str | None, allowed: set[str], available_tools: list[str] | None = None) -> dict | None:
    """Turn the model's reply into a validated verdict dict, or None."""
    if not text:
        return None
    candidate = _FENCE_RE.sub("", text.strip())
    obj = _outermost_object(candidate)
    if obj is None:
        return None
    try:
        verdict = json.loads(_TRAILING_COMMA_RE.sub(r"\1", obj))
    except Exception:
        return None
    if not isinstance(verdict, dict):
        return None
    action = verdict.get("action")
    if action not in allowed:
        return None
    if action in (ACTION_REPAIR, ACTION_REDIRECT) and not isinstance(verdict.get("args"), dict):
        if action == ACTION_REPAIR:
            return None
        verdict["args"] = {}
    if action == ACTION_RETRY and verdict.get("args") is not None and not isinstance(verdict.get("args"), dict):
        verdict["args"] = None
    if action == ACTION_REDIRECT:
        tool = verdict.get("tool")
        if available_tools is not None and tool not in available_tools:
            return None
    if action == ACTION_NOTE and not isinstance(verdict.get("note"), str):
        return None
    if action == ACTION_GIVE_UP and not isinstance(verdict.get("reason"), str):
        verdict["reason"] = "supervisor gave up"
    return verdict


class Supervisor:
    """The hook object passed as `supervisor=` into a subagent's agent loop.
    One instance per task — it carries the consult budget and a decision
    log for observability."""

    def __init__(
        self,
        task_id: str,
        intent: str,
        *,
        generate: Callable[..., Awaitable[str]] | None = None,
        failure_threshold: int = SUPERVISION_FAILURE_THRESHOLD,
        max_consults: int = SUPERVISION_MAX_CONSULTS,
    ):
        self.task_id = task_id
        self.intent = intent or ""
        self._generate = generate
        self.failure_threshold = max(1, int(failure_threshold))
        self.max_consults = max(0, int(max_consults))
        self.consults = 0
        self.decisions: list[dict] = []

    async def __call__(self, event: dict) -> dict | None:
        kind = event.get("kind")
        try:
            if kind == "malformed_args":
                verdict = await self._malformed(event)
            elif kind == "unknown_tool":
                verdict = await self._unknown(event)
            elif kind == "exec_error":
                verdict = await self._exec_error(event)
            else:
                verdict = None
        except Exception as e:  # a supervisor bug must never take the subagent down
            logger.warning(f"[supervisor {self.task_id}] hook failed on {kind}: {e}")
            verdict = None
        self.decisions.append({"kind": kind, "tool": event.get("tool"), "verdict": verdict})
        if verdict:
            logger.info(f"[supervisor {self.task_id}] {kind} on {event.get('tool')} -> {verdict.get('action')}")
        return verdict

    # ── per-kind policy ─────────────────────────────────────────────────

    async def _malformed(self, event: dict) -> dict | None:
        repaired = repair_json_args(event.get("raw_arguments"), event.get("schema"))
        if repaired is not None:
            return {"action": ACTION_REPAIR, "args": repaired, "source": "deterministic"}
        return await self._consult(event, {ACTION_REPAIR, ACTION_GIVE_UP})

    async def _unknown(self, event: dict) -> dict | None:
        available = list(event.get("available_tools") or [])
        match = closest_tool(event.get("tool", ""), available)
        if match:
            return {"action": ACTION_REDIRECT, "tool": match, "args": dict(event.get("args") or {}), "source": "deterministic"}
        return await self._consult(event, {ACTION_REDIRECT, ACTION_GIVE_UP})

    async def _exec_error(self, event: dict) -> dict | None:
        if int(event.get("consecutive_failures") or 1) < self.failure_threshold:
            return None   # let the subagent's own model react to the first slip
        return await self._consult(event, {ACTION_RETRY, ACTION_REDIRECT, ACTION_GIVE_UP, ACTION_NOTE})

    async def _consult(self, event: dict, allowed: set[str]) -> dict | None:
        if self.consults >= self.max_consults:
            logger.warning(f"[supervisor {self.task_id}] consult budget exhausted ({self.max_consults}); falling back to default handling")
            return None
        self.consults += 1
        generate = self._generate
        if generate is None:
            from backend.agent.llm_router import generate as router_generate
            generate = router_generate
        prompt = _format_event({**event, "_allowed": allowed}, self.intent)
        text = await generate(
            task_type="supervision",
            messages=[{"role": "user", "text": prompt}],
            system_instruction=SUPERVISOR_SYSTEM_PROMPT,
            max_tokens=600,
        )
        verdict = parse_verdict(text, allowed, list(event.get("available_tools") or []))
        if verdict is None:
            logger.info(f"[supervisor {self.task_id}] consult produced no usable verdict: {str(text)[:160]!r}")
        else:
            verdict["source"] = "consult"
        return verdict


# ── Phase 5: ask_supervisor — scoping clarification with pause / resume ──────
# Not an error path. A subagent that hits real ambiguity — a decision the
# brief doesn't settle and only the operator can make — asks instead of
# guessing. The Councilor answers from context when it can (same shape as
# consult_councilor); otherwise the task pauses, the operator is asked via
# the usual notification channel, and the reply routes back to this one
# task through backend/agent/subagent_resume.py. A pause has a timeout: an
# indefinitely paused subagent is a stuck subagent, so on timeout the tool
# raises AgentAbort and the task fails cleanly back to L1.

ASK_SUPERVISOR_SYSTEM_PROMPT = """You are The Councilor answering a scoping question from a subagent working on a task for Project Icarus.
Answer ONLY if the task brief plus ordinary good judgment settle it — keep it to
a few sentences the subagent can act on immediately.
If the decision is genuinely the operator's — spending money, sending anything
on their behalf, deleting or changing their data, choosing between options only
they can weigh, or anything the brief doesn't cover and can't reasonably be
inferred — reply with exactly the single word:
NEED_OPERATOR
"""


def make_ask_supervisor(
    *,
    task_id: str,
    intent: str,
    platform: str | None,
    chat_id: str | None,
    redis_getter: Callable[[], Awaitable[Any]],
    notify: Callable[[str | None, str | None, str], Any],
    generate: Callable[..., Awaitable[str]] | None = None,
    registry=None,
    timeout_seconds: int | None = None,
    memory_context: str = "",
    on_pause_state: Callable[[str, str | None], Awaitable[None]] | None = None,
):
    """Build the ask_supervisor tool bound to one subagent.

    redis_getter: async -> redis client (shared with L1, where replies land).
    notify: sync (platform, chat_id, text) -> NotifyResult-like with
        .message_ids / .target_id / .platform (None tolerated).
    registry: the Councilor's SubagentRegistry, or None for a worker that
        must not write it (single-writer rule) — on_pause_state is its
        alternative for surfacing paused/running state."""
    from backend.agent.subagent_resume import PAUSE_TIMEOUT_SECONDS, record_pause, clear_pause, wait_for_resume

    timeout = int(timeout_seconds if timeout_seconds is not None else PAUSE_TIMEOUT_SECONDS)

    async def _state(state: str, question: str | None) -> None:
        if registry is not None:
            try:
                if state == "paused":
                    await registry.set_status(task_id, "paused", paused_question=(question or "")[:2000])
                elif state == "running":
                    await registry.set_status(task_id, "running", paused_question=None)
            except Exception as e:
                logger.warning(f"[ask_supervisor {task_id}] registry update failed: {e}")
        if on_pause_state is not None:
            try:
                await on_pause_state(state, question)
            except Exception as e:
                logger.warning(f"[ask_supervisor {task_id}] pause-state callback failed: {e}")

    async def _activity(event_type: str, action: str, detail: str, severity: str = "info") -> None:
        try:
            from backend.agent.activity_repo import publish_activity
            await publish_activity(
                actor="councilor", event_type=event_type, action=action, detail=detail,
                thread_id=f"sub-{task_id}", platform=platform, user_id=None, severity=severity,
            )
        except Exception:
            pass

    async def ask_supervisor(question: str) -> str:
        """Ask your supervisor (the Councilor) when the task brief leaves a real decision open — not for facts your tools can find. Blocks until answered: the Councilor answers from context when it can; otherwise it asks the operator and pauses you until they reply (or a timeout ends the task). Ask one precise question."""
        import asyncio
        from backend.agent.local_llm import AgentAbort

        question = (question or "").strip()
        if not question:
            return "Ask a specific question — what decision do you need made?"

        # 1. The Councilor's own judgment first.
        gen = generate
        if gen is None:
            from backend.agent.llm_router import generate as router_generate
            gen = router_generate
        prompt = f"Task brief:\n{intent}\n\n"
        if memory_context:
            prompt += memory_context + "\n"
        prompt += f"Subagent's question:\n{question}"
        try:
            answer = await gen(
                task_type="supervision",
                messages=[{"role": "user", "text": prompt}],
                system_instruction=ASK_SUPERVISOR_SYSTEM_PROMPT,
                max_tokens=400,
            )
        except Exception as e:
            logger.warning(f"[ask_supervisor {task_id}] consult failed, relaying to operator: {e}")
            answer = ""
        answer = (answer or "").strip()
        if answer and "NEED_OPERATOR" not in answer.upper() and not answer.startswith("Local LLM error"):
            logger.info(f"[ask_supervisor {task_id}] answered from context")
            return f"Supervisor's answer: {answer}"

        # 2. Pause and relay to the operator.
        redis = await redis_getter()
        await _state("paused", question)
        text = (
            f"[Subagent {task_id} needs a decision]\n\n{question}\n\n"
            f"Reply to this message — or send `{task_id}: <your answer>` — to resume it. "
            f"It gives up after {max(1, timeout // 60)} min without an answer."
        )
        result = await asyncio.to_thread(notify, platform, chat_id, text)
        message_ids = [str(m) for m in (getattr(result, "message_ids", None) or [])]
        target_platform = getattr(result, "platform", None) or platform
        target_chat = getattr(result, "target_id", None) or chat_id
        if not message_ids:
            logger.warning(f"[ask_supervisor {task_id}] pause notice returned no message ids — only the explicit `{task_id}:` reply form will resume it")
        await record_pause(
            redis, task_id=task_id, question=question, platform=target_platform, chat_id=target_chat,
            delivery_message_ids=message_ids, timeout_seconds=timeout,
        )
        await _activity("paused", "paused — waiting on the operator", question, severity="warning")
        logger.info(f"[ask_supervisor {task_id}] paused, waiting up to {timeout}s for the operator")

        reply = await wait_for_resume(redis, task_id, timeout)
        await clear_pause(redis, task_id)
        if reply is None:
            await _state("timed_out", question)
            await _activity("failed", "pause timed out — no operator reply", question, severity="critical")
            raise AgentAbort(
                f"paused for an operator decision and none arrived within {timeout}s — the question was: {question[:200]}"
            )
        await _state("running", None)
        operator_answer = str(reply.get("answer") or "").strip() or "(empty reply)"
        await _activity("resumed", "resumed — operator answered", operator_answer)
        logger.info(f"[ask_supervisor {task_id}] resumed with the operator's answer")
        return f"Operator's answer: {operator_answer}"

    return ask_supervisor
