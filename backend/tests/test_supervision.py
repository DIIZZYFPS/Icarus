"""Phase 4: step-level supervision — the policy, and the hook inside the real loop."""

import json
import unittest

from backend.agent.local_llm import AgentAbort, local_agent_loop, _tool_schema
from backend.agent.supervision import Supervisor, closest_tool, parse_verdict, repair_json_args
from backend.tests.fake_llm import ScriptedLlamaServer, text_response, tool_call_response


async def _web_search(query: str, num_results: int = 5) -> str:
    """fake search"""
    return f"results for {query}"


_web_search.__name__ = "web_search"
SCHEMA = _tool_schema(_web_search)


class RepairTests(unittest.TestCase):
    def test_fences_and_trailing_commas(self):
        self.assertEqual(repair_json_args('```json\n{"query": "x",}\n```', SCHEMA), {"query": "x"})

    def test_python_literals(self):
        self.assertEqual(repair_json_args("{'query': 'x', 'num_results': 3}", SCHEMA), {"query": "x", "num_results": 3})

    def test_prose_around_the_object(self):
        self.assertEqual(repair_json_args('Here you go: {"query": "x"} hope that helps', SCHEMA), {"query": "x"})

    def test_smart_quotes(self):
        self.assertEqual(repair_json_args("{“query”: “x”}", SCHEMA), {"query": "x"})

    def test_rejects_wrong_shape_or_keys(self):
        self.assertIsNone(repair_json_args("[1, 2]", SCHEMA))
        self.assertIsNone(repair_json_args('{"qeury": "x"}', SCHEMA))        # unknown key
        self.assertIsNone(repair_json_args('{"num_results": 3}', SCHEMA))    # required key missing
        self.assertIsNone(repair_json_args("total garbage", SCHEMA))
        self.assertIsNone(repair_json_args(None, SCHEMA))

    def test_without_a_schema_any_object_passes(self):
        self.assertEqual(repair_json_args('{"anything": 1,}', None), {"anything": 1})


class ClosestToolTests(unittest.TestCase):
    def test_case_insensitive_exact_and_fuzzy(self):
        self.assertEqual(closest_tool("Web_Search", ["web_search", "web_extract"]), "web_search")
        self.assertEqual(closest_tool("web_serch", ["web_search", "web_extract"]), "web_search")
        self.assertIsNone(closest_tool("send_rocket", ["web_search", "web_extract"]))
        self.assertIsNone(closest_tool("", ["web_search"]))


class ParseVerdictTests(unittest.TestCase):
    def test_accepts_fenced_json_and_validates_action(self):
        v = parse_verdict('```json\n{"action": "repair", "args": {"query": "x"}}\n```', {"repair"})
        self.assertEqual(v, {"action": "repair", "args": {"query": "x"}})
        self.assertIsNone(parse_verdict('{"action": "retry"}', {"repair"}))
        self.assertIsNone(parse_verdict('{"action": "repair"}', {"repair"}))
        self.assertIsNone(parse_verdict("no json here", {"repair"}))

    def test_redirect_must_name_an_available_tool(self):
        self.assertIsNone(parse_verdict('{"action": "redirect", "tool": "nope"}', {"redirect"}, ["web_search"]))
        v = parse_verdict('{"action": "redirect", "tool": "web_search"}', {"redirect"}, ["web_search"])
        self.assertEqual(v["args"], {})

    def test_give_up_gets_a_default_reason(self):
        self.assertEqual(parse_verdict('{"action": "give_up"}', {"give_up"})["reason"], "supervisor gave up")


class SupervisorPolicyTests(unittest.IsolatedAsyncioTestCase):
    def _sup(self, replies=None, **kw):
        replies = list(replies or [])
        calls = []

        async def generate(**kwargs):
            calls.append(kwargs)
            return replies.pop(0) if replies else ""

        return Supervisor("sub-1-abcdef", "find X", generate=generate, **kw), calls

    async def test_malformed_is_repaired_without_a_consult(self):
        sup, calls = self._sup()
        v = await sup({"kind": "malformed_args", "tool": "web_search", "raw_arguments": '{"query": "x",}',
                       "schema": SCHEMA, "available_tools": ["web_search"]})
        self.assertEqual((v["action"], v["args"]), ("repair", {"query": "x"}))
        self.assertEqual(calls, [])
        self.assertEqual(sup.consults, 0)

    async def test_unrepairable_malformed_goes_to_the_model(self):
        sup, calls = self._sup(['{"action": "repair", "args": {"query": "icarus"}}'])
        v = await sup({"kind": "malformed_args", "tool": "web_search", "raw_arguments": "search for icarus pls",
                       "schema": SCHEMA, "available_tools": ["web_search"]})
        self.assertEqual(v["args"], {"query": "icarus"})
        self.assertEqual(v["source"], "consult")
        self.assertEqual(calls[0]["task_type"], "supervision")
        self.assertIn("search for icarus pls", calls[0]["messages"][0]["text"])
        self.assertIn("find X", calls[0]["messages"][0]["text"])

        sup, _ = self._sup(["I think you should..."])
        self.assertIsNone(await sup({"kind": "malformed_args", "tool": "web_search", "raw_arguments": "??",
                                     "schema": SCHEMA, "available_tools": ["web_search"]}))

    async def test_unknown_tool_near_miss_redirects_locally(self):
        sup, calls = self._sup()
        v = await sup({"kind": "unknown_tool", "tool": "web_serch", "args": {"query": "x"},
                       "available_tools": ["web_search", "web_extract"]})
        self.assertEqual((v["action"], v["tool"], v["args"]), ("redirect", "web_search", {"query": "x"}))
        self.assertEqual(calls, [])

    async def test_unknown_tool_far_miss_consults_and_validates(self):
        sup, calls = self._sup(['{"action": "redirect", "tool": "web_extract", "args": {"urls": ["u"]}}'])
        v = await sup({"kind": "unknown_tool", "tool": "fetch_page", "args": {}, "available_tools": ["web_search", "web_extract"]})
        self.assertEqual(v["tool"], "web_extract")
        self.assertEqual(len(calls), 1)
        sup, _ = self._sup(['{"action": "redirect", "tool": "rm_rf", "args": {}}'])
        self.assertIsNone(await sup({"kind": "unknown_tool", "tool": "fetch_page", "args": {}, "available_tools": ["web_search"]}))

    async def test_exec_error_respects_the_threshold(self):
        sup, calls = self._sup(['{"action": "retry", "args": {"query": "x2"}}'], failure_threshold=2)
        base = {"kind": "exec_error", "tool": "web_search", "args": {"query": "x"}, "error": "boom", "available_tools": ["web_search"]}
        self.assertIsNone(await sup({**base, "consecutive_failures": 1}))
        self.assertEqual(calls, [])
        second = await sup({**base, "consecutive_failures": 2})
        self.assertEqual((second["action"], second["args"]), ("retry", {"query": "x2"}))
        self.assertEqual(len(calls), 1)

    async def test_consult_budget(self):
        sup, calls = self._sup(['{"action": "give_up", "reason": "no creds"}', "unused"], max_consults=1)
        ev = {"kind": "malformed_args", "tool": "t", "raw_arguments": "??", "schema": None, "available_tools": ["t"]}
        self.assertEqual((await sup(ev))["action"], "give_up")
        self.assertIsNone(await sup(ev))
        self.assertEqual(len(calls), 1)

    async def test_hook_exceptions_are_swallowed(self):
        async def generate(**kw):
            raise RuntimeError("llm down")
        sup = Supervisor("sub-1-abcdef", "x", generate=generate)
        self.assertIsNone(await sup({"kind": "malformed_args", "tool": "t", "raw_arguments": "??", "available_tools": ["t"]}))
        self.assertIsNone(await sup({"kind": "weird"}))
        self.assertEqual(len(sup.decisions), 2)


class LoopIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """The real local_agent_loop, a scripted model, and a real Supervisor."""

    def _sup(self, replies=None, **kw):
        replies = list(replies or [])

        async def generate(**kwargs):
            return replies.pop(0) if replies else ""

        return Supervisor("sub-1-abcdef", "find X", generate=generate, **kw)

    async def test_malformed_arguments_are_repaired_before_execution(self):
        seen = []

        async def web_search(query: str, num_results: int = 5) -> str:
            """fake"""
            seen.append((query, num_results))
            return "RESULTS"

        server = ScriptedLlamaServer([
            tool_call_response([("web_search", '```json\n{"query": "icarus",}\n```')]),
            text_response("done"),
        ])
        sup = self._sup()
        with server.patched():
            out = await local_agent_loop("go", [web_search], supervisor=sup)
        self.assertEqual(out, "done")
        self.assertEqual(seen, [("icarus", 5)])
        self.assertEqual(sup.consults, 0)
        self.assertEqual(server.tool_messages(1), ["RESULTS"])

    async def test_without_a_supervisor_behaviour_is_unchanged(self):
        seen = []

        async def web_search(query: str = "", num_results: int = 5) -> str:
            """fake"""
            seen.append(query)
            return "R"

        server = ScriptedLlamaServer([
            tool_call_response([("web_search", "not json"), ("nope", "{}")]),
            text_response("done"),
        ])
        with server.patched():
            await local_agent_loop("go", [web_search])
        self.assertEqual(seen, [""])
        self.assertIn('"error": "Unknown tool: nope"', server.tool_messages(1)[1])

    async def test_unknown_tool_is_redirected(self):
        seen = []

        async def web_search(query: str) -> str:
            """fake"""
            seen.append(query)
            return "R"

        server = ScriptedLlamaServer([tool_call_response([("web_serch", '{"query": "q"}')]), text_response("done")])
        with server.patched():
            await local_agent_loop("go", [web_search], supervisor=self._sup())
        self.assertEqual(seen, ["q"])
        self.assertEqual(server.tool_messages(1), ["R"])

    async def test_exec_error_consult_after_threshold_then_retry_succeeds(self):
        attempts = []

        async def flaky(query: str) -> str:
            """fake"""
            attempts.append(query)
            if len(attempts) < 3:
                raise RuntimeError("transient")
            return "OK"

        # Turn 1: one failure (below threshold -> error text goes to the model).
        # Turn 2: the model retries, fails again -> threshold -> consult -> retry with fixed args -> OK.
        server = ScriptedLlamaServer([
            tool_call_response([("flaky", '{"query": "a"}')]),
            tool_call_response([("flaky", '{"query": "a"}')]),
            text_response("done"),
        ])
        sup = self._sup(['{"action": "retry", "args": {"query": "b"}}'], failure_threshold=2)
        with server.patched():
            out = await local_agent_loop("go", [flaky], supervisor=sup)
        self.assertEqual(out, "done")
        self.assertEqual(attempts, ["a", "a", "b"])
        self.assertEqual(sup.consults, 1)
        self.assertIn('"error": "transient"', server.tool_messages(1)[0])
        self.assertEqual(server.tool_messages(2)[-1], "OK")

    async def test_note_verdict_annotates_the_error(self):
        async def broken(query: str) -> str:
            """fake"""
            raise RuntimeError("401 unauthorized")

        server = ScriptedLlamaServer([tool_call_response([("broken", '{"query": "a"}')]), text_response("gave up politely")])
        sup = self._sup(['{"action": "note", "note": "credentials are missing; say so in your summary"}'], failure_threshold=1)
        with server.patched():
            await local_agent_loop("go", [broken], supervisor=sup)
        msg = json.loads(server.tool_messages(1)[0])
        self.assertEqual(msg["error"], "401 unauthorized")
        self.assertIn("credentials", msg["supervisor_note"])

    async def test_give_up_aborts_the_loop(self):
        async def broken(query: str) -> str:
            """fake"""
            raise RuntimeError("no creds")

        server = ScriptedLlamaServer([tool_call_response([("broken", '{"query": "a"}')]), text_response("unreachable")])
        sup = self._sup(['{"action": "give_up", "reason": "Gmail credentials are not configured"}'], failure_threshold=1)
        with server.patched():
            with self.assertRaises(AgentAbort) as ctx:
                await local_agent_loop("go", [broken], supervisor=sup)
        self.assertIn("Gmail credentials", str(ctx.exception))
        self.assertEqual(len(server.requests), 1)

    async def test_tool_raised_abort_propagates_untouched(self):
        async def pause(question: str) -> str:
            """fake"""
            raise AgentAbort("paused too long")

        server = ScriptedLlamaServer([tool_call_response([("pause", '{"question": "?"}')])])
        with server.patched():
            with self.assertRaises(AgentAbort):
                await local_agent_loop("go", [pause], supervisor=self._sup())

    async def test_supervisor_crash_never_breaks_the_loop(self):
        async def hook(event):
            raise RuntimeError("hook bug")

        async def t(query: str = "") -> str:
            """fake"""
            return "R"

        server = ScriptedLlamaServer([tool_call_response([("t", "garbage")]), text_response("done")])
        with server.patched():
            self.assertEqual(await local_agent_loop("go", [t], supervisor=hook), "done")

    async def test_consult_goes_through_the_router_with_thinking_off(self):
        """No fake generate: the Supervisor's consult is a real
        llm_router.generate(task_type="supervision") call, answered by the
        same scripted server between the subagent's own turns."""
        from backend.agent.supervision import SUPERVISOR_SYSTEM_PROMPT

        async def broken(query: str) -> str:
            """fake"""
            raise RuntimeError("timeout talking to upstream")

        server = ScriptedLlamaServer([
            tool_call_response([("broken", '{"query": "a"}')]),                          # subagent turn 1
            text_response('{"action": "note", "note": "upstream is flaky; try once more then report"}'),  # the consult
            text_response("reported"),                                                     # subagent turn 2
        ])
        sup = Supervisor("sub-1-abcdef", "find X", failure_threshold=1)
        with server.patched():
            out = await local_agent_loop("go", [broken], supervisor=sup)
        self.assertEqual(out, "reported")
        consult = server.requests[1]
        self.assertEqual(consult["messages"][0]["role"], "system")
        self.assertEqual(consult["messages"][0]["content"], SUPERVISOR_SYSTEM_PROMPT)
        self.assertNotIn("tools", consult)
        self.assertFalse(consult["chat_template_kwargs"]["enable_thinking"])
        self.assertIn("upstream is flaky", json.loads(server.tool_messages(2)[0])["supervisor_note"])


if __name__ == "__main__":
    unittest.main()
