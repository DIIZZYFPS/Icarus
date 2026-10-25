"""
desktop_tools.py — tools that act on the operator's PC through the desktop daemon.

Bound into L1's tool list only for platform="desktop" turns, and only while the
daemon is connected (see engine.run_icarus). Each tool is a typed request; the
daemon checks it against its own policy and runs it with hyprctl / playerctl /
the Spotify API. There is deliberately no shell or arbitrary-command tool here.
"""

import asyncio

from .desktop_session import DesktopUnavailable, get_active

DESKTOP_PROMPT = """

## Desktop voice mode
You are speaking out loud through DIIZZY's desktop avatar. Everything you write is converted to speech.
- Answer in one to three short spoken sentences. No markdown, lists, code, URLs, file paths or emoji.
- If the answer needs detail (code, a list, a link), call show_on_screen with it and say in a few words that it's on screen.
- Desktop tools, available only in this mode, act on DIIZZY's PC directly: open_app, focus_window, media_control, now_playing, spotify_play, show_on_screen. When asked, just do it and confirm briefly; don't narrate.
- There is no shell access from here. For anything those tools can't do, say so or delegate as you normally would.
"""


async def _call(name: str, args: dict, timeout: float | None = None) -> str:
    session = get_active()
    if session is None:
        return "The desktop daemon is not connected, so I can't act on the PC right now."
    try:
        result = await session.call(name, args, timeout)
    except asyncio.TimeoutError:
        return f"{name} timed out waiting for the desktop."
    except DesktopUnavailable as e:
        return f"Desktop unavailable: {e}"
    if result.get("ok"):
        return str(result.get("result") or "done")
    return f"{name} failed: {result.get('result')}"


async def open_app(name: str) -> str:
    """Open an installed desktop application on DIIZZY's PC by name, e.g. "firefox", "spotify", "discord", "steam". Matches against installed .desktop entries; returns which app was launched or why nothing was."""
    return await _call("open_app", {"name": name})


async def focus_window(name: str) -> str:
    """Bring an open window on DIIZZY's PC to the front, matched by its app or title, e.g. "firefox", "kitty"."""
    return await _call("focus_window", {"name": name})


async def media_control(action: str) -> str:
    """Control whatever media player is active on DIIZZY's PC. action is one of: play, pause, toggle, next, previous, volume_up, volume_down."""
    return await _call("media_control", {"action": action})


async def now_playing() -> str:
    """Report what is currently playing on DIIZZY's PC (artist, title, player, playing/paused)."""
    return await _call("now_playing", {})


async def spotify_play(query: str, kind: str = "track") -> str:
    """Search Spotify and start playing the best match on DIIZZY's PC. kind is one of: track, album, artist, playlist. For "play something by X" use kind="artist"."""
    return await _call("spotify_play", {"query": query, "kind": kind}, timeout=25)


async def show_on_screen(title: str, body: str) -> str:
    """Show text on DIIZZY's screen as a card next to the avatar: code, lists, links or anything too long to say out loud."""
    return await _call("show_on_screen", {"title": title, "body": body})


DESKTOP_TOOLS = [open_app, focus_window, media_control, now_playing, spotify_play, show_on_screen]


def desktop_tools_for_turn() -> list:
    """The desktop tools, or nothing if the daemon isn't connected right now."""
    return list(DESKTOP_TOOLS) if get_active() is not None else []
