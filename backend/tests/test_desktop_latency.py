"""Desktop latency work: streamed replies, sentence chunking, thinking off for
spoken turns, and the foreground gate that keeps background model calls waiting."""

import asyncio
import json
import os
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.agent import engine, foreground
from backend.agent.local_llm import local_agent_loop
from backend.agent.tools import current_platform
from backend.routes import desktop
from backend.tests.fake_llm import ScriptedLlamaServer, text_response, tool_call_response

TOKEN = "test-token"


def get_time() -> str:
    """Return the time."""
    return "noon"


class StreamingLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_streamed_turn_reports_text_and_runs_tools(self):
        server = ScriptedLlamaServer([
            tool_call_response([("get_time", '{}')], content="Checking. "),
            text_response("It's noon. Lunch time."),
        ])
        seen = []

        async def on_text(delta, done):
            seen.append((delta, done))

        with server.patched():
            result = await local_agent_loop("time?", tools=[get_time], on_text=on_text)

        self.assertEqual(result, "It's noon. Lunch time.")
        self.assertTrue(all(r.get("stream") for r in server.requests))
        self.assertEqual(server.tool_messages(1), ["noon"])
        text = "".join(d for d, _ in seen)
        self.assertEqual(text, "Checking. It's noon. Lunch time.")
        self.assertNotIn("hmm", text)  # reasoning deltas are never passed on
        self.assertEqual(sum(1 for _, done in seen if done), 2)  # one end marker per model call

    async def test_tool_call_arguments_are_reassembled_from_pieces(self):
        calls = []

        def remember(entry: str, tags: list[str] | None = None) -> str:
            """Remember."""
            calls.append((entry, tags))
            return "ok"

        server = ScriptedLlamaServer([
            tool_call_response([("remember", json.dumps({"entry": "a long memory entry", "tags": ["x", "y"]}))]),
            text_response("Saved."),
        ])

        async def on_text(delta, done):
            pass

        with server.patched():
            await local_agent_loop("go", tools=[remember], on_text=on_text)
        self.assertEqual(calls, [("a long memory entry", ["x", "y"])])

    async def test_without_on_text_nothing_streams(self):
        server = ScriptedLlamaServer([text_response("hi")])
        with server.patched():
            self.assertEqual(await local_agent_loop("x", tools=[]), "hi")
        self.assertNotIn("stream", server.requests[0])


class ThinkingTests(unittest.IsolatedAsyncioTestCase):
    async def _thinking(self, platform: str, env: dict | None = None) -> bool:
        server = ScriptedLlamaServer([text_response("ok")])
        token = current_platform.set(platform)
        try:
            with server.patched(), mock.patch.dict(os.environ, env or {}):
                await engine.run_icarus("hello")
        finally:
            current_platform.reset(token)
        return server.requests[0]["chat_template_kwargs"]["enable_thinking"]

    async def test_desktop_turns_skip_thinking(self):
        self.assertFalse(await self._thinking("desktop"))

    async def test_desktop_thinking_can_be_turned_back_on(self):
        self.assertTrue(await self._thinking("desktop", {"DESKTOP_THINKING": "1"}))

    async def test_other_platforms_keep_thinking(self):
        self.assertTrue(await self._thinking("discord"))


class SentenceChunkerTests(unittest.TestCase):
    def _run(self, deltas):
        chunker = desktop.SentenceChunker()
        out = []
        for d in deltas:
            out += chunker.feed(d)
        return out, chunker.flush()

    def test_sentences_come_out_whole(self):
        out, rest = self._run(["Sure", ", opening Spo", "tify. It's", " playing now! And", " the"])
        self.assertEqual(out, ["Sure, opening Spotify.", "It's playing now!"])
        self.assertEqual(rest, ["And the"])

    def test_decimals_and_newlines(self):
        out, rest = self._run(["Version 3.5 is out.\n", "Second line"])
        self.assertEqual(out, ["Version 3.5 is out."])
        self.assertEqual(rest, ["Second line"])

    def test_closing_quote_stays_with_its_sentence(self):
        out, _ = self._run(['He said "go." Then ', "left."])
        self.assertEqual(out, ['He said "go."'])

    def test_reply_was_spoken(self):
        spoken = ["Checking.", "It's noon.", "Lunch time."]
        self.assertTrue(desktop.reply_was_spoken("It's noon.  Lunch time.", spoken))
        self.assertFalse(desktop.reply_was_spoken("Agent loop error on turn 1: x", spoken))
        self.assertFalse(desktop.reply_was_spoken("anything", []))
        self.assertTrue(desktop.reply_was_spoken("(Agent produced no output)", spoken))


async def _static_ambient():
    return {"type": "ambient", "heat": .3, "unread": 0, "tasks_running": 0, "recent_task": False}


class StreamedTurnSocketTests(unittest.TestCase):
    def setUp(self):
        self.holds = []

        async def fake_hold(seconds, reason="desktop"):
            self.holds.append(seconds)

        for p in (
            mock.patch.dict(os.environ, {"DESKTOP_TOKEN": TOKEN}),
            mock.patch.object(desktop, "ambient_snapshot", _static_ambient),
            mock.patch.object(desktop, "AMBIENT_INTERVAL_SECONDS", 60),
            mock.patch.object(foreground, "hold", fake_hold),
        ):
            p.start()
            self.addCleanup(p.stop)
        app = FastAPI()
        app.include_router(desktop.router)
        self.client = TestClient(app)

    def _turn(self, fake_process) -> list[dict]:
        with mock.patch.object(desktop, "process_message", fake_process):
            with self.client.websocket_connect("/ws/desktop") as ws:
                ws.send_json({"type": "hello", "token": TOKEN})
                ws.send_json({"type": "utterance", "turn_id": "t1", "text": "hi"})
                got = []
                while True:
                    m = ws.receive_json()
                    if m["type"] in ("say", "reply"):
                        got.append(m)
                    if m["type"] == "reply":
                        return got

    def test_sentences_go_out_as_say_before_the_reply(self):
        async def fake_process(platform, user_id, text, on_text=None, **kw):
            for d in ["Sure. Openi", "ng it now."]:
                await on_text(d, False)
            await on_text("", True)
            return "Sure. Opening it now."

        got = self._turn(fake_process)
        self.assertEqual([m["text"] for m in got if m["type"] == "say"], ["Sure.", "Opening it now."])
        self.assertEqual(got[-1], {"type": "reply", "turn_id": "t1", "text": "Sure. Opening it now.", "streamed": True})
        self.assertIn(30, self.holds)                                   # held while the turn ran
        self.assertEqual(self.holds[-1], desktop.FOREGROUND_GRACE_SECONDS)  # and a while after

    def test_unstreamed_reply_is_marked_for_speaking(self):
        async def fake_process(platform, user_id, text, on_text=None, **kw):
            return "Passed your answer to subagent sub-1."

        got = self._turn(fake_process)
        self.assertEqual(got, [{"type": "reply", "turn_id": "t1", "text": "Passed your answer to subagent sub-1.",
                                "streamed": False}])

    def test_no_output_placeholder_is_never_spoken(self):
        async def fake_process(platform, user_id, text, on_text=None, **kw):
            return "(Agent produced no output)"

        got = self._turn(fake_process)
        self.assertEqual(got[-1]["text"], "")

    def test_listening_holds_the_foreground(self):
        with self.client.websocket_connect("/ws/desktop") as ws:
            ws.send_json({"type": "hello", "token": TOKEN})
            ws.send_json({"type": "listening"})
            ws.send_json({"type": "ping"})
            while ws.receive_json()["type"] != "pong":
                pass
        self.assertEqual(self.holds, [30])


class FakeRedis:
    def __init__(self):
        self.ttl = {}

    async def pttl(self, key):
        return self.ttl.get(key, -2)

    async def set(self, key, value, px=None):
        self.ttl[key] = px

    async def exists(self, key):
        return 1 if self.ttl.get(key, -2) > 0 else 0


class ForegroundTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.redis = FakeRedis()
        p = mock.patch.object(foreground, "get_redis_client", lambda: self.redis)
        p.start()
        self.addCleanup(p.stop)

    async def test_hold_never_shortens_a_longer_hold(self):
        await foreground.hold(45)
        await foreground.hold(30)
        self.assertEqual(self.redis.ttl[foreground.KEY], 45000)

    async def test_idle_returns_at_once(self):
        self.assertLess(await foreground.wait_until_idle(max_wait=5), 0.1)

    async def test_waits_while_busy_then_goes(self):
        await foreground.hold(30)

        async def release():
            await asyncio.sleep(0.15)
            self.redis.ttl[foreground.KEY] = -2

        asyncio.create_task(release())
        waited = await foreground.wait_until_idle(max_wait=5, poll=0.05)
        self.assertGreater(waited, 0.1)
        self.assertLess(waited, 1)

    async def test_gives_up_after_max_wait(self):
        await foreground.hold(30)
        waited = await foreground.wait_until_idle(max_wait=0.1, poll=0.05)
        self.assertLess(waited, 0.5)


if __name__ == "__main__":
    unittest.main()
