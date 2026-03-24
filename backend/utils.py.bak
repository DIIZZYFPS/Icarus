"""
utils.py — Shared utilities for the Icarus backend.

Consolidates duplicated helper functions used across multiple modules.
"""


def split_message(text: str, limit: int = 2000) -> list[str]:
    """Split text into chunks at newline boundaries where possible.

    Used by webhook.py, discord_bot.py, and other modules that need to
    respect platform message length limits.

    Args:
        text: The text to split.
        limit: Maximum character count per chunk (default 2000 for Discord).
    """
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
