"""
desktop_tools.py — tools that act on the operator's PC through the desktop daemon.

Bound into L1's tool list only for platform="desktop" turns, and only while the
daemon is connected (see engine.run_icarus). Each tool is a typed request; the
daemon checks it against its own policy and runs it with hyprctl / playerctl /
the Spotify API. There is deliberately no shell or arbitrary-command tool here.
"""

import asyncio
import json
import logging
import time

from .claude_runner import MODES as CLAUDE_MODES, parse_projects
from .desktop_session import DesktopUnavailable, get_active

logger = logging.getLogger(__name__)

DESKTOP_PROMPT = """

## Desktop voice mode
You are speaking out loud through DIIZZY's desktop avatar. Everything you write is converted to speech.
- Answer in one to three short spoken sentences. No markdown, lists, code, URLs, file paths or emoji.
- If the answer needs detail (code, a list, a link), call show_on_screen with it and say in a few words that it's on screen.
- Desktop tools, available only in this mode, act on DIIZZY's PC directly: open_app, focus_window, media_control, now_playing, spotify_play, show_on_screen. When asked, just do it and confirm briefly; don't narrate.
- Coding work in one of DIIZZY's projects (fix, implement, refactor, investigate or explain code) goes to run_claude_task, not escalate_to_councilor or delegate_task. Use mode="ask" to investigate or answer without changing anything, mode="edit" to make changes (DIIZZY confirms first; once tested, the change is applied to DIIZZY's checked-out branch). Projects: {projects}.
- When a finished Claude task says a restart is needed to load its change, offer it. If DIIZZY agrees (or asks Icarus to restart), call restart_icarus with the parts named; it runs after this conversation ends, so just say it'll restart in a moment.
- To explain something visual ("show me", an algorithm, an example), don't read it aloud: show it and narrate it in 2-6 beats. Code goes to show_visual; a grid that fills in step by step (DP tables, matrices) goes to show_table; flows, trees and architectures go to show_diagram. Keep examples small so they're quick: tables up to about 6x6, 3-5 beats, code under 25 lines. Call the tool once. Each beat is one or two spoken sentences plus the lines to light up while you say them. The beats are spoken for you, in order, with the lines highlighted, so after show_visual end your reply with one short closing sentence at most. If DIIZZY wants to keep it ("keep that up", "pop it out"), call keep_visual.
- If a screenshot is attached, it's the window DIIZZY is looking at right now (he pressed the look hotkey): answer about what's on it, and refer to things by what they say or where they are.
- There is no shell access from here. For anything those tools can't do, say so or delegate as you normally would.
"""


def desktop_prompt() -> str:
    projects = ", ".join(parse_projects()) or "none configured (set DESKTOP_CLAUDE_PROJECTS)"
    return DESKTOP_PROMPT.replace("{projects}", projects)


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


async def run_claude_task(project: str, task: str, mode: str = "ask") -> str:
    """Hand a coding task in one of DIIZZY's projects to Claude Code, which works on it in the background and reports back when it's done. mode="ask" investigates or answers questions about the code without changing anything; mode="edit" makes changes in an isolated worktree, has them tested, and applies them to the branch DIIZZY has checked out (kept on its own branch if they conflict or tests fail), after DIIZZY confirms on the desktop. Write task as a complete brief: Claude has no conversation context."""
    from .delegation import new_task_id
    from .tools import current_chat_id, current_platform, current_user_id
    from backend.database.redis_connection import get_redis_client

    projects = parse_projects()
    name = str(project).strip().lower()
    if name not in projects:
        return f"Unknown project {project!r}. Configured projects: {', '.join(projects) or 'none (DESKTOP_CLAUDE_PROJECTS is empty)'}."
    mode = str(mode).strip().lower()
    if mode not in CLAUDE_MODES:
        mode = "ask"
    if not str(task).strip():
        return "No task given. Write the task as a complete brief."
    session = get_active()
    if session is None:
        return "The desktop daemon is not connected, so I can't confirm or start a Claude task."
    if mode == "edit":
        try:
            answer = await session.call("confirm", {
                "title": f"Edit {name} with Claude?",
                "body": str(task)[:280],
                "meta": "Say yes, or press SUPER+I · SUPER+Esc cancels",
            }, timeout=60)
        except asyncio.TimeoutError:
            return "DIIZZY didn't confirm in time, so nothing was started."
        except DesktopUnavailable as e:
            return f"Desktop unavailable: {e}"
        if not answer.get("ok"):
            return "DIIZZY declined, so nothing was started."

    task_id = new_task_id()
    payload = {
        "type": "claude_code", "task_id": task_id, "timestamp": int(time.time()),
        "project": name, "task": str(task), "mode": mode,
        "platform": current_platform.get(), "chat_id": current_chat_id.get(), "user_id": current_user_id.get(),
    }
    receivers = await get_redis_client().publish("icarus:councilor:requests", json.dumps(payload))
    if not receivers:
        return "The Councilor isn't running, so the Claude task couldn't start."
    where = f" on branch claude/{task_id}" if mode == "edit" else ""
    return f"Started Claude ({mode}) on {name} as task {task_id}{where}. It reports back when it finishes; don't wait for it."


async def restart_icarus(parts: list[str]) -> str:
    """Restart parts of Icarus so code changes take effect: "desktop" (this avatar daemon), "councilor" (runs Claude tasks and subagents), "api" (the main Icarus server). It is queued and runs once the current conversation ends; the Councilor also waits for running tasks to finish. Only call it when DIIZZY asks or agrees. parts=["cancel"] cancels a queued restart."""
    if isinstance(parts, str):
        parts = [parts]
    return await _call("restart_icarus", {"parts": [str(p) for p in parts or []]})


MAX_BEATS = 8
MAX_CODE_LINES = 200


def _parse_lines(spec, n_lines: int) -> list[int]:
    """[3, 4], "5-9", "3,7", 4 -> sorted line numbers inside the code."""
    out: set[int] = set()
    items = spec if isinstance(spec, list) else [spec]
    for item in items:
        for part in str(item).replace(" ", "").split(","):
            if not part:
                continue
            a, sep, b = part.partition("-")
            try:
                lo, hi = int(a), int(b) if sep else int(a)
            except ValueError:
                continue
            out.update(i for i in range(min(lo, hi), max(lo, hi) + 1) if 1 <= i <= n_lines)
    return sorted(out)


def normalize_beats(beats, n_lines: int) -> list[dict]:
    """Keep only usable beats: something to say, line numbers inside the code, a short note."""
    clean = []
    for beat in beats if isinstance(beats, list) else []:
        if not isinstance(beat, dict):
            continue
        say = str(beat.get("say") or beat.get("text") or "").strip()
        if not say:
            continue
        clean.append({"say": say[:400], "lines": _parse_lines(beat.get("lines", []), n_lines),
                      "note": str(beat.get("note") or "").strip()[:120]})
    return clean[:MAX_BEATS]


async def show_visual(title: str, code: str, language: str = "", beats: list[dict] | None = None, keep_open: bool = False) -> str:
    """Put code on DIIZZY's screen next to the avatar and walk him through it, instead of reading code aloud. beats is the ordered walkthrough, 2-6 steps, each {"say": one or two spoken sentences, "lines": line numbers (1-based) to light up while saying it, like [3, 4] or "5-9", "note": optional short label shown beside them}. Keep code under about 40 lines; keep_open=true opens it in his browser instead (for long code or when he wants to keep it). The beats are spoken for you, so close your reply with one short sentence at most."""
    session = get_active()
    if session is None:
        return "The desktop daemon is not connected, so there's no screen to show it on."
    code = str(code or "").strip("\n")
    if not code.strip():
        return "Nothing to show: pass the code."
    lines = code.split("\n")
    if len(lines) > MAX_CODE_LINES:
        code = "\n".join(lines[:MAX_CODE_LINES])
        lines = lines[:MAX_CODE_LINES]
    raw = _as_list(beats)
    clean = normalize_beats(raw, len(lines))
    logger.info(f"[show_visual] {len(lines)} lines, {len(clean)}/{len(raw)} usable beat(s)")
    payload = {"kind": "code", "title": str(title or "Example")[:60], "code": code, "language": str(language or "")[:20].lower(),
               "beats": clean, "keep_open": bool(keep_open) or len(lines) > 40}
    result = await _call("show_visual", payload)
    return _shown(result, len(clean), len(raw), 'e.g. [{"say": "...", "lines": [3, 4], "note": "..."}]')


MAX_GRID = 16
MAX_NODES = 14


def _cell(spec, rows: int, cols: int):
    """[r, c], "r,c", {"row": r, "col": c} or {"r": r, "c": c} -> [r, c] inside the grid."""
    try:
        if isinstance(spec, dict):
            r, c = spec.get("row", spec.get("r")), spec.get("col", spec.get("c", spec.get("column")))
        elif isinstance(spec, str):
            r, c = spec.replace("(", "").replace(")", "").replace("[", "").replace("]", "").split(",")[:2]
        else:
            r, c = spec[0], spec[1]
        r, c = int(r), int(c)
    except (TypeError, ValueError, IndexError, AttributeError):
        return None
    return [r, c] if 0 <= r < rows and 0 <= c < cols else None


def _fill_item(item, rows: int, cols: int):
    """[r, c, value] or {"row", "col", "value"} -> [r, c, "value"]."""
    if isinstance(item, dict):
        cell, value = _cell(item, rows, cols), item.get("value", item.get("v", item.get("val")))
    elif isinstance(item, (list, tuple)) and len(item) >= 3:
        cell, value = _cell(item[:2], rows, cols), item[2]
    else:
        return None
    return cell + [str(value)[:6]] if cell and value is not None else None


def _arrow_item(item, rows: int, cols: int):
    """[r1, c1, r2, c2] or {"from": [r, c], "to": [r, c]} -> [r1, c1, r2, c2]."""
    if isinstance(item, dict):
        a, b = _cell(item.get("from"), rows, cols), _cell(item.get("to"), rows, cols)
    elif isinstance(item, (list, tuple)) and len(item) >= 4:
        a, b = _cell(item[:2], rows, cols), _cell(item[2:4], rows, cols)
    elif isinstance(item, (list, tuple)) and len(item) == 2:
        a, b = _cell(item[0], rows, cols), _cell(item[1], rows, cols)
    else:
        return None
    return a + b if a and b else None


def _as_list(value) -> list:
    """Models sometimes send one item where a list is expected, or a JSON string of the list."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return [value]
    if value is None:
        return []
    if isinstance(value, dict) or (isinstance(value, list) and value and not isinstance(value[0], (list, tuple, dict, str))):
        return [value]
    return value if isinstance(value, list) else [value]


def normalize_table_beats(beats, rows: int, cols: int) -> list[dict]:
    clean = []
    for beat in beats if isinstance(beats, list) else []:
        if not isinstance(beat, dict) or not str(beat.get("say") or "").strip():
            continue
        cells = [c for c in (_cell(x, rows, cols) for x in _as_list(beat.get("cells") or beat.get("highlight"))) if c]
        fill = [f for f in (_fill_item(x, rows, cols) for x in _as_list(beat.get("fill") or beat.get("fills"))) if f]
        arrows = []
        target = (fill[0][:2] if fill else cells[0] if cells else None)
        for item in _as_list(beat.get("arrows")):
            arrow = _arrow_item(item, rows, cols)
            is_bare_cell = not (isinstance(item, (list, tuple)) and len(item) != 2)
            if arrow is None and target and is_bare_cell:
                # a bare source cell ("this value comes from [0, 1]") points at the cell being filled
                src = _cell(item, rows, cols)
                arrow = src + target if src and src != target else None
            if arrow and arrow not in arrows:
                arrows.append(arrow)
        clean.append({"say": str(beat["say"]).strip()[:400], "cells": cells, "fill": fill, "arrows": arrows[:8],
                      "note": str(beat.get("note") or "").strip()[:120]})
    return clean[:MAX_BEATS]


async def show_table(title: str, rows: int, cols: int, row_labels: list[str] | None = None, col_labels: list[str] | None = None,
                     cells: list[list[str]] | None = None, beats: list[dict] | None = None, keep_open: bool = False) -> str:
    """Show a grid on DIIZZY's screen and fill it in as you explain: perfect for DP tables, matrices, comparisons. Indices are 0-based [row, col] (dp[0][0] is the top-left). cells is the starting grid of values ("" for empty). beats is the ordered walkthrough, each {"say": one or two spoken sentences, "cells": [[r, c], ...] to highlight, "fill": [[r, c, value], ...] values written in during this beat, "arrows": [[r1, c1, r2, c2], ...] showing which cells a value comes from, "note": optional short label}. Up to 16x16. The beats are spoken for you, so close your reply with one short sentence at most."""
    session = get_active()
    if session is None:
        return "The desktop daemon is not connected, so there's no screen to show it on."
    try:
        rows, cols = max(1, min(MAX_GRID, int(rows))), max(1, min(MAX_GRID, int(cols)))
    except (TypeError, ValueError):
        return "rows and cols must be numbers."
    grid = [["" for _ in range(cols)] for _ in range(rows)]
    for r, row in enumerate((cells or [])[:rows]):
        for c, value in enumerate((row if isinstance(row, list) else [])[:cols]):
            grid[r][c] = "" if value is None else str(value)[:6]
    labels = lambda xs, n: [str(x)[:8] for x in (xs or [])][:n] + [""] * max(0, n - len(xs or []))
    raw = _as_list(beats)
    clean = normalize_table_beats(raw, rows, cols)
    logger.info(f"[show_table] {rows}x{cols}, {len(clean)}/{len(raw)} usable beat(s); raw beats: {json.dumps(raw)[:600]}")
    payload = {"kind": "table", "title": str(title or "Table")[:60], "rows": rows, "cols": cols, "cells": grid,
               "row_labels": labels(row_labels, rows), "col_labels": labels(col_labels, cols),
               "beats": clean, "keep_open": bool(keep_open)}
    result = await _call("show_visual", payload)
    return _shown(result, len(clean), len(raw), 'e.g. [{"say": "...", "cells": [[1, 2]], "fill": [[1, 2, "5"]], "arrows": [[0, 2, 1, 2]]}]')


async def show_diagram(title: str, nodes: list[dict], edges: list[dict] | None = None, beats: list[dict] | None = None,
                       keep_open: bool = False) -> str:
    """Show a boxes-and-arrows diagram on DIIZZY's screen and walk through it: flows, architectures, recursion trees, state machines. nodes is [{"id": "a", "label": "Short label"}, ...] (up to 14); edges is [{"from": "a", "to": "b", "label": "optional"}, ...]; it's laid out left to right automatically. beats is the ordered walkthrough, each {"say": one or two spoken sentences, "nodes": [ids to light up], "edges": [["a", "b"], ...] to light up, "note": optional short label}. The beats are spoken for you, so close your reply with one short sentence at most."""
    session = get_active()
    if session is None:
        return "The desktop daemon is not connected, so there's no screen to show it on."
    clean_nodes, ids = [], set()
    for node in (nodes or [])[:MAX_NODES]:
        if isinstance(node, dict) and str(node.get("id") or "").strip():
            nid = str(node["id"]).strip()[:24]
            if nid not in ids:
                ids.add(nid)
                clean_nodes.append({"id": nid, "label": str(node.get("label") or nid)[:28]})
    if not clean_nodes:
        return "No nodes to show: pass nodes as [{\"id\", \"label\"}]."
    clean_edges = []
    for edge in edges or []:
        if isinstance(edge, dict) and str(edge.get("from")) in ids and str(edge.get("to")) in ids:
            clean_edges.append({"from": str(edge["from"]), "to": str(edge["to"]), "label": str(edge.get("label") or "")[:20]})
    clean_beats = []
    for beat in _as_list(beats):
        if isinstance(beat, dict) and str(beat.get("say") or "").strip():
            clean_beats.append({
                "say": str(beat["say"]).strip()[:400],
                "nodes": [str(n) for n in beat.get("nodes") or [] if str(n) in ids],
                "edges": [[str(e[0]), str(e[1])] for e in beat.get("edges") or []
                          if isinstance(e, (list, tuple)) and len(e) >= 2 and str(e[0]) in ids and str(e[1]) in ids],
                "note": str(beat.get("note") or "").strip()[:120],
            })
    payload = {"kind": "graph", "title": str(title or "Diagram")[:60], "nodes": clean_nodes, "edges": clean_edges[:30],
               "beats": clean_beats[:MAX_BEATS], "keep_open": bool(keep_open)}
    result = await _call("show_visual", payload)
    return _shown(result, len(payload["beats"]), len(_as_list(beats)), 'e.g. [{"say": "...", "nodes": ["a"], "edges": [["a", "b"]]}]')


def _shown(result: str, usable: int, sent: int, example: str) -> str:
    """Tell the model plainly that it worked, so it doesn't call the tool again to 'fix' it."""
    if "failed" in result or "not connected" in result or "unavailable" in result:
        return result
    if usable:
        return (f"Done: it's on screen and all set. {usable} beat(s) will be spoken in order with their parts highlighted. "
                "Do not call this tool again for it. Reply with one short closing sentence at most.")
    if sent:
        return (f"It's on screen, but none of the {sent} beat(s) were usable, so nothing will be narrated. "
                f"Either explain it briefly in your reply, or call once more with beats like {example}.")
    return "Done: it's on screen. Explain it briefly in your reply; do not call this tool again for it."


async def keep_visual() -> str:
    """Pop the code currently on screen out into DIIZZY's browser so it stays after the avatar leaves ("keep that up", "pop it out")."""
    return await _call("keep_visual", {})


DESKTOP_TOOLS = [open_app, focus_window, media_control, now_playing, spotify_play, show_on_screen, run_claude_task, restart_icarus,
                 show_visual, show_table, show_diagram, keep_visual]


def desktop_tools_for_turn() -> list:
    """The desktop tools, or nothing if the daemon isn't connected right now."""
    return list(DESKTOP_TOOLS) if get_active() is not None else []
