import os
import contextvars
from .esc_tool import escalate_to_councilor

# Captures respond() calls within a single async task — no cross-request contamination
_respond_ctx: contextvars.ContextVar[str] = contextvars.ContextVar('respond_ctx', default='')

def respond(message: str) -> str:
    """Send a plain text reply to the user. Call this when no file operation or escalation is needed."""
    _respond_ctx.set(message)
    return message

def read_file(filepath: str) -> str:
    """Reads the contents of a file from the workspace."""
    try:
        with open(filepath, 'r') as f:
            return f.read()
    except Exception as e:
        return f"Error reading file {filepath}: {str(e)}"

def list_directory(directory: str) -> list[str]:
    """Lists the contents of a directory."""
    try:
        return os.listdir(directory)
    except Exception as e:
        return [f"Error listing directory {directory}: {str(e)}"]

def replace_file_contents(filepath: str, new_contents: str) -> str:
    """Safely replaces the entire contents of a file with the provided text.
    Note: This is NOT for diffs. Provide the complete final file content."""
    backup_path = f"{filepath}.bak"
    try:
        # Create backup using atomic replace
        if os.path.exists(filepath):
            os.replace(filepath, backup_path)

        with open(filepath, 'w') as f:
            f.write(new_contents)

        return f"Successfully updated {filepath}"
    except Exception as e:
        # Rollback on failure
        if os.path.exists(backup_path):
            os.replace(backup_path, filepath)
        return f"Error updating {filepath}: {str(e)}"

# Master tool registry for the Google ADK
ICARUS_TOOLS = [
    respond,
    read_file,
    list_directory,
    replace_file_contents,
    escalate_to_councilor
]
