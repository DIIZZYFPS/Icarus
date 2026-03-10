import os
import logging
import discord
from discord.ext import commands
from backend.agent.processor import process_message

logger = logging.getLogger(__name__)

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
DISCORD_ALLOWED_CHANNEL_ID = os.environ.get("DISCORD_ALLOWED_CHANNEL_ID")
DISCORD_OPERATOR_ID = int(os.environ.get("DISCORD_OPERATOR_ID", "0"))

class IcarusBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.allowed_channel_id = int(DISCORD_ALLOWED_CHANNEL_ID) if DISCORD_ALLOWED_CHANNEL_ID else None

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

def split_message(text: str, limit: int = 2000) -> list[str]:
    """Split text into chunks at newline boundaries where possible."""
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, limit)
        if split_at == -1:
            split_at = limit
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks

async def push_discord_message(channel_id: int, text: str):
    """Send a message to a Discord channel."""
    if not bot.is_ready():
        logger.warning("Discord bot not ready — cannot push message")
        return

    channel = bot.get_channel(channel_id)
    if channel:
        chunks = split_message(text)
        for chunk in chunks:
            await channel.send(chunk)
    else:
        logger.error(f"Discord channel {channel_id} not found")

async def handle_discord_payload(channel_id: int, user_id: str, text: str):
    """Process Discord message and send response."""
    response_text = await process_message("discord", user_id, text, chat_id=str(channel_id))
    await push_discord_message(channel_id, response_text)
