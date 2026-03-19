"""
utils.py — Shared utilities for the Icarus backend.

Consolidates duplicated helper functions used across multiple modules.
"""

import subprocess
import shlex
import logging

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

def execute_shell_command(command: str, timeout: int = 30) -> dict:
    """
    Executes a shell command safely with timeout protection and input sanitization.

    Args:
        command: The shell command string to execute.
        timeout: Maximum execution time in seconds.

    Returns:
        A dictionary with 'stdout', 'stderr', 'returncode', and 'success' status.
    """
    # Sanitize and parse the command
    try:
        # Use shlex.split to handle shell argument parsing safely
        # Note: This prevents shell injection because it doesn't invoke a shell by default
        args = shlex.split(command)
    except ValueError as e:
        return {
            "stdout": "",
            "stderr": f"Command parsing error: {str(e)}",
            "returncode": 1,
            "success": False
        }

    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
            "success": result.returncode == 0
        }
    except subprocess.TimeoutExpired as e:
        return {
            "stdout": e.stdout or "",
            "stderr": f"Command timed out after {timeout} seconds",
            "returncode": -1,
            "success": False
        }
    except Exception as e:
        return {
            "stdout": "",
            "stderr": f"Unexpected error: {str(e)}",
            "returncode": 1,
            "success": False
        }
