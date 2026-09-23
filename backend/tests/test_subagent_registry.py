import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.agent import subagent_registry as reg
from backend.agent.subagent_registry import (
    KIND_ONESHOT, KIND_PERSISTENT, STATUS_QUEUED, STATUS_RUNNING, STATUS_PAUSED,
    STATUS_COMPLETED, STATUS_FAILED, DESIRED_RUNNING, SubagentRegistry, describe_task,
)


class RegistryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = SubagentRegistry(Path(self.tmp.name) / "nested" / "councilor.db")
        await self.registry.init()

    async def asyncTearDown(self):
        await self.registry.close()
        self.tmp.cleanup()

    async def _create(self, task_id="sub-1700000000-aaaaaa", **over):
        base = dict(
            task_id=task_id, kind=KIND_ONESHOT, request_type="delegation", intent="summarize X",
            capabilities=["web"], needs_network=True, needs_repo_write=False,
            platform="discord", chat_id="9001", user_id="42", thread_id=f"sub-{task_id}",
        )
        base.update(over)
        return await self.registry.create(**base)

    async def test_init_creates_file_and_wal(self):
        self.assertTrue(self.registry.exists())
        con = sqlite3.connect(self.registry.path)
        try:
            self.assertEqual(con.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
        finally:
            con.close()

    async def test_create_and_get_round_trip(self):
        created = await self._create()
        self.assertEqual(created["status"], STATUS_QUEUED)
        self.assertIsNone(created["started_at"])
        fetched = await self.registry.get("sub-1700000000-aaaaaa")
        self.assertEqual(fetched["capability_scope"], {"capabilities": ["web"], "needs_network": True, "needs_repo_write": False})
        self.assertEqual(fetched["thread_id"], "sub-sub-1700000000-aaaaaa")
        self.assertIsNone(await self.registry.get("sub-1700000000-ffffff"))

    async def test_status_transitions_stamp_timestamps_and_result(self):
        await self._create()
        self.assertTrue(await self.registry.set_status("sub-1700000000-aaaaaa", STATUS_RUNNING))
        row = await self.registry.get("sub-1700000000-aaaaaa")
        self.assertIsNotNone(row["started_at"])
        self.assertIsNone(row["finished_at"])

        envelope = {"task_id": "sub-1700000000-aaaaaa", "status": "completed", "summary": "done", "artifacts": {"pr_url": "https://pr/1"}}
        await self.registry.set_status("sub-1700000000-aaaaaa", STATUS_COMPLETED, result=envelope)
        row = await self.registry.get("sub-1700000000-aaaaaa")
        self.assertEqual(row["status"], STATUS_COMPLETED)
        self.assertIsNotNone(row["finished_at"])
        self.assertEqual(row["result"], envelope)

        await self.registry.set_status("sub-1700000000-aaaaaa", STATUS_FAILED, error="x" * 3000)
        row = await self.registry.get("sub-1700000000-aaaaaa")
        self.assertEqual(len(row["last_error"]), 2000)

    async def test_update_unknown_row_or_field(self):
        self.assertFalse(await self.registry.update("sub-1700000000-nope00", status=STATUS_RUNNING))
        await self._create()
        with self.assertRaises(ValueError):
            await self.registry.update("sub-1700000000-aaaaaa", bogus=1)
        with self.assertRaises(ValueError):
            await self.registry.update("sub-1700000000-aaaaaa", id="other")

    async def test_list_filters(self):
        await self._create("sub-1700000000-aaaaaa")
        await self._create("sub-1700000000-bbbbbb", kind=KIND_PERSISTENT, desired_state=DESIRED_RUNNING, status=STATUS_RUNNING)
        await self._create("sub-1700000000-cccccc")
        await self.registry.set_status("sub-1700000000-cccccc", STATUS_COMPLETED)

        ids = lambda rows: sorted(r["id"] for r in rows)
        self.assertEqual(ids(await self.registry.list()), ["sub-1700000000-aaaaaa", "sub-1700000000-bbbbbb", "sub-1700000000-cccccc"])
        self.assertEqual(ids(await self.registry.list(kind=KIND_PERSISTENT)), ["sub-1700000000-bbbbbb"])
        self.assertEqual(ids(await self.registry.list(statuses=(STATUS_QUEUED,))), ["sub-1700000000-aaaaaa"])
        self.assertEqual(ids(await self.registry.list(desired_state=DESIRED_RUNNING)), ["sub-1700000000-bbbbbb"])
        self.assertEqual(len(await self.registry.list(limit=2)), 2)

    async def test_fail_incomplete_oneshots_only_touches_active_oneshots(self):
        await self._create("sub-1700000000-aaaaaa")                                        # queued
        await self._create("sub-1700000000-bbbbbb", status=STATUS_RUNNING)                 # running
        await self._create("sub-1700000000-cccccc", status=STATUS_PAUSED)                  # paused
        await self._create("sub-1700000000-dddddd", status=STATUS_COMPLETED)               # terminal
        await self._create("sub-1700000000-eeeeee", kind=KIND_PERSISTENT, status=STATUS_RUNNING, desired_state=DESIRED_RUNNING)

        failed = await self.registry.fail_incomplete_oneshots("councilor restarted")
        self.assertEqual(sorted(failed), ["sub-1700000000-aaaaaa", "sub-1700000000-bbbbbb", "sub-1700000000-cccccc"])
        for tid in failed:
            row = await self.registry.get(tid)
            self.assertEqual(row["status"], STATUS_FAILED)
            self.assertEqual(row["last_error"], "councilor restarted")
            self.assertIsNotNone(row["finished_at"])
        self.assertEqual((await self.registry.get("sub-1700000000-dddddd"))["status"], STATUS_COMPLETED)
        self.assertEqual((await self.registry.get("sub-1700000000-eeeeee"))["status"], STATUS_RUNNING)
        self.assertEqual(await self.registry.fail_incomplete_oneshots("again"), [])

    async def test_increment_restart(self):
        await self._create()
        self.assertEqual(await self.registry.increment_restart("sub-1700000000-aaaaaa"), 1)
        self.assertEqual(await self.registry.increment_restart("sub-1700000000-aaaaaa"), 2)
        self.assertEqual(await self.registry.increment_restart("sub-1700000000-nope00"), 0)

    async def test_describe_task_variants(self):
        await self._create()
        await self.registry.set_status(
            "sub-1700000000-aaaaaa", STATUS_COMPLETED,
            result={"summary": "Found 3 things.", "artifacts": {"pr_url": "https://pr/7"}},
        )
        text = await describe_task("sub-1700000000-aaaaaa", self.registry)
        self.assertIn("completed", text)
        self.assertIn("caps: web [network]", text)
        self.assertIn("result: Found 3 things.", text)
        self.assertIn("PR: https://pr/7", text)

        self.assertIn("No task with id", await describe_task("sub-1700000000-ffffff", self.registry))
        self.assertIn("not a task id", await describe_task("esc-123", self.registry))
        listing = await describe_task("", self.registry)
        self.assertIn("Recent tasks", listing)
        self.assertIn("sub-1700000000-aaaaaa", listing)

        await self._create("sub-1700000000-bbbbbb", status=STATUS_PAUSED)
        await self.registry.update("sub-1700000000-bbbbbb", paused_question="Which inbox label?")
        self.assertIn("PAUSED — waiting on the operator: Which inbox label?", await describe_task("sub-1700000000-bbbbbb", self.registry))

    async def test_describe_task_without_a_registry_file(self):
        missing = SubagentRegistry(Path(self.tmp.name) / "nowhere" / "councilor.db")
        text = await describe_task("sub-1700000000-aaaaaa", missing)
        self.assertIn("isn't available yet", text)
        self.assertFalse(missing.path.exists())  # a reader must never create the file


class DefaultPathTests(unittest.TestCase):
    def test_env_override_wins(self):
        with mock.patch.dict(os.environ, {"COUNCILOR_DB_PATH": "/tmp/x/councilor.db"}):
            self.assertEqual(reg.default_registry_path(), Path("/tmp/x/councilor.db"))

    def test_default_is_workspace_memory(self):
        with mock.patch.dict(os.environ, {"COUNCILOR_DB_PATH": ""}):
            path = reg.default_registry_path()
        self.assertEqual(path.name, "councilor.db")
        self.assertEqual(path.parent.name, "memory")
        self.assertEqual(path.parent.parent.name, "workspace")


if __name__ == "__main__":
    unittest.main()
