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
