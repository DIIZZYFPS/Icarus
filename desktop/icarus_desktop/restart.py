"""restart.py — restarting parts of Icarus (to load changes) without cutting a conversation off."""

import asyncio
import logging
from typing import Callable

logger = logging.getLogger("icarus_desktop")

# What restart_icarus may restart, in the order they go. The daemon is last: restarting it ends
# this process, and --no-block lets systemd carry the job out after we're gone.
RESTART_TARGETS = {
    "api": ("docker", "restart", "icarus-api", "icarus-email_triage-1"),   # both run ./backend
    "councilor": ("systemctl", "--user", "--no-block", "restart", "icarus-councilor.service"),
    "desktop": ("systemctl", "--user", "--no-block", "restart", "icarus-desktop.service"),
}


class Restarter:
    """Queues restarts. Nothing restarts mid-conversation: ready() waits for `quiet`, and holds the
    Councilor while `tasks_busy` says it's running tasks."""

    def __init__(self, quiet: Callable[[], bool], tasks_busy: Callable[[], bool]):
        self.pending: set[str] = set()   # RESTART_TARGETS keys
        self.quiet = quiet
        self.tasks_busy = tasks_busy

    def schedule(self, args: dict) -> tuple[bool, str]:
        parts = args.get("parts")
        parts = [parts] if isinstance(parts, str) else list(parts or [])
        parts = [str(p).strip().lower() for p in parts if str(p).strip()]
        if not parts or parts == ["cancel"]:
            had, self.pending = bool(self.pending), set()
            return True, "Cancelled the pending restart." if had else "No restart was pending."
        unknown = [p for p in parts if p not in RESTART_TARGETS]
        if unknown:
            return False, f"can't restart {', '.join(unknown)}; choose from {', '.join(RESTART_TARGETS)}"
        self.pending.update(parts)
        names = ", ".join(p for p in RESTART_TARGETS if p in self.pending)
        wait = " and the running tasks finish" if "councilor" in self.pending and self.tasks_busy() else ""
        return True, f"Restart of {names} queued; it happens once this conversation ends{wait}."

    def ready(self) -> bool:
        if not self.pending or not self.quiet():
            return False
        return not ("councilor" in self.pending and self.tasks_busy())

    async def run(self):
        from .actions import run
        parts = [p for p in RESTART_TARGETS if p in self.pending]
        self.pending = set()
        logger.info(f"[restart] restarting {', '.join(parts)}")
        for part in parts:
            try:
                code, out = await run(*RESTART_TARGETS[part], timeout=90)
            except Exception as e:
                code, out = -1, str(e)
            if code != 0:
                logger.warning(f"[restart] {part} failed ({code}): {out[:200]}")

    async def loop(self):
        while True:
            await asyncio.sleep(2)
            if self.ready():
                await self.run()
