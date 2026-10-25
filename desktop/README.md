# Icarus desktop avatar

Voice and hands for Icarus on the desktop: hold a hotkey, talk, and Icarus answers out loud as the avatar in the bottom-right corner. It can open apps, focus windows, control media and play things on Spotify. Design: `icarus_desktop_avatar.md` in the repo root.

```
desktop/
  icarus_desktop/   daemon (Python 3.12 venv): mic, speech-to-text, voice, actions, link to icarus-api
  renderer/         avatar overlay (system Python: GTK3 + gtk-layer-shell + Cairo), display only
  bin/icarus-desktop  wrapper used by Hyprland binds and systemd
  systemd/          user unit
  tests/            daemon tests
```

## Setup

1. **Daemon venv** (already created on this machine):
   ```sh
   uv venv desktop/.venv --python 3.12
   uv pip install --python desktop/.venv/bin/python -r desktop/requirements.txt
   ```
   The overlay uses the system Python and needs `python-gobject`, `python-cairo` and `gtk-layer-shell` (GTK3) from pacman.

2. **Voice:** download a Piper voice into `desktop/voices/` (ignored by git). The default is `en_US-ryan-high`:
   ```sh
   cd desktop/voices
   for f in en_US-ryan-high.onnx en_US-ryan-high.onnx.json; do
     curl -LO "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/ryan/high/$f"; done
   ```
   The speech-to-text model (`small.en`) downloads itself on first start.

3. **Token:** set `DESKTOP_TOKEN` in the repo `.env` to a long random string. Both icarus-api and the daemon read it, and icarus-api refuses the desktop link without it.

4. **Restart icarus-api** so it loads the new route and the token. Use `--no-deps`, or compose also recreates Redis and wipes session state:
   ```sh
   docker compose up -d --no-deps icarus-api
   ```

5. **Hyprland binds** (`~/.config/hypr/hyprland.conf`): hold SUPER+I to talk, SUPER+Escape to cancel.
   ```
   bind  = SUPER, I, exec, ~/Projects/Icarus/desktop/bin/icarus-desktop ctl listen-start
   bindr = SUPER, I, exec, ~/Projects/Icarus/desktop/bin/icarus-desktop ctl listen-stop
   bind  = SUPER, Escape, exec, ~/Projects/Icarus/desktop/bin/icarus-desktop ctl cancel
   ```
   If release events ever misbehave, bind `ctl toggle` to a single key instead (tap to start, tap to send).

6. **Run it:**
   ```sh
   desktop/bin/icarus-desktop run            # foreground, spawns the overlay
   # or as a user service:
   cp desktop/systemd/icarus-desktop.service ~/.config/systemd/user/
   systemctl --user enable --now icarus-desktop
   ```
   The user service needs the Wayland session environment. If it can't open the overlay, add `exec-once = dbus-update-activation-environment --systemd WAYLAND_DISPLAY HYPRLAND_INSTANCE_SIGNATURE XDG_CURRENT_DESKTOP` to the Hyprland config.

## Trying it without a mic

```sh
desktop/bin/icarus-desktop say "what's playing right now?"
desktop/bin/icarus-desktop ctl status
python3 desktop/renderer/avatar.py --demo     # overlay only, scripted walk through every state
```

## Settings (repo `.env`)

| Variable | Default | |
|---|---|---|
| `DESKTOP_TOKEN` | (required) | shared secret for `/ws/desktop` |
| `DESKTOP_API_URL` | `ws://127.0.0.1:8000/ws/desktop` | |
| `DESKTOP_USER_ID` | `operator` | user id for transcripts and memory scoping (icarus-api side) |
| `DESKTOP_OUTPUT` | compositor's choice | Hyprland output for the overlay, e.g. `DP-2` (`DESKTOP_MONITOR` takes a GDK index instead) |
| `DESKTOP_WHISPER_MODEL` / `_DEVICE` / `_COMPUTE` | `small.en` / `cpu` / `int8` | `cuda` works but competes with llama-server for VRAM |
| `DESKTOP_PIPER_VOICE` | `desktop/voices/en_US-ryan-high.onnx` | |
| `DESKTOP_LINGER_SECONDS` | `6` | how long he stays after answering |
| `DESKTOP_APP_DENYLIST` | empty | comma-separated app names `open_app` must refuse |
| `DESKTOP_UNREAD_WINDOW_HOURS` | `6` | unanswered notifications younger than this count as unread |
| `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` | empty | from a free app at developer.spotify.com; without them "play X" opens a Spotify search instead of playing |

## What Icarus can do from here

Typed actions only, never a shell: `open_app` (installed, visible `.desktop` entries only, launched via `hyprctl dispatch exec gtk-launch`), `focus_window`, `media_control`, `now_playing`, `spotify_play`, `show_on_screen`. These tools exist only in desktop-platform turns, and only while the daemon is connected.

## Tests

```sh
PYTHONPATH=desktop desktop/.venv/bin/python -m unittest discover -s desktop/tests
docker exec -w /app icarus-api python -m unittest backend.tests.test_desktop
```
