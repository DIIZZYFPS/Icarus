import os
import logging
from .esc_tool import escalate_to_councilor

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

# Master tool registry for the Google ADK
ICARUS_TOOLS = [
    read_file,
    list_directory,
    replace_file_contents,
    escalate_to_councilor
]
