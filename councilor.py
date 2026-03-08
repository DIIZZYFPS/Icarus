import sys
import time
import json
import logging
import subprocess
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

def ensure_directories():
    IPC_DIR.mkdir(parents=True, exist_ok=True)

def process_escalation(file_path: Path):
    """Reads the escalation intent from the L1 model and triggers Gemini CLI."""
    logger.info(f"Escalation detected: {file_path.name}")
    try:
        with open(file_path, 'r') as f:
            data = json.load(f)
            
        intent_description = data.get("intent")
        target_files = data.get("target_files", [])
        
        if not intent_description:
            logger.error("No intent description found in payload. Discarding.")
            file_path.unlink()
            return
            
        logger.info(f"Target files: {target_files}")
        logger.info(f"Executing L2 Intent:\n{intent_description}")
        
        # Trigger Gemini CLI
        # System prompt is loaded from GEMINI.md in the project root
        cmd = ["gemini", "-p", intent_description]
        
        # In a real environment we might direct the output to specific files or use an auto-apply tool
        # For now, we execute the subcommand in the project root
        result = subprocess.run(
            cmd,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            shell=True  # Required on Windows: npm installs gemini as .cmd, not .exe
        )
        
        if result.returncode == 0:
            logger.info("L2 execution successful.")
            if result.stderr.strip():
                logger.info(f"[Gemini stderr]\n{result.stderr.strip()}")
            if result.stdout.strip():
                logger.info(f"[Gemini response]\n{result.stdout.strip()}")
            else:
                logger.info("[Gemini response] (no stdout captured)")
            logger.info("Restarting Docker containers to apply changes...")
            subprocess.run(["docker", "compose", "restart", "icarus-api"], cwd=str(PROJECT_ROOT))
            file_path.rename(file_path.with_suffix(".completed"))
        else:
            logger.error(f"L2 execution failed (exit {result.returncode})")
            if result.stdout.strip():
                logger.error(f"[Gemini stdout]\n{result.stdout.strip()}")
            if result.stderr.strip():
                logger.error(f"[Gemini stderr]\n{result.stderr.strip()}")
            file_path.rename(file_path.with_suffix(".failed"))
            
    except Exception as e:
        logger.error(f"Failed to process {file_path.name}: {e}")
        try:
             file_path.rename(file_path.with_suffix(".failed"))
        except:
             pass

def main():
    ensure_directories()
    logger.info(f"Councilor Daemon started. Watching {IPC_DIR}...")
    
    # Simple polling loop (avoiding watchdog dependency for now to keep host reqs light)
    while True:
        try:
            for file_path in IPC_DIR.glob("*.json"):
                process_escalation(file_path)
            time.sleep(2) # Poll every 2 seconds
        except KeyboardInterrupt:
            logger.info("Shutting down Councilor.")
            break
        except Exception as e:
            logger.error(f"Polling error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
