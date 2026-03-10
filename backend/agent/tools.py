import os
import logging
from datetime import datetime, timezone
from .esc_tool import escalate_to_councilor, consult_councilor, check_mailbox
from .github_tools import GITHUB_TOOLS

MEMORY_LOG_PATH = "/workspace/memory/memory.log"

logger = logging.getLogger(__name__)

def read_file(filepath: str) -> str:
    """Reads the contents of a file from the workspace."""
    logger.info(f"[tool:read_file] filepath={filepath!r}")
    try:
        with open(filepath, 'r') as f:
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
    logger.info(f"[tool:replace_file_contents] filepath={filepath!r} ({len(new_contents)} chars)\n--- new contents ---\n{new_contents[:500]}{'...[truncated]' if len(new_contents) > 500 else ''}\n--- end ---")
    backup_path = f"{filepath}.bak"
    try:
        # Create backup using atomic replace
        if os.path.exists(filepath):
            os.replace(filepath, backup_path)

        with open(filepath, 'w') as f:
            f.write(new_contents)

        logger.info(f"[tool:replace_file_contents] successfully wrote {filepath!r}")
        return f"Successfully updated {filepath}"
    except Exception as e:
        # Rollback on failure
        if os.path.exists(backup_path):
            os.replace(backup_path, filepath)
        logger.error(f"[tool:replace_file_contents] error: {e}")
        return f"Error updating {filepath}: {str(e)}"

def request_create_file(filepath: str, contents: str) -> str:
    """Creates a new file with the provided contents.
    The tool operates exclusively within the container's accessible paths."""
    logger.info(f"[tool:request_create_file] filepath={filepath!r}")
    try:
        # Ensure the directory exists
        os.makedirs(os.path.dirname(filepath), exist_ok=True)

        if os.path.exists(filepath):
             return f"Error: File {filepath} already exists. Use replace_file_contents to overwrite."

        with open(filepath, 'w') as f:
            f.write(contents)

        logger.info(f"[tool:request_create_file] successfully created {filepath!r}")
        return f"Successfully created {filepath}"
    except Exception as e:
        logger.error(f"[tool:request_create_file] error: {e}")
        return f"Error creating file {filepath}: {str(e)}"

def append_memory(entry: str) -> str:
    """Appends a timestamped entry to the persistent memory log at /workspace/memory/memory.log.
    Use this to record decisions made, operator preferences discovered, task outcomes,
    recurring errors, or any context worth retaining across sessions.
    The log persists across container restarts."""
    logger.info(f"[tool:append_memory] entry={entry!r}")
    try:
        os.makedirs(os.path.dirname(MEMORY_LOG_PATH), exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        line = f"[{timestamp}] {entry}\n"
        with open(MEMORY_LOG_PATH, 'a') as f:
            f.write(line)
        logger.info(f"[tool:append_memory] wrote entry to {MEMORY_LOG_PATH!r}")
        return f"Memory logged: {line.strip()}"
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
    *GITHUB_TOOLS
]

# Read-only subset: used for Discord server channels (untrusted context).
# Excludes all write/execute tools: replace_file_contents, request_create_file,
# escalate_to_councilor, and all GitHub mutation tools.
from .github_tools import (
    github_get_repo_info,
    github_list_repos,
    github_read_file,
    github_list_issues,
)

ICARUS_READONLY_TOOLS = [
    read_file,
    list_directory,
    append_memory,
    check_mailbox,
    consult_councilor,
    github_get_repo_info,
    github_list_repos,
    github_read_file,
    github_list_issues,
]
