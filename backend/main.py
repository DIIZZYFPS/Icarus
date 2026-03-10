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
    yield
    heartbeat_task.cancel()
    discord_task.cancel()
    logger.info("Icarus shutting down.")

app = FastAPI(title="Project Icarus API", lifespan=lifespan)

app.include_router(webhook.router)

@app.get("/health")
async def health_check():
    return {"status": "healthy", "version": "0.1.0"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=True)
