import json
import logging

from backend.database.redis_connection import get_redis_client

logger = logging.getLogger(__name__)

LATEST_METRICS_KEY = "icarus:metrics:latest"
# Mirrors metrics_consumer.SUMMARY_LIST_KEY — duplicated rather than imported,
# same reasoning as LATEST_METRICS_KEY above: this module stays a thin Redis
# reader with no dependency on the consumer's internals.
SUMMARY_LIST_KEY = "icarus:metrics:summary"


def _to_float(value):
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_snapshot(snapshot: dict) -> dict:
    return {
        "timestamp": snapshot.get("timestamp"),
        "source": snapshot.get("source"),
        "cpu_percent": _to_float(snapshot.get("cpu_percent")),
        "memory_used_mb": _to_float(snapshot.get("memory_used_mb")),
        "memory_total_mb": _to_float(snapshot.get("memory_total_mb")),
        "disk_used_percent": _to_float(snapshot.get("disk_used_percent")),
        "gpu_util_percent": _to_float(snapshot.get("gpu_util_percent")),
        "pressure_score": _to_float(snapshot.get("pressure_score")),
        "simulation_readiness": snapshot.get("simulation_readiness"),
    }


async def fetch_latest_telemetry_snapshot() -> dict | None:
    """Return the normalized latest telemetry snapshot, or None if unavailable."""
    redis = get_redis_client()
    raw = await redis.get(LATEST_METRICS_KEY)
    if not raw:
        return None

    payload = json.loads(raw)
    if not isinstance(payload, dict):
        return None

    return _normalize_snapshot(payload)


async def fetch_recent_telemetry_history(limit: int = 20) -> list[dict]:
    """Recent telemetry snapshots, oldest-first, for the dashboard's ticker
    sparkline. The summary list is LPUSHed (newest at index 0), so this
    reverses on the way out — a sparkline reads left-to-right as time moving
    forward, same convention as the activity timeline."""
    redis = get_redis_client()
    raw_items = await redis.lrange(SUMMARY_LIST_KEY, 0, max(1, limit) - 1)
    snapshots = []
    for raw in raw_items:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            snapshots.append(_normalize_snapshot(payload))
    snapshots.reverse()
    return snapshots


async def get_telemetry_snapshot() -> dict:
    """Tool: retrieve latest telemetry for status/health responses."""
    try:
        snapshot = await fetch_latest_telemetry_snapshot()
        if snapshot is None:
            return {
                "status": "unavailable",
                "message": "No telemetry snapshot is available yet.",
            }

        return {
            "status": "ok",
            **snapshot,
        }
    except json.JSONDecodeError:
        logger.warning("[telemetry] Latest telemetry payload is invalid JSON.")
        return {
            "status": "error",
            "message": "Latest telemetry payload is malformed.",
        }
    except Exception as e:
        logger.error("[telemetry] Failed to fetch telemetry snapshot: %s", e)
        return {
            "status": "error",
            "message": f"Telemetry read failed: {e}",
        }