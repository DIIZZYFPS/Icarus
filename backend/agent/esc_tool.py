import os
import json
import time
import asyncio
import logging
from typing import List

logger = logging.getLogger(__name__)

# This runs IN the container, pointing to the mapped volume
IPC_ESCALATION_DIR = "/workspace/ipc/escalation"
COUNCILOR_TIMEOUT_SECONDS = 120


async def escalate_to_councilor(intent_description: str, target_files: List[str]) -> str:
    """Escalates a task to the L2 Supervisor (The Councilor) and waits for a response.

    Use this tool ONLY when:
    1. You need to modify your own source code (FastAPI application).
    2. A coding task requires internet access, NPM/Node installation, or Docker restarts.
    3. You are unable to solve a problem due to your VRAM/Compute constraints.
    4. You need information or guidance that you cannot obtain within your container.

    This call blocks until the Councilor responds (up to 120 seconds).
    The Councilor's response is returned as this tool's result — use it to
    inform your reply to the user. Do not summarise it as "task delegated";
    actually relay what the Councilor said.

    Args:
        intent_description: A highly detailed prompt explaining EXACTLY what the L2 model must do.
        target_files: Files the L2 model will need to modify (empty list if informational).
    """

    os.makedirs(IPC_ESCALATION_DIR, exist_ok=True)

    timestamp = int(time.time())
    tmp_path = os.path.join(IPC_ESCALATION_DIR, f"intent_{timestamp}.tmp")
    file_path = os.path.join(IPC_ESCALATION_DIR, f"intent_{timestamp}.json")
    response_path = os.path.join(IPC_ESCALATION_DIR, f"intent_{timestamp}.response.json")

    payload = {
        "timestamp": timestamp,
        "intent": intent_description,
        "target_files": target_files,
        "status": "pending_host_pickup"
    }

    try:
        with open(tmp_path, 'w') as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp_path, file_path)
        logger.info(f"Dropped escalation payload to {file_path}. Awaiting Councilor response...")
    except Exception as e:
        error_msg = f"Failed to drop escalation payload: {e}"
        logger.error(error_msg)
        return error_msg

    # Poll for the Councilor's response file (written by councilor.py on the host).
    # asyncio.sleep yields control back to the event loop between checks — non-blocking.
    deadline = time.monotonic() + COUNCILOR_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        await asyncio.sleep(2)
        if os.path.exists(response_path):
            try:
                with open(response_path, 'r') as f:
                    data = json.load(f)
                message = data.get("message", "(Councilor returned no message)")
                if data.get("restart_performed", False):
                    message += "\n\n[Container was restarted to apply source changes.]"
                logger.info(f"Councilor response received. restart_performed={data.get('restart_performed', False)}")
                return message
            except Exception as e:
                logger.error(f"Failed to read Councilor response: {e}")
                return f"Councilor responded but the response file could not be read: {e}"

    return (
        f"Councilor did not respond within {COUNCILOR_TIMEOUT_SECONDS} seconds. "
        f"The intent file at {file_path} may still be processed."
    )
