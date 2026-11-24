"""
busy.py — what Icarus is working on in the background, for the avatar's thought bubbles.

Background jobs that aren't the operator's own desktop conversation (a Discord reply, an
email being triaged, a delegated task, a subagent cycle, a Councilor consult) wrap their
work in `async with busy(source):`. Each job is one field in a Redis hash, so every
process (icarus-api, the email worker, the Councilor on the host, subagent containers)
can report into it. The desktop route polls `snapshot()` and forwards changes.

A job carries an `until` deadline: a crashed process can't leave a bubble up forever.
Finished jobs stay listed for a moment (done, ok) so the avatar can play the pop.
Only the source is recorded, never content: the bubbles may be on a shared screen.

Claude tasks report here too, with content (their title and current step) because the badge
beside the orb needs it: task_update() writes one entry per task into a second hash and
tasks_snapshot() lists them. They ride the same push as the bubbles, so the avatar always gets
the whole current list and never has to patch one together from events.

Stdlib + redis only (the Councilor imports this from its minimal host venv). Every call
is best-effort; a Redis hiccup never breaks the work being reported.
"""

import json
import logging
import sys
import time
import uuid
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)

BUSY_KEY = "icarus:busy"
TASKS_KEY = "icarus:busy:tasks"
SOURCES = ("discord", "telegram", "email", "subagent", "councilor")
DEFAULT_TTL = 600.0     # a job that runs longer than this without ending is presumed dead
DONE_LINGER = 3.0       # how long a finished job stays listed, for the pop animation


def _redis():
    from backend.database.redis_connection import get_redis_client
    return get_redis_client()


def _enabled() -> bool:
    # Test runs share the live Redis when run inside icarus-api; reporting from them would
    # flash bubbles on the real avatar. busy's own tests patch this.
    return "unittest" not in sys.modules


async def _put(job_id: str, entry: dict) -> None:
    if not _enabled():
        return
    try:
        await _redis().hset(BUSY_KEY, job_id, json.dumps(entry))
    except Exception as e:
        logger.debug(f"[busy] {job_id} not recorded: {e}")


@asynccontextmanager
async def busy(source: str, ttl: float = DEFAULT_TTL):
    """Mark `source` busy for the duration of the block. Failure is reported if it raises."""
    if source not in SOURCES:
        raise ValueError(f"unknown busy source {source!r}")
    job_id = f"{source}-{uuid.uuid4().hex[:10]}"
    started = time.time()
    await _put(job_id, {"source": source, "started": started, "until": started + ttl})
    ok = False
    try:
        yield
        ok = True
    finally:
        await _put(job_id, {"source": source, "started": started, "done": True, "ok": ok,
                            "until": time.time() + DONE_LINGER})


async def task_update(task_id: str, text: str, done: bool = False, ok: bool = True, step: bool = True,
                      ttl: float = DEFAULT_TTL, **meta) -> None:
    """Record a task's current step (title, project, mode and started go in `meta`).
    `step` is False for the kickoff line, which isn't a tool call and doesn't count. A finished
    task stays listed for DONE_LINGER seconds so the avatar can close its chip."""
    if not _enabled():
        return
    try:
        redis = _redis()
        previous = {}
        raw = await redis.hget(TASKS_KEY, task_id)
        if raw:
            previous = json.loads(raw)
        entry = {**previous, **{k: v for k, v in meta.items() if v}, "id": task_id, "text": text,
                 "steps": int(previous.get("steps") or 0) + (1 if step and not done else 0),
                 "done": done, "ok": ok, "until": time.time() + (DONE_LINGER if done else ttl)}
        entry.setdefault("started", time.time())
        await redis.hset(TASKS_KEY, task_id, json.dumps(entry))
    except Exception as e:
        logger.debug(f"[busy] task {task_id} not recorded: {e}")


async def clear_tasks() -> None:
    """Forget every task entry: the process that was reporting them is gone."""
    if not _enabled():
        return
    try:
        await _redis().delete(TASKS_KEY)
    except Exception as e:
        logger.debug(f"[busy] tasks not cleared: {e}")


async def tasks_snapshot() -> list[dict]:
    """Running and just-finished tasks, oldest first. Expired entries are pruned on the way."""
    raw = await _redis().hgetall(TASKS_KEY)
    now, items, stale = time.time(), [], []
    for task_id, value in (raw or {}).items():
        task_id = task_id.decode() if isinstance(task_id, bytes) else task_id
        try:
            entry = json.loads(value)
            expired = float(entry.get("until") or 0) < now
        except (TypeError, ValueError):
            entry, expired = None, True
        if expired:
            stale.append(task_id)
            continue
        items.append({k: v for k, v in entry.items() if k != "until"})
    if stale:
        try:
            await _redis().hdel(TASKS_KEY, *stale)
        except Exception:
            pass
    return sorted(items, key=lambda i: i.get("started") or 0)


async def snapshot() -> list[dict]:
    """Live and just-finished jobs, oldest first. Expired entries are pruned on the way."""
    raw = await _redis().hgetall(BUSY_KEY)
    now, items, stale = time.time(), [], []
    for job_id, value in (raw or {}).items():
        job_id = job_id.decode() if isinstance(job_id, bytes) else job_id
        try:
            entry = json.loads(value)
            expired = float(entry.get("until") or 0) < now
        except (TypeError, ValueError):
            entry, expired = None, True
        if expired or entry.get("source") not in SOURCES:
            stale.append(job_id)
            continue
        items.append({"id": job_id, "source": entry["source"], "started": entry.get("started"),
                      "done": bool(entry.get("done")), "ok": bool(entry.get("ok", True))})
    if stale:
        try:
            await _redis().hdel(BUSY_KEY, *stale)
        except Exception:
            pass
    return sorted(items, key=lambda i: i.get("started") or 0)
