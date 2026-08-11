"""
councilor.py — The Councilor daemon (L2 agent).

Runs on the host machine (outside Docker). Listens for escalation and
consultation requests from Icarus (L1) via Redis pub/sub, routes them to
the appropriate model (Gemma 27B for consultations, Gemini Flash for
escalations), and delivers results back.

Replaces the old Gemini CLI subprocess approach with direct API calls
for dramatically lower latency and cross-escalation memory.
"""

import os
import re
import sys
import json
import time
import uuid
import asyncio
import logging
import subprocess
import urllib.request
from pathlib import Path
from collections import deque

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - THE COUNCILOR - %(levelname)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.resolve()

# ── Env loader ───────────────────────────────────────────────────────────────

def _load_env():
    """Load variables from .env into os.environ. Handles BOM and CRLF."""
    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists():
        return
    with open(env_file, "r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip().rstrip("\r")
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip())


# ── Cross-escalation memory buffer ──────────────────────────────────────────
# Retains summaries of recent escalations so subsequent calls have context.
MAX_MEMORY_ENTRIES = 10
_escalation_memory: deque[dict] = deque(maxlen=MAX_MEMORY_ENTRIES)


def _get_memory_context() -> str:
    """Format the escalation memory buffer for injection into the system prompt."""
    if not _escalation_memory:
        return ""
    lines = ["[RECENT ESCALATION HISTORY — use this context for continuity]"]
    for entry in _escalation_memory:
        lines.append(
            f"- [{entry['timestamp']}] {entry['type'].upper()}: "
            f"{entry['summary'][:200]}"
        )
    return "\n".join(lines) + "\n"


def _record_escalation(escalation_type: str, intent: str, outcome: str):
    """Record an escalation summary for cross-call memory."""
    _escalation_memory.append({
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "type": escalation_type,
        "summary": f"Intent: {intent[:100]} | Outcome: {outcome[:100]}",
    })


# ── Sandboxed, worktree-scoped tools for the agent loop ─────────────────────
# Escalations run in a disposable git worktree, never against the live
# checkout. read_file/write_file/list_directory are plain Python with a
# path-prefix jail — fine, since their risk is scope, not arbitrary code
# execution. run_command gets real OS-level confinement via bwrap: a string
# blocklist can't safely bound arbitrary shell text, so it isn't trusted to
# do that job anymore. The sandbox — read-only host outside the worktree, no
# network, no visibility into other processes — is the actual boundary now.

WORKTREE_ROOT = PROJECT_ROOT / ".worktrees"


def _make_tools(worktree_path: Path):
    """Build a fresh read_file/write_file/list_directory/run_command set
    bound to one escalation's worktree."""
    root = worktree_path.resolve()

    def _resolve(rel_path: str) -> Path | None:
        candidate = (root / rel_path).resolve()
        if not str(candidate).startswith(str(root)):
            return None
        return candidate

    def read_file(filepath: str) -> str:
        """Read the contents of a file from the escalation worktree."""
        resolved = _resolve(filepath)
        if resolved is None:
            return f"Error: path {filepath} is outside the escalation worktree."
        try:
            with open(resolved, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except Exception as e:
            return f"Error reading {filepath}: {e}"

    def write_file(filepath: str, contents: str) -> str:
        """Write contents to a file in the escalation worktree. Creates directories as needed."""
        resolved = _resolve(filepath)
        if resolved is None:
            return f"Error: path {filepath} is outside the escalation worktree."
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            if resolved.exists():
                backup = resolved.with_suffix(resolved.suffix + ".bak")
                resolved.replace(backup)
            with open(resolved, "w", encoding="utf-8") as f:
                f.write(contents)
            return f"Successfully wrote {filepath} ({len(contents)} chars)"
        except Exception as e:
            return f"Error writing {filepath}: {e}"

    def list_directory(directory: str) -> str:
        """List files and subdirectories in a directory within the escalation worktree."""
        resolved = _resolve(directory)
        if resolved is None:
            return f"Error: path {directory} is outside the escalation worktree."
        try:
            entries = sorted(os.listdir(resolved))
            return "\n".join(entries) if entries else "(empty directory)"
        except Exception as e:
            return f"Error listing {directory}: {e}"

    def run_command(command: str, cwd: str = ".") -> str:
        """Run a shell command, sandboxed to the escalation worktree: read-only
        view of the host outside it, no network, no visibility into other
        processes. Times out after 30 seconds. Anything requiring a download
        or external service will fail by design — that's a signal to escalate
        manually, not something this sandbox allows."""
        resolved_cwd = _resolve(cwd)
        if resolved_cwd is None:
            return f"Error: cwd {cwd} is outside the escalation worktree."
        bwrap_cmd = [
            "bwrap",
            "--ro-bind", "/", "/",
            "--dev", "/dev",
            "--proc", "/proc",
            "--tmpfs", "/tmp",
            "--bind", str(root), str(root),
            "--unshare-all",
            "--die-with-parent",
            "--new-session",
            "--chdir", str(resolved_cwd),
            "/bin/sh", "-c", command,
        ]
        try:
            result = subprocess.run(bwrap_cmd, capture_output=True, text=True, timeout=30)
            output = ""
            if result.stdout:
                output += result.stdout
            if result.stderr:
                output += f"\n[stderr] {result.stderr}"
            if result.returncode != 0:
                output += f"\n[exit code: {result.returncode}]"
            return output.strip() or "(no output)"
        except subprocess.TimeoutExpired:
            return "Command timed out after 30 seconds."
        except FileNotFoundError:
            return "Error: bwrap is not installed on this host — sandboxed execution is unavailable."
        except Exception as e:
            return f"Error running command: {e}"

    return [read_file, write_file, list_directory, run_command]


# ── System prompts ───────────────────────────────────────────────────────────

ESCALATION_SYSTEM_PROMPT = """You are The Councilor — the L2 meta-agent for Project Icarus.
You are operating inside an isolated git worktree — a disposable copy of the
repository on its own branch. The primary checkout is never touched by your
work, and run_command executes in a sandbox with no network access and a
read-only view of the host outside this worktree.

Your job: execute the given intent by reading and modifying source files as needed.

Rules:
- Focus ONLY on the given intent. Do not explore unrelated files.
- Use read_file to understand context before making changes.
- Use write_file to make changes. Provide COMPLETE file contents, not diffs.
- Use list_directory to explore the project structure if needed.
- Use run_command sparingly and only when necessary (e.g., to check syntax). It
  has no network access — anything requiring a download or external service
  will fail by design.
- Do NOT run git commands — commit, push, and PR creation are handled
  automatically after you finish.
- Be precise and surgical. Modify only what's needed.
- When done, provide a clear summary of what you changed and why.
"""

CONSULTATION_SYSTEM_PROMPT = """You are The Councilor — an advisory AI for Project Icarus.
You are being consulted for analysis, advice, or knowledge.

Rules:
- This is a READ-ONLY consultation. You cannot and should not modify files.
- Provide clear, actionable analysis based on the question.
- Be concise and direct. No filler.
- If you lack information to answer fully, say so and suggest what's needed.
"""


# ── Notification helpers ────────────────────────────────────────────────────

def _split_message(text: str, limit: int = 2000) -> list[str]:
    """Split text into chunks at newline boundaries."""
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


def _send_telegram(message: str, chat_id: str = None):
    """Send a message to DIIZZY via the Telegram bot."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or os.getenv("ALLOWED_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        for chunk in _split_message(message, 4000):
            data = json.dumps({"chat_id": int(chat_id), "text": chunk}).encode()
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        logger.warning(f"Failed to send Telegram notification: {e}")


def _send_discord(message: str, channel_id: str = None):
    """Send a message to DIIZZY via the Discord bot API."""
    token = os.getenv("DISCORD_BOT_TOKEN")
    channel_id = channel_id or os.getenv("DISCORD_ALLOWED_CHANNEL_ID")
    if not token or not channel_id:
        return
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
        "User-Agent": "Icarus-Councilor-v2.0.0",
    }
    for chunk in _split_message(message, 2000):
        try:
            data = json.dumps({"content": chunk}).encode()
            req = urllib.request.Request(url, data=data, headers=headers)
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            logger.warning(f"Failed to send Discord notification: {e}")
            break


def _notify(platform: str | None, chat_id: str | None, message: str):
    """Send notification to the originating platform."""
    if platform == "discord":
        _send_discord(message, chat_id)
    else:
        _send_telegram(message, chat_id)


# ── Git workflow ─────────────────────────────────────────────────────────────

def _get_repo_remote_info() -> tuple | None:
    """Parse owner and repo name from git remote origin URL."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None
        url = result.stdout.strip()
        match = re.search(r"github\.com[:/]([^/]+)/([^/\s]+?)(?:\.git)?$", url)
        return (match.group(1), match.group(2)) if match else None
    except Exception:
        return None


def _create_pr_via_api(token: str, owner: str, repo: str, branch: str, title: str, body: str) -> str | None:
    """Create a GitHub PR via REST API. Returns the PR HTML URL."""
    try:
        data = json.dumps({
            "title": title,
            "head": branch,
            "base": "main",
            "body": body,
        }).encode()
        req = urllib.request.Request(
            f"https://api.github.com/repos/{owner}/{repo}/pulls",
            data=data,
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github.v3+json",
                "Content-Type": "application/json",
                "User-Agent": "Icarus-Councilor-v2.0.0",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            pr_data = json.loads(resp.read().decode())
            return pr_data.get("html_url")
    except Exception as e:
        logger.warning(f"Failed to create PR: {e}")
        return None


def _create_escalation_worktree(timestamp: int) -> tuple[Path, str] | None:
    """Create an isolated git worktree on a fresh branch, based on main.
    Never touches the primary checkout — unlike the old stash/checkout dance,
    the live working tree isn't disturbed even while an escalation is running.
    Returns (worktree_path, branch_name), or None on failure."""
    branch_name = f"councilor/intent-{timestamp}-{uuid.uuid4().hex[:6]}"
    worktree_path = WORKTREE_ROOT / branch_name.replace("/", "_")
    WORKTREE_ROOT.mkdir(exist_ok=True)

    result = subprocess.run(
        ["git", "worktree", "add", "-b", branch_name, str(worktree_path), "main"],
        cwd=str(PROJECT_ROOT), capture_output=True, text=True,
    )
    if result.returncode != 0:
        logger.error(f"Failed to create escalation worktree: {result.stderr.strip()}")
        return None
    logger.info(f"Created escalation worktree {worktree_path} on branch {branch_name}")
    return worktree_path, branch_name


def _finalize_worktree(worktree_path: Path, branch_name: str, intent: str) -> str | None:
    """Commit, push, and PR from within the isolated worktree, if anything
    changed. Returns the PR URL, or None if there was nothing to land or the
    workflow failed."""
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(worktree_path), capture_output=True, text=True,
    )
    if not status.stdout.strip():
        logger.info("No source changes detected in worktree — skipping git workflow.")
        return None

    changed_list = [
        line.strip().split(None, 1)[-1]
        for line in status.stdout.strip().split("\n")
        if line.strip()
    ]
    logger.info(f"Changes detected in: {changed_list}")

    from backend.agent.capability_registry import classify_change
    risk_tier, sensitive_paths = classify_change(changed_list)
    if risk_tier == "sensitive":
        logger.warning(f"Escalation touches sensitive paths: {sensitive_paths}")

    subprocess.run(["git", "add", "-A"], cwd=str(worktree_path), capture_output=True)
    commit_msg = f"Councilor: {intent[:72]}"
    commit = subprocess.run(
        ["git", "commit", "-m", commit_msg],
        cwd=str(worktree_path), capture_output=True, text=True,
    )
    if commit.returncode != 0:
        logger.warning(f"Commit failed: {commit.stderr.strip()}")
        return None

    push = subprocess.run(
        ["git", "push", "origin", branch_name],
        cwd=str(worktree_path), capture_output=True, text=True,
    )
    if push.returncode != 0:
        logger.warning(f"Push failed: {push.stderr.strip()}")
        return None
    logger.info(f"Pushed {branch_name} to origin")

    pr_url = None
    github_token = os.getenv("GITHUB_TOKEN")
    repo_info = _get_repo_remote_info()
    if github_token and repo_info:
        owner, repo_name = repo_info
        changed_md = "\n".join(f"- `{f}`" for f in changed_list)
        risk_line = (
            f"⚠️ **Risk: sensitive** — touches {', '.join(f'`{p}`' for p in sensitive_paths)}. "
            f"Review carefully regardless of diff size."
            if risk_tier == "sensitive"
            else "**Risk: standard** — no declared sensitive paths touched."
        )
        pr_url = _create_pr_via_api(
            github_token, owner, repo_name, branch_name,
            title=f"Councilor: {intent[:60]}",
            body=(
                f"## Automated changes by The Councilor\n\n"
                f"{risk_line}\n\n"
                f"**Intent:**\n{intent}\n\n"
                f"**Changed files:**\n{changed_md}\n\n"
                f"_Built and committed inside an isolated worktree — the primary "
                f"checkout was never touched._"
            ),
        )
        if pr_url:
            logger.info(f"PR created: {pr_url}")

    return pr_url


def _cleanup_worktree(worktree_path: Path):
    """Remove the escalation worktree. Always call this — success or failure —
    so failed/aborted escalations don't leave orphaned worktrees behind."""
    try:
        subprocess.run(
            ["git", "worktree", "remove", str(worktree_path), "--force"],
            cwd=str(PROJECT_ROOT), capture_output=True,
        )
    except Exception as e:
        logger.warning(f"Failed to remove worktree {worktree_path}: {e}")
    finally:
        subprocess.run(["git", "worktree", "prune"], cwd=str(PROJECT_ROOT), capture_output=True)


# ── Request processors ──────────────────────────────────────────────────────

async def process_consultation(data: dict):
    """Handle a read-only advisory consultation."""
    question = data.get("question", "")
    platform = data.get("platform")
    chat_id = data.get("chat_id")
    timestamp = data.get("timestamp", int(time.time()))

    if not question:
        logger.error("Consultation with no question — discarding")
        return

    logger.info(f"Consultation [platform={platform}]: {question[:200]}")

    # Import here to avoid circular imports at module level
    from backend.agent.llm_router import generate

    memory_ctx = _get_memory_context()
    system = CONSULTATION_SYSTEM_PROMPT
    if memory_ctx:
        system += "\n\n" + memory_ctx

    response = await generate(
        task_type="consultation",
        messages=[{"role": "user", "text": question}],
        system_instruction=system,
    )

    _record_escalation("consultation", question, response[:100])

    # Publish response to Redis for the heartbeat to deliver
    await _publish_response(timestamp, "consultation", response, platform, chat_id)
    logger.info(f"Consultation complete ({len(response)} chars)")


async def process_escalation(data: dict):
    """Handle an execution/write escalation."""
    intent = data.get("intent", "")
    platform = data.get("platform")
    chat_id = data.get("chat_id")
    timestamp = data.get("timestamp", int(time.time()))
    max_retries = 2

    if not intent:
        logger.error("Escalation with no intent — discarding")
        return

    logger.info(f"Escalation [platform={platform}]: {intent[:200]}")

    from backend.agent.llm_router import agent_loop

    worktree = _create_escalation_worktree(timestamp)
    if worktree is None:
        error_msg = "Escalation failed: could not create an isolated worktree to work in."
        await _publish_response(timestamp, "escalation", error_msg, platform, chat_id)
        _notify(platform, chat_id, f"[Icarus Escalation Failed]\n\n{error_msg}")
        return
    worktree_path, branch_name = worktree
    tools = _make_tools(worktree_path)

    memory_ctx = _get_memory_context()
    system = ESCALATION_SYSTEM_PROMPT
    if memory_ctx:
        system += "\n\n" + memory_ctx

    try:
        # Retry loop — reuses the same worktree across attempts, so retries
        # see the prior attempt's partial progress rather than starting clean.
        last_error = None
        for attempt in range(max_retries + 1):
            if attempt > 0:
                logger.info(f"Retry {attempt}/{max_retries}...")

            response = await agent_loop(
                task_type="escalation",
                initial_prompt=intent,
                tools=tools,
                system_instruction=system,
            )

            # Check if response indicates failure
            if "error" in response.lower() and attempt < max_retries:
                last_error = response
                system += f"\n\n[PREVIOUS ATTEMPT FAILED]\n{response}\nPlease fix the issue and try again."
                continue

            # Success
            pr_url = _finalize_worktree(worktree_path, branch_name, intent)
            if pr_url:
                response += f"\n\nPR: {pr_url}"

            _record_escalation("escalation", intent, response[:100])
            await _publish_response(timestamp, "escalation", response, platform, chat_id)

            # Notify operator
            _notify(platform, chat_id, f"[Icarus Escalation Complete]\n\n{response}")
            logger.info(f"Escalation complete ({len(response)} chars)")
            return

        # All retries exhausted
        error_msg = f"Escalation failed after {max_retries + 1} attempts.\n\nLast error:\n{last_error or response}"
        await _publish_response(timestamp, "escalation", error_msg, platform, chat_id)
        _notify(platform, chat_id, f"[Icarus Escalation Failed]\n\n{error_msg}")
    finally:
        _cleanup_worktree(worktree_path)


# ── Redis IPC ────────────────────────────────────────────────────────────────

_redis_client = None


async def _get_redis():
    """Get or create the Redis client for the Councilor (host-side)."""
    global _redis_client
    if _redis_client is None:
        import redis.asyncio as redis
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        _redis_client = redis.from_url(redis_url, decode_responses=True)
        logger.info(f"[redis] Connected to {redis_url}")
    return _redis_client


async def _publish_response(timestamp: int, resp_type: str, message: str, platform: str = None, chat_id: str = None):
    """Publish a response to Redis.

    Escalations are always fire-and-forget — nobody's ever blocking on the
    pub/sub channel for those, so the heartbeat mailbox list is their only
    delivery path. Consultations are answered directly to whichever
    consult_councilor() call is blocking on this timestamp's channel; only
    fall back to the mailbox list if PUBLISH reports zero receivers (the
    blocking call already hit its timeout, or something unusual happened) —
    otherwise every consultation would get delivered twice: once directly,
    once again a few seconds later via the heartbeat, redundantly.
    """
    r = await _get_redis()
    payload = json.dumps({
        "timestamp": timestamp,
        "type": resp_type,
        "message": message,
        "platform": platform,
        "chat_id": chat_id,
    })

    receivers = await r.publish(f"icarus:councilor:response:{timestamp}", payload)

    if resp_type != "consultation" or receivers == 0:
        await r.lpush("icarus:councilor:responses", payload)

    logger.info(
        f"Published {resp_type} response (timestamp={timestamp}, direct_receivers={receivers})"
    )


async def _listen_for_requests():
    """Subscribe to Redis channel for incoming requests from L1."""
    r = await _get_redis()
    pubsub = r.pubsub()
    await pubsub.subscribe("icarus:councilor:requests")
    logger.info("[redis] Subscribed to icarus:councilor:requests")

    async for msg in pubsub.listen():
        if msg["type"] != "message":
            continue
        try:
            data = json.loads(msg["data"])
            req_type = data.get("type", "consultation")
            logger.info(f"Received {req_type} request via Redis")

            if req_type == "escalation":
                await process_escalation(data)
            else:
                await process_consultation(data)

        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in request: {e}")
        except Exception as e:
            logger.error(f"Error processing request: {e}")


# ── Main entry point ────────────────────────────────────────────────────────

async def async_main():
    """Start the Councilor daemon."""
    logger.info("Councilor v2.0 starting — Redis pub/sub + Tiered API")
    logger.info(f"Project root: {PROJECT_ROOT}")

    # Verify Redis is reachable
    try:
        r = await _get_redis()
        await r.ping()
        logger.info("[redis] Connection verified")
    except Exception as e:
        logger.error(f"[redis] Cannot connect: {e}")
        logger.error("Ensure Redis is running and REDIS_URL is set correctly.")
        sys.exit(1)

    # GOOGLE_API_KEY is only needed for the dormant cloud overflow path in
    # llm_router.py — consultation/scoring/escalation all route local by
    # default now, so this is no longer a hard requirement to start.
    if not os.getenv("GOOGLE_API_KEY"):
        logger.info("GOOGLE_API_KEY not set — fine, cloud overflow path stays unused.")

    # Enter the main listen loop
    await _listen_for_requests()


def main():
    _load_env()
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        logger.info("Shutting down Councilor.")


if __name__ == "__main__":
    main()
