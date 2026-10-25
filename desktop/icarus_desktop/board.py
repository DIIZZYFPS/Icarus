"""
board.py — "show me": code Icarus puts on screen and narrates, beat by beat.

A board is {id, title, code, language, beats: [{say, lines, note}]}. It shows in the
avatar overlay (renderer BoardWindow) while he talks; "keep it" pops it out to a local
page in the browser that stays after he leaves. Both follow the narration: the daemon
sends board_focus events to the overlay and to the page over a localhost WebSocket.
"""

import asyncio
import html
import json
import logging
import subprocess
from pathlib import Path

from .config import runtime_dir

logger = logging.getLogger(__name__)


def boards_dir() -> Path:
    path = runtime_dir() / "boards"
    path.mkdir(parents=True, exist_ok=True)
    return path


class BoardServer:
    """Pushes narration focus to any open board pages (ws://127.0.0.1:<port>)."""

    def __init__(self, port: int):
        self.port = port
        self.clients = set()
        self.last: dict[str, dict] = {}   # board id -> last focus, for pages that connect late

    async def start(self):
        from websockets.asyncio.server import serve
        try:
            self.server = await serve(self._client, "127.0.0.1", self.port)
            logger.info(f"[board] page sync on ws://127.0.0.1:{self.port}")
        except OSError as e:
            logger.warning(f"[board] page sync unavailable: {e}")

    async def _client(self, ws):
        self.clients.add(ws)
        try:
            for focus in self.last.values():
                await ws.send(json.dumps(focus))
            async for _ in ws:
                pass
        finally:
            self.clients.discard(ws)

    def broadcast(self, event: dict):
        if event.get("type") == "board_focus":
            self.last[str(event.get("id"))] = event
        for ws in list(self.clients):
            asyncio.ensure_future(_safe_send(ws, json.dumps(event)))


async def _safe_send(ws, data):
    try:
        await ws.send(data)
    except Exception:
        pass


def write_page(board: dict, port: int) -> Path:
    path = boards_dir() / f"{board['id']}.html"
    path.write_text(render_page(board, port), encoding="utf-8")
    return path


def open_page(path: Path) -> None:
    subprocess.Popen(["xdg-open", path.as_uri()], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _table_html(board: dict) -> str:
    cols = board.get("cols", 0)
    head = "".join(f"<th>{html.escape(str(x))}</th>" for x in (board.get("col_labels") or [""] * cols))
    rows = []
    for r, row in enumerate(board.get("cells") or []):
        label = (board.get("row_labels") or [""] * len(board["cells"]))[r]
        cells = "".join(f'<td data-rc="{r},{c}">{html.escape(str(v))}</td>' for c, v in enumerate(row))
        rows.append(f"<tr><th>{html.escape(str(label))}</th>{cells}</tr>")
    return f'<div class="code grid"><table id="grid"><tr><th></th>{head}</tr>{"".join(rows)}</table></div>'


def _graph_svg(board: dict) -> str:
    from .layout import layered_layout
    W, H = 760, 420
    pos = layered_layout(board.get("nodes") or [], board.get("edges") or [])
    xy = {nid: (40 + x * (W - 80), 30 + y * (H - 60)) for nid, (x, y) in pos.items()}
    parts = [f'<svg viewBox="0 0 {W} {H}" id="graph" role="img"><defs><marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" '
             'markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" fill="currentColor"/></marker></defs>']
    for e in board.get("edges") or []:
        if e["from"] in xy and e["to"] in xy:
            (x1, y1), (x2, y2) = xy[e["from"]], xy[e["to"]]
            label = f'<text x="{(x1 + x2) / 2}" y="{(y1 + y2) / 2 - 8}">{html.escape(e.get("label", ""))}</text>' if e.get("label") else ""
            parts.append(f'<g class="edge" data-e="{html.escape(e["from"])}|{html.escape(e["to"])}"><line x1="{x1 + 50}" y1="{y1}" '
                         f'x2="{x2 - 54}" y2="{y2}" marker-end="url(#ah)"/>{label}</g>')
    for n in board.get("nodes") or []:
        if n["id"] in xy:
            x, y = xy[n["id"]]
            parts.append(f'<g class="node" data-n="{html.escape(n["id"])}"><rect x="{x - 50}" y="{y - 16}" width="100" height="32"/>'
                         f'<text x="{x}" y="{y + 5}">{html.escape(n["label"][:16])}</text></g>')
    return '<div class="code grid">' + "".join(parts) + "</svg></div>"


def render_page(board: dict, port: int) -> str:
    kind = board.get("kind") or "code"
    lines = (board.get("code") or "").split("\n")
    code_rows = "\n".join(
        f'<div class="ln" data-n="{i}"><span class="no">{i}</span><code>{html.escape(line) or " "}</code></div>'
        for i, line in enumerate(lines, 1))
    if kind == "table":
        body = _table_html(board)
    elif kind == "graph":
        body = _graph_svg(board)
    else:
        body = f'<div class="code" id="code">{code_rows}</div>'
    beat_data = json.dumps([{k: b.get(k) or [] for k in ("lines", "cells", "fill", "arrows", "nodes", "edges")}
                            for b in board.get("beats") or []])
    initial = json.dumps(board.get("cells") or [])
    beats = "\n".join(
        f'<li data-i="{i}" data-lines="{",".join(map(str, b.get("lines") or []))}"><span class="say">{html.escape(b["say"])}</span>'
        + (f'<span class="note">{html.escape(b["note"])}</span>' if b.get("note") else "") + "</li>"
        for i, b in enumerate(board.get("beats") or []))
    lang = html.escape(board.get("language") or "")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(board['title'])} · Icarus</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github-dark.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>
<style>
:root {{ --void:#05080f; --panel:#0b111d; --line:#172133; --ink:#cfe6ee; --dim:#6c8193; --cy:#00f0ff; color-scheme:dark; }}
body {{ margin:0; background:var(--void); color:var(--ink); font:15px/1.5 "IBM Plex Sans", system-ui, sans-serif; }}
main {{ max-width:1100px; margin:0 auto; padding:28px 20px 60px; display:grid; grid-template-columns:minmax(0,3fr) minmax(260px,2fr); gap:22px; }}
@media (max-width:820px) {{ main {{ grid-template-columns:1fr; }} }}
header {{ grid-column:1/-1; display:flex; align-items:baseline; justify-content:space-between; gap:12px; flex-wrap:wrap; }}
h1 {{ margin:0; font:600 20px "Chakra Petch", sans-serif; letter-spacing:.12em; text-transform:uppercase; }}
h1 small {{ color:var(--cy); font-weight:500; margin-left:10px; letter-spacing:.2em; font-size:12px; }}
.code {{ background:var(--panel); border:1px solid var(--line); padding:12px 0; overflow-x:auto; font:13.5px/1.6 "JetBrains Mono", ui-monospace, monospace; }}
.ln {{ display:flex; padding:0 14px 0 0; border-left:2px solid transparent; transition:background .2s, opacity .2s; }}
.ln .no {{ width:44px; flex:none; text-align:right; padding-right:14px; color:var(--dim); user-select:none; }}
.ln code {{ white-space:pre; background:none !important; padding:0 !important; }}
.focus .ln {{ opacity:.45; }}
.focus .ln.on {{ opacity:1; background:rgba(0,240,255,.08); border-left-color:var(--cy); }}
ol {{ margin:0; padding:0; list-style:none; display:grid; gap:8px; counter-reset:b; }}
li {{ background:var(--panel); border:1px solid var(--line); padding:10px 12px 10px 38px; position:relative; cursor:pointer; counter-increment:b; }}
li::before {{ content:counter(b); position:absolute; left:12px; top:10px; color:var(--dim); font:600 12px "JetBrains Mono", monospace; }}
li.on {{ border-color:var(--cy); }} li.on::before {{ color:var(--cy); }}
.note {{ display:block; margin-top:6px; color:var(--cy); font:12px "JetBrains Mono", monospace; }}
button {{ font:600 11px "Chakra Petch", sans-serif; letter-spacing:.16em; text-transform:uppercase; color:var(--ink); background:none; border:1px solid #2c3b54; padding:7px 12px; cursor:pointer; }}
button:hover {{ border-color:var(--cy); color:var(--cy); }}
.live {{ color:var(--dim); font:11px "JetBrains Mono", monospace; }}
.grid {{ padding:16px; }}
table {{ border-collapse:collapse; font:14px "JetBrains Mono", monospace; margin:auto; }}
th {{ color:var(--dim); font-weight:400; padding:4px 8px; }}
td {{ min-width:46px; height:34px; text-align:center; border:1px solid #1d2a3f; transition:background .25s; }}
td.on {{ background:rgba(0,240,255,.16); border-color:var(--cy); }}
td.src {{ outline:2px solid #ffb000; outline-offset:-3px; }}
td.new {{ background:rgba(255,176,0,.25); }}
#graph {{ width:100%; height:auto; color:#2c3b54; }}
#graph line {{ stroke:currentColor; stroke-width:1.4; }}
#graph rect {{ fill:#060a12; stroke:#2c3b54; }}
#graph text {{ fill:var(--ink); font:12px "JetBrains Mono", monospace; text-anchor:middle; }}
#graph.focus .node:not(.on), #graph.focus .edge:not(.on) {{ opacity:.3; }}
#graph .node.on rect {{ stroke:var(--cy); fill:rgba(0,240,255,.12); }}
#graph .edge.on {{ color:var(--cy); }} #graph .edge.on line {{ stroke-width:2; }}
</style></head><body><main>
<header><h1>{html.escape(board['title'])}<small>{lang.upper() or {"table": "TABLE", "graph": "DIAGRAM"}.get(kind, "CODE")}</small></h1>
<div><span class="live" id="live">offline</span> <button id="copy" type="button">Copy code</button></div></header>
{body}
<ol id="beats">{beats}</ol>
</main>
<script>
const BOARD = {json.dumps(board['id'])}, LANG = {json.dumps(board.get('language') or '')}, KIND = {json.dumps(kind)};
const BEATS = {beat_data}, INITIAL = {initial};
const code = document.getElementById('code'), beats = [...document.querySelectorAll('#beats li')];
if (window.hljs) document.querySelectorAll('.ln code').forEach(el => {{
  try {{ el.innerHTML = (LANG && hljs.getLanguage(LANG) ? hljs.highlight(el.textContent, {{language: LANG}}) : hljs.highlightAuto(el.textContent)).value; }} catch (e) {{}}
}});
function focus(i) {{
  beats.forEach((b, k) => b.classList.toggle('on', k === i));
  const beat = BEATS[i] || {{}};
  if (KIND === 'table') {{
    // replay the fills up to this beat, so jumping around shows the table as it was then
    document.querySelectorAll('#grid td').forEach(td => {{
      const [r, c] = td.dataset.rc.split(',').map(Number);
      td.textContent = (INITIAL[r] || [])[c] ?? ''; td.className = '';
    }});
    BEATS.slice(0, i + 1).forEach((b, k) => (b.fill || []).forEach(([r, c, v]) => {{
      const td = document.querySelector(`#grid td[data-rc="${{r}},${{c}}"]`);
      if (td) {{ td.textContent = v; if (k === i) td.classList.add('new'); }}
    }}));
    (beat.cells || []).forEach(([r, c]) => document.querySelector(`#grid td[data-rc="${{r}},${{c}}"]`)?.classList.add('on'));
    (beat.arrows || []).forEach(([r, c]) => document.querySelector(`#grid td[data-rc="${{r}},${{c}}"]`)?.classList.add('src'));
    return;
  }}
  if (KIND === 'graph') {{
    const g = document.getElementById('graph'), ns = new Set(beat.nodes || []), es = new Set((beat.edges || []).map(e => e.join('|')));
    g.classList.toggle('focus', ns.size + es.size > 0);
    g.querySelectorAll('.node').forEach(n => n.classList.toggle('on', ns.has(n.dataset.n)));
    g.querySelectorAll('.edge').forEach(e => e.classList.toggle('on', es.has(e.dataset.e)));
    return;
  }}
  const lines = new Set((beats[i]?.dataset.lines || '').split(',').filter(Boolean).map(Number));
  code.classList.toggle('focus', lines.size > 0);
  document.querySelectorAll('.ln').forEach(el => el.classList.toggle('on', lines.has(+el.dataset.n)));
  const first = document.querySelector('.ln.on'); if (first) first.scrollIntoView({{block: 'center', behavior: 'smooth'}});
}}
beats.forEach((b, i) => b.addEventListener('click', () => focus(i)));
document.getElementById('copy').addEventListener('click', () => {{
  const text = KIND === 'table' ? [...document.querySelectorAll('#grid tr')].map(tr => [...tr.children].map(c => c.textContent).join('\\t')).join('\\n')
    : [...document.querySelectorAll('.ln code')].map(c => c.textContent).join('\\n');
  navigator.clipboard.writeText(text).then(() => document.getElementById('copy').textContent = 'Copied', () => {{}});
}});
(function connect() {{
  let ws; try {{ ws = new WebSocket('ws://127.0.0.1:{port}'); }} catch (e) {{ return; }}
  ws.onopen = () => document.getElementById('live').textContent = 'following Icarus';
  ws.onmessage = m => {{ const e = JSON.parse(m.data); if (e.type === 'board_focus' && e.id === BOARD) focus(e.index); }};
  ws.onclose = () => {{ document.getElementById('live').textContent = 'offline'; setTimeout(connect, 3000); }};
}})();
</script></body></html>"""
