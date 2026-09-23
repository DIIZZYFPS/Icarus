"""Phase 5: ask_supervisor pause/resume and the operator-reply routing."""

import asyncio
import json
import unittest
from unittest import mock

from backend.agent.local_llm import AgentAbort
from backend.agent.operator_notify import NotifyResult
from backend.agent.subagent_resume import (
    clear_pause, deliver_resume, get_pause, parse_task_reference, record_pause, resolve_paused_reply,
    resume_key, wait_for_resume,
)
from backend.agent.supervision import make_ask_supervisor

TID = "sub-1700000000-abcdef"


class FakeRedis:
    def __init__(self):
        self.hashes: dict[str, dict] = {}
        self.lists: dict[str, list] = {}
        self.expires: dict[str, int] = {}

    async def hset(self, key, field=None, value=None, mapping=None):
        h = self.hashes.setdefault(key, {})
        if mapping:
            h.update({str(k): str(v) for k, v in mapping.items()})
        if field is not None:
            h[str(field)] = str(value)

    async def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    async def expire(self, key, seconds):
        self.expires[key] = seconds

    async def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)

    async def ltrim(self, key, start, end):
        self.lists[key] = self.lists.get(key, [])[start:end + 1]

    async def lrange(self, key, start, end):
        values = self.lists.get(key, [])
        return values[start:] if end == -1 else values[start:end + 1]

    async def delete(self, key):
        self.hashes.pop(key, None)
        self.lists.pop(key, None)

    async def blpop(self, key, timeout=0):
        values = self.lists.get(key)
        if values:
            return key, values.pop(0)
        await asyncio.sleep(0.01)
        return None


class ParseTests(unittest.TestCase):
    def test_reference_forms(self):
        self.assertEqual(parse_task_reference(f"{TID}: yes"), (TID, "yes"))
        self.assertEqual(parse_task_reference(f"[User:d (id: 42)]: {TID}: use Work"), (TID, "use Work"))
        self.assertEqual(parse_task_reference(f"Resume {TID} — use Work"), (TID, "use Work"))
        self.assertEqual(parse_task_reference(f"{TID.upper()}: x"), (TID, "x"))
        self.assertEqual(parse_task_reference(f"{TID}: line one\nline two"), (TID, "line one\nline two"))
        self.assertIsNone(parse_task_reference(f"hello {TID} there"))
        self.assertIsNone(parse_task_reference("just chatting"))
        self.assertIsNone(parse_task_reference(None))


class ResolveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.redis = FakeRedis()
        await record_pause(
            self.redis, task_id=TID, question="Which label?", platform="discord", chat_id="9001",
            delivery_message_ids=["m1", "m2"], timeout_seconds=60,
        )

    async def test_explicit_reference_resolves_from_any_conversation(self):
        self.assertEqual(
            await resolve_paused_reply(self.redis, platform="telegram", chat_id="5", text=f"[User:x (id: 1)]: {TID}: use Work"),
            (TID, "use Work"),
        )

    async def test_reply_to_delivery_resolves_within_the_conversation_only(self):
        self.assertEqual(
            await resolve_paused_reply(self.redis, platform="discord", chat_id="9001", text="[User:d (id: 42)]: Work, please", reply_to_message_id="m2"),
            (TID, "Work, please"),
        )
        self.assertIsNone(await resolve_paused_reply(self.redis, platform="discord", chat_id="1234", text="Work", reply_to_message_id="m2"))
        self.assertIsNone(await resolve_paused_reply(self.redis, platform="discord", chat_id="9001", text="Work", reply_to_message_id="other"))

    async def test_two_paused_tasks_resolve_independently(self):
        other = "sub-1700000001-bbbbbb"
        await record_pause(
            self.redis, task_id=other, question="Archive or trash?", platform="discord", chat_id="9001",
            delivery_message_ids=["n1"], timeout_seconds=60,
        )
        self.assertEqual((await resolve_paused_reply(self.redis, platform="discord", chat_id="9001", text="trash", reply_to_message_id="n1"))[0], other)
        self.assertEqual((await resolve_paused_reply(self.redis, platform="discord", chat_id="9001", text="Work", reply_to_message_id="m1"))[0], TID)
        self.assertEqual((await resolve_paused_reply(self.redis, platform="discord", chat_id="9001", text=f"{other}: trash"))[0], other)
        # Answering one leaves the other waiting.
        await deliver_resume(self.redis, other, "trash")
        self.assertIsNone(await get_pause(self.redis, other))
        self.assertIsNotNone(await get_pause(self.redis, TID))
        self.assertIsNone(await resolve_paused_reply(self.redis, platform="discord", chat_id="9001", text="again", reply_to_message_id="n1"))

    async def test_plain_chat_is_not_a_resume(self):
        self.assertIsNone(await resolve_paused_reply(self.redis, platform="discord", chat_id="9001", text="hey what's up"))

    async def test_unknown_task_reference_is_ignored(self):
        self.assertIsNone(await resolve_paused_reply(self.redis, platform="discord", chat_id="9001", text="sub-1700000000-ffffff: x"))

    async def test_answered_pause_is_closed(self):
        await deliver_resume(self.redis, TID, "Work", user_id="42")
        self.assertIsNone(await get_pause(self.redis, TID))
        self.assertIsNone(await resolve_paused_reply(self.redis, platform="discord", chat_id="9001", text=f"{TID}: again"))
        payload = json.loads(self.redis.lists[resume_key(TID)][0])
        self.assertEqual((payload["answer"], payload["user_id"]), ("Work", "42"))

    async def test_wait_for_resume(self):
        await deliver_resume(self.redis, TID, "Work")
        self.assertEqual((await wait_for_resume(self.redis, TID, 5))["answer"], "Work")
        self.assertIsNone(await wait_for_resume(self.redis, TID, 0))
        await clear_pause(self.redis, TID)
        self.assertIsNone(await get_pause(self.redis, TID))


class FakeRegistry:
    def __init__(self):
        self.calls = []

    async def set_status(self, task_id, status, **fields):
        self.calls.append((task_id, status, fields))
        return True


class AskSupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.redis = FakeRedis()
        self.registry = FakeRegistry()
        self.notified: list[tuple] = []

        async def no_activity(**kw):
            pass
        self._activity = mock.patch("backend.agent.activity_repo.publish_activity", no_activity)
        self._activity.start()

    async def asyncTearDown(self):
        self._activity.stop()

    def _tool(self, reply="NEED_OPERATOR", *, notify=None, timeout=1, generate_error=False, registry=True):
        async def generate(**kw):
            if generate_error:
                raise RuntimeError("llm down")
            return reply

        async def redis_getter():
            return self.redis

        def default_notify(platform, chat_id, text):
            self.notified.append((platform, chat_id, text))
            return NotifyResult(platform, chat_id, ["m9"])

        return make_ask_supervisor(
            task_id=TID, intent="Sort my invoices into labels.", platform="discord", chat_id="9001",
            redis_getter=redis_getter, notify=notify or default_notify, generate=generate,
            registry=self.registry if registry else None, timeout_seconds=timeout,
        )

    async def test_answered_from_context_without_pausing(self):
        ask = self._tool("Use the existing 'Finance' label.")
        self.assertEqual(await ask("Which label should invoices get?"), "Supervisor's answer: Use the existing 'Finance' label.")
        self.assertIsNone(await get_pause(self.redis, TID))
        self.assertEqual(self.registry.calls, [])
        self.assertEqual(self.notified, [])

    async def test_empty_question_is_rejected_cheaply(self):
        self.assertIn("specific question", await self._tool()("   "))

    async def test_pauses_notifies_and_resumes_on_the_operator_reply(self):
        ask = self._tool()
        task = asyncio.create_task(ask("Create a new label or reuse 'Finance'?"))
        for _ in range(200):
            if await get_pause(self.redis, TID):
                break
            await asyncio.sleep(0.01)
        record = await get_pause(self.redis, TID)
        self.assertIsNotNone(record)
        self.assertEqual(json.loads(record["delivery_message_ids"]), ["m9"])
        self.assertEqual(self.registry.calls[-1][1], "paused")
        self.assertEqual(self.registry.calls[-1][2]["paused_question"], "Create a new label or reuse 'Finance'?")
        platform, chat_id, text = self.notified[0]
        self.assertEqual((platform, chat_id), ("discord", "9001"))
        self.assertIn(f"[Subagent {TID} needs a decision]", text)
        self.assertIn("reuse 'Finance'", text)
        self.assertIn(f"`{TID}: <your answer>`", text)

        # The operator replies to the delivered message; L1 routes it back.
        resolved = await resolve_paused_reply(self.redis, platform="discord", chat_id="9001", text="[User:d (id: 42)]: reuse Finance", reply_to_message_id="m9")
        self.assertEqual(resolved, (TID, "reuse Finance"))
        await deliver_resume(self.redis, *resolved, user_id="42")

        self.assertEqual(await asyncio.wait_for(task, 5), "Operator's answer: reuse Finance")
        self.assertEqual(self.registry.calls[-1][1], "running")
        self.assertIsNone(await get_pause(self.redis, TID))

    async def test_timeout_aborts_the_task(self):
        ask = self._tool(timeout=1)
        with self.assertRaises(AgentAbort) as ctx:
            await ask("Which label?")
        self.assertIn("none arrived within 1s", str(ctx.exception))
        self.assertIn("Which label?", str(ctx.exception))
        self.assertIsNone(await get_pause(self.redis, TID))
        self.assertEqual([c[1] for c in self.registry.calls], ["paused"])

    async def test_notify_returning_nothing_still_allows_the_explicit_form(self):
        ask = self._tool(notify=lambda p, c, t: None, registry=False)
        task = asyncio.create_task(ask("Which label?"))
        for _ in range(200):
            if await get_pause(self.redis, TID):
                break
            await asyncio.sleep(0.01)
        record = await get_pause(self.redis, TID)
        self.assertEqual(json.loads(record["delivery_message_ids"]), [])
        self.assertIsNone(await resolve_paused_reply(self.redis, platform="discord", chat_id="9001", text="Work", reply_to_message_id="m9"))
        resolved = await resolve_paused_reply(self.redis, platform="telegram", chat_id="1", text=f"{TID}: Work")
        await deliver_resume(self.redis, *resolved)
        self.assertEqual(await asyncio.wait_for(task, 5), "Operator's answer: Work")

    async def test_consult_failure_falls_through_to_the_operator(self):
        ask = self._tool(generate_error=True, timeout=1)
        with self.assertRaises(AgentAbort):
            await ask("Which label?")
        self.assertEqual(len(self.notified), 1)


try:
    from backend.routes import webhook as _webhook
except ImportError:
    _webhook = None


@unittest.skipUnless(_webhook is not None, "fastapi not installed on the host")
class TelegramReplyParseTests(unittest.TestCase):
    def test_extracts_reply_to_message_id(self):
        self.assertEqual(_webhook._extract_reply_to({"reply_to_message": {"message_id": 77}}), "77")
        self.assertIsNone(_webhook._extract_reply_to({"text": "hi"}))


try:
    from backend.agent import processor as _processor
except ImportError:
    _processor = None


@unittest.skipUnless(_processor is not None, "L1 processor deps not installed on the host")
class ProcessorShortCircuitTests(unittest.IsolatedAsyncioTestCase):
    async def test_operator_reply_is_routed_before_the_model_runs(self):
        redis = FakeRedis()
        await record_pause(redis, task_id=TID, question="Which label?", platform="discord", chat_id="9001",
                           delivery_message_ids=["m9"], timeout_seconds=60)
        logged = []

        async def log_message(platform, user_id, role, content, conversation_id=None):
            logged.append((role, content))

        async def run_icarus(*a, **kw):
            raise AssertionError("the model must not run for a resume reply")

        with mock.patch.object(_processor, "get_redis_client", lambda: redis), \
             mock.patch("backend.agent.transcript_repo.log_message", log_message), \
             mock.patch.object(_processor, "run_icarus", run_icarus):
            reply = await _processor.process_message(
                "discord", "42", f"[User:d (id: 42)]: {TID}: reuse Finance", chat_id="9001",
            )
        self.assertIn(f"Passed your answer to subagent {TID}", reply)
        self.assertEqual(json.loads(redis.lists[resume_key(TID)][0])["answer"], "reuse Finance")
        self.assertEqual([r for r, _ in logged], ["user", "assistant"])


if __name__ == "__main__":
    unittest.main()
