"""Procedural avatar renderer (pure cairo — no GTK, so it can be rendered headless to PNG).

Original vector character: silver hair, cyan eyes, navy/white/gold attire, holographic accents.
Everything is drawn from code, so there is no third-party artwork to license, it scales to any
size, and every expression is a parameter rather than a sprite sheet.

Coordinate system: a 240 × 330 logical canvas; the caller sets the scale.
The top 80 px are reserved for the speech bubble.
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field

import cairo

W, H = 240, 330
CX, HEAD_Y = 120, 182

# palette
CYAN = (0.37, 0.91, 1.0)
CYAN_DEEP = (0.10, 0.62, 0.78)
GOLD = (0.85, 0.70, 0.40)
NAVY = (0.06, 0.09, 0.18)
NAVY_2 = (0.10, 0.14, 0.26)
WHITE = (0.96, 0.97, 1.0)
SKIN = (0.98, 0.91, 0.87)
SKIN_SHADE = (0.93, 0.80, 0.76)
HAIR_LIGHT = (0.93, 0.95, 0.99)
HAIR_MID = (0.78, 0.82, 0.91)
HAIR_DARK = (0.55, 0.60, 0.74)
INK = (0.10, 0.12, 0.25)

STATE_COLORS = {
    "IDLE": CYAN, "LISTENING": (0.45, 1.0, 0.95), "THINKING": (0.62, 0.70, 1.0), "SPEAKING": CYAN,
    "WORKING": GOLD, "SUCCESS": (0.95, 0.82, 0.45), "ERROR": (1.0, 0.45, 0.35), "SLEEPING": (0.45, 0.55, 0.7),
}

# Expression parameters per mood:
#   lid: upper-lid openness 0..1, smile: -1 frown .. 1 smile, brow: tilt (+ = worried/inner-up,
#   - = angry/inner-down), brow_y: raise, blush: 0..1, smirk: asymmetric mouth, happy_eyes: ^^ arcs
MOODS = {
    "calm":      dict(lid=1.0,  smile=0.15, brow=0.0,  brow_y=0.0,  blush=0.15, smirk=0.0, happy_eyes=0.0, tilt=0.0),
    "amused":    dict(lid=0.85, smile=0.75, brow=0.1,  brow_y=1.5,  blush=0.35, smirk=0.0, happy_eyes=0.55, tilt=-2.0),
    "smug":      dict(lid=0.55, smile=0.35, brow=-0.15, brow_y=1.0, blush=0.15, smirk=0.8, happy_eyes=0.0, tilt=3.0),
    "curious":   dict(lid=1.1,  smile=0.1,  brow=0.25, brow_y=3.0,  blush=0.15, smirk=0.0, happy_eyes=0.0, tilt=5.0),
    "concerned": dict(lid=1.0,  smile=-0.35, brow=0.65, brow_y=1.5, blush=0.1,  smirk=0.0, happy_eyes=0.0, tilt=-1.5),
    "proud":     dict(lid=0.8,  smile=0.65, brow=-0.05, brow_y=2.0, blush=0.4,  smirk=0.25, happy_eyes=0.3, tilt=-3.0),
    "annoyed":   dict(lid=0.6,  smile=-0.25, brow=-0.55, brow_y=-1.0, blush=0.0, smirk=-0.4, happy_eyes=0.0, tilt=0.0),
    "sheepish":  dict(lid=0.85, smile=0.2,  brow=0.55, brow_y=1.0,  blush=0.65, smirk=0.0, happy_eyes=0.2, tilt=4.0),
    "warm":      dict(lid=0.8,  smile=0.7,  brow=0.15, brow_y=1.0,  blush=0.55, smirk=0.0, happy_eyes=0.6, tilt=-2.0),
    "sleepy":    dict(lid=0.3,  smile=0.0,  brow=0.2,  brow_y=-0.5, blush=0.1,  smirk=0.0, happy_eyes=0.0, tilt=6.0),
    "bored":     dict(lid=0.5,  smile=-0.1, brow=0.0,  brow_y=-1.0, blush=0.0,  smirk=0.3, happy_eyes=0.0, tilt=4.0),
}


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def ease(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


@dataclass
class Bubble:
    text: str
    born: float
    duration: float
    kind: str = "quip"  # quip | info | warn


@dataclass
class AvatarModel:
    """All animation state. The renderer only reads it; the UI updates it from backend events."""
    state: str = "IDLE"
    mood: str = "calm"
    connected: bool = True
    ghost: bool = False
    hover: bool = False
    state_since: float = field(default_factory=time.monotonic)
    # smoothed expression parameters (lerped toward the mood target each frame)
    expr: dict = field(default_factory=lambda: dict(MOODS["calm"]))
    # blink
    next_blink: float = field(default_factory=lambda: time.monotonic() + 2.5)
    blink_t: float = -1.0
    # gaze (smoothed) and target
    gaze: list = field(default_factory=lambda: [0.0, 0.0])
    gaze_target: list = field(default_factory=lambda: [0.0, 0.0])
    next_saccade: float = 0.0
    # speech
    speech_env: list = field(default_factory=list)
    speech_frame_ms: int = 40
    speech_start: float = 0.0
    speech_end: float = 0.0
    text_pulse: float = 0.0       # bumps on each streamed token when there's no audio
    mouth_open: float = 0.0
    bubble: Bubble | None = None
    particles: list = field(default_factory=list)

    def set_state(self, state: str, mood: str | None = None) -> None:
        if state != self.state:
            self.state = state
            self.state_since = time.monotonic()
            if state == "SUCCESS":
                self._burst()
        if mood:
            self.mood = mood

    def say(self, text: str, kind: str = "quip", duration: float | None = None) -> None:
        text = text.strip()
        if not text:
            return
        if len(text) > 140:
            text = text[:137].rstrip() + "…"
        self.bubble = Bubble(text, time.monotonic(), duration or min(9.0, 3.0 + len(text) * 0.055), kind)

    def start_speech(self, env: list[float], frame_ms: int, duration: float) -> None:
        now = time.monotonic()
        self.speech_env, self.speech_frame_ms = env, frame_ms
        self.speech_start, self.speech_end = now, now + duration

    def stop_speech(self) -> None:
        self.speech_env = []
        self.speech_end = 0.0

    def token(self) -> None:
        self.text_pulse = 1.0

    def _burst(self) -> None:
        now = time.monotonic()
        for i in range(10):
            a = i / 10 * math.tau + random.uniform(-0.2, 0.2)
            self.particles.append({"kind": "spark", "a": a, "born": now, "life": 1.2, "r0": 40,
                                   "speed": random.uniform(45, 75)})

    @property
    def animating(self) -> bool:
        """True when a high frame rate is needed (anything moving beyond idle breathing)."""
        now = time.monotonic()
        return (self.state in ("LISTENING", "THINKING", "SPEAKING", "WORKING", "SUCCESS", "ERROR")
                or self.blink_t >= 0 or now < self.speech_end or self.text_pulse > 0.01
                or any(p["kind"] != "z" for p in self.particles) or self.hover
                or abs(self.gaze[0] - self.gaze_target[0]) + abs(self.gaze[1] - self.gaze_target[1]) > 0.08
                or (self.bubble is not None and now - self.bubble.born < 0.4))


class Renderer:
    def __init__(self) -> None:
        self.t0 = time.monotonic()

    # ------------------------------------------------------------------ update
    def update(self, m: AvatarModel, dt: float) -> None:
        now = time.monotonic()
        target = MOODS.get("sleepy" if m.state == "SLEEPING" else m.mood, MOODS["calm"])
        k = 1 - math.exp(-dt * 6)
        for key, val in target.items():
            m.expr[key] = lerp(m.expr.get(key, val), val, k)

        # blinking (never while asleep — eyes are closed anyway)
        if m.state != "SLEEPING":
            if m.blink_t < 0 and now >= m.next_blink:
                m.blink_t = 0.0
            if m.blink_t >= 0:
                m.blink_t += dt
                if m.blink_t > 0.16:
                    m.blink_t = -1.0
                    m.next_blink = now + random.uniform(2.5, 6.0) * (0.6 if m.state == "THINKING" else 1.0)
                    if random.random() < 0.15:
                        m.next_blink = now + 0.25  # occasional double blink

        # gaze: where she looks depends on what she's doing
        if m.state == "THINKING":
            m.gaze_target = [2.2 * math.cos(now * 0.7), -2.6]
        elif m.state == "WORKING":
            m.gaze_target = [1.5 * math.sin(now * 1.3), 2.0]
        elif m.state == "LISTENING":
            m.gaze_target = [0.0, 0.0]
        elif now >= m.next_saccade:
            m.gaze_target = [random.uniform(-2.2, 2.2), random.uniform(-1.2, 1.2)]
            m.next_saccade = now + random.uniform(1.5, 4.0)
        gk = 1 - math.exp(-dt * 10)
        m.gaze[0] = lerp(m.gaze[0], m.gaze_target[0], gk)
        m.gaze[1] = lerp(m.gaze[1], m.gaze_target[1], gk)

        # mouth: audio envelope when speaking aloud, token pulses for silent streaming
        target_open = 0.0
        if now < m.speech_end and m.speech_env:
            idx = int((now - m.speech_start) * 1000 / m.speech_frame_ms)
            if 0 <= idx < len(m.speech_env):
                target_open = m.speech_env[idx] ** 0.7
        elif m.state == "SPEAKING":
            target_open = 0.45 * m.text_pulse * (0.6 + 0.4 * abs(math.sin(now * 17)))
        m.text_pulse = max(0.0, m.text_pulse - dt * 5)
        m.mouth_open = lerp(m.mouth_open, target_open, 1 - math.exp(-dt * 25))

        # ambient particles
        if m.state == "THINKING" and len(m.particles) < 14 and random.random() < dt * 12:
            m.particles.append({"kind": "mote", "a": random.uniform(0, math.tau), "born": now,
                                "life": random.uniform(1.6, 2.6), "r0": random.uniform(62, 82),
                                "speed": random.uniform(0.6, 1.4)})
        if m.state == "SLEEPING" and random.random() < dt * 0.5 and len(m.particles) < 3:
            m.particles.append({"kind": "z", "born": now, "life": 3.5, "x": CX + 48, "y": HEAD_Y - 40})
        m.particles = [p for p in m.particles if now - p["born"] < p["life"]]

        if m.bubble and now - m.bubble.born > m.bubble.duration:
            m.bubble = None

    # ------------------------------------------------------------------ draw
    def draw(self, cr: cairo.Context, m: AvatarModel, scale: float = 1.0) -> None:
        now = time.monotonic()
        t = now - self.t0
        cr.save()
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        cr.scale(scale, scale)

        asleep = m.state == "SLEEPING"
        dim = 0.78 if asleep else (0.55 if not m.connected else 1.0)
        breathe = math.sin(t * math.tau / (5.5 if asleep else 4.2))
        bob = breathe * (1.0 if asleep else 1.4)
        e = m.expr
        state_col = STATE_COLORS.get(m.state, CYAN) if m.connected else (0.5, 0.55, 0.65)

        grouped = dim < 0.999
        if grouped:  # an offscreen group costs a full-surface composite: only pay it when dimmed
            cr.push_group()
        self._halo(cr, m, t, state_col)
        cr.save()
        cr.translate(0, bob)
        self._back_hair(cr, t)
        self._body(cr, t, breathe)
        # head group tilts around the neck
        cr.save()
        cr.translate(CX, HEAD_Y + 40)
        cr.rotate(math.radians(e["tilt"] * 0.6 + (2.5 if asleep else 0) + math.sin(t * 0.5) * 0.4))
        cr.translate(-CX, -(HEAD_Y + 40))
        self._face(cr, m, t)
        self._front_hair(cr, t)
        # brows sit on top of the bangs (anime convention) so emotion stays readable
        for side in (-1, 1):
            self._brow(cr, CX + side * 21, 169 - m.expr["brow_y"], side, m.expr["brow"])
        self._ornament(cr, m, t, state_col)
        cr.restore()
        cr.restore()
        self._effects(cr, m, t, state_col)
        if grouped:
            cr.pop_group_to_source()
            cr.paint_with_alpha(dim)

        if not m.connected:
            self._disconnected_badge(cr, t)
        if m.bubble:
            self._bubble(cr, m.bubble, now)
        if m.ghost:
            cr.set_source_rgba(*CYAN, 0.5)
            cr.set_font_size(9)
            cr.move_to(CX - 18, H - 4)
            cr.show_text("ghost")
        cr.restore()

    # ------------------------------------------------------------------ parts
    def _halo(self, cr, m: AvatarModel, t: float, col) -> None:
        speed = {"THINKING": 1.4, "WORKING": 0.9, "LISTENING": 0.5, "SPEAKING": 0.35}.get(m.state, 0.12)
        strength = {"IDLE": 0.22, "SLEEPING": 0.08}.get(m.state, 0.45)
        if m.hover:
            strength += 0.15
        # soft radial glow
        g = cairo.RadialGradient(CX, HEAD_Y, 20, CX, HEAD_Y, 105)
        g.add_color_stop_rgba(0, *col, 0.20 * strength)
        g.add_color_stop_rgba(1, *col, 0.0)
        cr.set_source(g)
        cr.arc(CX, HEAD_Y, 105, 0, math.tau)
        cr.fill()
        # rotating holographic ring segments (one stroke per ring)
        cr.set_line_width(1.4)
        for i, (r, n, dirn) in enumerate(((88, 3, 1), (96, 5, -1))):
            cr.new_path()
            for k in range(n):
                a0 = t * speed * dirn + k * math.tau / n + i
                cr.new_sub_path()
                cr.arc(CX, HEAD_Y, r, a0, a0 + math.tau / n * 0.55)
            cr.set_source_rgba(*col, strength * (0.9 if i == 0 else 0.55))
            cr.stroke()
        # expanding rings for SUCCESS / ERROR (behind her, never across her face)
        age = time.monotonic() - m.state_since
        if m.state == "SUCCESS":
            ph = min(1.0, age / 1.2)
            cr.set_source_rgba(*GOLD, 0.7 * (1 - ph))
            cr.set_line_width(2.5)
            cr.arc(CX, HEAD_Y, 70 + ph * 45, 0, math.tau)
            cr.stroke()
        elif m.state == "ERROR":
            ph = (age * 1.3) % 1.0
            cr.set_source_rgba(*col, 0.55 * (1 - ph))
            cr.set_line_width(3)
            cr.arc(CX, HEAD_Y, 70 + ph * 40, 0, math.tau)
            cr.stroke()
        # tick marks — the "magic circle" look (single path)
        cr.set_line_width(1)
        cr.new_path()
        for k in range(24):
            a = k * math.tau / 24 - t * speed * 0.5
            r1, r2 = 100, 100 + (4 if k % 3 == 0 else 2)
            cr.move_to(CX + r1 * math.cos(a), HEAD_Y + r1 * math.sin(a))
            cr.line_to(CX + r2 * math.cos(a), HEAD_Y + r2 * math.sin(a))
        cr.set_source_rgba(*col, strength * 0.6)
        cr.stroke()

    def _hair_gradient(self, y0: float, y1: float):
        g = cairo.LinearGradient(CX - 40, y0, CX + 30, y1)
        g.add_color_stop_rgb(0, *HAIR_LIGHT)
        g.add_color_stop_rgb(0.45, *HAIR_MID)
        g.add_color_stop_rgb(1, *HAIR_DARK)
        return g

    def _back_hair(self, cr, t: float) -> None:
        sway = math.sin(t * 0.9) * 2.0
        cr.new_path()
        cr.move_to(CX, 112)
        cr.curve_to(CX - 62, 108, CX - 74, 150, CX - 70, 200)
        cr.curve_to(CX - 68, 245, CX - 80 + sway, 285, CX - 66 + sway, 318)
        cr.line_to(CX - 30 + sway * 0.6, 300)
        cr.line_to(CX + 30 + sway * 0.6, 300)
        cr.line_to(CX + 66 + sway, 318)
        cr.curve_to(CX + 80 + sway, 285, CX + 68, 245, CX + 70, 200)
        cr.curve_to(CX + 74, 150, CX + 62, 108, CX, 112)
        cr.close_path()
        cr.set_source(self._hair_gradient(110, 320))
        cr.fill_preserve()
        cr.set_source_rgba(*HAIR_DARK, 0.6)
        cr.set_line_width(1)
        cr.stroke()

    def _body(self, cr, t: float, breathe: float) -> None:
        sh = breathe * 0.8
        # neck
        cr.rectangle(CX - 10, 214, 20, 26)
        g = cairo.LinearGradient(0, 214, 0, 240)
        g.add_color_stop_rgb(0, *SKIN_SHADE)
        g.add_color_stop_rgb(1, *SKIN)
        cr.set_source(g)
        cr.fill()
        # coat
        cr.new_path()
        cr.move_to(CX - 14, 236)
        cr.curve_to(CX - 40, 244 - sh, CX - 70, 248 - sh, CX - 82, 272)
        cr.curve_to(CX - 92, 292, CX - 94, 315, CX - 96, H)
        cr.line_to(CX + 96, H)
        cr.curve_to(CX + 94, 315, CX + 92, 292, CX + 82, 272)
        cr.curve_to(CX + 70, 248 - sh, CX + 40, 244 - sh, CX + 14, 236)
        cr.close_path()
        g = cairo.LinearGradient(0, 236, 0, H)
        g.add_color_stop_rgb(0, *NAVY_2)
        g.add_color_stop_rgb(1, *NAVY)
        cr.set_source(g)
        cr.fill()
        # white high collar
        for side in (-1, 1):
            cr.new_path()
            cr.move_to(CX + side * 4, 252)
            cr.line_to(CX + side * 16, 226)
            cr.line_to(CX + side * 30, 240)
            cr.line_to(CX + side * 12, 262)
            cr.close_path()
            cr.set_source_rgb(*WHITE)
            cr.fill_preserve()
            cr.set_source_rgba(0.7, 0.75, 0.85, 0.8)
            cr.set_line_width(0.8)
            cr.stroke()
        # gold trim down the front + shoulder piping
        cr.set_source_rgba(*GOLD, 0.95)
        cr.set_line_width(1.6)
        for side in (-1, 1):
            cr.move_to(CX + side * 12, 262)
            cr.curve_to(CX + side * 16, 285, CX + side * 18, 305, CX + side * 20, H)
            cr.stroke()
            cr.move_to(CX + side * 34, 246)
            cr.curve_to(CX + side * 58, 250, CX + side * 74, 258, CX + side * 82, 274)
            cr.stroke()
        # brooch: gold setting + cyan crystal
        cr.arc(CX, 262, 5.5, 0, math.tau)
        cr.set_source_rgb(*GOLD)
        cr.fill()
        cr.arc(CX, 262, 3.4, 0, math.tau)
        g = cairo.RadialGradient(CX - 1, 261, 0.5, CX, 262, 3.4)
        g.add_color_stop_rgb(0, 0.85, 1.0, 1.0)
        g.add_color_stop_rgb(1, *CYAN_DEEP)
        cr.set_source(g)
        cr.fill()

    def _face_path(self, cr) -> None:
        cr.new_path()
        cr.move_to(CX - 46, 160)
        cr.curve_to(CX - 48, 192, CX - 38, 212, CX - 18, 226)
        cr.curve_to(CX - 8, 233, CX + 8, 233, CX + 18, 226)
        cr.curve_to(CX + 38, 212, CX + 48, 192, CX + 46, 160)
        cr.curve_to(CX + 44, 128, CX - 44, 128, CX - 46, 160)
        cr.close_path()

    def _face(self, cr, m: AvatarModel, t: float) -> None:
        e = m.expr
        self._face_path(cr)
        g = cairo.RadialGradient(CX - 8, 175, 10, CX, 185, 62)
        g.add_color_stop_rgb(0, *SKIN)
        g.add_color_stop_rgb(1, *SKIN_SHADE)
        cr.set_source(g)
        cr.fill_preserve()
        cr.set_source_rgba(0.80, 0.62, 0.60, 0.5)
        cr.set_line_width(0.9)
        cr.stroke()
        # blush
        if e["blush"] > 0.02:
            for side in (-1, 1):
                g = cairo.RadialGradient(CX + side * 26, 204, 1, CX + side * 26, 204, 11)
                g.add_color_stop_rgba(0, 1.0, 0.55, 0.6, 0.45 * e["blush"])
                g.add_color_stop_rgba(1, 1.0, 0.55, 0.6, 0)
                cr.set_source(g)
                cr.arc(CX + side * 26, 204, 11, 0, math.tau)
                cr.fill()
        asleep = m.state == "SLEEPING"
        blink = 0.0
        if m.blink_t >= 0:
            blink = 1 - abs(m.blink_t / 0.08 - 1)  # 0→1→0 over 160 ms
        openness = 0.0 if asleep else max(0.0, min(1.15, e["lid"]) * (1 - blink))
        for side in (-1, 1):
            self._eye(cr, CX + side * 21, 186, side, openness, e, m)
        # nose
        cr.set_source_rgba(0.78, 0.58, 0.56, 0.8)
        cr.set_line_width(1.0)
        cr.move_to(CX + 1, 200)
        cr.line_to(CX + 2.5, 205)
        cr.line_to(CX, 206)
        cr.stroke()
        self._mouth(cr, CX, 215, m)

    def _eye(self, cr, x: float, y: float, side: int, openness: float, e: dict, m: AvatarModel) -> None:
        happy = e["happy_eyes"]
        if openness < 0.08 or happy > 0.5:
            # closed / happy "^" eyes
            cr.set_source_rgb(*INK)
            cr.set_line_width(2.2)
            if happy > 0.5 and openness >= 0.08:
                cr.move_to(x - 10, y + 2)
                cr.curve_to(x - 4, y - 7, x + 4, y - 7, x + 10, y + 2)
            else:
                cr.move_to(x - 10, y)
                cr.curve_to(x - 4, y + 5, x + 4, y + 5, x + 10, y)
            cr.stroke()
            return
        h = 9.5 * openness
        cr.save()
        # almond eye-white clip
        cr.new_path()
        cr.move_to(x - 12, y)
        cr.curve_to(x - 6, y - h * 1.15, x + 7, y - h * 1.2, x + 12.5, y - 1)
        cr.curve_to(x + 7, y + h * 0.95, x - 6, y + h * 0.9, x - 12, y)
        cr.close_path()
        cr.set_source_rgb(0.99, 0.99, 1.0)
        cr.fill_preserve()
        cr.clip()
        # iris
        gx, gy = m.gaze
        ix, iy = x + gx + side * 0.4, y + gy + 0.5
        g = cairo.RadialGradient(ix - 1, iy - 2, 1, ix, iy, 8.6)
        g.add_color_stop_rgb(0, 0.75, 1.0, 1.0)
        g.add_color_stop_rgb(0.45, *CYAN)
        g.add_color_stop_rgb(0.85, *CYAN_DEEP)
        g.add_color_stop_rgb(1, 0.03, 0.25, 0.38)
        cr.set_source(g)
        cr.arc(ix, iy, 8.6, 0, math.tau)
        cr.fill()
        # iris ring detail ("magic" glyph ring)
        cr.set_source_rgba(1, 1, 1, 0.35)
        cr.set_line_width(0.6)
        cr.arc(ix, iy, 6.2, 0, math.tau)
        cr.stroke()
        # pupil
        cr.set_source_rgb(0.03, 0.10, 0.18)
        cr.arc(ix, iy + 0.3, 3.4, 0, math.tau)
        cr.fill()
        # highlights
        cr.set_source_rgba(1, 1, 1, 0.95)
        cr.arc(ix - 3, iy - 3.2, 2.4, 0, math.tau)
        cr.fill()
        cr.set_source_rgba(1, 1, 1, 0.75)
        cr.arc(ix + 2.6, iy + 2.6, 1.1, 0, math.tau)
        cr.fill()
        # upper lid shadow
        g = cairo.LinearGradient(0, y - h, 0, y - h + 5)
        g.add_color_stop_rgba(0, 0.2, 0.2, 0.35, 0.35)
        g.add_color_stop_rgba(1, 0.2, 0.2, 0.35, 0)
        cr.set_source(g)
        cr.paint()
        cr.restore()
        # upper lash line (thick, with outer flick)
        cr.set_source_rgb(*INK)
        cr.set_line_width(2.4)
        cr.move_to(x - 12.5, y + 0.5)
        cr.curve_to(x - 6, y - h * 1.2, x + 7, y - h * 1.25, x + 13, y - 1)
        cr.stroke()
        cr.set_line_width(1.6)
        ox = x + side * 12.5
        cr.move_to(ox, y - 1 if side > 0 else y + 0.5)
        cr.line_to(ox + side * 3.2, y - 3.5)
        cr.stroke()
        # lower lid
        cr.set_line_width(0.8)
        cr.set_source_rgba(*INK, 0.5)
        cr.move_to(x - 9, y + h * 0.75)
        cr.curve_to(x - 3, y + h * 0.95, x + 4, y + h * 0.95, x + 9, y + h * 0.6)
        cr.stroke()

    def _brow(self, cr, x: float, y: float, side: int, tilt: float) -> None:
        # positive tilt raises the inner end (worried); negative lowers it (stern)
        inner = x - side * 10
        outer = x + side * 11
        cr.set_source_rgba(0.42, 0.46, 0.62, 0.9)
        cr.set_line_width(1.9)
        cr.move_to(inner, y - tilt * 4)
        cr.curve_to(lerp(inner, outer, 0.35), y - 2.5 - tilt * 2, lerp(inner, outer, 0.7), y - 2.5, outer, y + 1)
        cr.stroke()

    def _mouth(self, cr, x: float, y: float, m: AvatarModel) -> None:
        e = m.expr
        o = m.mouth_open
        smile = e["smile"]
        smirk = e["smirk"]
        if o > 0.08:
            w, hgt = 6.5 + o * 2, 1.5 + o * 6.5
            cr.save()
            cr.translate(x, y + 1)
            cr.scale(w, hgt)
            cr.arc(0, 0, 1, 0, math.tau)
            cr.restore()
            cr.set_source_rgb(0.55, 0.22, 0.30)
            cr.fill_preserve()
            cr.set_source_rgb(*INK)
            cr.set_line_width(1.1)
            cr.stroke()
            return
        w = 7.5 + abs(smile) * 2
        lift_l = -smile * 3.2 + smirk * 1.5
        lift_r = -smile * 3.2 - smirk * 2.5
        mid = smile * 2.4
        cr.set_source_rgb(*INK)
        cr.set_line_width(1.4)
        cr.move_to(x - w, y + lift_l)
        cr.curve_to(x - w / 3, y + mid, x + w / 3, y + mid, x + w, y + lift_r)
        cr.stroke()

    def _front_hair(self, cr, t: float) -> None:
        sway = math.sin(t * 1.1) * 0.8
        grad = self._hair_gradient(120, 230)
        # side locks framing the face
        for side in (-1, 1):
            s = side
            cr.new_path()
            cr.move_to(CX + s * 40, 140)
            cr.curve_to(CX + s * 58, 170, CX + s * 54, 215, CX + s * (48 + sway), 252)
            cr.curve_to(CX + s * 44, 236, CX + s * 40, 210, CX + s * 36, 186)
            cr.curve_to(CX + s * 34, 170, CX + s * 34, 152, CX + s * 40, 140)
            cr.close_path()
            cr.set_source(grad)
            cr.fill_preserve()
            cr.set_source_rgba(*HAIR_DARK, 0.7)
            cr.set_line_width(0.9)
            cr.stroke()
        # crown + bangs: a cap with pointed strands
        tips = [(-45, 162), (-32, 166), (-20, 158), (-7, 170), (5, 160), (17, 166), (31, 158), (45, 155)]
        cr.new_path()
        cr.move_to(CX - 50, 166)
        cr.curve_to(CX - 54, 112, CX + 54, 112, CX + 50, 166)
        prev = (CX + 50, 166)
        for i, (dx, ty) in enumerate(reversed(tips)):
            tx = CX + dx + sway * (1 if i % 2 else -1) * 0.5
            valley_x = (prev[0] + tx) / 2
            cr.curve_to(prev[0] - 2, 150, valley_x, 146, tx, ty)
            prev = (tx, ty)
        cr.curve_to(CX - 48, 150, CX - 50, 158, CX - 50, 166)
        cr.close_path()
        cr.set_source(grad)
        cr.fill_preserve()
        cr.set_source_rgba(*HAIR_DARK, 0.75)
        cr.set_line_width(1)
        cr.stroke()
        # shine streaks
        cr.set_source_rgba(1, 1, 1, 0.55)
        cr.set_line_width(2.2)
        cr.arc(CX - 4, 150, 34, math.radians(200), math.radians(245))
        cr.stroke()
        cr.set_line_width(1.2)
        cr.arc(CX + 6, 152, 30, math.radians(285), math.radians(320))
        cr.stroke()

    def _ornament(self, cr, m: AvatarModel, t: float, col) -> None:
        # crystal hairpin; pulses while thinking/working
        x, y = CX + 38, 136
        pulse = 0.5 + 0.5 * math.sin(t * (6 if m.state in ("THINKING", "WORKING") else 1.5))
        g = cairo.RadialGradient(x, y, 0, x, y, 14)
        g.add_color_stop_rgba(0, *col, 0.35 + 0.35 * pulse)
        g.add_color_stop_rgba(1, *col, 0)
        cr.set_source(g)
        cr.arc(x, y, 14, 0, math.tau)
        cr.fill()
        cr.new_path()
        cr.move_to(x, y - 7)
        cr.line_to(x + 4.5, y)
        cr.line_to(x, y + 7)
        cr.line_to(x - 4.5, y)
        cr.close_path()
        g = cairo.LinearGradient(x - 4, y - 7, x + 4, y + 7)
        g.add_color_stop_rgb(0, 0.9, 1.0, 1.0)
        g.add_color_stop_rgb(1, *CYAN_DEEP)
        cr.set_source(g)
        cr.fill_preserve()
        cr.set_source_rgb(*GOLD)
        cr.set_line_width(1)
        cr.stroke()

    def _effects(self, cr, m: AvatarModel, t: float, col) -> None:
        now = time.monotonic()
        age = now - m.state_since
        if m.state == "LISTENING":
            for i in range(3):
                ph = (t * 1.2 + i / 3) % 1.0
                for side in (-1, 1):
                    cr.set_source_rgba(*col, 0.7 * (1 - ph))
                    cr.set_line_width(2)
                    r = 14 + ph * 26
                    a = 0 if side > 0 else math.pi
                    cr.arc(CX + side * 62, HEAD_Y + 10, r, a - 0.6, a + 0.6)
                    cr.stroke()
        if m.state == "WORKING":
            for i in range(3):
                a = t * 1.6 + i * math.tau / 3
                x, y = CX + 92 * math.cos(a), HEAD_Y + 92 * math.sin(a) * 0.9
                self._hexagon(cr, x, y, 5.5, a * 2, col, 0.85)
        if m.state == "ERROR":
            cr.set_source_rgba(*col, 0.95)
            cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            cr.set_font_size(20)
            cr.move_to(CX + 58, HEAD_Y - 50)
            cr.show_text("!")
        for p in m.particles:
            life = (now - p["born"]) / p["life"]
            if p["kind"] == "mote":
                a = p["a"] + (now - p["born"]) * p["speed"]
                r = p["r0"] + life * 8
                x, y = CX + r * math.cos(a), HEAD_Y + r * math.sin(a) - life * 10
                cr.set_source_rgba(*col, 0.9 * math.sin(math.pi * life))
                cr.arc(x, y, 1.6, 0, math.tau)
                cr.fill()
            elif p["kind"] == "spark":
                r = p["r0"] + life * p["speed"]
                x, y = CX + r * math.cos(p["a"]), HEAD_Y + r * math.sin(p["a"])
                self._star(cr, x, y, 4.5 * (1 - life) + 1, GOLD, 1 - life)
            elif p["kind"] == "z":
                cr.set_source_rgba(0.75, 0.85, 1.0, 0.8 * (1 - life))
                cr.select_font_face("Sans", cairo.FONT_SLANT_ITALIC, cairo.FONT_WEIGHT_BOLD)
                cr.set_font_size(9 + life * 7)
                cr.move_to(p["x"] + life * 18, p["y"] - life * 34)
                cr.show_text("z")

    @staticmethod
    def _hexagon(cr, x, y, r, rot, col, alpha) -> None:
        cr.new_path()
        for i in range(6):
            a = rot + i * math.tau / 6
            (cr.move_to if i == 0 else cr.line_to)(x + r * math.cos(a), y + r * math.sin(a))
        cr.close_path()
        cr.set_source_rgba(*col, alpha)
        cr.set_line_width(1.3)
        cr.stroke()

    @staticmethod
    def _star(cr, x, y, r, col, alpha) -> None:
        cr.new_path()
        for i in range(8):
            rr = r if i % 2 == 0 else r * 0.35
            a = i * math.tau / 8
            (cr.move_to if i == 0 else cr.line_to)(x + rr * math.cos(a), y + rr * math.sin(a))
        cr.close_path()
        cr.set_source_rgba(*col, max(0.0, alpha))
        cr.fill()

    def _disconnected_badge(self, cr, t: float) -> None:
        a = 0.6 + 0.3 * math.sin(t * 2)
        cr.set_source_rgba(0.12, 0.15, 0.25, 0.85)
        _rounded(cr, CX - 34, H - 22, 68, 16, 8)
        cr.fill()
        cr.set_source_rgba(1.0, 0.6, 0.45, a)
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(9)
        cr.move_to(CX - 26, H - 11)
        cr.show_text("brain offline")

    def _bubble(self, cr, b: Bubble, now: float) -> None:
        age = now - b.born
        alpha = ease(age / 0.25) * (1 - ease((age - (b.duration - 0.5)) / 0.5))
        if alpha <= 0.01:
            return
        lines = _wrap(cr, b.text, 196, 11)
        lh = 14
        h = 14 + lh * len(lines)
        h = min(h, 76)
        y = max(2, 80 - h - 6) + (1 - ease(age / 0.25)) * 6
        x, w = 14, 212
        cr.save()
        border = {"warn": (1.0, 0.6, 0.45), "info": GOLD}.get(b.kind, CYAN)
        _rounded(cr, x, y, w, h, 10)
        cr.set_source_rgba(0.05, 0.08, 0.16, 0.92 * alpha)
        cr.fill_preserve()
        cr.set_source_rgba(*border, 0.8 * alpha)
        cr.set_line_width(1.2)
        cr.stroke()
        # tail toward her head
        cr.move_to(CX + 10, y + h)
        cr.line_to(CX + 22, y + h + 8)
        cr.line_to(CX + 26, y + h)
        cr.close_path()
        cr.set_source_rgba(0.05, 0.08, 0.16, 0.92 * alpha)
        cr.fill()
        cr.set_source_rgba(0.88, 0.93, 1.0, alpha)
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(11)
        for i, line in enumerate(lines[:5]):
            cr.move_to(x + 10, y + 17 + i * lh)
            cr.show_text(line)
        cr.restore()


def _rounded(cr, x, y, w, h, r) -> None:
    cr.new_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
    cr.close_path()


def _wrap(cr, text: str, width: float, size: float) -> list[str]:
    cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
    cr.set_font_size(size)
    lines, cur = [], ""
    for word in text.split():
        trial = (cur + " " + word).strip()
        if cr.text_extents(trial).x_advance <= width:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    if len(lines) > 5:
        lines = lines[:5]
        lines[-1] = lines[-1].rstrip(".") + "…"
    return lines


def hit_regions() -> list[tuple[int, int, int, int]]:
    """Rectangles (logical px) covering her silhouette — used as the input region, so clicks on
    transparent pixels fall through to the windows underneath."""
    return [
        (CX - 60, 112, 120, 30),   # crown
        (CX - 66, 142, 132, 90),   # head + side locks
        (CX - 40, 232, 80, 14),    # neck/collar
        (CX - 92, 246, 184, H - 246),  # shoulders
    ]


def render_png(path: str, state: str = "IDLE", mood: str = "calm", scale: float = 2.0,
               bubble: str | None = None, warm_frames: int = 30) -> None:
    """Headless render for tests and docs."""
    m = AvatarModel()
    m.set_state(state, mood)
    m.next_blink = time.monotonic() + 999
    if bubble:
        m.say(bubble)
    if state == "SPEAKING":
        m.start_speech([0.8] * 50, 40, 2.0)
    r = Renderer()
    for _ in range(warm_frames):
        r.update(m, 1 / 30)
    if bubble:
        m.bubble.born = time.monotonic() - 1.0
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, int(W * scale), int(H * scale))
    cr = cairo.Context(surf)
    r.draw(cr, m, scale)
    surf.write_to_png(path)
