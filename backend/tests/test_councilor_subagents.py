"""Phase 3: Councilor-side persistent subagent handlers, host-only."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    import councilor
except ImportError:
    councilor = None

from backend.agent import subagent_registry
from backend.agent.subagent_manager import ReconcileReport, SubagentLimitError, SubagentNotFound
from backend.agent.subagent_registry import (
    KIND_ONESHOT, KIND_PERSISTENT, STATUS_RUNNING, STATUS_STOPPED, STATUS_FAILED, DESIRED_RUNNING, DESIRED_STOPPED,
    SubagentRegistry,
)


class FakeRedis:
    def __init__(self):
        self.mailbox: list[dict] = []

    async def publish(self, channel, payload):
        return 0

    async def lpush(self, key, payload):
        self.mailbox.append(json.loads(payload))


class FakeRuntime:
    def __init__(self, available=True):
        self._available = available

    async def available(self):
        return self._available


class FakeManager:
    def __init__(self, registry):
        self.registry = registry
        self.runtime = FakeRuntime()
        self.create_error = None
        self.report = ReconcileReport()
        self.stopped = []

    async def create(self, **kw):
        if self.create_error:
            raise self.create_error
        return await self.registry.create(
            task_id="sub-1700000000-c0ffee", kind=KIND_PERSISTENT, request_type="subagent", intent=kw["intent"],
            capabilities=kw["capabilities"], needs_network=kw["needs_network"], needs_repo_write=False,
            platform=kw["platform"], chat_id=kw["chat_id"], user_id=kw["user_id"], thread_id="sub-sub-1700000000-c0ffee",
            status=STATUS_RUNNING, desired_state=DESIRED_RUNNING, container_name="icarus-subagent-sub-1700000000-c0ffee",
            interval_seconds=kw["interval_seconds"] or 900, expires_at="2099-01-01T00:00:00Z",
        )

    async def stop(self, task_id, reason="operator request"):
        row = await self.registry.get(task_id)
        if row is None or row["kind"] != KIND_PERSISTENT:
            raise SubagentNotFound(task_id)
        self.stopped.append(task_id)
        await self.registry.set_status(task_id, STATUS_STOPPED, desired_state=DESIRED_STOPPED,
                                       result={"stopped_reason": reason, "container_removed": True})
        return await self.registry.get(task_id)

    async def reconcile(self):
        return self.report


@unittest.skipUnless(councilor is not None, "councilor.py only importable on the host")
class CouncilorSubagentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = SubagentRegistry(Path(self.tmp.name) / "councilor.db")
        await self.registry.init()
        self.redis = FakeRedis()
        self.notified = []
        self.activity = []
        self.manager = FakeManager(self.registry)

        async def fake_get_redis():
            return self.redis

        async def fake_publish_activity(**kw):
            self.activity.append(kw)

        self._patches = [
            mock.patch.object(councilor, "_get_redis", fake_get_redis),
            mock.patch.object(councilor, "_notify", lambda p, c, m: self.notified.append((p, c, m))),
            mock.patch("backend.agent.activity_repo.publish_activity", fake_publish_activity),
            mock.patch.object(subagent_registry, "_registry", self.registry),
            mock.patch.object(councilor, "_manager", lambda: self.manager),
        ]
        for p in self._patches:
            p.start()
        councilor._inflight.clear()

    async def asyncTearDown(self):
        for p in self._patches:
            p.stop()
        await self.registry.close()
        self.tmp.cleanup()

    async def test_create_confirms_with_id_and_usage_hints(self):
        await councilor.process_subagent_create({
            "type": "subagent_create", "timestamp": 1700000000, "platform": "discord", "chat_id": "9001", "user_id": "42",
            "intent": "watch invoices", "capabilities": ["gmail_read"], "needs_network": True, "interval_seconds": 600,
        })
        msg = self.redis.mailbox[0]
        self.assertEqual(msg["type"], "subagent_lifecycle")
        self.assertEqual(msg["task_id"], "sub-1700000000-c0ffee")
        self.assertEqual((msg["platform"], msg["chat_id"]), ("discord", "9001"))
        self.assertIn("is running", msg["message"])
        self.assertIn("check_task_status('sub-1700000000-c0ffee')", msg["message"])
        self.assertIn("every 10 min", msg["message"])
        self.assertIn("[Icarus Subagent]", self.notified[0][2])
        self.assertEqual(self.activity[0]["event_type"], "subagent_created")
        self.assertEqual(self.activity[0]["thread_id"], "sub-sub-1700000000-c0ffee")

    async def test_create_limit_and_declaration_errors_are_reported_not_raised(self):
        self.manager.create_error = SubagentLimitError("cap is 3")
        await councilor.process_subagent_create({"timestamp": 1, "intent": "x", "capabilities": [], "platform": "telegram", "chat_id": "5"})
        msg = self.redis.mailbox[0]
        self.assertIn("not created: cap is 3", msg["message"])
        self.assertIsNone(msg["task_id"])
        self.assertEqual(self.activity[0]["event_type"], "failed")

    async def test_stop_persistent_goes_through_the_manager(self):
        await self.manager.create(intent="x", capabilities=[], needs_network=False, platform="discord", chat_id="1", user_id="2", interval_seconds=None)
        await councilor.process_subagent_stop({"timestamp": 1, "task_id": "sub-1700000000-c0ffee", "platform": "discord", "chat_id": "1"})
        self.assertEqual(self.manager.stopped, ["sub-1700000000-c0ffee"])
        self.assertIn("Stopped persistent subagent", self.redis.mailbox[0]["message"])
        self.assertEqual((await self.registry.get("sub-1700000000-c0ffee"))["status"], STATUS_STOPPED)

    async def test_stop_cancels_an_inflight_oneshot(self):
        await self.registry.create(task_id="sub-1700000000-aaaaaa", kind=KIND_ONESHOT, request_type="delegation",
                                   intent="x", capabilities=[], needs_network=False, needs_repo_write=False, status=STATUS_RUNNING)

        async def forever():
            await asyncio.Event().wait()

        task = councilor._dispatch("sub-1700000000-aaaaaa", forever())
        await asyncio.sleep(0)
        await councilor.process_subagent_stop({"timestamp": 1, "task_id": "sub-1700000000-aaaaaa"})
        with self.assertRaises(asyncio.CancelledError):
            await task
        row = await self.registry.get("sub-1700000000-aaaaaa")
        self.assertEqual(row["status"], STATUS_FAILED)
        self.assertIn("cancelled by operator", row["last_error"])
        self.assertIn("Cancelled running task", self.redis.mailbox[0]["message"])

    async def test_stop_unknown_or_finished(self):
        await councilor.process_subagent_stop({"timestamp": 1, "task_id": "sub-1700000000-ffffff"})
        self.assertIn("No task or subagent", self.redis.mailbox[0]["message"])
        await self.registry.create(task_id="sub-1700000000-bbbbbb", kind=KIND_ONESHOT, request_type="delegation",
                                   intent="x", capabilities=[], needs_network=False, needs_repo_write=False, status="completed")
        await councilor.process_subagent_stop({"timestamp": 1, "task_id": "sub-1700000000-bbbbbb"})
        self.assertIn("already finished", self.redis.mailbox[1]["message"])

    async def test_reconcile_notifies_on_respawn_and_config_failure_only(self):
        for tid in ("sub-1700000000-aaaaaa", "sub-1700000000-bbbbbb", "sub-1700000000-cccccc"):
            await self.registry.create(task_id=tid, kind=KIND_PERSISTENT, request_type="subagent", intent="x",
                                       capabilities=[], needs_network=False, needs_repo_write=False,
                                       platform="discord", chat_id="9001", status=STATUS_RUNNING, desired_state=DESIRED_RUNNING)
        await self.registry.update("sub-1700000000-bbbbbb", last_error="worker exited with a configuration error")
        self.manager.report = ReconcileReport(healthy=["sub-1700000000-cccccc"], respawned=["sub-1700000000-aaaaaa"], config_failed=["sub-1700000000-bbbbbb"])
        report = await councilor._reconcile_subagents("test")
        self.assertIs(report, self.manager.report)
        messages = {m["task_id"]: m["message"] for m in self.redis.mailbox}
        self.assertEqual(set(messages), {"sub-1700000000-aaaaaa", "sub-1700000000-bbbbbb"})
        self.assertIn("respawned", messages["sub-1700000000-aaaaaa"])
        self.assertIn("configuration error", messages["sub-1700000000-bbbbbb"])

    async def test_reconcile_skips_when_docker_is_unavailable(self):
        self.manager.runtime = FakeRuntime(available=False)
        self.assertIsNone(await councilor._reconcile_subagents("test"))
        self.assertEqual(self.redis.mailbox, [])

    async def test_handle_request_routes_subagent_types(self):
        seen = []

        async def rec(name, data):
            seen.append(name)

        with mock.patch.object(councilor, "process_subagent_create", lambda d: rec("create", d)), \
             mock.patch.object(councilor, "process_subagent_stop", lambda d: rec("stop", d)):
            await asyncio.gather(
                councilor._handle_request({"type": "subagent_create", "timestamp": 1}),
                councilor._handle_request({"type": "subagent_stop", "timestamp": 2, "task_id": "sub-1700000000-abcdef"}),
            )
        self.assertEqual(seen, ["create", "stop"])


if __name__ == "__main__":
    unittest.main()
