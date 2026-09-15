import unittest
from unittest import mock

from backend.agent import delegation
from backend.agent.delegation import (
    CapabilitySpec,
    CapabilityUnavailable,
    CompletionEnvelope,
    DelegationError,
    DelegationRequest,
    build_delegation_request,
    is_task_id,
    looks_like_loop_failure,
    new_task_id,
    parse_delegation_request,
    resolve_tools,
    stub_request_from,
    truncate_summary,
    validate_declaration,
)


def _req(**overrides) -> DelegationRequest:
    base = dict(
        task_id=new_task_id(1700000000), intent="do a thing", capabilities=[],
        needs_network=False, needs_repo_write=False, kind="delegation",
        timestamp=1700000000, platform="discord", user_id="42", chat_id="9001",
    )
    base.update(overrides)
    return DelegationRequest(**base)


class TaskIdTests(unittest.TestCase):
    def test_minted_ids_match_the_wire_format(self):
        tid = new_task_id(1700000000)
        self.assertTrue(is_task_id(tid))
        self.assertTrue(tid.startswith("sub-1700000000-"))

    def test_rejects_foreign_shapes(self):
        for bad in ("esc-1700000000", "sub-", "sub-12-zzzzzz", 123, None, "sub-1700000000-ABCDEF"):
            self.assertFalse(is_task_id(bad), bad)


class DeclarationTests(unittest.TestCase):
    def test_unknown_capability_is_an_actionable_error(self):
        with self.assertRaises(DelegationError) as ctx:
            validate_declaration(["web", "teleport"], needs_network=True, needs_repo_write=False)
        self.assertIn("teleport", str(ctx.exception))
        self.assertIn("- web (network)", str(ctx.exception))  # lists the valid catalog

    def test_network_capability_requires_the_flag(self):
        with self.assertRaises(DelegationError) as ctx:
            validate_declaration(["web"], needs_network=False, needs_repo_write=False)
        self.assertIn("needs_network", str(ctx.exception))

    def test_non_network_capabilities_need_no_flag(self):
        self.assertEqual(
            validate_declaration(["time", "telemetry"], needs_network=False, needs_repo_write=False),
            ["time", "telemetry"],
        )

    def test_normalizes_case_dedupes_and_accepts_a_string(self):
        self.assertEqual(
            validate_declaration(["Web", "web", " TIME "], needs_network=True, needs_repo_write=False),
            ["web", "time"],
        )
        self.assertEqual(
            validate_declaration("web, time", needs_network=True, needs_repo_write=False),
            ["web", "time"],
        )

    def test_none_means_no_capabilities(self):
        self.assertEqual(validate_declaration(None, False, False), [])


class BuildRequestTests(unittest.TestCase):
    def test_builds_a_valid_delegation(self):
        req = build_delegation_request(
            intent="  summarize X ", capabilities=["web"], needs_network=True,
            platform="discord", user_id="42", chat_id="9001", timestamp=1700000000,
        )
        self.assertTrue(is_task_id(req.task_id))
        self.assertEqual(req.intent, "summarize X")
        self.assertEqual(req.kind, "delegation")
        self.assertEqual(req.thread_id, f"sub-{req.task_id}")
        self.assertEqual(req.response_type, "delegation")
        payload = req.to_payload()
        self.assertEqual(payload["type"], "delegation")
        self.assertEqual(payload["capabilities"], ["web"])
        self.assertTrue(payload["needs_network"])
        self.assertFalse(payload["needs_repo_write"])

    def test_empty_intent_is_rejected(self):
        with self.assertRaises(DelegationError):
            build_delegation_request(intent="   ", capabilities=[])

    def test_string_booleans_are_coerced(self):
        req = build_delegation_request(intent="x", capabilities=["web"], needs_network="true")
        self.assertTrue(req.needs_network)


class ParseRequestTests(unittest.TestCase):
    def test_round_trips_a_built_request(self):
        built = build_delegation_request(intent="x", capabilities=["time"], timestamp=1700000000)
        parsed = parse_delegation_request(built.to_payload())
        self.assertEqual(parsed.task_id, built.task_id)
        self.assertEqual(parsed.capabilities, ["time"])
        self.assertEqual(parsed.kind, "delegation")

    def test_legacy_escalation_is_a_repo_write_delegation(self):
        parsed = parse_delegation_request({
            "type": "escalation", "timestamp": 1700000000, "intent": "edit foo.py",
            "target_files": ["foo.py"], "platform": "telegram", "chat_id": 123,
        })
        self.assertEqual(parsed.kind, "escalation")
        self.assertTrue(parsed.needs_repo_write)
        self.assertFalse(parsed.needs_network)
        self.assertEqual(parsed.capabilities, [])
        self.assertTrue(is_task_id(parsed.task_id))          # minted, legacy carries none
        self.assertEqual(parsed.thread_id, "esc-1700000000")  # keeps L1's dispatch thread id
        self.assertEqual(parsed.response_type, "escalation")
        self.assertEqual(parsed.chat_id, "123")
        self.assertEqual(parsed.target_files, ["foo.py"])

    def test_missing_intent_is_rejected(self):
        with self.assertRaises(DelegationError):
            parse_delegation_request({"type": "delegation", "capabilities": []})

    def test_unknown_type_is_rejected(self):
        with self.assertRaises(DelegationError):
            parse_delegation_request({"type": "consultation", "intent": "x"})

    def test_invalid_task_id_is_replaced(self):
        parsed = parse_delegation_request({"type": "delegation", "intent": "x", "task_id": "bogus"})
        self.assertTrue(is_task_id(parsed.task_id))

    def test_declaration_is_revalidated_on_the_councilor_side(self):
        with self.assertRaises(DelegationError):
            parse_delegation_request({"type": "delegation", "intent": "x", "capabilities": ["gmail_read"]})

    def test_stub_request_keeps_routing_fields_for_rejections(self):
        stub = stub_request_from({"type": "escalation", "timestamp": "1700000000", "platform": "discord", "chat_id": 5})
        self.assertEqual(stub.kind, "escalation")
        self.assertTrue(stub.needs_repo_write)
        self.assertEqual(stub.chat_id, "5")
        self.assertEqual(stub.thread_id, "esc-1700000000")
        self.assertTrue(is_task_id(stub.task_id))


class ResolveToolsTests(unittest.TestCase):
    def _catalog(self, **specs):
        return mock.patch.dict(delegation.CAPABILITY_CATALOG, specs, clear=True)

    def test_grants_tools_in_declaration_order_after_extras(self):
        def alpha(): pass
        def beta(): pass
        def sandbox(): pass
        cat = {
            "a": CapabilitySpec("a", "", False, lambda req: [alpha]),
            "b": CapabilitySpec("b", "", False, lambda req: [beta]),
        }
        with self._catalog(**cat):
            resolved = resolve_tools(_req(capabilities=["b", "a"]), extra_tools=[sandbox])
        self.assertEqual([t.__name__ for t in resolved.tools], ["sandbox", "beta", "alpha"])
        self.assertEqual(resolved.granted, {"b": ["beta"], "a": ["alpha"]})
        self.assertEqual(resolved.unavailable, {})
        self.assertIn("- b: beta", resolved.describe())

    def test_dedupes_by_function_name(self):
        def alpha(): pass
        cat = {
            "a": CapabilitySpec("a", "", False, lambda req: [alpha]),
            "b": CapabilitySpec("b", "", False, lambda req: [alpha]),
        }
        with self._catalog(**cat):
            resolved = resolve_tools(_req(capabilities=["a", "b"]))
        self.assertEqual(len(resolved.tools), 1)
        self.assertEqual(resolved.granted, {"a": ["alpha"], "b": []})

    def test_missing_dependency_reports_unavailable(self):
        def broken(req):
            raise ImportError("No module named 'google'")
        def refused(req):
            raise CapabilityUnavailable("not on this host")
        cat = {
            "g": CapabilitySpec("g", "", True, broken),
            "t": CapabilitySpec("t", "", False, refused),
        }
        with self._catalog(**cat):
            resolved = resolve_tools(_req(capabilities=["g", "t"]))
        self.assertEqual(resolved.tools, [])
        self.assertIn("dependency missing", resolved.unavailable["g"])
        self.assertEqual(resolved.unavailable["t"], "not on this host")

    def test_real_catalog_time_capability_loads_anywhere(self):
        resolved = resolve_tools(_req(capabilities=["time"]))
        self.assertEqual([t.__name__ for t in resolved.tools], ["get_time"])
        self.assertIn("utc_iso", resolved.tools[0]())

    def test_loader_receives_the_request(self):
        seen = {}
        def loader(req):
            seen["user"] = req.user_id
            return []
        with self._catalog(x=CapabilitySpec("x", "", False, loader)):
            resolve_tools(_req(capabilities=["x"], user_id="u-1"))
        self.assertEqual(seen["user"], "u-1")


class EnvelopeTests(unittest.TestCase):
    def test_summary_is_capped(self):
        long = "x" * 5000
        env = CompletionEnvelope.build(task_id="sub-1-abcdef", kind="delegation", status="completed", raw_summary=long)
        self.assertLessEqual(len(env.summary), delegation.SUMMARY_MAX_CHARS + 120)
        self.assertIn("truncated", env.summary)
        self.assertEqual(truncate_summary("short"), "short")

    def test_render_includes_pr_and_error_lines(self):
        env = CompletionEnvelope.build(
            task_id="sub-1-abcdef", kind="escalation", status="completed",
            raw_summary="Changed foo.", artifacts={"pr_url": "https://gh/pr/1", "branch": None},
            elapsed_s=12.4,
        )
        text = env.render()
        self.assertIn("[escalation sub-1-abcdef] completed (12s)", text)
        self.assertIn("Changed foo.", text)
        self.assertIn("PR: https://gh/pr/1", text)
        self.assertEqual(env.artifacts, {"pr_url": "https://gh/pr/1"})  # empty values dropped

        failed = CompletionEnvelope.build(task_id="sub-1-abcdef", kind="delegation", status="failed", raw_summary="", error="boom")
        self.assertIn("Error: boom", failed.render())
        self.assertEqual(failed.to_dict()["status"], "failed")
        self.assertIsNone(failed.to_dict()["artifacts"].get("pr_url"))

    def test_loop_failure_sentinels(self):
        for bad in ("", None, "Agent loop error on turn 2: x", "(Agent loop exhausted without final response)", "(Agent produced no output)"):
            self.assertTrue(looks_like_loop_failure(bad), bad)
        self.assertFalse(looks_like_loop_failure("Summary: no errors were found."))


if __name__ == "__main__":
    unittest.main()
