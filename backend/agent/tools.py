"""
tools.py — Tool registry for Icarus L1 agent.

Defines all tools available to the ADK agent, including filesystem operations,
memory persistence, escalation/consultation, GitHub, Gmail, and worker dispatch.
"""

import os
import re
import asyncio
import logging
from datetime import datetime, timezone
from contextvars import ContextVar
from .esc_tool import (
    escalate_to_councilor,
    consult_councilor,
    check_mailbox,
    check_pending_upgrade,
    apply_pending_upgrade,
)
from .github_tools import GITHUB_TOOLS
from .orchestrator import dispatch_worker_task, get_worker_result
from .gmail_tools import gmail_list_messages, gmail_get_message
from .calendar_tools import calendar_list_upcoming_events
from .telemetry_tools import get_telemetry_snapshot
from .websearch_tools import web_search, web_extract

logger = logging.getLogger(__name__)

# Context variables for differentiating between Discord/Telegram and users.
# These are set in backend/agent/processor.py:process_message
current_platform = ContextVar("current_platform", default="global")
current_user_id = ContextVar("current_user_id", default="system")
current_chat_id = ContextVar("current_chat_id", default="0")
current_conversation_id = ContextVar("current_conversation_id", default="legacy")
current_access_mode = ContextVar("current_access_mode", default="private")

# Legacy path for migration detection
MEMORY_LOG_PATH = "/workspace/memory/memory.log"

# The only host-persisted, writable location available to the L1 agent —
# bind-mounted to ./workspace/projects on the host. Everywhere else in the
# container is either read-only (:ro source mount) or writable-but-ephemeral
# (e.g. the container's own home directory) — writes there succeed, report
# success accurately, and then vanish on the next container recreation with
# no warning. That's exactly what happened before this jail existed: the
# model tried /resume.md, /workspace/resume.md, /home/resume.md (all
# correctly denied), then /home/icarus_user/resume.md — which is writable,
# so it succeeded and truthfully reported success, into a file that was one
# `docker compose up --force-recreate` away from disappearing. Enforced here
# as a capability, not a system-prompt instruction, same principle as the
# Councilor's escalation sandbox.
WORKSPACE_WRITE_ROOT = "/workspace/projects"


def _resolve_in_write_root(filepath: str) -> str | None:
    """Resolve filepath against WORKSPACE_WRITE_ROOT. Returns the resolved
    absolute path, or None if it escapes the root (via '..' or by being an
    absolute path elsewhere)."""
    candidate = os.path.abspath(os.path.join(WORKSPACE_WRITE_ROOT, filepath))
    root = os.path.abspath(WORKSPACE_WRITE_ROOT)
    if candidate != root and not candidate.startswith(root + os.sep):
        return None
    return candidate


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
    Note: This is NOT for diffs. Provide the complete final file content.
    filepath is relative to your workspace (e.g. 'resume.md') — this is the
    only location that actually persists across restarts."""
    logger.info(
        f"[tool:replace_file_contents] filepath={filepath!r} ({len(new_contents)} chars)"
    )
    resolved = _resolve_in_write_root(filepath)
    if resolved is None:
        return (
            f"Error: '{filepath}' is outside your writable workspace. "
            f"Use a path relative to your workspace, e.g. 'resume.md' or 'notes/resume.md'."
        )
    backup_path = f"{resolved}.bak"
    try:
        os.makedirs(os.path.dirname(resolved), exist_ok=True)
        if os.path.exists(resolved):
            os.replace(resolved, backup_path)
        with open(resolved, "w") as f:
            f.write(new_contents)
        logger.info(f"[tool:replace_file_contents] successfully wrote {resolved!r}")
        return f"Successfully updated {filepath}"
    except Exception as e:
        if os.path.exists(backup_path):
            os.replace(backup_path, resolved)
        logger.error(f"[tool:replace_file_contents] error: {e}")
        return f"Error updating {filepath}: {str(e)}"


def request_create_file(filepath: str, contents: str) -> str:
    """Creates a new file with the provided contents. filepath is relative
    to your workspace (e.g. 'resume.md') — this is the only location that
    actually persists across restarts; anywhere else either isn't writable
    or is writable-but-ephemeral and will silently disappear later."""
    logger.info(f"[tool:request_create_file] filepath={filepath!r}")
    resolved = _resolve_in_write_root(filepath)
    if resolved is None:
        return (
            f"Error: '{filepath}' is outside your writable workspace. "
            f"Use a path relative to your workspace, e.g. 'resume.md' or 'notes/resume.md'."
        )
    try:
        os.makedirs(os.path.dirname(resolved), exist_ok=True)
        if os.path.exists(resolved):
            return f"Error: File {filepath} already exists. Use replace_file_contents to overwrite."
        with open(resolved, "w") as f:
            f.write(contents)
        logger.info(f"[tool:request_create_file] successfully created {resolved!r}")
        return f"Successfully created {filepath}"
    except Exception as e:
        logger.error(f"[tool:request_create_file] error: {e}")
        return f"Error creating file {filepath}: {str(e)}"


async def recall(query: str, limit: int = 8) -> str:
    """Search the full message transcript for anything relevant to `query` —
    every message ever exchanged on this platform with this user, not just
    what got explicitly saved via append_memory. Use this when you're asked
    about something you might not remember, or need to check whether you (or
    a background worker) already mentioned something earlier — e.g. "did you
    already tell me about that email", "what did we decide about X last week".
    This is a deliberate lookup, separate from the memory that's already
    injected into your context automatically — reach for it when the answer
    might be buried further back than your current context reaches.

    Args:
        query: What to search for.
        limit: Max results to return (default 8).
    """
    platform = current_platform.get()
    user_id = current_user_id.get()
    conversation_id = current_conversation_id.get()
    logger.info(
        f"[tool:recall] platform={platform!r} user_id={user_id!r} "
        f"conversation={conversation_id!r} query={query!r}"
    )

    try:
        from backend.agent.transcript_repo import search

        results = await search(
            platform=platform,
            user_id=user_id,
            query=query,
            limit=limit,
            conversation_id=conversation_id,
        )
        if not results:
            return "No matching messages found in the transcript."

        lines = [f"[{r.created_at}] {r.role}: {r.content}" for r in results]
        return "Transcript search results:\n" + "\n".join(lines)
    except Exception as e:
        logger.error(f"[tool:recall] error: {e}")
        return f"Error searching transcript: {str(e)}"


async def list_tracked_items(item_type: str = "") -> str:
    """List structured, stateful items tracked on your behalf — job
    applications and bills extracted from triaged email, plus scored job
    opportunities from job_scout (see dispatch_worker_task's tasks:job_scout
    stream) — most recently updated first. Use this for "what's outstanding",
    "where do things stand with my job search", "what bills are due", "what
    opportunities have you scored" — it reads current state directly rather
    than trying to reconstruct it from scattered email mentions.

    Args:
        item_type: Optional filter — "job_application" (applied to),
            "job_opportunity" (scored, not yet applied to), or "bill". Leave
            empty to list everything.
    """
    if current_access_mode.get() == "server":
        return "Private tracked items are unavailable in server channels."

    platform = current_platform.get()
    user_id = current_user_id.get()
    logger.info(f"[tool:list_tracked_items] platform={platform!r} user_id={user_id!r} item_type={item_type!r}")

    try:
        from backend.agent.tracked_items_repo import list_items

        items = await list_items(platform=platform, user_id=user_id, item_type=item_type or None)
        if not items:
            return "No tracked items found."

        lines = []
        for i in items:
            due = f", due {i.due_at}" if i.due_at else ""
            urgency = f" [{i.urgency}]" if i.urgency else ""
            lines.append(f"- ({i.item_type}) {i.entity_key}: {i.state}{due}{urgency} — {i.summary or ''}".rstrip())
        return "Tracked items:\n" + "\n".join(lines)
    except Exception as e:
        logger.error(f"[tool:list_tracked_items] error: {e}")
        return f"Error listing tracked items: {str(e)}"


def append_memory(entry: str, visibility: str = "private") -> str:
    """Appends a timestamped entry to the persistent memory database.
    Use this to record decisions made, operator preferences discovered, task outcomes,
    recurring errors, or any context worth retaining across sessions.
    The memory persists across container restarts.

    Args:
        entry: The memory entry to store.
        visibility: 'public' to store as global (visible to all contexts),
                    'private' to store scoped to the current platform/user (default).
                    In an untrusted server context, global visibility is
                    silently downgraded to private — it's never trusted with
                    an entry that would surface in the operator's own DMs.
    """
    logger.info(f"[tool:append_memory] entry={entry!r} visibility={visibility!r}")

    platform = current_platform.get()
    user_id = current_user_id.get()
    conversation_id = current_conversation_id.get()
    chat_id = current_chat_id.get()
    access_mode = current_access_mode.get()

    # Normalize visibility: if entry contains [GLOBAL] tag, treat as public
    wants_global = visibility == "public" or re.search(r"\[GLOBAL\]", entry, re.IGNORECASE)
    entry = re.sub(r"\[GLOBAL\]", "", entry, flags=re.IGNORECASE).strip()

    if wants_global and access_mode == "server":
        # Global-visibility entries are injected into every future context,
        # including the operator's private DMs (see memory_repo.retrieve_relevant).
        # A server channel is untrusted input — enforce this as a capability,
        # not a prompt instruction, same reasoning as WORKSPACE_WRITE_ROOT
        # above: an untrusted server member could otherwise plant a [GLOBAL]
        # entry that later surfaces inside the operator's own conversations.
        resolved_visibility = "private"
        logger.warning(
            f"[tool:append_memory] blocked global-visibility write from server "
            f"context (platform={platform!r} user={user_id!r}); stored as private"
        )
    elif wants_global:
        resolved_visibility = "global"
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
                    conversation_id=conversation_id,
                    visibility=resolved_visibility,
                    source="agent",
                    chat_id=chat_id,
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
                    conversation_id=conversation_id,
                    visibility=resolved_visibility,
                    source="agent",
                    chat_id=chat_id,
                )
            )
            logger.info(f"[tool:append_memory] stored entry id={row_id}")
            return f"Memory logged (id={row_id}): {entry}"
    except Exception as e:
        logger.error(f"[tool:append_memory] error: {e}")
        return f"Error logging memory: {str(e)}"


def get_time() -> dict:
    """Return the current UTC time from the system clock.

    This reads directly from the OS clock — no in-memory sync, no drift.
    Returns ISO 8601 timestamp and epoch float for programmatic use.

    Returns:
        dict with keys:
            - utc_iso: ISO 8601 UTC timestamp (e.g. "2025-01-15T14:30:00Z")
            - epoch_float: Unix epoch as a float (seconds since 1970-01-01)
    """
    now = datetime.now(timezone.utc)
    return {
        "utc_iso": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "epoch_float": now.timestamp(),
    }


# Master tool registry for the Google ADK
ICARUS_TOOLS = [
    read_file,
    list_directory,
    replace_file_contents,
    request_create_file,
    append_memory,
    recall,
    list_tracked_items,
    check_mailbox,
    consult_councilor,
    escalate_to_councilor,
    check_pending_upgrade,
    apply_pending_upgrade,
    dispatch_worker_task,
    get_worker_result,
    gmail_list_messages,
    gmail_get_message,
    calendar_list_upcoming_events,
    get_telemetry_snapshot,
    web_search,
    web_extract,
    get_time,
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
    recall,
    list_tracked_items,
    check_mailbox,
    consult_councilor,
    get_telemetry_snapshot,
    web_search,
    web_extract,
    get_time,
    github_get_repo_info,
    github_list_repos,
    github_read_file,
    github_list_issues,
    github_read_issue,
]
