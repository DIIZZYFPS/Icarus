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
