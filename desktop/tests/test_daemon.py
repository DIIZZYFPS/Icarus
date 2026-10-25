"""Desktop daemon tests. Run from the repo root with the daemon venv:

    PYTHONPATH=desktop desktop/.venv/bin/python -m unittest discover -s desktop/tests
"""

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from icarus_desktop import actions as actions_mod
from icarus_desktop.actions import ActionError, Actions, AppEntry, match_app, scan_apps
from icarus_desktop.config import Config
from icarus_desktop.daemon import Daemon
from icarus_desktop.speech import sentences, speakable


def _entry(path: Path, body: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[Desktop Entry]\n" + body, encoding="utf-8")


class AppMatchingTests(unittest.TestCase):
    APPS = [
        AppEntry("firefox.desktop", "Firefox", "Web Browser", "internet;web;"),
        AppEntry("com.spotify.Client.desktop", "Spotify", "Music Player", "music;"),
        AppEntry("discord.desktop", "Discord", "", ""),
        AppEntry("org.gnome.Nautilus.desktop", "Files", "File Manager", "folder;"),
        AppEntry("steam.desktop", "Steam", "", "games;"),
    ]

    def test_exact_name_and_desktop_id_stem(self):
        self.assertEqual(match_app("firefox", self.APPS).name, "Firefox")
        self.assertEqual(match_app("Spotify", self.APPS).name, "Spotify")
        self.assertEqual(match_app("nautilus", self.APPS).name, "Files")

    def test_generic_name_and_typos(self):
        self.assertEqual(match_app("web browser", self.APPS).name, "Firefox")
        self.assertEqual(match_app("discrod", self.APPS).name, "Discord")

    def test_no_match(self):
        self.assertIsNone(match_app("photoshop", self.APPS))
        self.assertIsNone(match_app("", self.APPS))

    def test_scan_skips_hidden_and_respects_priority(self):
        with tempfile.TemporaryDirectory() as d:
            user, system = Path(d, "user"), Path(d, "system")
            _entry(system / "firefox.desktop", "Type=Application\nName=Firefox\nExec=firefox %u\n")
            _entry(system / "hidden.desktop", "Type=Application\nName=Hidden\nExec=x\nNoDisplay=true\n")
            _entry(system / "link.desktop", "Type=Link\nName=Link\nURL=https://x\n")
            _entry(system / "steam.desktop", "Type=Application\nName=Steam\nExec=steam\n")
            _entry(user / "steam.desktop", "Type=Application\nName=Steam\nExec=steam\nHidden=true\n")
            names = sorted(a.name for a in scan_apps([user, system]))
        self.assertEqual(names, ["Firefox"])


class ActionPolicyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = Config(app_denylist=("steam",))
        self.cards = []

        async def show(card):
            self.cards.append(card)

        self.actions = Actions(self.config, show_card=show)
        self.actions._apps = AppMatchingTests.APPS
        self.calls = []

        async def fake_run(*cmd, timeout=8.0):
            self.calls.append(cmd)
            if cmd[:2] == ("hyprctl", "dispatch"):
                return 0, "ok"
            if cmd == ("playerctl", "-l"):
                return 0, "spotify"
            if cmd[:2] == ("playerctl", "metadata"):
                return 0, "Playing|Perturbator|Future Club|spotify"
            if cmd[0] == "playerctl":
                return 0, ""
            return 1, "unexpected"

        for patcher in (mock.patch.object(actions_mod, "run", fake_run),
                        mock.patch.object(actions_mod, "scan_apps", lambda dirs=None: AppMatchingTests.APPS)):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_open_app_launches_through_gtk_launch_only(self):
        self.assertEqual(await self.actions.run("open_app", {"name": "firefox"}), "Opened Firefox")
        self.assertEqual(self.calls, [("hyprctl", "dispatch", "exec", "gtk-launch firefox.desktop")])

    async def test_unknown_app_and_denylist_are_refused(self):
        with self.assertRaisesRegex(ActionError, "no installed app"):
            await self.actions.run("open_app", {"name": "rm -rf /"})
        with self.assertRaisesRegex(ActionError, "deny list"):
            await self.actions.run("open_app", {"name": "steam"})
        self.assertEqual(self.calls, [])

    async def test_unknown_action_is_refused(self):
        with self.assertRaisesRegex(ActionError, "unknown action"):
            await self.actions.run("run_command", {"command": "ls"})

    async def test_media_control_maps_to_playerctl_and_reports_state(self):
        result = await self.actions.run("media_control", {"action": "next"})
        self.assertEqual(self.calls[0], ("playerctl", "next"))
        self.assertIn("Future Club by Perturbator", result)
        with self.assertRaises(ActionError):
            await self.actions.run("media_control", {"action": "shutdown"})

    async def test_show_on_screen_sends_a_card(self):
        await self.actions.run("show_on_screen", {"title": "Link", "body": "https://example.com"})
        self.assertEqual(self.cards, [{"kind": "info", "title": "Link", "body": "https://example.com"}])

    async def test_spotify_without_keys_opens_a_search(self):
        result = await self.actions.run("spotify_play", {"query": "perturbator", "kind": "artist"})
        self.assertIn(("playerctl", "-p", "spotify", "open", "spotify:search:perturbator"), self.calls)
        self.assertIn("keys aren't set", result)


class EndOfSpeechTests(unittest.TestCase):
    def test_ends_after_speech_then_silence_once(self):
        from icarus_desktop.daemon import EndOfSpeech
        vad = EndOfSpeech(1.0)
        t = 0.0
        fired = []
        for level in [.005] * 8 + [.08] * 20 + [.006] * 30:   # noise, speech, quiet (50 ms frames)
            fired.append(vad.feed(level, t))
            t += .05
        self.assertEqual(fired.count(True), 1)
        self.assertGreaterEqual(fired.index(True), 8 + 20 + 19)

    def test_silence_alone_never_ends(self):
        from icarus_desktop.daemon import EndOfSpeech
        vad = EndOfSpeech(1.0)
        self.assertFalse(any(vad.feed(.005, i * .05) for i in range(100)))


class VoiceFxTests(unittest.TestCase):
    def test_zero_is_untouched_and_full_stays_bounded(self):
        import numpy as np
        from icarus_desktop.speech import construct_fx
        tone = (np.sin(np.arange(22050) / 22050 * 2 * np.pi * 220) * .5).astype(np.float32)
        self.assertIs(construct_fx(tone, 22050, 0), tone)
        out = construct_fx(tone, 22050, 1)
        self.assertEqual(out.shape, tone.shape)
        self.assertLessEqual(float(np.abs(out).max()), .95)
        self.assertGreater(float(np.abs(out - tone).mean()), .01)


class SpeechTextTests(unittest.TestCase):
    def test_speakable_strips_markdown_and_links(self):
        self.assertEqual(speakable("**Done.** See https://x.io/a for `code`."), "Done. See the link for code.")

    def test_sentences_split_and_merge_fragments(self):
        self.assertEqual(
            sentences("Two things. A recruiter call at three, and the token expires tonight. Want a reminder?"),
            ["Two things. A recruiter call at three, and the token expires tonight.", "Want a reminder?"],
        )


class FakeLink:
    def __init__(self):
        self.sent = []
        self.connected = asyncio.Event()
        self.connected.set()

    async def send(self, message):
        self.sent.append(message)
        return True


class FakeTTS:
    async def synthesize(self, text):
        return b"\x00\x00" * 2205, 22050  # 0.1 s


class FakePlayer:
    def __init__(self):
        self.played = 0

    async def play(self, pcm, rate, on_level=None):
        self.played += 1
        return True

    async def stop(self):
        pass


class DaemonFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.daemon = Daemon(Config(linger_seconds=.05, followup_seconds=0, token="t"))
        self.daemon.link = FakeLink()
        self.daemon.tts = FakeTTS()
        self.daemon.player = FakePlayer()
        self.events = []
        self.daemon.bus.emit = self.events.append

    def states(self):
        return [e["name"] for e in self.events if e["type"] == "state"]

    async def test_typed_turn_runs_through_every_state(self):
        await self.daemon.on_control("say", "what's on today")
        utterance = self.daemon.link.sent[-1]
        self.assertEqual((utterance["type"], utterance["text"]), ("utterance", "what's on today"))
        await self.daemon.on_api_message({"type": "status", "turn_id": utterance["turn_id"], "text": "Checking your calendar"})
        await self.daemon.on_api_message({"type": "reply", "turn_id": utterance["turn_id"], "text": "Two things today. Both after lunch."})
        await self.daemon.speak_task
        self.assertEqual(self.states(), ["thinking", "speaking", "linger", "dormant"])
        self.assertEqual([e["text"] for e in self.events if e["type"] == "subtitle"], ["Two things today. Both after lunch."])
        self.assertTrue(any(e["type"] == "chip" and e["text"] == "Checking your calendar" for e in self.events))

    async def test_replies_for_a_superseded_turn_are_ignored(self):
        await self.daemon.on_control("say", "first")
        first = self.daemon.link.sent[-1]["turn_id"]
        await self.daemon.on_control("say", "second")
        self.assertIn({"type": "cancel", "turn_id": first}, self.daemon.link.sent)
        await self.daemon.on_api_message({"type": "reply", "turn_id": first, "text": "stale"})
        self.assertIsNone(self.daemon.speak_task)

    async def test_action_result_goes_back_to_the_api(self):
        async def fake_run(name, args):
            return "Opened Firefox"
        self.daemon.actions.run = fake_run
        await self.daemon.run_action({"type": "action", "id": "a1", "name": "open_app", "args": {"name": "firefox"}})
        self.assertIn({"type": "action_result", "id": "a1", "ok": True, "result": "Opened Firefox"}, self.daemon.link.sent)
        self.assertIn("acting", self.states())

    async def test_refused_action_reports_the_reason(self):
        async def refuse(name, args):
            raise ActionError("no installed app matches 'x'")
        self.daemon.actions.run = refuse
        await self.daemon.run_action({"type": "action", "id": "a2", "name": "open_app", "args": {"name": "x"}})
        self.assertIn({"type": "action_result", "id": "a2", "ok": False, "result": "no installed app matches 'x'"}, self.daemon.link.sent)

    async def test_offline_api_fails_cleanly(self):
        self.daemon.link.connected.clear()
        await self.daemon.on_control("say", "hello")
        self.assertEqual(self.states(), ["error", "linger", "dormant"])
        self.assertFalse(any(m["type"] == "utterance" for m in self.daemon.link.sent))

    async def test_hotkey_hold_transcribes_and_sends(self):
        class FakeRecorder:
            active = False
            async def start(self): self.active = True
            async def stop(self): self.active = False; return b"\x01\x00" * 16000
        class FakeSTT:
            async def transcribe(self, pcm): return "Open Firefox."
        self.daemon.recorder, self.daemon.stt = FakeRecorder(), FakeSTT()
        await self.daemon.on_control("listen-start")
        self.assertEqual(self.daemon.state, "listening")
        await self.daemon.listen_stop()
        sent = self.daemon.link.sent[-1]
        self.assertEqual((sent["type"], sent["text"]), ("utterance", "Open Firefox."))
        self.assertEqual(self.states(), ["listening", "thinking"])
        self.assertIn({"type": "transcript", "text": "open firefox", "listening": False}, self.events)

    async def test_silence_is_not_sent(self):
        class FakeRecorder:
            active = True
            async def stop(self): self.active = False; return b"\x00\x00" * 8000
        class FakeSTT:
            async def transcribe(self, pcm): return "you"
        self.daemon.recorder, self.daemon.stt = FakeRecorder(), FakeSTT()
        await self.daemon.listen_stop()
        self.assertFalse(any(m["type"] == "utterance" for m in self.daemon.link.sent))
        self.assertIn({"type": "subtitle", "text": "I didn't catch that.", "duration_ms": 100}, self.events)

    async def test_cancel_returns_to_dormant(self):
        await self.daemon.on_control("say", "long question")
        await self.daemon.on_control("cancel")
        self.assertEqual(self.daemon.state, "dormant")
        self.assertEqual(self.daemon.link.sent[-1]["type"], "cancel")


class RestartTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.daemon = Daemon(Config(linger_seconds=.05, followup_seconds=0, token="t"))
        self.daemon.link = FakeLink()
        self.daemon.bus.emit = lambda ev: None
        self.ran = []

        async def fake_run(*cmd, timeout=8.0):
            self.ran.append(cmd)
            return 0, ""
        patcher = mock.patch("icarus_desktop.actions.run", fake_run)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def restart(self, parts):
        await self.daemon.run_action({"type": "action", "id": "r1", "name": "restart_icarus", "args": {"parts": parts}})
        return self.daemon.link.sent[-1]

    async def test_waits_for_the_conversation_then_restarts_itself_last(self):
        self.daemon.state = "speaking"
        reply = await self.restart(["desktop", "api", "councilor"])
        self.assertTrue(reply["ok"])
        self.assertIn("once this conversation ends", reply["result"])
        self.assertFalse(self.daemon.restart_ready())          # still talking
        self.daemon.state = "dormant"
        self.assertTrue(self.daemon.restart_ready())
        await self.daemon.do_restart()
        self.assertEqual([c[-1] for c in self.ran], ["icarus-api", "icarus-councilor.service", "icarus-desktop.service"])
        self.assertFalse(self.daemon.pending_restart)

    async def test_councilor_waits_for_running_claude_tasks(self):
        self.daemon.on_task_event({"task_id": "sub-1", "text": "Editing app.py"})
        reply = await self.restart(["councilor"])
        self.assertIn("running tasks finish", reply["result"])
        self.assertFalse(self.daemon.restart_ready())
        self.daemon.on_task_event({"task_id": "sub-1", "text": "Claude finished", "done": True})
        self.assertTrue(self.daemon.restart_ready())

    async def test_desktop_alone_doesnt_wait_for_tasks(self):
        self.daemon.on_task_event({"task_id": "sub-1", "text": "Editing app.py"})
        await self.restart("desktop")
        self.assertTrue(self.daemon.restart_ready())

    async def test_unknown_parts_are_refused_and_cancel_clears(self):
        reply = await self.restart(["desktop", "rm -rf"])
        self.assertFalse(reply["ok"])
        self.assertFalse(self.daemon.pending_restart)
        await self.restart(["api"])
        reply = await self.restart(["cancel"])
        self.assertEqual(reply["result"], "Cancelled the pending restart.")
        self.assertFalse(self.daemon.restart_ready())


class FollowUpTests(unittest.IsolatedAsyncioTestCase):
    """After an answer the mic stays open briefly ('anything else?')."""

    def setUp(self):
        self.daemon = Daemon(Config(linger_seconds=.05, followup_seconds=.3, token="t"))
        self.daemon.link = FakeLink()
        self.daemon.tts = FakeTTS()
        self.daemon.player = FakePlayer()
        self.events = []
        self.daemon.bus.emit = self.events.append
        daemon = self.daemon

        class FakeRecorder:
            active = False
            starts = 0
            async def start(self):
                self.active = True
                self.starts += 1
            async def stop(self):
                self.active = False
                return b"\x01\x00" * 16000

        self.recorder = daemon.recorder = FakeRecorder()
        self.heard = "Tell me more."

        class FakeSTT:
            async def transcribe(_self, pcm):
                return self.heard

        daemon.stt = FakeSTT()

    def states(self):
        return [e["name"] for e in self.events if e["type"] == "state"]

    async def answer(self, text="Here's the idea."):
        await self.daemon.on_control("say", "brainstorm with me")
        turn = self.daemon.link.sent[-1]["turn_id"]
        await self.daemon.on_api_message({"type": "reply", "turn_id": turn, "text": text})
        for _ in range(50):
            await asyncio.sleep(.01)
            if self.recorder.active:
                return

    def utterances(self):
        return [m["text"] for m in self.daemon.link.sent if m["type"] == "utterance"]

    async def test_mic_opens_with_a_prompt_after_answering(self):
        await self.answer()
        self.assertTrue(self.recorder.active)
        self.assertEqual(self.states()[-1], "listening")
        prompt = [e for e in self.events if e["type"] == "transcript" and e.get("prompt")]
        self.assertEqual(prompt[-1]["prompt"], "Anything else?")
        self.daemon.speak_task.cancel()

    async def test_silence_dismisses(self):
        await self.answer()
        await self.daemon.speak_task
        self.assertFalse(self.recorder.active)
        self.assertEqual(self.states()[-2:], ["linger", "dormant"])
        self.assertEqual(self.utterances(), ["brainstorm with me"])

    async def test_speech_becomes_the_next_turn(self):
        await self.answer()
        self.daemon._vad.spoke = True          # you started talking
        await asyncio.sleep(.15)
        await self.daemon.listen_stop()         # end-of-speech fired
        self.assertEqual(self.utterances(), ["brainstorm with me", "Tell me more."])
        self.assertEqual(self.daemon.state, "thinking")

    async def test_dismissal_phrase_ends_the_call(self):
        self.heard = "No thanks, that's all."
        await self.answer()
        self.daemon._vad.spoke = True
        await self.daemon.listen_stop()
        self.assertEqual(self.utterances(), ["brainstorm with me"])
        self.assertEqual(self.daemon.state, "dormant")

    async def test_hotkey_takes_over_the_window(self):
        await self.answer()
        await self.daemon.on_control("listen-start")   # keeps the same recording
        self.assertEqual(self.recorder.starts, 1)
        await asyncio.sleep(.4)                        # past the window: must not be dismissed
        self.assertTrue(self.recorder.active)
        await self.daemon.listen_stop()
        self.assertEqual(self.utterances()[-1], "Tell me more.")


class PopUpTests(unittest.IsolatedAsyncioTestCase):
    """Notification pop-ups wait for a good moment, answer like a call, and tag the reply."""

    def setUp(self):
        self.daemon = Daemon(Config(linger_seconds=.05, followup_seconds=0, token="t"))
        self.daemon.link = FakeLink()
        self.daemon.tts = FakeTTS()
        self.daemon.player = FakePlayer()
        self.events = []
        self.daemon.bus.emit = self.events.append

    def states(self):
        return [e["name"] for e in self.events if e["type"] == "state"]

    async def test_queued_popup_shows_in_the_unread_count(self):
        await self.daemon.on_api_message({"type": "ambient", "heat": .2, "unread": 1, "tasks_running": 0, "recent_task": False})
        await self.daemon.on_api_message({"type": "notify", "id": "desktop-1", "text": "Task done."})
        self.assertEqual(self.events[-1]["unread"], 2)

    async def test_delivery_connects_stamps_speaks_and_tags_the_reply(self):
        item = {"id": "desktop-7", "text": "The playlist research is done. Want the summary?",
                "stamp": {"word": "COMPLETE", "color": "cyan", "sub": "ICARUS // 1 UPDATE"}, "received": 0}
        with mock.patch("icarus_desktop.daemon.asyncio.sleep", new=mock.AsyncMock()):
            await self.daemon.deliver(item)
            await self.daemon.speak_task
        self.assertEqual(self.states()[:2], ["connecting", "speaking"])
        self.assertTrue(any(e["type"] == "stamp" and e["word"] == "COMPLETE" for e in self.events))
        self.assertEqual(self.daemon.reply_to, None)   # the call ended (no follow-up in this config)
        self.daemon.reply_to = "desktop-7"              # as if answered inside the follow-up window
        await self.daemon.submit("yes, read it")
        self.assertEqual(self.daemon.link.sent[-1]["reply_to"], "desktop-7")

    async def test_busy_or_fullscreen_holds_the_popup(self):
        self.daemon.state = "speaking"
        self.assertEqual(await self.daemon.suppressed(), "busy")
        self.daemon.state = "dormant"

        async def fake_run(*cmd, timeout=8.0):
            if cmd[:2] == ("hyprctl", "activewindow"):
                return 0, json.dumps({"class": "steam_app_1", "fullscreen": 2})
            return 0, ""
        with mock.patch("icarus_desktop.actions.run", fake_run):
            self.assertEqual(await self.daemon.suppressed(), "fullscreen")

    async def test_quiet_hours_wrap_midnight(self):
        self.daemon.config.quiet_hours = (23, 8)
        with mock.patch("icarus_desktop.daemon.time.localtime", return_value=time.struct_time((2026, 1, 1, 2, 0, 0, 0, 1, 0))):
            self.assertEqual(await self.daemon.suppressed(), "quiet hours")

    async def test_fresh_call_is_not_a_reply_to_an_old_popup(self):
        class FakeRecorder:
            active = False
            async def start(self): self.active = True
            async def stop(self): self.active = False; return b""
        self.daemon.recorder = FakeRecorder()
        self.daemon.reply_to = "desktop-old"
        await self.daemon.on_control("listen-start")
        self.assertIsNone(self.daemon.reply_to)


class LookTests(unittest.IsolatedAsyncioTestCase):
    """SUPER+SHIFT+I: the focused window goes with the question, and its follow-ups."""

    def setUp(self):
        self.daemon = Daemon(Config(linger_seconds=.05, followup_seconds=0, token="t"))
        self.daemon.link = FakeLink()
        self.events = []
        self.daemon.bus.emit = self.events.append

        class FakeRecorder:
            active = False
            async def start(self): self.active = True
            async def stop(self): self.active = False; return b"\x01\x00" * 16000

        class FakeSTT:
            async def transcribe(self, pcm): return "What's this error?"

        self.daemon.recorder, self.daemon.stt = FakeRecorder(), FakeSTT()

    async def capture(self):
        return {"data_uri": "data:image/jpeg;base64,AAAA", "rect": [10, 20, 800, 600], "label": "kitty"}

    async def test_screenshot_rides_with_the_question_and_follow_ups(self):
        with mock.patch("icarus_desktop.actions.capture_focused", self.capture):
            await self.daemon.on_control("look-start")
        self.assertIn({"type": "scan", "rect": [10, 20, 800, 600]}, self.events)
        self.assertTrue(any(e["type"] == "chip" and e.get("text") == "Looking at kitty" for e in self.events))
        await self.daemon.listen_stop()
        first = self.daemon.link.sent[-1]
        self.assertEqual((first["text"], first["images"]), ("What's this error?", ["data:image/jpeg;base64,AAAA"]))
        await self.daemon.submit("and the second one?")          # follow-up in the same call
        self.assertEqual(self.daemon.link.sent[-1]["images"], ["data:image/jpeg;base64,AAAA"])

    async def test_a_new_call_does_not_reuse_an_old_screenshot(self):
        with mock.patch("icarus_desktop.actions.capture_focused", self.capture):
            await self.daemon.on_control("look-start")
        await self.daemon.on_control("cancel")
        self.daemon.state = "dormant"
        await self.daemon.on_control("listen-start")
        await self.daemon.listen_stop()
        self.assertNotIn("images", self.daemon.link.sent[-1])

    async def test_failed_capture_still_listens(self):
        async def broken():
            return None
        with mock.patch("icarus_desktop.actions.capture_focused", broken):
            await self.daemon.on_control("look-start")
        self.assertEqual(self.daemon.state, "listening")
        self.assertTrue(any(e["type"] == "chip" and "Couldn't capture" in e.get("text", "") for e in self.events))


class BoardTests(unittest.IsolatedAsyncioTestCase):
    """'Show me': the board appears on the tool call, then each beat is spoken with its lines lit."""

    BOARD = {"title": "2D DP", "code": "dp = []\nfor i in rows:\n    fill(i)", "language": "python",
             "beats": [{"say": "Here's the table. It starts empty.", "lines": [1], "note": "the table"},
                       {"say": "Then we fill it row by row.", "lines": [2, 3], "note": ""}]}

    def setUp(self):
        self.daemon = Daemon(Config(linger_seconds=.05, followup_seconds=0, token="t"))
        self.daemon.link = FakeLink()
        self.daemon.tts = FakeTTS()
        self.daemon.player = FakePlayer()
        self.events = []
        self.daemon.bus.emit = self.events.append
        self.broadcast = []
        self.daemon.board_server.broadcast = self.broadcast.append

    async def test_board_shows_then_beats_are_narrated_in_order(self):
        await self.daemon.run_action({"type": "action", "id": "a1", "name": "show_visual", "args": self.BOARD})
        self.assertEqual(self.daemon.link.sent[-1]["ok"], True)
        shown = [e for e in self.events if e["type"] == "board"]
        self.assertEqual(shown[0]["title"], "2D DP")
        self.daemon.turn_id = "t1"
        await self.daemon.on_api_message({"type": "reply", "turn_id": "t1", "text": "That's the whole idea."})
        await self.daemon.speak_task
        order = [(e["type"], e.get("index", e.get("text"))) for e in self.events if e["type"] in ("board_focus", "subtitle")]
        self.assertEqual(order, [("board_focus", 0), ("subtitle", "Here's the table. It starts empty."),
                                 ("board_focus", 1), ("subtitle", "Then we fill it row by row."),
                                 ("subtitle", "That's the whole idea.")])
        self.assertEqual([f["lines"] for f in self.broadcast], [[1], [2, 3]])   # the browser page follows too
        self.assertIn({"type": "board_clear"}, self.events)                     # gone when he leaves

    async def test_keep_pops_out_and_survives_him_leaving(self):
        opened = []
        with mock.patch("icarus_desktop.daemon.write_page", lambda board, port: f"/tmp/{board['id']}.html"), \
             mock.patch("icarus_desktop.daemon.open_page", opened.append):
            await self.daemon.run_action({"type": "action", "id": "a1", "name": "show_visual", "args": self.BOARD})
            await self.daemon.run_action({"type": "action", "id": "a2", "name": "keep_visual", "args": {}})
        self.assertEqual(len(opened), 1)
        self.assertEqual(self.daemon.link.sent[-1]["ok"], True)
        self.events.clear()
        await self.daemon.on_control("cancel")
        self.assertNotIn({"type": "board_clear"}, self.events)   # already off the overlay; the page stays

    async def test_keep_with_nothing_on_screen(self):
        await self.daemon.run_action({"type": "action", "id": "a3", "name": "keep_visual", "args": {}})
        self.assertEqual(self.daemon.link.sent[-1]["ok"], False)


class LayoutAndTableTests(unittest.IsolatedAsyncioTestCase):
    def test_layout_puts_dependencies_left_to_right(self):
        from icarus_desktop.layout import layered_layout
        pos = layered_layout([{"id": i} for i in "abcd"],
                             [{"from": "a", "to": "b"}, {"from": "b", "to": "c"}, {"from": "a", "to": "d"}, {"from": "c", "to": "a"}])
        self.assertEqual(set(pos), set("abcd"))
        self.assertLess(pos["b"][0], pos["c"][0])          # a cycle doesn't break it
        self.assertTrue(all(0 < x < 1 and 0 < y < 1 for x, y in pos.values()))

    async def test_table_beats_carry_their_fills(self):
        d = Daemon(Config(linger_seconds=.05, followup_seconds=0, token="t"))
        d.link, d.tts, d.player = FakeLink(), FakeTTS(), FakePlayer()
        events = []
        d.bus.emit = events.append
        d.board_server.broadcast = lambda e: None
        await d.run_action({"type": "action", "id": "a", "name": "show_visual", "args": {
            "kind": "table", "title": "DP", "rows": 1, "cols": 2, "cells": [["0", ""]],
            "beats": [{"say": "Fill it.", "cells": [[0, 1]], "fill": [[0, 1, "3"]], "arrows": [[0, 0, 0, 1]]}]}})
        board = next(e for e in events if e["type"] == "board")
        self.assertEqual((board["kind"], board["cells"]), ("table", [["0", ""]]))
        d.turn_id = "t"
        await d.on_api_message({"type": "reply", "turn_id": "t", "text": "Done."})
        await d.speak_task
        focus = next(e for e in events if e["type"] == "board_focus")
        self.assertEqual((focus["fill"], focus["arrows"]), ([[0, 1, "3"]], [[0, 0, 0, 1]]))


class BoardPageTests(unittest.IsolatedAsyncioTestCase):
    async def test_page_server_pushes_focus_to_open_pages(self):
        import socket
        from websockets.asyncio.client import connect
        from icarus_desktop.board import BoardServer, render_page
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        server = BoardServer(port)
        await server.start()
        try:
            async with connect(f"ws://127.0.0.1:{port}") as ws:
                await asyncio.sleep(.05)
                server.broadcast({"type": "board_focus", "id": "b1", "index": 2, "lines": [4]})
                self.assertEqual(json.loads(await asyncio.wait_for(ws.recv(), 2))["index"], 2)
        finally:
            server.server.close()
        page = render_page({"id": "b1", "title": "<DP>", "code": "x = 1\nif x < 2:", "language": "python",
                            "beats": [{"say": "first", "lines": [1, 2], "note": "n"}]}, port)
        self.assertIn("&lt;DP&gt;", page)                      # escaped
        self.assertIn('data-lines="1,2"', page)
        self.assertIn(f"ws://127.0.0.1:{port}", page)


class ConfirmTests(unittest.IsolatedAsyncioTestCase):
    """Edits ask first: amber AUTHORIZE, a spoken question, and a yes/no."""

    def setUp(self):
        self.daemon = Daemon(Config(linger_seconds=.05, followup_seconds=0, token="t"))
        self.daemon.link = FakeLink()
        self.daemon.tts = FakeTTS()
        self.daemon.player = FakePlayer()
        self.events = []
        self.daemon.bus.emit = self.events.append
        test = self

        class FakeRecorder:
            active = False
            async def start(self): self.active = True
            async def stop(self): self.active = False; return b"\x01\x00" * 16000

        class FakeSTT:
            async def transcribe(self, pcm): return test.heard

        self.daemon.recorder, self.daemon.stt = FakeRecorder(), FakeSTT()
        self.heard = "Yes, do it."

    async def ask(self, then):
        task = asyncio.create_task(self.daemon.run_action(
            {"type": "action", "id": "c1", "name": "confirm", "args": {"title": "Edit icarus with Claude?", "body": "Fix the test"}}))
        for _ in range(50):
            await asyncio.sleep(.01)
            if self.daemon.recorder.active:
                break
        await then()
        await task
        return self.daemon.link.sent[-1]

    async def test_spoken_yes_confirms(self):
        result = await self.ask(self.daemon.listen_stop)
        self.assertEqual(result, {"type": "action_result", "id": "c1", "ok": True, "result": "confirmed"})
        self.assertTrue(any(e["type"] == "stamp" and e["word"] == "AUTHORIZE" for e in self.events))
        self.assertIn("confirm", [e["name"] for e in self.events if e["type"] == "state"])

    async def test_unclear_answer_is_a_no(self):
        self.heard = "Hmm, maybe later."
        self.assertFalse((await self.ask(self.daemon.listen_stop))["ok"])

    async def test_no_is_a_no(self):
        self.heard = "No, don't."
        self.assertFalse((await self.ask(self.daemon.listen_stop))["ok"])

    async def test_hotkey_confirms_and_release_is_not_sent_as_a_turn(self):
        async def press_and_release():
            await self.daemon.on_control("listen-start")
            await self.daemon.listen_stop()
        result = await self.ask(press_and_release)
        self.assertTrue(result["ok"])
        self.assertFalse(any(m["type"] == "utterance" for m in self.daemon.link.sent))

    async def test_escape_declines(self):
        async def cancel():
            await self.daemon.on_control("cancel")
        sent = await self.ask(cancel)
        self.assertEqual(sent["result"], "declined")

    async def test_claude_progress_shows_as_chips_while_visible(self):
        self.daemon.state = "thinking"
        await self.daemon.on_api_message({"type": "task_event", "task_id": "sub-1", "text": "Editing app.py"})
        self.assertIn({"type": "chip", "id": "task-sub-1", "text": "Editing app.py", "done": False}, self.events)


class TaskBadgeTests(unittest.IsolatedAsyncioTestCase):
    """Running Claude tasks reach the renderer even in orb mode, for the badge beside the orb."""

    def setUp(self):
        self.daemon = Daemon(Config(token="t"))
        self.events = []
        self.daemon.bus.emit = self.events.append

    def badge(self):
        return [e for e in self.events if e["type"] == "tasks"][-1]["items"]

    async def event(self, text, **extra):
        await self.daemon.on_api_message({"type": "task_event", "task_id": "sub-1", "text": text, **extra})

    async def test_badge_shows_title_progress_and_steps_while_dormant(self):
        meta = {"title": "fix the flaky test", "project": "icarus", "mode": "edit", "started": 1000.0}
        await self.event("Claude: editing icarus", **meta)
        await self.event("Reading engine.py", **meta)
        await self.event("Editing engine.py", **meta)
        [item] = self.badge()
        self.assertEqual((item["title"], item["project"], item["mode"]), ("fix the flaky test", "icarus", "edit"))
        self.assertEqual((item["text"], item["steps"], item["started"]), ("Editing engine.py", 2, 1000.0))
        self.assertNotIn("seen", item)
        self.assertFalse(any(e["type"] == "chip" for e in self.events))   # chips only while he's on screen

    async def test_done_removes_it(self):
        await self.event("Reading a.py")
        await self.event("Claude finished", done=True)
        self.assertEqual(self.badge(), [])

    async def test_snapshot_includes_running_tasks(self):
        await self.event("Reading a.py", title="t")
        self.assertEqual(self.daemon._snapshot()[-1]["items"][0]["title"], "t")

    async def test_quiet_task_cleared_when_registry_says_nothing_runs(self):
        await self.event("Reading a.py")
        idle = {"type": "ambient", "heat": .3, "unread": 0, "tasks_running": 0, "recent_task": True}
        await self.daemon.on_api_message(idle)
        self.assertEqual(len(self.daemon.task_progress), 1)   # fresh: one bad read mustn't blink it away
        self.daemon.task_progress["sub-1"]["seen"] -= 120
        await self.daemon.on_api_message(idle)
        self.assertEqual(self.badge(), [])


if __name__ == "__main__":
    unittest.main()


class StreamedReplyTests(unittest.IsolatedAsyncioTestCase):
    """Sentences arrive as `say` while the model is still generating; the reply ends the stream."""

    def setUp(self):
        self.daemon = Daemon(Config(linger_seconds=.05, followup_seconds=0, token="t"))
        self.daemon.link = FakeLink()
        self.daemon.tts = FakeTTS()
        self.daemon.player = FakePlayer()
        self.events = []
        self.daemon.bus.emit = self.events.append

    def subtitles(self):
        return [e["text"] for e in self.events if e["type"] == "subtitle"]

    async def _turn(self) -> str:
        await self.daemon.on_control("say", "play something")
        return self.daemon.link.sent[-1]["turn_id"]

    async def test_speaking_starts_before_the_reply(self):
        turn = await self._turn()
        await self.daemon.on_api_message({"type": "say", "turn_id": turn, "text": "Putting on some Daft Punk."})
        for _ in range(50):
            if self.subtitles():
                break
            await asyncio.sleep(.01)
        self.assertEqual(self.subtitles(), ["Putting on some Daft Punk."])  # no reply yet
        await self.daemon.on_api_message({"type": "say", "turn_id": turn, "text": "It's queued up now."})
        await self.daemon.on_api_message({"type": "reply", "turn_id": turn, "text": "Putting on some Daft Punk. It's queued up now.",
                                          "streamed": True})
        await self.daemon.speak_task
        self.assertEqual(self.subtitles(), ["Putting on some Daft Punk.", "It's queued up now."])  # nothing twice
        self.assertEqual(self.daemon.state, "dormant")
        self.assertIsNone(self.daemon.speech_q)

    async def test_unstreamed_tail_is_still_spoken(self):
        turn = await self._turn()
        await self.daemon.on_api_message({"type": "say", "turn_id": turn, "text": "Let me check that for you."})
        await self.daemon.on_api_message({"type": "reply", "turn_id": turn, "text": "Agent loop error on turn 1.", "streamed": False})
        await self.daemon.speak_task
        self.assertEqual(self.subtitles(), ["Let me check that for you.", "Agent loop error on turn 1."])

    async def test_say_for_a_superseded_turn_is_ignored(self):
        first = await self._turn()
        await self._turn()
        await self.daemon.on_api_message({"type": "say", "turn_id": first, "text": "Stale sentence here."})
        self.assertIsNone(self.daemon.speech_q)

    async def test_interrupt_drops_the_stream(self):
        turn = await self._turn()
        await self.daemon.on_api_message({"type": "say", "turn_id": turn, "text": "This will be cut off."})
        await self.daemon.interrupt()
        self.assertIsNone(self.daemon.speech_q)
        self.assertTrue(self.daemon.speech_idle.is_set())

    async def test_board_beats_come_before_the_closing_line(self):
        turn = await self._turn()
        self.daemon.board_action("show_visual", {"title": "Loop", "code": "for x in y:\n    pass",
                                                 "beats": [{"say": "First we loop over everything.", "lines": [1]}]})
        await self.daemon.on_api_message({"type": "say", "turn_id": turn, "text": "That's the whole trick."})
        await self.daemon.on_api_message({"type": "reply", "turn_id": turn, "text": "That's the whole trick.", "streamed": True})
        await self.daemon.speak_task
        self.assertEqual(self.subtitles(), ["First we loop over everything.", "That's the whole trick."])

    async def test_listening_tells_the_api(self):
        class FakeRecorder:
            active = False
            async def start(self):
                self.active = True
        self.daemon.recorder = FakeRecorder()
        await self.daemon.listen_start()
        self.assertEqual(self.daemon.link.sent[-1], {"type": "listening"})
