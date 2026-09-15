import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.agent import delegation
from backend.agent.delegation import CapabilitySpec
from backend.agent import worker_subagent as ws
from backend.agent.worker_subagent import (
    EXIT_CONFIG_ERROR, EXIT_OK, MAX_REPORTS_PER_CYCLE, PersistentSubagent, SubagentConfigError, SubagentState, load_spec,
)


class FakeRedis:
    def __init__(self):
        self.mailbox: list[dict] = []
        self.hashes: dict[str, dict] = {}
        self.expires: dict[str, int] = {}

    async def lpush(self, key, value):
        self.mailbox.append(json.loads(value))

    async def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = json.loads(value)

    async def expire(self, key, seconds):
        self.expires[key] = seconds


class StateTests(unittest.IsolatedAsyncioTestCase):
    async def test_notes_and_cycles(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = SubagentState(Path(tmp) / "deep" / "state.db")
            await state.init()
            self.assertEqual(await state.read_notes(), [])
            await state.save_note("inbox", "checked 3 mails")
            await state.save_note("inbox", "checked 4 mails")
            await state.save_note("other", "x")
            notes = await state.read_notes()
            self.assertEqual({k: t for k, t, _ in notes}, {"inbox": "checked 4 mails", "other": "x"})
            self.assertTrue(await state.delete_note("other"))
            self.assertFalse(await state.delete_note("other"))
            self.assertEqual(await state.last_cycle(), 0)
            await state.record_cycle(1, "t0", "t1", "nothing")
            await state.record_cycle(2, "t2", "t3", "reported")
            self.assertEqual(await state.last_cycle(), 2)


class PersistentSubagentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmp.name) / "sub-1700000000-abcdef"
        self.state_dir.mkdir()
        self.spec = {
            "task_id": "sub-1700000000-abcdef", "intent": "watch the inbox for invoices", "capabilities": ["time"],
            "needs_network": False, "interval_seconds": 60, "expires_at": None,
            "platform": "discord", "chat_id": "9001", "user_id": "42",
        }
        self.redis = FakeRedis()

        async def no_activity(**kw):
            pass
        self._activity = mock.patch("backend.agent.activity_repo.publish_activity", no_activity)
        self._activity.start()

    async def asyncTearDown(self):
        self._activity.stop()
        self.tmp.cleanup()

    def _agent(self, loop=None, **spec_over):
        spec = {**self.spec, **spec_over}
        return PersistentSubagent(spec, self.state_dir, redis=self.redis, agent_loop=loop)

    async def test_build_tools_binds_own_tools_plus_grants(self):
        agent = self._agent()
        agent.build_tools()
        self.assertEqual(
            [t.__name__ for t in agent.tools],
            ["save_note", "read_notes", "delete_note", "report_to_operator", "ask_supervisor", "get_time"],
        )
        self.assertIn("- time: get_time", agent.grants)
        self.assertIn("sub-1700000000-abcdef", agent.system_prompt)
        self.assertIn("Capabilities granted", agent.system_prompt)
        self.assertIn("persistent subagent", agent.system_prompt)

    async def test_build_tools_rejects_bad_or_unavailable_capabilities(self):
        with self.assertRaises(SubagentConfigError):
            self._agent(capabilities=["web"], needs_network=False).build_tools()

        def broken(req):
            raise ImportError("No module named 'google'")
        with mock.patch.dict(delegation.CAPABILITY_CATALOG, {"gmail_read": CapabilitySpec("gmail_read", "", True, broken)}):
            with self.assertRaises(SubagentConfigError) as ctx:
                self._agent(capabilities=["gmail_read"], needs_network=True).build_tools()
        self.assertIn("gmail_read", str(ctx.exception))

    async def test_cycle_prompt_includes_directive_and_notes_and_is_recorded(self):
        seen = []

        async def loop(**kw):
            seen.append(kw)
            return "checked, nothing new"

        agent = self._agent(loop)
        await agent.state.init()
        await agent.state.save_note("last_seen", "invoice #12 on Monday")
        agent.build_tools()

        outcome = await agent.run_cycle()
        self.assertEqual(outcome, "checked, nothing new")
        kw = seen[0]
        self.assertIn("Cycle 1.", kw["initial_prompt"])
        self.assertIn("this is your first cycle", kw["initial_prompt"])
        self.assertIn("watch the inbox for invoices", kw["initial_prompt"])
        self.assertIn("invoice #12 on Monday", kw["initial_prompt"])
        self.assertEqual(kw["system_instruction"], agent.system_prompt)
        self.assertIs(kw["tools"], agent.tools)
        self.assertEqual(kw["max_turns"], ws.MAX_TURNS_PER_CYCLE)
        self.assertNotIn("supervisor", kw)

        await agent.run_cycle()
        self.assertEqual(await agent.state.last_cycle(), 2)
        self.assertIn("Cycle 2.", seen[1]["initial_prompt"])

    async def test_supervisor_is_forwarded_when_set(self):
        seen = []

        async def loop(**kw):
            seen.append(kw)
            return "ok"

        async def supervisor(event):
            return None

        agent = PersistentSubagent(self.spec, self.state_dir, redis=self.redis, agent_loop=loop, supervisor=supervisor)
        await agent.state.init()
        agent.build_tools()
        await agent.run_cycle()
        self.assertIs(seen[0]["supervisor"], supervisor)

    async def test_report_to_operator_pushes_mailbox_payload_and_rate_limits(self):
        agent = self._agent()
        for i in range(MAX_REPORTS_PER_CYCLE):
            self.assertEqual(await agent.report_to_operator(f"update {i}"), "Reported to the operator.")
        self.assertIn("Report limit reached", await agent.report_to_operator("one more"))
        self.assertEqual(len(self.redis.mailbox), MAX_REPORTS_PER_CYCLE)
        payload = self.redis.mailbox[0]
        self.assertEqual(payload["type"], "subagent_report")
        self.assertEqual(payload["task_id"], "sub-1700000000-abcdef")
        self.assertEqual((payload["platform"], payload["chat_id"], payload["user_id"]), ("discord", "9001", "42"))
        self.assertEqual(payload["message"], "update 0")

        agent._reports_this_cycle = 0
        await agent.report_to_operator("x" * 5000)
        self.assertIn("truncated", self.redis.mailbox[-1]["message"])
        self.assertLess(len(self.redis.mailbox[-1]["message"]), 1700)

    async def test_run_exits_cleanly_when_the_grant_has_expired(self):
        calls = []

        async def loop(**kw):
            calls.append(kw)
            return "should not run"

        agent = self._agent(loop, expires_at="2000-01-01T00:00:00Z")
        self.assertEqual(await agent.run(), EXIT_OK)
        self.assertEqual(calls, [])
        self.assertEqual(len(self.redis.mailbox), 1)
        self.assertIn("expired", self.redis.mailbox[0]["message"])
        self.assertEqual(self.redis.hashes[ws.HEALTH_HASH]["sub-1700000000-abcdef"]["status"], "expired")

    async def test_run_loops_until_stopped_and_survives_a_failing_cycle(self):
        agent_holder = {}
        calls = []

        async def loop(**kw):
            calls.append(kw)
            if len(calls) == 1:
                raise RuntimeError("model hiccup")
            agent_holder["agent"].stop()
            return "done"

        agent = self._agent(loop, interval_seconds=1)
        agent_holder["agent"] = agent
        self.assertEqual(await agent.run(), EXIT_OK)
        self.assertEqual(len(calls), 2)
        self.assertEqual(await agent.state.last_cycle(), 2)
        outcomes = [o for _, _, _, o in await agent.state.recent_cycles(5)]
        self.assertEqual(outcomes, ["done", "failed: model hiccup"])
        self.assertIn("#1 (", calls[1]["initial_prompt"])           # the failure fed the next prompt
        self.assertIn("failed: model hiccup", calls[1]["initial_prompt"])
        self.assertEqual(self.redis.hashes[ws.HEALTH_HASH]["sub-1700000000-abcdef"]["status"], "stopped")
        self.assertGreaterEqual(self.redis.expires[ws.HEALTH_HASH], 120)

    async def test_cycle_numbering_resumes_from_state(self):
        async def loop(**kw):
            return "ok"
        agent = self._agent(loop)
        await agent.state.init()
        await agent.state.record_cycle(7, "a", "b", "old")
        agent.stop()               # run() should do its setup, then exit immediately
        await agent.run()
        self.assertEqual(agent.cycle_n, 7)


class EntrypointTests(unittest.TestCase):
    def test_load_spec_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SubagentConfigError):
                load_spec(Path(tmp))
            (Path(tmp) / "spec.json").write_text(json.dumps({"task_id": "sub-1-abcdef"}))
            with self.assertRaises(SubagentConfigError):
                load_spec(Path(tmp))
            (Path(tmp) / "spec.json").write_text(json.dumps({"task_id": "sub-1-abcdef", "intent": "x"}))
            self.assertEqual(load_spec(Path(tmp))["intent"], "x")

    def test_main_without_task_id_is_a_config_error(self):
        with mock.patch.dict(os.environ, {"SUBAGENT_TASK_ID": ""}):
            self.assertEqual(ws.main(), EXIT_CONFIG_ERROR)

    def test_main_with_missing_spec_is_a_config_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"SUBAGENT_TASK_ID": "sub-1700000000-abcdef", "SUBAGENT_STATE_DIR": tmp}):
                self.assertEqual(ws.main(), EXIT_CONFIG_ERROR)


if __name__ == "__main__":
    unittest.main()
