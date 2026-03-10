import os
import json
import asyncio
import logging

logger = logging.getLogger(__name__)

IPC_DIR = "/workspace/ipc/mail"
HEARTBEAT_INTERVAL = 15  # seconds between mailbox polls


async def _poll_mailbox():
    """Scan for unacked Councilor responses and wake Icarus for each one."""
    if not os.path.isdir(IPC_DIR):
        return

    unacked = []
    for fname in os.listdir(IPC_DIR):
        if not fname.endswith(".response.json"):
            continue
        if fname.startswith("intent_") or fname.startswith("consult_"):
            stem = fname[:-len(".response.json")]
        else:
            continue
        ack_path = os.path.join(IPC_DIR, f"{stem}.acked")
        if os.path.exists(ack_path):
            continue
        try:
            with open(os.path.join(IPC_DIR, fname)) as f:
                data = json.load(f)
            unacked.append((stem, ack_path, data))
        except Exception as e:
            logger.warning(f"[heartbeat] Could not read {fname}: {e}")

    if not unacked:
        return

    # Lazy import to avoid circular import at module load time
    from backend.routes.webhook import process_telegram_payload

    chat_id = int(os.environ.get("ALLOWED_CHAT_ID", "0"))
    if chat_id == 0:
        logger.warning("[heartbeat] ALLOWED_CHAT_ID not set — cannot deliver mailbox notifications")
        return

    for stem, ack_path, data in unacked:
        message = data.get("message", "(no message)")
        restart = data.get("restart_performed", False)

        if stem.startswith("intent_"):
            lines = [f"[MAILBOX] Councilor escalation complete ({stem})."]
        else:
            lines = [f"[MAILBOX] Councilor consultation response ({stem})."]
        lines.append(message)
        if restart:
            lines.append("[Container was restarted — source changes applied.]")
        prompt = "\n\n".join(lines)

        logger.info(f"[heartbeat] Delivering mailbox notification for {stem}")
        try:
            await process_telegram_payload(chat_id, prompt)
        except Exception as e:
            logger.error(f"[heartbeat] Failed to deliver notification for {stem}: {e}")
            continue

        # Mark as acked so this result isn't delivered again
        try:
            open(ack_path, 'w').close()
            logger.info(f"[heartbeat] Acked {stem}")
        except Exception as e:
            logger.warning(f"[heartbeat] Failed to write ack for {stem}: {e}")


async def run_heartbeat():
    """Background loop — polls the IPC mailbox every HEARTBEAT_INTERVAL seconds."""
    logger.info(f"[heartbeat] Started. Polling every {HEARTBEAT_INTERVAL}s.")
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL)
        try:
            await _poll_mailbox()
        except Exception as e:
            logger.error(f"[heartbeat] Unexpected poll error: {e}")
