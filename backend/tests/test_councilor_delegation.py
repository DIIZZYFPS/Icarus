"""
Councilor-side delegation tests. These import councilor.py, which lives at
the repo root (outside the ./backend image context), so they only run on
the host — in-container discovery skips them.

Everything with a side effect is faked: Redis (the live daemon is
subscribed to the real channel), operator notifications, activity events,
git worktrees. The agent loop itself is the real local_agent_loop driven by
a scripted fake llama-server, so the tool-dispatch path is exercised for
real — which is what the "L1 never sees raw tool output" guarantee is
about.
"""

import json
import unittest
from pathlib import Path
from unittest import mock

try:
    import councilor
except ImportError:  # in-container: councilor.py isn't shipped in the image
    councilor = None

from backend.agent import delegation
from backend.agent.delegation import CapabilitySpec, SUMMARY_MAX_CHARS
from backend.tests.fake_llm import ScriptedLlamaServer, tool_call_response, text_response, error_response


RAW_DUMP_MARKER = "RAW_TOOL_DUMP_MARKER"


class FakeRedis:
    def __init__(self):
        self.published: list[tuple[str, str]] = []
        self.mailbox: list[str] = []

    async def publish(self, channel, payload):
        self.published.append((channel, payload))
        return 0  # nobody listening directly -> mailbox fallback

    async def lpush(self, key, payload):
        assert key == "icarus:councilor:responses", key
        self.mailbox.append(payload)


@unittest.skipUnless(councilor is not None, "councilor.py only importable on the host")
class ProcessDelegationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.redis = FakeRedis()
        self.notified: list[tuple] = []
        self.activity: list[dict] = []

        async def fake_get_redis():
            return self.redis

        async def fake_publish_activity(**kw):
            self.activity.append(kw)

        self._patches = [
            mock.patch.object(councilor, "_get_redis", fake_get_redis),
            mock.patch.object(councilor, "_notify", lambda p, c, m: self.notified.append((p, c, m))),
            mock.patch("backend.agent.activity_repo.publish_activity", fake_publish_activity),
        ]
        for p in self._patches:
            p.start()
        councilor._escalation_memory.clear()

    async def asyncTearDown(self):
        for p in self._patches:
            p.stop()

    def _mailbox(self) -> list[dict]:
        return [json.loads(raw) for raw in self.redis.mailbox]

    @staticmethod
    def _fake_web_capability(dump: str):
        async def web_search(query: str, num_results: int = 5) -> str:
            """fake search"""
            return dump
        return {"web": CapabilitySpec("web", "fake web", True, lambda req: [web_search])}

    async def test_non_repo_task_delivers_only_the_short_envelope(self):
        dump = (RAW_DUMP_MARKER + " lorem ipsum ") * 400  # ~10KB of "scrape output"
        server = ScriptedLlamaServer([
            tool_call_response([("web_search", json.dumps({"query": "icarus"}))]),
            text_response("Icarus is a Greek myth about flying too close to the sun."),
        ])
        data = delegation.build_delegation_request(
            intent="look up Icarus and summarize", capabilities=["web"], needs_network=True,
            platform="discord", user_id="42", chat_id="9001", timestamp=1700000000,
        ).to_payload()

        with mock.patch.dict(delegation.CAPABILITY_CATALOG, self._fake_web_capability(dump), clear=False), server.patched():
            await councilor.process_delegation(data)

        # The subagent really did receive the dump inside its own loop...
        self.assertEqual(len(server.requests), 2)
        self.assertEqual(server.tool_names_offered(0), ["web_search"])
        self.assertTrue(any(RAW_DUMP_MARKER in m for m in server.tool_messages(1)))

        # ...and L1's mailbox only ever gets the envelope.
        mailbox = self._mailbox()
        self.assertEqual(len(mailbox), 1)
        msg = mailbox[0]
        self.assertEqual(msg["type"], "delegation")
        self.assertEqual(msg["task_id"], data["task_id"])
        self.assertEqual(msg["platform"], "discord")
        self.assertEqual(msg["chat_id"], "9001")
        self.assertNotIn(RAW_DUMP_MARKER, msg["message"])
        self.assertIn("Greek myth", msg["message"])
        self.assertLess(len(msg["message"]), SUMMARY_MAX_CHARS + 200)
        self.assertEqual(msg["envelope"]["status"], "completed")
        self.assertEqual(msg["envelope"]["kind"], "delegation")
        self.assertNotIn(RAW_DUMP_MARKER, json.dumps(msg["envelope"]))

        # Operator ping + activity thread use the task id.
        self.assertEqual(self.notified[0][0], "discord")
        self.assertIn("[Icarus Task Complete]", self.notified[0][2])
        self.assertEqual({a["event_type"] for a in self.activity}, {"received", "responded"})
        self.assertTrue(all(a["thread_id"] == f"sub-{data['task_id']}" for a in self.activity))

        # The system prompt told the subagent exactly what it was granted.
        system_msg = server.requests[0]["messages"][0]
        self.assertEqual(system_msg["role"], "system")
        self.assertIn("- web: web_search", system_msg["content"])
        self.assertIn("delegated subagent", system_msg["content"])

    async def test_rejected_declaration_is_answered_not_dropped(self):
        data = {
            "type": "delegation", "intent": "read my mail", "capabilities": ["gmail_read"],
            "needs_network": False, "timestamp": 1700000000, "platform": "telegram", "chat_id": "77",
        }
        server = ScriptedLlamaServer([])
        with server.patched():
            await councilor.process_delegation(data)

        self.assertEqual(server.requests, [])  # never reached the model
        msg = self._mailbox()[0]
        self.assertEqual(msg["envelope"]["status"], "failed")
        self.assertIn("needs_network", msg["envelope"]["error"])
        self.assertEqual(msg["chat_id"], "77")
        self.assertIn("Rejected", self.notified[0][2])

    async def test_unavailable_capability_fails_fast_with_the_reason(self):
        def broken(req):
            raise ImportError("No module named 'google'")
        data = delegation.build_delegation_request(
            intent="x", capabilities=["gmail_read"], needs_network=True, timestamp=1700000000,
        ).to_payload()
        server = ScriptedLlamaServer([])
        cat = {"gmail_read": CapabilitySpec("gmail_read", "", True, broken)}
        with mock.patch.dict(delegation.CAPABILITY_CATALOG, cat, clear=False), server.patched():
            await councilor.process_delegation(data)

        self.assertEqual(server.requests, [])
        env = self._mailbox()[0]["envelope"]
        self.assertEqual(env["status"], "failed")
        self.assertIn("gmail_read", env["error"])
        self.assertIn("dependency missing", env["error"])
        self.assertEqual([a["event_type"] for a in self.activity], ["received", "failed"])

    async def test_legacy_escalation_uses_the_sandbox_and_lands_a_pr(self):
        cleaned = []
        with mock.patch.object(councilor, "_create_escalation_worktree", lambda ts: (Path("/tmp/fake-wt"), "councilor/intent-1700000000-abc123")), \
             mock.patch.object(councilor, "_finalize_worktree", lambda path, branch, intent: "https://github.com/x/y/pull/9"), \
             mock.patch.object(councilor, "_cleanup_worktree", lambda path: cleaned.append(path)):
            server = ScriptedLlamaServer([
                tool_call_response([("list_directory", json.dumps({"directory": "."}))]),
                text_response("Updated backend/foo.py to add the route."),
            ])
            with server.patched():
                await councilor.process_escalation({
                    "type": "escalation", "timestamp": 1700000000, "platform": "discord",
                    "chat_id": "9001", "intent": "add a /healthz route", "target_files": ["backend/main.py"],
                })

        self.assertEqual(
            server.tool_names_offered(0), ["read_file", "write_file", "list_directory", "run_command"],
        )
        self.assertIn("isolated git worktree", server.requests[0]["messages"][0]["content"])
        msg = self._mailbox()[0]
        self.assertEqual(msg["type"], "escalation")   # heartbeat prefix unchanged for legacy
        self.assertIn("PR: https://github.com/x/y/pull/9", msg["message"])
        self.assertIn("Updated backend/foo.py", msg["message"])
        self.assertEqual(msg["envelope"]["artifacts"], {"pr_url": "https://github.com/x/y/pull/9", "branch": "councilor/intent-1700000000-abc123"})
        self.assertEqual(cleaned, [Path("/tmp/fake-wt")])
        self.assertTrue(all(a["thread_id"] == "esc-1700000000" for a in self.activity))
        self.assertIn("[Icarus Escalation Complete]", self.notified[0][2])
        self.assertEqual(self.activity[-1]["action"], "responded — patch applied")

    async def test_worktree_failure_is_reported(self):
        with mock.patch.object(councilor, "_create_escalation_worktree", lambda ts: None):
            await councilor.process_escalation({"type": "escalation", "timestamp": 1, "intent": "x"})
        env = self._mailbox()[0]["envelope"]
        self.assertEqual(env["status"], "failed")
        self.assertIn("worktree", env["error"])

    async def test_long_final_summary_is_truncated_for_l1(self):
        server = ScriptedLlamaServer([text_response("word " * 2000)])
        data = delegation.build_delegation_request(intent="x", capabilities=["time"], timestamp=1700000000).to_payload()
        with server.patched():
            await councilor.process_delegation(data)
        msg = self._mailbox()[0]
        self.assertIn("truncated", msg["message"])
        self.assertLess(len(msg["message"]), SUMMARY_MAX_CHARS + 250)

    async def test_transport_failure_retries_then_fails_cleanly(self):
        server = ScriptedLlamaServer([error_response(), error_response(), error_response()])
        data = delegation.build_delegation_request(intent="x", capabilities=[], timestamp=1700000000).to_payload()
        with server.patched():
            await councilor.process_delegation(data)
        self.assertEqual(len(server.requests), 1 + councilor.MAX_DELEGATION_RETRIES)
        env = self._mailbox()[0]["envelope"]
        self.assertEqual(env["status"], "failed")
        self.assertIn("Agent loop error", env["error"])
        self.assertIn("Failed", self.notified[0][2])

    async def test_word_error_in_a_web_summary_does_not_trigger_a_retry(self):
        server = ScriptedLlamaServer([text_response("The page reports an error rate of 2%.")])
        data = delegation.build_delegation_request(intent="x", capabilities=[], timestamp=1700000000).to_payload()
        with server.patched():
            await councilor.process_delegation(data)
        self.assertEqual(len(server.requests), 1)
        self.assertEqual(self._mailbox()[0]["envelope"]["status"], "completed")

    # ── Phase 4: step-level supervision inside a delegated task ───────────

    async def test_supervisor_repairs_a_malformed_tool_call_inside_a_delegated_task(self):
        seen = []

        async def web_search(query: str, num_results: int = 5) -> str:
            """fake"""
            seen.append(query)
            return "RESULTS"

        server = ScriptedLlamaServer([
            tool_call_response([("web_search", '{"query": "bwrap",}')]),   # trailing comma: not JSON
            text_response("bwrap is a sandboxing tool."),
        ])
        data = delegation.build_delegation_request(
            intent="what is bwrap", capabilities=["web"], needs_network=True, timestamp=1700000000,
        ).to_payload()
        cat = {"web": CapabilitySpec("web", "", True, lambda req: [web_search])}
        with mock.patch.dict(delegation.CAPABILITY_CATALOG, cat, clear=False), server.patched():
            await councilor.process_delegation(data)

        self.assertEqual(seen, ["bwrap"])   # repaired and executed, not run with {}
        msg = self._mailbox()[0]
        self.assertEqual(msg["envelope"]["status"], "completed")
        self.assertIn("sandboxing tool", msg["message"])
        self.assertEqual(len(server.requests), 2)   # no model round-trip was spent on the repair

    async def test_supervisor_give_up_fails_the_task_without_retries(self):
        from backend.agent.supervision import Supervisor

        async def gmail_list_messages(query: str = "is:unread") -> str:
            """fake"""
            raise RuntimeError("invalid_grant: refresh token revoked")

        async def verdict(**kw):
            return '{"action": "give_up", "reason": "Gmail credentials are revoked; the operator must re-authorize"}'

        server = ScriptedLlamaServer([
            tool_call_response([("gmail_list_messages", '{"query": "invoices"}')]),
            text_response("unreachable"),
        ])
        data = delegation.build_delegation_request(
            intent="find invoices", capabilities=["gmail_read"], needs_network=True, timestamp=1700000000,
        ).to_payload()
        cat = {"gmail_read": CapabilitySpec("gmail_read", "", True, lambda req: [gmail_list_messages])}
        with mock.patch.dict(delegation.CAPABILITY_CATALOG, cat, clear=False), server.patched(), \
             mock.patch.object(councilor, "_make_supervisor",
                               lambda req: Supervisor(req.task_id, req.intent, generate=verdict, failure_threshold=1)):
            await councilor.process_delegation(data)

        self.assertEqual(len(server.requests), 1)   # aborted: no retries, no further turns
        msg = self._mailbox()[0]
        self.assertEqual(msg["envelope"]["status"], "failed")
        self.assertIn("Agent aborted", msg["envelope"]["error"])
        self.assertIn("re-authorize", msg["envelope"]["error"])
        self.assertIn("Failed", self.notified[0][2])

    async def test_l1_loop_gets_no_supervisor(self):
        # The §2 boundary, checked structurally: nothing in L1's engine ever
        # mentions a supervisor. Read as text — engine.py imports the Google
        # tool modules, which the Councilor's host venv doesn't have.
        engine_source = (Path(__file__).resolve().parents[1] / "agent" / "engine.py").read_text(encoding="utf-8")
        self.assertNotIn("supervisor", engine_source)


if __name__ == "__main__":
    unittest.main()
