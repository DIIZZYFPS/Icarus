import sys
import os
import json
import asyncio
import logging
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from backend.database.connection import init_db
from backend.routes import webhook
from backend.routes import dashboard
from backend.routes import triage
from backend.agent.heartbeat import run_heartbeat
from backend.agent.discord_bot import run_discord_bot
from backend.agent.gmail_watcher import run_gmail_watcher
from backend.agent.metrics_consumer import run_metrics_consumer
from backend.agent.activity_consumer import run_activity_consumer
from backend.agent.calendar_watcher import run_calendar_watcher
from backend.agent.spam_sweep import run_spam_sweep

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

# Restart Notification Protocol - Informational only
RESTART_INFO_PATH = Path("/workspace/ipc/restart_info.json")
LEGACY_MEMORY_LOG = "/workspace/memory/memory.log"

from backend.database.redis_connection import get_redis_client, close_redis


# Supervisor logic
async def run_supervisor():
    """Monitor queue depth and log scale recommendations.

    Dynamic scaling via docker compose is intentionally NOT executed from inside
    the API container (no docker socket is mounted).  Instead, this supervisor
    calculates the desired worker count and logs a recommendation; an external
    operator or host-level watcher can act on it.
    """
    redis = get_redis_client()
    # Threshold of pending+lagging messages that triggers a scale-up suggestion
    THRESHOLD = 5
    MAX_WORKERS = 10
    
    logger.info("Supervisor starting...")
    while True:
        try:
            # Check queue depth for tasks:email_triage.
            # "pending" counts messages delivered but not yet ACK-ed.
            # "lag" counts messages not yet delivered to any consumer.
            # Together they represent the true unprocessed backlog.
            info = await redis.xinfo_groups("tasks:email_triage")
            pending = 0
            lag = 0
            for group in info:
                if group["name"] == "email_triage_group":
                    pending = group.get("pending", 0)
                    lag = group.get("lag", 0)
            
            backlog = pending + lag

            # Simple scaling logic: 1 worker per THRESHOLD messages, minimum 1 when
            # there is any backlog at all.
            needed_workers = (backlog // THRESHOLD) + (1 if backlog > 0 else 0)
            needed_workers = min(needed_workers, MAX_WORKERS)

            if needed_workers > 0:
                logger.info(
                    f"[supervisor] Backlog={backlog} (pending={pending}, lag={lag}). "
                    f"Recommended email_worker replicas: {needed_workers}. "
                    f"To scale: docker compose up -d --scale email_worker={needed_workers}"
                )

        except Exception as e:
            if "no such key" in str(e).lower():
                pass  # Stream doesn't exist yet
            else:
                logger.warning(f"Supervisor error: {e}")
        
        await asyncio.sleep(30)

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing Icarus Database...")
    await init_db()

    # One-time migration: move flat-file memory.log entries into SQLite+FTS5
    try:
        from backend.agent.memory_repo import migrate_from_log
        migrated = await migrate_from_log(LEGACY_MEMORY_LOG)
        if migrated > 0:
            logger.info(f"Migrated {migrated} memory entries from flat file to SQLite.")
    except Exception as e:
        logger.warning(f"Memory migration skipped or failed: {e}")
    
    # Process Restart Context if present
    if RESTART_INFO_PATH.exists():
        try:
            with open(RESTART_INFO_PATH, 'r') as f:
                data = json.load(f)
            
            reason = data.get("reason", "Code update / Restart")
            files = data.get("files", [])
            context = data.get("context", "No context provided")
            
            # 1. Log to daemon stdout
            logger.info("--- RESTART CHANGE NOTIFICATION ---")
            logger.info(f"REASON: {reason}")
            logger.info(f"FILES MODIFIED: {', '.join(files) if files else 'None'}")
            logger.info(f"ACTION CONTEXT: {context}")
            logger.info("------------------------------------")

            # 2. Store restart context in the memory database
            try:
                from backend.agent.memory_repo import store_entry
                restart_entry = f"Restart — Reason: {reason}. Files: {', '.join(files)}. Context: {context}"
                await store_entry(
                    platform="global", user_id="system",
                    entry=restart_entry, visibility="global",
                    category="restart", source="system",
                )
            except Exception as mem_e:
                logger.warning(f"Failed to store restart context in memory DB: {mem_e}")
            
            # 3. Cleanup: remove the info file so it doesn't trigger on every restart
            os.remove(RESTART_INFO_PATH)
            
        except Exception as e:
            logger.warning(f"Failed to process restart context: {e}")

    logger.info("Database initialized successfully. Icarus is coming online.")
    heartbeat_task = asyncio.create_task(run_heartbeat())
    discord_task = asyncio.create_task(run_discord_bot())
    supervisor_task = asyncio.create_task(run_supervisor())
    metrics_task = asyncio.create_task(run_metrics_consumer())
    activity_task = asyncio.create_task(run_activity_consumer())
    calendar_task = asyncio.create_task(run_calendar_watcher())
    spam_sweep_task = asyncio.create_task(run_spam_sweep())


    # Start Gmail watcher if Pub/Sub is configured
    gmail_task = None
    if os.getenv("GMAIL_PUBSUB_TOPIC", "").strip():
        gmail_task = asyncio.create_task(run_gmail_watcher())
        logger.info("Gmail watcher started.")

    yield
    heartbeat_task.cancel()
    discord_task.cancel()
    supervisor_task.cancel()
    metrics_task.cancel()
    activity_task.cancel()
    calendar_task.cancel()
    spam_sweep_task.cancel()

    if gmail_task:
        gmail_task.cancel()
    await close_redis()
    logger.info("Icarus shutting down.")

app = FastAPI(title="Project Icarus API", lifespan=lifespan)

app.include_router(webhook.router)
app.include_router(dashboard.router)
app.include_router(triage.router)

_DASHBOARD_DIR = Path(__file__).parent / "static" / "dashboard"
if _DASHBOARD_DIR.exists():
    app.mount("/dashboard", StaticFiles(directory=str(_DASHBOARD_DIR), html=True), name="dashboard")

@app.get("/health")
async def health_check():
    return {"status": "healthy", "version": "0.1.0"}


@app.get("/metrics/latest")
async def metrics_latest():
    redis = get_redis_client()
    raw = await redis.get("icarus:metrics:latest")
    if not raw:
        return {"status": "empty", "message": "No telemetry received yet."}

    try:
        return json.loads(raw)
    except Exception:
        return {"status": "error", "message": "Stored telemetry payload is invalid JSON."}


@app.get("/metrics/recent")
async def metrics_recent(limit: int = 10):
    redis = get_redis_client()
    safe_limit = max(1, min(limit, 100))
    items = await redis.lrange("icarus:metrics:summary", 0, safe_limit - 1)
    parsed = []
    for item in items:
        try:
            parsed.append(json.loads(item))
        except Exception:
            continue
    return {"count": len(parsed), "items": parsed}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=True)
