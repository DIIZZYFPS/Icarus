"""
desktop.py — /ws/desktop, the link between icarus-api and the host desktop daemon.

Protocol (JSON messages, ADR icarus_desktop_avatar.md §5.3):

  daemon -> server  hello{token}, utterance{turn_id, text}, action_result{id, ok, result},
                    cancel{turn_id}, ping
  server -> daemon  ready, status{turn_id, text}, reply{turn_id, text}, error{turn_id?, message},
                    action{id, name, args}, ambient{heat, unread, tasks_running, recent_task}, pong

Each utterance runs process_message(platform="desktop") as its own task, so the
socket keeps reading while a turn is in flight: action results and cancels
arrive on the same connection the turn is waiting on.
"""

import asyncio
import hmac
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from backend.agent.desktop_session import DesktopSession, clear_active, set_active
from backend.agent.processor import process_message

logger = logging.getLogger(__name__)
router = APIRouter()

PLATFORM = "desktop"
CHAT_ID = "desktop"
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}
AMBIENT_INTERVAL_SECONDS = float(os.getenv("DESKTOP_AMBIENT_INTERVAL_SECONDS", "3"))
RECENT_TASK_SECONDS = 10 * 60
UNREAD_WINDOW_SECONDS = float(os.getenv("DESKTOP_UNREAD_WINDOW_HOURS", "6")) * 3600


def _user_id() -> str:
    return os.getenv("DESKTOP_USER_ID", "operator")


def _token_ok(given) -> bool:
    expected = os.getenv("DESKTOP_TOKEN", "").strip()
    return bool(expected) and isinstance(given, str) and hmac.compare_digest(given, expected)


def clean_status(text: str) -> str:
    """'🔎 Searching your history for “x”…' -> 'Searching your history for “x”'."""
    text = re.sub(r"^[^\w“\"']+", "", str(text or "")).strip()
    return text.rstrip("…. ").strip()


# ── Ambient state ────────────────────────────────────────────────────────────

async def _gpu_heat() -> float | None:
    from backend.database.redis_connection import get_redis_client
    raw = await get_redis_client().get("icarus:metrics:latest")
    if not raw:
        return None
    gpu = json.loads(raw).get("gpu_util_percent")
    return None if gpu is None else max(0.0, min(1.0, float(gpu) / 100.0))


async def _unread_count(limit: int = 200) -> int:
    """Notifications from the last UNREAD_WINDOW_SECONDS that nobody has replied to yet.
    Older unanswered ones don't count: most notifications never get a reply."""
    from backend.database.redis_connection import get_redis_client
    redis = get_redis_client()
    cutoff = time.time() - UNREAD_WINDOW_SECONDS
    count, seen = 0, 0
    async for key in redis.scan_iter(match="icarus:notifications:index:*", count=100):
        name = key.decode() if isinstance(key, bytes) else str(key)
        if name.startswith(f"icarus:notifications:index:{PLATFORM}:"):
            continue  # desktop pop-ups were already spoken aloud
        for raw_id in await redis.lrange(key, 0, 19):
            seen += 1
            nid = raw_id.decode() if isinstance(raw_id, bytes) else str(raw_id)
            consumed, created = await redis.hmget(f"icarus:notifications:record:{nid}", "consumed_at", "created_at")
            try:
                fresh = float(created or 0) >= cutoff
            except ValueError:
                fresh = False
            if fresh and consumed in (None, b"", ""):
                count += 1
            if seen >= limit:
                return count
    return count


async def _task_state() -> tuple[int, bool]:
    from backend.agent.subagent_registry import ACTIVE_STATUSES, TERMINAL_STATUSES, get_registry
    registry = get_registry()
    if not registry.exists():
        return 0, False
    running = await registry.list(statuses=ACTIVE_STATUSES, limit=50)
    finished = await registry.list(statuses=TERMINAL_STATUSES, limit=1)
    recent = False
    if finished and finished[0].get("finished_at"):
        try:
            done = datetime.strptime(finished[0]["finished_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            recent = (datetime.now(timezone.utc) - done).total_seconds() < RECENT_TASK_SECONDS
        except ValueError:
            pass
    return len(running), recent


async def ambient_snapshot() -> dict:
    """Facts the avatar renders from (ADR §6.5). Each source fails independently."""
    snap = {"type": "ambient", "heat": None, "unread": 0, "tasks_running": 0, "recent_task": False}
    try:
        snap["heat"] = await _gpu_heat()
    except Exception as e:
        logger.debug(f"[desktop] heat unavailable: {e}")
    try:
        snap["unread"] = await _unread_count()
    except Exception as e:
        logger.debug(f"[desktop] unread count unavailable: {e}")
    try:
        snap["tasks_running"], snap["recent_task"] = await _task_state()
    except Exception as e:
        logger.debug(f"[desktop] task state unavailable: {e}")
    return snap


async def _ambient_loop(send) -> None:
    while True:
        await send(await ambient_snapshot())
        await asyncio.sleep(AMBIENT_INTERVAL_SECONDS)


# ── Turns ────────────────────────────────────────────────────────────────────

async def _run_turn(send, turn_id: str, text: str, reply_to: str | None = None,
                    images: list[str] | None = None) -> None:
    async def on_activity(status: str) -> None:
        await send({"type": "status", "turn_id": turn_id, "text": clean_status(status)})

    try:
        reply = await process_message(PLATFORM, _user_id(), text, chat_id=CHAT_ID, on_activity=on_activity,
                                      reply_to_message_id=reply_to, image_urls=images or None)
        await send({"type": "reply", "turn_id": turn_id, "text": reply})
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("[desktop] turn %s failed", turn_id)
        await send({"type": "error", "turn_id": turn_id, "message": "Icarus couldn't finish that turn."})


@router.websocket("/ws/desktop")
async def desktop_ws(ws: WebSocket):
    client_host = ws.client.host if ws.client else ""
    if client_host not in LOCAL_HOSTS:
        await ws.close(code=4403)
        return
    await ws.accept()
    try:
        hello = await asyncio.wait_for(ws.receive_json(), timeout=5)
    except Exception:
        await ws.close(code=4400)
        return
    if not isinstance(hello, dict) or hello.get("type") != "hello" or not _token_ok(hello.get("token")):
        await ws.send_json({"type": "error", "message": "unauthorized"})
        await ws.close(code=4401)
        return

    send_lock = asyncio.Lock()

    async def send(message: dict) -> None:
        async with send_lock:
            await ws.send_json(message)

    session = DesktopSession(send)
    set_active(session)
    turns: dict[str, asyncio.Task] = {}
    await send({"type": "ready"})
    ambient = asyncio.create_task(_ambient_loop(send))
    logger.info("[desktop] daemon connected")

    try:
        while True:
            message = await ws.receive_json()
            kind = message.get("type") if isinstance(message, dict) else None
            if kind == "utterance":
                text = str(message.get("text") or "").strip()
                if not text:
                    continue
                turn_id = str(message.get("turn_id") or uuid.uuid4().hex[:8])
                # A new utterance supersedes anything still running.
                for task in turns.values():
                    task.cancel()
                reply_to = message.get("reply_to")
                # Screenshots arrive inline as data:image/… URIs (SUPER+SHIFT+I); at most two per turn.
                images = [i for i in (message.get("images") or []) if isinstance(i, str) and i.startswith("data:image/")][:2]
                task = asyncio.create_task(_run_turn(send, turn_id, text, str(reply_to) if reply_to else None, images))
                turns[turn_id] = task
                task.add_done_callback(lambda _t, tid=turn_id: turns.pop(tid, None))
            elif kind == "action_result":
                session.resolve(str(message.get("id")), message.get("ok"), message.get("result"))
            elif kind == "cancel":
                task = turns.get(str(message.get("turn_id")))
                if task:
                    task.cancel()
            elif kind == "ping":
                await send({"type": "pong"})
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("[desktop] connection error")
    finally:
        ambient.cancel()
        for task in turns.values():
            task.cancel()
        session.close()
        clear_active(session)
        logger.info("[desktop] daemon disconnected")
