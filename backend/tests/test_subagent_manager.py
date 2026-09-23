import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.agent.delegation import DelegationError
from backend.agent.docker_runtime import (
    CommandResult, ContainerSpec, ContainerState, DockerError, DockerRuntime, SUBAGENT_LABEL, _default_runner,
)
from backend.agent.subagent_manager import (
    EXIT_CONFIG_ERROR, DEFAULT_INTERVAL_SECONDS, SubagentLimitError, SubagentManager, SubagentNotFound,
    clamp_interval, container_name_for,
)
from backend.agent.subagent_registry import (
    DESIRED_RUNNING, DESIRED_STOPPED, KIND_ONESHOT, KIND_PERSISTENT, STATUS_FAILED, STATUS_RUNNING, STATUS_STOPPED,
    SubagentRegistry,
)


class FakeDockerRuntime:
    """In-memory stand-in with DockerRuntime's async surface."""

    def __init__(self):
        self.containers: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.available_flag = True
        self.fail_next_run: str | None = None
        self._seq = 0

    async def available(self):
        return self.available_flag

    async def run_detached(self, spec: ContainerSpec) -> str:
        self.calls.append(("run", spec.name))
        if self.fail_next_run:
            msg, self.fail_next_run = self.fail_next_run, None
            raise DockerError(msg)
        if spec.name in self.containers:
            raise DockerError(f'Conflict. The container name "/{spec.name}" is already in use')
        self._seq += 1
        cid = f"cid{self._seq:04d}" + "0" * 56
        self.containers[spec.name] = {"id": cid, "running": True, "exit_code": 0, "spec": spec}
        return cid

    async def inspect(self, name: str):
        self.calls.append(("inspect", name))
        c = self.containers.get(name)
        if c is None:
            return None
        return ContainerState(
            id=c["id"], name=name, status="running" if c["running"] else "exited",
            running=c["running"], exit_code=c["exit_code"],
        )

    async def start(self, name: str):
        self.calls.append(("start", name))
        self.containers[name]["running"] = True
        self.containers[name]["exit_code"] = 0

    async def stop(self, name: str, timeout_s: int = 10):
        self.containers[name]["running"] = False

    async def remove(self, name: str, force: bool = True) -> bool:
        self.calls.append(("rm", name))
        return self.containers.pop(name, None) is not None

    async def list_subagent_containers(self):
        return [
            {"id": c["id"], "name": n, "state": "running" if c["running"] else "exited",
             "task_id": c["spec"].labels.get(SUBAGENT_LABEL, "")}
            for n, c in self.containers.items()
        ]

    # test helpers
    def crash(self, name: str):
        self.containers.pop(name)

    def exit(self, name: str, code: int):
        self.containers[name]["running"] = False
        self.containers[name]["exit_code"] = code

    def add_orphan(self, name: str, task_id: str):
        spec = ContainerSpec(name=name, image="x", command=[], labels={SUBAGENT_LABEL: task_id})
        self.containers[name] = {"id": "orphan", "running": True, "exit_code": 0, "spec": spec}


class ManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "backend").mkdir()
        (self.root / "workspace" / "projects").mkdir(parents=True)
        (self.root / ".env").write_text("X=1\n")
        self.registry = SubagentRegistry(self.root / "workspace" / "memory" / "councilor.db")
        await self.registry.init()
        self.runtime = FakeDockerRuntime()
        self.manager = SubagentManager(self.registry, self.runtime, self.root, max_persistent=2)

    async def asyncTearDown(self):
        await self.registry.close()
        self.tmp.cleanup()

    async def _create(self, **over):
        base = dict(intent="watch inbox", capabilities=["time"], needs_network=False, interval_seconds=300,
                    platform="discord", chat_id="9001", user_id="42")
        base.update(over)
        return await self.manager.create(**base)

    async def test_create_writes_spec_registry_and_runs_container(self):
        row = await self._create()
        tid = row["id"]
        self.assertEqual(row["kind"], KIND_PERSISTENT)
        self.assertEqual(row["status"], STATUS_RUNNING)
        self.assertEqual(row["desired_state"], DESIRED_RUNNING)
        self.assertEqual(row["container_name"], container_name_for(tid))
        self.assertTrue(row["container_id"].startswith("cid"))
        self.assertEqual(row["interval_seconds"], 300)
        self.assertIsNotNone(row["expires_at"])
        self.assertEqual(row["state_path"], f"/workspace/memory/subagents/{tid}")
        self.assertEqual(row["thread_id"], f"sub-{tid}")
        self.assertEqual(row["capability_scope"], {"capabilities": ["time"], "needs_network": False, "needs_repo_write": False})

        spec_path = self.root / "workspace" / "memory" / "subagents" / tid / "spec.json"
        spec = json.loads(spec_path.read_text())
        self.assertEqual(spec["intent"], "watch inbox")
        self.assertEqual(spec["capabilities"], ["time"])
        self.assertEqual(spec["interval_seconds"], 300)
        self.assertEqual(spec["task_id"], tid)
        self.assertEqual(spec["chat_id"], "9001")

        cspec = self.runtime.containers[row["container_name"]]["spec"]
        self.assertEqual(cspec.command, ["python", "-m", "backend.agent.worker_subagent"])
        self.assertEqual(cspec.image, "icarus-worker")
        self.assertEqual(cspec.env["SUBAGENT_TASK_ID"], tid)
        self.assertIn("REDIS_URL", cspec.env)
        self.assertIn("LOCAL_LLM_URL", cspec.env)
        self.assertTrue(cspec.env_file.endswith(".env"))
        self.assertIn((str(self.root / "backend"), "/app/backend", "ro"), cspec.volumes)
        self.assertIn((str(self.root / "workspace" / "memory"), "/workspace/memory", "rw"), cspec.volumes)
        self.assertEqual(cspec.labels[SUBAGENT_LABEL], tid)
        self.assertEqual(cspec.restart, "on-failure:5")
        self.assertEqual(cspec.network, "host")
        self.assertEqual(cspec.user, "1000")
        argv = cspec.to_run_argv()
        self.assertEqual(argv[:3], ["docker", "run", "-d"])
        self.assertEqual(argv[-3:], ["python", "-m", "backend.agent.worker_subagent"])

    async def test_cap_is_enforced_and_freed_by_stop(self):
        a = await self._create()
        await self._create()
        with self.assertRaises(SubagentLimitError) as ctx:
            await self._create()
        self.assertIn("cap is 2", str(ctx.exception))
        await self.manager.stop(a["id"])
        await self._create()
        self.assertEqual(await self.manager.active_count(), 2)

    async def test_declaration_is_validated_before_anything_is_created(self):
        with self.assertRaises(DelegationError):
            await self._create(capabilities=["web"], needs_network=False)
        with self.assertRaises(DelegationError):
            await self._create(intent="   ")
        self.assertEqual(self.runtime.containers, {})
        self.assertEqual(await self.registry.list(), [])

    async def test_interval_and_ttl_bounds(self):
        row = await self._create(interval_seconds=5)
        self.assertEqual(row["interval_seconds"], 60)
        row2 = await self._create(interval_seconds=None, ttl_hours=0)
        self.assertEqual(row2["interval_seconds"], DEFAULT_INTERVAL_SECONDS)
        self.assertIsNone(row2["expires_at"])
        self.assertEqual(clamp_interval("abc"), DEFAULT_INTERVAL_SECONDS)
        self.assertEqual(clamp_interval(10 ** 9), 86400)

    async def test_docker_failure_marks_the_row_failed(self):
        self.runtime.fail_next_run = "docker run failed: No such image: icarus-worker"
        with self.assertRaises(DockerError):
            await self._create()
        rows = await self.registry.list(kind=KIND_PERSISTENT)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], STATUS_FAILED)
        self.assertEqual(rows[0]["desired_state"], DESIRED_STOPPED)
        self.assertIn("No such image", rows[0]["last_error"])
        self.assertEqual(await self.manager.active_count(), 0)

    async def test_stop_removes_container_and_records_reason(self):
        row = await self._create()
        name = row["container_name"]
        stopped = await self.manager.stop(row["id"], reason="test")
        self.assertNotIn(name, self.runtime.containers)
        self.assertEqual(stopped["status"], STATUS_STOPPED)
        self.assertEqual(stopped["desired_state"], DESIRED_STOPPED)
        self.assertEqual(stopped["result"]["stopped_reason"], "test")
        self.assertTrue(stopped["result"]["container_removed"])
        again = await self.manager.stop(row["id"])
        self.assertFalse(again["result"]["container_removed"])
        with self.assertRaises(SubagentNotFound):
            await self.manager.stop("sub-1700000000-ffffff")
        await self.registry.create(
            task_id="sub-1700000000-aaaaaa", kind=KIND_ONESHOT, request_type="delegation", intent="x",
            capabilities=[], needs_network=False, needs_repo_write=False,
        )
        with self.assertRaises(SubagentNotFound):
            await self.manager.stop("sub-1700000000-aaaaaa")

    async def test_reconcile_healthy_is_a_noop(self):
        row = await self._create()
        self.runtime.calls.clear()
        report = await self.manager.reconcile()
        self.assertEqual(report.healthy, [row["id"]])
        self.assertEqual([c for c in self.runtime.calls if c[0] != "inspect"], [])
        self.assertIn("healthy=1", report.summary())

    async def test_reconcile_respawns_a_missing_container_without_duplicating(self):
        row = await self._create()
        old_id = row["container_id"]
        self.runtime.crash(row["container_name"])
        report = await self.manager.reconcile()
        self.assertEqual(report.respawned, [row["id"]])
        fresh = await self.registry.get(row["id"])
        self.assertEqual(fresh["restart_count"], 1)
        self.assertNotEqual(fresh["container_id"], old_id)
        self.assertEqual(fresh["status"], STATUS_RUNNING)
        self.assertTrue(self.runtime.containers[row["container_name"]]["running"])
        report2 = await self.manager.reconcile()
        self.assertEqual(report2.healthy, [row["id"]])
        self.assertEqual(report2.respawned, [])
        self.assertEqual(len(self.runtime.containers), 1)

    async def test_reconcile_starts_an_exited_container(self):
        row = await self._create()
        self.runtime.exit(row["container_name"], 1)
        report = await self.manager.reconcile()
        self.assertEqual(report.started, [row["id"]])
        self.assertTrue(self.runtime.containers[row["container_name"]]["running"])
        self.assertEqual((await self.registry.get(row["id"]))["restart_count"], 0)

    async def test_reconcile_retires_config_error_exits(self):
        row = await self._create()
        self.runtime.exit(row["container_name"], EXIT_CONFIG_ERROR)
        report = await self.manager.reconcile()
        self.assertEqual(report.config_failed, [row["id"]])
        self.assertNotIn(row["container_name"], self.runtime.containers)
        fresh = await self.registry.get(row["id"])
        self.assertEqual(fresh["status"], STATUS_FAILED)
        self.assertEqual(fresh["desired_state"], DESIRED_STOPPED)
        self.assertIn("configuration error", fresh["last_error"])

    async def test_reconcile_expires_grants(self):
        row = await self._create()
        await self.registry.update(row["id"], expires_at="2000-01-01T00:00:00Z")
        report = await self.manager.reconcile()
        self.assertEqual(report.expired, [row["id"]])
        self.assertNotIn(row["container_name"], self.runtime.containers)
        fresh = await self.registry.get(row["id"])
        self.assertEqual(fresh["status"], STATUS_STOPPED)
        self.assertEqual(fresh["result"]["stopped_reason"], "capability grant expired")

    async def test_reconcile_removes_containers_that_should_be_stopped(self):
        row = await self._create()
        await self.registry.update(row["id"], desired_state=DESIRED_STOPPED)
        report = await self.manager.reconcile()
        self.assertEqual(report.removed, [row["id"]])
        self.assertNotIn(row["container_name"], self.runtime.containers)
        self.assertEqual((await self.registry.get(row["id"]))["status"], STATUS_STOPPED)

    async def test_reconcile_reports_orphans_without_touching_them(self):
        self.runtime.add_orphan("icarus-subagent-sub-1600000000-abcdef", "sub-1600000000-abcdef")
        report = await self.manager.reconcile()
        self.assertEqual(report.orphans, ["icarus-subagent-sub-1600000000-abcdef"])
        self.assertIn("icarus-subagent-sub-1600000000-abcdef", self.runtime.containers)

    async def test_reconcile_isolates_per_row_failures(self):
        a = await self._create()
        b = await self._create()
        real_inspect = self.runtime.inspect

        async def inspect(name):
            if name == a["container_name"]:
                raise DockerError("daemon hiccup")
            return await real_inspect(name)

        with mock.patch.object(self.runtime, "inspect", inspect):
            report = await self.manager.reconcile()
        self.assertIn(a["id"], report.failed)
        self.assertEqual(report.healthy, [b["id"]])
        self.assertTrue((await self.registry.get(a["id"]))["last_error"].startswith("reconcile:"))


class ContainerSpecTests(unittest.TestCase):
    def test_argv_shape(self):
        spec = ContainerSpec(
            name="n", image="img", command=["python", "-m", "x"], env={"A": "1"}, env_file="/e",
            volumes=[("/h", "/c", "ro"), ("/h2", "/c2", "")], labels={"k": "v"},
        )
        argv = spec.to_run_argv()
        pairs = [argv[i:i + 2] for i in range(len(argv) - 1)]
        for pair in (["--name", "n"], ["--restart", "unless-stopped"], ["--network", "host"], ["--user", "1000"],
                     ["--workdir", "/app"], ["--env-file", "/e"], ["-e", "A=1"], ["-v", "/h:/c:ro"],
                     ["-v", "/h2:/c2"], ["--label", "k=v"]):
            self.assertIn(pair, pairs)
        self.assertEqual(argv[-4:], ["img", "python", "-m", "x"])


class DockerRuntimeParsingTests(unittest.IsolatedAsyncioTestCase):
    def _runtime(self, results: dict):
        calls = []

        def runner(argv, timeout):
            calls.append(argv)
            return results.get(argv[1], CommandResult(1, "", "unexpected"))

        return DockerRuntime(runner), calls

    async def test_inspect_parses_state(self):
        rt, _ = self._runtime({"inspect": CommandResult(0, "abc123\t/icarus-subagent-x\trunning\ttrue\t0\n", "")})
        st = await rt.inspect("icarus-subagent-x")
        self.assertEqual((st.id, st.name, st.status, st.running, st.exit_code), ("abc123", "icarus-subagent-x", "running", True, 0))

    async def test_inspect_missing_is_none_but_other_errors_raise(self):
        rt, _ = self._runtime({"inspect": CommandResult(1, "", "Error: No such object: nope")})
        self.assertIsNone(await rt.inspect("nope"))
        rt, _ = self._runtime({"inspect": CommandResult(1, "", "Cannot connect to the Docker daemon")})
        with self.assertRaises(DockerError):
            await rt.inspect("x")

    async def test_run_detached(self):
        rt, calls = self._runtime({"run": CommandResult(0, "deadbeef\n", "")})
        spec = ContainerSpec(name="n", image="img", command=["sleep", "1"])
        self.assertEqual(await rt.run_detached(spec), "deadbeef")
        self.assertEqual(calls[0][:3], ["docker", "run", "-d"])
        rt, _ = self._runtime({"run": CommandResult(125, "", "Unable to find image")})
        with self.assertRaises(DockerError) as ctx:
            await rt.run_detached(spec)
        self.assertIn("Unable to find image", str(ctx.exception))

    async def test_remove_semantics(self):
        rt, _ = self._runtime({"rm": CommandResult(0, "n\n", "")})
        self.assertTrue(await rt.remove("n"))
        rt, _ = self._runtime({"rm": CommandResult(1, "", "Error: No such container: n")})
        self.assertFalse(await rt.remove("n"))
        rt, _ = self._runtime({"rm": CommandResult(1, "", "permission denied")})
        with self.assertRaises(DockerError):
            await rt.remove("n")

    async def test_list_parses_rows(self):
        rt, _ = self._runtime({"ps": CommandResult(0, "id1\tname1\trunning\tsub-1\nid2\tname2\texited\tsub-2\n", "")})
        rows = await rt.list_subagent_containers()
        self.assertEqual(rows, [
            {"id": "id1", "name": "name1", "state": "running", "task_id": "sub-1"},
            {"id": "id2", "name": "name2", "state": "exited", "task_id": "sub-2"},
        ])

    async def test_available(self):
        rt, _ = self._runtime({"version": CommandResult(0, "29.0\n", "")})
        self.assertTrue(await rt.available())
        rt, _ = self._runtime({})
        self.assertFalse(await rt.available())

    def test_default_runner_handles_a_missing_binary(self):
        result = _default_runner(["definitely-not-a-binary-xyz"], 5)
        self.assertEqual(result.returncode, 127)


@unittest.skipUnless(os.environ.get("ICARUS_DOCKER_TESTS") == "1", "set ICARUS_DOCKER_TESTS=1 to run against the real Docker daemon")
class RealDockerSmokeTests(unittest.IsolatedAsyncioTestCase):
    """Creates a real container from the icarus-worker image running `sleep`,
    then proves reconcile adopts it after a "Councilor restart" (fresh
    manager over the same registry file), respawns it once after an
    out-of-band removal, and stop() actually removes it. Temp registry and
    temp memory dir; the real repo root only for the read-only backend mount."""

    async def asyncSetUp(self):
        self.runtime = DockerRuntime()
        if not await self.runtime.available():
            self.skipTest("docker daemon not reachable")
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.registry = SubagentRegistry(tmp / "councilor.db")
        await self.registry.init()
        self.root = Path(__file__).resolve().parents[2]
        self.memory_dir = tmp / "memory"
        self.manager = SubagentManager(
            self.registry, self.runtime, self.root, memory_dir=self.memory_dir, image="icarus-worker",
            max_persistent=1, command_override=["sleep", "600"],
        )
        self.created_names: list[str] = []

    async def asyncTearDown(self):
        for name in self.created_names:
            try:
                await self.runtime.remove(name, force=True)
            except Exception:
                pass
        await self.registry.close()
        self.tmp.cleanup()

    async def test_lifecycle_against_the_real_daemon(self):
        row = await self.manager.create(intent="smoke test — sleeps only", capabilities=["time"], interval_seconds=60)
        name = row["container_name"]
        self.created_names.append(name)
        state = await self.runtime.inspect(name)
        self.assertTrue(state.running)
        self.assertEqual(state.id, row["container_id"])
        self.assertIn(row["id"], [c["task_id"] for c in await self.runtime.list_subagent_containers()])

        # "Councilor restart": a fresh manager over the same registry file adopts, never duplicates.
        registry2 = SubagentRegistry(self.registry.path)
        manager2 = SubagentManager(
            registry2, self.runtime, self.root, memory_dir=self.memory_dir, image="icarus-worker",
            max_persistent=1, command_override=["sleep", "600"],
        )
        try:
            report = await manager2.reconcile()
            self.assertEqual(report.healthy, [row["id"]])
            self.assertEqual(report.respawned, [])

            # Container vanishes out of band -> respawned exactly once.
            self.assertTrue(await self.runtime.remove(name, force=True))
            report = await manager2.reconcile()
            self.assertEqual(report.respawned, [row["id"]])
            fresh = await registry2.get(row["id"])
            self.assertEqual(fresh["restart_count"], 1)
            self.assertNotEqual(fresh["container_id"], row["container_id"])
            self.assertTrue((await self.runtime.inspect(name)).running)
            report = await manager2.reconcile()
            self.assertEqual((report.healthy, report.respawned), ([row["id"]], []))

            stopped = await manager2.stop(row["id"])
            self.assertIsNone(await self.runtime.inspect(name))
            self.assertEqual(stopped["status"], STATUS_STOPPED)
            self.assertTrue(stopped["result"]["container_removed"])
        finally:
            await registry2.close()


if __name__ == "__main__":
    unittest.main()
