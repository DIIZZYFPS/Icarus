"""Desktop avatar link: /ws/desktop protocol, the action round trip, and tool binding."""

import asyncio
import os
import threading
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.agent import desktop_session, desktop_tools, engine
from backend.agent.tools import current_platform
from backend.routes import desktop
from backend.tests.fake_llm import ScriptedLlamaServer, text_response

TOKEN = "test-token"


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(desktop.router)
    return app


async def _static_ambient():
    return {"type": "ambient", "heat": .3, "unread": 0, "tasks_running": 0, "recent_task": False}


def _recv_until(ws, kind: str, limit: int = 20) -> dict:
    """Skip ambient frames (and anything else) until a message of `kind` arrives."""
    for _ in range(limit):
        message = ws.receive_json()
        if message.get("type") == kind:
            return message
    raise AssertionError(f"no {kind!r} message")


class DesktopSocketTests(unittest.TestCase):
    def setUp(self):
        patches = [
            mock.patch.dict(os.environ, {"DESKTOP_TOKEN": TOKEN}),
            mock.patch.object(desktop, "ambient_snapshot", _static_ambient),
            mock.patch.object(desktop, "AMBIENT_INTERVAL_SECONDS", 60),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(_app())

    def _connect(self, token=TOKEN):
        ws = self.client.websocket_connect("/ws/desktop").__enter__()
        self.addCleanup(lambda: ws.__exit__(None, None, None))
        ws.send_json({"type": "hello", "token": token})
        return ws

    def test_wrong_token_is_rejected(self):
        ws = self._connect("nope")
        self.assertEqual(ws.receive_json(), {"type": "error", "message": "unauthorized"})

    def test_no_configured_token_rejects_everyone(self):
        with mock.patch.dict(os.environ, {"DESKTOP_TOKEN": ""}):
            ws = self._connect("")
            self.assertEqual(ws.receive_json()["message"], "unauthorized")

    def test_utterance_streams_status_then_reply(self):
        calls = []

        async def fake_process(platform, user_id, text, chat_id="0", on_activity=None, **kw):
            calls.append((platform, user_id, text, chat_id))
            await on_activity("🔎 Searching your history for “x”…")
            return "Two things today."

        with mock.patch.object(desktop, "process_message", fake_process):
            ws = self._connect()
            _recv_until(ws, "ready")
            ws.send_json({"type": "utterance", "turn_id": "t1", "text": "what's on today"})
            status = _recv_until(ws, "status")
            reply = _recv_until(ws, "reply")
        self.assertEqual(status, {"type": "status", "turn_id": "t1", "text": "Searching your history for “x”"})
        self.assertEqual(reply, {"type": "reply", "turn_id": "t1", "text": "Two things today."})
        self.assertEqual(calls, [("desktop", "operator", "what's on today", "desktop")])

    def test_desktop_tool_round_trips_through_the_daemon(self):
        async def fake_process(platform, user_id, text, chat_id="0", on_activity=None, **kw):
            return await desktop_tools.open_app("firefox")

        with mock.patch.object(desktop, "process_message", fake_process):
            ws = self._connect()
            _recv_until(ws, "ready")
            ws.send_json({"type": "utterance", "turn_id": "t2", "text": "open firefox"})
            action = _recv_until(ws, "action")
            self.assertEqual((action["name"], action["args"]), ("open_app", {"name": "firefox"}))
            ws.send_json({"type": "action_result", "id": action["id"], "ok": True, "result": "Opened Firefox"})
            reply = _recv_until(ws, "reply")
        self.assertEqual(reply["text"], "Opened Firefox")

    def test_failed_action_is_reported_to_the_model(self):
        async def fake_process(platform, user_id, text, chat_id="0", on_activity=None, **kw):
            return await desktop_tools.open_app("notepad++")

        with mock.patch.object(desktop, "process_message", fake_process):
            ws = self._connect()
            _recv_until(ws, "ready")
            ws.send_json({"type": "utterance", "turn_id": "t3", "text": "open notepad"})
            action = _recv_until(ws, "action")
            ws.send_json({"type": "action_result", "id": action["id"], "ok": False, "result": "no installed app matches"})
            reply = _recv_until(ws, "reply")
        self.assertEqual(reply["text"], "open_app failed: no installed app matches")

    def test_cancel_stops_the_turn_without_a_reply(self):
        cancelled = threading.Event()

        async def fake_process(platform, user_id, text, chat_id="0", on_activity=None, **kw):
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return "should never be sent"

        with mock.patch.object(desktop, "process_message", fake_process):
            ws = self._connect()
            _recv_until(ws, "ready")
            ws.send_json({"type": "utterance", "turn_id": "t4", "text": "long thing"})
            ws.send_json({"type": "ping"})
            _recv_until(ws, "pong")
            ws.send_json({"type": "cancel", "turn_id": "t4"})
            ws.send_json({"type": "ping"})
            _recv_until(ws, "pong")
        self.assertTrue(cancelled.wait(2))

    def test_failed_turn_sends_an_error(self):
        async def fake_process(*a, **kw):
            raise RuntimeError("llama-server down")

        with mock.patch.object(desktop, "process_message", fake_process):
            ws = self._connect()
            _recv_until(ws, "ready")
            ws.send_json({"type": "utterance", "turn_id": "t5", "text": "hi"})
            error = _recv_until(ws, "error")
        self.assertEqual(error["turn_id"], "t5")


class DesktopToolBindingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async def _send(message):
            pass
        self.session = desktop_session.DesktopSession(_send)

    def tearDown(self):
        desktop_session.clear_active(self.session)

    async def _offered(self, platform: str) -> tuple[list[str], str]:
        server = ScriptedLlamaServer([text_response("ok")])
        token = current_platform.set(platform)
        try:
            with server.patched():
                await engine.run_icarus("hello")
        finally:
            current_platform.reset(token)
        system = server.requests[0]["messages"][0]["content"]
        return server.tool_names_offered(0), system

    async def test_desktop_turn_with_daemon_gets_desktop_tools_and_voice_prompt(self):
        desktop_session.set_active(self.session)
        names, system = await self._offered("desktop")
        self.assertIn("open_app", names)
        self.assertIn("spotify_play", names)
        self.assertIn("Desktop voice mode", system)

    async def test_desktop_turn_without_daemon_gets_voice_prompt_but_no_tools(self):
        names, system = await self._offered("desktop")
        self.assertNotIn("open_app", names)
        self.assertIn("Desktop voice mode", system)

    async def test_other_platforms_never_get_desktop_tools(self):
        desktop_session.set_active(self.session)
        names, system = await self._offered("discord")
        self.assertNotIn("open_app", names)
        self.assertNotIn("Desktop voice mode", system)

    async def test_tool_without_daemon_says_so(self):
        self.assertIn("not connected", await desktop_tools.now_playing())

    async def test_disconnect_fails_pending_action(self):
        desktop_session.set_active(self.session)
        pending = asyncio.create_task(desktop_tools.now_playing())
        await asyncio.sleep(0)
        self.session.close()
        self.assertIn("Desktop unavailable", await pending)


class StatusCleaningTests(unittest.TestCase):
    def test_strips_emoji_and_ellipsis(self):
        self.assertEqual(desktop.clean_status("📅 Checking your calendar…"), "Checking your calendar")
        self.assertEqual(desktop.clean_status("🎵 Searching Spotify for “x”…"), "Searching Spotify for “x”")


if __name__ == "__main__":
    unittest.main()
