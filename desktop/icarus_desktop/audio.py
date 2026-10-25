"""Mic capture and speech playback through PipeWire's pw-record / pw-play (raw PCM)."""

import asyncio
import contextlib
import heapq
import logging
import math
import time
from array import array
from collections import deque
from typing import Callable

import numpy as np

from .voice import MachineRoom

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




class _Segment:
    __slots__ = ("audio", "pos", "marks", "done", "start")

    def __init__(self, audio: np.ndarray, marks, done: asyncio.Future | None):
        self.audio, self.marks, self.done = audio, marks, done
        self.pos, self.start = 0, None


class Player:
    """Speech output through one continuous stereo pw-play stream, played through MachineRoom.

    begin() opens the stream (no-op if it's already open at that rate), gap() and say() queue
    audio on it, end() lets the room ring out and closes it. Between lines the stream keeps
    running with the room's tail and noise floor, so it never drops to digital silence.
    Audio is written a little ahead of a wall clock; marks and levels fire on that clock."""

    LEAD = .2        # seconds written ahead of the playback clock
    LATENCY = .05    # roughly what pw-play buffers before the speakers
    TICK = .02
    RING_OUT = .45   # room tail + floor fade after the last line

    def __init__(self, room: float = 1.0):
        self.room_amount = room
        self._proc: asyncio.subprocess.Process | None = None
        self._pump: asyncio.Task | None = None
        self._room: MachineRoom | None = None
        self._rate = 0
        self._segments: deque[_Segment] = deque()
        self._marks: list = []   # heap of (frame, seq, callback)
        self._seq = 0
        self._levels: deque = deque()
        self._pending: set[asyncio.Future] = set()
        self._on_level: Callable[[float], None] | None = None
        self._written = 0
        self._start = 0.0
        self._closing = False

    @property
    def is_open(self) -> bool:
        return self._proc is not None and not self._closing

    async def begin(self, rate: int, on_level: Callable[[float], None] | None = None) -> None:
        if self.is_open and rate == self._rate:
            self._on_level = on_level or self._on_level
            return
        if self._closing and self._pump:
            with contextlib.suppress(Exception):
                await self._pump
        await self.stop()
        proc = await asyncio.create_subprocess_exec(
            "pw-play", "--raw", "--rate", str(rate), "--channels", "2", "--format", "s16",
            "--latency", "40ms", "-",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        self._proc, self._rate, self._on_level = proc, rate, on_level
        self._room = MachineRoom(rate, self.room_amount)
        self._room.floor_on()
        self._segments.clear()
        self._marks.clear()
        self._levels.clear()
        self._written, self._closing = 0, False
        self._start = time.monotonic()
        self._pump = asyncio.create_task(self._run(proc))

    def gap(self, seconds: float) -> None:
        """Queue a pause; the room keeps ringing through it."""
        if self._proc and seconds > 0:
            self._segments.append(_Segment(np.zeros(int(seconds * self._rate), np.float32), (), None))

    async def say(self, pcm: bytes, marks=()) -> bool:
        """Queue mono s16le PCM and wait until it has played. marks are (seconds, callback) pairs
        fired when playback reaches that point of this line. False if playback was stopped."""
        if not self._proc:
            return False
        audio = np.frombuffer(pcm[: len(pcm) // 2 * 2], np.int16).astype(np.float32) / 32768.0
        done = asyncio.get_running_loop().create_future()
        self._pending.add(done)
        done.add_done_callback(self._pending.discard)
        self._segments.append(_Segment(audio, tuple(marks), done))
        return await done

    async def play(self, pcm: bytes, rate: int, on_level: Callable[[float], None] | None = None) -> bool:
        """One-off line: joins an open stream if there is one, then closes it."""
        await self.begin(rate, on_level)
        ok = await self.say(pcm)
        await self.end()
        return ok

    async def end(self) -> None:
        """Let the queued audio finish and the room ring out, then close the stream."""
        pump = self._pump
        if not self._proc or not pump:
            return
        self._closing = True
        with contextlib.suppress(Exception):
            await pump

    async def stop(self) -> None:
        """Cut off immediately (barge-in, new turn)."""
        proc, pump = self._proc, self._pump
        self._proc = self._pump = None
        self._closing = False
        if pump and not pump.done():
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pump
        if proc and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
                await proc.wait()
        self._abort()

    # ── internals ───────────────────────────────────────────────────────────

    def _schedule(self, frame: int, callback: Callable[[], None]) -> None:
        self._seq += 1
        heapq.heappush(self._marks, (frame, self._seq, callback))

    def _pull(self, n: int) -> np.ndarray:
        out = np.zeros(n, np.float32)
        filled = 0
        lat = int(self._room.latency * self._rate)
        while filled < n and self._segments:
            seg = self._segments[0]
            if seg.start is None:
                seg.start = self._written + filled
                for off, cb in seg.marks:
                    self._schedule(seg.start + lat + int(off * self._rate), cb)
            take = min(n - filled, seg.audio.size - seg.pos)
            out[filled:filled + take] = seg.audio[seg.pos:seg.pos + take]
            seg.pos += take
            filled += take
            if seg.pos >= seg.audio.size:
                self._segments.popleft()
                if seg.done:
                    fut = seg.done
                    self._schedule(seg.start + lat + seg.audio.size, lambda f=fut: f.done() or f.set_result(True))
        return out

    def _fire(self, played: int) -> None:
        while self._marks and self._marks[0][0] <= played:
            _, _, cb = heapq.heappop(self._marks)
            try:
                cb()
            except Exception:
                logger.exception("[tts] playback mark failed")

    def _level(self, played: int) -> float:
        while len(self._levels) > 1 and self._levels[0][0] < played:
            self._levels.popleft()
        return self._levels[0][1] if self._levels else 0.0

    def _abort(self) -> None:
        for fut in list(self._pending):
            if not fut.done():
                fut.set_result(False)
        self._segments.clear()
        self._marks.clear()
        self._levels.clear()

    async def _run(self, proc: asyncio.subprocess.Process) -> None:
        rate, block, room = self._rate, MachineRoom.BLOCK, self._room
        lead, latency = int(self.LEAD * rate), int(self.LATENCY * rate)
        close_at = None
        level_due = 0.0
        try:
            while proc.returncode is None:
                clock = int((time.monotonic() - self._start) * rate)
                while self._written < clock + lead and (close_at is None or self._written < close_at):
                    mono = self._pull(block)
                    out = room.process(mono)
                    self._written += block
                    self._levels.append((self._written, float(np.sqrt((mono * mono).mean()))))
                    proc.stdin.write((out.T * 32767).astype("<i2").tobytes())
                    if self._closing and close_at is None and not self._segments:
                        room.floor_off()
                        close_at = self._written + int(self.RING_OUT * rate)
                if self._closing and close_at is None and not self._segments:
                    room.floor_off()
                    close_at = self._written + int(self.RING_OUT * rate)
                await proc.stdin.drain()
                played = clock - latency
                self._fire(played)
                level = self._level(played)
                now = time.monotonic()
                if self._on_level and now >= level_due:  # the avatar only needs ~20 levels a second
                    level_due = now + .05
                    self._on_level(level)
                if close_at is not None and self._written >= close_at and played >= self._written:
                    break
                await asyncio.sleep(self.TICK)
        except (BrokenPipeError, ConnectionResetError):
            logger.warning("[tts] playback stream closed early")
        finally:
            if self._proc is proc:
                self._proc = self._pump = None
                self._closing = False
                self._abort()
            if self._on_level:
                with contextlib.suppress(Exception):
                    self._on_level(0.0)
            with contextlib.suppress(Exception):
                proc.stdin.close()
            if proc.returncode is None:
                # pw-play drains and exits on its own; don't hold up the follow-up mic for it.
                asyncio.get_running_loop().create_task(self._reap(proc))

    @staticmethod
    async def _reap(proc: asyncio.subprocess.Process) -> None:
        try:
            await asyncio.wait_for(proc.wait(), 3)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
                await proc.wait()
