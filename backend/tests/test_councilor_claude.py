"""Councilor `claude_code` tasks: registry, progress events and delivery (host-only)."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    import councilor
except ImportError:  # in-container: councilor.py isn't shipped in the image
    councilor = None

from backend.agent.claude_runner import ClaudeResult


class FakeRedis:
    def __init__(self):
        self.lists = {}

    async def lpush(self, key, payload):
        self.lists.setdefault(key, []).insert(0, json.loads(payload))

    async def ltrim(self, key, start, end):
        pass

    async def publish(self, channel, payload):
        return 0


@unittest.skipUnless(councilor is not None, "councilor.py only importable on the host")
class ProcessClaudeCodeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from backend.agent.subagent_registry import SubagentRegistry
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = SubagentRegistry(Path(self.tmp.name) / "councilor.db")
        await self.registry.init()
        self.redis, self.notified, self.runs = FakeRedis(), [], []

        async def fake_get_redis():
            return self.redis

        async def fake_publish_activity(**kw):
            pass

        async def fake_run(**kw):
            self.runs.append(kw)
            await kw["on_progress"]("Editing app.py")
            return ClaudeResult(True, "Fixed the greeting.", branch="claude/sub-5-aaaaaa",
                                worktree="/wt/sub-5", diffstat="1 file changed",
                                artifacts={"branch": "claude/sub-5-aaaaaa"}, elapsed_s=12)

        self._patches = [
            mock.patch.object(councilor, "_registry", lambda: self.registry),
            mock.patch.object(councilor, "_get_redis", fake_get_redis),
            mock.patch.object(councilor, "_notify", lambda p, c, m: self.notified.append((p, c, m))),
            mock.patch("backend.agent.activity_repo.publish_activity", fake_publish_activity),
            mock.patch("backend.agent.claude_runner.run_claude", fake_run),
            mock.patch.dict(os.environ, {"DESKTOP_CLAUDE_PROJECTS": "icarus=/tmp"}),
        ]
        for p in self._patches:
            p.start()

    async def asyncTearDown(self):
        for p in self._patches:
            p.stop()
        await self.registry.close()
        self.tmp.cleanup()

    async def test_edit_task_runs_reports_progress_and_delivers_an_envelope(self):
        await councilor.process_claude_code({
            "type": "claude_code", "task_id": "sub-5-aaaaaa", "timestamp": 5, "project": "icarus",
            "task": "Fix the greeting", "mode": "edit", "platform": "desktop", "chat_id": "desktop",
        })
        self.assertEqual(self.runs[0]["mode"], "edit")
        events = [e["text"] for e in reversed(self.redis.lists["icarus:desktop:task_events"])]
        self.assertEqual(events, ["Claude: editing icarus", "Editing app.py", "Claude finished"])
        row = await self.registry.get("sub-5-aaaaaa")
        self.assertEqual((row["status"], row["request_type"]), ("completed", "claude_code"))
        mailbox = self.redis.lists["icarus:councilor:responses"][0]
        self.assertEqual((mailbox["type"], mailbox["platform"]), ("delegation", "desktop"))
        self.assertIn("claude/sub-5-aaaaaa", mailbox["message"])

    async def test_unknown_project_fails_cleanly(self):
        await councilor.process_claude_code({"task_id": "sub-6-bbbbbb", "timestamp": 6, "project": "nope",
                                             "task": "x", "mode": "ask", "platform": "desktop"})
        self.assertEqual(self.runs, [])
        row = await self.registry.get("sub-6-bbbbbb")
        self.assertEqual(row["status"], "failed")
        self.assertIn("unknown project", row["last_error"])


if __name__ == "__main__":
    unittest.main()
