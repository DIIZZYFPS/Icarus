"""
actions.py — the typed desktop actions Icarus can request, and the policy around them.

Every action is a fixed operation with validated arguments. There is no shell:
apps launch through `hyprctl dispatch exec gtk-launch <desktop-id>` and only if they
match an installed, visible .desktop entry; media goes through playerctl; Spotify
through its Web API search + MPRIS OpenUri.
"""

import asyncio
import configparser
import difflib
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from .spotify import SpotifyClient, SpotifyError

logger = logging.getLogger(__name__)


class ActionError(Exception):
    """A refused or failed action; the message goes back to Icarus verbatim."""


@dataclass
class AppEntry:
    desktop_id: str
    name: str
    generic: str
    keywords: str


def _application_dirs() -> list[Path]:
    data_home = Path(os.getenv("XDG_DATA_HOME", Path.home() / ".local/share"))
    data_dirs = os.getenv("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":")
    dirs = [data_home / "applications"] + [Path(d) / "applications" for d in data_dirs if d]
    dirs += [Path("/var/lib/flatpak/exports/share/applications"), data_home / "flatpak/exports/share/applications"]
    seen, out = set(), []
    for d in dirs:
        if d not in seen and d.is_dir():
            seen.add(d)
            out.append(d)
    return out


def scan_apps(dirs: list[Path] | None = None) -> list[AppEntry]:
    """Visible applications from .desktop files; earlier dirs win, like the spec says."""
    apps: dict[str, AppEntry] = {}
    for base in dirs if dirs is not None else _application_dirs():
        for path in sorted(base.rglob("*.desktop")):
            desktop_id = str(path.relative_to(base)).replace("/", "-")
            if desktop_id in apps:
                continue
            parser = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                parser.read(path, encoding="utf-8")
            except (configparser.Error, UnicodeDecodeError):
                continue
            if "Desktop Entry" not in parser:
                continue
            entry = parser["Desktop Entry"]
            hidden = entry.get("NoDisplay", "false").lower() == "true" or entry.get("Hidden", "false").lower() == "true"
            if entry.get("Type") != "Application" or hidden or not entry.get("Exec"):
                apps[desktop_id] = None  # shadowing entry still blocks lower-priority ones
                continue
            apps[desktop_id] = AppEntry(desktop_id, entry.get("Name", desktop_id), entry.get("GenericName", ""), entry.get("Keywords", ""))
    return [a for a in apps.values() if a]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def match_app(query: str, apps: list[AppEntry]) -> AppEntry | None:
    q = _norm(query)
    if not q:
        return None
    stem = lambda a: _norm(a.desktop_id.rsplit(".desktop", 1)[0].split(".")[-1])
    for test in (
        lambda a: _norm(a.name) == q or stem(a) == q,
        lambda a: _norm(a.name).startswith(q) or stem(a).startswith(q),
        lambda a: q in _norm(a.name) or q in stem(a),
        lambda a: q in _norm(a.generic) or q in _norm(a.keywords).split(";"),
    ):
        hits = [a for a in apps if test(a)]
        if hits:
            return min(hits, key=lambda a: len(a.name))
    names = {_norm(a.name): a for a in apps}
    close = difflib.get_close_matches(q, list(names), n=1, cutoff=.75)
    return names[close[0]] if close else None


async def run(*cmd: str, timeout: float = 8.0) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise ActionError(f"{cmd[0]} timed out")
    return proc.returncode, out.decode(errors="replace").strip()


MEDIA_COMMANDS = {
    "play": ["play"], "pause": ["pause"], "toggle": ["play-pause"], "play_pause": ["play-pause"],
    "next": ["next"], "skip": ["next"], "previous": ["previous"], "prev": ["previous"],
    "volume_up": ["volume", "0.1+"], "volume_down": ["volume", "0.1-"], "stop": ["pause"],
}
SPOTIFY_KINDS = ("track", "album", "artist", "playlist")


class Actions:
    def __init__(self, config, show_card: Callable[[dict], Awaitable[None]] | None = None,
                 spotify: SpotifyClient | None = None):
        self.config = config
        self.show_card = show_card
        self.spotify = spotify or SpotifyClient(config.spotify_client_id, config.spotify_client_secret, config.spotify_market)
        self._apps: list[AppEntry] | None = None

    def apps(self) -> list[AppEntry]:
        if self._apps is None:
            self._apps = scan_apps()
        return self._apps

    def describe(self, name: str, args: dict) -> dict:
        """The card shown while an action runs."""
        if name == "open_app":
            return {"kind": "app", "title": "Launch", "body": str(args.get("name", ""))}
        if name == "focus_window":
            return {"kind": "app", "title": "Focus", "body": str(args.get("name", ""))}
        if name in ("media_control", "now_playing"):
            return {"kind": "play", "title": "Media", "body": str(args.get("action", "now playing"))}
        if name == "spotify_play":
            return {"kind": "play", "title": "Spotify", "body": f"{args.get('query', '')} ({args.get('kind', 'track')})"}
        return {"kind": "info", "title": name.replace("_", " "), "body": ""}

    async def run(self, name: str, args: dict) -> str:
        handler = getattr(self, f"_do_{name}", None)
        if handler is None or not isinstance(args, dict):
            raise ActionError(f"unknown action {name!r}")
        return await handler(**{k: v for k, v in args.items() if isinstance(k, str)})

    # ── handlers ────────────────────────────────────────────────────────────

    async def _do_open_app(self, name: str = "", **_) -> str:
        app = match_app(str(name), self.apps())
        if app is None:
            self._apps = None  # rescan once: it may have been installed since the last scan
            app = match_app(str(name), self.apps())
        if app is None:
            raise ActionError(f"no installed app matches {name!r}")
        if _norm(app.name) in self.config.app_denylist or _norm(app.desktop_id) in self.config.app_denylist:
            raise ActionError(f"{app.name} is on the desktop deny list")
        code, out = await run("hyprctl", "dispatch", "exec", f"gtk-launch {app.desktop_id}")
        if code != 0 or "ok" not in out.lower():
            raise ActionError(f"hyprctl refused to launch {app.name}: {out[:120]}")
        return f"Opened {app.name}"

    async def _do_focus_window(self, name: str = "", **_) -> str:
        code, out = await run("hyprctl", "clients", "-j")
        if code != 0:
            raise ActionError("couldn't list windows")
        q = _norm(str(name))
        clients = [c for c in json.loads(out or "[]") if c.get("mapped", True)]
        best = None
        for c in clients:
            cls, title = _norm(c.get("class", "")), _norm(c.get("title", ""))
            if q and (cls == q or q in cls or q in title):
                if best is None or cls == q:
                    best = c
        if not best:
            raise ActionError(f"no open window matches {name!r}")
        code, out = await run("hyprctl", "dispatch", "focuswindow", f"address:{best['address']}")
        if code != 0:
            raise ActionError(f"couldn't focus it: {out[:120]}")
        return f"Focused {best.get('class') or best.get('title')}"

    async def _do_media_control(self, action: str = "", **_) -> str:
        cmd = MEDIA_COMMANDS.get(str(action).lower().replace("-", "_").replace(" ", "_"))
        if not cmd:
            raise ActionError(f"unknown media action {action!r}; use play, pause, toggle, next, previous, volume_up or volume_down")
        code, out = await run("playerctl", *cmd)
        if code != 0:
            raise ActionError("no media player is running" if "No players" in out else out[:120])
        await asyncio.sleep(.3)
        return await self._do_now_playing()

    async def _do_now_playing(self, **_) -> str:
        code, out = await run("playerctl", "metadata", "--format", "{{status}}|{{artist}}|{{title}}|{{playerName}}")
        if code != 0 or "|" not in out:
            return "Nothing is playing."
        status, artist, title, player = (out.split("|") + ["", "", "", ""])[:4]
        what = f"{title} by {artist}" if artist else title or "something"
        return f"{status}: {what} ({player})"

    async def _do_spotify_play(self, query: str = "", kind: str = "track", **_) -> str:
        kind = str(kind).lower()
        if kind not in SPOTIFY_KINDS:
            kind = "track"
        if not str(query).strip():
            raise ActionError("nothing to search for")
        await self._ensure_spotify()
        if not self.spotify.configured:
            # Without API keys, open Spotify's own search instead of playing.
            await run("playerctl", "-p", "spotify", "open", f"spotify:search:{query}")
            return f"Spotify API keys aren't set, so I opened a Spotify search for {query} instead of playing it."
        try:
            hit = await self.spotify.best_match(str(query), kind)
        except SpotifyError as e:
            raise ActionError(str(e))
        if not hit:
            raise ActionError(f"Spotify found nothing for {query!r}")
        code, out = await run("playerctl", "-p", "spotify", "open", hit["uri"])
        if code != 0:
            raise ActionError(f"Spotify wouldn't open it: {out[:120]}")
        return f"Playing {hit['label']} on Spotify"

    async def _do_show_on_screen(self, title: str = "", body: str = "", **_) -> str:
        if self.show_card:
            await self.show_card({"kind": "info", "title": str(title)[:80], "body": str(body)[:1200]})
        return "Shown on screen."

    async def _ensure_spotify(self) -> None:
        code, out = await run("playerctl", "-l")
        if "spotify" in out:
            return
        app = match_app("spotify", self.apps())
        if app is None:
            raise ActionError("Spotify isn't installed")
        await run("hyprctl", "dispatch", "exec", f"gtk-launch {app.desktop_id}")
        for _ in range(30):
            await asyncio.sleep(.5)
            code, out = await run("playerctl", "-l")
            if "spotify" in out:
                await asyncio.sleep(1.5)  # let it finish starting before OpenUri
                return
        raise ActionError("Spotify didn't start in time")
