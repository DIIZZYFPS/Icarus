import sys
import time
import json
import logging
import subprocess
import threading
import os
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

def _write_response(timestamp: int, message: str, restart_performed: bool):
    """Atomically writes a response file for esc_tool.py to pick up in the container."""
    response_path = IPC_DIR / f"intent_{timestamp}.response.json"
    tmp_path = IPC_DIR / f"intent_{timestamp}.response.tmp"
    payload = {
        "message": message,
        "restart_performed": restart_performed
    }
    with open(tmp_path, 'w') as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp_path, response_path)
    logger.info(f"Wrote Councilor response to {response_path.name}")

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

        else:
            logger.error(f"L2 execution failed (exit {returncode})")

            error_message = (
                f"Councilor failed (exit {returncode})."
                + (f"\n\n{stdout}" if stdout else "")
                + (f"\n\nstderr: {stderr}" if stderr else "")
            )
            _write_response(timestamp, error_message, restart_performed=False)
            file_path.rename(file_path.with_suffix(".failed"))

    except Exception as e:
        logger.error(f"Failed to process {file_path.name}: {e}")
        try:
            # Best-effort: write a response so L1 isn't left polling forever
            timestamp = int(file_path.stem.split("_")[-1]) if "_" in file_path.stem else int(time.time())
            _write_response(timestamp, f"Councilor encountered an unexpected error: {e}", restart_performed=False)
            file_path.rename(file_path.with_suffix(".failed"))
        except Exception:
            pass

def main():
    ensure_directories()
    logger.info(f"Councilor Daemon started. Watching {IPC_DIR}...")

    while True:
        try:
            for file_path in IPC_DIR.glob("*.json"):
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
