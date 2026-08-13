import asyncio
import json
import logging
from datetime import datetime, timezone

from backend.database.redis_connection import get_redis_client

logger = logging.getLogger(__name__)

METRICS_SOURCE_LIST = "icarus:metrics:sidecar"
LATEST_KEY = "icarus:metrics:latest"
SUMMARY_LIST_KEY = "icarus:metrics:summary"
MAX_SUMMARY_ITEMS = 300
# Same pub/sub-behind-a-WebSocket shape as activity_repo.ACTIVITY_CHANNEL —
# the dashboard's /ws/telemetry route just subscribes and forwards. Every
# stored summary gets published here too, so the ticker can update live
# instead of only ever reflecting whatever was true at page load.
TELEMETRY_CHANNEL = "icarus:telemetry"


def _to_float(value, default=0.0):
    try:
        if value is None:
            return float(default)
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _compute_pressure_score(metrics: dict) -> tuple[float, str]:
    cpu = max(0.0, min(100.0, _to_float(metrics.get("cpu_percent"), 0.0)))
    mem_total = max(1.0, _to_float(metrics.get("memory_total_mb"), 1.0))
    mem_used = max(0.0, min(mem_total, _to_float(metrics.get("memory_used_mb"), 0.0)))
    disk = max(0.0, min(100.0, _to_float(metrics.get("disk_used_percent"), 0.0)))

    gpu_raw = metrics.get("gpu_util_percent")
    gpu = None if gpu_raw is None else max(0.0, min(100.0, _to_float(gpu_raw, 0.0)))

    mem_ratio = mem_used / mem_total
    cpu_ratio = cpu / 100.0
    disk_ratio = disk / 100.0

    if gpu is None:
        score = (cpu_ratio * 0.45) + (mem_ratio * 0.35) + (disk_ratio * 0.20)
    else:
        score = (cpu_ratio * 0.40) + (mem_ratio * 0.30) + (disk_ratio * 0.20) + ((gpu / 100.0) * 0.10)

    if score < 0.55 and cpu < 80 and mem_ratio < 0.85:
        readiness = "green"
    elif score < 0.75 and cpu < 92 and mem_ratio < 0.93:
        readiness = "yellow"
    else:
        readiness = "red"

    return round(score, 4), readiness


async def _store_summary(payload: dict, score: float, readiness: str):
    redis = get_redis_client()

    metrics = payload.get("metrics") or {}
    source = payload.get("source") or "unknown-sidecar"
    timestamp = payload.get("timestamp") or datetime.now(timezone.utc).isoformat()

    summary = {
        "timestamp": timestamp,
        "source": source,
        "cpu_percent": _to_float(metrics.get("cpu_percent"), 0.0),
        "memory_used_mb": _to_float(metrics.get("memory_used_mb"), 0.0),
        "memory_total_mb": _to_float(metrics.get("memory_total_mb"), 1.0),
        "disk_used_percent": _to_float(metrics.get("disk_used_percent"), 0.0),
        "gpu_util_percent": None if metrics.get("gpu_util_percent") is None else _to_float(metrics.get("gpu_util_percent"), 0.0),
        "pressure_score": score,
        "simulation_readiness": readiness,
    }

    summary_json = json.dumps(summary)
    await redis.set(LATEST_KEY, summary_json)
    await redis.lpush(SUMMARY_LIST_KEY, summary_json)
    await redis.ltrim(SUMMARY_LIST_KEY, 0, MAX_SUMMARY_ITEMS - 1)
    await redis.publish(TELEMETRY_CHANNEL, summary_json)

    logger.info(
        "[metrics] %s readiness=%s score=%.3f cpu=%.1f%% mem=%.0f/%.0fMB disk=%.1f%%",
        source,
        readiness,
        score,
        summary["cpu_percent"],
        summary["memory_used_mb"],
        summary["memory_total_mb"],
        summary["disk_used_percent"],
    )


async def run_metrics_consumer():
    redis = get_redis_client()
    logger.info("[metrics] Consumer started. Waiting for sidecar events.")

    while True:
        try:
            result = await redis.blpop(METRICS_SOURCE_LIST, timeout=5)
            if not result:
                continue

            _, raw = result
            payload = json.loads(raw)
            metrics = payload.get("metrics")
            if not isinstance(metrics, dict):
                logger.warning("[metrics] Ignored payload with missing metrics object.")
                continue

            score, readiness = _compute_pressure_score(metrics)
            await _store_summary(payload, score, readiness)

        except json.JSONDecodeError:
            logger.warning("[metrics] Ignored malformed JSON payload from sidecar.")
        except asyncio.CancelledError:
            logger.info("[metrics] Consumer cancelled.")
            raise
        except Exception as e:
            logger.error("[metrics] Consumer loop error: %s", e)
            await asyncio.sleep(1)
