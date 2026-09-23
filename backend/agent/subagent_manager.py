"""
subagent_manager.py — Persistent subagent lifecycle: create, stop, reconcile.

A persistent subagent is "monitor my task list and keep it updated" as a
real request type instead of a docker-compose.yml edit: a container on the
same daemon the static workers run on, built from the SAME icarus-worker
image they use, given a task directive and a capability scope via a spec
file in its own state directory, running backend/agent/worker_subagent.py.

Ownership: the Councilor owns these containers. The registry row is the
source of truth for what *should* be running (desired_state); Docker is the
source of truth for what *is*. reconcile() closes the gap — on Councilor
startup, and periodically — which is the dynamic equivalent of compose's
restart policy for the static services. It is needed because these
containers outlive the Councilor process, and the Councilor itself has no
restart supervision.

Bounds (all config, see .env.example):
  - MAX_PERSISTENT_SUBAGENTS — hard cap on concurrently desired-running
    subagents. Default 3: llama-server runs --parallel 1, so every extra
    scheduled agent is another periodic contender for the single slot L1
    chat also needs.
  - SUBAGENT_DEFAULT_TTL_HOURS — capability grants expire. A one-shot task's
    access window is minutes; a monitor holding Gmail read access for
    months is a different exposure, so a grant lasts a week by default and
    the operator re-creates the subagent to renew it. The worker exits
    cleanly on expiry and reconcile() removes the container.

Container restart policy is on-failure (not unless-stopped): a crash gets a
few automatic restarts, but a deliberate clean exit — expiry, or exit code
EXIT_CONFIG_ERROR for "my declared capabilities can't be loaded" — stays
down so it can't spam the operator in a restart loop; reconcile() decides
what happens next.
"""

import os
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .delegation import DelegationError, new_task_id, validate_declaration
from .docker_runtime import (
    ContainerSpec, DockerError, DockerRuntime, SUBAGENT_LABEL, SUBAGENT_NAME_PREFIX,
)
from .subagent_registry import (
    SubagentRegistry, KIND_PERSISTENT, STATUS_QUEUED, STATUS_RUNNING, STATUS_FAILED, STATUS_STOPPED,
    DESIRED_RUNNING, DESIRED_STOPPED,
)

logger = logging.getLogger(__name__)

MAX_PERSISTENT_SUBAGENTS = int(os.getenv("MAX_PERSISTENT_SUBAGENTS", "3"))
DEFAULT_INTERVAL_SECONDS = int(os.getenv("SUBAGENT_DEFAULT_INTERVAL_SECONDS", "900"))
MIN_INTERVAL_SECONDS = 60
MAX_INTERVAL_SECONDS = 86400
DEFAULT_TTL_HOURS = float(os.getenv("SUBAGENT_DEFAULT_TTL_HOURS", "168"))
SUBAGENT_IMAGE = os.getenv("SUBAGENT_IMAGE", "icarus-worker")
RESTART_POLICY = "on-failure:5"

REQUEST_TYPE_SUBAGENT = "subagent"
SPEC_FILENAME = "spec.json"
STATE_DB_FILENAME = "state.db"
CONTAINER_MEMORY_DIR = "/workspace/memory"
SUBAGENTS_DIRNAME = "subagents"
WORKER_MODULE = "backend.agent.worker_subagent"

# Worker exit codes reconcile() understands.
EXIT_OK = 0
EXIT_CONFIG_ERROR = 3   # declared capabilities can't be provided; don't restart, mark failed


class SubagentLimitError(RuntimeError):
    pass


class SubagentNotFound(KeyError):
    pass


def container_name_for(task_id: str) -> str:
    return f"{SUBAGENT_NAME_PREFIX}{task_id}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def clamp_interval(seconds) -> int:
    try:
        value = int(seconds) if seconds is not None else DEFAULT_INTERVAL_SECONDS
    except (TypeError, ValueError):
        value = DEFAULT_INTERVAL_SECONDS
    return max(MIN_INTERVAL_SECONDS, min(MAX_INTERVAL_SECONDS, value))


@dataclass
class ReconcileReport:
    healthy: list[str] = field(default_factory=list)
    respawned: list[str] = field(default_factory=list)
    started: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    expired: list[str] = field(default_factory=list)
    config_failed: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    orphans: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [
            f"healthy={len(self.healthy)}", f"respawned={len(self.respawned)}", f"started={len(self.started)}",
            f"removed={len(self.removed)}", f"expired={len(self.expired)}", f"config_failed={len(self.config_failed)}",
            f"errors={len(self.failed)}", f"orphans={len(self.orphans)}",
        ]
        return " ".join(parts)


class SubagentManager:
    def __init__(
        self,
        registry: SubagentRegistry,
        runtime: DockerRuntime,
        project_root: Path,
        *,
        memory_dir: Path | None = None,
        image: str = SUBAGENT_IMAGE,
        max_persistent: int = MAX_PERSISTENT_SUBAGENTS,
        env_overrides: dict[str, str] | None = None,
        command_override: list[str] | None = None,
    ):
        self.registry = registry
        self.runtime = runtime
        self.project_root = Path(project_root)
        self.memory_dir = Path(memory_dir) if memory_dir else self.project_root / "workspace" / "memory"
        self.image = image
        self.max_persistent = max_persistent
        self.env_overrides = dict(env_overrides or {})
        self.command_override = list(command_override) if command_override else None

    # ── paths ───────────────────────────────────────────────────────────

    def host_state_dir(self, task_id: str) -> Path:
        return self.memory_dir / SUBAGENTS_DIRNAME / task_id

    @staticmethod
    def container_state_dir(task_id: str) -> str:
        return f"{CONTAINER_MEMORY_DIR}/{SUBAGENTS_DIRNAME}/{task_id}"

    def build_container_spec(self, task_id: str) -> ContainerSpec:
        env = {
            "SUBAGENT_TASK_ID": task_id,
            "PYTHONUNBUFFERED": "1",
            # Host networking, same as the compose services — see docker-compose.yml.
            "REDIS_URL": os.getenv("SUBAGENT_REDIS_URL", "redis://127.0.0.1:6379/0"),
            "LOCAL_LLM_URL": os.getenv("SUBAGENT_LOCAL_LLM_URL", os.getenv("LOCAL_LLM_URL", "http://127.0.0.1:8080/v1")),
        }
        env.update(self.env_overrides)
        env_file = self.project_root / ".env"
        return ContainerSpec(
            name=container_name_for(task_id),
            image=self.image,
            command=self.command_override or ["python", "-m", WORKER_MODULE],
            env=env,
            env_file=str(env_file) if env_file.exists() else None,
            volumes=[
                (str(self.project_root / "backend"), "/app/backend", "ro"),
                (str(self.memory_dir), CONTAINER_MEMORY_DIR, "rw"),
                (str(self.project_root / "workspace" / "projects"), "/workspace/projects", "rw"),
            ],
            labels={SUBAGENT_LABEL: task_id, f"{SUBAGENT_LABEL}.created_at": _iso(_now())},
            network="host",
            user="1000",
            workdir="/app",
            restart=RESTART_POLICY,
        )

    # ── lifecycle ───────────────────────────────────────────────────────

    async def active_count(self) -> int:
        rows = await self.registry.list(kind=KIND_PERSISTENT, desired_state=DESIRED_RUNNING, limit=500)
        return len(rows)

    async def create(
        self,
        *,
        intent: str,
        capabilities: list[str] | None,
        needs_network: bool = False,
        interval_seconds: int | None = None,
        ttl_hours: float | None = None,
        platform: str | None = None,
        chat_id: str | None = None,
        user_id: str | None = None,
    ) -> dict:
        intent = (intent or "").strip()
        if not intent:
            raise DelegationError("intent must be a non-empty standing directive")
        caps = validate_declaration(capabilities, bool(needs_network), False)
        interval = clamp_interval(interval_seconds)
        ttl = DEFAULT_TTL_HOURS if ttl_hours is None else float(ttl_hours)
        expires_at = _iso(_now() + timedelta(hours=ttl)) if ttl > 0 else None

        active = await self.active_count()
        if active >= self.max_persistent:
            raise SubagentLimitError(
                f"{active} persistent subagent(s) already running — the cap is {self.max_persistent} "
                f"(MAX_PERSISTENT_SUBAGENTS). Stop one with stop_subagent first."
            )

        task_id = new_task_id()
        name = container_name_for(task_id)
        state_dir = self.host_state_dir(task_id)
        state_dir.mkdir(parents=True, exist_ok=True)
        spec = {
            "task_id": task_id,
            "intent": intent,
            "capabilities": caps,
            "needs_network": bool(needs_network),
            "interval_seconds": interval,
            "expires_at": expires_at,
            "platform": platform,
            "chat_id": chat_id,
            "user_id": user_id,
            "container_name": name,
            "created_at": _iso(_now()),
        }
        (state_dir / SPEC_FILENAME).write_text(json.dumps(spec, indent=2), encoding="utf-8")

        await self.registry.create(
            task_id=task_id, kind=KIND_PERSISTENT, request_type=REQUEST_TYPE_SUBAGENT, intent=intent,
            capabilities=caps, needs_network=bool(needs_network), needs_repo_write=False,
            platform=platform, chat_id=chat_id, user_id=user_id, thread_id=f"sub-{task_id}",
            status=STATUS_QUEUED, desired_state=DESIRED_RUNNING, container_name=name,
            state_path=self.container_state_dir(task_id), interval_seconds=interval, expires_at=expires_at,
        )
        try:
            container_id = await self.runtime.run_detached(self.build_container_spec(task_id))
        except DockerError as e:
            await self.registry.set_status(task_id, STATUS_FAILED, error=str(e), desired_state=DESIRED_STOPPED)
            raise
        await self.registry.update(task_id, status=STATUS_RUNNING, container_id=container_id)
        logger.info(f"[subagents] created {task_id} -> {name} ({container_id[:12]}) every {interval}s, expires {expires_at}")
        return await self.registry.get(task_id)

    async def stop(self, task_id: str, reason: str = "operator request") -> dict:
        row = await self.registry.get(task_id)
        if row is None or row["kind"] != KIND_PERSISTENT:
            raise SubagentNotFound(f"no persistent subagent with id {task_id}")
        name = row["container_name"] or container_name_for(task_id)
        removed = await self.runtime.remove(name, force=True)
        await self.registry.set_status(
            task_id, STATUS_STOPPED, desired_state=DESIRED_STOPPED,
            result={"stopped_reason": reason, "container_removed": removed, "stopped_at": _iso(_now())},
        )
        logger.info(f"[subagents] stopped {task_id} ({reason}); container removed={removed}")
        return await self.registry.get(task_id)

    async def reconcile(self) -> ReconcileReport:
        """Make Docker match the registry's desired state, one row at a time,
        never letting one row's failure stop the sweep."""
        report = ReconcileReport()
        rows = await self.registry.list(kind=KIND_PERSISTENT, limit=500)
        now_iso = _iso(_now())

        for row in rows:
            task_id = row["id"]
            name = row["container_name"] or container_name_for(task_id)
            try:
                if row["desired_state"] == DESIRED_RUNNING:
                    if row.get("expires_at") and row["expires_at"] <= now_iso:
                        await self.stop(task_id, reason="capability grant expired")
                        report.expired.append(task_id)
                        continue
                    state = await self.runtime.inspect(name)
                    if state is None:
                        restarts = await self.registry.increment_restart(task_id)
                        container_id = await self.runtime.run_detached(self.build_container_spec(task_id))
                        await self.registry.update(task_id, status=STATUS_RUNNING, container_id=container_id, last_error=None)
                        logger.warning(f"[subagents] {task_id}: container missing — respawned as {container_id[:12]} (restart #{restarts})")
                        report.respawned.append(task_id)
                    elif not state.running:
                        if state.exit_code == EXIT_CONFIG_ERROR:
                            await self.runtime.remove(name, force=True)
                            await self.registry.set_status(
                                task_id, STATUS_FAILED, desired_state=DESIRED_STOPPED,
                                error="worker exited with a configuration error (declared capabilities unavailable in the worker image) — see its logs",
                            )
                            report.config_failed.append(task_id)
                        else:
                            await self.runtime.start(name)
                            await self.registry.update(task_id, status=STATUS_RUNNING)
                            report.started.append(task_id)
                    else:
                        if row["status"] != STATUS_RUNNING:
                            await self.registry.update(task_id, status=STATUS_RUNNING)
                        report.healthy.append(task_id)
                else:
                    state = await self.runtime.inspect(name)
                    if state is not None:
                        await self.runtime.remove(name, force=True)
                        report.removed.append(task_id)
                    if row["status"] not in (STATUS_STOPPED, STATUS_FAILED):
                        await self.registry.set_status(task_id, STATUS_STOPPED)
            except Exception as e:
                logger.error(f"[subagents] reconcile failed for {task_id}: {e}")
                report.failed[task_id] = str(e)
                try:
                    await self.registry.update(task_id, last_error=f"reconcile: {e}"[:2000])
                except Exception:
                    pass

        # Containers carrying our label with no registry row: report, don't
        # touch. Nothing manages them, but deleting on sight would also hit
        # anything a test or another checkout labelled the same way.
        try:
            known = {r["id"] for r in rows}
            for c in await self.runtime.list_subagent_containers():
                if c["task_id"] not in known:
                    report.orphans.append(c["name"])
            if report.orphans:
                logger.warning(f"[subagents] labelled containers with no registry row (left alone): {report.orphans}")
        except Exception as e:
            logger.warning(f"[subagents] could not list labelled containers: {e}")

        return report
