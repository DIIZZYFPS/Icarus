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
        self.particles = []
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
        elif kind == "mic_level":
            self.mic = clamp(float(ev.get("rms", 0)) * 6, 0, 1)
        elif kind == "speech_level":
            self.speech = clamp(float(ev.get("rms", 0)) * 5, 0, 1)
        elif kind == "transcript":
            self.transcript = {"text": ev.get("text", ""), "listening": bool(ev.get("listening"))}
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
        elif kind == "clear":
            self.chips, self.card, self.subtitle, self.transcript = [], None, None, None

    def fire_stamp(self, word, sub, col, ms):
        now = time.monotonic()
        self.stamp = {"word": word, "sub": sub or "ICARUS // SIGNAL", "col": col, "t0": now, "ms": ms}
        self.freeze_until = now + .14
        self.flash_until = now + .07
        self.flash_col = col
        self.glitch_until = now + .16

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
        self.burn_stage = stage5
        if self.scorch < .2:
            self.hot_stamped = False

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
        hot = sun_color(a.heat)
        edge = rgba(hot, .55)
        tq = a.step / 12

        def wind(i):
            return math.sin(tq * 2.3 + i * 1.7) * .7 + math.sin(tq * 5.1 + i * .9) * .3

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

        # blade wings
        for side in (-1, 1):
            for i in (3, 2, 1, 0):
                ang = -math.pi / 2 + side * lerp(.1 + i * .07, .55 + i * .33, a.spread) + wind(i + side * 4) * .02
                L = (228 - i * 28) * (1 - a.scorch * .2 * (1 - i * .15))
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
                if a.scorch > .05:
                    tx, ty = rx + ca * L, ry + sa * L
                    glow_line(c, EMBER, min(1, a.scorch * 1.5) * (.7 + .3 * math.sin(t * 9 + i)), 1.6, 8,
                              lambda: (c.move_to(tx - ca * 10 + px * 3, ty - sa * 10 + py * 3), c.line_to(tx, ty)))
                    if random.random() < a.scorch * .08:
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
        if a.scorch > .03:
            c.save()
            c.set_dash([2 + a.scorch * 10, 10], -t * 18)
            ridge = top + top_l
            def ridge_path():
                # two runs from the hood peak down each shoulder, never across the chest
                for run in (top, [top[0]] + top_l[::-1]):
                    c.move_to(*run[0])
                    for pt in run[1:]:
                        c.line_to(*pt)
            glow_line(c, EMBER, min(1, a.scorch * 1.6), 1.6, 8, ridge_path)
            c.restore()
            if random.random() < a.scorch * .25:
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

        self.draw_frame_graphics(c, t, hot)

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
        if self.a.p > .001:
            self.render_character(t)
            self.composite_character(cr, t)
        self.draw_column(cr, t)
        self.draw_transcript(cr, t)
        self.draw_stamp(cr, t)


# ── window + event feed ──────────────────────────────────────────────────────

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
    (2.8, {"type": "subtitle", "text": "Fix is on claude/sub-4471 when you're ready.", "duration_ms": 2400}),
    (2.6, {"type": "state", "name": "linger"}),
    (4.0, {"type": "clear"}),
    (0.0, {"type": "state", "name": "dormant"}),
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
    if args.demo:
        run_demo(avatar, args.loop)
    elif args.socket:
        threading.Thread(target=feed_socket, args=(args.socket, avatar), daemon=True).start()
    else:
        sys.exit("pass --socket PATH or --demo")
    Gtk.main()


if __name__ == "__main__":
    main()
