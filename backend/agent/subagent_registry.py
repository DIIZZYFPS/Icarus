"""
subagent_registry.py — Durable bookkeeping for every subagent the Councilor runs.

One row per task: the one-shot delegations process_delegation() runs
in-process, and (from Phase 3) the persistent subagents that live in their
own containers. This is what check_task_status reads, what reconciliation
on Councilor startup walks, and where a completion envelope is kept after
the mailbox has delivered it.

Storage is its own SQLite file — `workspace/memory/councilor.db`, next to
the main icarus.db — NOT a table in icarus.db. activity_repo.py's rule
still holds: icarus.db has exactly one writer (icarus-api). The Councilor
is the only writer *here*; the icarus-api container can read the file
through the same bind mount it already has for /workspace/memory, which is
how L1's check_task_status tool works without a request round-trip. WAL
mode plus a busy timeout makes that cross-process read safe.

Redis would have been simpler for the shared-access part, but the compose
file gives icarus-redis no volume — it's wiped on recreate — and Phase 3's
reconciliation needs the registry to outlive both the Councilor process and
a `docker compose down`. A file on disk is the only thing here that does.

Requires sqlalchemy + aiosqlite in the Councilor's host venv (present).
"""

import os
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import Column, Integer, String, Text, select, text
from sqlalchemy.orm import declarative_base
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

logger = logging.getLogger(__name__)

RegistryBase = declarative_base()

# ── Vocabulary ───────────────────────────────────────────────────────────────

KIND_ONESHOT = "oneshot"          # process_delegation(): runs to completion in the Councilor
KIND_PERSISTENT = "persistent"    # Phase 3: a long-lived container

STATUS_QUEUED = "queued"          # accepted, not started
STATUS_RUNNING = "running"
STATUS_PAUSED = "paused"          # Phase 5: waiting on an operator decision
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_STOPPED = "stopped"        # persistent: torn down on request

ACTIVE_STATUSES = (STATUS_QUEUED, STATUS_RUNNING, STATUS_PAUSED)
TERMINAL_STATUSES = (STATUS_COMPLETED, STATUS_FAILED, STATUS_STOPPED)

DESIRED_RUNNING = "running"
DESIRED_STOPPED = "stopped"


class SubagentTask(RegistryBase):
    __tablename__ = "subagent_tasks"

    id = Column(String, primary_key=True)                    # sub-<ts>-<hex6>
    kind = Column(String, nullable=False, index=True)        # KIND_ONESHOT | KIND_PERSISTENT
    request_type = Column(String, nullable=False)            # wire type: delegation | escalation | subagent
    intent = Column(Text, nullable=False)
    capability_scope = Column(Text, nullable=False)          # JSON {"capabilities": [...], "needs_network": b, "needs_repo_write": b}
    status = Column(String, nullable=False, index=True)
    desired_state = Column(String, nullable=True)            # persistent only: DESIRED_RUNNING | DESIRED_STOPPED
    platform = Column(String, nullable=True)
    chat_id = Column(String, nullable=True)
    user_id = Column(String, nullable=True)
    thread_id = Column(String, nullable=True)                # activity-event correlation id
    result_ref = Column(Text, nullable=True)                 # JSON CompletionEnvelope / last report
    last_error = Column(Text, nullable=True)
    container_id = Column(String, nullable=True)             # persistent only
    container_name = Column(String, nullable=True)           # persistent only
    state_path = Column(String, nullable=True)               # persistent only: its private state dir
    restart_count = Column(Integer, default=0)               # persistent only: respawns by reconciliation
    interval_seconds = Column(Integer, nullable=True)        # persistent only
    expires_at = Column(String, nullable=True)               # persistent only: capability grant expiry
    paused_question = Column(Text, nullable=True)            # Phase 5
    created_at = Column(String, nullable=False, index=True)
    updated_at = Column(String, nullable=False)
    started_at = Column(String, nullable=True)
    finished_at = Column(String, nullable=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def default_registry_path() -> Path:
    """COUNCILOR_DB_PATH if set; otherwise /workspace/memory/councilor.db in a
    container, or <repo>/workspace/memory/councilor.db on the host — resolved
    from this file, never from the working directory, so a Councilor started
    from anywhere still finds the same file the container sees."""
    override = os.getenv("COUNCILOR_DB_PATH", "").strip()
    if override:
        return Path(override)
    if os.path.exists("/workspace"):
        return Path("/workspace/memory/councilor.db")
    return Path(__file__).resolve().parents[2] / "workspace" / "memory" / "councilor.db"


def row_to_dict(row: SubagentTask) -> dict:
    scope = {}
    try:
        scope = json.loads(row.capability_scope or "{}")
    except Exception:
        pass
    result = None
    if row.result_ref:
        try:
            result = json.loads(row.result_ref)
        except Exception:
            result = {"raw": row.result_ref}
    return {
        "id": row.id,
        "kind": row.kind,
        "request_type": row.request_type,
        "intent": row.intent,
        "capability_scope": scope,
        "status": row.status,
        "desired_state": row.desired_state,
        "platform": row.platform,
        "chat_id": row.chat_id,
        "user_id": row.user_id,
        "thread_id": row.thread_id,
        "result": result,
        "last_error": row.last_error,
        "container_id": row.container_id,
        "container_name": row.container_name,
        "state_path": row.state_path,
        "restart_count": row.restart_count or 0,
        "interval_seconds": row.interval_seconds,
        "expires_at": row.expires_at,
        "paused_question": row.paused_question,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
    }


class SubagentRegistry:
    """Async CRUD over one registry file. Construct with an explicit path in
    tests; production code goes through get_registry()."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else default_registry_path()
        self._engine = None
        self._sessions: async_sessionmaker[AsyncSession] | None = None
        self._initialized = False

    # ── engine ──────────────────────────────────────────────────────────

    def _ensure_engine(self):
        if self._engine is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._engine = create_async_engine(
                f"sqlite+aiosqlite:///{self.path}",
                echo=False,
                # timeout -> sqlite3 busy timeout: a concurrent reader/writer
                # waits instead of failing with "database is locked".
                connect_args={"check_same_thread": False, "timeout": 5.0},
            )
            self._sessions = async_sessionmaker(
                bind=self._engine, class_=AsyncSession,
                expire_on_commit=False, autocommit=False, autoflush=False,
            )
        return self._sessions

    async def init(self) -> None:
        """Create the schema and switch the file to WAL. Called by the
        Councilor at startup — the writer. Readers (L1) must not call this."""
        sessions = self._ensure_engine()
        async with self._engine.begin() as conn:
            await conn.run_sync(RegistryBase.metadata.create_all)
            try:
                await conn.execute(text("PRAGMA journal_mode=WAL"))
            except Exception as e:
                logger.warning(f"[registry] could not enable WAL on {self.path}: {e}")
        self._initialized = True
        logger.info(f"[registry] ready at {self.path}")

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._sessions = None

    def exists(self) -> bool:
        return self.path.exists()

    # ── writes ──────────────────────────────────────────────────────────

    async def create(
        self,
        *,
        task_id: str,
        kind: str,
        request_type: str,
        intent: str,
        capabilities: list[str],
        needs_network: bool,
        needs_repo_write: bool,
        platform: str | None = None,
        chat_id: str | None = None,
        user_id: str | None = None,
        thread_id: str | None = None,
        status: str = STATUS_QUEUED,
        desired_state: str | None = None,
        container_name: str | None = None,
        state_path: str | None = None,
        interval_seconds: int | None = None,
        expires_at: str | None = None,
    ) -> dict:
        now = _now_iso()
        sessions = self._ensure_engine()
        async with sessions() as session:
            row = SubagentTask(
                id=task_id, kind=kind, request_type=request_type, intent=intent,
                capability_scope=json.dumps({
                    "capabilities": list(capabilities),
                    "needs_network": bool(needs_network),
                    "needs_repo_write": bool(needs_repo_write),
                }),
                status=status, desired_state=desired_state,
                platform=platform, chat_id=chat_id, user_id=user_id, thread_id=thread_id,
                container_name=container_name, state_path=state_path,
                interval_seconds=interval_seconds, expires_at=expires_at,
                restart_count=0, created_at=now, updated_at=now,
                started_at=now if status == STATUS_RUNNING else None,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row_to_dict(row)

    async def update(self, task_id: str, **fields) -> bool:
        """Set arbitrary columns on a row. Returns False if the row doesn't
        exist. Stamps updated_at; started_at/finished_at follow status."""
        sessions = self._ensure_engine()
        async with sessions() as session:
            row = await session.get(SubagentTask, task_id)
            if row is None:
                return False
            now = _now_iso()
            new_status = fields.get("status")
            if new_status == STATUS_RUNNING and not row.started_at:
                row.started_at = now
            if new_status in TERMINAL_STATUSES:
                row.finished_at = now
            for key, value in fields.items():
                if not hasattr(row, key) or key in ("id", "created_at"):
                    raise ValueError(f"unknown or immutable registry field {key!r}")
                setattr(row, key, value)
            row.updated_at = now
            await session.commit()
            return True

    async def set_status(
        self,
        task_id: str,
        status: str,
        *,
        error: str | None = None,
        result: dict | str | None = None,
        **fields,
    ) -> bool:
        if error is not None:
            fields["last_error"] = error[:2000]
        if result is not None:
            fields["result_ref"] = result if isinstance(result, str) else json.dumps(result)
        return await self.update(task_id, status=status, **fields)

    async def increment_restart(self, task_id: str) -> int:
        sessions = self._ensure_engine()
        async with sessions() as session:
            row = await session.get(SubagentTask, task_id)
            if row is None:
                return 0
            row.restart_count = (row.restart_count or 0) + 1
            row.updated_at = _now_iso()
            await session.commit()
            return row.restart_count

    async def fail_incomplete_oneshots(self, reason: str) -> list[str]:
        """Startup reconciliation for one-shot tasks: anything still marked
        active belonged to a Councilor process that no longer exists, so it
        can never finish. Mark it failed with the reason and return the ids.
        Persistent rows are NOT touched here — their containers outlive the
        Councilor and are reconciled against Docker separately."""
        sessions = self._ensure_engine()
        failed: list[str] = []
        now = _now_iso()
        async with sessions() as session:
            result = await session.execute(
                select(SubagentTask).where(
                    SubagentTask.kind == KIND_ONESHOT,
                    SubagentTask.status.in_(ACTIVE_STATUSES),
                )
            )
            for row in result.scalars().all():
                row.status = STATUS_FAILED
                row.last_error = reason
                row.finished_at = now
                row.updated_at = now
                failed.append(row.id)
            await session.commit()
        if failed:
            logger.warning(f"[registry] marked {len(failed)} orphaned one-shot task(s) failed: {failed}")
        return failed

    # ── reads ───────────────────────────────────────────────────────────

    async def get(self, task_id: str) -> dict | None:
        sessions = self._ensure_engine()
        async with sessions() as session:
            row = await session.get(SubagentTask, task_id)
            return row_to_dict(row) if row else None

    async def list(
        self,
        *,
        kind: str | None = None,
        statuses: tuple[str, ...] | None = None,
        desired_state: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        sessions = self._ensure_engine()
        async with sessions() as session:
            query = select(SubagentTask)
            if kind:
                query = query.where(SubagentTask.kind == kind)
            if statuses:
                query = query.where(SubagentTask.status.in_(statuses))
            if desired_state:
                query = query.where(SubagentTask.desired_state == desired_state)
            query = query.order_by(SubagentTask.created_at.desc(), SubagentTask.id.desc()).limit(max(1, min(limit, 500)))
            result = await session.execute(query)
            return [row_to_dict(r) for r in result.scalars().all()]


# ── Process-wide instance ────────────────────────────────────────────────────

_registry: SubagentRegistry | None = None


def get_registry() -> SubagentRegistry:
    global _registry
    if _registry is None:
        _registry = SubagentRegistry()
    return _registry


# ── Read-side rendering (L1's check_task_status) ─────────────────────────────

_DESCRIBE_SUMMARY_CHARS = 600


def _fmt_task_line(t: dict) -> str:
    scope = t.get("capability_scope") or {}
    caps = ", ".join(scope.get("capabilities") or []) or "none"
    flags = []
    if scope.get("needs_repo_write"):
        flags.append("repo-write")
    if scope.get("needs_network"):
        flags.append("network")
    flag_text = f" [{', '.join(flags)}]" if flags else ""
    intent = (t.get("intent") or "").strip().replace("\n", " ")
    if len(intent) > 90:
        intent = intent[:87] + "…"
    return f"{t['id']} · {t['kind']} · {t['status']} · caps: {caps}{flag_text} · {t['created_at']} · {intent}"


def render_task(t: dict) -> str:
    """Full, model-readable status for one task."""
    lines = [_fmt_task_line(t)]
    if t.get("desired_state"):
        lines.append(f"desired state: {t['desired_state']}; container: {t.get('container_name') or '—'}; restarts: {t.get('restart_count', 0)}")
    if t.get("expires_at"):
        lines.append(f"capability grant expires: {t['expires_at']}")
    if t.get("started_at"):
        lines.append(f"started: {t['started_at']}" + (f"; finished: {t['finished_at']}" if t.get("finished_at") else ""))
    if t.get("status") == STATUS_PAUSED and t.get("paused_question"):
        lines.append(f"PAUSED — waiting on the operator: {t['paused_question']}")
    result = t.get("result") or {}
    summary = result.get("summary") if isinstance(result, dict) else None
    if summary:
        if len(summary) > _DESCRIBE_SUMMARY_CHARS:
            summary = summary[:_DESCRIBE_SUMMARY_CHARS] + "…"
        lines.append(f"result: {summary}")
    if isinstance(result, dict) and result.get("artifacts", {}).get("pr_url"):
        lines.append(f"PR: {result['artifacts']['pr_url']}")
    if t.get("last_error"):
        lines.append(f"error: {t['last_error']}")
    return "\n".join(lines)


async def describe_task(task_id: str | None, registry: SubagentRegistry | None = None) -> str:
    """Read-only lookup for L1. Never blocks on the task itself. An empty
    id lists recent tasks instead."""
    from backend.agent.delegation import is_task_id

    registry = registry or get_registry()
    if not registry.exists():
        return (
            f"The Councilor's task registry isn't available yet (no registry file at {registry.path}). "
            f"The Councilor has to be running with the delegation update for tasks to be tracked."
        )
    task_id = (task_id or "").strip()
    try:
        if not task_id:
            recent = await registry.list(limit=10)
            if not recent:
                return "No delegated tasks or subagents recorded yet."
            return "Recent tasks (newest first):\n" + "\n".join(f"- {_fmt_task_line(t)}" for t in recent)
        if not is_task_id(task_id):
            return f"'{task_id}' is not a task id — ids look like sub-1700000000-a1b2c3. Call with an empty id to list recent tasks."
        task = await registry.get(task_id)
        if task is None:
            return f"No task with id {task_id} in the registry."
        return render_task(task)
    except Exception as e:
        logger.error(f"[registry] describe_task failed: {e}")
        return f"Could not read the task registry: {e}"
