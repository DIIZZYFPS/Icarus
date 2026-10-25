"""Desktop daemon tests. Run from the repo root with the daemon venv:

    PYTHONPATH=desktop desktop/.venv/bin/python -m unittest discover -s desktop/tests
"""

import asyncio
import tempfile
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
        self.daemon = Daemon(Config(linger_seconds=.05, token="t"))
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


if __name__ == "__main__":
    unittest.main()
