import os
import logging
import discord
from discord.ext import commands
from backend.agent.conversation_context import build_conversation_id
from backend.agent.notification_repo import bind_delivery, record_notification
from backend.agent.processor import process_message
from backend.utils import split_message

logger = logging.getLogger(__name__)

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
DISCORD_ALLOWED_CHANNEL_ID = os.environ.get("DISCORD_ALLOWED_CHANNEL_ID")
DISCORD_OPERATOR_ID = int(os.environ.get("DISCORD_OPERATOR_ID", "0"))

# Discord doesn't always set Attachment.content_type (depends on client/CDN
# response), so fall back to the filename extension.
_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")


def _to_int_or_none(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_image_attachment(attachment) -> bool:
    if attachment.content_type and attachment.content_type.startswith("image/"):
        return True
    return attachment.filename.lower().endswith(_IMAGE_EXTENSIONS)


def _extract_image_urls(message) -> list[str] | None:
    urls = [a.url for a in message.attachments if _is_image_attachment(a)]
    return urls or None


class IcarusBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        # Server-channel access is opt-in through the deployment environment.
        self.allowed_channel_id = _to_int_or_none(DISCORD_ALLOWED_CHANNEL_ID)

    async def on_ready(self):
        logger.info(
            f"Logged in as {self.user} (ID: {self.user.id}), "
            f"(allowed_channel_id={self.allowed_channel_id})"
        )
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

        conversation_id = build_conversation_id(
            platform="discord",
            user_id=str(message.author.id),
            chat_id=str(message.channel.id),
            guild_id=str(message.guild.id) if message.guild is not None else None,
        )
        reply_to_message_id = None
        if message.reference is not None and message.reference.message_id is not None:
            reply_to_message_id = str(message.reference.message_id)

        async with message.channel.typing():
            # Lazily created on the first tool call, then edited per call —
            # nothing is sent for plain replies that never use a tool, so the
            # common case is unchanged. Deleted before the real answer goes
            # out, success or failure, so it never lingers as stale status.
            status_message = {"msg": None}

            async def on_activity(text: str):
                if status_message["msg"] is None:
                    status_message["msg"] = await message.channel.send(text)
                else:
                    try:
                        await status_message["msg"].edit(content=text)
                    except Exception as e:
                        logger.warning(f"Failed to update status message: {e}")

            try:
                content = message.content
                if not is_dm:
                    # Strip the @mention prefix before sending to the model
                    content = content.replace(f'<@{self.user.id}>', '').replace(f'<@!{self.user.id}>', '').strip()
                # Prefix with author identity so the model knows who is speaking
                content = f"[User:{message.author.name} (id: {message.author.id})]: {content}"
                image_urls = _extract_image_urls(message)
                response = await process_message(
                    "discord",
                    str(message.author.id),
                    content,
                    chat_id=str(message.channel.id),
                    read_only=read_only,
                    on_activity=on_activity,
                    conversation_id=conversation_id,
                    reply_to_message_id=reply_to_message_id,
                    image_urls=image_urls,
                )
                if response:
                    chunks = split_message(response)
                    for index, chunk in enumerate(chunks):
                        # Server channels can have several conversations going
                        # at once — reply-threading the first chunk to the
                        # triggering message makes it obvious who Icarus is
                        # answering. DMs are already 1:1, so plain sends there.
                        if index == 0 and message.guild is not None:
                            await message.reply(chunk, mention_author=False)
                        else:
                            await message.channel.send(chunk)
            except Exception as e:
                logger.error(f"Error processing Discord message: {e}")
                if message.guild is not None:
                    await message.reply(f"Error: {e}", mention_author=False)
                else:
                    await message.channel.send(f"Error: {e}")
            finally:
                if status_message["msg"] is not None:
                    try:
                        await status_message["msg"].delete()
                    except Exception as e:
                        logger.warning(f"Failed to delete status message: {e}")


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


async def _resolve_discord_target(channel_id: int | None, user_id: str | None):
    """Resolve a channel directly, or create the operator's DM channel."""
    numeric_channel_id = _to_int_or_none(channel_id)
    if numeric_channel_id is not None:
        target_channel = bot.get_channel(numeric_channel_id)
        if target_channel is None:
            try:
                target_channel = await bot.fetch_channel(numeric_channel_id)
            except Exception as e:
                logger.warning(f"Failed to fetch Discord channel {numeric_channel_id}: {e}")
        if target_channel is not None:
            return target_channel

    numeric_user_id = _to_int_or_none(user_id)
    if numeric_user_id is None:
        logger.error(
            f"Discord destination unresolved (channel_id={channel_id}, user_id={user_id})"
        )
        return None

    user = bot.get_user(numeric_user_id)
    if user is None:
        try:
            user = await bot.fetch_user(numeric_user_id)
        except Exception as e:
            logger.error(f"Discord user {numeric_user_id} not found for DM fallback: {e}")
            return None

    try:
        return user.dm_channel or await user.create_dm()
    except Exception as e:
        logger.error(f"Failed to create Discord DM for user {numeric_user_id}: {e}")
        return None


async def push_discord_message(
    channel_id: int | None,
    text: str,
    user_id: str | None = None,
    reply_to_message_id: str | None = None,
) -> list[str]:
    """Send a message and return the sent Discord message IDs."""
    if not bot.is_ready():
        logger.warning("Discord bot not ready — cannot push message")
        return []

    target_channel = await _resolve_discord_target(channel_id, user_id)
    if target_channel is None:
        return []

    sent_ids = []
    for index, chunk in enumerate(split_message(text)):
        send_kwargs = {}
        if index == 0 and reply_to_message_id is not None:
            send_kwargs["reference"] = discord.MessageReference(
                message_id=int(reply_to_message_id),
                channel_id=target_channel.id,
            )
        sent = await target_channel.send(chunk, **send_kwargs)
        sent_ids.append(str(sent.id))
    return sent_ids


async def handle_discord_payload(channel_id: int, user_id: str, text: str):
    """Process Discord message and send response."""
    conversation_id = build_conversation_id(
        platform="discord",
        user_id=str(user_id),
        chat_id=str(channel_id),
    )
    response_text = await process_message(
        "discord",
        user_id,
        text,
        chat_id=str(channel_id),
        conversation_id=conversation_id,
    )
    await push_discord_message(channel_id, response_text, user_id=user_id)


async def _relay_notification(
    *,
    notification_type: str,
    operator_user_id: str,
    chat_id: str | None,
    source_id: str | None,
    hydration_prompt: str,
    fallback_text: str,
):
    normalized_user_id = str(operator_user_id).strip() if operator_user_id is not None else ""
    if not normalized_user_id:
        logger.error("Cannot relay notification: missing Discord operator user ID")
        return

    target = await _resolve_discord_target(None, normalized_user_id)
    if target is None:
        return

    dm_channel_id = str(target.id)
    effective_chat_id = str(chat_id) if chat_id is not None else dm_channel_id
    conversation_id = build_conversation_id(
        platform="discord",
        user_id=normalized_user_id,
        chat_id=dm_channel_id,
    )
    response_text = await process_message(
        "discord",
        normalized_user_id,
        hydration_prompt,
        chat_id=effective_chat_id,
        conversation_id=conversation_id,
        read_only=False,
        skip_history=True,
    )

    final_text = response_text if response_text else fallback_text
    delivery_ids = await push_discord_message(int(dm_channel_id), final_text)
    if not delivery_ids:
        logger.warning(
            "Notification response was not delivered; pending context was not recorded "
            "for user=%s type=%s",
            normalized_user_id,
            notification_type,
        )
        return

    notification_id = await record_notification(
        conversation_id=conversation_id,
        notification_type=notification_type,
        source_id=source_id,
        content=final_text,
    )
    await bind_delivery(notification_id, delivery_ids)
    logger.info(
        "Delivered %s notification to Discord DM user=%s notification_id=%s",
        notification_type,
        normalized_user_id,
        notification_id,
    )


async def relay_councilor_mailbox_to_operator(
    mailbox_text: str,
    operator_user_id: str,
    chat_id: str | None = None,
    source_id: str | None = None,
):
    """Hydrate the agent with Councilor mailbox output, then DM the operator."""
    await _relay_notification(
        notification_type="councilor",
        operator_user_id=operator_user_id,
        chat_id=chat_id,
        source_id=source_id,
        hydration_prompt=(
            "[System: Councilor mailbox response received. Analyze it, decide what to do next, "
            "and provide the operator with a concise actionable update.]\n\n"
            f"{mailbox_text}"
        ),
        fallback_text=mailbox_text,
    )


async def relay_email_notification_to_operator(
    email_text: str,
    operator_user_id: str,
    chat_id: str | None = None,
    source_id: str | None = None,
):
    """Hydrate the agent with email triage output, then DM the operator."""
    await _relay_notification(
        notification_type="email",
        operator_user_id=operator_user_id,
        chat_id=chat_id,
        source_id=source_id,
        hydration_prompt=(
            "[System: New email classified. Analyze the classification, decide if any immediate action is needed, "
            "and provide the operator with a concise actionable update in your voice.]\n\n"
            f"{email_text}"
        ),
        fallback_text=f"📧 **Email Alert:**\n{email_text}",
    )


async def relay_job_notification_to_operator(
    job_text: str,
    operator_user_id: str,
    chat_id: str | None = None,
    source_id: str | None = None,
):
    """Hydrate the agent with a scored job opportunity, then DM the operator."""
    await _relay_notification(
        notification_type="job",
        operator_user_id=operator_user_id,
        chat_id=chat_id,
        source_id=source_id,
        hydration_prompt=(
            "[System: A new job opportunity was scored. Analyze the opportunity, explain why it matters, "
            "and provide the operator with a concise actionable update in your voice.]\n\n"
            f"{job_text}"
        ),
        fallback_text=f"💼 **Job Opportunity:**\n{job_text}",
    )
