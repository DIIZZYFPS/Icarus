import os
import json
import time
import logging
from typing import List

logger = logging.getLogger(__name__)

# This runs IN the container, pointing to the mapped volume
IPC_ESCALATION_DIR = "/workspace/ipc/escalation"

def escalate_to_councilor(intent_description: str, target_files: List[str]) -> str:
    """Escalates a complex coding task to the L2 Supervisor (The Councilor).
    
    Use this tool ONLY when:
    1. You need to modify your own source code (FastAPI application).
    2. A coding task requires internet access, NPM/Node installation, or Docker restarts.
    3. You are unable to solve a problem due to your VRAM/Compute constraints.
    
    Args:
        intent_description: A highly detailed prompt explaining EXACTLY what the L2 model must do.
        target_files: A list of files the L2 model will need to modify.
    """
    
    # Ensure directory exists
    os.makedirs(IPC_ESCALATION_DIR, exist_ok=True)
    
    # Create unique filename timestamp
    timestamp = int(time.time())
    tmp_path = os.path.join(IPC_ESCALATION_DIR, f"intent_{timestamp}.tmp")
    file_path = os.path.join(IPC_ESCALATION_DIR, f"intent_{timestamp}.json")

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
            
        logger.info(f"Dropped escalation payload to {file_path}")
        return f"Escalation successful. The Councilor has been summoned. The API container may restart momentarily as changes are applied. You will be notified when complete."
        
    except Exception as e:
        error_msg = f"Failed to drop escalation payload: {e}"
        logger.error(error_msg)
        return error_msg
