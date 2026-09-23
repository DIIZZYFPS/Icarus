"""
docker_runtime.py — a thin, injectable wrapper over the `docker` CLI.

Persistent subagents (subagent_manager.py) are containers on the same daemon
the compose services already run on. This goes through the CLI rather than
the docker SDK because the SDK isn't in the Councilor's host venv and the
CLI is what councilor.py's apply_pending_upgrade already shells out to — one
dependency surface, same daemon, same permissions.

Every call is a subprocess in a worker thread so it never blocks the
Councilor's event loop. The subprocess runner is injectable so the manager
can be tested against a fake daemon without touching Docker at all.
"""

import asyncio
import logging
import subprocess
from dataclasses import dataclass, field
from typing import Callable

logger = logging.getLogger(__name__)

SUBAGENT_LABEL = "icarus.subagent"          # label key; value is the task id
SUBAGENT_NAME_PREFIX = "icarus-subagent-"


class DockerError(RuntimeError):
    pass


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass
class ContainerState:
    id: str
    name: str
    status: str          # created | running | paused | restarting | removing | exited | dead
    running: bool
    exit_code: int


@dataclass
class ContainerSpec:
    name: str
    image: str
    command: list[str]
    env: dict[str, str] = field(default_factory=dict)
    env_file: str | None = None
    volumes: list[tuple[str, str, str]] = field(default_factory=list)   # (host, container, mode)
    labels: dict[str, str] = field(default_factory=dict)
    network: str | None = "host"
    user: str | None = "1000"
    workdir: str | None = "/app"
    restart: str = "unless-stopped"

    def to_run_argv(self) -> list[str]:
        argv = ["docker", "run", "-d", "--name", self.name, "--restart", self.restart]
        if self.network:
            argv += ["--network", self.network]
        if self.user:
            argv += ["--user", self.user]
        if self.workdir:
            argv += ["--workdir", self.workdir]
        if self.env_file:
            argv += ["--env-file", self.env_file]
        for key, value in self.env.items():
            argv += ["-e", f"{key}={value}"]
        for host_path, container_path, mode in self.volumes:
            spec = f"{host_path}:{container_path}" + (f":{mode}" if mode else "")
            argv += ["-v", spec]
        for key, value in self.labels.items():
            argv += ["--label", f"{key}={value}"]
        argv.append(self.image)
        argv += list(self.command)
        return argv


def _default_runner(argv: list[str], timeout: float) -> CommandResult:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return CommandResult(proc.returncode, proc.stdout, proc.stderr)
    except FileNotFoundError:
        return CommandResult(127, "", "docker CLI not found on PATH")
    except subprocess.TimeoutExpired:
        return CommandResult(124, "", f"docker command timed out after {timeout:.0f}s: {' '.join(argv[:3])}")


class DockerRuntime:
    def __init__(self, runner: Callable[[list[str], float], CommandResult] | None = None):
        self._runner = runner or _default_runner

    async def _run(self, argv: list[str], timeout: float = 60) -> CommandResult:
        result = await asyncio.to_thread(self._runner, argv, timeout)
        if not result.ok:
            logger.debug(f"[docker] {' '.join(argv[:4])}... -> rc={result.returncode} {result.stderr.strip()[:200]}")
        return result

    async def available(self) -> bool:
        result = await self._run(["docker", "version", "--format", "{{.Server.Version}}"], timeout=15)
        return result.ok

    async def run_detached(self, spec: ContainerSpec) -> str:
        """Start a container; returns its id. Raises DockerError on failure."""
        result = await self._run(spec.to_run_argv(), timeout=120)
        if not result.ok:
            raise DockerError(f"docker run failed for {spec.name}: {result.stderr.strip()[:500]}")
        container_id = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
        if not container_id:
            raise DockerError(f"docker run for {spec.name} returned no container id")
        return container_id

    async def inspect(self, name_or_id: str) -> ContainerState | None:
        """None if the container doesn't exist."""
        fmt = "{{.Id}}\t{{.Name}}\t{{.State.Status}}\t{{.State.Running}}\t{{.State.ExitCode}}"
        result = await self._run(["docker", "inspect", "--type", "container", "--format", fmt, name_or_id], timeout=30)
        if not result.ok:
            if "No such" in result.stderr or "no such" in result.stderr:
                return None
            raise DockerError(f"docker inspect failed for {name_or_id}: {result.stderr.strip()[:300]}")
        parts = result.stdout.strip().split("\t")
        if len(parts) < 5:
            raise DockerError(f"unexpected docker inspect output for {name_or_id}: {result.stdout!r}")
        cid, name, status, running, exit_code = parts[:5]
        try:
            exit_code_int = int(exit_code)
        except ValueError:
            exit_code_int = -1
        return ContainerState(
            id=cid, name=name.lstrip("/"), status=status,
            running=(running.strip().lower() == "true"), exit_code=exit_code_int,
        )

    async def start(self, name_or_id: str) -> None:
        result = await self._run(["docker", "start", name_or_id], timeout=60)
        if not result.ok:
            raise DockerError(f"docker start failed for {name_or_id}: {result.stderr.strip()[:300]}")

    async def stop(self, name_or_id: str, timeout_s: int = 10) -> None:
        result = await self._run(["docker", "stop", "-t", str(timeout_s), name_or_id], timeout=timeout_s + 30)
        if not result.ok and "No such" not in result.stderr:
            raise DockerError(f"docker stop failed for {name_or_id}: {result.stderr.strip()[:300]}")

    async def remove(self, name_or_id: str, force: bool = True) -> bool:
        """Remove a container. Returns False if it didn't exist."""
        argv = ["docker", "rm"] + (["-f"] if force else []) + [name_or_id]
        result = await self._run(argv, timeout=60)
        if result.ok:
            return True
        if "No such" in result.stderr or "no such" in result.stderr:
            return False
        raise DockerError(f"docker rm failed for {name_or_id}: {result.stderr.strip()[:300]}")

    async def list_subagent_containers(self) -> list[dict]:
        """Every container carrying our label, running or not."""
        fmt = "{{.ID}}\t{{.Names}}\t{{.State}}\t{{.Label \"" + SUBAGENT_LABEL + "\"}}"
        result = await self._run(["docker", "ps", "-a", "--filter", f"label={SUBAGENT_LABEL}", "--format", fmt], timeout=30)
        if not result.ok:
            raise DockerError(f"docker ps failed: {result.stderr.strip()[:300]}")
        rows = []
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 4:
                rows.append({"id": parts[0], "name": parts[1], "state": parts[2], "task_id": parts[3]})
        return rows

    async def logs_tail(self, name_or_id: str, lines: int = 50) -> str:
        result = await self._run(["docker", "logs", "--tail", str(lines), name_or_id], timeout=30)
        return (result.stdout + result.stderr).strip()
