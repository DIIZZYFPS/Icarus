# Architecture Design Record: Icarus Executor Routing with a Local Decision Model

**Document Version:** 1.0.0
**Status:** Proposed. Not implemented. The gating benchmark (§9) has not been run.
**Date:** 2026-10-25
**Builds on:** `icarus_subagent_delegation.md` (subagent delegation and supervision, v3.0.0) and `icarus_desktop_avatar.md` (§5.5 Claude Code tasks)
**Primary components touched:** `councilor.py`, `backend/agent/delegation.py`, `backend/agent/llm_router.py`, `backend/agent/desktop_tools.py`, `backend/agent/claude_runner.py`, `backend/agent/subagent_registry.py`

---

## 1. Why this exists

Delegation, as built, does not add capability. A "subagent" is the same Qwen3.6-35B that L1 runs on, given a narrower tool list and an empty context. Every LLM call in the system (L1 chat, each delegation loop, supervision consults, email scoring, persistent workers) goes to the one llama-server on `:8080`, which runs `--parallel 1`. Delegating a task therefore buys isolation and context hygiene, but it adds queueing on a single slot. More local agents means more waiting, not more throughput.

The original Councilor design had a tiered routing idea: pick the right brain for the task. It was dropped when the Google dependencies were removed, because the only other tier was a cloud model. That reason is gone. There are now several executors on the same machine with different cost and capability profiles, and a new class of small open-weight **decision models** that choose among options in one forward pass without generating text.

This ADR proposes using one of those as a router that picks the executor for a delegated task.

## 2. Decision

Add a **local decision model** (JevK5, GGUF, on `llama-server`) as a router stage in the Councilor. For each delegated task it answers a fixed set of typed questions and a deterministic policy layer turns the answers into an executor choice.

- The router **advises L1's delegation**. It does not pre-empt L1. L1 still decides that a task should be delegated (§2 of `icarus_subagent_delegation.md` stays true); the router decides which executor runs it.
- The router is **local-only**. Task text never goes to a hosted decision API.
- A **deterministic gate** runs before the model and has the final say on privacy. The model refines choices inside the gate; it never widens it.
- The router ships in **shadow mode** first and is promoted one decision at a time, only after §9's acceptance criteria are met on this hardware.

## 3. Baseline: what exists today

Verified against the repository and the running machine on 2026-10-25.

| Piece | Current state |
|---|---|
| L1 | `backend/agent/engine.py`, one prompt, about 25 tools, 15-turn loop on the local 35B. L1 picks `delegate_task`, `escalate_to_councilor`, `consult_councilor` or `run_claude_task` by reading the prompt. |
| Desktop routing | `desktop_tools.py` hardcodes "coding work goes to `run_claude_task`, not `escalate_to_councilor` or `delegate_task`". |
| Councilor dispatch | `_handle_request` switches on request type: `consultation`, `delegation`, `escalation`, `subagent_create`, `subagent_stop`, `claude_code`, `deploy_check`, `deploy_apply`. Handlers run concurrently as tasks. |
| Capability declaration | L1's model chooses the capability list and the `needs_network` / `needs_repo_write` flags. `validate_declaration` rejects only unknown names and undeclared network use. |
| Cloud executor | Claude Code, headless, via `claude_runner.py` (`ask` read-only, `edit` in a worktree). Edit mode asks the operator to confirm first. |
| Other executors | Codex is not wired in anywhere. Hermes is a separate project; the repo only mentions it in a comment in `websearch_tools.py`. |
| Model routing | `llm_router.ROUTING_TABLE` maps every task type to `"local"`. |
| Outcome data | `workspace/memory/councilor.db`, table `subagent_tasks`: intent, capability scope, status, timestamps, result. 28 rows at the time of writing, of which about 10 are test pings. |
| Host | RTX 4080 Super, about 2.7 GB VRAM free; 24 CPU threads, of which the two 35B servers use 4 and 12; about 1 GB RAM free and about 26 GB reclaimable. |

## 4. Non-goals

- **No hosted decision API.** TypeSafe's Jev appears to be hosted-only (API key, per-token pricing, no stated open weights or retention terms). Sending email or repo content to it would break the local-first invariant.
- **No LLM-generated plan for every request.** Generating a DAG on the contended 35B costs the seconds this ADR is trying to save. Plans come from templates; the LLM only composes a plan for genuinely multi-step tasks (§5.5).
- **No change to the sandbox boundary.** `needs_repo_write` still means worktree plus bwrap, PR-only landing, human merge. Claude edit mode keeps its confirm step.
- **No auto-merge, and no router authority over confirmation.** Routing a task to an executor never skips an existing operator confirmation.
- **No router in L1's own loop.** L1's tool choice is untouched. The router sits behind the delegation tools.

## 5. Design

### 5.1 Shape

```
  L1 decides "delegate this"                      (ADR v3 §2 preserved: L1 chooses, nothing pre-empts it)
        │  intent (+ plan template or composed DAG for multi-step work)
        ▼
  ┌────────────────────── Router (new, Councilor side, before _handle_request) ──────────────────────┐
  │ 1. Deterministic gate                                                                            │
  │      capability in {gmail_read, calendar_read, tracked_items} → executor must be LOCAL           │
  │      executor availability (daemon connected? claude on PATH? codex/hermes present?)             │
  │      daily cloud budget not exhausted                                                            │
  │ 2. Decision model (JevK5, local, one batch of parallel questions per node)                       │
  │      Choice  executor    {direct_answer, local_subagent, claude_ask, claude_edit, ask_operator}  │
  │      Noul    needs_network, private_data, repo_write                                             │
  │      Noul    one per capability in the catalog (replaces L1 guessing the list)                   │
  │ 3. Policy                                                                                        │
  │      low confidence        → local_subagent, or ask_operator via the existing pause/resume path  │
  │      cloud executor chosen and P(private) ≥ 0.2 → downgrade to local (asymmetric on purpose)     │
  │ 4. Record decision + later outcome in councilor.db                                               │
  └──────────────────────────────────────────────────────────────────────────────────────────────────┘
        ▼
  direct_answer │ local_subagent (existing) │ claude_code ask|edit (existing) │ codex │ hermes
  a node sent to anything on the right frees the single llama slot for L1 chat and voice
```

### 5.2 The decision model

JevK5 (community fine-tune of Qwen3.5, Apache-2.0, not affiliated with TypeSafe) is the first candidate. It reads a document plus a yes/no, choice or score question and returns a probability per option from next-token log-probabilities, generating nothing.

- **Runtime:** `llama-server`, the same runtime already in use. Only `llama-server` is documented as exposing the log-probabilities the calibrated readout needs.
- **Candidate files:** `jevk5-4b-v0.3-Q4_K_M` (2.71 GB; temperature 1.22) first, `jevk5-2b-v0.2-Q8_0` (2.01 GB; temperature 1.42) as the low-latency alternative. Each file has its own calibration temperature; the table is on the model card.
- **Placement:** a dedicated CPU instance on its own port with its own slots (`--parallel 4`, about 4 threads, `-ngl 0`). VRAM headroom on this machine (about 2.7 GB) is too small for the 4B alongside the 35B and its context, and a separate instance means a routing decision never queues behind the 35B.
- **Vendor-reported figures (unverified here):** about 0.57 s per 4B decision on a 48-thread CPU, 0.23 to 0.27 s for the 2B, and the 4B Q4_K_M matching the unquantized model on 221 of 231 public JevBench items. These come from the author's own runs, not from JevBench's official results, on machines with more free cores than this one. §9 exists to replace them with local measurements.
- **Not chosen:** `samatv256/mini-Jev` (0.6B). It has no GGUF, needs a CUDA GPU with BF16 and its own loader, documents only `choice` questions, and its author reports 34% on the full typed-decisions test (Choice 27%, Score 25.6%) with probabilities that are not calibrated outside its training domains. It is an early checkpoint (step 10,626 of 50,000). Revisit after its training completes, and only for step-level tool picking inside a subagent, not for routing.
- **Not chosen:** TypeSafe's hosted Jev, for the privacy reason in §4.

### 5.3 Executor registry

A small table, one row per executor, so adding Codex or Hermes is a row plus a runner:

| Field | Meaning |
|---|---|
| `id` | `local_subagent`, `claude_ask`, `claude_edit`, `codex_*`, `hermes_*` |
| `privacy_class` | `local` or `cloud`. Cloud executors may only receive tasks that pass the gate and the privacy policy. |
| `runner` | Existing callable: `process_delegation`, `run_claude`, or a new runner with the same `ClaudeResult`-shaped contract |
| `needs_confirm` | Whether the operator confirms first (true for `claude_edit`) |
| `cost_class`, `typical_latency` | Inputs to the budget check and to the shadow-mode report |

Hermes and Codex are listed because they run on the same device, but neither is integrated today. Their runners are Phase 2 work and need an interface check first (§8).

### 5.4 Capability declaration

Today L1's model guesses the capability list and network flag, and a wrong guess fails minutes later in the Councilor. The router asks one Noul per catalog capability (`web`, `gmail_read`, `calendar_read`, `github_read`, `github_write`, `telemetry`, `tracked_items`, `time`) in the same parallel batch. The router's list is compared with L1's. Disagreements are logged in shadow mode and, once trusted, resolved toward the **narrower** grant, never the wider one.

### 5.5 Plans

Most requests are one node. For multi-step work, a plan is a small DAG whose nodes each carry an intent and an optional capability hint; the router chooses an executor per node.

- **Templates first:** a short, hand-written list (for example: research then summarize; inspect code then edit; check mail then notify). Selecting one is itself a Choice question.
- **Composed plans only on demand:** the 35B composes a DAG only when no template fits, with a hard cap on nodes, and the composition cost is logged so the trade-off stays visible.
- **Privacy flows along edges:** a node that consumes the output of a local-only node inherits its restriction. A cloud executor never receives data derived from a local-only capability.

### 5.6 Privacy policy

The privacy rule is deliberately **not** a model decision on its own.

1. Capabilities that touch the operator's mail, calendar or tracked items are local-only. Fixed in code, evaluated before the model runs.
2. The model's `private_data` probability is a second line for tasks that name personal data without declaring a capability. Threshold is asymmetric: P(private) ≥ 0.2 blocks a cloud executor, while the same score never forces a task to a cloud executor.
3. Claude Code already receives repository contents when the operator asks for it; routing does not change that exposure, and `claude_edit` keeps its confirmation.

### 5.7 Fallback

If the decision server is down, slow or returns low confidence, the router degrades to **today's behaviour**: the executor L1 asked for runs unchanged. A router fault must never block a task.

## 6. Phases

Each phase is independently shippable.

### Phase 0: benchmark (gate for everything below)
Run the benchmark in §9 on this machine. If it fails the acceptance criteria, stop; nothing else in this ADR is worth building on that model.

### Phase 1: shadow mode
- Stand up the decision server as a managed process (user systemd unit or a Councilor-supervised child; not a compose service, to avoid recreating Redis, see the compose Redis note in project memory).
- Add `decision_log` to `councilor.db`: task id, router inputs (intent hash and a length-capped excerpt), per-question probabilities, the router's would-be executor and flags, what actually ran, and the outcome joined from `subagent_tasks`.
- No behaviour changes. A report compares the router with what L1 chose and with outcomes.

**Test:** replay the existing registry rows through the router offline; confirm decision rows are written for live tasks and that disabling the router leaves dispatch byte-identical.

### Phase 2: executor registry and extra runners
- Introduce the registry (§5.3) around the existing `process_delegation` and `process_claude_code`.
- Add Codex and Hermes runners behind the same interface once their headless interfaces are confirmed.

**Test:** each runner returns a `CompletionEnvelope`; a cloud executor is refused for a task carrying a local-only capability.

### Phase 3: promote decisions one at a time
1. Capability and network flags (lowest risk; narrower-grant rule).
2. Local versus Claude for code tasks.
3. Plan templates and per-node routing.

Each promotion is behind its own flag in `.env.example`, with the shadow report as the evidence.

### Phase 4: remove hardcoded routing
Replace the "coding goes to `run_claude_task`" sentence in `desktop_tools.py` and the routing prose in L1's prompt with the router's output, shrinking the prompt L1 reads on every turn.

## 7. Risks

| Risk | Mitigation |
|---|---|
| Model is a one-person fine-tune; accuracy may not hold on Icarus traffic | §9 gate; shadow mode; LLM-free fallback (§5.7) |
| Vendor benchmark figures are self-reported and measured on bigger CPUs | Treat them as hypotheses; measure latency here under real contention |
| Confidence values can drift between runs, and calibration may not transfer to this domain | Threshold from measured buckets, not from the model card; re-check after any model or quant change |
| Router mistakes route private data to a cloud executor | Gate in code first; asymmetric privacy threshold; privacy flows along DAG edges |
| Extra process competes for RAM and CPU with the 35B servers | Dedicated small instance, 4 threads, `--parallel 4`; measure 35B tokens/s with and without it |
| Dataset is tiny (28 registry rows) | Labelled set in §9 is hand-extended; shadow mode accumulates real rows before any promotion |
| Hidden coupling: router becomes a second place that must know the capability catalog | Generate the question set from `CAPABILITY_CATALOG`, not a copy |

## 8. Open questions

- Which runtime interface do Codex and Hermes expose for headless, non-interactive use on this machine? Not verified here.
- Should the router also see recent conversation context, or only the intent text? More context helps ambiguity detection but costs latency and widens what the model reads.
- What daily budget, if any, for cloud executors, and where is spend measured? `ClaudeResult.cost_usd` exists for Claude only.
- Where should the decision server live long term (user systemd unit versus Councilor child)?
- Is a Score question worth adding (difficulty or ambiguity)? Not covered by §9's first pass.

## 9. Benchmark (Phase 0 gate)

**Status: not run.** An attempt on 2026-10-25 was lost when the machine rebooted partway through (scratchpad and background processes gone); no results exist. Treat every JevK5 figure above as unverified.

**Setup:** JevK5 4B v0.3 Q4_K_M and 2B v0.2 Q8_0 (verify SHA-256 against the repository's `SHA256SUMS`), each on a separate CPU-only `llama-server` instance, `-c 4096 --parallel 4 --threads 4`, using the model card's standalone client and per-file temperature. Run while the 35B servers are in their normal state.

**Test set (about 57 items):**
- about 17 taken from real registry intents, labelled by hand (corrected where L1's recorded choice looked wrong or the intent was ambiguous);
- about 40 hand-written items covering direct answers, mail/calendar/web lookups, read-only code investigation, code edits, and vague requests.

**Questions per item:** executor Choice over `{direct_answer, local_subagent, claude_ask, claude_edit, ask_operator}`, plus Noul for `needs_network`, `private_data`, `repo_write`, asked in parallel.

**Acceptance criteria (proposed; adjust before running):**

| Measure | Required |
|---|---|
| Executor top-1 accuracy | ≥ 0.85 overall |
| Executor accuracy where confidence ≥ 0.9 | ≥ 0.95 |
| `private_data` false negatives on tasks that involve personal data | 0 |
| `repo_write` false negatives | 0 |
| p90 wall time for one routing decision (4 parallel questions) | ≤ 1.5 s with the 35B servers active |
| Impact on 35B generation speed while the router runs | no more than a 5% drop |

**Not covered by this pass:** Score questions, long inputs, adversarial text embedded in task content (for example text copied from a web page that tries to steer the router). Prompt-injection behaviour of the decision model needs its own test before any web-derived text is routed.

## 10. Sources

- JevK5 GGUF model card (author-reported figures): https://huggingface.co/alibiserikbay/JevK5-GGUF
- mini-Jev model card: https://huggingface.co/samatv256/mini-Jev
- TypeSafe Jev with LangChain: https://www.langchain.com/blog/building-a-harness-with-jev
- Jev coverage and analyst caveats: https://www.techtarget.com/it-infrastructure/news/366650696/Jev-decision-model-touted-as-quicker-cheaper-LLM-alternative
- Jev router shadow-mode test by a third party: https://mindshub.ai/blog/jev-decision-model-agent-routing
