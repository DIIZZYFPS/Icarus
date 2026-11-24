"""link.py — the daemon's two connections: the avatar overlay's socket and the WebSocket to icarus-api."""

import asyncio
import contextlib
import json
import logging
import os

from .config import Config

logger = logging.getLogger("icarus_desktop")


class RendererBus:
    """Unix socket the overlay connects to; every event is broadcast as one JSON line."""

    def __init__(self, path, snapshot):
        self.path = path
        self.snapshot = snapshot  # () -> list of events to replay for a newly connected renderer
        self.writers: set[asyncio.StreamWriter] = set()

    async def start(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()
        self.server = await asyncio.start_unix_server(self._client, path=str(self.path))
        os.chmod(self.path, 0o600)

    async def _client(self, reader, writer):
        self.writers.add(writer)
        for ev in self.snapshot():
            self._write(writer, ev)
        try:
            while await reader.readline():
                pass
        finally:
            self.writers.discard(writer)

    def _write(self, writer, ev):
        try:
            writer.write((json.dumps(ev) + "\n").encode())
        except Exception:
            self.writers.discard(writer)

    def emit(self, ev: dict):
        for writer in list(self.writers):
            self._write(writer, ev)


class Link:
    """The WebSocket to icarus-api, reconnecting forever."""

    def __init__(self, config: Config, on_message):
        self.config = config
        self.on_message = on_message
        self.ws = None
        self.connected = asyncio.Event()

    async def run(self):
        from websockets.asyncio.client import connect
        delay = 1.0
        while True:
            try:
                async with connect(self.config.api_url, open_timeout=5, ping_interval=20) as ws:
                    await ws.send(json.dumps({"type": "hello", "token": self.config.token}))
                    first = json.loads(await asyncio.wait_for(ws.recv(), 5))
                    if first.get("type") != "ready":
                        logger.error(f"[link] icarus-api refused the connection: {first}")
                        await asyncio.sleep(30)
                        continue
                    self.ws = ws
                    self.connected.set()
                    delay = 1.0
                    logger.info("[link] connected to icarus-api")
                    async for raw in ws:
                        try:
                            await self.on_message(json.loads(raw))
                        except Exception:
                            logger.exception("[link] message handler failed")
            except (OSError, asyncio.TimeoutError) as e:
                logger.debug(f"[link] connect failed: {e}")
            except Exception as e:
                logger.warning(f"[link] disconnected: {e}")
            finally:
                self.ws = None
                self.connected.clear()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15)

    async def send(self, message: dict) -> bool:
        ws = self.ws
        if ws is None:
            return False
        try:
            await ws.send(json.dumps(message))
            return True
        except Exception:
            return False
