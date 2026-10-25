"""Voice delivery: chunking, pauses, pace, trimming, the machine room and the continuous stream."""

import asyncio
import unittest
from unittest import mock

import numpy as np

from icarus_desktop import audio as audio_mod
from icarus_desktop.config import Config
from icarus_desktop.daemon import Daemon
from icarus_desktop.voice import MachineRoom, gap_before, line_offsets, pace, trim_silence

from test_daemon import FakeLink, FakePlayer, FakeTTS

RATE = 24000


def tone(seconds: float, amp: float = .3, hz: float = 220) -> np.ndarray:
    t = np.arange(int(seconds * RATE)) / RATE
    return (np.sin(2 * np.pi * hz * t) * amp).astype(np.float32)


class PaceAndPauseTests(unittest.TestCase):
    def test_short_confirmations_are_brisker_than_explanations(self):
        self.assertGreater(pace("Done."), pace("Here's how the scheduler decides which worker picks up the job next."))
        self.assertEqual(pace("x" * 200), 1.0)

    def test_pause_follows_how_the_last_line_ended(self):
        self.assertEqual(gap_before(None, "Hi."), 0.0)
        question = gap_before("Want a reminder?", "Okay.")
        comma = gap_before("First,", "Okay.")
        self.assertGreater(question, comma)
        self.assertGreater(gap_before("Done.", "Next.", new_beat=True), gap_before("Done.", "Next."))
        self.assertEqual(gap_before("Done.", "Next."), gap_before("Done.", "Next."))  # deterministic

    def test_trim_cuts_engine_padding_but_keeps_the_voice(self):
        padded = np.concatenate([np.zeros(RATE // 10), tone(.5), np.zeros(int(RATE * .7))]).astype(np.float32)
        out = trim_silence(padded, RATE)
        self.assertLess(out.size / RATE, .62)
        self.assertGreater(out.size / RATE, .5)
        self.assertLess(abs(out[0]), 1e-3)          # faded edges, no click
        self.assertEqual(trim_silence(np.zeros(100, np.float32), RATE).size, 0)

    def test_line_offsets_snap_to_the_pause_between_sentences(self):
        speech = np.concatenate([tone(1.0), np.zeros(int(RATE * .2)), tone(1.0)]).astype(np.float32)
        offsets = line_offsets(["First sentence here.", "Second one too, ok."], speech, RATE)
        self.assertEqual(offsets[0], 0.0)
        self.assertTrue(1.0 <= offsets[1] <= 1.2, offsets)


class MachineRoomTests(unittest.TestCase):
    def test_room_keeps_loudness_and_is_stereo(self):
        x = tone(1.0, amp=.05, hz=1000)              # below the compressor's threshold
        out = MachineRoom(RATE).render(x)
        self.assertEqual(out.shape[0], 2)
        rms_in = np.sqrt((x ** 2).mean())
        rms_out = np.sqrt((out[:, RATE // 4: RATE * 3 // 4] ** 2).mean())
        self.assertAlmostEqual(rms_out / rms_in, 10 ** (2 / 20), delta=.25)   # ~+2 dB make-up only
        self.assertFalse(np.allclose(out[0], out[1]))    # different reflections per ear

    def test_room_rings_into_pauses_then_goes_quiet(self):
        room = MachineRoom(RATE)
        room.floor_on()
        x = np.concatenate([tone(.2), np.zeros(MachineRoom.BLOCK * 2, np.float32)])
        x = x[: x.size // MachineRoom.BLOCK * MachineRoom.BLOCK]
        blocks = [room.process(x[i:i + MachineRoom.BLOCK]) for i in range(0, x.size, MachineRoom.BLOCK)]
        self.assertGreater(np.abs(blocks[-2]).max(), 1e-3)   # not digital silence after the voice
        room.floor_off()
        for _ in range(int(RATE * .6 / MachineRoom.BLOCK) + 1):
            room.process(np.zeros(MachineRoom.BLOCK, np.float32))
        self.assertTrue(room.quiet)

    def test_amount_zero_is_the_dry_voice(self):
        x = tone(.3)
        out = MachineRoom(RATE, 0).render(x)
        np.testing.assert_allclose(out[0, :x.size], x, atol=1e-6)
        np.testing.assert_allclose(out[0], out[1])
        self.assertEqual(MachineRoom(RATE, 0).latency, 0.0)


class ChunkingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.daemon = Daemon(Config(linger_seconds=.05, followup_seconds=0, token="t"))
        self.daemon.link, self.daemon.tts, self.daemon.player = FakeLink(), FakeTTS(), FakePlayer()
        self.events = []
        self.daemon.bus.emit = self.events.append

    async def test_waiting_lines_are_spoken_as_one_chunk_with_per_line_subtitles(self):
        lines = ["Two things.", "A recruiter call at three.", "The token expires tonight.", "Want a reminder?"]
        await self.daemon.say_lines(lines, self.daemon.generation)
        self.assertEqual(self.daemon.tts.texts, [" ".join(lines)])
        self.assertEqual([e["text"] for e in self.events if e["type"] == "subtitle"], lines)
        self.assertFalse(self.daemon.player.is_open)   # stream closed after the reply

    async def test_first_chunk_stays_short_so_he_starts_quickly(self):
        lines = [f"This is sentence number {i} of a longer explanation." for i in range(8)]
        await self.daemon.say_lines(lines, self.daemon.generation)
        texts = self.daemon.tts.texts
        self.assertGreater(len(texts), 1)
        self.assertLessEqual(len(texts[0]), 120)
        self.assertEqual(" ".join(texts), " ".join(lines))
        self.assertEqual(len(self.daemon.player.gaps), len(texts))

    async def test_streamed_sentences_flow_through_one_stream(self):
        q = self.daemon._speech_queue()
        q.put_nowait("Looking that up.")
        await asyncio.sleep(.05)
        q.put_nowait("Found it.")
        q.put_nowait(None)
        self.daemon.speech_q = None
        await self.daemon.speak_task
        self.assertEqual(self.daemon.player.played, 2)
        self.assertEqual(self.daemon.player.gaps[0], .1)        # breath of room as the stream opens
        self.assertGreater(self.daemon.player.gaps[1], .2)       # a sentence pause, not the old fixed sleep


class StreamPlayerTests(unittest.IsolatedAsyncioTestCase):
    """The real Player against a stand-in for pw-play that just swallows the audio."""

    async def asyncSetUp(self):
        real = asyncio.create_subprocess_exec

        async def fake_exec(*args, **kwargs):
            self.args = args
            return await real("cat", stdin=kwargs["stdin"], stdout=asyncio.subprocess.DEVNULL)

        patcher = mock.patch.object(audio_mod.asyncio, "create_subprocess_exec", fake_exec)
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def pcm(seconds: float) -> bytes:
        return (tone(seconds) * 32767).astype(np.int16).tobytes()

    async def test_lines_play_on_one_stereo_stream_with_marks_in_order(self):
        player = audio_mod.Player()
        fired, levels = [], []
        await player.begin(RATE, on_level=levels.append)
        self.assertIn("2", self.args[self.args.index("--channels") + 1])
        player.gap(.05)
        ok1 = await player.say(self.pcm(.15), [(0, lambda: fired.append("a")), (.1, lambda: fired.append("b"))])
        self.assertTrue(player.is_open)
        player.gap(.1)
        ok2 = await player.say(self.pcm(.1), [(0, lambda: fired.append("c"))])
        await player.end()
        self.assertTrue(ok1 and ok2)
        self.assertEqual(fired, ["a", "b", "c"])
        self.assertFalse(player.is_open)
        self.assertTrue(any(lv > 0 for lv in levels))
        self.assertEqual(levels[-1], 0.0)

    async def test_stop_cuts_a_line_short(self):
        player = audio_mod.Player()
        await player.begin(RATE)
        line = asyncio.create_task(player.say(self.pcm(2.0)))
        await asyncio.sleep(.1)
        await player.stop()
        self.assertFalse(await line)
        self.assertFalse(player.is_open)

    async def test_play_one_off_opens_and_closes(self):
        player = audio_mod.Player(room=0)
        self.assertTrue(await player.play(self.pcm(.1), RATE))
        self.assertFalse(player.is_open)


if __name__ == "__main__":
    unittest.main()
