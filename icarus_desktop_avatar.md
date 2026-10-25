# Architecture Design Record: Icarus Desktop Avatar (Voice + Desktop Actions)

**Document Version:** 1.2.0
**Status:** Phases 1–3 implemented on `feat/desktop-avatar` (2026-10-24); Phases 4–5 not started
**Date:** 2026-10-24
**Primary components touched:** new `desktop/` package (host daemon + renderer), `backend/routes/desktop.py` (new), `backend/agent/desktop_tools.py` (new), `backend/agent/engine.py`, `backend/agent/processor.py`, `councilor.py`, `backend/agent/delegation.py`

---

## 1. Goal

Give Icarus a physical presence on the desktop: a small always-on-top avatar that sits dormant as an orb, and on a hotkey listens, thinks, and answers out loud as a cyberpunk character with subtitles. Beyond conversation it can act on the machine: open apps, control and play music on Spotify, and hand coding tasks to Claude Code.

This is a **new platform adapter**, not a new brain. Discord and Telegram already wrap `process_message(platform, user_id, text, …)` (`backend/agent/processor.py:199`); the avatar is a third adapter whose input is speech and whose output is speech + an animated character.

---

## 2. Design principles

- **Icarus stays the brain, the avatar is eyes, ears and hands.** All reasoning goes through L1's existing loop. The desktop daemon never decides *what* to do; it only captures audio, renders state, and executes typed actions L1 asks for.
- **No raw shell, ever.** Desktop actions are a closed set of typed tools (`open_app`, `media_control`, `spotify_play`, …). There is no `run_command` exposed to the desktop surface. A misheard word can at worst open the wrong app.
- **Confirmation is enforced by the daemon, not the model.** Anything that can change files (a Claude Code edit task) requires a physical confirmation (hotkey) handled in the daemon. L1 cannot skip it by phrasing.
- **Human review stays the gate on code changes.** Claude Code edit tasks run in a git worktree on a new branch, consistent with `icarus_overhaul.md` §2. Nothing lands on a checked-out branch or merges automatically.
- **Local first.** Speech-to-text and text-to-speech run on this machine. No audio leaves the host and none is written to disk.
- **The renderer is swappable.** The daemon and the renderer talk over a small event protocol (§5.3), so the orb/character art and rendering tech can change without touching audio, actions or the Icarus link.

---

## 3. Baseline: what exists today

| Piece | Current state |
|---|---|
| Message entry point | `process_message(platform, user_id, text, chat_id, …, on_activity)` — `backend/agent/processor.py:199`. `on_activity` already streams short status strings during a turn. |
| Adapters | Discord (`backend/agent/discord_bot.py`), Telegram (`backend/routes/webhook.py`). Both are thin wrappers around `process_message`. |
| API process | `icarus-api` runs with `network_mode: host`, so it is reachable on localhost from host processes. Already serves WebSockets (`/ws/activity`, `/ws/telemetry` in `backend/routes/dashboard.py`). |
| Host-side processes | Councilor (`councilor.py`) runs on bare host with Redis at `localhost:6379`, owns the subagent task registry, and already does lifecycle notifications. |
| Tool scoping | `current_platform` ContextVar in `backend/agent/tools.py:36`; Discord server channels already get a read-only tool subset. |
| Host environment | Hyprland (Wayland), PipeWire, HyperX QuadCast S mic, RTX 4080 SUPER (~13.8/16 GB used by llama-server at idle), `spotify`, `playerctl`, `hyprctl`, `gtk-launch`, `claude` CLI, `ffmpeg` all installed. Host Python is 3.14. |

---

## 4. Non-goals

- **No general shell or computer-use control** from the avatar. Typed actions only.
- **No wake word in v1.** Hotkey first; wake word is Phase 5.
- **No always-recording mic.** The mic is opened only while the hotkey is held (or, in Phase 5, by the wake-word detector, which never ships audio anywhere).
- **No cloud speech services.** Local STT/TTS only.
- **No desktop actions from Discord/Telegram in v1.** Remote "play X on my PC" is a later, separately reviewed decision (§9).
- **No change to L1's loop or to Councilor's supervision model.** The Claude Code runner is a new Councilor task kind, not a new supervisor.

---

## 5. Target shape

### 5.1 Components

```
 Hyprland keybind (SUPER+I hold / release)
        │  icarus-desktop ctl listen-start | listen-stop | confirm | cancel
        ▼
 ┌──────────────────────────── host ─────────────────────────────┐
 │ icarus-desktop daemon (Python, own venv)                      │
 │   ├─ audio: PipeWire capture → faster-whisper (STT)           │
 │   ├─ voice: Piper (TTS) → playback, sentence by sentence      │
 │   ├─ actions: typed executor (hyprctl / playerctl / D-Bus)    │
 │   ├─ policy: allowlists + confirmation gate                   │
 │   └─ link: WebSocket → icarus-api /ws/desktop                 │
 │        ▲ unix socket (UI events)                              │
 │        ▼                                                      │
 │ avatar renderer (layer-shell overlay, transparent)            │
 │   orb ⇄ character, subtitles, HUD chips, cards                │
 │                                                               │
 │ councilor.py ── new task kind: claude_code ── `claude -p …`   │
 └───────────────────────────────────────────────────────────────┘
        ▲ WebSocket (localhost)          ▲ Redis (existing)
 ┌──── icarus-api (Docker, host network) ┴──────────────────────┐
 │ /ws/desktop → process_message(platform="desktop", …)          │
 │ desktop_tools: bound only when platform=="desktop" AND the     │
 │ calling session's socket is connected                          │
 └────────────────────────────────────────────────────────────────┘
```

### 5.2 Turn flow

1. Operator holds `SUPER+I`. Hyprland `bind` runs `icarus-desktop ctl listen-start`; the daemon opens the mic and the renderer enters `listening`: the character materializes mid-activity (§6.3).
2. Operator releases. Hyprland `bindr` runs `listen-stop`; the daemon closes the mic and transcribes with faster-whisper.
3. Daemon sends `utterance{turn_id, text}` over `/ws/desktop`. The renderer shows the transcript and switches to `thinking`.
4. `icarus-api` calls `process_message(platform="desktop", …, on_activity=…)`. Each `on_activity` status is pushed as `status{turn_id, text}` and becomes a HUD chip.
5. If L1 calls a desktop tool, the server sends `action{id, name, args}`; the daemon checks policy, runs it (asking for confirmation if required) and replies `action_result{id, ok, result}`. The tool call awaits this with a timeout (default 15 s; confirmation 30 s).
6. The final reply arrives as `reply{turn_id, text}`. The daemon splits it into sentences, synthesizes the first sentence immediately and queues the rest, and the renderer goes to `speaking`, with the eyes pulsing to the audio and subtitles.
7. After speaking, the character lingers for `DESKTOP_LINGER_SECONDS` (default 6). Pressing the hotkey again during that window is a follow-up in the same conversation; otherwise it collapses back to the orb.

`cancel` (`SUPER+Escape`) stops playback, drops pending actions, and tells the server to discard the turn's reply.

### 5.3 Protocols

**Daemon ⇄ icarus-api (`/ws/desktop`, JSON):**

| Direction | Message |
|---|---|
| → server | `hello{version, token}`, `utterance{turn_id, text}`, `action_result{id, ok, result}`, `cancel{turn_id}` |
| ← server | `status{turn_id, text}`, `action{id, name, args}`, `reply{turn_id, text}`, `error{turn_id, message}`, `task_event{task_id, kind, text}` |

The socket is localhost-only and authenticated with a shared `DESKTOP_TOKEN` from `.env`, because `icarus-api` is on the host network.

**Daemon → renderer (unix socket, newline-delimited JSON):**
`state{name}`, `ambient{heat, tasks_running, recent_task, unread}`, `activity{name}`, `mic_level{rms}`, `speech_level{rms}`, `transcript{text}`, `subtitle{text, duration_ms}`, `chip{id, text, done}`, `card{kind, title, body}`, `stamp{word, sub, color}`. The renderer derives `scorch`, poses and timing itself, so the daemon only sends facts. See §6.5 for the full state model.

The renderer is display-only and sends nothing back except `ready`.

### 5.4 Desktop tools (L1-facing)

Bound into L1's tool list only when `current_platform == "desktop"` and a daemon session is connected. Described in the system prompt the same way `delegate_task` is.

| Tool | Executes as | Gate |
|---|---|---|
| `open_app(name)` | fuzzy match against installed `.desktop` entries → `hyprctl dispatch exec "gtk-launch <id>"` | Must match an installed entry; optional `DESKTOP_APP_DENYLIST` |
| `focus_window(name)` | `hyprctl clients -j` match → `hyprctl dispatch focuswindow` | none |
| `media_control(action)` | `playerctl` (play, pause, toggle, next, previous, volume ±) | none |
| `now_playing()` | `playerctl metadata` | none |
| `spotify_play(query, kind)` | Spotify Web API search (client-credentials app key) → MPRIS `OpenUri(spotify:track:…)` over D-Bus; starts Spotify if not running | none |
| `run_claude_task(project, task, mode)` | Councilor `claude_code` task (§5.5) | `project` must be in `DESKTOP_CLAUDE_PROJECTS`; `mode="edit"` requires hotkey confirmation |

### 5.5 Claude Code tasks via Councilor

`run_claude_task` doesn't run Claude itself. After the confirmation gate it dispatches a new Councilor request type, `claude_code`, which:

- registers in the existing `subagent_tasks` registry (kind `claude_code`), so `check_task_status` works unchanged;
- runs `claude -p "<task>"` headless with streaming JSON output in the project directory. `mode="ask"` uses a read-only permission mode (answer or plan, no edits). `mode="edit"` creates a worktree on branch `claude/<task_id>` and allows edits there only. Exact CLI flags are pinned against the installed `claude` version during Phase 4;
- streams progress as `task_event`s (Redis → icarus-api → `/ws/desktop`), shown as HUD chips;
- on completion returns the standard `CompletionEnvelope` (`backend/agent/delegation.py`). The avatar speaks the summary, and if the daemon is offline it falls back to `_notify` (Discord/Telegram) like other tasks;
- is bounded by `CLAUDE_TASK_TIMEOUT_MINUTES` and a concurrency cap of 1 by default.

Edit tasks leave a branch for review and never merge or push without being asked.

### 5.6 Voice-mode prompt

When `platform == "desktop"`, `build_system_prompt` adds a short voice section: answer in one to three short spoken sentences, no markdown, code or URLs read aloud. If the answer needs more, say so and put the detail in a `card` (Phase 3 adds an `show_on_screen(title, body)` tool for this). `soul.md` stays the single source of personality.

---

## 6. Visual design

Chosen 2026-10-24 after prototyping five directions (reference prototype: `icarus_desktop_avatar_prototype.html`, open it in a browser). This section describes the starting point, and it's expected to change as the build goes.

### 6.1 Principles

- **Minimal and graphic, in the spirit of Marathon (Bungie).** One strong silhouette in flat black and one signal color. Detail lives in graphic-design elements around the character, not on him.
- **Composure over motion.** He mostly holds still. The eyes do the acting. Glitches, wing openings and impact moments are rare enough to count.
- **What you see is real state.** Every visual (sun heat, what he's doing when you call, the wings, the stamps) maps to something actually happening in Icarus (§6.5). Nothing is decoration-only.
- **Motion "on twos".** Poses update at 12 fps, so movement reads as drawn rather than tweened. Effects (glitch, particles, scanlines) run at full frame rate.

### 6.2 The character

| Element | Design |
|---|---|
| Silhouette | One flat black (`#030406`) angular shape: a pointed hood flowing straight into the shoulders, with a 1 px outline in the sun's color. No seams, straps or face detail. |
| Eyes | Two slanted red slits (`rgb(255,70,120)` with glow), the only facial feature. They brighten while listening, pulse with speech, narrow and drop while thinking, blink occasionally, and shift sideways when he turns. When he's turned away, the far eye dims and shortens. |
| Sun | A flat disc behind his head with bold horizontal cut stripes and a slowly rotating ring of tick marks. Its color follows GPU load: cool white, then amber, then red. |
| Wings | Four flat black blades per side, rooted behind the shoulders. Folded (nearly vertical, tips above the hood) by default; they open only for big moments (§6.4). |
| Frame graphics | A vertical `ICARUS` label in condensed display type, registration marks at the corners, a barcode, the current state name, a 10-segment heat gauge and an integrity readout. These carry status information so the character stays clean. |
| Hologram treatment | Scanlines over the character only, a constant 1–2 px RGB split, and aggressive glitching: slice displacement plus occasional whole-frame jolts. |
| Dormant orb | A small sun-colored disc with the two red eye slits, about 35% opacity, breathing slowly. A magenta dot orbits it when there are unread notifications. |

Type: Chakra Petch (display, labels), JetBrains Mono (subtitles, data). Colors: cyan `#00F0FF` for UI and chips, magenta `#FF2A6D` for alerts, amber `#FFB000` for confirmation, ember `rgb(255,122,40)` for heat.

### 6.3 Answering like a holo call

Calling Icarus should feel like interrupting someone who was busy, not opening an app (inspired by Cyberpunk 2077's holo calls).

1. On hotkey press, he materializes immediately (a scanline sweep up from the orb), already mid-activity (§6.5).
2. He holds that pose for about 0.8 s, then turns to face you in stepped frames while you're still talking. On the first turn of a fresh call, the wings snap open once and fold again.
3. While thinking, he glances aside and down. While speaking, he faces you and glances off to the side every 4–7 s.
4. After replying, he lingers about 5 s while slowly turning back to his activity, then de-rezzes into the orb.

There is no back-turned pose; it doesn't work with this art style. The most turned-away pose is three-quarters.

### 6.4 Impact moments ("stamps")

These replace full-screen cut-ins, which read as Ace Attorney rather than cyberpunk. Each one is:

1. A **freeze**: the hologram holds on one frame for about 140 ms.
2. A **flash**: the character renders as a flat silhouette in the signal color for two frames.
3. A **stamp**: a large condensed word cuts in beside him, with a small code line above it (registration mark + context) and a bar that fills in steps. It holds 1.3–1.6 s.

| Stamp | Color | Trigger | Wings |
|---|---|---|---|
| `AUTHORIZE` | amber | a typed action needs confirmation (e.g. a Claude edit task) | open to 70% and hold until answered |
| `COMPLETE` | cyan | a delegated or Claude task finishes while he's on screen | fully open for about 1.8 s |
| `RUNNING HOT` | ember | heat crosses the high threshold, once per heat cycle | no change |
| `FAILED` | magenta | a task or action fails | no change, heavy glitch |

### 6.5 State model

The renderer is driven by one state machine plus a few continuous values. The daemon owns all of it and pushes it to the renderer (§5.3).

**Conversation state** (exclusive):

| State | Source | Visual |
|---|---|---|
| `dormant` | no active turn | orb only |
| `connecting` | Icarus-initiated speech (proactive notification read aloud, Phase 5) | character mid-activity, no transcript |
| `listening` | hotkey held | orb waveform from real mic level; character mid-activity, then turns to you |
| `thinking` | utterance sent, no reply yet | eyes narrowed and lowered; `status` messages from `on_activity` become chips |
| `acting` | a desktop `action` is executing | action card; eyes lowered |
| `confirm` | an action awaiting operator confirmation | `AUTHORIZE` stamp, amber card, wings partly open |
| `speaking` | TTS playing | faces you; eyes and orb ring pulse with audio; subtitles |
| `linger` | reply finished, follow-up window open | slow turn back to activity |
| `error` | turn failed (API down, STT failure, action error) | `FAILED` stamp, one-line subtitle explaining it |

**Ambient state** (continuous, read regardless of conversation state):

| Value | Source | Drives |
|---|---|---|
| `heat` (0–1) | GPU load from the telemetry sidecar (`/ws/telemetry`) | sun color, heat gauge, scorch, `RUNNING HOT` |
| `scorch` (0–1) | integrates `heat` above 0.55 over time; recovers slowly below it | ember line on the silhouette edge, ember wing tips, ash, integrity readout |
| `tasks_running` | Councilor `subagent_tasks` registry (running count) | activity selection; small task count in the frame |
| `recent_task` | a task finished within the last N minutes | activity selection |
| `unread` | `notification_repo` undelivered or unacknowledged items | orb satellite dot; activity selection |
| `mic_level` | daemon audio capture | listening waveform |

**Activity** (what you catch him doing; chosen when a call starts):

| Activity | Chosen when | Pose |
|---|---|---|
| `venting` | `heat` > 0.6 | turned away; vapor puffs off the collar |
| `working` | `tasks_running` > 0, `unread`, or `recent_task` | three-quarters turned, eyes down, thin cyan screen line at chest |
| `watching` | otherwise | three-quarters turned toward the sun |

### 6.6 Voice

Kokoro `am_onyx`, chosen 2026-10-24 from an audition of 16 local voices (Kokoro and Piper). It renders about 0.5 s per short sentence on CPU, and speech is pipelined sentence by sentence. Piper `en_US-ryan-high` remains the fallback engine. An optional "construct" filter (`DESKTOP_VOICE_FX`, 0–1: chorus plus a faint ring mod) is available but off.

---

## 7. Phases

Each phase is shippable and testable on its own.

### Phase 1 — Voice loop with the orb

**Goal:** hold a hotkey, speak, hear Icarus answer. Orb only, no character yet.

- `desktop/` package: daemon with `ctl` subcommands over a unix socket; PipeWire capture; faster-whisper (`small`, int8; GPU if memory allows, CPU fallback); Piper TTS with sentence queueing; `cancel`.
- `backend/routes/desktop.py`: `/ws/desktop` with token auth, `process_message(platform="desktop")`, `on_activity` → `status`.
- Voice-mode section in `build_system_prompt` for `platform == "desktop"`.
- Renderer spike: transparent layer-shell overlay on the Hyprland `overlay` layer with an empty input region, drawing the orb in DORMANT / LISTENING / THINKING / SPEAKING and a plain subtitle line. Decided: see §8.
- Hyprland config snippet: `bind = SUPER, I, exec, icarus-desktop ctl listen-start`, `bindr = SUPER, I, exec, icarus-desktop ctl listen-stop`, `bind = SUPER, Escape, exec, icarus-desktop ctl cancel`.
- `systemd --user` unit for the daemon.

**Test:** protocol unit tests with `backend/tests/fake_llm.py` (utterance → reply, status forwarding, cancel discards reply, bad token rejected). Manual: hotkey round trip, orb stays above a fullscreen window and never steals clicks while dormant.

### Phase 2 — The character

**Goal:** the character as described in §6, driven by the real state model (§6.5).

- Port the prototype's stripped renderer (silhouette, eyes, flat sun, blade wings, frame graphics, hologram treatment) into the chosen renderer stack.
- Holo-call answering (§6.3), stamps (§6.4), scorch, subtitles paced per sentence, chips.
- Daemon feeds ambient state: GPU load from the telemetry sidecar, running and recent tasks from the Councilor registry, unread items from `notification_repo`.
- No external art needed: the character is fully procedural.

**Test:** a scripted renderer demo that cycles every state and activity from a recorded event file, so visuals can be checked without the LLM.

### Phase 3 — Desktop actions

**Goal:** "open Firefox", "pause", "play X on Spotify".

- `backend/agent/desktop_tools.py` (L1 side: send `action`, await `action_result`, timeout) and the daemon's typed executor + policy.
- `open_app`, `focus_window`, `media_control`, `now_playing`, `spotify_play`, `show_on_screen`.
- Spotify developer app key in `.env` (`SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`).

**Test:** policy unit tests (unknown app rejected, no tool reachable when platform ≠ desktop or socket disconnected, action timeout returns a clean error to L1). Manual: each action end to end.

### Phase 4 — Claude Code orchestration

**Goal:** "have Claude look into the failing test in Icarus" from the avatar.

- `run_claude_task` tool, confirmation gate in the daemon (CONFIRM state), Councilor `claude_code` task kind, worktree for edit mode, streamed `task_event`s, envelope on completion, `_notify` fallback.
- `DESKTOP_CLAUDE_PROJECTS` allowlist, e.g. `icarus=/home/diizzy/Projects/Icarus`.

**Test:** Councilor test with a fake `claude` executable (stream-json fixture) covering success, failure, timeout and registry status; confirm an edit-mode task without confirmation never dispatches.

### Phase 5 — Polish (each item optional)

- Wake word (openWakeWord; a custom "hey Icarus" model needs training).
- Kokoro voice.
- A brief "connecting" flicker before he appears, like a Cyberpunk holo call's static frame.
- Desktop actions reachable from Discord/Telegram ("play X on my PC"), only after the decision in §9.

---

## 8. Implementation notes (Phases 1–3)

Decisions made while building, where reality differed from the plan above:

- **Renderer stack: GTK3 + gtk-layer-shell + Cairo**, on the system Python. Neither WebKitGTK nor gtk4-layer-shell is installed and they can't be added without root, so the WebView option was out. Cairo's API is close enough to canvas that the stripped prototype ported directly (`desktop/renderer/avatar.py`). Cairo has no blur, so glows are layered strokes. The overlay is a 960×600 surface on the `overlay` layer anchored bottom-right with an empty input region, so it never takes clicks.
- **Two processes, two Pythons.** The daemon runs in `desktop/.venv` (Python 3.12, uv-managed) for faster-whisper and Piper wheels; the overlay runs on system Python 3.14 for the GObject bindings. The daemon spawns and restarts the overlay.
- **Audio** goes through `pw-record --raw` / `pw-play --raw`; `parecord` returned silence from the QuadCast on this machine.
- **Speech-to-text** defaults to `small.en` on CPU (int8). llama-server leaves too little VRAM to share safely, and CPU is fast enough for short commands.
- **Ambient state** is sent by icarus-api every 3 s on the same socket: GPU load from `icarus:metrics:latest` (the telemetry sidecar), unread = notifications under 6 h old with no reply (counting every unanswered notification gave 20 permanent "unread"), and running/recent tasks from the Councilor registry file.
- **Desktop tools** are bound in `engine.run_icarus` only when `current_platform == "desktop"` and a daemon session is live; the voice-mode prompt is added for every desktop turn. Without Spotify API keys, `spotify_play` opens a Spotify search instead of failing.
- **Not done yet:** the `confirm` control command is reserved for Phase 4 (nothing in Phases 1–3 needs confirmation). The thinking-state heat is real: GPU load spikes during every LLM turn, so the sun reddens and scorch builds on long answers. The thresholds need tuning on daily use.

## 9. Open questions

- **GPU speech-to-text.** CPU is fine for now; moving Whisper to CUDA needs VRAM headroom llama-server doesn't currently leave (about 1.9 GB free when measured).
- **Visual tuning.** Scorch rate, glitch frequency and stamp timing are first guesses from the prototype and need tuning on the real desktop.
- **Remote desktop actions.** Should Discord/Telegram be allowed to trigger desktop tools? It's convenient, but it means a compromised chat account can act on the PC. Deferred; not in v1.
- **Hotkey semantics.** Hold-to-talk (`bind` + `bindr`) is the plan; a tap-to-toggle fallback may be needed if release events prove unreliable.
