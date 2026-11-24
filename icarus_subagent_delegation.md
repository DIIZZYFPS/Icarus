# Architecture Design Record: Icarus Subagent Delegation & Supervision

**Document Version:** 3.0.0
**Status:** Draft — supersedes v2.0.0 (corrupted export, superseded in full)
**Primary components touched:** `councilor.py`, `backend/agent/llm_router.py`, `backend/agent/local_llm.py`, `backend/agent/engine.py`, `backend/agent/tools.py`, `backend/agent/capability_registry.py`

---

## 1. Why this revision exists

v2.0.0 proposed rebuilding Icarus around ephemeral Podman/gVisor containers spawned per-task, a dedicated Gateway Adapter Tier, and systemd-supervised host services. Re-reading it against the actual codebase turned up two problems:

1. **The file was corrupted** — sections were interleaved, a code block was split across two headings, and every heading was numbered "1." It couldn't be safely used as scope for implementation work.
2. **It solved problems the codebase doesn't have, and missed the one it does.** Councilor already runs on bare host, not in a container — there's no `docker.sock`-in-container exposure to eliminate. Icarus (L1) already has `consult_councilor` and `escalate_to_councilor` as real tool calls, so "the Councilor is an advisor that never gets used" isn't a missing-plumbing problem — the tool exists, it's just scoped narrowly to *editing Icarus's own source*. Meanwhile the actual gap — Councilor as a **supervisor of dynamically spawned subagents**, both one-shot and persistent — wasn't in v2.0.0 at all.

This revision drops the container-runtime rewrite and instead extends what's already there: the tool-call-shaped delegation interface, the worktree+bwrap sandbox, and the existing `consult`/`escalate` request split in `councilor.py`.

---

## 2. Design philosophy (unchanged from v2.0.0's good parts)

- **Inversion of routing.** Icarus (L1) is the permanent conversational surface. Delegation is a tool call *it* chooses to make, never an external classifier that pre-empts it. This stays true for everything in this document — see §7 for the one place that needed explicit scoping to keep it true.
- **Context-rot avoidance for the local quantized model.** L1 never ingests raw subagent output — logs, stdout, scrape dumps. It receives a short structured completion envelope. This is already how `process_escalation` behaves today (it returns a summary string, not the sandbox's raw output) — this document generalizes that shape rather than inventing it.
- **Supervision of children, not preemption of the parent.** Councilor may intervene inside a subagent it spawned — correcting a malformed tool call, redirecting after a container error, answering a scoping question. It does **not** intervene inside L1's own loop. L1 remains undisturbed; a subagent is Councilor's child, not L1's sibling. (This distinction is why the CoT-intervention idea from the source discussion is sound for subagents and would have been a mistake for L1 itself.)
- **Human review stays the gate on anything that mutates the repo.** No auto-merge. `capability_registry.py`'s sensitive/standard classification continues to apply to every escalation PR, unchanged.

---

## 3. Baseline: what actually exists today

Grounding this so later phases are diffs against reality, not against v2.0.0's assumptions.

| Piece | Current state |
|---|---|
| Councilor process | Bare foreground host process (`start.sh`: `python councilor.py`), no systemd unit, no restart-on-crash |
| L1 → Councilor interface | Real tool calls: `consult_councilor(question)` (blocks ≤60s, read-only) and `escalate_to_councilor(intent, target_files)` (fire-and-forget, background) — `backend/agent/engine.py:57-58`, bound in `backend/agent/tools.py:15-16` |
| Escalation sandbox | Disposable `git worktree` + `bwrap --unshare-all` (read-only host view, no network, jailed to the worktree) — `councilor.py:88-179`. Scoped only to editing this repo's own source. |
| Escalation lifecycle | `_create_escalation_worktree` → `agent_loop` with `read_file`/`write_file`/`list_directory`/`run_command` → `_finalize_worktree` (commit, push, PR via GitHub API, never auto-merge) → `_cleanup_worktree`. Whole-task retry (`max_retries=2`) on failure, not step-level correction. |
| Request dispatch | `councilor.py:_listen_for_requests` — a single `async for` over one Redis pub/sub channel, **awaited inline**. One escalation blocks every other request until it finishes. No concurrency, no task registry. |
| Persistent workers | Static Docker Compose services (`email_triage`, `job_scout`, `email_priority`), each a `WorkerBase` subclass consuming a Redis Stream with its own consumer group, retry/backoff, and DLQ (`backend/agent/worker_base.py`). Fixed at deploy time — adding one means editing `docker-compose.yml` and redeploying. |
| Tool-call failure handling | `local_agent_loop` (`backend/agent/local_llm.py:246-373`): malformed JSON args silently become `{}` (no correction, no surfacing — `json.JSONDecodeError` is swallowed at line 342); unknown tool or a raised exception becomes an `{"error": ...}` string appended to the transcript for the *same* model to react to next turn. No external supervision hook exists. The one existing callback, `on_activity`, is a fire-and-forget progress notifier, not an intervention point. |
| Deploy application | Manual, human-triggered only: `check_pending_upgrade`/`apply_pending_upgrade` (`councilor.py:436-558`) — fast-forward-only pull, rebuild only if `requirements.txt`/`Dockerfile` changed, refuses a dirty checkout. Not part of this document's scope; noted so phases below don't reinvent it. |
| Model routing | `llm_router.py` — task_type-keyed table, local-only today (`consultation`, `scoring`, `escalation` → local llama-server). Cloud (Gemini/Gemma) is dormant, explicitly not wired in, kept as a documented last-resort path. |

---

## 4. Non-goals

- **No Podman/gVisor/rootless-container-per-task tier.** Ephemeral, non-repo-mutating subagent tasks don't need OCI-level isolation — they run declared tools against declared capabilities, not arbitrary untrusted code. The worktree+bwrap pattern already gives real isolation for the one case (repo edits) that actually executes shell commands.
- **No unified "Gateway Adapter Tier" rewrite.** Discord/Gmail allowlisting stays where it is per-adapter unless a concrete incident shows it needs consolidating. Out of scope here.
- **No auto-merge, ever**, regardless of capability tier or how many times a subagent has succeeded before.
- **No mid-loop intervention in L1's own agent loop.** Only subagents Councilor spawns are supervised this way.
- **No change to the deploy-application flow** (`check_pending_upgrade`/`apply_pending_upgrade`) — it already does the right thing.

---

## 5. Target shape

```
                    ┌──────────────────────────┐
                    │   Icarus (L1, Discord-    │
                    │   facing, bare host)      │
                    │                           │
                    │  delegate_task(...)       │──┐
                    │  create_persistent_       │  │  tool calls,
                    │    subagent(...)          │  │  same shape as
                    │  stop_subagent(id)         │  │  consult/escalate
                    │  check_task_status(id)     │  │  today
                    └──────────────────────────┘  │
                                                    ▼
                    ┌───────────────────────────────────────────┐
                    │      Councilor (L2, bare host)             │
                    │                                             │
                    │  ┌─────────────┐   ┌──────────────────┐    │
                    │  │ Task        │   │ Subagent Registry │    │
                    │  │ Dispatcher  │──▶│ (SQLite)           │    │
                    │  │ (concurrent)│   │ id/status/kind/    │    │
                    │  └─────────────┘   │ capability_scope/  │    │
                    │        │           │ container_id       │    │
                    │        │           └──────────────────┘    │
                    │        ▼                                    │
                    │  ┌──────────────────────────────┐           │
                    │  │ Step-level supervision hook   │           │
                    │  │ (malformed call / exec error / │           │
                    │  │  ask_supervisor)                │           │
                    │  └──────────────────────────────┘           │
                    └───────────────────────────────────────────┘
                          │                          │
                          ▼                          ▼
              ┌───────────────────┐      ┌───────────────────────┐
              │ Ephemeral subagent │      │ Persistent subagent    │
              │ worktree + bwrap   │      │ Docker container,      │
              │ (repo-touching) OR │      │ own state DB,          │
              │ scoped tool list,  │      │ restart_count tracked, │
              │ no worktree        │      │ reconciled on boot     │
              │ (non-repo tasks)   │      └───────────────────────┘
              └───────────────────┘
```

---

## 6. Phases

Each phase is independently shippable and testable — no phase requires a later one to be useful.

### Phase 1 — Generalize delegation + capability scoping

**Goal:** `escalate_to_councilor` stops being "code edits only" and becomes a general `delegate_task` that declares what it's allowed to touch.

- Add a capability-declaration shape to the delegation request: `{intent, capabilities: [tool names / categories], needs_network: bool, needs_repo_write: bool}`.
- `councilor.py` builds the subagent's tool list and sandbox from the declaration instead of the hardcoded worktree+bwrap-no-network path:
  - `needs_repo_write: true` → existing worktree + bwrap path, unchanged.
  - `needs_repo_write: false` → no worktree; tool list is whatever subset of the existing tool modules (`gmail_tools`, `calendar_tools`, `websearch_tools`, etc.) the declaration asked for, run through `agent_loop` directly. Network allowed only if declared.
- Reuse the existing completion-envelope shape (`process_escalation` already returns a short summary, not raw output) — codify it as the contract every task type returns, not an accident of how code-edit summaries happen to look.
- `capability_registry.py`'s sensitive-path classification stays as-is for repo-write tasks; non-repo tasks get no risk classification yet (nothing to diff) — that's fine, this phase doesn't touch human review.

**Test:** dispatch a non-repo task ("look up X and summarize it") end-to-end through the new path; confirm L1's context only gets the short envelope, never raw tool output.

### Phase 2 — Concurrent dispatch + task registry

**Goal:** stop `_listen_for_requests` from serializing every request, and give L1 something to check status against.

- Replace the inline `await process_escalation(data)` (etc.) calls in `councilor.py:_listen_for_requests` with `asyncio.create_task(...)`, so one long-running task doesn't block a `consult_councilor` that comes in behind it.
- New SQLite table (same pattern as `tracked_items_repo`/`activity_repo`): `subagent_tasks(id, kind, intent, capability_scope, status, created_at, result_ref, platform, chat_id)`.
- New L1 tool: `check_task_status(task_id)` — read-only lookup against the registry, doesn't block.
- Existing `_publish_response`/`activity_repo` notification paths are unchanged; the registry is additive bookkeeping, not a new delivery mechanism.

**Test:** fire two `delegate_task` calls back to back; confirm the second starts before the first finishes, and `check_task_status` reflects both independently.

### Phase 3 — Persistent subagents

**Goal:** "monitor my task list and keep it updated" becomes a real request type, not something that requires editing `docker-compose.yml`.

- New L1 tools: `create_persistent_subagent(intent, capability_scope)`, `stop_subagent(task_id)`.
- Provisioning uses the Docker API against the same daemon the existing `email_triage`/`job_scout` containers already run on — a generic worker image, given a task directive and a capability scope, not a bespoke image per subagent.
- Each persistent subagent gets its own SQLite state file (mirrors the isolation `WorkerBase`'s consumer-group-per-stream pattern already gives the static workers), referenced from its `subagent_tasks` row.
- **Reconciliation on Councilor startup:** read the registry, and for every row marked "should be running," check `docker ps` and respawn if missing. This is the dynamic equivalent of Compose's `restart: unless-stopped` for the static services — necessary because these containers now outlive Councilor's own process, and Councilor itself has no restart supervision today (see §3).
- Hard cap on concurrent persistent subagents (config value, not enforced by infra) and a teardown path that actually stops+removes the container, not just marks the registry dead.

**Test:** create a persistent subagent, kill the Councilor process, restart it, confirm reconciliation finds the still-running container and doesn't duplicate or orphan it. Separately: confirm `stop_subagent` actually removes the container.

### Phase 4 — Step-level supervision: malformed calls & execution errors

**Goal:** a subagent's own (possibly smaller/cheaper) model doesn't have to unsupervised-guess its way through a bad tool call or a failed command — Councilor gets a shot at correcting it before the subagent's own loop reacts.

- Extend `local_agent_loop` with an optional supervision hook, following the existing `on_activity` precedent (`backend/agent/local_llm.py:253`) rather than changing its core contract:
  - On `json.JSONDecodeError` (currently silently swallowed to `{}` at line 342) — give the hook the raw arguments string and the tool schema; let it attempt a repair before falling back to `{}`.
  - On unknown tool name or a raised tool exception — give the hook the error before it's serialized into the transcript as plain text; let it decide retry / redirect / give up, rather than leaving that judgment to the subagent's own model.
- The hook itself is a cheap `consult`-shaped call from Councilor (same mechanism as `consult_councilor` today, just Councilor-to-itself), not a new model tier.
- Only wired in for subagent-spawned loops. L1's own `agent_loop` call sites are untouched — this is the §2 boundary, enforced structurally by only ever passing the hook into subagent invocations.

**Test:** deliberately break a tool call's expected schema inside a delegated task; confirm the hook fires and the task recovers instead of the subagent's model flailing on its own for the rest of its turn budget.

### Phase 5 — Scoping clarification (pause / resume)

**Goal:** a subagent that hits real ambiguity — not an error, a legitimate "I need a decision only the operator can make" — can pause and ask, instead of guessing.

- New tool bound into subagent tool lists only: `ask_supervisor(question)`. Blocks the subagent's turn.
- Councilor tries to answer from its own context first (identical to `consult_councilor`'s existing behavior). If it can't, it relays to the human via the existing `_notify` helper (Discord/Telegram — `councilor.py:236-279`) and the subagent moves to a `paused` status in the registry.
- **This needs a new primitive that doesn't exist today:** a reply from the operator has to route back to resume the *one specific* paused task, not just get logged. Extend the existing `thread_id` pattern (`esc-{timestamp}` in `process_escalation`) to persistent, registry-backed thread IDs (`sub-{task_id}`) so a Discord/Telegram reply in that thread resolves unambiguously to the waiting subagent.
- Pause has a timeout — an indefinitely paused subagent is a stuck subagent; on timeout it fails cleanly and reports back to L1 rather than hanging forever.

**Test:** delegate a task with deliberately underspecified scope; confirm it pauses, confirm an operator reply in the right thread resumes exactly that task and no other.

---

## 7. Open questions (not resolved by this document)

- Should capability grants for **persistent** subagents expire or need periodic re-approval? A one-shot task's access window is minutes; a monitor with Gmail read access sitting there for months is a materially different exposure. Not decided — flagging so Phase 3 doesn't ship without an explicit answer.
- What's the actual trigger threshold for Phase 4's "give Councilor a shot at it" — every single malformed call, or only after N consecutive failures (mirroring `WorkerBase`'s `max_retries` pattern)? Leaning toward the latter to avoid Councilor overhead on transient one-off glitches, but not settled.
- Cap value for concurrent persistent subagents (§6, Phase 3) — needs a number before Phase 3 ships, currently unset.
