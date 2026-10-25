#!/usr/bin/env python3
"""
avatar.py — the Icarus desktop avatar renderer.

A transparent, click-through overlay on Hyprland's top layer (GTK3 + gtk-layer-shell),
drawn with Cairo. It is display-only: it connects to the desktop daemon's unix socket,
receives newline-delimited JSON events, and draws. All decisions (what to say, which
actions run) live in the daemon.

Visual design: icarus_desktop_avatar.md §6 ("stripped" direction). Poses, activity
choice, scorch and timing are derived here from the facts the daemon sends (§6.5).

Runs on the system Python, which has python-gobject, python-cairo and gtk-layer-shell.

    python3 desktop/renderer/avatar.py --socket $XDG_RUNTIME_DIR/icarus-desktop/renderer.sock
    python3 desktop/renderer/avatar.py --demo        # scripted walk through every state
"""

import argparse
import ctypes
import glob
import json
import math
import os
import random
import socket
import sys
import threading
import time

import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GtkLayerShell", "0.1")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, GLib, Gtk, GtkLayerShell, Pango, PangoCairo  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from icarus_desktop.layout import layered_layout  # noqa: E402
TAU = math.pi * 2

# Surface: the bottom-right corner of the screen, same layout as the prototype's stage.
SW, SH = 960, 600
ORB = (904, 544)
PANEL = (420, 30, 520, 440)        # x, y, w, h of the character panel
OFFX, OFFY = 110, 30               # design frame (300x400) offset inside the panel
COL_RIGHT, COL_BOTTOM, COL_W = 412, 450, 360

CY = (0, 240, 255)
MG = (255, 42, 109)
AM = (255, 176, 0)
EMBER = (255, 122, 40)
EYE = (255, 70, 120)
INK = (3, 4, 6)

DISPLAY = "Chakra Petch"
MONO = "JetBrainsMono Nerd Font, JetBrains Mono, monospace"

VISIBLE = {"connecting", "listening", "thinking", "acting", "confirm", "speaking", "linger", "error"}
ACTIVITY = {
    "watching": {"turn": -1.0, "deck": 0, "vent": False},
    "working": {"turn": -1.0, "deck": 1, "vent": False},
    "venting": {"turn": -1.0, "deck": 0, "vent": True},
}


def load_fonts():
    """Register the bundled fonts for this process only (no system install)."""
    try:
        fc = ctypes.CDLL("libfontconfig.so.1")
        fc.FcConfigAppFontAddFile.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        for path in glob.glob(os.path.join(HERE, "fonts", "*.ttf")):
            fc.FcConfigAppFontAddFile(None, path.encode())
    except OSError:
        pass


def monitor_for_output(name):
    """GTK3 can't see connector names (DP-2, HDMI-A-1); find the monitor at that output's position."""
    import subprocess
    try:
        outputs = json.loads(subprocess.run(["hyprctl", "monitors", "-j"], capture_output=True, text=True, timeout=3).stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    target = next((o for o in outputs if o.get("name") == name), None)
    if not target:
        return None
    display = Gdk.Display.get_default()
    for i in range(display.get_n_monitors()):
        mon = display.get_monitor(i)
        geo = mon.get_geometry()
        if (geo.x, geo.y) == (target["x"], target["y"]):
            return mon
    return None


def lerp(a, b, k):
    return a + (b - a) * k


def clamp(v, a, b):
    return max(a, min(b, v))


def mix(c1, c2, k):
    return tuple(lerp(c1[i], c2[i], k) for i in range(3))


def rgba(c, a=1.0):
    return (c[0] / 255, c[1] / 255, c[2] / 255, a)


def hashf(n):
    s = math.sin(n * 12.9898) * 43758.5453
    return s - math.floor(s)


def sun_color(heat):
    k = clamp(heat, 0, 1)
    if k < .6:
        return mix((150, 225, 255), (255, 168, 60), k / .6)
    return mix((255, 168, 60), (255, 64, 56), (k - .6) / .4)


# ── text ─────────────────────────────────────────────────────────────────────

def layout(cr, text, family, px, weight="", width=None, spacing=0.0):
    lay = PangoCairo.create_layout(cr)
    desc = Pango.FontDescription.from_string(f"{family} {weight}".strip())
    desc.set_absolute_size(px * Pango.SCALE)
    lay.set_font_description(desc)
    if spacing:
        attrs = Pango.AttrList()
        attrs.insert(Pango.attr_letter_spacing_new(int(spacing * Pango.SCALE)))
        lay.set_attributes(attrs)
    if width:
        lay.set_width(int(width * Pango.SCALE))
        lay.set_wrap(Pango.WrapMode.WORD_CHAR)
    lay.set_text(text, -1)
    return lay


def size_of(lay):
    w, h = lay.get_pixel_size()
    return w, h


def draw_layout(cr, lay, x, y, color):
    cr.save()
    cr.move_to(x, y)
    cr.set_source_rgba(*color)
    PangoCairo.show_layout(cr, lay)
    cr.restore()


def cut_box(cr, x, y, w, h, cut):
    cr.move_to(x, y)
    cr.line_to(x + w - cut, y)
    cr.line_to(x + w, y + cut)
    cr.line_to(x + w, y + h)
    cr.line_to(x + cut, y + h)
    cr.line_to(x, y + h - cut)
    cr.close_path()


def glow_line(cr, col, a, w, blur, path):
    """Cairo has no blur: approximate a glow with wide, faint strokes under the core line."""
    cr.save()
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    for extra, mult in ((blur * .9, .06), (blur * .45, .14), (0, 1.0)):
        cr.set_line_width(w + extra)
        cr.set_source_rgba(*rgba(col, a * mult))
        path()
        cr.stroke()
    cr.restore()


# ── state ────────────────────────────────────────────────────────────────────

class Avatar:
    def __init__(self):
        now = time.monotonic()
        self.state = "dormant"
        self.p = 0.0
        self.p_target = 0.0
        self.orb_r = 17.0
        self.orb_a = .45
        self.level = 0.0
        self.mic = 0.0
        self.pulse = 0.0
        self.speech = 0.0
        self.turn = -1.0
        self.deck = 0.0
        self.spread = 0.0
        self.spread_until = 0.0
        self.answered = True
        self.step = -1
        self.activity = "watching"
        self.call_t0 = now
        self.glance_until = 0.0
        self.next_glance = 0.0
        self.glitch_until = 0.0
        self.freeze_until = 0.0
        self.flash_until = 0.0
        self.flash_col = CY
        self.heat = .3
        self.heat_known = False
        self.scorch = 0.0
        self.burn_stage = 0
        self.hot_stamped = False
        self.unread = 0
        self.tasks_running = 0
        self.recent_task = False
        self.tasks = []          # running Claude tasks [{id, title, project, mode, text, steps, started}]
        self.tasks_t0 = 0.0      # when the badge appeared / started fading
        self.tasks_last = []     # what the badge showed, kept while it fades out
        self.particles = []
        self.fire_t0 = 0.0       # fire glitch: a brief flash of Icarus burning
        self.fire_until = 0.0
        self.fire_burst = False
        self.next_fire = now + 30 + random.random() * 40
        self.push_t0 = -10.0     # camera: push-in when he answers or a stamp hits
        self.push_amt = 0.0
        self.shake_until = 0.0   # camera: short shake when the fire glitch hits
        self.chips = []          # [{id, text, done}]
        self.card = None         # {kind, title, body, meta, t0}
        self.subtitle = None     # {text, t0, dur}
        self.transcript = None   # {text, listening}
        self.stamp = None        # {word, sub, col, t0, ms}
        self.connected = False

    # facts from the daemon
    def apply(self, ev):
        kind = ev.get("type")
        now = time.monotonic()
        if kind == "state":
            self.set_state(ev.get("name", "dormant"))
        elif kind == "ambient":
            if ev.get("heat") is not None:
                self.heat = clamp(float(ev["heat"]), 0, 1)
                self.heat_known = True
            self.unread = int(ev.get("unread") or 0)
            self.tasks_running = int(ev.get("tasks_running") or 0)
            self.recent_task = bool(ev.get("recent_task"))
        elif kind == "tasks":
            items = [i for i in (ev.get("items") or []) if isinstance(i, dict)]
            if bool(items) != bool(self.tasks):
                self.tasks_t0 = now
            if items:
                self.tasks_last = items
            self.tasks = items
        elif kind == "mic_level":
            self.mic = clamp(float(ev.get("rms", 0)) * 6, 0, 1)
        elif kind == "speech_level":
            self.speech = clamp(float(ev.get("rms", 0)) * 5, 0, 1)
        elif kind == "transcript":
            self.transcript = {"text": ev.get("text", ""), "listening": bool(ev.get("listening")),
                               "prompt": ev.get("prompt"), "window": float(ev.get("window_ms") or 0) / 1000, "t0": now}
        elif kind == "transcript_clear":
            self.transcript = None
        elif kind == "subtitle":
            self.subtitle = {"text": ev.get("text", ""), "t0": now, "dur": max(.3, float(ev.get("duration_ms", 1500)) / 1000)}
        elif kind == "subtitle_clear":
            self.subtitle = None
        elif kind == "chip":
            for chip in self.chips:
                if chip["id"] == ev.get("id"):
                    chip["done"] = bool(ev.get("done"))
                    chip["text"] = ev.get("text") or chip["text"]
                    break
            else:
                self.chips.append({"id": ev.get("id"), "text": ev.get("text", ""), "done": bool(ev.get("done"))})
                self.chips = self.chips[-4:]
        elif kind == "chips_clear":
            self.chips = []
        elif kind == "card":
            self.card = {"kind": ev.get("kind", "info"), "title": ev.get("title", ""), "body": ev.get("body", ""), "meta": ev.get("meta", ""), "t0": now}
        elif kind == "card_clear":
            self.card = None
        elif kind == "stamp":
            col = {"amber": AM, "cyan": CY, "ember": EMBER, "magenta": MG}.get(ev.get("color"), CY)
            self.fire_stamp(ev.get("word", ""), ev.get("sub", ""), col, int(ev.get("ms", 1400)))
            if ev.get("wings"):
                self.spread_until = now + float(ev["wings"])
        elif kind in ("board", "board_focus", "board_clear"):
            win = getattr(self, "board_window", None)
            if win is not None:
                {"board": win.show_board, "board_focus": win.set_focus, "board_clear": win.hide_board}[kind](ev)
        elif kind == "scan":
            if ev.get("rect"):
                ScanFlash(ev["rect"])
        elif kind == "fire":
            self.ignite(float(ev.get("seconds", .8)))
        elif kind == "clear":
            self.chips, self.card, self.subtitle, self.transcript = [], None, None, None

    def fire_stamp(self, word, sub, col, ms):
        now = time.monotonic()
        self.push(.02)
        self.stamp = {"word": word, "sub": sub or "ICARUS // SIGNAL", "col": col, "t0": now, "ms": ms}
        self.freeze_until = now + .14
        self.flash_until = now + .07
        self.flash_col = col
        self.glitch_until = now + .16

    def push(self, amount):
        self.push_t0, self.push_amt = time.monotonic(), amount

    def camera(self, t):
        """Slow handheld drift (two incommensurate waves, smooth, not on twos) plus brief
        reactions. Returns (dx, dy, scale) for the projection; text never moves."""
        dx = 2.2 * math.sin(t * TAU / 11.3) + .8 * math.sin(t * TAU / 4.7 + 1.3)
        dy = 1.6 * math.sin(t * TAU / 13.7 + .6) + .6 * math.sin(t * TAU / 6.1 + 2)
        if t < self.shake_until:
            amp = 3 * (self.shake_until - t) / .35
            dx += (hashf(t * 61.7) - .5) * 2 * amp
            dy += (hashf(t * 47.3 + 9) - .5) * 2 * amp
        age = t - self.push_t0
        push = 0.0
        if 0 <= age < 1.2:
            push = self.push_amt * min(1, age / .08) * math.exp(-age * 4)
        return dx, dy, 1 + push

    def ignite(self, seconds):
        now = time.monotonic()
        self.shake_until = now + .35
        self.fire_t0, self.fire_until = now, now + seconds
        self.fire_burst = True
        self.glitch_until = max(self.glitch_until, now + .14)

    def fire_level(self, t):
        """0 outside a fire glitch; inside it, 1 with stepped flicker (on twos, like the rest)."""
        if t >= self.fire_until or self.p < .5:
            return 0.0
        return .75 + .25 * hashf(self.step * 1.37)

    def pick_activity(self):
        if self.heat > .6:
            return "venting"
        if self.tasks_running or self.unread or self.recent_task:
            return "working"
        return "watching"

    def set_state(self, name):
        now = time.monotonic()
        fresh = self.p < .5
        self.state = name
        if name in ("listening", "connecting") and fresh:
            self.activity = self.pick_activity()
            self.call_t0 = now
            self.answered = False
        if name == "speaking":
            self.next_glance = now + 2.5
        if name == "error":
            self.fire_stamp("FAILED", "ICARUS // ERROR", MG, 1400)
            self.ignite(.6)
        self.p_target = 1.0 if name in VISIBLE else 0.0

    def pose_target(self, t):
        a = ACTIVITY[self.activity]
        s = self.state
        if s == "listening":
            return a if t - self.call_t0 < .8 else {"turn": 0.0, "deck": 0}
        if s in ("thinking", "acting"):
            return {"turn": -.65, "deck": 1}
        if s == "confirm":
            return {"turn": 0.0, "deck": 1}
        if s == "speaking":
            return {"turn": -.5 if t < self.glance_until else 0.0, "deck": 0}
        return a

    def tick(self, t, dt):
        up = self.p_target > self.p
        rate = (1 / .75) if up else (1 / .6)
        self.p = min(self.p_target, self.p + rate * dt) if up else max(self.p_target, self.p - rate * dt)
        if 0 < self.p < 1 and random.random() < .1:
            self.glitch_until = t + .06
        elif self.p >= 1 and random.random() < .0096:
            self.glitch_until = t + .08

        if self.state == "speaking" and t > self.next_glance:
            self.glance_until = t + .7
            self.next_glance = t + 4 + random.random() * 3
        step = int(t * 12)
        if step != self.step:
            self.step = step
            pose = self.pose_target(t)
            r = 1.0 if self.p < .05 else (.09 if self.state == "linger" else .3)
            self.turn = lerp(self.turn, pose["turn"], r)
            if abs(self.turn - pose["turn"]) < .01:
                self.turn = pose["turn"]
            self.deck = lerp(self.deck, pose["deck"], 1 if r == 1 else .3)
            if not self.answered and self.p > .5 and self.turn > -.15:
                self.answered = True
                self.spread_until = t + .7
                self.push(.012)   # a slight lean toward you as he turns to answer
            target = 1.0 if t < self.spread_until else (.7 if self.state == "confirm" else 0.0)
            self.spread = lerp(self.spread, target, .35)

        # scorch integrates heat above 0.55; recovers more slowly
        heat = self.heat
        self.scorch = clamp(self.scorch + (dt * .07 * (heat - .55) / .45 if heat > .55 else -dt * .025), 0, 1)
        stage5 = int(self.scorch * 5)
        if self.p > .9:
            if stage5 > self.burn_stage:
                self.glitch_until = t + .18
            if self.scorch > .55 and not self.hot_stamped:
                self.hot_stamped = True
                self.fire_stamp("RUNNING HOT", f"GPU {round(heat * 100)}% // WINGS FAILING", EMBER, 1300)
                self.ignite(.9)
        self.burn_stage = stage5
        if self.scorch < .2:
            self.hot_stamped = False

        if self.p >= .95 and t > self.next_fire and t > self.fire_until:
            self.ignite(.45 + random.random() * .45)
            self.next_fire = t + (25 + random.random() * 55) * (1 - .6 * self.heat)
        elif self.p < .5:
            self.next_fire = max(self.next_fire, t + 6)   # never right as he appears
        if self.fire_until and self.fire_until < t < self.fire_until + .05:
            self.glitch_until = max(self.glitch_until, t + .1)   # glitch back out

        self.level = lerp(self.level, self.mic if self.state == "listening" else 0, .3)
        self.pulse = lerp(self.pulse, self.speech if self.state == "speaking" else 0, .35)

        alive = []
        for p in self.particles:
            p["life"] += dt
            if p["life"] < p["max"]:
                p["vy"] += p.get("g", 0) * dt
                p["x"] += p["vx"] * dt
                p["y"] += p["vy"] * dt
                alive.append(p)
        self.particles = alive[-320:]

    def busy(self):
        return self.p > 0 or self.state != "dormant" or self.particles or self.stamp is not None


# ── drawing ──────────────────────────────────────────────────────────────────

class Renderer:
    def __init__(self, avatar):
        self.a = avatar
        self.char = cairo.ImageSurface(cairo.FORMAT_ARGB32, PANEL[2], PANEL[3])
        scan = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 3)
        sc = cairo.Context(scan)
        sc.set_source_rgba(0, 0, 0, .32)
        sc.rectangle(0, 0, 1, 1)
        sc.fill()
        self.scan = cairo.SurfacePattern(scan)
        self.scan.set_extend(cairo.EXTEND_REPEAT)

    # character, drawn in the 300x400 design frame
    def emit_burn(self, x, y):
        r = random.random()
        a = self.a
        if r < .5:
            a.particles.append({"x": x, "y": y, "vx": -10 - random.random() * 16, "vy": -14 - random.random() * 22, "life": 0, "max": 1.2 + random.random() * 1.6, "s": 1 + random.random() * 1.5, "col": (255, 236, 200) if random.random() < .35 else EMBER})
        else:
            a.particles.append({"x": x, "y": y, "vx": -6 - random.random() * 10, "vy": -6 - random.random() * 10, "life": 0, "max": 2 + random.random() * 2, "s": 1.6 + random.random() * 1.6, "col": (26, 22, 24), "ash": True})

    def draw_frame_graphics(self, c, t, hot):
        a = self.a
        col, dim = rgba(hot, .85), rgba(hot, .38)
        c.save()
        c.translate(-74, 318)
        c.rotate(-math.pi / 2)
        lay = layout(c, "ICARUS", DISPLAY, 34, "Bold")
        draw_layout(c, lay, 0, -32, col)
        lay = layout(c, "L1 // LOCAL INTELLIGENCE // ICR-01", MONO, 7)
        draw_layout(c, lay, 0, 6, dim)
        c.restore()
        c.set_line_width(1)
        c.set_source_rgba(*dim)
        for x, y in ((-96, -16), (398, -16), (-96, 398), (398, 398)):
            c.new_sub_path()
            c.arc(x, y, 4.5, 0, TAU)
            c.move_to(x - 8, y)
            c.line_to(x + 8, y)
            c.move_to(x, y - 8)
            c.line_to(x, y + 8)
        c.stroke()
        x = -96
        for k in range(22):
            w = 1 if hashf(k + 3) < .5 else 2.5
            c.rectangle(x, 362, w, 14)
            x += w + 1.6
        c.fill()
        draw_layout(c, layout(c, a.state.upper(), MONO, 9, "SemiBold"), 312, 340, col)
        draw_layout(c, layout(c, "HEAT", MONO, 9), 312, 358, dim)
        lit = round(a.heat * 10)
        for k in range(10):
            c.set_source_rgba(*(rgba(EMBER, .95) if k < lit and k >= 7 else col if k < lit else rgba(hot, .15)))
            c.rectangle(342 + k * 6, 361, 4, 9)
            c.fill()
        draw_layout(c, layout(c, f"INTEGRITY {round(100 - a.scorch * 30)}%", MONO, 9), 312, 374, dim)
        if a.tasks_running:
            draw_layout(c, layout(c, f"TASKS {a.tasks_running}", MONO, 9), 312, 390, col)

    def draw_character(self, c, t):
        a = self.a
        # A fire glitch is a flash of the high-heat scorch look at full strength.
        fire = a.fire_level(t)
        scorch = max(a.scorch, fire)
        hot = sun_color(max(a.heat, fire))
        edge = rgba(hot, .55)
        tq = a.step / 12

        def wind(i):
            return math.sin(tq * 2.3 + i * 1.7) * .7 + math.sin(tq * 5.1 + i * .9) * .3

        cam_x, cam_y, cam_s = a.camera(t)
        c.save()
        c.translate(cam_x * .5, cam_y * .5)   # the sun sits further back: half the drift

        # flat sun, bold cut stripes, tick ring
        c.set_source_rgba(*rgba(hot, .95))
        c.arc(150, 138, 100, 0, TAU)
        c.fill()
        c.set_operator(cairo.OPERATOR_DEST_OUT)
        for k in range(4):
            c.rectangle(40, 176 + k * 15, 220, 3 + k * 1.6)
        c.fill()
        c.set_operator(cairo.OPERATOR_OVER)
        c.set_source_rgba(*rgba(hot, .45))
        c.set_line_width(1)
        c.arc(150, 138, 113, 0, TAU)
        c.stroke()
        for k in range(36):
            ang = k / 36 * TAU + t * .05
            r2 = 123 if k % 9 == 0 else 117
            c.move_to(150 + math.cos(ang) * 113, 138 + math.sin(ang) * 113)
            c.line_to(150 + math.cos(ang) * r2, 138 + math.sin(ang) * r2)
        c.stroke()

        # thinking: the tick ring turns into a sweeping spinner and a scan line runs down the sun
        if a.state in ("thinking", "acting"):
            head = (t * 1.8) % TAU
            for k in range(16):
                ang = head - k * .06
                alpha = (1 - k / 16) ** 1.4
                glow_line(c, CY, alpha, 3, 10, lambda ang=ang: (
                    c.move_to(150 + math.cos(ang) * 109, 138 + math.sin(ang) * 109),
                    c.line_to(150 + math.cos(ang) * 130, 138 + math.sin(ang) * 130)))
            c.save()
            c.arc(150, 138, 100, 0, TAU)
            c.clip()
            sy = 38 + (t * 70) % 200
            g = cairo.LinearGradient(0, sy - 26, 0, sy)
            g.add_color_stop_rgba(0, *rgba(CY, 0))
            g.add_color_stop_rgba(1, *rgba(CY, .35))
            c.set_source(g)
            c.rectangle(50, sy - 26, 200, 26)
            c.fill()
            c.set_source_rgba(*rgba((210, 255, 255), .9))
            c.rectangle(50, sy, 200, 1.5)
            c.fill()
            c.restore()

        c.restore()
        c.save()
        c.translate(150 + cam_x, 240 + cam_y)   # Icarus: full drift, push-in around his chest
        c.scale(cam_s, cam_s)
        c.translate(-150, -240)

        # blade wings
        for side in (-1, 1):
            for i in (3, 2, 1, 0):
                ang = -math.pi / 2 + side * lerp(.1 + i * .07, .55 + i * .33, a.spread) + wind(i + side * 4) * .02
                L = (228 - i * 28) * (1 - scorch * .2 * (1 - i * .15))
                rx, ry = 150 + side * 38, 252
                ca, sa = math.cos(ang), math.sin(ang)
                px, py = -sa, ca
                c.move_to(rx + px * 8, ry + py * 8)
                c.line_to(rx + ca * L * .55 + px * 6, ry + sa * L * .55 + py * 6)
                c.line_to(rx + ca * L, ry + sa * L)
                c.line_to(rx - px * 5, ry - py * 5)
                c.close_path()
                c.set_source_rgba(*rgba(INK))
                c.fill_preserve()
                c.set_source_rgba(*edge)
                c.set_line_width(1)
                c.stroke()
                if scorch > .05:
                    tx, ty = rx + ca * L, ry + sa * L
                    glow_line(c, EMBER, min(1, scorch * 1.5) * (.7 + .3 * math.sin(t * 9 + i)), 1.6, 8,
                              lambda: (c.move_to(tx - ca * 10 + px * 3, ty - sa * 10 + py * 3), c.line_to(tx, ty)))
                    if random.random() < scorch * .08:
                        self.emit_burn(tx, ty)

        # the silhouette
        top = [(156, 50), (205, 82), (215, 150), (205, 206), (234, 222), (277, 262)]
        top_l = [(23, 262), (66, 222), (95, 206), (85, 150), (97, 82)]
        c.move_to(156, 50)
        for x, y in top[1:] + [(285, 404), (15, 404)] + top_l:
            c.line_to(x, y)
        c.close_path()
        c.set_source_rgba(*rgba(INK))
        c.fill_preserve()
        c.set_source_rgba(*edge)
        c.set_line_width(1.2)
        c.stroke()
        if fire and a.fire_burst:
            a.fire_burst = False
            for _ in range(30):
                x, y = random.choice(top + top_l)
                self.emit_burn(x + (random.random() - .5) * 16, y - random.random() * 8)
        if scorch > .03:
            c.save()
            c.set_dash([2 + scorch * 10, 10], -t * 18)
            ridge = top + top_l
            def ridge_path():
                # two runs from the hood peak down each shoulder, never across the chest
                for run in (top, [top[0]] + top_l[::-1]):
                    c.move_to(*run[0])
                    for pt in run[1:]:
                        c.line_to(*pt)
            glow_line(c, EMBER, min(1, scorch * 1.6), 1.6, 8, ridge_path)
            c.restore()
            if random.random() < scorch * .25:
                x, y = random.choice(ridge)
                self.emit_burn(x + (random.random() - .5) * 10, y)

        # eyes do the acting
        turn = max(-1.0, a.turn)
        ox = turn * 12
        thinking = a.state in ("thinking", "acting")
        down = 3 if (a.activity == "working" and turn < -.5) or thinking else 0
        ey = 142 + down
        narrow = .45 if thinking else 1
        blink = (t % 4.3) < .1 or (t % 7.1) < .08
        bright = min(1, .72 + a.pulse * .28 + (.3 if a.state == "listening" else 0))
        far = lerp(1, .25, clamp(-turn, 0, 1))

        def eye(x1, x2, alpha):
            if blink:
                c.set_source_rgba(*rgba(EYE, .6 * alpha))
                c.set_line_width(1)
                c.move_to(x1, ey)
                c.line_to(x2, ey)
                c.stroke()
                return
            glow_line(c, EYE, bright * alpha, 2.8 * (.8 if narrow < 1 else 1), 16,
                      lambda: (c.move_to(x1, ey - 3 * narrow), c.line_to(x2, ey + 1.5 * narrow)))

        eye(125 + ox, 139 + ox, 1)
        eye(175 + ox, 175 + ox - 14 * lerp(1, .6, clamp(-turn, 0, 1)), far)

        if a.deck > .05:
            glow_line(c, CY, .8 * a.deck, 1.5, 8, lambda: (c.move_to(118, 300), c.line_to(182, 300)))
        if ACTIVITY[a.activity]["vent"] and turn < -.6 and (t % 2.6) < .6 and random.random() < .55:
            a.particles.append({"x": 92 + random.random() * 10, "y": 214, "vx": -12 - random.random() * 10, "vy": -16 - random.random() * 12, "life": 0, "max": 1.8 + random.random(), "s": 3 + random.random() * 3, "smoke": True, "col": (205, 215, 228)})

        c.restore()
        self.draw_frame_graphics(c, t, hot)   # fixed: the frame doesn't move with the projection

        for p in a.particles:
            k = 1 - p["life"] / p["max"]
            if p.get("smoke"):
                r = p["s"] * (1 + 1.5 * p["life"] / p["max"])
                g = cairo.RadialGradient(p["x"], p["y"], 0, p["x"], p["y"], r)
                g.add_color_stop_rgba(0, *rgba(p["col"], .3 * k))
                g.add_color_stop_rgba(1, *rgba(p["col"], 0))
                c.set_source(g)
                c.arc(p["x"], p["y"], r, 0, TAU)
                c.fill()
            else:
                c.set_source_rgba(*rgba(p["col"], k * (.8 if p.get("ash") else 1)))
                c.rectangle(p["x"], p["y"], p["s"], p["s"])
                c.fill()

    def render_character(self, t):
        a = self.a
        if t < a.freeze_until:
            return
        c = cairo.Context(self.char)
        c.set_operator(cairo.OPERATOR_CLEAR)
        c.paint()
        c.set_operator(cairo.OPERATOR_OVER)
        c.translate(OFFX, OFFY)
        c.set_line_join(cairo.LINE_JOIN_MITER)
        self.draw_character(c, t)
        c.identity_matrix()
        fade = cairo.LinearGradient(0, PANEL[3] * .7, 0, PANEL[3])
        fade.add_color_stop_rgba(0, 0, 0, 0, 0)
        fade.add_color_stop_rgba(1, 0, 0, 0, 1)
        c.set_operator(cairo.OPERATOR_DEST_OUT)
        c.set_source(fade)
        c.paint()
        c.set_operator(cairo.OPERATOR_ATOP)
        c.set_source(self.scan)
        c.paint()
        if t < a.flash_until:
            c.set_source_rgba(*rgba(a.flash_col))
            c.paint()

    def composite_character(self, cr, t):
        a = self.a
        if a.p <= .001:
            return
        px, py, pw, ph = PANEL
        glitch = t < a.glitch_until
        split = 5 * 2.4 if glitch else 1.3 * 2.4 * .5
        y0 = py + ph * (1 - a.p)
        cr.save()
        cr.rectangle(px - 40, y0, pw + 80, py + ph - y0)
        cr.clip()
        jolt = (random.random() - .5) * 12 if glitch and random.random() < .5 else 0
        cr.set_operator(cairo.OPERATOR_ADD)
        cr.set_source_rgba(*rgba(MG, .45))
        cr.mask_surface(self.char, px - split + jolt, py)
        cr.set_source_rgba(*rgba((42, 125, 255), .45))
        cr.mask_surface(self.char, px + split + jolt, py)
        cr.set_operator(cairo.OPERATOR_OVER)
        cr.set_source_surface(self.char, px + jolt, py)
        cr.paint_with_alpha(.82 if random.random() < .03 else 1)
        if glitch:
            for _ in range(7):
                y = random.random() * ph
                h = 3 + random.random() * 12
                dx = (random.random() - .5) * 18 * 2.4
                cr.save()
                cr.rectangle(px - 40, py + y, pw + 80, h)
                cr.clip()
                cr.set_operator(cairo.OPERATOR_CLEAR)
                cr.paint()
                cr.set_operator(cairo.OPERATOR_OVER)
                cr.set_source_surface(self.char, px + dx, py)
                cr.paint()
                cr.restore()
        cr.restore()
        if .001 < a.p < .999:
            col = sun_color(a.heat)
            g = cairo.LinearGradient(0, y0 - 14, 0, y0 + 2)
            g.add_color_stop_rgba(0, *rgba(col, 0))
            g.add_color_stop_rgba(1, *rgba(col, .5))
            cr.set_source(g)
            cr.rectangle(px, y0 - 14, pw, 16)
            cr.fill()
            cr.set_source_rgba(.9, 1, 1, .95)
            cr.rectangle(px, y0, pw, 1.5)
            cr.fill()
            for _ in range(6):
                cr.set_source_rgba(*rgba(col, .8))
                cr.rectangle(px + random.random() * pw, y0 + random.random() * 8 - 4, 8 + random.random() * 40, 1)
                cr.fill()

    def draw_orb(self, cr, t):
        a = self.a
        ox, oy = ORB
        active = a.state != "dormant"
        a.orb_r = lerp(a.orb_r, 30 if a.state == "listening" else 23 if active else 17, .12)
        a.orb_a = lerp(a.orb_a, 1 if active else (.45 if a.connected else .25), .08)
        col = AM if a.state == "confirm" else sun_color(a.heat)
        R = a.orb_r * ((1 + .05 * math.sin(t * math.pi / 2)) if a.state == "dormant" else 1)
        cr.push_group()
        g = cairo.RadialGradient(ox, oy, 0, ox, oy, R * 3)
        g.add_color_stop_rgba(0, *rgba(col, .32))
        g.add_color_stop_rgba(1, *rgba(col, 0))
        cr.set_source(g)
        cr.arc(ox, oy, R * 3, 0, TAU)
        cr.fill()
        g = cairo.RadialGradient(ox - R * .3, oy - R * .35, 1, ox, oy, R)
        g.add_color_stop_rgba(0, 1, .98, .94, .95)
        g.add_color_stop_rgba(.4, *rgba(col, .85))
        g.add_color_stop_rgba(1, *rgba(col, .12))
        cr.set_source(g)
        cr.arc(ox, oy, R, 0, TAU)
        cr.fill()
        cr.save()
        cr.arc(ox, oy, R, 0, TAU)
        cr.clip()
        cr.set_operator(cairo.OPERATOR_DEST_OUT)
        for k in range(5):
            cr.rectangle(ox - R, oy + 2 + k * 4 + (t * 4) % 4, R * 2, 1 + k * .4)
        cr.fill()
        cr.restore()
        cr.set_source_rgba(.02, .03, .05, .85)
        cr.save()
        cr.translate(ox, oy - R * .1)
        cr.scale(R * .62, R * .42)
        cr.arc(0, 0, 1, 0, TAU)
        cr.restore()
        cr.fill()
        for x in (ox - R * .45, ox + R * .13):
            glow_line(cr, EYE, 1, 2, 5, lambda x=x: (cr.move_to(x, oy - R * .18), cr.line_to(x + R * .32, oy - R * .18)))

        cr.set_line_width(1)
        if a.state == "listening":
            cr.set_line_width(2)
            cr.set_source_rgba(*rgba(col, .85))
            for i in range(56):
                ang = i / 56 * TAU
                amp = a.level * (.35 + .65 * abs(math.sin(i * 1.7 + t * 9) * math.cos(i * .63 - t * 5.3)))
                r1, r2 = R + 7, R + 10 + amp * 24
                cr.move_to(ox + math.cos(ang) * r1, oy + math.sin(ang) * r1)
                cr.line_to(ox + math.cos(ang) * r2, oy + math.sin(ang) * r2)
            cr.stroke()
        elif a.state in ("thinking", "acting"):
            cr.set_line_width(1.5)
            cr.set_dash([7, 9])
            cr.set_source_rgba(*rgba(col, .8))
            cr.arc(ox, oy, R + 10, t * 2.2, t * 2.2 + TAU)
            cr.stroke()
            cr.set_dash([3, 12])
            cr.set_source_rgba(*rgba(col, .5))
            cr.arc(ox, oy, R + 17, -t * 1.4, -t * 1.4 + TAU)
            cr.stroke()
            cr.set_dash([])
        elif a.state == "connecting":
            for k in range(3):  # incoming-call pulse
                ph = ((t * 1.6) + k / 3) % 1
                cr.set_line_width(1.6)
                cr.set_source_rgba(*rgba(col, (1 - ph) * .9))
                cr.arc(ox, oy, R + 6 + ph * 26, 0, TAU)
                cr.stroke()
        elif a.state == "confirm":
            cr.set_line_width(2)
            cr.set_source_rgba(*rgba(AM, .5 + .5 * abs(math.sin(t * 4))))
            cr.arc(ox, oy, R + 10, 0, TAU)
            cr.stroke()
        else:
            cr.set_line_width(1 + a.pulse * 1.5)
            cr.set_source_rgba(*rgba(col, .4 + a.pulse * .5))
            cr.arc(ox, oy, R + 8 + a.pulse * 6, 0, TAU)
            cr.stroke()
        if a.unread:
            ang = t * 1.3
            x, y = ox + math.cos(ang) * (R + 16), oy + math.sin(ang) * (R + 16) * .55
            g = cairo.RadialGradient(x, y, 0, x, y, 9)
            g.add_color_stop_rgba(0, *rgba(MG, .9))
            g.add_color_stop_rgba(1, *rgba(MG, 0))
            cr.set_source(g)
            cr.arc(x, y, 9, 0, TAU)
            cr.fill()
            cr.set_source_rgba(1, .84, .88, 1)
            cr.arc(x, y, 2.2, 0, TAU)
            cr.fill()
        cr.pop_group_to_source()
        cr.paint_with_alpha(a.orb_a)

    def draw_task_badge(self, cr, t):
        """Running Claude task, beside the orb: what it is, what it's doing, how long it's been.
        Lives in orb mode; fades out while he's on screen (the chips carry progress then)."""
        a = self.a
        items = a.tasks or a.tasks_last
        if not items:
            return
        age = t - a.tasks_t0
        k = min(1, age / .3) if a.tasks else max(0, 1 - age / .4)
        k *= 1 - a.p
        if k <= .01:
            if not a.tasks:
                a.tasks_last = []
            return
        task = items[0]
        ox, oy = ORB
        bw, pad = 300, 11
        right = ox - 44 + (1 - k) ** 2 * 18
        x0 = right - bw
        inner = bw - pad * 2

        def line(text, family, px, weight="", width=inner, spacing=0.0):
            lay = layout(cr, text, family, px, weight, spacing=spacing)
            lay.set_width(int(width * Pango.SCALE))
            lay.set_ellipsize(Pango.EllipsizeMode.END)
            return lay

        elapsed = max(0, int(time.time() - float(task.get("started") or time.time())))
        clock = (f"{elapsed // 3600}:{elapsed // 60 % 60:02}:{elapsed % 60:02}" if elapsed >= 3600
                 else f"{elapsed // 60:02}:{elapsed % 60:02}")
        more = f"+{len(items) - 1} · " if len(items) > 1 else ""
        right_lay = layout(cr, more + clock, MONO, 10, "SemiBold")
        rw, _ = size_of(right_lay)
        scope = " · ".join(str(task[key]).upper() for key in ("mode", "project") if task.get(key))
        head = line("CLAUDE TASK" + (f" // {scope}" if scope else ""), DISPLAY, 9.5, "SemiBold", inner - rw - 8, 1.8)
        title = line(task.get("title") or "Running Claude task…", MONO, 12.5, "Medium")
        steps = int(task.get("steps") or 0)
        steps_lay = layout(cr, f"{steps} STEP{'S' if steps != 1 else ''}", MONO, 9.5) if steps else None
        sw = size_of(steps_lay)[0] + 8 if steps_lay else 0
        sub = line("› " + (task.get("text") or "starting"), MONO, 10.5, width=inner - sw)
        hh, th, sh = size_of(head)[1], size_of(title)[1], size_of(sub)[1]
        bh = pad + hh + 5 + th + 3 + sh + 9 + 2 + pad - 3
        y0 = clamp(oy - bh / 2, 8, SH - bh - 8)

        cut_box(cr, x0, y0, bw, bh, 9)
        cr.set_source_rgba(.016, .03, .063, .9 * k)   # near-opaque: Hyprland shows layers through
        cr.fill_preserve()
        cr.set_source_rgba(*rgba(CY, .45 * k))
        cr.set_line_width(1)
        cr.stroke()
        cr.set_source_rgba(*rgba(CY, .9 * k))   # left accent rail
        cr.rectangle(x0, y0 + 9, 2, bh - 18)
        cr.fill()
        # tether to the orb
        cr.set_source_rgba(*rgba(CY, .35 * k))
        cr.move_to(right, oy + .5)
        cr.line_to(ox - a.orb_r - 10, oy + .5)
        cr.stroke()
        cr.rectangle(ox - a.orb_r - 12, oy - 1.5, 3, 3)
        cr.fill()

        x, y = x0 + pad, y0 + pad - 2
        blink = .55 + .45 * (1 if int(t * 2) % 2 == 0 else 0)   # stepped, like the rest
        draw_layout(cr, layout(cr, "◆", DISPLAY, 9.5), x, y, rgba(CY, blink * k))
        draw_layout(cr, head, x + 14, y, rgba(CY, k))
        draw_layout(cr, right_lay, x0 + bw - pad - rw, y - 1, rgba((157, 179, 194), k))
        y += hh + 5
        draw_layout(cr, title, x, y, rgba((232, 248, 255), k))
        y += th + 3
        draw_layout(cr, sub, x, y, rgba((127, 180, 189), k))
        if steps_lay:
            draw_layout(cr, steps_lay, x0 + bw - pad - sw + 8, y + 1, rgba((108, 129, 147), k))
        y += sh + 9
        # no percentage from Claude: an indeterminate sweep, so it reads as alive at a glance
        cr.set_source_rgba(*rgba(CY, .14 * k))
        cr.rectangle(x, y, inner, 2)
        cr.fill()
        seg = inner * .28
        head_x = ((t * .45) % 1) * (inner + seg) - seg
        cr.save()
        cr.rectangle(x, y - 1, inner, 4)
        cr.clip()
        g = cairo.LinearGradient(x + head_x, 0, x + head_x + seg, 0)
        g.add_color_stop_rgba(0, *rgba(CY, 0))
        g.add_color_stop_rgba(1, *rgba(CY, .95 * k))
        cr.set_source(g)
        cr.rectangle(x + head_x, y, seg, 2)
        cr.fill()
        cr.restore()

    def draw_column(self, cr, t):
        """Card, chips and subtitles, stacked upward from the panel's bottom-left."""
        a = self.a
        right, y = COL_RIGHT, COL_BOTTOM
        if a.subtitle:
            sub = a.subtitle
            shown = sub["text"][: int(len(sub["text"]) * min(1, (t - sub["t0"]) / (sub["dur"] * .92)))]
            lay = layout(cr, shown + ("▌" if int(t * 2) % 2 == 0 else " "), MONO, 15, width=COL_W - 28)
            w, h = size_of(lay)
            full_w, _ = size_of(layout(cr, sub["text"] + "▌", MONO, 15, width=COL_W - 28))
            bw, bh = min(COL_W, full_w + 28), h + 38
            x0 = right - bw
            cr.set_source_rgba(.016, .03, .063, .74)
            cr.rectangle(x0, y - bh, bw, bh)
            cr.fill()
            cr.set_source_rgba(*rgba(CY, .32))
            cr.set_line_width(1)
            cr.move_to(x0, y - bh + .5)
            cr.line_to(right, y - bh + .5)
            cr.move_to(x0, y - .5)
            cr.line_to(right, y - .5)
            cr.stroke()
            draw_layout(cr, layout(cr, "ICARUS", DISPLAY, 9.5, "Medium", spacing=2.2), x0 + 14, y - bh + 9, rgba(CY))
            draw_layout(cr, lay, x0 + 14, y - bh + 25, (.91, .98, 1, 1))
            y -= bh + 10
        for chip in reversed(a.chips):
            text = ("◆ " if chip["done"] else "◇ ") + chip["text"].upper()
            lay = layout(cr, text, DISPLAY, 10.5, "Medium", spacing=1.6)
            w, h = size_of(lay)
            bw, bh = w + 20, h + 8
            cut_box(cr, right - bw, y - bh, bw, bh, 0)
            cr.new_path()
            cr.move_to(right - bw, y - bh)
            cr.line_to(right - 7, y - bh)
            cr.line_to(right, y - bh + 7)
            cr.line_to(right, y)
            cr.line_to(right - bw, y)
            cr.close_path()
            cr.set_source_rgba(*rgba(CY, .02 if chip["done"] else .07))
            cr.fill_preserve()
            cr.set_source_rgba(*rgba(CY, .16 if chip["done"] else .35))
            cr.set_line_width(1)
            cr.stroke()
            draw_layout(cr, lay, right - bw + 9, y - bh + 4, rgba((127, 180, 189)) if chip["done"] else rgba(CY))
            y -= bh + 6
        if a.card:
            card = a.card
            k = min(1, (t - card["t0"]) / .3)
            dx = (1 - k) ** 2 * 22
            accent = AM if card["kind"] == "confirm" else CY
            bw = 320
            title = layout(cr, card["title"].upper(), DISPLAY, 11, "SemiBold", spacing=2.2)
            body = layout(cr, card["body"], MONO, 13, width=bw - 28)
            meta = layout(cr, card["meta"], MONO, 11, width=bw - 28) if card.get("meta") else None
            th, bh_ = size_of(title)[1], size_of(body)[1]
            mh = size_of(meta)[1] if meta else 0
            bh = 12 + th + 4 + bh_ + (4 + mh if meta else 0) + 12
            x0 = right - bw + dx
            y -= 4
            cut_box(cr, x0, y - bh, bw, bh, 12)
            cr.set_source_rgba(.024, .04, .08, .8 * k)
            cr.fill_preserve()
            cr.set_source_rgba(*rgba(accent, .45 * k))
            cr.set_line_width(1)
            cr.stroke()
            yy = y - bh + 11
            draw_layout(cr, title, x0 + 14, yy, rgba(accent, k))
            yy += th + 4
            draw_layout(cr, body, x0 + 14, yy, rgba((207, 230, 238), k))
            if meta:
                draw_layout(cr, meta, x0 + 14, yy + bh_ + 4, rgba((108, 129, 147), k))

    def draw_transcript(self, cr, t):
        tr = self.a.transcript
        if not tr:
            return
        if tr["listening"] and not tr["text"] and tr.get("prompt"):
            # follow-up window: the question plus a bar that drains until he gives up
            lay = layout(cr, tr["prompt"].upper(), DISPLAY, 13, "SemiBold", spacing=1.8)
            w, h = size_of(lay)
            draw_layout(cr, lay, 820 - w, 548, rgba(CY))
            left = 1 - min(1, (t - tr["t0"]) / tr["window"]) if tr["window"] else 1
            cr.set_source_rgba(*rgba(CY, .18))
            cr.rectangle(820 - w, 548 + h + 4, w, 2)
            cr.fill()
            cr.set_source_rgba(*rgba(CY, .9))
            cr.rectangle(820 - w, 548 + h + 4, w * left, 2)
            cr.fill()
            return
        if tr["listening"] and not tr["text"]:
            lay = layout(cr, "LISTENING", DISPLAY, 11, "Medium", spacing=1.5)
            w, h = size_of(lay)
            draw_layout(cr, lay, 820 - w, 556, rgba((108, 129, 147)))
            return
        lay = layout(cr, tr["text"], MONO, 13)
        w, h = size_of(lay)
        w = min(w, 460)
        draw_layout(cr, lay, 820 - w, 554, rgba((157, 179, 194)))
        draw_layout(cr, layout(cr, ">", MONO, 13, "Medium"), 820 - w - 14, 554, rgba(CY))

    def draw_stamp(self, cr, t):
        st = self.a.stamp
        if not st:
            return
        age = t - st["t0"]
        if age > st["ms"] / 1000:
            self.a.stamp = None
            return
        col = st["col"]
        right, top = 400, 72
        word = layout(cr, st["word"], DISPLAY, 66, "Bold")
        ww, wh = size_of(word)
        if ww > right - 16:  # long words shrink to fit beside him
            word = layout(cr, st["word"], DISPLAY, 66 * (right - 16) / ww, "Bold")
            ww, wh = size_of(word)
        sub = layout(cr, st["sub"], MONO, 10, spacing=2.2)
        sw, sh = size_of(sub)
        reveal = min(1, math.floor(age / .06 + 1) / 4)
        cr.save()
        cr.rectangle(0, top, SW, (sh + wh + 30) * reveal)
        cr.clip()
        shift = 0 if age > .24 else (1 - reveal) * -14
        draw_layout(cr, sub, right - sw + shift, top, rgba(col))
        cx, cy = right - sw - 14 + shift, top + sh / 2
        cr.set_source_rgba(*rgba(col))
        cr.set_line_width(1)
        cr.arc(cx, cy, 4.5, 0, TAU)
        cr.move_to(cx - 4.5, cy)
        cr.line_to(cx + 4.5, cy)
        cr.move_to(cx, cy - 4.5)
        cr.line_to(cx, cy + 4.5)
        cr.stroke()
        jit = 4 if age < .48 and int(age / .08) % 2 == 1 else 0
        wx, wy = right - ww + shift + jit, top + sh + 4
        if jit:
            draw_layout(cr, word, wx - 4, wy, rgba(MG, .75))
            draw_layout(cr, word, wx + 4, wy, rgba((42, 125, 255), .75))
        draw_layout(cr, word, wx, wy, rgba(col))
        fill = min(1, math.floor(age / .08) / 10)
        cr.set_source_rgba(*rgba(col))
        cr.rectangle(right - ww, wy + wh + 2, ww * fill, 3)
        cr.fill()
        cr.restore()

    def draw(self, cr, t):
        cr.set_operator(cairo.OPERATOR_CLEAR)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        self.draw_orb(cr, t)
        self.draw_task_badge(cr, t)
        if self.a.p > .001:
            self.render_character(t)
            self.composite_character(cr, t)
        self.draw_column(cr, t)
        self.draw_transcript(cr, t)
        self.draw_stamp(cr, t)


# ── window + event feed ──────────────────────────────────────────────────────

TOKEN_COLORS = (   # pygments token family -> (hex colour, italic)
    ("Comment", "#5d7486", True), ("Keyword", "#ff5f96", False), ("Name.Function", "#00f0ff", False),
    ("Name.Class", "#00f0ff", False), ("Name.Builtin", "#7fd8e0", False), ("Literal.String", "#b8e07a", False),
    ("Literal.Number", "#ffb000", False), ("Operator", "#8fa6b8", False), ("Punctuation", "#8fa6b8", False),
)


def highlight_lines(code, language):
    """Pango markup for each line of code, coloured with Pygments if it's available."""
    plain = [GLib.markup_escape_text(line) for line in code.split("\n")]
    try:
        from pygments import lex
        from pygments.lexers import TextLexer, get_lexer_by_name, guess_lexer
        from pygments.token import string_to_tokentype
    except ImportError:
        return plain
    try:
        lexer = get_lexer_by_name(language) if language else guess_lexer(code)
    except Exception:
        lexer = TextLexer()
    families = [(string_to_tokentype(name), colour, italic) for name, colour, italic in TOKEN_COLORS]
    lines = [""]
    for ttype, value in lex(code, lexer):
        colour, italic = "#cfe6ee", False
        for family, c, it in families:
            if ttype in family:
                colour, italic = c, it
                break
        for i, part in enumerate(value.split("\n")):
            if i:
                lines.append("")
            if part:
                style = ' style="italic"' if italic else ""
                lines[-1] += f'<span foreground="{colour}"{style}>{GLib.markup_escape_text(part)}</span>'
    n = len(plain)
    return (lines + plain[len(lines):])[:n]


class BoardWindow(Gtk.Window):
    """'Show me': code on a holo panel left of Icarus. The beat he's speaking lights its
    lines; the rest dim; a note sits under the active lines. Click-through, display only."""
    W, H = 660, 560
    LH = 19

    def __init__(self, monitor=None, margin=8):
        super().__init__()
        self.set_app_paintable(True)
        visual = self.get_screen().get_rgba_visual()
        if visual:
            self.set_visual(visual)
        GtkLayerShell.init_for_window(self)
        GtkLayerShell.set_namespace(self, "icarus-board")
        GtkLayerShell.set_layer(self, GtkLayerShell.Layer.OVERLAY)
        GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.BOTTOM, True)
        GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.RIGHT, True)
        GtkLayerShell.set_margin(self, GtkLayerShell.Edge.BOTTOM, margin + 40)
        GtkLayerShell.set_margin(self, GtkLayerShell.Edge.RIGHT, margin + SW + 12)
        GtkLayerShell.set_exclusive_zone(self, 0)
        GtkLayerShell.set_keyboard_mode(self, GtkLayerShell.KeyboardMode.NONE)
        if monitor:
            GtkLayerShell.set_monitor(self, monitor)
        self.set_size_request(self.W, self.H)
        self.connect("draw", self.on_draw)
        self.connect("realize", lambda _w: self.input_shape_combine_region(cairo.Region()))
        self.board = None
        self.ticking = False

    def show_board(self, ev):
        kind = ev.get("kind") or "code"
        self.board = {"id": ev.get("id"), "kind": kind, "title": str(ev.get("title", "")), "language": str(ev.get("language", ""))}
        if kind == "table":
            self.board.update(rows=int(ev.get("rows", 1)), cols=int(ev.get("cols", 1)),
                              grid=[list(r) for r in ev.get("cells") or []],
                              row_labels=ev.get("row_labels") or [], col_labels=ev.get("col_labels") or [])
            self.flash = {}
        elif kind == "graph":
            self.board.update(nodes=ev.get("nodes") or [], edges=ev.get("edges") or [])
            self.board["pos"] = layered_layout(self.board["nodes"], self.board["edges"])
        else:
            self.board["markup"] = highlight_lines(str(ev.get("code", "")), str(ev.get("language", "")))
        self.layouts = None
        self.focus = None
        self.scroll = 0.0
        self.t_show, self.t_hide = time.monotonic(), None
        self.show_all()
        if not self.ticking:
            self.ticking = True
            GLib.timeout_add(33, self.on_tick)

    def set_focus(self, ev):
        if self.board and ev.get("id") == self.board["id"]:
            now = time.monotonic()
            self.focus = {"lines": set(ev.get("lines") or []), "note": ev.get("note") or "",
                          "cells": {tuple(c) for c in ev.get("cells") or []}, "arrows": ev.get("arrows") or [],
                          "nodes": set(ev.get("nodes") or []), "edges": {tuple(e) for e in ev.get("edges") or []},
                          "index": int(ev.get("index", 0)), "total": int(ev.get("total", 0)), "t0": now}
            if self.board["kind"] == "table":
                grid = self.board["grid"]
                for r, c, value in ev.get("fill") or []:
                    if 0 <= r < len(grid) and 0 <= c < len(grid[r]):
                        grid[r][c] = value
                        self.flash[(r, c)] = now

    def hide_board(self, _ev=None):
        if self.board and self.t_hide is None:
            self.t_hide = time.monotonic()

    def on_tick(self):
        if self.t_hide is not None and time.monotonic() - self.t_hide > .35:
            self.board, self.t_hide, self.ticking = None, None, False
            self.hide()
            return False
        self.queue_draw()
        return True

    def draw_code(self, cr, now):
        b, W, H, LH = self.board, self.W, self.H, self.LH
        if self.layouts is None:
            self.layouts = []
            for markup in b["markup"]:
                lay = PangoCairo.create_layout(cr)
                desc = Pango.FontDescription.from_string(MONO)
                desc.set_absolute_size(12.5 * Pango.SCALE)
                lay.set_font_description(desc)
                lay.set_width(int((W - 70) * Pango.SCALE))
                lay.set_ellipsize(Pango.EllipsizeMode.END)
                lay.set_markup(markup or " ", -1)
                self.layouts.append(lay)
        y0, rows = 50, (H - 50 - 30) // LH
        focus = self.focus["lines"] if self.focus else set()
        if focus:
            mid = (min(focus) + max(focus)) / 2 - 1
            target = clamp(mid - rows / 2 + .5, 0, max(0, len(self.layouts) - rows))
        else:
            target = 0
        self.scroll = lerp(self.scroll, target, .25)
        cr.save()
        cr.rectangle(8, y0 - 2, W - 16, rows * LH + 4)
        cr.clip()
        for i, lay in enumerate(self.layouts):
            y = y0 + (i - self.scroll) * LH
            if y < y0 - LH or y > y0 + rows * LH:
                continue
            on = (i + 1) in focus
            if on:
                cr.set_source_rgba(*rgba(CY, .10))
                cr.rectangle(10, y - 1, W - 20, LH)
                cr.fill()
                cr.set_source_rgba(*rgba(CY, .9))
                cr.rectangle(10, y - 1, 2, LH)
                cr.fill()
            alpha = 1.0 if (on or not focus) else .38
            num = layout(cr, str(i + 1), MONO, 11)
            draw_layout(cr, num, 46 - size_of(num)[0], y + 1, rgba(CY if on else (93, 116, 134), alpha))
            cr.push_group()
            cr.move_to(56, y)
            PangoCairo.update_layout(cr, lay)
            PangoCairo.show_layout(cr, lay)
            cr.pop_group_to_source()
            cr.paint_with_alpha(alpha)
        cr.restore()

        if self.focus and self.focus["note"] and focus:
            last = max(focus) - 1
            ny = y0 + (last + 1 - self.scroll) * LH + 6
            note = layout(cr, self.focus["note"], MONO, 12, width=W * .6)
            nw, nh = size_of(note)
            nx = W - 22 - nw
            ny = min(ny, y0 + rows * LH - nh - 8)
            fade = min(1, (now - self.focus["t0"]) / .25)
            cut_box(cr, nx - 10, ny, nw + 20, nh + 10, 7)
            cr.set_source_rgba(.02, .06, .1, .95 * fade)
            cr.fill_preserve()
            cr.set_source_rgba(*rgba(CY, .7 * fade))
            cr.stroke()
            draw_layout(cr, note, nx, ny + 5, rgba((232, 251, 255), fade))

    def draw_note(self, cr, now, y):
        if not (self.focus and self.focus["note"]):
            return
        note = layout(cr, self.focus["note"], MONO, 12, width=self.W - 60)
        nw, nh = size_of(note)
        fade = min(1, (now - self.focus["t0"]) / .25)
        x = (self.W - nw) / 2
        cut_box(cr, x - 10, y, nw + 20, nh + 10, 7)
        cr.set_source_rgba(.02, .06, .1, .95 * fade)
        cr.fill_preserve()
        cr.set_source_rgba(*rgba(CY, .7 * fade))
        cr.set_line_width(1)
        cr.stroke()
        draw_layout(cr, note, x, y + 5, rgba((232, 251, 255), fade))

    @staticmethod
    def arrow(cr, x1, y1, x2, y2, col, alpha, width=1.6):
        cr.set_source_rgba(*rgba(col, alpha))
        cr.set_line_width(width)
        cr.move_to(x1, y1)
        cr.line_to(x2, y2)
        cr.stroke()
        ang = math.atan2(y2 - y1, x2 - x1)
        for side in (-.45, .45):
            cr.move_to(x2, y2)
            cr.line_to(x2 - 8 * math.cos(ang + side), y2 - 8 * math.sin(ang + side))
        cr.stroke()

    def draw_table(self, cr, now):
        b, W, H = self.board, self.W, self.H
        rows, cols, grid = b["rows"], b["cols"], b["grid"]
        focus = self.focus or {}
        hot = focus.get("cells") or set()
        left, top = 60, 74
        cw = min(56, (W - left - 24) / cols)
        ch = min(34, (H - top - 110) / rows)
        gx = left + (W - left - 24 - cw * cols) / 2
        gy = top
        for c, label in enumerate(b["col_labels"][:cols]):
            lay = layout(cr, str(label), MONO, 11)
            draw_layout(cr, lay, gx + c * cw + (cw - size_of(lay)[0]) / 2, gy - 18, rgba((93, 116, 134)))
        for r, label in enumerate(b["row_labels"][:rows]):
            lay = layout(cr, str(label), MONO, 11)
            draw_layout(cr, lay, gx - 10 - size_of(lay)[0], gy + r * ch + (ch - size_of(lay)[1]) / 2, rgba((93, 116, 134)))
        for r in range(rows):
            for c in range(cols):
                x, y = gx + c * cw, gy + r * ch
                on = (r, c) in hot
                cr.rectangle(x + 1, y + 1, cw - 2, ch - 2)
                cr.set_source_rgba(*(rgba(CY, .16) if on else (.025, .05, .09, 1)))
                cr.fill_preserve()
                cr.set_source_rgba(*rgba(CY, .85 if on else .16))
                cr.set_line_width(1.4 if on else 1)
                cr.stroke()
                age = now - self.flash.get((r, c), -9)
                if age < .6:
                    cr.rectangle(x + 1, y + 1, cw - 2, ch - 2)
                    cr.set_source_rgba(*rgba(AM, .35 * (1 - age / .6)))
                    cr.fill()
                value = grid[r][c] if r < len(grid) and c < len(grid[r]) else ""
                if value != "":
                    lay = layout(cr, str(value), MONO, 13, "SemiBold" if on else "")
                    tw, th = size_of(lay)
                    col = (255, 236, 200) if age < .6 else ((232, 251, 255) if on or not hot else (150, 170, 185))
                    draw_layout(cr, lay, x + (cw - tw) / 2, y + (ch - th) / 2, rgba(col))
        for r1, c1, r2, c2 in focus.get("arrows") or []:
            x1, y1 = gx + c1 * cw + cw / 2, gy + r1 * ch + ch / 2
            x2, y2 = gx + c2 * cw + cw / 2, gy + r2 * ch + ch / 2
            d = math.hypot(x2 - x1, y2 - y1) or 1
            ux, uy = (x2 - x1) / d, (y2 - y1) / d
            self.arrow(cr, x1 + ux * 10, y1 + uy * 10, x2 - ux * 12, y2 - uy * 12, AM, .95)
        self.draw_note(cr, now, min(H - 78, gy + rows * ch + 14))

    def draw_graph(self, cr, now):
        b, W, H = self.board, self.W, self.H
        focus = self.focus or {}
        lit_nodes, lit_edges = focus.get("nodes") or set(), focus.get("edges") or set()
        active = bool(lit_nodes or lit_edges)
        x0, y0, gw, gh = 40, 64, W - 80, H - 64 - 110
        boxes = {}
        for node in b["nodes"]:
            px, py = b["pos"].get(node["id"], (.5, .5))
            lay = layout(cr, node["label"], MONO, 12)
            tw, th = size_of(lay)
            bw, bh = tw + 22, th + 12
            boxes[node["id"]] = (x0 + px * gw, y0 + py * gh, bw, bh, lay)
        for e in b["edges"]:
            if e["from"] not in boxes or e["to"] not in boxes:
                continue
            ax, ay, aw, _, _ = boxes[e["from"]]
            bx, by, bw2, _, _ = boxes[e["to"]]
            on = (e["from"], e["to"]) in lit_edges
            sx, ex = (ax + aw / 2, bx - bw2 / 2) if bx > ax + 4 else (ax, bx)
            self.arrow(cr, sx, ay, ex - 2, by, CY, .95 if on else (.14 if active else .45), 2 if on else 1.2)
            if e.get("label"):
                lay = layout(cr, e["label"], MONO, 10)
                draw_layout(cr, lay, (sx + ex) / 2 - size_of(lay)[0] / 2, (ay + by) / 2 - 16, rgba((108, 129, 147)))
        for nid, (cx, cy, bw, bh, lay) in boxes.items():
            on = nid in lit_nodes
            cut_box(cr, cx - bw / 2, cy - bh / 2, bw, bh, 6)
            cr.set_source_rgba(*(rgba(CY, .14) if on else (.025, .05, .09, 1)))
            cr.fill_preserve()
            cr.set_source_rgba(*rgba(CY, .95 if on else (.18 if active else .45)))
            cr.set_line_width(1.6 if on else 1)
            cr.stroke()
            draw_layout(cr, lay, cx - bw / 2 + 11, cy - bh / 2 + 6,
                        rgba((232, 251, 255) if on or not active else (120, 140, 155)))
        self.draw_note(cr, now, H - 92)

    def on_draw(self, _w, cr):
        cr.set_operator(cairo.OPERATOR_CLEAR)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        b = self.board
        if not b:
            return False
        now = time.monotonic()
        k = min(1, (now - self.t_show) / .4)
        if self.t_hide is not None:
            k = max(0, 1 - (now - self.t_hide) / .35)
        W, H, LH = self.W, self.H, self.LH
        reveal = H * k
        cr.save()
        cr.rectangle(0, 0, W, reveal)
        cr.clip()
        cut_box(cr, .5, .5, W - 1, H - 1, 14)
        cr.set_source_rgba(.016, .03, .063, 1.0)   # solid: anything translucent lets busy windows bleed through the code
        cr.fill_preserve()
        cr.set_source_rgba(*rgba(CY, .35))
        cr.set_line_width(1)
        cr.stroke()
        draw_layout(cr, layout(cr, "◇ " + b["title"].upper(), DISPLAY, 13, "SemiBold", spacing=2), 16, 12, rgba(CY))
        tag = (b["language"] or {"table": "table", "graph": "diagram"}.get(b.get("kind"), "code")).upper()
        if self.focus and self.focus["total"]:
            tag += f"   {self.focus['index'] + 1}/{self.focus['total']}"
        lay = layout(cr, tag, MONO, 10, spacing=1.5)
        draw_layout(cr, lay, W - 16 - size_of(lay)[0], 15, rgba((108, 129, 147)))
        cr.set_source_rgba(*rgba(CY, .18))
        cr.rectangle(12, 40, W - 24, 1)
        cr.fill()

        kind = b.get("kind", "code")
        if kind == "table":
            self.draw_table(cr, now)
        elif kind == "graph":
            self.draw_graph(cr, now)
        else:
            self.draw_code(cr, now)
        hint = layout(cr, 'SAY "KEEP IT" TO OPEN IN YOUR BROWSER', DISPLAY, 9, "Medium", spacing=1.8)
        draw_layout(cr, hint, 16, H - 22, rgba((108, 129, 147), .8))
        cr.restore()
        if 0 < k < 1:
            cr.set_source_rgba(.9, 1, 1, .9)
            cr.rectangle(0, reveal - 1, W, 1.5)
            cr.fill()
        return False


class ScanFlash(Gtk.Window):
    """A brief, click-through, full-monitor layer that sweeps a scan line over the window
    Icarus just captured, so it's always visible when he looks at your screen."""
    DURATION = .85

    def __init__(self, rect):
        super().__init__()
        x, y, w, h = rect
        display = Gdk.Display.get_default()
        mon, geo = None, None
        for i in range(display.get_n_monitors()):
            m = display.get_monitor(i)
            g = m.get_geometry()
            if g.x <= x + w / 2 < g.x + g.width and g.y <= y + h / 2 < g.y + g.height:
                mon, geo = m, g
                break
        if mon is None:
            return
        self.rect = (x - geo.x, y - geo.y, w, h)
        self.set_app_paintable(True)
        visual = self.get_screen().get_rgba_visual()
        if visual:
            self.set_visual(visual)
        GtkLayerShell.init_for_window(self)
        GtkLayerShell.set_namespace(self, "icarus-scan")
        GtkLayerShell.set_layer(self, GtkLayerShell.Layer.OVERLAY)
        for edge in (GtkLayerShell.Edge.TOP, GtkLayerShell.Edge.BOTTOM, GtkLayerShell.Edge.LEFT, GtkLayerShell.Edge.RIGHT):
            GtkLayerShell.set_anchor(self, edge, True)
        GtkLayerShell.set_exclusive_zone(self, -1)
        GtkLayerShell.set_keyboard_mode(self, GtkLayerShell.KeyboardMode.NONE)
        GtkLayerShell.set_monitor(self, mon)
        self.connect("draw", self.on_draw)
        self.connect("realize", lambda _w: self.input_shape_combine_region(cairo.Region()))
        self.t0 = time.monotonic()
        self.show_all()
        GLib.timeout_add(16, self.on_tick)

    def on_tick(self):
        if time.monotonic() - self.t0 > self.DURATION:
            self.destroy()
            return False
        self.queue_draw()
        return True

    def on_draw(self, _w, cr):
        cr.set_operator(cairo.OPERATOR_CLEAR)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        k = (time.monotonic() - self.t0) / self.DURATION
        alpha = 1.0 if k < .7 else max(0.0, (1 - k) / .3)
        x, y, w, h = self.rect
        cr.set_source_rgba(*rgba(CY, .05 * alpha))
        cr.rectangle(x, y, w, h)
        cr.fill()
        L = min(42, w * .12, h * .12)
        cr.set_source_rgba(*rgba(CY, .9 * alpha))
        cr.set_line_width(2)
        for cx, cy, dx, dy in ((x, y, 1, 1), (x + w, y, -1, 1), (x, y + h, 1, -1), (x + w, y + h, -1, -1)):
            cr.move_to(cx + dx * L, cy)
            cr.line_to(cx, cy)
            cr.line_to(cx, cy + dy * L)
        cr.stroke()
        sy = y + h * min(1, k / .75)
        g = cairo.LinearGradient(0, sy - 70, 0, sy)
        g.add_color_stop_rgba(0, *rgba(CY, 0))
        g.add_color_stop_rgba(1, *rgba(CY, .22 * alpha))
        cr.set_source(g)
        cr.rectangle(x, sy - 70, w, 70)
        cr.fill()
        cr.set_source_rgba(.85, 1, 1, .95 * alpha)
        cr.rectangle(x, sy - 1, w, 2)
        cr.fill()
        lay = layout(cr, "ICARUS // LOOKING", DISPLAY, 12, "SemiBold", spacing=2.4)
        draw_layout(cr, lay, x + 10, y + 8, rgba(CY, alpha))
        return False


class Overlay(Gtk.Window):
    def __init__(self, avatar, monitor_index=None, margin=8, output=None):
        super().__init__()
        self.avatar = avatar
        self.renderer = Renderer(avatar)
        self.set_app_paintable(True)
        visual = self.get_screen().get_rgba_visual()
        if visual:
            self.set_visual(visual)
        GtkLayerShell.init_for_window(self)
        GtkLayerShell.set_namespace(self, "icarus-avatar")
        GtkLayerShell.set_layer(self, GtkLayerShell.Layer.OVERLAY)
        GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.BOTTOM, True)
        GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.RIGHT, True)
        GtkLayerShell.set_margin(self, GtkLayerShell.Edge.BOTTOM, margin)
        GtkLayerShell.set_margin(self, GtkLayerShell.Edge.RIGHT, margin)
        GtkLayerShell.set_exclusive_zone(self, 0)
        GtkLayerShell.set_keyboard_mode(self, GtkLayerShell.KeyboardMode.NONE)
        mon = monitor_for_output(output) if output else None
        if mon is None and monitor_index is not None:
            mon = Gdk.Display.get_default().get_monitor(monitor_index)
        if mon:
            GtkLayerShell.set_monitor(self, mon)
        self.monitor = mon
        self.set_size_request(SW, SH)
        self.connect("draw", self.on_draw)
        self.connect("realize", self.on_realize)
        self.last = time.monotonic()
        self.frame = 0
        GLib.timeout_add(33, self.on_tick)

    def on_realize(self, _w):
        # Click-through everywhere: an empty input region.
        self.input_shape_combine_region(cairo.Region())

    def on_tick(self):
        now = time.monotonic()
        dt = min(.1, now - self.last)
        self.last = now
        self.avatar.tick(now, dt)
        self.frame += 1
        if self.avatar.busy() or self.frame % 3 == 0:
            self.queue_draw()
        return True

    def on_draw(self, _w, cr):
        self.renderer.draw(cr, time.monotonic())
        return False


def feed_socket(path, avatar):
    """Read events from the daemon; reconnect forever. Runs in a thread."""
    while True:
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(path)
            sock.sendall(b'{"type":"ready"}\n')
            GLib.idle_add(lambda: setattr(avatar, "connected", True) or False)
            buf = b""
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line.strip():
                        try:
                            ev = json.loads(line)
                        except ValueError:
                            continue
                        GLib.idle_add(lambda ev=ev: avatar.apply(ev) or False)
        except OSError:
            pass
        GLib.idle_add(lambda: (setattr(avatar, "connected", False), avatar.apply({"type": "clear"}), avatar.set_state("dormant")) and False)
        time.sleep(2)


DEMO = [
    (1.5, {"type": "ambient", "heat": .35, "unread": 1, "tasks_running": 0, "recent_task": False}),
    (0.0, {"type": "state", "name": "listening"}),
    (0.0, {"type": "transcript", "text": "", "listening": True}),
    (2.2, {"type": "transcript", "text": "have claude fix the failing subagent test"}),
    (0.0, {"type": "state", "name": "thinking"}),
    (0.6, {"type": "chip", "id": "c1", "text": "Preparing Claude task"}),
    (0.9, {"type": "chip", "id": "c1", "done": True}),
    (0.0, {"type": "state", "name": "confirm"}),
    (0.0, {"type": "stamp", "word": "AUTHORIZE", "sub": "REQ 4471 // EDIT ICARUS", "color": "amber", "ms": 1600, "wings": 1.2}),
    (1.1, {"type": "card", "kind": "confirm", "title": "Edit Icarus with Claude?", "body": "Runs in a worktree on branch claude/sub-4471. Nothing merges.", "meta": "SUPER+I confirm · SUPER+ESC cancel"}),
    (2.0, {"type": "card", "kind": "claude", "title": "⧉ Claude · Icarus", "body": "running in claude/sub-4471", "meta": "mode: edit"}),
    (0.0, {"type": "state", "name": "acting"}),
    (0.0, {"type": "chip", "id": "c2", "text": "Running pytest"}),
    (1.2, {"type": "chip", "id": "c2", "done": True}),
    (0.0, {"type": "stamp", "word": "COMPLETE", "sub": "CLAUDE // claude/sub-4471", "color": "cyan", "ms": 1500, "wings": 1.8}),
    (1.4, {"type": "card_clear"}),
    (0.0, {"type": "transcript_clear"}),
    (0.0, {"type": "state", "name": "speaking"}),
    (0.0, {"type": "subtitle", "text": "Done. The test was racing the registry cleanup.", "duration_ms": 2600}),
    (0.9, {"type": "fire", "seconds": .8}),
    (1.9, {"type": "subtitle", "text": "Fix is on claude/sub-4471 when you're ready.", "duration_ms": 2400}),
    (2.6, {"type": "state", "name": "linger"}),
    (4.0, {"type": "clear"}),
    (0.0, {"type": "state", "name": "dormant"}),
    (0.0, {"type": "tasks", "items": [{"id": "sub-4472", "title": "add a running-task badge beside the orb", "project": "icarus",
                                        "mode": "edit", "text": "Claude: editing icarus", "steps": 0, "started": time.time() - 64}]}),
    (2.0, {"type": "tasks", "items": [{"id": "sub-4472", "title": "add a running-task badge beside the orb", "project": "icarus",
                                        "mode": "edit", "text": "Editing avatar.py", "steps": 7, "started": time.time() - 64}]}),
    (2.0, {"type": "tasks", "items": [{"id": "sub-4472", "title": "add a running-task badge beside the orb", "project": "icarus",
                                        "mode": "edit", "text": "Running python -m unittest discover", "steps": 12, "started": time.time() - 64}]}),
    (2.0, {"type": "ambient", "heat": .9, "unread": 0, "tasks_running": 1, "recent_task": False}),
    (0.0, {"type": "state", "name": "listening"}),
    (0.0, {"type": "transcript", "text": "", "listening": True}),
    (2.0, {"type": "transcript", "text": "how hot is the gpu"}),
    (0.0, {"type": "state", "name": "thinking"}),
    (1.0, {"type": "state", "name": "speaking"}),
    (0.0, {"type": "transcript_clear"}),
    (0.0, {"type": "subtitle", "text": "Ninety percent and climbing. I can feel it.", "duration_ms": 2500}),
    (9.0, {"type": "state", "name": "linger"}),
    (3.0, {"type": "clear"}),
    (0.0, {"type": "state", "name": "dormant"}),
    (3.0, {"type": "tasks", "items": []}),
    (0.0, {"type": "ambient", "heat": .3, "unread": 0, "tasks_running": 0, "recent_task": False}),
]


def run_demo(avatar, loop):
    def play(i=0):
        delay, ev = DEMO[i % len(DEMO)]
        avatar.apply(ev)
        if ev.get("type") == "state" and ev.get("name") == "speaking":
            def talk():
                avatar.apply({"type": "speech_level", "rms": random.random() * .2})
                return avatar.state == "speaking"
            GLib.timeout_add(60, talk)
        if ev.get("type") == "state" and ev.get("name") == "listening":
            def hear():
                avatar.apply({"type": "mic_level", "rms": random.random() * .15})
                return avatar.state == "listening"
            GLib.timeout_add(50, hear)
        if i + 1 >= len(DEMO) and not loop:
            GLib.timeout_add(int(delay * 1000) + 1500, lambda: Gtk.main_quit() or False)
            return False
        GLib.timeout_add(int(max(delay, .01) * 1000), lambda: play(i + 1) or False)
        return False
    avatar.connected = True
    play()


def main():
    parser = argparse.ArgumentParser(description="Icarus avatar overlay")
    parser.add_argument("--socket", help="daemon renderer socket path")
    parser.add_argument("--demo", action="store_true", help="play a scripted walk through every state")
    parser.add_argument("--loop", action="store_true", help="with --demo, repeat forever")
    parser.add_argument("--fire-demo", action="store_true", help="stay on screen and ignite every 2 s (for tuning the fire glitch)")
    parser.add_argument("--board-demo", nargs="?", const="code", choices=("code", "table", "graph"),
                        help="show a 'show me' board and step through its beats")
    parser.add_argument("--monitor", type=int, default=None, help="monitor index (default: compositor's choice)")
    parser.add_argument("--output", default=None, help="Hyprland output name, e.g. DP-2 (wins over --monitor)")
    parser.add_argument("--margin", type=int, default=8)
    args = parser.parse_args()
    if not GtkLayerShell.is_supported():
        sys.exit("gtk-layer-shell is not supported by this compositor")
    load_fonts()
    avatar = Avatar()
    win = Overlay(avatar, args.monitor, args.margin, args.output)
    win.show_all()
    avatar.board_window = BoardWindow(win.monitor, args.margin)
    if args.board_demo:
        avatar.connected = True
        code = ("def lcs(a, b):\n    n, m = len(a), len(b)\n    # dp[i][j]: LCS length of a[:i] and b[:j]\n"
                "    dp = [[0] * (m + 1) for _ in range(n + 1)]\n    for i in range(1, n + 1):\n        for j in range(1, m + 1):\n"
                "            if a[i - 1] == b[j - 1]:\n                dp[i][j] = dp[i - 1][j - 1] + 1\n            else:\n"
                "                dp[i][j] = max(dp[i - 1][j], dp[i][j - 1])\n    return dp[n][m]")
        beats = [([3, 4], "a table, one cell per prefix pair"), ([5, 6], "fill row by row"),
                 ([7, 8], "match: extend the diagonal"), ([9, 10], "no match: best of skipping either"), ([11], "answer in the corner")]
        avatar.apply({"type": "state", "name": "speaking"})
        if args.board_demo == "table":
            # 0/1 knapsack, capacity 0..5, items (w, v) = (1, 1), (3, 4), (4, 5)
            vals = [[0, 1, 1, 1, 1, 1], [0, 1, 1, 4, 5, 5], [0, 1, 1, 4, 5, 6]]
            grid = [["0"] + [""] * 5 for _ in range(3)]
            avatar.apply({"type": "board", "id": "demo", "kind": "table", "title": "2D DP: 0/1 knapsack", "rows": 3, "cols": 6,
                          "cells": grid, "row_labels": ["w1 v1", "w3 v4", "w4 v5"], "col_labels": [f"c={c}" for c in range(6)]})
            beats = []
            for r in range(3):
                fill = [[r, c, str(vals[r][c])] for c in range(1, 6)]
                arrows = [[r - 1, 4, r, 4], [r - 1, 4 - [1, 3, 4][r], r, 4]] if r and 4 - [1, 3, 4][r] >= 0 else []
                beats.append(([[r, 4]], fill, arrows, f"row {r}: best value using items up to {r}"))
            def next_beat():
                cells, fill, arrows, note = beats[step["i"] % len(beats)]
                if step["i"] % len(beats) == 0:
                    avatar.apply({"type": "board", "id": "demo", "kind": "table", "title": "2D DP: 0/1 knapsack", "rows": 3, "cols": 6,
                                  "cells": [["0"] + [""] * 5 for _ in range(3)], "row_labels": ["w1 v1", "w3 v4", "w4 v5"],
                                  "col_labels": [f"c={c}" for c in range(6)]})
                avatar.apply({"type": "board_focus", "id": "demo", "index": step["i"] % len(beats), "total": len(beats),
                              "cells": cells, "fill": fill, "arrows": arrows, "note": note})
                step["i"] += 1
                return True
            step = {"i": 0}
            GLib.timeout_add(1300, next_beat)
        elif args.board_demo == "graph":
            nodes = [{"id": "hk", "label": "Hotkey"}, {"id": "mic", "label": "pw-record"}, {"id": "stt", "label": "Whisper"},
                     {"id": "api", "label": "icarus-api"}, {"id": "l1", "label": "Icarus L1"}, {"id": "cl", "label": "Councilor"},
                     {"id": "tts", "label": "Kokoro"}, {"id": "av", "label": "Avatar"}]
            edges = [{"from": a, "to": b} for a, b in (("hk", "mic"), ("mic", "stt"), ("stt", "api"), ("api", "l1"), ("l1", "cl"),
                                                        ("l1", "tts"), ("tts", "av"), ("cl", "av"))]
            avatar.apply({"type": "board", "id": "demo", "kind": "graph", "title": "One voice turn", "nodes": nodes, "edges": edges})
            gbeats = [(["hk", "mic", "stt"], [["hk", "mic"], ["mic", "stt"]], "your voice, transcribed locally"),
                      (["api", "l1"], [["stt", "api"], ["api", "l1"]], "the text becomes an Icarus turn"),
                      (["cl"], [["l1", "cl"]], "big jobs go to the Councilor"),
                      (["tts", "av"], [["l1", "tts"], ["tts", "av"]], "the answer, spoken")]
            step = {"i": 0}
            def next_beat():
                ns, es, note = gbeats[step["i"] % len(gbeats)]
                avatar.apply({"type": "board_focus", "id": "demo", "index": step["i"] % len(gbeats), "total": len(gbeats),
                              "nodes": ns, "edges": es, "note": note})
                step["i"] += 1
                return True
            GLib.timeout_add(1300, next_beat)
        else:
          avatar.apply({"type": "board", "id": "demo", "title": "2D DP: longest common subsequence", "code": code, "language": "python"})
          step = {"i": 0}
          def next_beat():
            lines, note = beats[step["i"] % len(beats)]
            avatar.apply({"type": "board_focus", "id": "demo", "index": step["i"] % len(beats), "total": len(beats), "lines": lines, "note": note})
            avatar.apply({"type": "subtitle", "text": note.capitalize() + ".", "duration_ms": 1500})
            step["i"] += 1
            return True
          GLib.timeout_add(1200, next_beat)
    elif args.fire_demo:
        avatar.connected = True
        avatar.apply({"type": "state", "name": "speaking"})
        avatar.apply({"type": "subtitle", "text": "Testing the fire glitch.", "duration_ms": 2000})
        GLib.timeout_add(1500, lambda: avatar.ignite(1.0) or True)
    elif args.demo:
        run_demo(avatar, args.loop)
    elif args.socket:
        threading.Thread(target=feed_socket, args=(args.socket, avatar), daemon=True).start()
    else:
        sys.exit("pass --socket PATH or --demo")
    Gtk.main()


if __name__ == "__main__":
    main()
