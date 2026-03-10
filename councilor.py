import sys
import time
import json
import logging
import subprocess
import threading
import os
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

def _send_telegram(message: str):
    """Send a message to DIIZZY via the Telegram bot."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("ALLOWED_CHAT_ID")
    if not token or not chat_id:
        logger.warning("TELEGRAM_BOT_TOKEN or ALLOWED_CHAT_ID not set — skipping Telegram notification")
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = json.dumps({"chat_id": int(chat_id), "text": message}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
        logger.info("Telegram notification sent.")
    except Exception as e:
        logger.warning(f"Failed to send Telegram notification: {e}")

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

def _write_response(timestamp: int, message: str, restart_performed: bool):
    """Atomically writes a response file to the mail directory for heartbeat to deliver."""
    response_path = MAIL_DIR / f"intent_{timestamp}.response.json"
    tmp_path = MAIL_DIR / f"intent_{timestamp}.response.tmp"
    payload = {
        "message": message,
        "restart_performed": restart_performed
    }
    with open(tmp_path, 'w') as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp_path, response_path)
    logger.info(f"Wrote Councilor response to {response_path.name}")

def _write_consult_response(timestamp: int, message: str):
    """Atomically writes a consultation response file to the mail directory."""
    response_path = MAIL_DIR / f"consult_{timestamp}.response.json"
    tmp_path = MAIL_DIR / f"consult_{timestamp}.response.tmp"
    payload = {"message": message}
    with open(tmp_path, 'w') as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp_path, response_path)
    logger.info(f"Wrote consultation response to {response_path.name}")

def process_consultation(file_path: Path):
    """Handles a read-only advisory consultation request from Icarus."""
    logger.info(f"Consultation detected: {file_path.name}")
    try:
        with open(file_path, 'r') as f:
            data = json.load(f)

        question = data.get("question")
        timestamp = data.get("timestamp", int(time.time()))

        if not question:
            logger.error("No question found in consultation payload. Discarding.")
            file_path.unlink()
            return

        logger.info(f"Consulting Gemini (read-only): {question[:200]}")

        # Advisory prefix instructs Gemini not to execute anything
        prompt = (
            "CONSULTATION (read-only advisory — analyse and advise only, "
            "do not execute any commands or modify any files):\n\n"
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

        _write_consult_response(timestamp, message)
        file_path.rename(file_path.with_suffix(".completed"))

    except Exception as e:
        logger.error(f"Failed to process consultation {file_path.name}: {e}")
        try:
            timestamp = int(file_path.stem.split("_")[-1]) if "_" in file_path.stem else int(time.time())
            _write_consult_response(timestamp, f"Councilor encountered an error during consultation: {e}")
            file_path.rename(file_path.with_suffix(".failed"))
        except Exception:
            pass

def process_escalation(file_path: Path):
    """Reads the escalation intent from the L1 model and triggers Gemini CLI."""
    logger.info(f"Escalation detected: {file_path.name}")
    try:
        with open(file_path, 'r') as f:
            data = json.load(f)

        intent_description = data.get("intent")
        target_files = data.get("target_files", [])
        timestamp = data.get("timestamp", int(time.time()))

        if not intent_description:
            logger.error("No intent description found in payload. Discarding.")
            file_path.unlink()
            return

        logger.info(f"Target files: {target_files}")
        logger.info(f"Executing L2 Intent:\n{intent_description}")

        # Trigger Gemini CLI.
        # System prompt is loaded from GEMINI.md in the project root.
        # Popen + threads streams output in real time instead of buffering until exit.
        cmd = ["gemini", "--yolo", "--model", "gemini-3-flash-preview", "-p", intent_description]
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

            restart_performed = False
            if files_changed:
                logger.info("Source changes detected — restarting icarus-api...")

                # RESTART PROTOCOL: Write info file for the daemon to parse on startup
                restart_info_path = PROJECT_ROOT / "workspace" / "ipc" / "restart_info.json"
                try:
                    changed_list = [line.strip().split(None, 1)[-1] for line in git_check.stdout.strip().split("\n") if line.strip()]
                    restart_data = {
                        "reason": "Councilor Escalation: Source Modification",
                        "files": changed_list,
                        "context": intent_description
                    }
                    with open(restart_info_path, 'w') as f:
                        json.dump(restart_data, f, indent=2)
                    logger.info(f"Wrote restart context to {restart_info_path.name}")
                except Exception as e:
                    logger.warning(f"Failed to write restart context: {e}")

                subprocess.run(
                    ["docker", "compose", "restart", "icarus-api"],
                    cwd=str(PROJECT_ROOT)
                )
                restart_performed = True
            else:
                logger.info("No source changes — skipping container restart.")

            message = stdout or "(Councilor completed with no output)"
            _write_response(timestamp, message, restart_performed)
            file_path.rename(file_path.with_suffix(".completed"))

            # Notify DIIZZY via Telegram — Icarus returned immediately and doesn't poll
            notify = f"[Icarus Escalation Complete]\n\n{message}"
            if restart_performed:
                notify += "\n\n[Container restarted — source changes applied.]"
            _send_telegram(notify)

        else:
            logger.error(f"L2 execution failed (exit {returncode})")

            error_message = (
                f"Councilor failed (exit {returncode})."
                + (f"\n\n{stdout}" if stdout else "")
                + (f"\n\nstderr: {stderr}" if stderr else "")
            )
            _write_response(timestamp, error_message, restart_performed=False)
            file_path.rename(file_path.with_suffix(".failed"))
            _send_telegram(f"[Icarus Escalation Failed]\n\n{error_message}")

    except Exception as e:
        logger.error(f"Failed to process {file_path.name}: {e}")
        try:
            timestamp = int(file_path.stem.split("_")[-1]) if "_" in file_path.stem else int(time.time())
            error_msg = f"Councilor encountered an unexpected error: {e}"
            _write_response(timestamp, error_msg, restart_performed=False)
            file_path.rename(file_path.with_suffix(".failed"))
            _send_telegram(f"[Icarus Escalation Error]\n\n{error_msg}")
        except Exception:
            pass

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
