"""
tools.py — Tool registry for Icarus L1 agent.

Defines all tools available to the ADK agent, including filesystem operations,
memory persistence, escalation/consultation, GitHub, Gmail, and worker dispatch.
"""

import os
import re
import asyncio
import logging
from contextvars import ContextVar
from .esc_tool import escalate_to_councilor, consult_councilor, check_mailbox
from .github_tools import GITHUB_TOOLS
from .orchestrator import dispatch_worker_task, get_worker_result
from .gmail_tools import gmail_list_messages, gmail_get_message
from .telemetry_tools import get_telemetry_snapshot

logger = logging.getLogger(__name__)

# Context variables for differentiating between Discord/Telegram and users.
# These are set in backend/agent/processor.py:process_message
current_platform = ContextVar("current_platform", default="global")
current_user_id = ContextVar("current_user_id", default="system")
current_chat_id = ContextVar("current_chat_id", default="0")

# Legacy path for migration detection
MEMORY_LOG_PATH = "/workspace/memory/memory.log"


def read_file(filepath: str) -> str:
    """Reads the contents of a file from the workspace."""
    logger.info(f"[tool:read_file] filepath={filepath!r}")
    try:
        with open(filepath, "r") as f:
            content = f.read()
        logger.info(f"[tool:read_file] read {len(content)} chars from {filepath!r}")
        return content
    except Exception as e:
        logger.error(f"[tool:read_file] error: {e}")
        return f"Error reading file {filepath}: {str(e)}"


def list_directory(directory: str) -> list[str]:
    """Lists the contents of a directory."""
    logger.info(f"[tool:list_directory] directory={directory!r}")
    try:
        entries = os.listdir(directory)
        logger.info(f"[tool:list_directory] {len(entries)} entries in {directory!r}")
        return entries
    except Exception as e:
        logger.error(f"[tool:list_directory] error: {e}")
        return [f"Error listing directory {directory}: {str(e)}"]


def replace_file_contents(filepath: str, new_contents: str) -> str:
    """Safely replaces the entire contents of a file with the provided text.
    Note: This is NOT for diffs. Provide the complete final file content."""
    logger.info(
        f"[tool:replace_file_contents] filepath={filepath!r} ({len(new_contents)} chars)"
    )
    backup_path = f"{filepath}.bak"
    try:
        if os.path.exists(filepath):
            os.replace(filepath, backup_path)
        with open(filepath, "w") as f:
            f.write(new_contents)
        logger.info(f"[tool:replace_file_contents] successfully wrote {filepath!r}")
        return f"Successfully updated {filepath}"
    except Exception as e:
        if os.path.exists(backup_path):
            os.replace(backup_path, filepath)
        logger.error(f"[tool:replace_file_contents] error: {e}")
        return f"Error updating {filepath}: {str(e)}"


def request_create_file(filepath: str, contents: str) -> str:
    """Creates a new file with the provided contents.
    The tool operates exclusively within the container's accessible paths."""
    logger.info(f"[tool:request_create_file] filepath={filepath!r}")
    try:
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        if os.path.exists(filepath):
            return f"Error: File {filepath} already exists. Use replace_file_contents to overwrite."
        with open(filepath, "w") as f:
            f.write(contents)
        logger.info(f"[tool:request_create_file] successfully created {filepath!r}")
        return f"Successfully created {filepath}"
    except Exception as e:
        logger.error(f"[tool:request_create_file] error: {e}")
        return f"Error creating file {filepath}: {str(e)}"


def append_memory(entry: str, visibility: str = "private") -> str:
    """Appends a timestamped entry to the persistent memory database.
    Use this to record decisions made, operator preferences discovered, task outcomes,
    recurring errors, or any context worth retaining across sessions.
    The memory persists across container restarts.

    Args:
        entry: The memory entry to store.
        visibility: 'public' to store as global (visible to all contexts),
                    'private' to store scoped to the current platform/user (default).
    """
    logger.info(f"[tool:append_memory] entry={entry!r} visibility={visibility!r}")

    platform = current_platform.get()
    user_id = current_user_id.get()

    # Normalize visibility: if entry contains [GLOBAL] tag, treat as public
    if visibility == "public" or re.search(r"\[GLOBAL\]", entry, re.IGNORECASE):
        resolved_visibility = "global"
        entry = re.sub(r"\[GLOBAL\]", "", entry, flags=re.IGNORECASE).strip()
    else:
        resolved_visibility = "private"

    try:
        from backend.agent.memory_repo import store_entry

        # store_entry is async — run it in the current event loop
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # We're in an async context (ADK tool call) — use create_task pattern
            future = asyncio.ensure_future(
                store_entry(
                    platform=platform,
                    user_id=user_id,
                    entry=entry,
                    visibility=resolved_visibility,
                    source="agent",
                )
            )
            # Can't await here since this is sync — but the task will execute
            logger.info(f"[tool:append_memory] scheduled async store for {platform}:{user_id}")
            return f"Memory logged: [{platform}:{user_id}] {entry}"
        else:
            row_id = loop.run_until_complete(
                store_entry(
                    platform=platform,
                    user_id=user_id,
                    entry=entry,
                    visibility=resolved_visibility,
                    source="agent",
                )
            )
            logger.info(f"[tool:append_memory] stored entry id={row_id}")
            return f"Memory logged (id={row_id}): {entry}"
    except Exception as e:
        logger.error(f"[tool:append_memory] error: {e}")
        return f"Error logging memory: {str(e)}"


# Master tool registry for the Google ADK
ICARUS_TOOLS = [
    read_file,
    list_directory,
    replace_file_contents,
    request_create_file,
    append_memory,
    check_mailbox,
    consult_councilor,
    escalate_to_councilor,
    dispatch_worker_task,
    get_worker_result,
    gmail_list_messages,
    gmail_get_message,
    get_telemetry_snapshot,
    *GITHUB_TOOLS,
]

# Read-only subset: used for Discord server channels (untrusted context).
from .github_tools import (
    github_get_repo_info,
    github_list_repos,
    github_read_file,
    github_list_issues,
    github_read_issue,
)

ICARUS_READONLY_TOOLS = [
    read_file,
    list_directory,
    append_memory,
    check_mailbox,
    consult_councilor,
    get_telemetry_snapshot,
    github_get_repo_info,
    github_list_repos,
    github_read_file,
    github_list_issues,
    github_read_issue,
]
