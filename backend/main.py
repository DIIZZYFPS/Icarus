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

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

# Restart Notification Protocol - Informational only
RESTART_INFO_PATH = Path("/workspace/ipc/restart_info.json")
MEMORY_LOG_PATH = Path("/workspace/memory/memory.log")

from backend.database.redis_connection import get_redis_client, close_redis

# Supervisor logic
async def run_supervisor():
    """Monitor queue depth and scale workers."""
    redis = get_redis_client()
    # Threshold for scaling
    THRESHOLD = 5
    # Max workers to spawn
    MAX_WORKERS = 10
    current_workers = 0
    
    logger.info("Supervisor starting...")
    while True:
        try:
            # Check queue depth for tasks:email_priority
            # xlen gives current total length, but we want pending messages
            info = await redis.xinfo_groups("tasks:email_priority")
            pending = 0
            for group in info:
                if group["name"] == "email_worker_group":
                    pending = group["pending"]
            
            # Simple scaling logic
            needed_workers = (pending // THRESHOLD) + (1 if pending > 0 else 0)
            needed_workers = min(needed_workers, MAX_WORKERS)
            
            if needed_workers != current_workers:
                logger.info(f"Scaling email_worker: {current_workers} -> {needed_workers} (Pending: {pending})")
                # Using subprocess to run docker commands if socket is mounted
                try:
                    # Note: Inside container, we need to ensure docker-compose is installed
                    # or use docker directly if it's a swarm/standalone.
                    # Since it's docker-compose, we can try scaling via docker directly if name is known,
                    # but docker-compose up --scale is easier.
                    proc = await asyncio.create_subprocess_exec(
                        "docker", "compose", "up", "-d", "--scale", f"email_worker={needed_workers}",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    stdout, stderr = await proc.communicate()
                    if proc.returncode == 0:
                        current_workers = needed_workers
                    else:
                        logger.error(f"Scaling failed: {stderr.decode()}")
                except Exception as e:
                    logger.error(f"Scaling command failed: {e}")
            
        except Exception as e:
            if "no such key" in str(e).lower():
                pass # Stream doesn't exist yet
            else:
                logger.warning(f"Supervisor error: {e}")
        
        await asyncio.sleep(30)

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing Icarus Database...")
    await init_db()
    
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
            
            # 2. Append to memory log so the L1 agent sees it in prompt context
            if MEMORY_LOG_PATH.parent.exists():
                from datetime import datetime, timezone
                timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                entry = f"[{timestamp}] [RESTART] Reason: {reason}. Files: {', '.join(files)}. Context: {context}\n"
                with open(MEMORY_LOG_PATH, 'a') as f:
                    f.write(entry)
            
            # 3. Cleanup: remove the info file so it doesn't trigger on every restart
            os.remove(RESTART_INFO_PATH)
            
        except Exception as e:
            logger.warning(f"Failed to process restart context: {e}")

    logger.info("Database initialized successfully. Icarus is coming online.")
    heartbeat_task = asyncio.create_task(run_heartbeat())
    discord_task = asyncio.create_task(run_discord_bot())
    supervisor_task = asyncio.create_task(run_supervisor())
    yield
    heartbeat_task.cancel()
    discord_task.cancel()
    supervisor_task.cancel()
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
