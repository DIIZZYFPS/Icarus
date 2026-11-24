# Icarus

A local-first personal assistant. Everything model-related runs on the host's own `llama-server`; nothing goes to a cloud model by default.

## Parts

| Part | Where | What it does |
|---|---|---|
| **Icarus (L1)** | `icarus-api` container, `backend/` | The conversational agent: Discord, the desktop avatar, tools (`backend/agent/engine.py`, `tools.py`). Persona in `backend/agent/soul.md`. |
| **Councilor (L2)** | bare host, `councilor.py` | Consultations, delegated subagents and their supervision, repo-edit escalations (worktree + bwrap, PR only), Claude Code tasks, deploy checks. |
| **Workers** | `email_triage` container | Redis-stream consumers with retry and DLQ (`backend/agent/worker_base.py`). Gmail and calendar watchers run inside `icarus-api`. |
| **Desktop avatar** | host, `desktop/` | Hotkey voice, speech-to-text, TTS, the on-screen avatar, and desktop actions. See `desktop/README.md`. |
| **Telemetry sidecar** | host, `sidecar/` | C++ sampler publishing CPU, memory, disk and GPU load to Redis. See `sidecar/README.md`. |

State: SQLite under `workspace/memory/` (`icarus.db` is written only by `icarus-api`; `councilor.db` holds the subagent registry), Redis for streams, sessions and short-lived flags.

## Running

```sh
./start.sh                                   # docker services, then the Councilor in the foreground
docker compose up -d --no-deps icarus-api    # reload just the API (plain `up -d` recreates Redis and wipes session state)
```

Configuration is in `.env` (see `.env.example`); `LOCAL_LLM_URL` points at the local `llama-server`.

Tests are plain unittest, run from the repo root. Backend tests need the container's dependencies: `docker exec -w /app icarus-api python -m unittest discover -s backend/tests`. Desktop tests use the desktop venv: `PYTHONPATH=desktop desktop/.venv/bin/python -m unittest discover -s desktop/tests`. Tests must fake Redis; the live Councilor listens on `icarus:councilor:requests`.

## Design docs

- `icarus_subagent_delegation.md`: subagent delegation and supervision (implemented).
- `icarus_desktop_avatar.md`: the desktop avatar and voice.
- `icarus_executor_routing.md`: executor routing with a local decision model (proposed).
