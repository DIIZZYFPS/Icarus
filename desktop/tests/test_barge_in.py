"""Talking over him: the detector, the recorder's pre-roll, and the daemon cutting in.

    PYTHONPATH=desktop desktop/.venv/bin/python -m unittest discover -s desktop/tests
"""

import asyncio
import time
import unittest

from icarus_desktop import audio as audio_mod
from icarus_desktop.config import Config
from icarus_desktop.daemon import Daemon
from icarus_desktop.vad import BargeInDetector, EndOfSpeech

from test_daemon import FakeLink, FakePlayer, FakeTTS


def run_levels(detector, levels, step=.05, start=0.0):
    """Feed one level per `step` seconds; return the time it fired, or None."""
    for i, level in enumerate(levels):
        if detector.feed(level, start + i * step):
            return start + i * step
    return None


QUIET, LOUD = .004, .08


class BargeInDetectorTests(unittest.TestCase):
    def test_sustained_speech_fires_once_after_the_onset(self):
        det = BargeInDetector(onset=.3)
        fired = run_levels(det, [QUIET] * 6 + [LOUD] * 20)
        self.assertIsNotNone(fired)
        self.assertGreaterEqual(fired, .3 + .3)          # floor window, then the onset
        self.assertFalse(det.feed(LOUD, fired + .05))    # only once

    def test_a_cough_is_not_an_interruption(self):
        det = BargeInDetector(onset=.3)
        self.assertIsNone(run_levels(det, [QUIET] * 6 + [LOUD] * 3 + [QUIET] * 20))

    def test_a_dip_inside_a_word_keeps_counting(self):
        det = BargeInDetector(onset=.3)
        self.assertIsNotNone(run_levels(det, [QUIET] * 6 + [LOUD] * 3 + [QUIET] + [LOUD] * 6, step=.05))

    def test_a_noisy_room_raises_the_bar(self):
        det = BargeInDetector(onset=.3)
        self.assertIsNone(run_levels(det, [.03] * 6 + [.05] * 30))   # louder than before, but not above 4x the floor
        self.assertAlmostEqual(det.floor, .03)

    def test_silence_never_fires(self):
        self.assertIsNone(run_levels(BargeInDetector(), [QUIET] * 100))


class EndOfSpeechPresetTests(unittest.TestCase):
    def test_a_recording_that_started_mid_sentence_ends_on_silence(self):
        vad = EndOfSpeech(1.0)
        vad.preset(.005, 0.0)
        self.assertFalse(vad.feed(LOUD, .05))
        fired = run_levels(vad, [QUIET] * 40, start=.1)
        self.assertIsNotNone(fired)
        self.assertAlmostEqual(fired - .15, 1.0, delta=.1)


class FakeProc:
    def __init__(self, chunks):
        self.stdout = self
        self.chunks = list(chunks)

    async def read(self, n):
        return self.chunks.pop(0) if self.chunks else b""


class RecorderPrerollTests(unittest.IsolatedAsyncioTestCase):
    async def test_keeps_only_the_trailing_audio_until_kept(self):
        rec = audio_mod.Recorder(max_seconds=30)
        rec._preroll = 3200          # 0.1 s
        await rec._read(FakeProc([b"a" * 1600, b"b" * 1600, b"c" * 1600]))
        self.assertEqual(bytes(rec._buf), b"b" * 1600 + b"c" * 1600)
        rec.keep()
        await rec._read(FakeProc([b"d" * 1600]))
        self.assertEqual(bytes(rec._buf), b"b" * 1600 + b"c" * 1600 + b"d" * 1600)


class FakeRecorder:
    def __init__(self):
        self.active = False
        self.preroll = None
        self.kept = False
        self.starts = 0

    async def start(self, preroll=0.0):
        self.active, self.preroll = True, preroll
        self.starts += 1

    def keep(self):
        self.kept = True

    async def stop(self):
        self.active = False
        return b"\x01\x00" * 16000


class BlockingPlayer(FakePlayer):
    """say() holds until released, so a reply stays 'being spoken'."""

    def __init__(self):
        super().__init__()
        self.release = asyncio.Event()

    async def say(self, pcm, marks=()):
        for _, fire in marks:
            fire()
        await self.release.wait()
        return True


class BargeInDaemonTests(unittest.IsolatedAsyncioTestCase):
    def make(self, **config):
        d = Daemon(Config(linger_seconds=.05, followup_seconds=0, token="t", barge_in=True, **config))
        d.link, d.tts, d.player, d.recorder = FakeLink(), FakeTTS(), BlockingPlayer(), FakeRecorder()
        d._headphones = (time.monotonic(), True)
        self.events = []
        d.bus.emit = self.events.append
        return d

    async def start_speaking(self, d):
        d.turn_id = "t1"
        await d.on_api_message({"type": "say", "turn_id": "t1", "text": "Here is a long answer."})
        for _ in range(50):
            if d.recorder.active:
                break
            await asyncio.sleep(.01)

    async def asyncTearDown(self):
        pass

    async def test_listens_while_he_speaks_and_stops_when_he_finishes(self):
        d = self.make()
        await self.start_speaking(d)
        self.assertTrue(d.recorder.active)
        self.assertEqual(d.recorder.preroll, d.BARGE_IN_PREROLL)
        d.player.release.set()
        await d.on_api_message({"type": "reply", "turn_id": "t1", "text": "Here is a long answer.", "streamed": True})
        await d.speak_task
        self.assertFalse(d.recorder.active)
        self.assertIsNone(d._barge)

    async def test_talking_over_him_cuts_the_turn_and_keeps_the_mic(self):
        d = self.make()
        await self.start_speaking(d)
        self.assertEqual(d.state, "speaking")
        await d._barge_in()
        self.assertIn({"type": "cancel", "turn_id": "t1"}, d.link.sent)
        self.assertIn({"type": "listening"}, d.link.sent)
        self.assertEqual(d.state, "listening")
        self.assertTrue(d.recorder.active and d.recorder.kept)       # the open mic became the recording
        self.assertTrue(d._vad.spoke)                                # and end-of-speech knows you're mid-sentence
        self.assertIsNone(d._barge)
        await asyncio.wait_for(asyncio.shield(d.speak_task), 1)    # his speech was cut, not left running
        self.assertTrue(d.speak_task.done())

    async def test_the_level_callback_routes_to_the_detector(self):
        d = self.make()
        await self.start_speaking(d)

        class Fires:
            floor, peak = .004, .08
            def feed(self, level, now): return True
        d._barge = Fires()
        d._mic_level(.1)
        for _ in range(20):
            await asyncio.sleep(.01)
            if d.state == "listening":
                break
        self.assertEqual(d.state, "listening")
        self.assertIn({"type": "cancel", "turn_id": "t1"}, d.link.sent)

    async def test_off_by_default(self):
        d = self.make()
        d.config.barge_in = False
        d.turn_id = "t1"
        await d.on_api_message({"type": "say", "turn_id": "t1", "text": "Hello there."})
        await asyncio.sleep(.1)
        self.assertEqual(d.recorder.starts, 0)

    async def test_speakers_are_left_alone(self):
        d = self.make()
        d._headphones = (time.monotonic(), False)
        d.turn_id = "t1"
        await d.on_api_message({"type": "say", "turn_id": "t1", "text": "Hello there."})
        await asyncio.sleep(.1)
        self.assertEqual(d.recorder.starts, 0)

    async def test_the_hotkey_takes_over_from_the_listening_mic(self):
        d = self.make()
        await self.start_speaking(d)
        await d.on_control("listen-start")
        self.assertIsNone(d._barge)
        self.assertEqual(d.state, "listening")
        self.assertFalse(d.in_followup)
        self.assertTrue(d.recorder.active)          # a fresh recording, not the pre-roll buffer
        self.assertEqual(d.recorder.starts, 2)
        self.assertEqual(d.recorder.preroll, 0.0)

    async def test_a_confirmation_gets_the_mic_to_itself(self):
        d = self.make()
        await self.start_speaking(d)
        d.player.release.set()
        d.speech_idle.set()
        task = asyncio.create_task(d.confirm({"title": "Go ahead?"}))
        for _ in range(50):
            await asyncio.sleep(.01)
            if d._confirm is not None and d.recorder.starts == 2:
                break
        self.assertIsNone(d._barge)
        self.assertEqual(d.recorder.starts, 2)      # the confirmation opened its own recording
        self.assertEqual(d.recorder.preroll, 0.0)
        d._confirm.set_result(True)
        self.assertTrue(await task)

    async def test_headphone_detection(self):
        d = self.make(barge_in_sinks=("ew300",))
        d._headphones = (0.0, False)

        async def sink(name):
            async def fake_run(*cmd, timeout=8.0):
                return 0, name + "\n"
            from unittest import mock
            d._headphones = (0.0, False)
            with mock.patch("icarus_desktop.actions.run", fake_run):
                return await d._on_headphones()
        self.assertTrue(await sink("bluez_output.AA_BB.1"))
        self.assertTrue(await sink("alsa_output.usb-LE_XIAN_SIMGOT_EW300_DSP.pro-output-0"))
        self.assertFalse(await sink("alsa_output.pci-0000_01_00.1.hdmi-stereo"))


if __name__ == "__main__":
    unittest.main()
