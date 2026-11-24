"""Councilor `claude_code` tasks: registry, progress events and delivery (host-only)."""

import asyncio
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

        self.task_updates = []

        async def fake_task_update(task_id, text, done=False, **kw):
            self.task_updates.append((task_id, text, done, kw))

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
            mock.patch("backend.agent.busy.task_update", fake_task_update),
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
        self.assertEqual([(u[1], u[2]) for u in self.task_updates],
                         [("Claude: editing icarus", False), ("Editing app.py", False), ("Claude finished", True)])
        self.assertFalse(self.task_updates[0][3]["step"])      # the kickoff line isn't a tool call
        self.assertTrue(self.task_updates[1][3]["step"])
        self.assertEqual(self.task_updates[0][3]["project"], "icarus")
        row = await self.registry.get("sub-5-aaaaaa")
        self.assertEqual((row["status"], row["request_type"]), ("completed", "claude_code"))
        mailbox = self.redis.lists["icarus:councilor:responses"][0]
        self.assertEqual((mailbox["type"], mailbox["platform"]), ("delegation", "desktop"))
        self.assertIn("claude/sub-5-aaaaaa", mailbox["message"])

    async def test_workspace_task_runs_in_its_area_and_reports_what_it_saved(self):
        calls = []

        async def fake_workspace(**kw):
            calls.append(kw)
            await kw["on_progress"]("Reading the assignment")
            return ClaudeResult(True, "Pulled week 3 and drafted notes.", files=["Lab8/notes.md"], elapsed_s=8,
                                artifacts={"folder": "/c/CS4440", "changed": ["lab7.txt"], "removed": [], "tracked": True})

        with mock.patch("backend.agent.claude_runner.run_workspace", fake_workspace), \
             mock.patch.dict(os.environ, {"DESKTOP_CLAUDE_WORKSPACES": "courses=/c+canvas"}):
            await councilor.process_claude_code({
                "type": "claude_code", "task_id": "sub-9-eeeeee", "timestamp": 9, "project": "courses", "area": "CS4440",
                "task": "Pull the week 3 assignment", "mode": "edit", "platform": "desktop", "chat_id": "desktop"})
        self.assertEqual((calls[0]["area"], calls[0]["workspace"].name), ("CS4440", "courses"))
        self.assertEqual(self.runs, [])                                    # not the git-worktree runner
        row = await self.registry.get("sub-9-eeeeee")
        self.assertEqual(row["status"], "completed")
        self.assertIn("[claude work \u00b7 courses/CS4440]", row["intent"])
        self.assertFalse(row["capability_scope"]["needs_repo_write"])      # whatever mode was asked for
        message = self.redis.lists["icarus:councilor:responses"][0]["message"]
        self.assertIn("Saved 1 new file in /c/CS4440: Lab8/notes.md.", message)
        self.assertIn("Heads up: existing files were touched (changed lab7.txt)", message)
        self.assertEqual(self.task_updates[0][3]["project"], "courses/CS4440")
        self.assertEqual(self.task_updates[-1][1:3], ("Claude finished", True))

    async def test_unknown_project_names_the_workspaces_too(self):
        with mock.patch.dict(os.environ, {"DESKTOP_CLAUDE_WORKSPACES": "courses=/c"}):
            await councilor.process_claude_code({"task_id": "sub-10-ffffff", "timestamp": 10, "project": "nope",
                                                 "task": "x", "platform": "desktop"})
        row = await self.registry.get("sub-10-ffffff")
        self.assertEqual(row["status"], "failed")
        self.assertIn("courses", row["last_error"])

    async def test_a_cancelled_task_leaves_the_avatars_badge(self):
        async def hanging_run(**kw):
            await asyncio.sleep(60)
        with mock.patch("backend.agent.claude_runner.run_claude", hanging_run):
            task = asyncio.create_task(councilor.process_claude_code({
                "type": "claude_code", "task_id": "sub-8-dddddd", "timestamp": 8, "project": "icarus",
                "task": "Fix the greeting", "mode": "edit", "platform": "desktop", "chat_id": "desktop"}))
            await asyncio.sleep(.1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        text, done, kw = self.task_updates[-1][1], self.task_updates[-1][2], self.task_updates[-1][3]
        self.assertEqual((text, done, kw["ok"]), ("Claude task stopped", True, False))

    async def test_landed_edit_says_where_it_was_applied(self):
        async def landed_run(**kw):
            return ClaudeResult(True, "Fixed the greeting.", branch="claude/sub-7-cccccc", landed_on="feat/x",
                                diffstat="1 file changed", elapsed_s=3, files=["desktop/renderer/avatar.py"])
        with mock.patch("backend.agent.claude_runner.run_claude", landed_run):
            await councilor.process_claude_code({"task_id": "sub-7-cccccc", "timestamp": 7, "project": "icarus",
                                                 "task": "Fix", "mode": "edit", "platform": "desktop"})
        message = self.redis.lists["icarus:councilor:responses"][0]["message"]
        self.assertIn("Applied to feat/x (1 file changed)", message)
        self.assertNotIn("git merge", message)
        self.assertIn("restarting: desktop", message)

    def test_restart_hint_maps_files_to_services(self):
        hint = councilor.restart_hint
        self.assertEqual(hint("icarus", ["desktop/x.py", "backend/agent/a.py", "backend/tests/t.py"]), ["api", "councilor", "desktop"])
        self.assertEqual(hint("icarus", ["councilor.py"]), ["councilor"])
        self.assertEqual(hint("icarus", ["README.md", "backend/tests/t.py"]), [])
        self.assertEqual(hint("site", ["desktop/x.py"]), [])

    async def test_held_edit_says_why_and_how_to_apply(self):
        async def held_run(**kw):
            return ClaudeResult(True, "Fixed.", branch="claude/sub-8-dddddd", worktree="/wt/sub-8",
                                hold_reason="you have uncommitted changes in app.py", diffstat="1 file changed")
        with mock.patch("backend.agent.claude_runner.run_claude", held_run):
            await councilor.process_claude_code({"task_id": "sub-8-dddddd", "timestamp": 8, "project": "icarus",
                                                 "task": "Fix", "mode": "edit", "platform": "desktop"})
        message = self.redis.lists["icarus:councilor:responses"][0]["message"]
        self.assertIn("Not applied because you have uncommitted changes in app.py", message)
        self.assertIn("git merge claude/sub-8-dddddd", message)

    async def test_unknown_project_fails_cleanly(self):
        await councilor.process_claude_code({"task_id": "sub-6-bbbbbb", "timestamp": 6, "project": "nope",
                                             "task": "x", "mode": "ask", "platform": "desktop"})
        self.assertEqual(self.runs, [])
        row = await self.registry.get("sub-6-bbbbbb")
        self.assertEqual(row["status"], "failed")
        self.assertIn("unknown project", row["last_error"])


if __name__ == "__main__":
    unittest.main()
