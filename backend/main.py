import sys
import os
import json
import asyncio
import logging
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI
from backend.database.connection import init_db
from backend.routes import webhook
from backend.agent.heartbeat import run_heartbeat
from backend.agent.discord_bot import run_discord_bot
<<<<<<< Updated upstream
from backend.agent.gmail_watcher import run_gmail_watcher
=======
from backend.agent.memory_repo import migrate_from_log, store_entry
>>>>>>> Stashed changes

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

# Restart Notification Protocol - Informational only
RESTART_INFO_PATH = Path("/workspace/ipc/restart_info.json")
<<<<<<< Updated upstream
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
=======
MEMORY_LOG_PATH   = Path("/workspace/memory/memory.log")
>>>>>>> Stashed changes

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing Icarus Database...")
    await init_db()

<<<<<<< Updated upstream
    # One-time migration: move flat-file memory.log entries into SQLite+FTS5
    try:
        from backend.agent.memory_repo import migrate_from_log
        migrated = await migrate_from_log(LEGACY_MEMORY_LOG)
        if migrated > 0:
            logger.info(f"Migrated {migrated} memory entries from flat file to SQLite.")
    except Exception as e:
        logger.warning(f"Memory migration skipped or failed: {e}")
    
=======
    # Migrate legacy flat-file memory log into SQLite on first boot after upgrade
    if MEMORY_LOG_PATH.exists():
        try:
            migrated = await migrate_from_log(str(MEMORY_LOG_PATH))
            if migrated > 0:
                logger.info(f"Migrated {migrated} entries from memory.log into SQLite.")
        except Exception as e:
            logger.warning(f"Memory log migration failed (non-fatal): {e}")

>>>>>>> Stashed changes
    # Process Restart Context if present
    if RESTART_INFO_PATH.exists():
        try:
            with open(RESTART_INFO_PATH, 'r') as f:
                data = json.load(f)

            reason  = data.get("reason", "Code update / Restart")
            files   = data.get("files", [])
            context = data.get("context", "No context provided")

            # 1. Log to daemon stdout
            logger.info("--- RESTART CHANGE NOTIFICATION ---")
            logger.info(f"REASON: {reason}")
            logger.info(f"FILES MODIFIED: {', '.join(files) if files else 'None'}")
            logger.info(f"ACTION CONTEXT: {context}")
            logger.info("------------------------------------")

<<<<<<< Updated upstream
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
            
=======
            # 2. Store in SQLite memory so the L1 agent sees it in prompt context
            entry_text = f"Restart: {reason}. Files: {', '.join(files) if files else 'None'}. Context: {context}"
            await store_entry(
                platform="global",
                user_id="system",
                entry=entry_text,
                visibility="global",
                category="restart",
                importance=1.5,
                source="system",
            )

>>>>>>> Stashed changes
            # 3. Cleanup: remove the info file so it doesn't trigger on every restart
            os.remove(RESTART_INFO_PATH)

        except Exception as e:
            logger.warning(f"Failed to process restart context: {e}")

    logger.info("Database initialized successfully. Icarus is coming online.")
    heartbeat_task = asyncio.create_task(run_heartbeat())
<<<<<<< Updated upstream
    discord_task = asyncio.create_task(run_discord_bot())
    supervisor_task = asyncio.create_task(run_supervisor())


    # Start Gmail watcher if Pub/Sub is configured
    gmail_task = None
    if os.getenv("GMAIL_PUBSUB_TOPIC", "").strip():
        gmail_task = asyncio.create_task(run_gmail_watcher())
        logger.info("Gmail watcher started.")

=======
    discord_task   = asyncio.create_task(run_discord_bot())
>>>>>>> Stashed changes
    yield
    heartbeat_task.cancel()
    discord_task.cancel()
    supervisor_task.cancel()

    if gmail_task:
        gmail_task.cancel()
    await close_redis()
    logger.info("Icarus shutting down.")

app = FastAPI(title="Project Icarus API", lifespan=lifespan)

app.include_router(webhook.router)

@app.get("/health")
async def health_check():
    return {"status": "healthy", "version": "0.1.0"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=True)
