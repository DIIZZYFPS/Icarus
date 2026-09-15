"""Phase 2: concurrent dispatch + registry, host-only (imports councilor.py)."""

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

from backend.agent import delegation
from backend.agent import subagent_registry
from backend.agent.delegation import CapabilitySpec
from backend.agent.subagent_registry import SubagentRegistry, describe_task, STATUS_RUNNING, STATUS_COMPLETED, STATUS_FAILED
from backend.tests.fake_llm import ScriptedLlamaServer, tool_call_response, text_response


class FakeRedis:
    def __init__(self):
        self.mailbox: list[str] = []

    async def publish(self, channel, payload):
        return 0

    async def lpush(self, key, payload):
        self.mailbox.append(payload)


@unittest.skipUnless(councilor is not None, "councilor.py only importable on the host")
class ConcurrentDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = SubagentRegistry(Path(self.tmp.name) / "councilor.db")
        await self.registry.init()
        self.redis = FakeRedis()

        async def fake_get_redis():
            return self.redis

        async def fake_publish_activity(**kw):
            pass

        self._patches = [
            mock.patch.object(councilor, "_get_redis", fake_get_redis),
            mock.patch.object(councilor, "_notify", lambda *a: None),
            mock.patch("backend.agent.activity_repo.publish_activity", fake_publish_activity),
            mock.patch.object(subagent_registry, "_registry", self.registry),
        ]
        for p in self._patches:
            p.start()
        councilor._inflight.clear()

    async def asyncTearDown(self):
        for p in self._patches:
            p.stop()
        await self.registry.close()
        self.tmp.cleanup()

    @staticmethod
    def _route(body: dict) -> str:
        # Each task's intent carries a marker; route on the first user message.
        for m in body["messages"]:
            if m.get("role") == "user":
                return "A" if "TASK_A" in m["content"] else "B"
        return "?"

    async def test_second_delegation_starts_before_the_first_finishes(self):
        go = asyncio.Event()
        a_blocked = asyncio.Event()

        async def wait_for_go() -> str:
            """block until released"""
            a_blocked.set()
            await go.wait()
            return "released"

        cat = {"slow": CapabilitySpec("slow", "test", False, lambda req: [wait_for_go])}
        server = ScriptedLlamaServer(
            {
                "A": [tool_call_response([("wait_for_go", "{}")]), text_response("A finished")],
                "B": [text_response("B finished")],
            },
            route=self._route,
        )
        with mock.patch.dict(delegation.CAPABILITY_CATALOG, cat, clear=False), server.patched():
            req_a = delegation.build_delegation_request(intent="TASK_A: wait", capabilities=["slow"], timestamp=1700000000)
            req_b = delegation.build_delegation_request(intent="TASK_B: quick", capabilities=[], timestamp=1700000001)
            task_a = councilor._handle_request(req_a.to_payload())
            task_b = councilor._handle_request(req_b.to_payload())
            self.assertEqual(set(councilor._inflight), {req_a.task_id, req_b.task_id})

            await asyncio.wait_for(a_blocked.wait(), 5)        # A is mid-tool, holding nothing but itself
            await asyncio.wait_for(task_b, 5)                  # B runs to completion meanwhile
            self.assertFalse(task_a.done())

            self.assertEqual((await self.registry.get(req_a.task_id))["status"], STATUS_RUNNING)
            self.assertEqual((await self.registry.get(req_b.task_id))["status"], STATUS_COMPLETED)
            self.assertIn("running", await describe_task(req_a.task_id))
            self.assertIn("B finished", await describe_task(req_b.task_id))

            go.set()
            await asyncio.wait_for(task_a, 5)

        self.assertEqual(councilor._inflight, {})
        a = await self.registry.get(req_a.task_id)
        self.assertEqual(a["status"], STATUS_COMPLETED)
        self.assertEqual(a["result"]["summary"], "A finished")
        self.assertIsNotNone(a["started_at"])
        self.assertIsNotNone(a["finished_at"])
        # Both envelopes reached the mailbox, one each.
        types = [json.loads(m)["task_id"] for m in self.redis.mailbox]
        self.assertEqual(sorted(types), sorted([req_a.task_id, req_b.task_id]))

    async def test_rejected_request_leaves_a_failed_row(self):
        data = {"type": "delegation", "intent": "mail", "capabilities": ["gmail_read"], "needs_network": False, "timestamp": 1700000000}
        await councilor.process_delegation(data)
        rows = await self.registry.list()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], STATUS_FAILED)
        self.assertIn("rejected", rows[0]["last_error"])
        self.assertIn("needs_network", await describe_task(rows[0]["id"]))

    async def test_registry_failure_never_breaks_the_task(self):
        server = ScriptedLlamaServer([text_response("fine")])
        req = delegation.build_delegation_request(intent="x", capabilities=[], timestamp=1700000000)
        broken = mock.AsyncMock(side_effect=RuntimeError("disk full"))
        with mock.patch.object(self.registry, "create", broken), \
             mock.patch.object(self.registry, "set_status", broken), server.patched():
            await councilor.process_delegation(req.to_payload())
        msg = json.loads(self.redis.mailbox[0])
        self.assertEqual(msg["envelope"]["status"], "completed")

    async def test_crashing_handler_is_logged_and_forgotten(self):
        async def boom():
            raise RuntimeError("kaboom")
        with self.assertLogs(councilor.logger, level="ERROR") as logs:
            task = councilor._dispatch("consultation-1-abcd", boom())
            with self.assertRaises(RuntimeError):
                await task
            await asyncio.sleep(0)  # let the done-callback run
        self.assertTrue(any("crashed" in line and "kaboom" in line for line in logs.output))
        self.assertNotIn("consultation-1-abcd", councilor._inflight)

    async def test_dispatch_key_prefers_task_id(self):
        self.assertEqual(councilor._dispatch_key("delegation", {"task_id": "sub-1700000000-abcdef"}), "sub-1700000000-abcdef")
        key = councilor._dispatch_key("consultation", {"timestamp": 5})
        self.assertTrue(key.startswith("consultation-5-"))

    async def test_handle_request_routes_every_type(self):
        seen = []

        async def rec(name, data):
            seen.append(name)

        with mock.patch.object(councilor, "process_delegation", lambda d: rec("delegation", d)), \
             mock.patch.object(councilor, "process_consultation", lambda d: rec("consultation", d)), \
             mock.patch.object(councilor, "process_deploy_check", lambda d: rec("deploy_check", d)), \
             mock.patch.object(councilor, "process_deploy_apply", lambda d: rec("deploy_apply", d)):
            tasks = [
                councilor._handle_request({"type": "escalation", "timestamp": 1}),
                councilor._handle_request({"type": "delegation", "timestamp": 2}),
                councilor._handle_request({"type": "deploy_check", "timestamp": 3}),
                councilor._handle_request({"type": "deploy_apply", "timestamp": 4}),
                councilor._handle_request({"timestamp": 5}),
            ]
            await asyncio.gather(*tasks)
        self.assertEqual(seen, ["delegation", "delegation", "deploy_check", "deploy_apply", "consultation"])


if __name__ == "__main__":
    unittest.main()
