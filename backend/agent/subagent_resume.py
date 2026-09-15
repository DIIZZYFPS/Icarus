"""
subagent_resume.py — The pause/resume primitive behind ask_supervisor.

Before this, a Councilor-to-operator message was fire-and-forget: the
operator's reply just became L1 conversation. A paused subagent needs the
reply to come back to *it* — the one specific task that asked — which is
a routing primitive that didn't exist. It lives in Redis, which both sides
already share: the Councilor (host) and the persistent workers write pause
records and wait; L1 (icarus-api) resolves an incoming operator message
against them BEFORE its own model ever sees it, and pushes the answer.

Keys (all short-lived — a pause has a timeout, see SUBAGENT_PAUSE_TIMEOUT_SECONDS):
  icarus:subagent:pause:<task_id>                 hash — the open question, where it was
                                                  delivered, and the delivered message ids
  icarus:subagent:pause_index:<platform>:<chat_id> list — task ids paused into that conversation
  icarus:subagent:resume:<task_id>                list — the answer, BLPOP'd by the waiter

Two ways a reply resolves, both unambiguous:
  1. An explicit reference: a message starting with `sub-…: <answer>`
     (or `resume sub-…: …`). Works from any private conversation, on any
     platform, whether or not the client supports reply threading.
  2. A platform "reply to" the delivered message (Discord message
     reference, Telegram reply_to_message) — matched by delivery id within
     the same conversation, extending the notification_repo reply pattern.
Nothing else resumes a task: ordinary chat in the same conversation while a
subagent is paused is still ordinary chat.
"""

import os
import re
import json
import time
import asyncio
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

PAUSE_TIMEOUT_SECONDS = int(os.getenv("SUBAGENT_PAUSE_TIMEOUT_SECONDS", "1800"))
_INDEX_MAX = 20
_BLPOP_SLICE_SECONDS = 2   # short blocking reads so a socket timeout / cancellation can't wedge the waiter

_IDENTITY_PREFIX_RE = re.compile(r"^\s*\[User:[^\]]*\]:\s*")
TASK_REF_RE = re.compile(r"^\s*(?:resume\s+)?(sub-\d{6,}-[0-9a-f]{6})\s*[:\-—]\s*(.+)$", re.IGNORECASE | re.DOTALL)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pause_key(task_id: str) -> str:
    return f"icarus:subagent:pause:{task_id}"


def index_key(platform: str | None, chat_id: str | None) -> str:
    return f"icarus:subagent:pause_index:{platform or 'unknown'}:{chat_id or 'unknown'}"


def resume_key(task_id: str) -> str:
    return f"icarus:subagent:resume:{task_id}"


def strip_identity_prefix(text: str | None) -> str:
    """L1 prefixes operator messages with `[User:name (id: …)]: ` before the
    model sees them; the explicit-reference form has to survive that."""
    return _IDENTITY_PREFIX_RE.sub("", text or "", count=1)


def parse_task_reference(text: str | None) -> tuple[str, str] | None:
    m = TASK_REF_RE.match(strip_identity_prefix(text))
    if not m:
        return None
    return m.group(1).lower(), m.group(2).strip()


# ── Writer side (the waiting subagent) ───────────────────────────────────────

async def record_pause(
    redis,
    *,
    task_id: str,
    question: str,
    platform: str | None,
    chat_id: str | None,
    delivery_message_ids: list[str] | None,
    timeout_seconds: int = PAUSE_TIMEOUT_SECONDS,
) -> None:
    ttl = int(timeout_seconds) + 120
    await redis.hset(pause_key(task_id), mapping={
        "task_id": task_id,
        "question": question,
        "platform": platform or "",
        "chat_id": str(chat_id) if chat_id else "",
        "delivery_message_ids": json.dumps([str(m) for m in (delivery_message_ids or [])]),
        "created_at": _now_iso(),
        "expires_at": str(int(time.time()) + int(timeout_seconds)),
        "answered_at": "",
    })
    await redis.expire(pause_key(task_id), ttl)
    idx = index_key(platform, chat_id)
    await redis.lpush(idx, task_id)
    await redis.ltrim(idx, 0, _INDEX_MAX - 1)
    await redis.expire(idx, ttl)


async def get_pause(redis, task_id: str) -> dict | None:
    """The open pause record for a task, or None if there isn't one (or it
    was already answered)."""
    raw = await redis.hgetall(pause_key(task_id))
    if not raw:
        return None
    record = {
        (k.decode() if isinstance(k, bytes) else str(k)): (v.decode() if isinstance(v, bytes) else str(v))
        for k, v in raw.items()
    }
    if record.get("answered_at"):
        return None
    return record


async def clear_pause(redis, task_id: str) -> None:
    await redis.delete(pause_key(task_id))


async def wait_for_resume(redis, task_id: str, timeout_seconds: int = PAUSE_TIMEOUT_SECONDS) -> dict | None:
    """Block (in short slices) until an answer is pushed or the timeout
    passes. Returns the answer payload or None."""
    deadline = time.monotonic() + max(0, int(timeout_seconds))
    key = resume_key(task_id)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        slice_s = max(1, min(_BLPOP_SLICE_SECONDS, int(remaining) or 1))
        res = await redis.blpop(key, timeout=slice_s)
        if res:
            raw = res[1]
            try:
                return json.loads(raw.decode() if isinstance(raw, bytes) else raw)
            except Exception:
                return {"answer": raw.decode() if isinstance(raw, bytes) else str(raw)}
        await asyncio.sleep(0)


# ── Reader side (L1, resolving an operator message) ──────────────────────────

async def resolve_paused_reply(
    redis,
    *,
    platform: str,
    chat_id: str | None,
    text: str | None,
    reply_to_message_id: str | None = None,
) -> tuple[str, str] | None:
    """Return (task_id, answer) if this operator message is an answer to a
    paused subagent; None if it's ordinary conversation."""
    ref = parse_task_reference(text)
    if ref:
        task_id, answer = ref
        if await get_pause(redis, task_id):
            return task_id, answer
        return None   # references a task that isn't waiting — let L1 handle the message

    if reply_to_message_id:
        ids = await redis.lrange(index_key(platform, chat_id), 0, _INDEX_MAX - 1)
        for raw_id in ids:
            task_id = raw_id.decode() if isinstance(raw_id, bytes) else str(raw_id)
            record = await get_pause(redis, task_id)
            if not record:
                continue
            try:
                delivered = json.loads(record.get("delivery_message_ids") or "[]")
            except json.JSONDecodeError:
                delivered = []
            if str(reply_to_message_id) in {str(m) for m in delivered}:
                return task_id, strip_identity_prefix(text).strip()
    return None


async def deliver_resume(redis, task_id: str, answer: str, *, user_id: str | None = None) -> None:
    """Hand the operator's answer to the waiting subagent and close the
    pause record so the same message can't resume it twice."""
    await redis.hset(pause_key(task_id), mapping={"answered_at": _now_iso()})
    await redis.lpush(resume_key(task_id), json.dumps({
        "answer": answer, "user_id": user_id, "answered_at": _now_iso(),
    }))
    await redis.expire(resume_key(task_id), 600)
    logger.info(f"[resume] delivered operator answer to {task_id}")
