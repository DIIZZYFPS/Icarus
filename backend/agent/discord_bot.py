import os
import logging
import discord
from discord.ext import commands
from backend.agent.processor import process_message
from backend.utils import split_message

logger = logging.getLogger(__name__)

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
DISCORD_ALLOWED_CHANNEL_ID = os.environ.get("DISCORD_ALLOWED_CHANNEL_ID")
DISCORD_OPERATOR_ID = int(os.environ.get("DISCORD_OPERATOR_ID", "0"))


def _to_int_or_none(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

class IcarusBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        # Intentionally disabled: operator-only DM mode.
        self.allowed_channel_id = None

    async def on_ready(self):
        logger.info(f"Logged in as {self.user} (ID: {self.user.id}), (allowed_channel_id={self.allowed_channel_id})")
        logger.info("------")

    async def on_message(self, message):
        # Don't respond to ourselves
        if message.author == self.user:
            return

        is_dm = message.guild is None and message.author.id == DISCORD_OPERATOR_ID

        if is_dm:
            # DMs are treated as operator access — full permissions, no channel restriction.
            read_only = False
        else:
            # Server channel — must match the allowed channel, mention-only, always read-only.
            if not self.allowed_channel_id:
                return
            if message.channel.id != self.allowed_channel_id:
                return
            if self.user not in message.mentions:
                return
            read_only = True

        async with message.channel.typing():
            try:
                content = message.content
                if not is_dm:
                    # Strip the @mention prefix before sending to the model
                    content = content.replace(f'<@{self.user.id}>', '').replace(f'<@!{self.user.id}>', '').strip()
                # Prefix with author identity so the model knows who is speaking
                content = f"[User:{message.author.name} (id: {message.author.id})]: {content}"
                response = await process_message("discord", str(message.author.id), content, chat_id=str(message.channel.id), read_only=read_only)
                if response:
                    chunks = split_message(response)
                    for chunk in chunks:
                        await message.channel.send(chunk)
            except Exception as e:
                logger.error(f"Error processing Discord message: {e}")
                await message.channel.send(f"Error: {e}")

bot = IcarusBot()

async def run_discord_bot():
    if not DISCORD_BOT_TOKEN:
        logger.warning("DISCORD_BOT_TOKEN not set — skipping Discord bot")
        return

    logger.info("Starting Discord bot...")
    try:
        await bot.start(DISCORD_BOT_TOKEN)
    except Exception as e:
        logger.error(f"Failed to start Discord bot: {e}")

# split_message imported from backend.utils

async def push_discord_message(channel_id: int | None, text: str, user_id: str | None = None):
    """Send a message to a Discord channel, or DM a user as fallback."""
    if not bot.is_ready():
        logger.warning("Discord bot not ready — cannot push message")
        return

    target_channel = None
    numeric_channel_id = _to_int_or_none(channel_id)
    if numeric_channel_id is not None:
        target_channel = bot.get_channel(numeric_channel_id)
        if target_channel is None:
            try:
                target_channel = await bot.fetch_channel(numeric_channel_id)
            except Exception as e:
                logger.warning(f"Failed to fetch Discord channel {numeric_channel_id}: {e}")

    if target_channel:
        chunks = split_message(text)
        for chunk in chunks:
            await target_channel.send(chunk)
        return

    numeric_user_id = _to_int_or_none(user_id)
    if numeric_user_id is None:
        logger.error(
            f"Discord destination unresolved (channel_id={channel_id}, user_id={user_id})"
        )
        return

    user = bot.get_user(numeric_user_id)
    if user is None:
        try:
            user = await bot.fetch_user(numeric_user_id)
        except Exception as e:
            logger.error(f"Discord user {numeric_user_id} not found for DM fallback: {e}")
            return

    try:
        dm = user.dm_channel or await user.create_dm()
        chunks = split_message(text)
        for chunk in chunks:
            await dm.send(chunk)
        logger.info(f"Delivered Discord mailbox notification via DM to user {numeric_user_id}")
    except Exception as e:
        logger.error(f"Failed to deliver Discord DM to user {numeric_user_id}: {e}")

async def handle_discord_payload(channel_id: int, user_id: str, text: str):
    """Process Discord message and send response."""
    response_text = await process_message("discord", user_id, text, chat_id=str(channel_id))
    await push_discord_message(channel_id, response_text, user_id=user_id)


async def relay_councilor_mailbox_to_operator(
    mailbox_text: str,
    operator_user_id: str,
    chat_id: str | None = None,
):
    """Hydrate the agent with Councilor mailbox output, then DM the operator."""
    normalized_user_id = str(operator_user_id).strip() if operator_user_id is not None else ""
    if not normalized_user_id:
        logger.error("Cannot relay Councilor mailbox output: missing Discord operator user ID")
        return

    effective_chat_id = str(chat_id) if chat_id is not None else "0"
    hydration_prompt = (
        "[System: Councilor mailbox response received. Analyze it, decide what to do next, "
        "and provide the operator with a concise actionable update.]\n\n"
        f"{mailbox_text}"
    )

    response_text = await process_message(
        "discord",
        normalized_user_id,
        hydration_prompt,
        chat_id=effective_chat_id,
        read_only=False,
    )

    # If the model returns an empty string, still deliver the original mailbox payload.
    final_text = response_text if response_text else mailbox_text
    await push_discord_message(None, final_text, user_id=normalized_user_id)


async def relay_email_notification_to_operator(
    email_text: str,
    operator_user_id: str,
    chat_id: str | None = None,
):
    """Hydrate the agent with email triage output, then DM the operator."""
    normalized_user_id = str(operator_user_id).strip() if operator_user_id is not None else ""
    if not normalized_user_id:
        logger.error("Cannot relay email notification: missing Discord operator user ID")
        return

    effective_chat_id = str(chat_id) if chat_id is not None else "0"
    hydration_prompt = (
        "[System: New email classified. Analyze the classification, decide if any immediate action is needed, "
        "and provide the operator with a concise actionable update in your voice.]\n\n"
        f"{email_text}"
    )

    from backend.agent.processor import process_message
    response_text = await process_message(
        "discord",
        normalized_user_id,
        hydration_prompt,
        chat_id=effective_chat_id,
        read_only=False,
    )

    # If the model returns an empty string, still deliver the original classification.
    final_text = response_text if response_text else f"📧 **Email Alert:**\n{email_text}"
    await push_discord_message(None, final_text, user_id=normalized_user_id)
