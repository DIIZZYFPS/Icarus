import os
import json
import time
import asyncio
import logging
from typing import List

logger = logging.getLogger(__name__)

# This runs IN the container, pointing to the mapped volume
IPC_ESCALATION_DIR = "/workspace/ipc/escalation"
IPC_MAIL_DIR = "/workspace/ipc/mail"
CONSULTATION_TIMEOUT_SECONDS = 30


def check_mailbox() -> str:
    """Scan the IPC mail directory for any unprocessed Councilor responses.
    The heartbeat delivers these automatically every 15s, but call this
    to check immediately without waiting for the next cycle.
    Returns a summary of pending results, or confirms the mailbox is empty."""
    if not os.path.isdir(IPC_MAIL_DIR):
        return "Mail directory not found — no mailbox to check."
    pending = []
    for fname in os.listdir(IPC_MAIL_DIR):
        if not fname.endswith(".response.json"):
            continue
        if fname.startswith("intent_") or fname.startswith("consult_"):
            stem = fname[:-len(".response.json")]
        else:
            continue
        ack_path = os.path.join(IPC_MAIL_DIR, f"{stem}.acked")
        if os.path.exists(ack_path):
            continue
        try:
            with open(os.path.join(IPC_MAIL_DIR, fname)) as f:
                data = json.load(f)
            message = data.get("message", "(no message)")
            restart = data.get("restart_performed", False)
            entry = f"{stem}: {message[:200]}{'...' if len(message) > 200 else ''}"
            if restart:
                entry += " [restart performed]"
            pending.append(entry)
        except Exception as e:
            pending.append(f"{stem}: (unreadable — {e})")
    if not pending:
        return "Mailbox empty — no unprocessed Councilor responses."
    return "Pending Councilor responses:\n" + "\n".join(pending)


async def consult_councilor(question: str) -> str:
    """Consult the Councilor (Gemini L2) for analysis, advice, or knowledge.

    This is a READ-ONLY consultation — Gemini will not execute any commands or
    modify any files. Use this when you need:
    - Analysis or explanation of code, errors, or concepts
    - Advice on how to approach a problem
    - Context or knowledge you don't have within your container

    This call blocks until the Councilor responds (up to 30 seconds).
    Returns Gemini's answer directly — relay it to the user.

    Args:
        question: The question or topic you want Gemini to analyse or explain.
    """
    os.makedirs(IPC_ESCALATION_DIR, exist_ok=True)
    os.makedirs(IPC_MAIL_DIR, exist_ok=True)

    from backend.agent.tools import current_platform, current_user_id, current_chat_id
    
    platform = current_platform.get()
    user_id = current_user_id.get()
    chat_id = current_chat_id.get()
    
    timestamp = int(time.time())
    tmp_path = os.path.join(IPC_ESCALATION_DIR, f"consult_{timestamp}.tmp")
    file_path = os.path.join(IPC_ESCALATION_DIR, f"consult_{timestamp}.json")
    response_path = os.path.join(IPC_MAIL_DIR, f"consult_{timestamp}.response.json")

    payload = {
        "timestamp": timestamp,
        "platform": platform,
        "user_id": user_id,
        "chat_id": chat_id,
        "question": question,
        "status": "pending_host_pickup"
    }

    try:
        with open(tmp_path, 'w') as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp_path, file_path)
        logger.info(f"Dropped consultation payload to {file_path}. Awaiting Councilor response...")
    except Exception as e:
        error_msg = f"Failed to drop consultation payload: {e}"
        logger.error(error_msg)
        return error_msg

    deadline = time.monotonic() + CONSULTATION_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        await asyncio.sleep(2)
        if os.path.exists(response_path):
            try:
                with open(response_path, 'r') as f:
                    data = json.load(f)
                message = data.get("message", "(Councilor returned no message)")
                logger.info(f"Consultation response received from Councilor.")
                # Write ack so the heartbeat doesn't re-deliver this response
                ack_path = os.path.join(IPC_MAIL_DIR, f"consult_{timestamp}.acked")
                try:
                    open(ack_path, 'w').close()
                except Exception:
                    pass
                return message
            except Exception as e:
                logger.error(f"Failed to read consultation response: {e}")
                return f"Councilor responded but the response file could not be read: {e}"

    return (
        f"Councilor did not respond within {CONSULTATION_TIMEOUT_SECONDS} seconds. "
        f"The consultation file at {file_path} may still be processed."
    )


async def escalate_to_councilor(intent_description: str, target_files: List[str]) -> str:
    """Dispatches a write/execute task to the L2 Supervisor (The Councilor).

    Use this tool when:
    1. You need to modify your own source code (FastAPI application).
    2. A task requires installing dependencies, Docker restarts, or host commands.
    3. You are unable to complete a task due to container restrictions.

    This call returns IMMEDIATELY — it does not block. The Councilor processes the
    task in the background and delivers the result via the originating platform directly to DIIZZY.
    After calling this tool, log the dispatch with append_memory so you remember
    the operation is pending.

    Args:
        intent_description: A highly detailed prompt explaining EXACTLY what the L2 model must do.
        target_files: Files the L2 model will need to modify (empty list if informational).
    """
    os.makedirs(IPC_ESCALATION_DIR, exist_ok=True)

    from backend.agent.tools import current_platform, current_user_id, current_chat_id
    
    platform = current_platform.get()
    user_id = current_user_id.get()
    chat_id = current_chat_id.get()
    
    timestamp = int(time.time())
    tmp_path = os.path.join(IPC_ESCALATION_DIR, f"intent_{timestamp}.tmp")
    file_path = os.path.join(IPC_ESCALATION_DIR, f"intent_{timestamp}.json")

    payload = {
        "timestamp": timestamp,
        "platform": platform,
        "user_id": user_id,
        "chat_id": chat_id,
        "intent": intent_description,
        "target_files": target_files,
        "status": "pending_host_pickup"
    }

    try:
        with open(tmp_path, 'w') as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp_path, file_path)
        logger.info(f"Dropped escalation payload to {file_path}. Councilor will notify via {(platform or 'unknown').capitalize()} on completion.")
        return (
            f"Escalation dispatched to the Councilor. "
            f"The result will be delivered via the originating platform when complete. "
            f"Intent file: intent_{timestamp}.json"
        )
    except Exception as e:
        error_msg = f"Failed to drop escalation payload: {e}"
        logger.error(error_msg)
        return error_msg
