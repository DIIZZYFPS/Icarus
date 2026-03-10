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

    # Lazy imports to avoid circular import at module load time
    from backend.routes.webhook import handle_telegram_payload
    from backend.agent.discord_bot import handle_discord_payload

    telegram_chat_id = int(os.environ.get("ALLOWED_CHAT_ID", "0"))
    discord_channel_id = int(os.environ.get("DISCORD_ALLOWED_CHANNEL_ID", "0"))

    if telegram_chat_id == 0 and discord_channel_id == 0:
        logger.warning("[heartbeat] No delivery channels configured — cannot deliver mailbox notifications")
        return

    for stem, ack_path, data in unacked:
        message = data.get("message", "(no message)")
        restart = data.get("restart_performed", False)
        platform = data.get("platform")
        user_id = data.get("user_id")
        chat_id = data.get("chat_id")

        if stem.startswith("intent_"):
            lines = [f"[MAILBOX] Councilor escalation complete ({stem})."]
        else:
            lines = [f"[MAILBOX] Councilor consultation response ({stem})."]
        lines.append(message)
        if restart:
            lines.append("[Container was restarted — source changes applied.]")
        prompt = "\n\n".join(lines)

        logger.info(f"[heartbeat] Delivering mailbox notification for {stem} to {platform}:{chat_id}")
        
        # Targeted delivery based on the platform that initiated the request
        if platform == "telegram" and chat_id:
            try:
                await handle_telegram_payload(int(chat_id), str(user_id), prompt)
            except Exception as e:
                logger.error(f"[heartbeat] Failed to deliver Telegram notification for {stem}: {e}")
        elif platform == "discord" and chat_id:
            try:
                await handle_discord_payload(int(chat_id), str(user_id), prompt)
            except Exception as e:
                logger.error(f"[heartbeat] Failed to deliver Discord notification for {stem}: {e}")
        else:
            # Fallback for legacy payloads or when platform/chat info is missing
            telegram_chat_id = int(os.environ.get("ALLOWED_CHAT_ID", "0"))
            discord_channel_id = int(os.environ.get("DISCORD_ALLOWED_CHANNEL_ID", "0"))

            if platform != "discord" and telegram_chat_id != 0:
                try:
                    await handle_telegram_payload(telegram_chat_id, str(telegram_chat_id), prompt)
                except Exception as e:
                    logger.error(f"[heartbeat] Failed to deliver fallback Telegram notification for {stem}: {e}")
            if platform != "telegram" and discord_channel_id != 0:
                try:
                    await handle_discord_payload(discord_channel_id, str(discord_channel_id), prompt)
                except Exception as e:
                    logger.error(f"[heartbeat] Failed to deliver fallback Discord notification for {stem}: {e}")

        # Mark as acked so this result isn't re-delivered if we crash mid-cleanup,
        # then delete both files to keep the mail directory clean.
        response_path = os.path.join(IPC_DIR, f"{stem}.response.json")
        try:
            open(ack_path, 'w').close()
            logger.info(f"[heartbeat] Acked {stem}")
        except Exception as e:
            logger.warning(f"[heartbeat] Failed to write ack for {stem}: {e}")
            continue

        for path in (response_path, ack_path):
            try:
                os.remove(path)
            except Exception as e:
                logger.warning(f"[heartbeat] Failed to remove {path}: {e}")


async def run_heartbeat():
    """Background loop — polls the IPC mailbox every HEARTBEAT_INTERVAL seconds."""
    logger.info(f"[heartbeat] Started. Polling every {HEARTBEAT_INTERVAL}s.")
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL)
        try:
            await _poll_mailbox()
        except Exception as e:
            logger.error(f"[heartbeat] Unexpected poll error: {e}")
