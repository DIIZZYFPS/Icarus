"""
desktop_session.py — the live link to the host desktop daemon (the avatar).

The daemon holds one WebSocket to /ws/desktop (backend/routes/desktop.py).
While it is connected, desktop tools (backend/agent/desktop_tools.py) can ask
it to do things on the operator's PC: send an `action`, wait for the matching
`action_result`. The daemon is the side that decides whether an action is
allowed — this module only carries requests and answers.

There is at most one active session: one daemon, one operator, one desktop.
A newer connection replaces an older one.
"""

import asyncio
import itertools
import logging
import os
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

ACTION_TIMEOUT_SECONDS = float(os.getenv("DESKTOP_ACTION_TIMEOUT_SECONDS", "15"))


class DesktopUnavailable(Exception):
    """The daemon is not connected, or disconnected while an action was pending."""


class DesktopSession:
    def __init__(self, send: Callable[[dict], Awaitable[None]]):
        self._send = send
        self._pending: dict[str, asyncio.Future] = {}
        self._ids = itertools.count(1)
        self.closed = False

    async def call(self, name: str, args: dict[str, Any], timeout: float | None = None) -> dict:
        """Ask the daemon to run one action. Returns {"ok": bool, "result": ...}.
        Raises DesktopUnavailable or asyncio.TimeoutError."""
        if self.closed:
            raise DesktopUnavailable("desktop daemon is not connected")
        action_id = f"a{next(self._ids)}"
        future = asyncio.get_running_loop().create_future()
        self._pending[action_id] = future
        try:
            await self._send({"type": "action", "id": action_id, "name": name, "args": args})
            return await asyncio.wait_for(future, timeout or ACTION_TIMEOUT_SECONDS)
        finally:
            self._pending.pop(action_id, None)

    def resolve(self, action_id: str, ok: Any, result: Any) -> None:
        future = self._pending.get(action_id)
        if future and not future.done():
            future.set_result({"ok": bool(ok), "result": result})

    def close(self) -> None:
        self.closed = True
        for future in self._pending.values():
            if not future.done():
                future.set_exception(DesktopUnavailable("desktop daemon disconnected"))
        self._pending.clear()


_active: DesktopSession | None = None


def set_active(session: DesktopSession) -> None:
    global _active
    if _active is not None and _active is not session:
        logger.info("[desktop] a new daemon connection replaced the previous one")
        _active.close()
    _active = session


def clear_active(session: DesktopSession) -> None:
    global _active
    if _active is session:
        _active = None


def get_active() -> DesktopSession | None:
    if _active is not None and _active.closed:
        return None
    return _active
