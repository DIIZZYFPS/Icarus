"""
esc_tool.py — IPC bridge between L1 (Icarus) and L2 (The Councilor).

All communication uses Redis pub/sub instead of filesystem-based IPC.
  - escalate_to_councilor() → publishes to Redis, non-blocking
  - consult_councilor()     → publishes and waits for response via Redis subscription
  - check_mailbox()         → reads undelivered responses from Redis list
"""

import json
import time
import asyncio
import logging
from typing import List

logger = logging.getLogger(__name__)

CONSULTATION_TIMEOUT_SECONDS = 60  # Raised from 30s — API calls are faster but still need headroom


def check_mailbox() -> str:
    """Scan Redis for any undelivered Councilor responses.
    The heartbeat delivers these automatically every 15s, but call this
    to check immediately without waiting for the next cycle.
    Returns a summary of pending results, or confirms the mailbox is empty."""
    import asyncio
    from backend.agent.tools import current_access_mode

    if current_access_mode.get() == "server":
        return "The private Councilor mailbox is unavailable in server channels."

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # We're inside an async context — schedule as a task
            # But since this is a sync function called by ADK, we need to handle this
            # Use a thread-safe approach
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(_check_mailbox_sync).result(timeout=5)
        return loop.run_until_complete(_check_mailbox_async())
    except Exception as e:
        logger.error(f"check_mailbox error: {e}")
        return f"Error checking mailbox: {e}"


def _check_mailbox_sync() -> str:
    """Synchronous wrapper for mailbox check."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_check_mailbox_async())
    finally:
        loop.close()


async def _check_mailbox_async() -> str:
    """Async implementation of mailbox check."""
    from backend.database.redis_connection import get_redis_client
    redis = get_redis_client()

    try:
        # Peek at the response list without consuming
        responses = await redis.lrange("icarus:councilor:responses", 0, 9)
        if not responses:
            return "Mailbox empty — no unprocessed Councilor responses."

        pending = []
        for raw in responses:
            try:
                data = json.loads(raw)
                msg = data.get("message", "(no message)")
                resp_type = data.get("type", "unknown")
                ts = data.get("timestamp", "?")
                pending.append(
                    f"[{resp_type}] (ts={ts}): {msg[:200]}{'...' if len(msg) > 200 else ''}"
                )
            except json.JSONDecodeError:
                pending.append("(unreadable response)")
        return "Pending Councilor responses:\n" + "\n".join(pending)
    except Exception as e:
        return f"Error reading mailbox: {e}"


async def consult_councilor(question: str) -> str:
    """Consult the Councilor (L2) for analysis, advice, or knowledge.

    This is a READ-ONLY consultation — the Councilor will not execute any commands
    or modify any files. Use this when you need:
    - Analysis or explanation of code, errors, or concepts
    - Advice on how to approach a problem
    - Context or knowledge you don't have within your container

    This call blocks until the Councilor responds (up to 60 seconds).
    Returns the Councilor's answer directly — relay it to the user.

    Args:
        question: The question or topic you want the Councilor to analyse or explain.
    """
    from backend.database.redis_connection import get_redis_client
    from backend.agent.tools import current_platform, current_user_id, current_chat_id

    platform = current_platform.get()
    user_id = current_user_id.get()
    chat_id = current_chat_id.get()
    timestamp = int(time.time())

    redis = get_redis_client()

    payload = json.dumps({
        "type": "consultation",
        "timestamp": timestamp,
        "platform": platform,
        "user_id": user_id,
        "chat_id": chat_id,
        "question": question,
    })

    try:
        # Publish the request
        await redis.publish("icarus:councilor:requests", payload)
        logger.info(f"Published consultation request (ts={timestamp}). Awaiting response...")
    except Exception as e:
        error_msg = f"Failed to publish consultation request: {e}"
        logger.error(error_msg)
        return error_msg

    from backend.agent.activity_repo import publish_activity
    await publish_activity(
        actor="icarus", event_type="dispatch_consultation",
        action="asked consult_councilor", detail=question,
        thread_id=f"consult-{timestamp}", platform=platform, user_id=user_id,
    )

    # Subscribe to the response channel for this specific request
    pubsub = redis.pubsub()
    response_channel = f"icarus:councilor:response:{timestamp}"
    await pubsub.subscribe(response_channel)

    try:
        deadline = time.monotonic() + CONSULTATION_TIMEOUT_SECONDS
        async for msg in pubsub.listen():
            if time.monotonic() >= deadline:
                break
            if msg["type"] != "message":
                continue
            try:
                data = json.loads(msg["data"])
                message = data.get("message", "(Councilor returned no message)")
                logger.info("Consultation response received.")
                return message
            except json.JSONDecodeError:
                continue
    finally:
        await pubsub.unsubscribe(response_channel)
        await pubsub.close()

    return (
        f"Councilor did not respond within {CONSULTATION_TIMEOUT_SECONDS} seconds. "
        f"The request may still be processed — check your mailbox later."
    )


async def check_pending_upgrade() -> str:
    """Check whether the host checkout is behind origin/main — i.e. whether a
    Councilor-authored PR has been merged on GitHub but never pulled onto the
    host or applied to the running containers. Read-only; changes nothing.

    This call blocks until the Councilor responds (up to 60 seconds).
    Returns a summary of pending commits/files, or confirms there's nothing
    to apply."""
    from backend.database.redis_connection import get_redis_client
    from backend.agent.tools import current_platform, current_user_id, current_chat_id

    platform = current_platform.get()
    user_id = current_user_id.get()
    chat_id = current_chat_id.get()
    timestamp = int(time.time())

    redis = get_redis_client()

    payload = json.dumps({
        "type": "deploy_check",
        "timestamp": timestamp,
        "platform": platform,
        "user_id": user_id,
        "chat_id": chat_id,
    })

    try:
        await redis.publish("icarus:councilor:requests", payload)
        logger.info(f"Published deploy_check request (ts={timestamp}). Awaiting response...")
    except Exception as e:
        error_msg = f"Failed to publish deploy_check request: {e}"
        logger.error(error_msg)
        return error_msg

    pubsub = redis.pubsub()
    response_channel = f"icarus:councilor:response:{timestamp}"
    await pubsub.subscribe(response_channel)

    try:
        deadline = time.monotonic() + CONSULTATION_TIMEOUT_SECONDS
        async for msg in pubsub.listen():
            if time.monotonic() >= deadline:
                break
            if msg["type"] != "message":
                continue
            try:
                data = json.loads(msg["data"])
                return data.get("message", "(Councilor returned no message)")
            except json.JSONDecodeError:
                continue
    finally:
        await pubsub.unsubscribe(response_channel)
        await pubsub.close()

    return (
        f"Councilor did not respond within {CONSULTATION_TIMEOUT_SECONDS} seconds. "
        f"The request may still be processed — check your mailbox later."
    )


async def apply_pending_upgrade() -> str:
    """Pull a merged Councilor upgrade from origin/main onto the host and
    restart (or rebuild, if requirements.txt/Dockerfile changed) the running
    containers — including the one you're running in.

    Only call this when the operator has explicitly asked you to apply a
    pending upgrade (ideally after check_pending_upgrade confirmed there is
    one) — this restarts live infrastructure. This call returns IMMEDIATELY;
    the Councilor performs the pull/restart in the background on the host and
    notifies DIIZZY directly with the outcome. After calling this, log the
    dispatch with append_memory."""
    from backend.database.redis_connection import get_redis_client
    from backend.agent.tools import current_platform, current_user_id, current_chat_id

    platform = current_platform.get()
    user_id = current_user_id.get()
    chat_id = current_chat_id.get()
    timestamp = int(time.time())

    redis = get_redis_client()

    payload = json.dumps({
        "type": "deploy_apply",
        "timestamp": timestamp,
        "platform": platform,
        "user_id": user_id,
        "chat_id": chat_id,
    })

    try:
        await redis.publish("icarus:councilor:requests", payload)
        logger.info(
            f"Published deploy_apply request (ts={timestamp}). "
            f"Councilor will notify via {(platform or 'unknown').capitalize()}."
        )

        from backend.agent.activity_repo import publish_activity
        await publish_activity(
            actor="icarus", event_type="dispatch_deploy",
            action="dispatched apply_pending_upgrade", detail="apply pending upgrade",
            thread_id=f"deploy-{timestamp}", platform=platform, user_id=user_id,
        )

        return (
            f"Upgrade apply dispatched to the Councilor. It will pull origin/main and "
            f"restart or rebuild the containers as needed, then notify you via "
            f"{(platform or 'unknown').capitalize()} when done. Request timestamp: {timestamp}"
        )
    except Exception as e:
        error_msg = f"Failed to dispatch upgrade apply: {e}"
        logger.error(error_msg)
        return error_msg


async def escalate_to_councilor(intent_description: str, target_files: List[str]) -> str:
    """Dispatches a write/execute task to the L2 Supervisor (The Councilor).

    Use this tool when:
    1. You need to modify your own source code (FastAPI application).
    2. A task requires installing dependencies, Docker restarts, or host commands.
    3. You are unable to complete a task due to container restrictions.

    This call returns IMMEDIATELY — it does not block. The Councilor processes the
    task in the background and delivers the result via the originating platform
    directly to DIIZZY. After calling this tool, log the dispatch with
    append_memory so you remember the operation is pending.

    Args:
        intent_description: A highly detailed prompt explaining EXACTLY what the L2 model must do.
        target_files: Files the L2 model will need to modify (empty list if informational).
    """
    from backend.database.redis_connection import get_redis_client
    from backend.agent.tools import current_platform, current_user_id, current_chat_id

    platform = current_platform.get()
    user_id = current_user_id.get()
    chat_id = current_chat_id.get()
    timestamp = int(time.time())

    redis = get_redis_client()

    payload = json.dumps({
        "type": "escalation",
        "timestamp": timestamp,
        "platform": platform,
        "user_id": user_id,
        "chat_id": chat_id,
        "intent": intent_description,
        "target_files": target_files,
    })

    try:
        await redis.publish("icarus:councilor:requests", payload)
        logger.info(
            f"Published escalation request (ts={timestamp}). "
            f"Councilor will notify via {(platform or 'unknown').capitalize()}."
        )

        from backend.agent.activity_repo import publish_activity
        await publish_activity(
            actor="icarus", event_type="dispatch_escalation",
            action="dispatched escalate_to_councilor", detail=intent_description,
            thread_id=f"esc-{timestamp}", platform=platform, user_id=user_id,
        )

        return (
            f"Escalation dispatched to the Councilor via Redis. "
            f"The result will be delivered via {(platform or 'unknown').capitalize()} when complete. "
            f"Request timestamp: {timestamp}"
        )
    except Exception as e:
        error_msg = f"Failed to publish escalation request: {e}"
        logger.error(error_msg)
        return error_msg


async def delegate_task(
    intent: str,
    capabilities: list[str],
    needs_network: bool = False,
    needs_repo_write: bool = False,
) -> str:
    """Delegate a self-contained task to a Councilor-supervised subagent.

    The subagent runs in the background with ONLY the capabilities you declare
    here — nothing else is callable for it — and you never see its raw tool
    output: a short completion summary is delivered to the operator via the
    current platform when it finishes. This call returns IMMEDIATELY with a
    task id. After calling it, log the pending task id with append_memory.

    Args:
        intent: A complete, self-contained brief of what to do and what the
            summary should answer. The subagent has none of your conversation
            context — include everything it needs.
        capabilities: Capability names the task needs, from: time, web,
            gmail_read, calendar_read, github_read, github_write, telemetry,
            tracked_items. Declare only what's needed.
        needs_network: True if any declared capability reaches the network
            (web, gmail_read, calendar_read, github_read, github_write).
        needs_repo_write: True only if the task must modify Icarus's own
            source code — it then runs in the sandboxed worktree and lands as
            a PR for human review, like escalate_to_councilor.
    """
    from backend.database.redis_connection import get_redis_client
    from backend.agent.tools import current_platform, current_user_id, current_chat_id, current_access_mode
    from backend.agent.delegation import build_delegation_request, DelegationError

    # Same capability-not-prompt enforcement as check_mailbox: an untrusted
    # server channel never gets to spend Councilor time, even if the tool
    # somehow ends up bound there.
    if current_access_mode.get() == "server":
        return "Delegation is unavailable in server channels."

    platform = current_platform.get()
    user_id = current_user_id.get()
    chat_id = current_chat_id.get()

    # Validate the declaration here, synchronously, so the model gets an
    # actionable error on THIS turn instead of a fire-and-forget request that
    # comes back rejected minutes later through the mailbox.
    try:
        req = build_delegation_request(
            intent=intent,
            capabilities=capabilities,
            needs_network=needs_network,
            needs_repo_write=needs_repo_write,
            platform=platform,
            user_id=user_id,
            chat_id=chat_id,
        )
    except DelegationError as e:
        return f"Delegation rejected before dispatch: {e}"

    redis = get_redis_client()
    try:
        await redis.publish("icarus:councilor:requests", json.dumps(req.to_payload()))
        logger.info(
            f"Published delegation {req.task_id} (caps={req.capabilities}, network={req.needs_network}, "
            f"repo_write={req.needs_repo_write}). Councilor will notify via {(platform or 'unknown').capitalize()}."
        )
    except Exception as e:
        error_msg = f"Failed to publish delegation request: {e}"
        logger.error(error_msg)
        return error_msg

    from backend.agent.activity_repo import publish_activity
    await publish_activity(
        actor="icarus", event_type="dispatch_delegation",
        action="dispatched delegate_task", detail=intent,
        thread_id=req.thread_id, platform=platform, user_id=user_id,
    )

    scope = ", ".join(req.capabilities) if req.capabilities else "none"
    where = "sandboxed worktree (result lands as a PR)" if req.needs_repo_write else "Councilor, no repo access"
    return (
        f"Delegated task {req.task_id} to the Councilor — capabilities: {scope}; "
        f"network: {'yes' if req.needs_network else 'no'}; runs in: {where}. "
        f"It runs in the background; the completion summary will be delivered via "
        f"{(platform or 'unknown').capitalize()} when done. Check on it any time with "
        f"check_task_status('{req.task_id}'). Log the pending task id with append_memory."
    )


async def check_task_status(task_id: str = "") -> str:
    """Check the status of a delegated task or subagent by its id (sub-…).

    Read-only and instant — reads the Councilor's task registry directly and
    never waits for the task. Reports status (queued / running / paused /
    completed / failed / stopped), the capability scope, and the completion
    summary or error once there is one. Call with an empty id to list the
    ten most recent tasks.

    Args:
        task_id: The id returned by delegate_task, or empty to list recent tasks.
    """
    from backend.agent.tools import current_access_mode
    if current_access_mode.get() == "server":
        return "Task status is unavailable in server channels."

    from backend.agent.subagent_registry import describe_task
    return await describe_task(task_id)


async def create_persistent_subagent(
    intent: str,
    capabilities: list[str],
    needs_network: bool = False,
    interval_minutes: int = 15,
) -> str:
    """Create a persistent subagent that keeps working on a standing directive on a schedule.

    Unlike delegate_task (one job, then done), this provisions a long-lived
    worker container that wakes every `interval_minutes`, acts on the
    directive with ONLY the capabilities declared here, keeps its own notes
    between cycles, and messages the operator only when something material
    changes. Its capability grant expires after a configured TTL (default one
    week) — re-create it to renew. This call returns IMMEDIATELY; the
    Councilor confirms the subagent's id via the current platform. Log that
    id with append_memory when it arrives.

    Args:
        intent: The standing directive, complete and self-contained — what to
            watch or maintain, what counts as worth reporting, what to ignore.
        capabilities: Capability names it needs, from: time, web, gmail_read,
            calendar_read, github_read, github_write, telemetry, tracked_items.
        needs_network: True if any declared capability reaches the network.
        interval_minutes: Minutes between cycles (1–1440; default 15).
    """
    from backend.database.redis_connection import get_redis_client
    from backend.agent.tools import current_platform, current_user_id, current_chat_id, current_access_mode
    from backend.agent.delegation import validate_declaration, DelegationError

    if current_access_mode.get() == "server":
        return "Persistent subagents are unavailable in server channels."

    intent = (intent or "").strip()
    if not intent:
        return "Rejected: intent must be a non-empty standing directive."
    try:
        caps = validate_declaration(capabilities, bool(needs_network), False)
    except DelegationError as e:
        return f"Rejected before dispatch: {e}"
    try:
        minutes = int(interval_minutes)
    except (TypeError, ValueError):
        minutes = 15
    minutes = max(1, min(1440, minutes))

    platform = current_platform.get()
    user_id = current_user_id.get()
    chat_id = current_chat_id.get()
    timestamp = int(time.time())
    payload = json.dumps({
        "type": "subagent_create",
        "timestamp": timestamp,
        "platform": platform,
        "user_id": user_id,
        "chat_id": chat_id,
        "intent": intent,
        "capabilities": caps,
        "needs_network": bool(needs_network),
        "interval_seconds": minutes * 60,
    })
    redis = get_redis_client()
    try:
        await redis.publish("icarus:councilor:requests", payload)
    except Exception as e:
        return f"Failed to publish subagent request: {e}"

    from backend.agent.activity_repo import publish_activity
    await publish_activity(
        actor="icarus", event_type="dispatch_subagent_create",
        action="requested create_persistent_subagent", detail=intent,
        thread_id=f"subagent-{timestamp}", platform=platform, user_id=user_id,
    )
    return (
        f"Requested a persistent subagent (capabilities: {', '.join(caps) or 'none'}; every {minutes} min). "
        f"The Councilor is provisioning it and will confirm its id (sub-…) via "
        f"{(platform or 'unknown').capitalize()} — log that id with append_memory when it arrives."
    )


async def stop_subagent(task_id: str) -> str:
    """Stop and remove a persistent subagent, or cancel a running delegated task, by its id (sub-…).

    Returns IMMEDIATELY; the Councilor confirms via the current platform. Only
    call this when DIIZZY asks for a subagent or task to be stopped.

    Args:
        task_id: The id from create_persistent_subagent's confirmation or delegate_task.
    """
    from backend.database.redis_connection import get_redis_client
    from backend.agent.tools import current_platform, current_user_id, current_chat_id, current_access_mode
    from backend.agent.delegation import is_task_id

    if current_access_mode.get() == "server":
        return "Stopping subagents is unavailable in server channels."
    task_id = (task_id or "").strip()
    if not is_task_id(task_id):
        return f"'{task_id}' is not a task id — ids look like sub-1700000000-a1b2c3. Use check_task_status('') to list recent ones."

    platform = current_platform.get()
    user_id = current_user_id.get()
    chat_id = current_chat_id.get()
    timestamp = int(time.time())
    payload = json.dumps({
        "type": "subagent_stop",
        "timestamp": timestamp,
        "platform": platform,
        "user_id": user_id,
        "chat_id": chat_id,
        "task_id": task_id,
    })
    redis = get_redis_client()
    try:
        await redis.publish("icarus:councilor:requests", payload)
    except Exception as e:
        return f"Failed to publish stop request: {e}"

    from backend.agent.activity_repo import publish_activity
    await publish_activity(
        actor="icarus", event_type="dispatch_subagent_stop",
        action="requested stop_subagent", detail=task_id,
        thread_id=f"sub-{task_id}", platform=platform, user_id=user_id,
    )
    return f"Stop requested for {task_id}. The Councilor will confirm via {(platform or 'unknown').capitalize()}."
