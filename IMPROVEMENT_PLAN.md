# Icarus Modernization Plan

Drafted 2026-07-18 from a codebase review. Context: long-term goal is a
DAEX ⇄ Icarus "uplink" — one agent identity across phone and desktop, where
DAEX escalates to the desktop's compute when reachable (local-first remote
inference + cross-device tool/memory federation).

## Current state (what the review found)

- Event-driven platform, not a conversational one: entry points are the
  Discord bot and Gmail watcher; work flows through Redis streams into
  triage/priority workers.
- Core agent: Google ADK + LiteLLM → Ollama `icarus-qwen` (Qwen 3.5 9B),
  model name hardcoded in `backend/agent/engine.py`.
- `backend/agent/llm_router.py` routes consultation/scoring to
  `gemma-3-27b-it` and escalation to `gemini-3.1-flash-lite-preview` via the
  Google cloud API — cloud-dependent L2, including a preview model that has
  likely been renamed/deprecated.
- C++ telemetry sidecar → Redis → metrics endpoints works.
- Councilor autonomously opens PRs (see merged `councilor/intent-*` branches).
- API surface is only `/health`, `/metrics/*`, webhook. **No chat endpoint,
  no sessions, no token streaming.**
- Cruft: `httpx` and `google-adk` each listed twice in requirements; legacy
  stream aliases in orchestrator; Ollama exposed on host port 8000 while the
  API takes 8080.

## Phases (each independently useful)

### Phase 1 — Give him a mouth (do first; unblocks everything)
- `/chat` endpoint with WebSocket or SSE token streaming.
- Session management backed by the existing Redis.
- OpenAI-compatible `/v1/chat/completions` alias so any client (including
  DAEX) can speak to him with boring, standard client code.

### Phase 2 — Local-first model refresh
- Move the routing table + engine model names into config, not code.
- L2 (27B-class) runs on local Ollama; cloud escalation becomes an explicit
  opt-in tier, not the default. Retire the preview model.
- Before choosing replacement models, check what's current — model names in
  this repo are pinned to early-2026 releases.

### Phase 3 — Capability registry
- Fold Gmail/GitHub/telemetry/ESC tools into a single declared-capability
  system so "which body can perform this action" (phone vs desktop) becomes a
  lookup. Prep for cross-device federation.

### Phase 4 — The DAEX uplink
- Pairing: QR code from desktop encoding endpoint + generated key; mutual
  auth always, even on LAN.
- Discovery: mDNS/NSD (`_icarus._tcp`), manual IP fallback.
- DAEX router chooses per-request: on-device model vs uplink (reachability,
  latency, battery, task weight). Visible state in DAEX UI
  (`CORE: LOCAL` vs `UPLINK: 27B`).
- Off-LAN later via WireGuard/Tailscale — don't build a custom relay.
- Memory sync: desktop is source of truth while uplinked; phone journals
  deltas offline and reconciles on reconnect.
- Open question (needs DaexAndroid review): is DAEX's engine layer a
  pluggable interface a "remote engine" can implement, or is LiteRT woven
  through the chat pipeline?

## Quick hygiene (anytime)
- Dedupe requirements.txt; pin or constrain versions.
- Remove legacy stream aliases once callers are confirmed migrated.
- Reconsider exposing Ollama on host port 8000 (only the API should need
  host exposure).
