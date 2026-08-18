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

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

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
    from backend.agent.telemetry_tools import fetch_latest_telemetry_snapshot, fetch_recent_telemetry_history

    platform, user_id = DASHBOARD_PLATFORM, _operator_user_id()

    all_items = await list_items(platform=platform, user_id=user_id)

    attention = [
        _serialize_tracked_item(i)
        for i in all_items
        if i.item_type in ("job_application", "bill", "job_opportunity") and not i.dismissed
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
    telemetry_history = await fetch_recent_telemetry_history(limit=20)
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
            "telemetry_history": telemetry_history,
        },
    }


@router.get("/api/jobs")
async def jobs_sheet():
    """Full job pipeline — every job_application and job_opportunity row,
    dismissed or not (unlike /api/today's attention list, which is
    outstanding-only). Sorting/filtering happens client-side against this
    one payload rather than round-tripping per column click."""
    from backend.agent.tracked_items_repo import list_items

    platform, user_id = DASHBOARD_PLATFORM, _operator_user_id()
    all_items = await list_items(platform=platform, user_id=user_id)
    items = [
        _serialize_tracked_item(i)
        for i in all_items
        if i.item_type in ("job_application", "job_opportunity")
    ]
    return {"count": len(items), "items": items}


@router.post("/api/tracked-items/{item_id}/dismiss")
async def dismiss_tracked_item(item_id: int):
    from backend.agent.tracked_items_repo import set_dismissed
    ok = await set_dismissed(item_id, True)
    if not ok:
        raise HTTPException(status_code=404, detail="Tracked item not found")
    return {"status": "ok", "id": item_id, "dismissed": True}


@router.post("/api/tracked-items/{item_id}/undismiss")
async def undismiss_tracked_item(item_id: int):
    from backend.agent.tracked_items_repo import set_dismissed
    ok = await set_dismissed(item_id, False)
    if not ok:
        raise HTTPException(status_code=404, detail="Tracked item not found")
    return {"status": "ok", "id": item_id, "dismissed": False}


@router.post("/api/tracked-items/{item_id}/promote")
async def promote_tracked_item(item_id: int):
    """Operator confirms they actually applied to a scored opportunity —
    flips it from job_opportunity to job_application in place. See
    tracked_items_repo.promote_to_application()'s docstring for why this
    reuses the same row instead of creating a new one."""
    from backend.agent.tracked_items_repo import promote_to_application
    ok = await promote_to_application(item_id)
    if not ok:
        raise HTTPException(status_code=404, detail="job_opportunity not found")
    return {"status": "ok", "id": item_id, "item_type": "job_application"}


class UrgencyAdjustRequest(BaseModel):
    urgency: str


@router.post("/api/tracked-items/{item_id}/urgency")
async def adjust_tracked_item_urgency(item_id: int, body: UrgencyAdjustRequest):
    """The "lower importance / mark important" correction from the
    dashboard. If this item came from email triage (has a message_id), the
    correction also feeds the learning loop — it's recorded against the
    TriageClassification that created it, same as any other review, so a
    similar email from the same sender is judged differently next time."""
    from backend.agent.tracked_items_repo import get_by_id, set_urgency

    item = await get_by_id(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Tracked item not found")

    previous_urgency = item.urgency
    ok = await set_urgency(item_id, body.urgency)
    if not ok:
        raise HTTPException(status_code=404, detail="Tracked item not found")

    if item.message_id:
        try:
            from backend.agent.triage_repo import get_by_message_id, record_review
            classification = await get_by_message_id(kind="inbox", message_id=item.message_id)
            if classification is not None:
                await record_review(
                    item_id=classification.id, review_action="overridden",
                    corrected_urgency=body.urgency,
                    feedback_note=f"Urgency adjusted from the dashboard (was {previous_urgency}).",
                )
        except Exception as e:
            logger.warning(f"[dashboard] Failed to feed urgency correction into triage learning loop: {e}")

    return {"status": "ok", "id": item_id, "urgency": body.urgency}


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
        "dismissed": bool(item.dismissed),
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


@router.websocket("/ws/telemetry")
async def ws_telemetry(websocket: WebSocket):
    """Exact mirror of /ws/activity above, just against
    metrics_consumer.TELEMETRY_CHANNEL — a separate socket rather than
    piggybacking telemetry onto the activity feed, since it's a different
    event shape (a raw normalized snapshot, not an activity_events row) and
    a different cadence (~every 2s from the sidecar vs. whenever something
    actually happens)."""
    from backend.database.redis_connection import get_redis_client
    from backend.agent.metrics_consumer import TELEMETRY_CHANNEL

    await websocket.accept()
    redis = get_redis_client()
    pubsub = redis.pubsub()
    await pubsub.subscribe(TELEMETRY_CHANNEL)
    logger.info("[dashboard] Telemetry WebSocket client connected")

    try:
        async for msg in pubsub.listen():
            if msg["type"] != "message":
                continue
            try:
                # Already-JSON from metrics_consumer's _store_summary —
                # pass straight through, same as the activity socket.
                await websocket.send_text(msg["data"])
            except WebSocketDisconnect:
                break
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning(f"[dashboard] Telemetry WebSocket error: {e}")
    finally:
        await pubsub.unsubscribe(TELEMETRY_CHANNEL)
        await pubsub.close()
        logger.info("[dashboard] Telemetry WebSocket client disconnected")
