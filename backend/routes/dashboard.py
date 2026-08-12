"""
dashboard.py — REST + WebSocket surface for the Icarus dashboard frontend.

Two read models, matching the two dashboard pages:
  - /api/today            — "at a glance" snapshot (tracked items, calendar,
                             ticker stats) for the Today page.
  - /api/activity/recent  — durable backfill for the Activity page's
                             timeline, read from activity_events.
  - /ws/activity           — live tail of the same events, straight off the
                             icarus:activity Redis channel (not through the
                             DB — so live updates aren't gated on the
                             consumer's persistence latency).
"""

import os
import json
import socket
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

router = APIRouter()

DASHBOARD_PLATFORM = "discord"


def _operator_user_id() -> str:
    return os.environ.get("DISCORD_OPERATOR_ID", "0")


async def _get_model_display_name() -> str:
    """The real loaded model name, read live from llama-server's own
    /v1/models — not LOCAL_LLM_MODEL, which is just the placeholder string
    ("local") sent in the request body's model field. llama.cpp is a
    single-model server and ignores that field entirely, so it was never
    the actual model identity; this asks the server what it actually
    loaded instead of trusting a static env var that can drift the moment
    the GGUF file changes. Falls back to the env var (or "unknown") if the
    server's unreachable — a dashboard load should never break over this."""
    import httpx

    base_url = os.getenv("LOCAL_LLM_URL", "").rstrip("/")
    fallback = os.getenv("LOCAL_LLM_MODEL", "") or "unknown"
    if not base_url:
        return fallback
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{base_url}/models")
            resp.raise_for_status()
            data = resp.json()
        model_id = data["data"][0]["id"]
        # e.g. "/home/diizzy/Qwen3.6-35B-A3B-MTP-UD-Q4_K_XL.gguf" -> "Qwen3.6-35B-A3B-MTP-UD-Q4_K_XL"
        name = os.path.basename(model_id)
        if name.lower().endswith(".gguf"):
            name = name[:-5]
        return name or fallback
    except Exception as e:
        logger.warning(f"[dashboard] Failed to read live model name from llama-server: {e}")
        return fallback


@router.get("/api/activity/recent")
async def activity_recent(limit: int = 150, actor: str | None = None):
    from backend.agent.activity_repo import list_recent_activity
    events = await list_recent_activity(limit=limit, actor=actor)
    return {"count": len(events), "events": events}


@router.get("/api/today")
async def today_snapshot():
    from backend.agent.tracked_items_repo import list_items
    from backend.agent.activity_repo import latest_activity_for_actor
    from backend.agent.telemetry_tools import fetch_latest_telemetry_snapshot

    platform, user_id = DASHBOARD_PLATFORM, _operator_user_id()

    all_items = await list_items(platform=platform, user_id=user_id)

    attention = [
        _serialize_tracked_item(i)
        for i in all_items
        if i.item_type in ("job_application", "bill")
    ]
    calendar = [
        _serialize_tracked_item(i)
        for i in all_items
        if i.item_type == "calendar_event"
    ]
    calendar.sort(key=lambda e: e.get("due_at") or "")

    councilor_latest = await latest_activity_for_actor("councilor")
    triage_latest = await latest_activity_for_actor("triage")
    telemetry = await fetch_latest_telemetry_snapshot()
    model_name = await _get_model_display_name()

    return {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "hostname": socket.gethostname() or "unknown-host",
        "attention": attention,
        "calendar": calendar,
        "ticker": {
            "councilor": councilor_latest,
            "triage": triage_latest,
            "heartbeat_interval_seconds": 15,
            "model": {
                "url": os.getenv("LOCAL_LLM_URL", ""),
                "name": model_name,
            },
            "telemetry": telemetry,
        },
    }


def _serialize_tracked_item(item) -> dict:
    payload = None
    if item.payload:
        try:
            payload = json.loads(item.payload)
        except Exception:
            payload = None
    return {
        "id": item.id,
        "item_type": item.item_type,
        "entity_key": item.entity_key,
        "state": item.state,
        "summary": item.summary,
        "next_action": item.next_action,
        "due_at": item.due_at,
        "urgency": item.urgency,
        "payload": payload,
        "source": item.source,
        "updated_at": item.updated_at,
    }


@router.websocket("/ws/activity")
async def ws_activity(websocket: WebSocket):
    from backend.database.redis_connection import get_redis_client
    from backend.agent.activity_repo import ACTIVITY_CHANNEL

    await websocket.accept()
    redis = get_redis_client()
    pubsub = redis.pubsub()
    await pubsub.subscribe(ACTIVITY_CHANNEL)
    logger.info("[dashboard] Activity WebSocket client connected")

    try:
        async for msg in pubsub.listen():
            if msg["type"] != "message":
                continue
            try:
                # Already-JSON from publish_activity — pass straight through.
                await websocket.send_text(msg["data"])
            except WebSocketDisconnect:
                break
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning(f"[dashboard] Activity WebSocket error: {e}")
    finally:
        await pubsub.unsubscribe(ACTIVITY_CHANNEL)
        await pubsub.close()
        logger.info("[dashboard] Activity WebSocket client disconnected")
