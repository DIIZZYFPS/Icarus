import sys
import time
import json
import logging
import subprocess
import threading
import os
import re
import urllib.request
from pathlib import Path

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - THE COUNCILOR - %(levelname)s - %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.resolve()
IPC_DIR = PROJECT_ROOT / "workspace" / "ipc" / "escalation"
MAIL_DIR = PROJECT_ROOT / "workspace" / "ipc" / "mail"

def _load_env():
    """Load variables from .env into os.environ. Handles BOM and CRLF line endings."""
    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists():
        return
    with open(env_file, 'r', encoding='utf-8-sig') as f:
        for line in f:
            line = line.strip().rstrip('\r')
            if line and not line.startswith('#') and '=' in line:
                key, _, val = line.partition('=')
                os.environ.setdefault(key.strip(), val.strip())

def _send_telegram(message: str, chat_id: str = None):
    """Send a message to DIIZZY via the Telegram bot."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or os.getenv("ALLOWED_CHAT_ID")
    if not token or not chat_id:
        logger.warning("TELEGRAM_BOT_TOKEN or chat_id not set — skipping Telegram notification")
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = json.dumps({"chat_id": int(chat_id), "text": message}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
        logger.info("Telegram notification sent.")
    except Exception as e:
        logger.warning(f"Failed to send Telegram notification: {e}")

def _split_discord_message(text: str, limit: int = 2000) -> list:
    """Split text into chunks at newline boundaries where possible, respecting Discord's limit."""
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

def _send_discord(message: str, channel_id: str = None):
    """Send a message to DIIZZY via the Discord bot API."""
    token = os.getenv("DISCORD_BOT_TOKEN")
    channel_id = channel_id or os.getenv("DISCORD_ALLOWED_CHANNEL_ID")
    if not token or not channel_id:
        logger.warning("DISCORD_BOT_TOKEN or channel_id not set — skipping Discord notification")
        return
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
        "User-Agent": "Icarus-Councilor-v0.1.0",
    }
    chunks = _split_discord_message(message)
    for i, chunk in enumerate(chunks):
        try:
            data = json.dumps({"content": chunk}).encode()
            req = urllib.request.Request(url, data=data, headers=headers)
            urllib.request.urlopen(req, timeout=10)
            logger.info(f"Discord notification sent (chunk {i + 1}/{len(chunks)}).")
        except Exception as e:
            remaining = len(chunks) - i
            logger.warning(f"Failed to send Discord notification chunk {i + 1}/{len(chunks)} ({remaining} chunk(s) skipped): {e}")
            break

def _get_repo_remote_info() -> tuple:
    """Parse owner and repo name from the git remote origin URL. Returns (owner, repo) or None."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True
        )
        if result.returncode != 0:
            return None
        url = result.stdout.strip()
        # Matches both HTTPS (https://github.com/owner/repo.git) and SSH (git@github.com:owner/repo.git)
        match = re.search(r'github\.com[:/]([^/]+)/([^/\s]+?)(?:\.git)?$', url)
        if match:
            return match.group(1), match.group(2)
        return None
    except Exception:
        return None

def _create_pr_via_api(token: str, owner: str, repo: str, branch: str, title: str, body: str) -> str:
    """Create a GitHub PR via REST API. Returns the PR HTML URL or an error string."""
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
                "User-Agent": "Icarus-Councilor-v0.1.0",
            }
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            pr_data = json.loads(resp.read().decode())
            return pr_data.get("html_url", "(no URL in response)")
    except Exception as e:
        logger.warning(f"Failed to create PR via GitHub API: {e}")
        return None

def _stream_pipe(pipe, log_fn, collector):
    """Read lines from a subprocess pipe, log each one, and collect for later."""
    for line in iter(pipe.readline, ''):
        stripped = line.rstrip('\n')
        if stripped:
            log_fn(stripped)
            collector.append(stripped)
    pipe.close()

def ensure_directories():
    IPC_DIR.mkdir(parents=True, exist_ok=True)
    MAIL_DIR.mkdir(parents=True, exist_ok=True)

def _write_response(timestamp: int, message: str, restart_performed: bool, platform: str = None, user_id: str = None, chat_id: str = None):
    """Atomically writes a response file to the mail directory for heartbeat to deliver."""
    response_path = MAIL_DIR / f"intent_{timestamp}.response.json"
    tmp_path = MAIL_DIR / f"intent_{timestamp}.response.tmp"
    payload = {
        "message": message,
        "restart_performed": restart_performed,
        "platform": platform,
        "user_id": user_id,
        "chat_id": chat_id
    }
    with open(tmp_path, 'w') as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp_path, response_path)
    logger.info(f"Wrote Councilor response to {response_path.name}")

def _write_consult_response(timestamp: int, message: str, platform: str = None, user_id: str = None, chat_id: str = None):
    """Atomically writes a consultation response file to the mail directory."""
    response_path = MAIL_DIR / f"consult_{timestamp}.response.json"
    tmp_path = MAIL_DIR / f"consult_{timestamp}.response.tmp"
    payload = {
        "message": message,
        "platform": platform,
        "user_id": user_id,
        "chat_id": chat_id
    }
    with open(tmp_path, 'w') as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp_path, response_path)
    logger.info(f"Wrote consultation response to {response_path.name}")

def process_consultation(file_path: Path):
    """Handles a read-only advisory consultation request from Icarus."""
    logger.info(f"Consultation detected: {file_path.name}")
    platform = None
    user_id = None
    chat_id = None
    try:
        with open(file_path, 'r') as f:
            data = json.load(f)

        question = data.get("question")
        timestamp = data.get("timestamp", int(time.time()))
        platform = data.get("platform")
        user_id = data.get("user_id")
        chat_id = data.get("chat_id")

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
        try:
            timestamp = int(file_path.stem.split("_")[-1]) if "_" in file_path.stem else int(time.time())
            _write_consult_response(timestamp, f"Councilor encountered an error during consultation: {e}", platform, user_id, chat_id)
            file_path.rename(file_path.with_suffix(".failed"))
        except Exception:
            pass

def process_escalation(file_path: Path):
    """Reads the escalation intent from the L1 model and triggers Gemini CLI."""
    logger.info(f"Escalation detected: {file_path.name}")
    platform = None
    user_id = None
    chat_id = None
    try:
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

    except Exception as e:
        logger.error(f"Failed to process {file_path.name}: {e}")
        try:
            timestamp = int(file_path.stem.split("_")[-1]) if "_" in file_path.stem else int(time.time())
            error_msg = f"Councilor encountered an unexpected error: {e}"
            _write_response(timestamp, error_msg, False, platform, user_id, chat_id)
            file_path.rename(file_path.with_suffix(".failed"))
            
            err_notify = f"[Icarus Escalation Error]\n\n{error_msg}"
            if platform == "discord":
                _send_discord(err_notify, chat_id)
            else:
                _send_telegram(err_notify, chat_id)
        except Exception as inner_e:
            logger.error(f"Failed to write error response for {file_path.name}: {inner_e}")

def main():
    _load_env()
    ensure_directories()
    logger.info(f"Councilor Daemon started. Watching {IPC_DIR}...")

    while True:
        try:
            for file_path in IPC_DIR.glob("consult_*.json"):
                process_consultation(file_path)
            for file_path in IPC_DIR.glob("intent_*.json"):
                process_escalation(file_path)
            time.sleep(2)
        except KeyboardInterrupt:
            logger.info("Shutting down Councilor.")
            break
        except Exception as e:
            logger.error(f"Polling error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
