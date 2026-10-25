"""
daemon.py — the Icarus desktop daemon: eyes, ears and hands for the avatar.

  hotkey ─ctl socket─▶ daemon ─pw-record─▶ faster-whisper ─/ws/desktop─▶ icarus-api (Icarus L1)
                         │◀──────────── status / action / reply / ambient ─────────┘
                         ├─ actions (hyprctl, playerctl, Spotify) ─▶ action_result
                         ├─ Piper ─pw-play─▶ speakers
                         └─ events ─renderer socket─▶ avatar overlay

The daemon owns the conversation state (ADR icarus_desktop_avatar.md §6.5) and
tells the renderer what to show. The renderer derives poses and timing itself.
"""

import asyncio
import contextlib
import itertools
import json
import logging
import os
import time
import uuid

from .actions import ActionError, Actions
from .audio import Player, Recorder
from .config import Config
from .speech import SpeechToText, TextToSpeech, sentences

logger = logging.getLogger("icarus_desktop")

BUSY_STATES = {"thinking", "acting", "confirm", "speaking"}


class EndOfSpeech:
    """Ends a recording after speech followed by `silence` seconds of quiet.

    The threshold adapts to the room: the first 300 ms set the noise floor, and
    speech is anything clearly above it."""

    def __init__(self, silence: float):
        self.silence = silence
        self.reset()

    def reset(self):
        self.t0 = None
        self.floor = []
        self.spoke = False
        self.quiet_since = None
        self.fired = False

    def feed(self, level: float, now: float) -> bool:
        if self.silence <= 0 or self.fired:
            return False
        if self.t0 is None:
            self.t0 = now
        if now - self.t0 < .3:
            self.floor.append(level)
            return False
        floor = sum(self.floor) / len(self.floor) if self.floor else 0.0
        threshold = max(.012, floor * 3)
        if level > threshold:
            self.spoke = True
            self.quiet_since = None
            return False
        if not self.spoke:
            return False
        if self.quiet_since is None:
            self.quiet_since = now
        self.fired = now - self.quiet_since >= self.silence
        return self.fired


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


class Daemon:
    def __init__(self, config: Config):
        self.config = config
        self.state = "dormant"
        self.ambient = {"type": "ambient", "heat": None, "unread": 0, "tasks_running": 0, "recent_task": False}
        self.turn_id: str | None = None
        self.generation = 0            # bumps on every new turn or cancel; stale work checks it
        self.chip_ids = itertools.count(1)
        self.last_chip: str | None = None
        self.speak_task: asyncio.Task | None = None
        self.linger_task: asyncio.Task | None = None
        self.renderer_proc: asyncio.subprocess.Process | None = None
        self._last_mic = 0.0
        self._vad = EndOfSpeech(config.end_silence_seconds)

        self.bus = RendererBus(config.renderer_socket, self._snapshot)
        self.link = Link(config, self.on_api_message)
        self.recorder = Recorder(on_level=self._mic_level, max_seconds=config.max_record_seconds)
        self.player = Player()
        self.stt = SpeechToText(config.whisper_model, config.whisper_device, config.whisper_compute)
        self.tts = TextToSpeech(config.piper_voice)
        self.actions = Actions(config, show_card=self._show_card)

    # ── renderer ────────────────────────────────────────────────────────────

    def _snapshot(self):
        return [self.ambient, {"type": "state", "name": self.state}]

    def emit(self, **ev):
        self.bus.emit(ev)

    def set_state(self, name: str):
        if name == self.state:
            return
        logger.debug(f"[state] {self.state} -> {name}")
        self.state = name
        self.emit(type="state", name=name)

    def _mic_level(self, level: float):
        now = time.monotonic()
        if self._vad.feed(level, now) and self.recorder.active:
            logger.info("[mic] end of speech detected")
            asyncio.get_running_loop().create_task(self.listen_stop())
        if now - self._last_mic > .045:
            self._last_mic = now
            self.emit(type="mic_level", rms=round(level, 4))

    async def _show_card(self, card: dict):
        self.emit(type="card", **card)

    def _chip(self, text: str):
        if self.last_chip:
            self.emit(type="chip", id=self.last_chip, done=True)
        self.last_chip = f"c{next(self.chip_ids)}"
        self.emit(type="chip", id=self.last_chip, text=text, done=False)

    def _finish_chips(self):
        if self.last_chip:
            self.emit(type="chip", id=self.last_chip, done=True)
            self.last_chip = None

    def _clear(self):
        self._finish_chips()
        self.emit(type="clear")

    async def _spawn_renderer(self):
        if not self.config.renderer_cmd or os.getenv("DESKTOP_NO_RENDERER"):
            return
        while True:
            try:
                self.renderer_proc = await asyncio.create_subprocess_exec(
                    *self.config.renderer_cmd, "--socket", str(self.config.renderer_socket))
                code = await self.renderer_proc.wait()
                logger.warning(f"[renderer] exited with {code}; restarting in 3s")
            except FileNotFoundError as e:
                logger.error(f"[renderer] can't start: {e}")
                return
            await asyncio.sleep(3)

    # ── control (hotkeys) ───────────────────────────────────────────────────

    async def on_control(self, command: str, arg: str = "") -> str:
        if command == "listen-start":
            await self.listen_start()
        elif command == "listen-stop":
            asyncio.create_task(self.listen_stop())
        elif command == "toggle":
            if self.recorder.active:
                asyncio.create_task(self.listen_stop())
            else:
                await self.listen_start()
        elif command == "cancel":
            await self.cancel()
        elif command == "say":
            if not arg.strip():
                return "nothing to say"
            await self.interrupt()
            await self.submit(arg.strip())
        elif command == "status":
            return json.dumps({"state": self.state, "api": self.link.connected.is_set(), "ambient": self.ambient})
        else:
            return f"unknown command {command!r}"
        return "ok"

    async def interrupt(self):
        """Stop whatever is happening (speech, pending turn) without leaving the call."""
        self.generation += 1
        if self.turn_id:
            await self.link.send({"type": "cancel", "turn_id": self.turn_id})
            self.turn_id = None
        for task in (self.speak_task, self.linger_task):
            if task and not task.done():
                task.cancel()
        await self.player.stop()
        self._clear()

    async def listen_start(self):
        if self.recorder.active:
            return
        await self.interrupt()
        self._vad.reset()
        await self.recorder.start()
        self.set_state("listening")
        self.emit(type="transcript", text="", listening=True)

    async def listen_stop(self):
        if not self.recorder.active:
            return
        gen = self.generation
        try:
            pcm = await self.recorder.stop()
        except Exception:
            logger.exception("[mic] stopping the recording failed")
            await self.fail("The microphone failed.")
            return
        # He's working from the moment you stop talking, not once the transcript is back.
        self.set_state("thinking")
        self.emit(type="transcript", text="…", listening=True)
        self._chip("Transcribing")
        try:
            text = await self.stt.transcribe(pcm)
        except Exception:
            logger.exception("[stt] transcription failed")
            text = None
        if gen != self.generation:
            return
        if text is None:
            await self.fail("Speech recognition failed.")
            return
        if not text or text.lower().strip(" .!?") in ("", "you", "thank you"):
            # Whisper's usual hallucinations on near-silence count as nothing heard.
            self._clear()
            await self.say_lines(["I didn't catch that."], gen)
            return
        await self.submit(text)

    async def submit(self, text: str):
        gen = self.generation
        self._finish_chips()
        self.emit(type="transcript", text=text.lower().rstrip("."), listening=False)
        if not self.link.connected.is_set():
            await self.fail("I can't reach the Icarus API.")
            return
        self.turn_id = uuid.uuid4().hex[:8]
        self.set_state("thinking")
        if not await self.link.send({"type": "utterance", "turn_id": self.turn_id, "text": text}):
            if gen == self.generation:
                await self.fail("Lost the link to the Icarus API.")

    async def cancel(self):
        if self.recorder.active:
            await self.recorder.stop()
        await self.interrupt()
        self.set_state("dormant")

    # ── icarus-api messages ─────────────────────────────────────────────────

    async def on_api_message(self, msg: dict):
        kind = msg.get("type")
        if kind == "ambient":
            self.ambient = {k: msg.get(k) for k in ("type", "heat", "unread", "tasks_running", "recent_task")}
            self.bus.emit(self.ambient)
        elif kind == "action":
            asyncio.create_task(self.run_action(msg))
        elif msg.get("turn_id") and msg.get("turn_id") != self.turn_id:
            return  # a superseded turn
        elif kind == "status":
            self._chip(str(msg.get("text", "")))
        elif kind == "reply":
            self.turn_id = None
            self._finish_chips()
            self.speak_task = asyncio.create_task(self.say_lines(sentences(str(msg.get("text", ""))), self.generation))
        elif kind == "error":
            self.turn_id = None
            await self.fail(str(msg.get("message") or "Something went wrong."))

    async def run_action(self, msg: dict):
        action_id, name, args = msg.get("id"), str(msg.get("name", "")), msg.get("args") or {}
        prev = self.state
        self.set_state("acting")
        self.emit(type="card", **self.actions.describe(name, args))
        try:
            result = await asyncio.wait_for(self.actions.run(name, args), self.config.action_timeout)
            ok = True
        except ActionError as e:
            ok, result = False, str(e)
        except asyncio.TimeoutError:
            ok, result = False, f"{name} took too long"
        except Exception as e:
            logger.exception(f"[action] {name} crashed")
            ok, result = False, f"{name} failed unexpectedly: {e}"
        logger.info(f"[action] {name}({args}) -> {'ok' if ok else 'refused'}: {result}")
        await self.link.send({"type": "action_result", "id": action_id, "ok": ok, "result": result})
        if name != "show_on_screen":
            card = self.actions.describe(name, args)
            card["meta"] = str(result)[:140]
            self.emit(type="card", **card)
        if self.state == "acting":
            self.set_state(prev if prev in ("thinking", "speaking") else "thinking")

    # ── speaking ────────────────────────────────────────────────────────────

    async def say_lines(self, lines: list[str], gen: int):
        try:
            if not lines:
                lines = ["Done."]
            self.emit(type="transcript_clear")
            self.set_state("speaking")
            next_audio = asyncio.create_task(self.tts.synthesize(lines[0]))
            for i, line in enumerate(lines):
                pcm, rate = await next_audio
                if i + 1 < len(lines):
                    next_audio = asyncio.create_task(self.tts.synthesize(lines[i + 1]))
                if gen != self.generation:
                    return
                self.emit(type="subtitle", text=line, duration_ms=int(len(pcm) / 2 / rate * 1000))
                await self.player.play(pcm, rate, on_level=lambda lv: self.emit(type="speech_level", rms=round(lv, 4)))
                if gen != self.generation:
                    return
                await asyncio.sleep(.25)
            await self.linger(gen)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("[tts] speaking failed")
            await self.fail("My voice failed.", speak=False)

    async def linger(self, gen: int):
        self.set_state("linger")
        self.linger_task = asyncio.current_task()
        try:
            await asyncio.sleep(self.config.linger_seconds)
        except asyncio.CancelledError:
            return
        if gen == self.generation and self.state == "linger":
            self._clear()
            self.set_state("dormant")

    async def fail(self, message: str, speak: bool = True):
        gen = self.generation
        logger.warning(f"[turn] {message}")
        self._finish_chips()
        self.set_state("error")
        self.emit(type="subtitle", text=message, duration_ms=1500)
        if speak:
            try:
                pcm, rate = await self.tts.synthesize(message)
                await self.player.play(pcm, rate)
            except Exception:
                pass
        if gen == self.generation:
            await self.linger(gen)

    # ── main ────────────────────────────────────────────────────────────────

    async def serve_control(self):
        path = self.config.ctl_socket
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            path.unlink()

        async def handle(reader, writer):
            try:
                line = (await asyncio.wait_for(reader.readline(), 2)).decode().strip()
                command, _, arg = line.partition(" ")
                reply = await self.on_control(command, arg)
            except Exception as e:
                reply = f"error: {e}"
            writer.write((reply + "\n").encode())
            await writer.drain()
            writer.close()

        server = await asyncio.start_unix_server(handle, path=str(path))
        os.chmod(path, 0o600)
        return server

    async def run(self):
        if not self.config.token:
            logger.error("DESKTOP_TOKEN is not set (in the repo .env); icarus-api will refuse the connection")
        await self.bus.start()
        await self.serve_control()
        tasks = [
            asyncio.create_task(self.link.run()),
            asyncio.create_task(self._spawn_renderer()),
            asyncio.create_task(self.stt.warm_up()),
            asyncio.create_task(self.tts.warm_up()),
        ]
        logger.info(f"[daemon] ready; control socket {self.config.ctl_socket}")
        try:
            await asyncio.gather(*tasks)
        finally:
            if self.renderer_proc and self.renderer_proc.returncode is None:
                self.renderer_proc.terminate()
