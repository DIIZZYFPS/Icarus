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


# ── Host filesystem tools for the agent loop ────────────────────────────────
# These run on the HOST, giving Gemini Flash full access to Icarus source code.

def read_file(filepath: str) -> str:
    """Read the contents of a file from the project."""
    try:
        resolved = (PROJECT_ROOT / filepath).resolve()
        # Security: ensure we stay within project root
        if not str(resolved).startswith(str(PROJECT_ROOT)):
            return f"Error: path {filepath} is outside the project root."
        with open(resolved, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception as e:
        return f"Error reading {filepath}: {e}"


def write_file(filepath: str, contents: str) -> str:
    """Write contents to a file in the project. Creates directories as needed."""
    try:
        resolved = (PROJECT_ROOT / filepath).resolve()
        if not str(resolved).startswith(str(PROJECT_ROOT)):
            return f"Error: path {filepath} is outside the project root."
        resolved.parent.mkdir(parents=True, exist_ok=True)
        # Backup existing file
        if resolved.exists():
            backup = resolved.with_suffix(resolved.suffix + ".bak")
            resolved.replace(backup)
        with open(resolved, "w", encoding="utf-8") as f:
            f.write(contents)
        return f"Successfully wrote {filepath} ({len(contents)} chars)"
    except Exception as e:
        return f"Error writing {filepath}: {e}"


def list_directory(directory: str) -> str:
    """List files and subdirectories in a directory."""
    try:
        resolved = (PROJECT_ROOT / directory).resolve()
        if not str(resolved).startswith(str(PROJECT_ROOT)):
            return f"Error: path {directory} is outside the project root."
        entries = sorted(os.listdir(resolved))
        return "\n".join(entries) if entries else "(empty directory)"
    except Exception as e:
        return f"Error listing {directory}: {e}"


def run_command(command: str, cwd: str = ".") -> str:
    """Run a shell command on the host. Returns stdout + stderr.
    Times out after 30 seconds. Dangerous commands are blocked."""
    blocked = ["rm -rf /", "format ", "del /f", "shutdown", "reboot"]
    if any(b in command.lower() for b in blocked):
        return f"Blocked: dangerous command detected."
    try:
        resolved_cwd = (PROJECT_ROOT / cwd).resolve()
        if not str(resolved_cwd).startswith(str(PROJECT_ROOT)):
            return f"Error: cwd {cwd} is outside the project root."
        result = subprocess.run(
            command,
            shell=True,
            cwd=str(resolved_cwd),
            capture_output=True,
            text=True,
            timeout=30,
        )
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
    except Exception as e:
        return f"Error running command: {e}"


COUNCILOR_TOOLS = [read_file, write_file, list_directory, run_command]


# ── System prompts ───────────────────────────────────────────────────────────

ESCALATION_SYSTEM_PROMPT = """You are The Councilor — the L2 meta-agent for Project Icarus.
You run on the host machine (X3R0) with full filesystem access.

Your job: execute the given intent by reading and modifying source files as needed.

Rules:
- Focus ONLY on the given intent. Do not explore unrelated files.
- Use read_file to understand context before making changes.
- Use write_file to make changes. Provide COMPLETE file contents, not diffs.
- Use list_directory to explore the project structure if needed.
- Use run_command sparingly and only when necessary (e.g., to check syntax).
- Do NOT run git commands — git operations are handled automatically after you finish.
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


def _git_workflow(intent: str, timestamp: int) -> str | None:
    """Branch, commit, push, and PR if files were changed. Returns PR URL or None."""
    git_check = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
    )
    if not git_check.stdout.strip():
        logger.info("No source changes detected — skipping git workflow.")
        return None

    changed_list = [
        line.strip().split(None, 1)[-1]
        for line in git_check.stdout.strip().split("\n")
        if line.strip()
    ]
    logger.info(f"Changes detected in: {changed_list}")

    branch_name = f"councilor/intent-{timestamp}"

    # Stash → main → feature branch
    subprocess.run(["git", "stash"], cwd=str(PROJECT_ROOT), capture_output=True)
    subprocess.run(["git", "checkout", "main"], cwd=str(PROJECT_ROOT), capture_output=True)
    create = subprocess.run(
        ["git", "checkout", "-b", branch_name],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
    )
    if create.returncode != 0:
        logger.warning(f"Branch creation failed: {create.stderr.strip()}")
        # Pop the stash and return — don't leave the repo in a dirty state
        subprocess.run(["git", "stash", "pop"], cwd=str(PROJECT_ROOT), capture_output=True)
        return None

    # Pop stash to bring changes onto the new branch
    subprocess.run(["git", "stash", "pop"], cwd=str(PROJECT_ROOT), capture_output=True)

    # Commit
    subprocess.run(["git", "add", "-A"], cwd=str(PROJECT_ROOT), capture_output=True)
    commit_msg = f"Councilor: {intent[:72]}"
    commit = subprocess.run(
        ["git", "commit", "-m", commit_msg],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
    )
    if commit.returncode != 0:
        logger.warning(f"Commit failed: {commit.stderr.strip()}")
        subprocess.run(["git", "checkout", "main"], cwd=str(PROJECT_ROOT), capture_output=True)
        return None

    # Push
    push = subprocess.run(
        ["git", "push", "origin", branch_name],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
    )
    if push.returncode != 0:
        logger.warning(f"Push failed: {push.stderr.strip()}")
        subprocess.run(["git", "checkout", "main"], cwd=str(PROJECT_ROOT), capture_output=True)
        return None

    logger.info(f"Pushed {branch_name} to origin")

    # PR
    pr_url = None
    github_token = os.getenv("GITHUB_TOKEN")
    repo_info = _get_repo_remote_info()
    if github_token and repo_info:
        owner, repo_name = repo_info
        changed_md = "\n".join(f"- `{f}`" for f in changed_list)
        pr_url = _create_pr_via_api(
            github_token, owner, repo_name, branch_name,
            title=f"Councilor: {intent[:60]}",
            body=f"## Automated changes by The Councilor\n\n**Intent:**\n{intent}\n\n**Changed files:**\n{changed_md}",
        )
        if pr_url:
            logger.info(f"PR created: {pr_url}")

    # Return to main
    subprocess.run(["git", "checkout", "main"], cwd=str(PROJECT_ROOT), capture_output=True)
    return pr_url


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

    memory_ctx = _get_memory_context()
    system = ESCALATION_SYSTEM_PROMPT
    if memory_ctx:
        system += "\n\n" + memory_ctx

    # Retry loop
    last_error = None
    for attempt in range(max_retries + 1):
        if attempt > 0:
            logger.info(f"Retry {attempt}/{max_retries}...")

        response = await agent_loop(
            task_type="escalation",
            initial_prompt=intent,
            tools=COUNCILOR_TOOLS,
            system_instruction=system,
        )

        # Check if response indicates failure
        if "error" in response.lower() and attempt < max_retries:
            last_error = response
            system += f"\n\n[PREVIOUS ATTEMPT FAILED]\n{response}\nPlease fix the issue and try again."
            continue

        # Success
        pr_url = _git_workflow(intent, timestamp)
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
    """Publish a response to Redis for the heartbeat to deliver."""
    r = await _get_redis()
    payload = json.dumps({
        "timestamp": timestamp,
        "type": resp_type,
        "message": message,
        "platform": platform,
        "chat_id": chat_id,
    })
    # Push to a list that the heartbeat polls
    await r.lpush("icarus:councilor:responses", payload)
    # Also publish to a specific channel for blocking consultations
    await r.publish(f"icarus:councilor:response:{timestamp}", payload)
    logger.info(f"Published {resp_type} response (timestamp={timestamp})")


async def _listen_for_requests():
    """Subscribe to Redis channel for incoming requests from L1."""
    r = await _get_redis()
    pubsub = r.pubsub()
    await pubsub.subscribe("icarus:councilor:requests")
    logger.info("[redis] Subscribed to icarus:councilor:requests")

<<<<<<< Updated upstream
    async for msg in pubsub.listen():
        if msg["type"] != "message":
            continue
=======
        if not question:
            logger.error("No question found in consultation payload. Discarding.")
            file_path.unlink()
            return

        logger.info(f"Consulting Gemini (read-only) [platform={platform}]: {question[:200]}")

        # Inject the question directly as immediate context, not as something to search for.
        # Prepend with strong instructions to avoid unnecessary project scanning.
        prompt = (
            "**IMMEDIATE TASK — READ-ONLY CONSULTATION**\n"
            "You are given a direct question below. DO NOT search the project or read source files first.\n"
            "Analyze and advise based on the question itself and your knowledge.\n"
            "Do not execute any commands or modify any files.\n\n"
            "QUESTION:\n"
            + question
        )
        cmd = ["gemini","--model", "gemini-3-flash-preview", "-p", prompt]
        proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=True,
            bufsize=1,
        )

        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        t_out = threading.Thread(
            target=_stream_pipe,
            args=(proc.stdout, lambda l: logger.info(f"[Gemini consult] {l}"), stdout_lines),
            daemon=True,
        )
        t_err = threading.Thread(
            target=_stream_pipe,
            args=(proc.stderr, lambda l: logger.info(f"[Gemini consult stderr] {l}"), stderr_lines),
            daemon=True,
        )
        t_out.start()
        t_err.start()
        t_out.join()
        t_err.join()
        proc.wait()

        stdout = '\n'.join(stdout_lines).strip()
        stderr = '\n'.join(stderr_lines).strip()

        if proc.returncode == 0:
            message = stdout or "(Gemini returned no output)"
        else:
            message = (
                f"Consultation failed (exit {proc.returncode})."
                + (f"\n\n{stdout}" if stdout else "")
                + (f"\n\nstderr: {stderr}" if stderr else "")
            )

        _write_consult_response(timestamp, message, platform, user_id, chat_id)
        file_path.rename(file_path.with_suffix(".completed"))

    except Exception as e:
        logger.error(f"Failed to process consultation {file_path.name}: {e}")
>>>>>>> Stashed changes
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
<<<<<<< Updated upstream
        r = await _get_redis()
        await r.ping()
        logger.info("[redis] Connection verified")
=======
        with open(file_path, 'r') as f:
            data = json.load(f)

        intent_description = data.get("intent")
        target_files = data.get("target_files", [])
        timestamp = data.get("timestamp", int(time.time()))
        platform = data.get("platform")
        user_id = data.get("user_id")
        chat_id = data.get("chat_id")

        if not intent_description:
            logger.error("No intent description found in payload. Discarding.")
            file_path.unlink()
            return

        logger.info(f"Target files: {target_files}")
        logger.info(f"Executing L2 Intent [platform={platform}]:\n{intent_description}")

        # Prepend strong focus instructions to prevent unnecessary full-project scanning.
        # Task-specific prompts help Gemini prioritize correctly without exploring every file.
        focused_intent = (
            "**IMMEDIATE TASK — EXECUTE THIS INTENT DIRECTLY**\n"
            "You are given a specific intent below. Focus on fulfilling ONLY this intent.\n"
            "Read source files only if they are directly needed to complete this task.\n"
            "Do not scan the entire project or explore unrelated areas.\n\n"
            "INTENT:\n"
            + intent_description
        )

        # Ensure we start from a clean main branch before letting Gemini work.
        # Stash any leftover uncommitted changes from a previous failed run, then
        # switch to main and create a dedicated feature branch for this escalation.
        branch_name = f"councilor/intent-{timestamp}"
        subprocess.run(["git", "stash"], cwd=str(PROJECT_ROOT), capture_output=True)
        checkout_main = subprocess.run(
            ["git", "checkout", "main"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True
        )
        if checkout_main.returncode != 0:
            logger.warning(f"Could not checkout main: {checkout_main.stderr.strip()} — will branch from current HEAD")
        create_branch = subprocess.run(
            ["git", "checkout", "-b", branch_name],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True
        )
        if create_branch.returncode == 0:
            logger.info(f"Working on branch: {branch_name}")
        else:
            logger.warning(f"Branch creation failed: {create_branch.stderr.strip()} — continuing without branch isolation")
            branch_name = None

        # Trigger Gemini CLI.
        # System prompt is loaded from GEMINI.md in the project root.
        # Popen + threads streams output in real time instead of buffering until exit.
        cmd = ["gemini", "--yolo", "--model", "gemini-3-flash-preview", "-p", focused_intent]
        proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=True,   # Required on Windows: gemini is installed as .cmd, not .exe
            bufsize=1,    # Line-buffered on Python's side; Node may still chunk internally
        )

        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        t_out = threading.Thread(
            target=_stream_pipe,
            args=(proc.stdout, lambda l: logger.info(f"[Gemini] {l}"), stdout_lines),
            daemon=True,
        )
        t_err = threading.Thread(
            target=_stream_pipe,
            args=(proc.stderr, lambda l: logger.info(f"[Gemini stderr] {l}"), stderr_lines),
            daemon=True,
        )
        t_out.start()
        t_err.start()
        t_out.join()
        t_err.join()
        proc.wait()

        returncode = proc.returncode
        stdout = '\n'.join(stdout_lines).strip()
        stderr = '\n'.join(stderr_lines).strip()

        if returncode == 0:
            logger.info("L2 execution successful.")
            if not stdout:
                logger.info("[Gemini] (no stdout captured)")

            # Detect whether gemini actually modified any tracked files.
            git_check = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True
            )
            files_changed = bool(git_check.stdout.strip())

            pr_url = None
            if files_changed:
                logger.info("Source changes detected.")
                changed_list = [line.strip().split(None, 1)[-1] for line in git_check.stdout.strip().split("\n") if line.strip()]

                # COMMIT + PUSH + PR on the feature branch
                if branch_name:
                    subprocess.run(["git", "add", "-A"], cwd=str(PROJECT_ROOT), capture_output=True)
                    commit_msg = f"Councilor: {intent_description[:72]}"
                    git_commit = subprocess.run(
                        ["git", "commit", "-m", commit_msg],
                        cwd=str(PROJECT_ROOT),
                        capture_output=True,
                        text=True
                    )
                    if git_commit.returncode == 0:
                        logger.info(f"Committed changes to {branch_name}")
                        git_push = subprocess.run(
                            ["git", "push", "origin", branch_name],
                            cwd=str(PROJECT_ROOT),
                            capture_output=True,
                            text=True
                        )
                        if git_push.returncode == 0:
                            logger.info(f"Pushed {branch_name} to origin")
                            github_token = os.getenv("GITHUB_TOKEN")
                            repo_info = _get_repo_remote_info()
                            if github_token and repo_info:
                                owner, repo = repo_info
                                changed_files_md = "\n".join(f"- `{f}`" for f in changed_list)
                                pr_body = (
                                    f"## Automated changes by The Councilor\n\n"
                                    f"**Intent:**\n{intent_description}\n\n"
                                    f"**Changed files:**\n{changed_files_md}"
                                )
                                pr_url = _create_pr_via_api(
                                    github_token, owner, repo, branch_name,
                                    title=f"Councilor: {intent_description[:60]}",
                                    body=pr_body
                                )
                                if pr_url:
                                    logger.info(f"PR created: {pr_url}")
                                else:
                                    logger.warning("PR creation returned no URL.")
                            else:
                                logger.warning("GITHUB_TOKEN or remote info missing — PR not created.")
                        else:
                            logger.warning(f"git push failed: {git_push.stderr.strip()}")
                    else:
                        logger.warning(f"git commit failed: {git_commit.stderr.strip()}")

                # Switch back to main — deployment is handled externally.
                subprocess.run(
                    ["git", "checkout", "main"],
                    cwd=str(PROJECT_ROOT),
                    capture_output=True
                )
                logger.info("Switched back to main. Deployment is up to the operator.")
            else:
                logger.info("No source changes — nothing to commit.")

            message = stdout or "(Councilor completed with no output)"
            if files_changed and pr_url:
                message += f"\n\nPR: {pr_url}"
            _write_response(timestamp, message, False, platform, user_id, chat_id)
            file_path.rename(file_path.with_suffix(".completed"))

            # Notify operator via the platform they used
            notify_msg = f"[Icarus Escalation Complete]\n\n{message}"
            if platform == "discord":
                _send_discord(notify_msg, chat_id)
            else:
                _send_telegram(notify_msg, chat_id)

        else:
            logger.error(f"L2 execution failed (exit {returncode})")

            error_message = (
                f"Councilor failed (exit {returncode})."
                + (f"\n\n{stdout}" if stdout else "")
                + (f"\n\nstderr: {stderr}" if stderr else "")
            )
            _write_response(timestamp, error_message, False, platform, user_id, chat_id)
            file_path.rename(file_path.with_suffix(".failed"))
            
            fail_msg = f"[Icarus Escalation Failed]\n\n{error_message}"
            if platform == "discord":
                _send_discord(fail_msg, chat_id)
            else:
                _send_telegram(fail_msg, chat_id)

>>>>>>> Stashed changes
    except Exception as e:
        logger.error(f"[redis] Cannot connect: {e}")
        logger.error("Ensure Redis is running and REDIS_URL is set correctly.")
        sys.exit(1)

    # Verify Google API key
    if not os.getenv("GOOGLE_API_KEY"):
        logger.error("GOOGLE_API_KEY is not set. Get one from https://aistudio.google.com/apikey")
        sys.exit(1)

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
