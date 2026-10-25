"""Mic capture and speech playback through PipeWire's pw-record / pw-play (raw PCM)."""

import asyncio
import logging
import math
import time
from array import array
from typing import Callable

logger = logging.getLogger(__name__)

MIC_RATE = 16000


def rms(pcm: bytes) -> float:
    """Root-mean-square of s16le PCM, scaled to 0..1."""
    samples = array("h", pcm[: len(pcm) // 2 * 2])
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples)) / 32768.0


class Recorder:
    """Records 16 kHz mono s16le from the default source while open."""

    def __init__(self, on_level: Callable[[float], None] | None = None, max_seconds: float = 30.0):
        self.on_level = on_level
        self.max_bytes = int(max_seconds * MIC_RATE * 2)
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._buf = bytearray()

    @property
    def active(self) -> bool:
        return self._proc is not None

    async def start(self) -> None:
        if self._proc:
            return
        self._buf = bytearray()
        self._proc = await asyncio.create_subprocess_exec(
            "pw-record", "--raw", "--rate", str(MIC_RATE), "--channels", "1", "--format", "s16",
            "--latency", "20ms", "-",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        self._reader = asyncio.create_task(self._read(self._proc))

    async def _read(self, proc: asyncio.subprocess.Process) -> None:
        # Holds its own reference: stop() clears self._proc before this loop drains.
        while True:
            chunk = await proc.stdout.read(1600)  # 50 ms
            if not chunk:
                return
            if len(self._buf) < self.max_bytes:
                self._buf.extend(chunk)
            if self.on_level:
                self.on_level(rms(chunk))

    async def stop(self) -> bytes:
        proc, self._proc = self._proc, None
        if not proc:
            return b""
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), 2)
        except (ProcessLookupError, asyncio.TimeoutError):
            proc.kill()
        if self._reader:
            try:
                await asyncio.wait_for(self._reader, 1)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._reader.cancel()
            except Exception:
                logger.exception("[mic] reader failed")
        return bytes(self._buf)


class Player:
    """Plays s16le mono PCM; reports a speech level on the playback clock."""

    def __init__(self):
        self._proc: asyncio.subprocess.Process | None = None

    async def play(self, pcm: bytes, rate: int, on_level: Callable[[float], None] | None = None) -> bool:
        """Returns False if playback was stopped early."""
        await self.stop()
        proc = await asyncio.create_subprocess_exec(
            "pw-play", "--raw", "--rate", str(rate), "--channels", "1", "--format", "s16", "-",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        self._proc = proc
        frame = int(rate * .05) * 2
        duration = len(pcm) / 2 / rate

        async def feed():
            try:
                proc.stdin.write(pcm)
                await proc.stdin.drain()
                proc.stdin.close()
            except (BrokenPipeError, ConnectionResetError):
                pass

        feeder = asyncio.create_task(feed())
        start = time.monotonic()
        if on_level:
            # Emit levels on the wall clock so the avatar pulses with what you hear.
            while self._proc is proc:
                elapsed = time.monotonic() - start
                if elapsed >= duration:
                    break
                i = int(elapsed / .05) * frame
                on_level(rms(pcm[i:i + frame]))
                await asyncio.sleep(.05)
            on_level(0.0)
        try:
            await asyncio.wait_for(proc.wait(), timeout=max(2.0, duration + 2))
        except asyncio.TimeoutError:
            proc.kill()
        await feeder
        finished = self._proc is proc
        if finished:
            self._proc = None
        return finished and proc.returncode == 0

    async def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc and proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
