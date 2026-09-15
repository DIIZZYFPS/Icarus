"""
delegation.py — The L1 ↔ L2 contract for delegated subagent tasks.

Both sides of the Redis boundary import this module — esc_tool.py (L1, in
the icarus-api container) to validate and build a delegation request before
publishing it, and councilor.py (L2, bare host) to parse it back, resolve the
declared capabilities into an actual tool list, and shape the result into the
one thing L1 ever receives: a short CompletionEnvelope.

Three things live here, deliberately together so the contract can't drift
between the two processes:

  1. DelegationRequest — the wire shape. A task declares what it's allowed
     to touch ({intent, capabilities, needs_network, needs_repo_write})
     instead of every escalation implicitly meaning "edit this repo's source
     in a sandbox". The legacy `escalation` request type still works: it's
     normalized onto the same shape with needs_repo_write=True.

  2. CAPABILITY_CATALOG — capability name -> the tool functions it grants.
     Loaders are lazy on purpose: the Councilor's host venv is minimal
     (redis/httpx/sqlalchemy), so importing gmail_tools at module level
     would make this file unimportable there. A capability whose module
     can't be imported resolves as *unavailable* with a reason, and the task
     fails fast with that reason rather than running without a tool it was
     declared to need.

  3. CompletionEnvelope — the completion contract. process_escalation
     already returned a summary string rather than the sandbox's raw output;
     this codifies that as the shape every task kind returns. Summaries are
     hard-capped (DELEGATION_SUMMARY_MAX_CHARS) so raw tool output — logs,
     scrape dumps, email bodies — structurally cannot reach L1's context:
     it stays in the subagent's own transcript, which is discarded when the
     task ends.

Module-level imports here must stay dependency-free (stdlib only) — see (2).
"""

import os
import re
import json
import time
import uuid
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

logger = logging.getLogger(__name__)

# ── Task ids ─────────────────────────────────────────────────────────────────
# `sub-<unix ts>-<6 hex>`. L1 mints the id at dispatch time so it can hand it
# straight back to the model (and log it) without waiting for the Councilor;
# the Councilor validates and, for legacy escalations that carry none, mints
# its own. The same id is the registry key and the activity thread id.

TASK_ID_RE = re.compile(r"^sub-\d{6,}-[0-9a-f]{6}$")


def new_task_id(timestamp: int | float | None = None) -> str:
    ts = int(timestamp if timestamp is not None else time.time())
    return f"sub-{ts}-{uuid.uuid4().hex[:6]}"


def is_task_id(value: Any) -> bool:
    return isinstance(value, str) and bool(TASK_ID_RE.match(value))


class DelegationError(ValueError):
    """A delegation request that can't be run as declared. The message is
    written for the model that made the call — it names what's wrong and
    what the valid options are, so the next attempt can be correct."""


# ── Capability catalog ───────────────────────────────────────────────────────

class CapabilityUnavailable(RuntimeError):
    """Raised by a loader when its tools can't be provided in this process
    (missing dependency, missing config, wrong host)."""


@dataclass(frozen=True)
class CapabilitySpec:
    name: str
    description: str
    needs_network: bool
    # Given the request (for anything that must be bound to the requesting
    # platform/user), returns the tool callables. Raises ImportError or
    # CapabilityUnavailable if they can't be provided here.
    loader: Callable[["DelegationRequest"], list[Callable]]


def _get_time() -> dict:
    """Return the current UTC time from the system clock as ISO 8601 plus an
    epoch float. Use for any question about the current date or time."""
    now = datetime.now(timezone.utc)
    return {
        "utc_iso": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "epoch_float": now.timestamp(),
    }


# Tool-name compatibility: the model sees `get_time`, same as L1's tool.
_get_time.__name__ = "get_time"


def _load_time(req: "DelegationRequest") -> list[Callable]:
    return [_get_time]


def _load_web(req: "DelegationRequest") -> list[Callable]:
    from .websearch_tools import web_search, web_extract
    return [web_search, web_extract]


def _load_gmail_read(req: "DelegationRequest") -> list[Callable]:
    from .gmail_tools import gmail_list_messages, gmail_get_message
    return [gmail_list_messages, gmail_get_message]


def _load_calendar_read(req: "DelegationRequest") -> list[Callable]:
    from .calendar_tools import calendar_list_upcoming_events
    return [calendar_list_upcoming_events]


def _load_github_read(req: "DelegationRequest") -> list[Callable]:
    from .github_tools import (
        github_get_repo_info, github_list_repos, github_read_file,
        github_list_issues, github_read_issue,
    )
    return [github_get_repo_info, github_list_repos, github_read_file,
            github_list_issues, github_read_issue]


def _load_github_write(req: "DelegationRequest") -> list[Callable]:
    from .github_tools import (
        github_create_issue, github_create_branch, github_write_file, github_create_pr,
    )
    return [github_create_issue, github_create_branch, github_write_file, github_create_pr]


def _load_telemetry(req: "DelegationRequest") -> list[Callable]:
    from .telemetry_tools import get_telemetry_snapshot
    return [get_telemetry_snapshot]


def _load_tracked_items(req: "DelegationRequest") -> list[Callable]:
    # backend.database.connection resolves the shared icarus.db relative to
    # the working directory when not in a container. From the Councilor's
    # host process that's only right if it was started from the repo root —
    # refuse (rather than silently open a brand-new empty DB elsewhere) if
    # the shared file isn't where the connection module will look for it.
    from backend.database import connection as db
    db_file = os.path.join(db.DB_DIR, "icarus.db")
    if not os.path.exists(db_file):
        raise CapabilityUnavailable(
            f"shared icarus.db not found at {db_file!r} from this process's working directory"
        )
    from .tracked_items_repo import list_items

    platform = req.platform or "discord"
    user_id = req.user_id or os.environ.get("DISCORD_OPERATOR_ID", "0")

    async def list_tracked_items(item_type: str = "") -> str:
        """List the operator's tracked job applications, scored job
        opportunities, and bills — current state, most recently updated
        first. item_type filters to "job_application", "job_opportunity" or
        "bill"; leave empty for everything."""
        items = await list_items(platform=platform, user_id=user_id, item_type=item_type or None)
        if not items:
            return "No tracked items found."
        lines = []
        for i in items:
            due = f", due {i.due_at}" if i.due_at else ""
            urgency = f" [{i.urgency}]" if i.urgency else ""
            lines.append(f"- ({i.item_type}) {i.entity_key}: {i.state}{due}{urgency} — {i.summary or ''}".rstrip())
        return "Tracked items:\n" + "\n".join(lines)

    return [list_tracked_items]


CAPABILITY_CATALOG: dict[str, CapabilitySpec] = {
    spec.name: spec for spec in (
        CapabilitySpec("time", "current UTC time (get_time)", False, _load_time),
        CapabilitySpec("web", "web search + page extraction via Tavily (web_search, web_extract)", True, _load_web),
        CapabilitySpec("gmail_read", "read Gmail (gmail_list_messages, gmail_get_message)", True, _load_gmail_read),
        CapabilitySpec("calendar_read", "read upcoming calendar events (calendar_list_upcoming_events)", True, _load_calendar_read),
        CapabilitySpec("github_read", "read GitHub repos/files/issues (github_get_repo_info, github_list_repos, github_read_file, github_list_issues, github_read_issue)", True, _load_github_read),
        CapabilitySpec("github_write", "GitHub writes on a branch + PRs (github_create_issue, github_create_branch, github_write_file, github_create_pr)", True, _load_github_write),
        CapabilitySpec("telemetry", "host telemetry snapshot from Redis (get_telemetry_snapshot)", False, _load_telemetry),
        CapabilitySpec("tracked_items", "the operator's tracked jobs/bills from the shared DB (list_tracked_items)", False, _load_tracked_items),
    )
}


def catalog_summary() -> str:
    """One line per capability — used in error messages and L1's tool doc."""
    return "\n".join(
        f"- {s.name}{' (network)' if s.needs_network else ''}: {s.description}"
        for s in CAPABILITY_CATALOG.values()
    )


def validate_declaration(
    capabilities: list[str] | None,
    needs_network: bool,
    needs_repo_write: bool,
) -> list[str]:
    """Return the normalized capability list, or raise DelegationError.

    Rules: every name must be in the catalog; a capability that reaches the
    network may only be granted when needs_network is declared — the
    declaration is the whole point, so an undeclared network need is an
    error the model can fix, not something to grant silently."""
    if capabilities is None:
        capabilities = []
    if isinstance(capabilities, str):
        capabilities = [c for c in re.split(r"[,\s]+", capabilities) if c]
    if not isinstance(capabilities, list):
        raise DelegationError("capabilities must be a list of capability names")

    normalized: list[str] = []
    unknown: list[str] = []
    for raw in capabilities:
        name = str(raw).strip().lower()
        if not name:
            continue
        if name not in CAPABILITY_CATALOG:
            unknown.append(name)
        elif name not in normalized:
            normalized.append(name)
    if unknown:
        raise DelegationError(
            f"Unknown capability name(s): {', '.join(unknown)}. Valid capabilities:\n{catalog_summary()}"
        )

    undeclared_network = [
        n for n in normalized if CAPABILITY_CATALOG[n].needs_network and not needs_network
    ]
    if undeclared_network:
        raise DelegationError(
            f"Capabilities {', '.join(undeclared_network)} reach the network, but needs_network is false. "
            f"Re-issue with needs_network=true (only if the task genuinely needs it)."
        )
    return normalized


# ── Request ──────────────────────────────────────────────────────────────────

REQUEST_TYPE_DELEGATION = "delegation"
REQUEST_TYPE_ESCALATION = "escalation"   # legacy wire type: repo-write task


@dataclass
class DelegationRequest:
    task_id: str
    intent: str
    capabilities: list[str]
    needs_network: bool
    needs_repo_write: bool
    kind: str                    # REQUEST_TYPE_DELEGATION | REQUEST_TYPE_ESCALATION
    timestamp: int
    platform: str | None = None
    user_id: str | None = None
    chat_id: str | None = None
    target_files: list[str] = field(default_factory=list)   # legacy hint, informational

    @property
    def thread_id(self) -> str:
        # Legacy escalations keep the `esc-<ts>` thread id L1's
        # escalate_to_councilor already publishes under, so the dashboard's
        # dispatch/response correlation is unchanged for them.
        if self.kind == REQUEST_TYPE_ESCALATION:
            return f"esc-{self.timestamp}"
        return f"sub-{self.task_id}"

    @property
    def response_type(self) -> str:
        return self.kind

    def to_payload(self) -> dict:
        return {
            "type": self.kind,
            "task_id": self.task_id,
            "timestamp": self.timestamp,
            "platform": self.platform,
            "user_id": self.user_id,
            "chat_id": self.chat_id,
            "intent": self.intent,
            "capabilities": list(self.capabilities),
            "needs_network": self.needs_network,
            "needs_repo_write": self.needs_repo_write,
            "target_files": list(self.target_files),
        }


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y", "on")
    return False


def build_delegation_request(
    *,
    intent: str,
    capabilities: list[str] | None,
    needs_network: bool = False,
    needs_repo_write: bool = False,
    platform: str | None = None,
    user_id: str | None = None,
    chat_id: str | None = None,
    timestamp: int | None = None,
    task_id: str | None = None,
) -> DelegationRequest:
    """L1-side constructor: validates the declaration up front so the model
    gets an immediate, actionable error instead of a fire-and-forget request
    that fails minutes later in the Councilor."""
    intent = (intent or "").strip()
    if not intent:
        raise DelegationError("intent must be a non-empty description of the task")
    ts = int(timestamp if timestamp is not None else time.time())
    return DelegationRequest(
        task_id=task_id if is_task_id(task_id) else new_task_id(ts),
        intent=intent,
        capabilities=validate_declaration(capabilities, _as_bool(needs_network), _as_bool(needs_repo_write)),
        needs_network=_as_bool(needs_network),
        needs_repo_write=_as_bool(needs_repo_write),
        kind=REQUEST_TYPE_DELEGATION,
        timestamp=ts,
        platform=platform,
        user_id=user_id,
        chat_id=chat_id,
    )


def parse_delegation_request(data: dict) -> DelegationRequest:
    """Councilor-side parser for a request payload off the Redis channel.
    Accepts both the new `delegation` type and the legacy `escalation` type
    (normalized to needs_repo_write=True, no extra capabilities unless the
    payload declares some). Raises DelegationError on anything unusable."""
    if not isinstance(data, dict):
        raise DelegationError("request payload must be a JSON object")
    req_type = data.get("type") or REQUEST_TYPE_DELEGATION
    if req_type not in (REQUEST_TYPE_DELEGATION, REQUEST_TYPE_ESCALATION):
        raise DelegationError(f"unsupported request type {req_type!r}")

    intent = (data.get("intent") or "").strip()
    if not intent:
        raise DelegationError("request has no intent")

    ts = data.get("timestamp")
    try:
        ts = int(ts) if ts is not None else int(time.time())
    except (TypeError, ValueError):
        ts = int(time.time())

    needs_network = _as_bool(data.get("needs_network", False))
    if req_type == REQUEST_TYPE_ESCALATION:
        needs_repo_write = True
    else:
        needs_repo_write = _as_bool(data.get("needs_repo_write", False))

    capabilities = validate_declaration(data.get("capabilities"), needs_network, needs_repo_write)

    task_id = data.get("task_id")
    if not is_task_id(task_id):
        task_id = new_task_id(ts)

    target_files = data.get("target_files") or []
    if not isinstance(target_files, list):
        target_files = [str(target_files)]

    return DelegationRequest(
        task_id=task_id,
        intent=intent,
        capabilities=capabilities,
        needs_network=needs_network,
        needs_repo_write=needs_repo_write,
        kind=req_type,
        timestamp=ts,
        platform=data.get("platform"),
        user_id=str(data["user_id"]) if data.get("user_id") is not None else None,
        chat_id=str(data["chat_id"]) if data.get("chat_id") is not None else None,
        target_files=[str(f) for f in target_files],
    )


def stub_request_from(data: dict) -> DelegationRequest:
    """A best-effort DelegationRequest for a payload parse_delegation_request
    rejected — enough (ids, routing fields) to deliver the rejection back to
    whoever asked, so a malformed request fails loudly instead of vanishing."""
    data = data if isinstance(data, dict) else {}
    kind = data.get("type") if data.get("type") in (REQUEST_TYPE_DELEGATION, REQUEST_TYPE_ESCALATION) else REQUEST_TYPE_DELEGATION
    try:
        ts = int(data.get("timestamp") or time.time())
    except (TypeError, ValueError):
        ts = int(time.time())
    task_id = data.get("task_id")
    return DelegationRequest(
        task_id=task_id if is_task_id(task_id) else new_task_id(ts),
        intent=str(data.get("intent") or ""),
        capabilities=[],
        needs_network=False,
        needs_repo_write=(kind == REQUEST_TYPE_ESCALATION),
        kind=kind,
        timestamp=ts,
        platform=data.get("platform"),
        user_id=str(data["user_id"]) if data.get("user_id") is not None else None,
        chat_id=str(data["chat_id"]) if data.get("chat_id") is not None else None,
    )


# ── Tool resolution ──────────────────────────────────────────────────────────

@dataclass
class ResolvedTools:
    tools: list[Callable]
    granted: dict[str, list[str]]        # capability -> tool names it contributed
    unavailable: dict[str, str]          # capability -> why it couldn't be loaded

    def describe(self) -> str:
        """Human/model-readable grant list for the subagent's system prompt."""
        if not self.granted:
            return "(no capabilities granted beyond the base tools)"
        return "\n".join(f"- {cap}: {', '.join(names)}" for cap, names in self.granted.items())


def resolve_tools(req: DelegationRequest, extra_tools: list[Callable] | None = None) -> ResolvedTools:
    """Turn a validated declaration into the concrete tool list for the
    subagent loop. `extra_tools` (e.g. the sandboxed worktree tools for a
    repo-write task) go first; catalog tools are appended in declaration
    order, de-duplicated by function name. A capability that fails to load
    is reported in `unavailable` — callers should fail the task on any
    entry there rather than run it partially equipped."""
    tools: list[Callable] = list(extra_tools or [])
    seen = {fn.__name__ for fn in tools}
    granted: dict[str, list[str]] = {}
    unavailable: dict[str, str] = {}

    for name in req.capabilities:
        spec = CAPABILITY_CATALOG.get(name)
        if spec is None:
            unavailable[name] = "unknown capability"
            continue
        try:
            fns = spec.loader(req)
        except ImportError as e:
            unavailable[name] = f"dependency missing in this process: {e}"
            continue
        except CapabilityUnavailable as e:
            unavailable[name] = str(e)
            continue
        except Exception as e:  # a loader bug should fail the task, not crash the daemon
            unavailable[name] = f"loader error: {e}"
            continue
        contributed = []
        for fn in fns:
            if fn.__name__ in seen:
                continue
            seen.add(fn.__name__)
            tools.append(fn)
            contributed.append(fn.__name__)
        granted[name] = contributed

    return ResolvedTools(tools=tools, granted=granted, unavailable=unavailable)


# ── Completion envelope ──────────────────────────────────────────────────────

SUMMARY_MAX_CHARS = int(os.getenv("DELEGATION_SUMMARY_MAX_CHARS", "1500"))
ERROR_MAX_CHARS = 500

STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"

# Loop-level failure sentinels emitted by local_llm.local_agent_loop — the
# only responses that mean "the loop itself broke", as opposed to a model
# summary that happens to contain the word "error".
_LOOP_FAILURE_PREFIXES = (
    "Agent loop error on turn",
    "(Agent loop exhausted",
    "(Agent produced no output)",
    "Local LLM error:",
)


def looks_like_loop_failure(response: str | None) -> bool:
    text = (response or "").strip()
    return not text or any(text.startswith(p) for p in _LOOP_FAILURE_PREFIXES)


def truncate_summary(text: str | None, limit: int = SUMMARY_MAX_CHARS) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rstrip()
    return f"{cut}\n…[summary truncated at {limit} chars — the full transcript stayed with the subagent]"


@dataclass
class CompletionEnvelope:
    """The only thing that crosses back to L1 when a delegated task ends."""
    task_id: str
    kind: str
    status: str                       # STATUS_COMPLETED | STATUS_FAILED
    summary: str
    artifacts: dict = field(default_factory=dict)   # e.g. {"pr_url": ..., "branch": ...}
    error: str | None = None
    elapsed_s: float = 0.0
    finished_at: str = ""

    @classmethod
    def build(
        cls,
        *,
        task_id: str,
        kind: str,
        status: str,
        raw_summary: str | None,
        artifacts: dict | None = None,
        error: str | None = None,
        elapsed_s: float = 0.0,
    ) -> "CompletionEnvelope":
        return cls(
            task_id=task_id,
            kind=kind,
            status=status,
            summary=truncate_summary(raw_summary),
            artifacts={k: v for k, v in (artifacts or {}).items() if v},
            error=(error or "")[:ERROR_MAX_CHARS] or None,
            elapsed_s=round(float(elapsed_s), 1),
            finished_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "kind": self.kind,
            "status": self.status,
            "summary": self.summary,
            "artifacts": dict(self.artifacts),
            "error": self.error,
            "elapsed_s": self.elapsed_s,
            "finished_at": self.finished_at,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    def render(self) -> str:
        """Plain-text form delivered to L1 via the mailbox. Kept close to the
        pre-envelope escalation format (summary, then a PR line) so L1's
        existing [MAILBOX] handling reads it the same way."""
        lines = [f"[{self.kind} {self.task_id}] {self.status} ({self.elapsed_s:.0f}s)"]
        if self.summary:
            lines += ["", self.summary]
        elif self.status == STATUS_FAILED and not self.error:
            lines += ["", "(no summary produced)"]
        pr_url = self.artifacts.get("pr_url")
        if pr_url:
            lines += ["", f"PR: {pr_url}"]
        if self.error:
            lines += ["", f"Error: {self.error}"]
        return "\n".join(lines)
