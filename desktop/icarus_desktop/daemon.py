"""
daemon.py — the Icarus desktop daemon: eyes, ears and hands for the avatar.

  hotkey ─ctl socket─▶ daemon ─pw-record─▶ faster-whisper ─/ws/desktop─▶ icarus-api (Icarus L1)
                         │◀───────── status / action / say / reply / ambient ──────┘
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
import re
import time
import uuid

from .actions import ActionError, Actions
from .board import BoardServer, open_page, write_page
from .audio import Player, Recorder
from .config import Config
from .speech import SpeechToText, TextToSpeech, sentences

logger = logging.getLogger("icarus_desktop")

# What restart_icarus may restart, in the order they go. The daemon is last: restarting it ends
# this process, and --no-block lets systemd carry the job out after we're gone.
RESTART_TARGETS = {
    "api": ("docker", "restart", "icarus-api"),
    "councilor": ("systemctl", "--user", "--no-block", "restart", "icarus-councilor.service"),
    "desktop": ("systemctl", "--user", "--no-block", "restart", "icarus-desktop.service"),
}

BUSY_STATES = {"thinking", "acting", "confirm", "speaking"}

# Said in a follow-up window, these end the conversation instead of starting a turn.
# The whole utterance must be dismissal phrases ("no thanks, that's all"); "no, play the
# next song" is a request and goes through.
_DISMISS_PHRASE = (r"(no|nope|nah|nothing( else)?|that'?s (all|it|everything)|that is all|that'?ll be all|"
                   r"we'?re (done|good)|i'?m (good|done)|never ?mind|thanks?( you)?|all good|(good)?bye|see you|"
                   r"icarus|dismissed)")
DISMISS = re.compile(rf"^\s*{_DISMISS_PHRASE}([\s,.!]+{_DISMISS_PHRASE})*[\s.!]*$", re.I)


YES = re.compile(r"^\W*(yes|yeah|yep|yup|sure|do it|go ahead|go for it|confirm(ed)?|approved?|ok(ay)?|affirmative|send it|ship it|let'?s go)\b", re.I)
NO = re.compile(r"^\W*(no|nope|nah|cancel|stop|don'?t|negative|abort|wait|hold on)\b", re.I)


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
        self.speech_q: asyncio.Queue | None = None    # lines of the reply being streamed in; None ends it
        self.speech_idle = asyncio.Event()            # set while nothing is being spoken
        self.speech_idle.set()
        self.linger_task: asyncio.Task | None = None
        self.in_followup = False
        self.reply_to: str | None = None     # pop-up id a follow-up reply answers
        self.announcements: list[dict] = []  # spoken pop-ups waiting for a good moment
        self._confirm: asyncio.Future | None = None   # pending yes/no for an edit request
        self.task_progress: dict[str, dict] = {}      # Claude task id -> {title, text, steps, started, ...}
        self.pending_restart: set[str] = set()        # RESTART_TARGETS keys, run once he's done talking
        self.pending_image: str | None = None         # screenshot to send with the next utterance
        self.call_image: str | None = None            # ...and re-sent with follow-ups in the same call
        self.board: dict | None = None                # code on screen ("show me")
        self.board_server = BoardServer(config.board_port)
        self.renderer_proc: asyncio.subprocess.Process | None = None
        self._last_mic = 0.0
        self._vad = EndOfSpeech(config.end_silence_seconds)

        self.bus = RendererBus(config.renderer_socket, self._snapshot)
        self.link = Link(config, self.on_api_message)
        self.recorder = Recorder(on_level=self._mic_level, max_seconds=config.max_record_seconds)
        self.player = Player()
        self.stt = SpeechToText(config.whisper_model, config.whisper_device, config.whisper_compute,
                                beam=config.whisper_beam, threads=config.whisper_threads, vocab=config.whisper_vocab)
        self.tts = TextToSpeech(config)
        self.actions = Actions(config, show_card=self._show_card)

    # ── renderer ────────────────────────────────────────────────────────────

    def _snapshot(self):
        return [self.ambient, {"type": "state", "name": self.state}, self._tasks_event()]

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

    def _emit_ambient(self):
        self.bus.emit({**self.ambient, "unread": int(self.ambient.get("unread") or 0) + len(self.announcements)})

    def _tasks_event(self) -> dict:
        """Running Claude tasks for the badge beside the orb, newest first."""
        items = sorted(self.task_progress.values(), key=lambda v: v["started"], reverse=True)
        return {"type": "tasks", "items": [{k: v for k, v in item.items() if k != "seen"} for item in items]}

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
        elif command == "look-start":
            await self.look_start()
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
        if self.in_followup and self.recorder.active:
            await self.recorder.stop()
        self.in_followup = False
        if self.turn_id:
            await self.link.send({"type": "cancel", "turn_id": self.turn_id})
            self.turn_id = None
        self.speech_q = None
        for task in (self.speak_task, self.linger_task):
            if task and not task.done():
                task.cancel()
        await self.player.stop()
        self.speech_idle.set()
        self._clear()

    async def listen_start(self):
        if self._confirm and not self._confirm.done():
            self._confirm.set_result(True)   # SUPER+I on the AUTHORIZE prompt confirms
            return
        if self.recorder.active:
            # Hotkey pressed during a follow-up window: keep that recording; the release sends it.
            self.in_followup = False
            return
        if self.state in ("dormant", "error"):
            self.reply_to = None  # a fresh call isn't a reply to an old pop-up
            self.call_image = None
        await self.interrupt()
        self._vad.reset()
        await self.recorder.start()
        self.set_state("listening")
        self.emit(type="transcript", text="", listening=True)
        await self.link.send({"type": "listening"})  # icarus-api holds background model work

    async def look_start(self):
        """SUPER+SHIFT+I: grab the focused window *before* he appears, then listen as usual.
        The screenshot goes with this question only and is never written to disk."""
        if self.recorder.active or (self._confirm and not self._confirm.done()):
            await self.listen_start()
            return
        from .actions import capture_focused
        shot = None
        try:
            shot = await capture_focused()
        except Exception:
            logger.exception("[look] capture failed")
        await self.listen_start()
        if shot:
            self.pending_image = self.call_image = shot["data_uri"]
            self.emit(type="scan", rect=shot["rect"])
            self._chip(f"Looking at {shot['label']}")
            logger.info(f"[look] captured {shot['label']} ({len(shot['data_uri']) // 1024} KB)")
        else:
            self._chip("Couldn't capture the screen")

    async def listen_stop(self):
        if not self.recorder.active:
            return
        if self._confirm is not None and self._confirm.done():
            return  # answered by hotkey; the key release mustn't send the confirmation audio as a turn
        if self._confirm and not self._confirm.done():
            pcm = await self.recorder.stop()
            text = ""
            try:
                text = await self.stt.transcribe(pcm)
            except Exception:
                logger.exception("[confirm] transcription failed")
            self.emit(type="transcript", text=text.lower().rstrip(".") or "…", listening=False)
            if not self._confirm.done():
                self._confirm.set_result(bool(YES.match(text)) and not NO.match(text))
            return
        gen = self.generation
        followup, self.in_followup = self.in_followup, False
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
        if followup and (not text or DISMISS.match(text.strip())):
            logger.info(f"[followup] dismissed by {text!r}")
            await self.dismiss(gen)
            return
        if not text or text.lower().strip(" .!?") in ("", "you", "thank you"):
            # Whisper's usual hallucinations on near-silence count as nothing heard.
            self._clear()
            await self.say_lines(["I didn't catch that."], gen, followup=False)
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
        message = {"type": "utterance", "turn_id": self.turn_id, "text": text}
        if self.reply_to:
            message["reply_to"] = self.reply_to
        image = self.pending_image or self.call_image
        if image:
            message["images"] = [image]
            self.pending_image = None
        if not await self.link.send(message):
            if gen == self.generation:
                await self.fail("Lost the link to the Icarus API.")

    async def cancel(self):
        if self._confirm and not self._confirm.done():
            self._confirm.set_result(False)
        if self.recorder.active:
            await self.recorder.stop()
        await self.interrupt()
        self.reply_to = None
        self.pending_image = self.call_image = None
        self._hide_board()
        self.set_state("dormant")

    # ── icarus-api messages ─────────────────────────────────────────────────

    async def on_api_message(self, msg: dict):
        kind = msg.get("type")
        if kind == "ambient":
            self.ambient = {k: msg.get(k) for k in ("type", "heat", "unread", "tasks_running", "recent_task")}
            self._emit_ambient()
            if self.task_progress and not self.ambient.get("tasks_running"):
                # The registry says nothing is running: a lost "done" event mustn't leave the badge up.
                # Only quiet entries go, so one failed registry read doesn't blink a live task away.
                stale = [k for k, v in self.task_progress.items() if time.monotonic() - v["seen"] > 60]
                for k in stale:
                    del self.task_progress[k]
                if stale:
                    self.bus.emit(self._tasks_event())
        elif kind == "task_event":
            self.on_task_event(msg)
        elif kind == "notify":
            self.announcements.append({**msg, "received": time.monotonic()})
            logger.info(f"[notify] pop-up {msg.get('id')} queued ({len(self.announcements)} waiting)")
            self._emit_ambient()
        elif kind == "action":
            asyncio.create_task(self.run_action(msg))
        elif msg.get("turn_id") and msg.get("turn_id") != self.turn_id:
            return  # a superseded turn
        elif kind == "status":
            self._chip(str(msg.get("text", "")))
        elif kind == "say":
            # One finished sentence while the model is still generating: start speaking now.
            queue = self._speech_queue()
            for item in self._board_beats() + sentences(str(msg.get("text", ""))):
                queue.put_nowait(item)
        elif kind == "reply":
            self.turn_id = None
            self._finish_chips()
            text = str(msg.get("text", ""))
            if self.speech_q is None:
                self.speak_task = asyncio.create_task(self.say_lines(self.narration(text), self.generation))
            else:
                # Already speaking the streamed reply: add anything not yet said, then end it.
                queue, self.speech_q = self.speech_q, None
                for item in (self._board_beats() if msg.get("streamed") else self.narration(text)):
                    queue.put_nowait(item)
                queue.put_nowait(None)
        elif kind == "error":
            self.turn_id = None
            await self.fail(str(msg.get("message") or "Something went wrong."))

    async def run_action(self, msg: dict):
        action_id, name, args = msg.get("id"), str(msg.get("name", "")), msg.get("args") or {}
        if name in ("show_visual", "keep_visual"):
            ok, result = self.board_action(name, args)
            await self.link.send({"type": "action_result", "id": action_id, "ok": ok, "result": result})
            return
        if name == "restart_icarus":
            ok, result = self.schedule_restart(args)
            logger.info(f"[restart] {args} -> {result}")
            await self.link.send({"type": "action_result", "id": action_id, "ok": ok, "result": result})
            return
        if name == "confirm":
            ok = await self.confirm(args)
            await self.link.send({"type": "action_result", "id": action_id, "ok": ok,
                                  "result": "confirmed" if ok else "declined"})
            return
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

    def _speech_queue(self) -> asyncio.Queue:
        """The queue for the reply being streamed in, starting its speaker on first use."""
        if self.speech_q is None:
            self.speech_q = asyncio.Queue()
            self.speak_task = asyncio.create_task(self.speak(self.speech_q, self.generation))
        return self.speech_q

    async def say_lines(self, lines: list, gen: int, followup: bool = True):
        queue: asyncio.Queue = asyncio.Queue()
        for line in lines or ["Done."]:
            queue.put_nowait(line)
        queue.put_nowait(None)
        await self.speak(queue, gen, followup)

    async def speak(self, queue: asyncio.Queue, gen: int, followup: bool = True):
        """Speak lines as they arrive on `queue` until None, then open the follow-up window.

        A line is text, or (text, focus event) for a board beat. Synthesis runs one
        line ahead of playback, so the next sentence is ready when this one ends."""
        audio: asyncio.Queue = asyncio.Queue(maxsize=2)

        async def synthesize():
            while True:
                item = await queue.get()
                if item is None:
                    await audio.put(None)
                    return
                text, focus = (item, None) if isinstance(item, str) else item
                pcm, rate = await self.tts.synthesize(text)
                await audio.put((text, focus, pcm, rate))

        producer = asyncio.create_task(synthesize())
        spoke = False
        try:
            while True:
                if audio.empty() and spoke and self.state == "speaking":
                    self.set_state("thinking")  # between sentences while a tool runs
                got = await audio.get()
                if got is None:
                    break
                line, focus, pcm, rate = got
                if gen != self.generation:
                    return
                if not spoke:
                    self.emit(type="transcript_clear")
                    spoke = True
                self.speech_idle.clear()
                self.set_state("speaking")
                if focus:
                    self.emit(**focus)
                    self.board_server.broadcast(focus)
                self.emit(type="subtitle", text=line, duration_ms=int(len(pcm) / 2 / rate * 1000))
                await self.player.play(pcm, rate, on_level=lambda lv: self.emit(type="speech_level", rms=round(lv, 4)))
                if gen != self.generation:
                    return
                await asyncio.sleep(.25)
                if audio.empty():
                    self.speech_idle.set()
            self.speech_idle.set()
            if not spoke:
                await self.linger(gen)
            elif followup and self.config.followup_seconds > 0:
                await self.follow_up(gen)
            else:
                await self.linger(gen)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("[tts] speaking failed")
            await self.fail("My voice failed.", speak=False)
        finally:
            producer.cancel()
            self.speech_idle.set()

    async def follow_up(self, gen: int):
        """'Anything else?': keep the mic open briefly so a conversation can continue hands-free.

        Speech in the window becomes the next turn (end-of-speech detection sends it);
        silence or a dismissal ends the call."""
        self._vad.reset()
        try:
            await self.recorder.start()
        except Exception:
            logger.exception("[followup] couldn't open the mic")
            await self.linger(gen)
            return
        self.in_followup = True
        await self.link.send({"type": "listening"})
        window = self.config.followup_seconds
        self.set_state("listening")
        self.emit(type="transcript", text="", listening=True, prompt="Anything else?", window_ms=int(window * 1000))
        deadline = time.monotonic() + window
        while time.monotonic() < deadline:
            await asyncio.sleep(.1)
            if gen != self.generation or not self.in_followup or not self.recorder.active:
                return  # interrupted, taken over by the hotkey, or already being transcribed
            if self._vad.spoke:
                break
        else:
            if gen == self.generation and self.in_followup:
                logger.info("[followup] nothing said; dismissing")
                self.in_followup = False
                await self.recorder.stop()
                await self.dismiss(gen)
            return
        # Speaking: end-of-speech detection finishes it; cap a recording that never goes quiet.
        cap = time.monotonic() + self.config.max_record_seconds
        while self.in_followup and self.recorder.active and gen == self.generation:
            if time.monotonic() > cap:
                await self.listen_stop()
                return
            await asyncio.sleep(.2)

    async def dismiss(self, gen: int):
        self._clear()
        await self.linger(gen, seconds=min(2.5, self.config.linger_seconds))

    async def linger(self, gen: int, seconds: float | None = None):
        self.set_state("linger")
        self.linger_task = asyncio.current_task()
        try:
            await asyncio.sleep(self.config.linger_seconds if seconds is None else seconds)
        except asyncio.CancelledError:
            return
        if gen == self.generation and self.state == "linger":
            self._clear()
            self._hide_board()
            self.reply_to = None
            self.pending_image = self.call_image = None
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

    async def confirm(self, args: dict) -> bool:
        """Ask before anything that changes files: amber AUTHORIZE, a spoken question, yes/no.
        SUPER+I confirms, SUPER+Esc declines, anything unclear or silence counts as no."""
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.speech_idle.wait(), 15)  # finish the sentence in progress first
        loop = asyncio.get_running_loop()
        self._confirm = loop.create_future()
        title = str(args.get("title") or "Go ahead?")
        self.set_state("confirm")
        self.emit(type="stamp", word="AUTHORIZE", sub="ICARUS // " + title.upper()[:40], color="amber", ms=1600, wings=1.2)
        self.emit(type="card", kind="confirm", title=title, body=str(args.get("body", "")), meta=str(args.get("meta", "")))
        try:
            try:
                pcm, rate = await self.tts.synthesize(title.replace("?", "") + ". Yes or no?")
                if not self._confirm.done():
                    self.emit(type="subtitle", text=title, duration_ms=int(len(pcm) / 2 / rate * 1000))
                    await self.player.play(pcm, rate)
            except Exception:
                logger.exception("[confirm] couldn't speak the question")
            if not self._confirm.done():
                self._vad.reset()
                await self.recorder.start()
                self.emit(type="transcript", text="", listening=True, prompt="Yes or no?", window_ms=30000)
            ok = await asyncio.wait_for(asyncio.shield(self._confirm), 30)
        except asyncio.TimeoutError:
            ok = False
        finally:
            if self.recorder.active:
                await self.recorder.stop()
            self._confirm = None
        logger.info(f"[confirm] {title} -> {'yes' if ok else 'no'}")
        self.emit(type="card", kind="confirm" if ok else "info", title="Confirmed" if ok else "Declined", body=title)
        self.emit(type="transcript_clear")
        if self.state == "confirm":
            self.set_state("thinking")
        return ok

    def board_action(self, name: str, args: dict) -> tuple[bool, str]:
        if name == "keep_visual":
            if not self.board:
                return False, "Nothing is on screen to keep."
            self.pop_out_board()
            return True, "Opened in his browser; it stays after I leave."
        content = {k: v for k, v in args.items() if k not in ("beats", "keep_open", "id", "type")}
        board = {**content, "kind": str(args.get("kind") or "code"), "title": str(args.get("title") or "Example"),
                 "id": "b" + uuid.uuid4().hex[:8], "code": str(args.get("code") or ""), "language": str(args.get("language") or ""),
                 "beats": [b for b in (args.get("beats") or []) if isinstance(b, dict)], "narrated": False, "popped": False}
        self._hide_board()
        self.board = board
        if args.get("keep_open"):
            self.pop_out_board()
            return True, "Opened in his browser."
        self.emit(type="board", **{k: v for k, v in board.items() if k not in ("beats", "narrated", "popped")})
        return True, "On screen next to the avatar."

    def pop_out_board(self):
        board = self.board
        if not board or board.get("popped"):
            return
        board["popped"] = True
        path = write_page(board, self.config.board_port)
        open_page(path)
        self.emit(type="board_clear")
        logger.info(f"[board] {board['id']} popped out to {path}")

    def _hide_board(self):
        if self.board and not self.board.get("popped"):
            self.emit(type="board_clear")
        self.board = None

    def narration(self, text: str) -> list:
        """The board's beats (each with its focus event), then the spoken reply."""
        return self._board_beats() + sentences(text)

    def _board_beats(self) -> list:
        """The current board's narration beats as (line, focus) items, once per board."""
        items: list = []
        board = self.board
        if board and board["beats"] and not board["narrated"]:
            board["narrated"] = True
            total = len(board["beats"])
            for i, beat in enumerate(board["beats"]):
                focus = {"type": "board_focus", "id": board["id"], "index": i, "total": total,
                         "note": beat.get("note") or "",
                         **{k: beat.get(k) or [] for k in ("lines", "cells", "fill", "arrows", "nodes", "edges")}}
                for j, line in enumerate(sentences(beat.get("say", ""))):
                    items.append((line, focus if j == 0 else None))
        return items

    def on_task_event(self, msg: dict):
        task_id, text = str(msg.get("task_id")), str(msg.get("text", ""))
        if msg.get("done"):
            self.task_progress.pop(task_id, None)
        else:
            entry = self.task_progress.setdefault(task_id, {"id": task_id, "steps": 0, "started": time.time()})
            if not text.startswith("Claude:"):   # the kickoff line isn't a tool call
                entry["steps"] += 1
            entry["text"], entry["seen"] = text, time.monotonic()
            for key in ("title", "project", "mode", "started"):
                if msg.get(key):
                    entry[key] = msg[key]
        self.bus.emit(self._tasks_event())
        if self.state != "dormant":
            self.emit(type="chip", id=f"task-{task_id}", text=text, done=bool(msg.get("done")))

    # ── restarting Icarus (to load changes) ─────────────────────────────────

    def _tasks_busy(self) -> bool:
        return bool(self.task_progress) or int(self.ambient.get("tasks_running") or 0) > 0

    def schedule_restart(self, args: dict) -> tuple[bool, str]:
        """Queue parts of Icarus to restart. Nothing restarts mid-conversation: restart_loop
        waits for dormant, and holds the Councilor while it's running tasks."""
        parts = args.get("parts")
        parts = [parts] if isinstance(parts, str) else list(parts or [])
        parts = [str(p).strip().lower() for p in parts if str(p).strip()]
        if not parts or parts == ["cancel"]:
            had, self.pending_restart = bool(self.pending_restart), set()
            return True, "Cancelled the pending restart." if had else "No restart was pending."
        unknown = [p for p in parts if p not in RESTART_TARGETS]
        if unknown:
            return False, f"can't restart {', '.join(unknown)}; choose from {', '.join(RESTART_TARGETS)}"
        self.pending_restart.update(parts)
        names = ", ".join(p for p in RESTART_TARGETS if p in self.pending_restart)
        wait = " and the running tasks finish" if "councilor" in self.pending_restart and self._tasks_busy() else ""
        return True, f"Restart of {names} queued; it happens once this conversation ends{wait}."

    def restart_ready(self) -> bool:
        if not self.pending_restart or self.state != "dormant" or self.recorder.active or self.announcements:
            return False
        if self._confirm is not None and not self._confirm.done():
            return False
        return not ("councilor" in self.pending_restart and self._tasks_busy())

    async def do_restart(self):
        from .actions import run
        parts = [p for p in RESTART_TARGETS if p in self.pending_restart]
        self.pending_restart = set()
        logger.info(f"[restart] restarting {', '.join(parts)}")
        for part in parts:
            try:
                code, out = await run(*RESTART_TARGETS[part], timeout=90)
            except Exception as e:
                code, out = -1, str(e)
            if code != 0:
                logger.warning(f"[restart] {part} failed ({code}): {out[:200]}")

    async def restart_loop(self):
        while True:
            await asyncio.sleep(2)
            if self.restart_ready():
                await self.do_restart()

    # ── speaking first (notification pop-ups) ───────────────────────────────

    async def suppressed(self) -> str | None:
        """Why now is a bad moment to speak up, or None if it's fine."""
        if self.state != "dormant" or self.recorder.active:
            return "busy"
        if self.config.quiet_hours:
            start, end = self.config.quiet_hours
            hour = time.localtime().tm_hour
            if (start <= hour < end) if start < end else (hour >= start or hour < end):
                return "quiet hours"
        try:
            from .actions import run
            code, out = await run("hyprctl", "activewindow", "-j", timeout=3)
            if code == 0 and out.strip().startswith("{") and json.loads(out).get("fullscreen") not in (0, False, None):
                return "fullscreen"
            code, out = await run("pactl", "list", "source-outputs", "short", timeout=3)
            if code == 0 and out.strip():
                return "mic in use"
        except Exception as e:
            logger.debug(f"[notify] suppression check failed: {e}")
        return None

    async def announce_loop(self):
        while True:
            await asyncio.sleep(2)
            if not self.announcements:
                continue
            fresh = [a for a in self.announcements if time.monotonic() - a["received"] < self.config.announce_max_age]
            if len(fresh) != len(self.announcements):
                self.announcements = fresh
                self._emit_ambient()
            if not fresh or await self.suppressed():
                continue
            await self.deliver(self.announcements.pop(0))

    async def deliver(self, item: dict):
        """Answer like an incoming holo call: connect, catch him mid-activity, turn, speak."""
        await self.interrupt()
        gen = self.generation
        self._emit_ambient()
        self.reply_to = item.get("id")
        self.set_state("connecting")
        stamp = item.get("stamp")
        await asyncio.sleep(1.2)
        if gen != self.generation:
            return
        if stamp:
            self.emit(type="stamp", word=stamp.get("word", ""), sub=stamp.get("sub", ""), color=stamp.get("color", "cyan"),
                      ms=1500, wings=stamp.get("wings", 0))
            await asyncio.sleep(.6)
        logger.info(f"[notify] delivering pop-up {item.get('id')}")
        self.speak_task = asyncio.create_task(self.say_lines(sentences(str(item.get("text", ""))), gen))

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
        await self.board_server.start()
        tasks = [
            asyncio.create_task(self.link.run()),
            asyncio.create_task(self._spawn_renderer()),
            asyncio.create_task(self.stt.warm_up()),
            asyncio.create_task(self.tts.warm_up()),
            asyncio.create_task(self.announce_loop()),
            asyncio.create_task(self.restart_loop()),
        ]
        logger.info(f"[daemon] ready; control socket {self.config.ctl_socket}")
        try:
            await asyncio.gather(*tasks)
        finally:
            if self.renderer_proc and self.renderer_proc.returncode is None:
                self.renderer_proc.terminate()
