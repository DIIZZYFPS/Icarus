"""Stable privacy scopes for conversational state.

A conversation scope is deliberately separate from the transport's raw chat ID.
Discord DMs are private to the operator; server conversations are public within
one guild channel (or thread, whose channel ID is distinct).
"""


def build_conversation_id(
    *,
    platform: str,
    user_id: str,
    chat_id: str | None,
    guild_id: str | None = None,
) -> str:
    """Return the storage key used for short-term and durable chat history."""
    platform = str(platform)
    user_id = str(user_id)

    if platform == "discord":
        if guild_id is None:
            return f"discord:dm:{user_id}"
        if chat_id is None:
            raise ValueError("Discord server conversations require a channel ID")
        return f"discord:server:{guild_id}:channel:{chat_id}"

    if chat_id is not None:
        return f"{platform}:chat:{chat_id}"
    return f"{platform}:user:{user_id}"


def is_server_conversation(conversation_id: str | None) -> bool:
    """Return whether a scope belongs to a Discord server conversation."""
    return bool(conversation_id and conversation_id.startswith("discord:server:"))
