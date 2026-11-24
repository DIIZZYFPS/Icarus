"""
daemon.py — the Icarus desktop daemon: eyes, ears and hands for the avatar.

  hotkey ─ctl socket─▶ daemon ─pw-record─▶ faster-whisper ─/ws/desktop─▶ icarus-api (Icarus L1)
                         │◀───────── status / action / say / reply / ambient ──────┘
                         ├─ actions (hyprctl, playerctl, Spotify) ─▶ action_result
                         ├─ Kokoro/Piper ─MachineRoom─pw-play (one stream per reply)─▶ speakers
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

import numpy as np

from .actions import ActionError, Actions
from .board import Board
from .audio import Player, Recorder
from .config import Config
from .link import Link, RendererBus
from .restart import Restarter
from .speech import SpeechToText, TextToSpeech, sentences
from .vad import BargeInDetector, EndOfSpeech
from .voice import CHUNK_CHARS, FIRST_CHUNK_CHARS, gap_before, line_offsets, pace

logger = logging.getLogger("icarus_desktop")


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
        self.announced: dict[str, None] = {}   # ids of recent pop-ups (an ordered set), to ignore resends
        self._confirm: asyncio.Future | None = None   # pending yes/no for an edit request
        self.task_progress: dict[str, dict] = {}      # Claude task id -> {title, text, steps, started, done, ...}
        self.busy_items: list[dict] = []              # background jobs for the thought bubbles
        self.busy_at = 0.0                            # when icarus-api last sent the list
        self.pending_image: str | None = None         # screenshot to send with the next utterance
        self.call_image: str | None = None            # ...and re-sent with follow-ups in the same call
        self.board = Board(lambda ev: self.bus.emit(ev), config.board_port)   # code on screen ("show me")
        self.renderer_proc: asyncio.subprocess.Process | None = None
        self._last_mic = 0.0
        self._vad = EndOfSpeech(config.end_silence_seconds)
        self._barge: BargeInDetector | None = None    # set while the mic listens for being talked over
        self._headphones = (0.0, False)               # (checked at, result) for the default output

        self.bus = RendererBus(config.renderer_socket, self._snapshot)
        self.link = Link(config, self.on_api_message)
        self.recorder = Recorder(on_level=self._mic_level, max_seconds=config.max_record_seconds)
        self.player = Player(config.voice_room)
        self.stt = SpeechToText(config.whisper_model, config.whisper_device, config.whisper_compute,
                                beam=config.whisper_beam, threads=config.whisper_threads, vocab=config.whisper_vocab)
        self.tts = TextToSpeech(config)
        self.actions = Actions(config, show_card=self._show_card)
        self.restarter = Restarter(self._quiet, self._tasks_busy)

    # ── renderer ────────────────────────────────────────────────────────────

    def _snapshot(self):
        return [self.ambient, {"type": "state", "name": self.state}, self._tasks_event(),
                {"type": "bubbles", "items": self.busy_items}]

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
        if self._barge is not None:
            if self._barge.feed(level, now):
                asyncio.get_running_loop().create_task(self._barge_in())
            return
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
        running = [t for t in self.task_progress.values() if not t.get("done")]
        running.sort(key=lambda t: t.get("started") or 0, reverse=True)
        return {"type": "tasks", "items": [{k: t[k] for k in self.TASK_FIELDS if k in t} for t in running]}

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
        await self._barge_stop()
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
        await self._barge_stop()
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
        if self.config.barge_in:
            asyncio.create_task(self._on_headphones())   # warm, so the first spoken line doesn't wait on pactl
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
        self.board.hide()
        self.set_state("dormant")

    # ── icarus-api messages ─────────────────────────────────────────────────

    async def on_api_message(self, msg: dict):
        kind = msg.get("type")
        if kind == "ambient":
            self.ambient = {k: msg.get(k) for k in ("type", "heat", "unread", "tasks_running", "recent_task")}
            self._emit_ambient()
        elif kind == "busy":
            self.on_busy(msg)
        elif kind == "notify":
            if msg.get("id") in self.announced:
                return   # sent again after a reconnect; already queued or spoken
            self.announced[msg.get("id")] = None
            while len(self.announced) > 50:
                self.announced.pop(next(iter(self.announced)))
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
            for item in self.board.beats() + sentences(str(msg.get("text", ""))):
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
                for item in (self.board.beats() if msg.get("streamed") else self.narration(text)):
                    queue.put_nowait(item)
                queue.put_nowait(None)
        elif kind == "error":
            self.turn_id = None
            await self.fail(str(msg.get("message") or "Something went wrong."))

    async def run_action(self, msg: dict):
        action_id, name, args = msg.get("id"), str(msg.get("name", "")), msg.get("args") or {}
        if name in ("show_visual", "keep_visual"):
            ok, result = self.board.action(name, args)
            await self.link.send({"type": "action_result", "id": action_id, "ok": ok, "result": result})
            return
        if name == "restart_icarus":
            ok, result = self.restarter.schedule(args)
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

    async def say_lines(self, lines: list, gen: int, followup: bool = True, on_start=None):
        queue: asyncio.Queue = asyncio.Queue()
        for line in lines or ["Done."]:
            queue.put_nowait(line)
        queue.put_nowait(None)
        await self.speak(queue, gen, followup, on_start)

    async def speak(self, queue: asyncio.Queue, gen: int, followup: bool = True, on_start=None):
        """Speak lines as they arrive on `queue` until None, then open the follow-up window.

        A line is text, or (text, focus event) for a board beat. Lines that are already waiting
        are synthesized together as one chunk, so the voice carries its intonation across
        sentences instead of restarting every line. Synthesis runs one chunk ahead of playback,
        and everything plays on one continuous stream with pauses chosen from the text.
        `on_start` is called once, as the first line begins to play."""
        audio: asyncio.Queue = asyncio.Queue(maxsize=1)
        end = object()

        async def synthesize():
            nothing = object()
            held, prev, first = nothing, None, True
            while True:
                item = held if held is not nothing else await queue.get()
                held = nothing
                if item is None:
                    await audio.put(end)
                    return
                lines = [(item, None) if isinstance(item, str) else tuple(item)]
                budget = FIRST_CHUNK_CHARS if first else CHUNK_CHARS
                size = len(lines[0][0])
                while not queue.empty():
                    nxt = queue.get_nowait()
                    line = None if nxt is None else ((nxt, None) if isinstance(nxt, str) else tuple(nxt))
                    # A new board beat starts its own chunk (it gets a longer pause and its focus).
                    if line is None or line[1] is not None or size + len(line[0]) > budget:
                        held = nxt
                        break
                    lines.append(line)
                    size += len(line[0]) + 1
                text = " ".join(t for t, _ in lines)
                pcm, rate = await self.tts.synthesize(text, pace=pace(text))
                gap = gap_before(prev, text, new_beat=lines[0][1] is not None)
                await audio.put((lines, pcm, rate, gap))
                prev, first = text, False

        starting = [on_start]

        def mark(text: str, focus: dict | None, ms: int):
            def fire():
                if gen != self.generation:
                    return
                self.set_state("speaking")
                callback, starting[0] = starting[0], None
                if callback:
                    callback()
                if focus:
                    self.emit(**focus)
                    self.board.server.broadcast(focus)
                self.emit(type="subtitle", text=text, duration_ms=ms)
            return fire

        producer = asyncio.create_task(synthesize())
        getter: asyncio.Task | None = None
        spoke = False
        try:
            while True:
                if audio.empty() and spoke and self.state == "speaking":
                    self.set_state("thinking")  # between sentences while a tool runs
                getter = getter or asyncio.ensure_future(audio.get())
                if self.player.is_open:
                    # Nothing ready soon (a tool is running): let the room ring out and close.
                    await asyncio.wait({getter}, timeout=1.5)
                    if not getter.done():
                        await self.player.end()
                got = await getter
                getter = None
                if got is end:
                    break
                lines, pcm, rate, gap = got
                if gen != self.generation:
                    return
                if not spoke:
                    self.emit(type="transcript_clear")
                    spoke = True
                    await self._barge_start(gen)
                self.speech_idle.clear()
                fresh = not self.player.is_open
                await self.player.begin(rate, on_level=lambda lv: self.emit(type="speech_level", rms=round(lv, 4)))
                # A freshly opened stream gets a breath of room before he speaks.
                self.player.gap(.1 if fresh else gap)
                pcm_audio = np.frombuffer(pcm[: len(pcm) // 2 * 2], np.int16)
                offsets = line_offsets([t for t, _ in lines], pcm_audio, rate)
                total = len(pcm_audio) / rate
                bounds = offsets[1:] + [total]
                marks = [(off, mark(text, focus, int((b - off) * 1000)))
                         for (text, focus), off, b in zip(lines, offsets, bounds)]
                await self.player.say(pcm, marks)
                if gen != self.generation:
                    return
                if audio.empty():
                    self.speech_idle.set()
            await self.player.end()
            await self._barge_stop()
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
            if getter:
                getter.cancel()
            await self._barge_stop()
            self.speech_idle.set()

    # ── talking over him (barge-in) ─────────────────────────────────────────

    BARGE_IN_PREROLL = 1.0   # seconds of mic audio kept so the first word of the interruption isn't lost
    HEADPHONES = re.compile(r"headphone|headset|bluez_output", re.I)

    async def _on_headphones(self) -> bool:
        """Barge-in listens while he speaks, so it needs the mic not to hear him: headphones only."""
        from .actions import run
        checked, result = self._headphones
        if time.monotonic() - checked < 30:
            return result
        try:
            code, out = await run("pactl", "get-default-sink", timeout=3)
        except Exception:
            code, out = -1, ""
        sink = out.strip().lower()
        result = code == 0 and bool(sink) and (bool(self.HEADPHONES.search(sink))
                                               or any(part in sink for part in self.config.barge_in_sinks))
        self._headphones = (time.monotonic(), result)
        return result

    async def _barge_start(self, gen: int):
        if not self.config.barge_in or self._barge is not None or self.recorder.active:
            return
        if not await self._on_headphones() or gen != self.generation or self.recorder.active:
            return
        self._barge = BargeInDetector(self.config.barge_in_seconds)
        try:
            await self.recorder.start(preroll=self.BARGE_IN_PREROLL)
        except Exception:
            logger.exception("[barge-in] couldn't open the mic")
            self._barge = None

    async def _barge_stop(self):
        """Close the listening mic without using what it heard (no-op once it became a recording)."""
        if self._barge is None:
            return
        barge, self._barge = self._barge, None
        logger.info(f"[barge-in] no interruption: floor {barge.floor:.4f}, loudest {barge.peak:.4f}, "
                    f"needs {max(.012, barge.floor * 3):.4f}")
        await self.recorder.stop()

    async def _barge_in(self):
        """You started talking over him: stop him and the turn, and carry on with the mic you have open."""
        barge, self._barge = self._barge, None
        if barge is None:
            return
        logger.info(f"[barge-in] interrupted: floor {barge.floor:.4f}, loudest {barge.peak:.4f}")
        await self.interrupt()
        self.recorder.keep()
        self._vad.preset(barge.floor, time.monotonic())
        self.set_state("listening")
        self.emit(type="transcript", text="", listening=True)
        await self.link.send({"type": "listening"})

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
            self.board.hide()
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
        await self._barge_stop()   # the confirmation owns the mic now
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

    def narration(self, text: str) -> list:
        """The board's beats (each with its focus event), then the spoken reply."""
        return self.board.beats() + sentences(text)

    # ── background work: thought bubbles and Claude tasks ───────────────────
    # icarus-api sends the whole current picture (backend/agent/busy.py), so each message replaces
    # what we hold and nothing has to be patched together from events.

    BUSY_STALE_SECONDS = 15   # icarus-api resends every 5 s; silence this long means the list is dead

    TASK_FIELDS = ("id", "title", "project", "mode", "text", "steps", "started")

    def on_busy(self, msg: dict):
        items = [{"id": str(i.get("id")), "source": str(i.get("source")), "done": bool(i.get("done")),
                  "ok": bool(i.get("ok", True))} for i in (msg.get("items") or []) if isinstance(i, dict)]
        self.busy_at = time.monotonic()
        if items != self.busy_items:
            self.busy_items = items
            self.bus.emit({"type": "bubbles", "items": items})
        self.set_tasks([t for t in (msg.get("tasks") or []) if isinstance(t, dict) and t.get("id")])

    def set_tasks(self, tasks: list[dict]):
        new = {str(t["id"]): t for t in tasks}
        changed = {k: v for k, v in new.items()
                   if (v.get("text"), v.get("done")) != (self.task_progress.get(k, {}).get("text"), self.task_progress.get(k, {}).get("done"))}
        if not changed and new.keys() == self.task_progress.keys():
            return
        self.task_progress = new
        self.bus.emit(self._tasks_event())
        if self.state != "dormant":
            for task_id, task in changed.items():
                self.emit(type="chip", id=f"task-{task_id}", text=str(task.get("text", "")), done=bool(task.get("done")))

    def expire_busy(self):
        if (self.busy_items or self.task_progress) and (not self.link.connected.is_set()
                                or time.monotonic() - self.busy_at > self.BUSY_STALE_SECONDS):
            self.busy_items = []
            self.bus.emit({"type": "bubbles", "items": []})
            self.set_tasks([])

    async def busy_loop(self):
        while True:
            await asyncio.sleep(3)
            self.expire_busy()

    # ── restarting Icarus (to load changes) ─────────────────────────────────

    def _tasks_busy(self) -> bool:
        return any(not t.get("done") for t in self.task_progress.values()) or int(self.ambient.get("tasks_running") or 0) > 0

    def _quiet(self) -> bool:
        if self.state != "dormant" or self.recorder.active or self.announcements:
            return False
        return self._confirm is None or self._confirm.done()

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

    def _receipt(self, delivery_id):
        """Tell icarus-api this pop-up is being spoken, so it isn't sent again."""
        asyncio.get_running_loop().create_task(self.link.send({"type": "spoken", "id": delivery_id}))

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
        self.speak_task = asyncio.create_task(self.say_lines(sentences(str(item.get("text", ""))), gen,
                                                             on_start=lambda: self._receipt(item.get("id"))))

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
        await self.board.server.start()
        tasks = [
            asyncio.create_task(self.link.run()),
            asyncio.create_task(self._spawn_renderer()),
            asyncio.create_task(self.stt.warm_up()),
            asyncio.create_task(self.tts.warm_up()),
            asyncio.create_task(self.announce_loop()),
            asyncio.create_task(self.restarter.loop()),
            asyncio.create_task(self.busy_loop()),
        ]
        logger.info(f"[daemon] ready; control socket {self.config.ctl_socket}")
        try:
            await asyncio.gather(*tasks)
        finally:
            if self.renderer_proc and self.renderer_proc.returncode is None:
                self.renderer_proc.terminate()
