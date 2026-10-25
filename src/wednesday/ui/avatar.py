"""The always-on-top avatar: a wlr-layer-shell surface on Hyprland.

Why layer-shell instead of a normal window + Hyprland rules:
  * OVERLAY layer = above normal and fullscreen windows without touching your hyprland.conf;
  * keyboard interactivity NONE = she can never steal focus;
  * the input region only covers her silhouette, so clicks on transparent pixels fall through
    to the window underneath (ghost mode makes the whole surface click-through).
Falls back to an ordinary undecorated keep-above window if layer-shell is unavailable (X11).

Interaction: left-click → chat panel · drag → move · Ctrl+scroll → resize · right-click → menu.
"""

from __future__ import annotations

import json
import logging
import time

import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

try:
    gi.require_version("GtkLayerShell", "0.1")
    from gi.repository import GtkLayerShell  # noqa: E402
except (ValueError, ImportError):
    GtkLayerShell = None

from wednesday.config import Config  # noqa: E402
from wednesday.ui.renderer import H, W, AvatarModel, Renderer, hit_regions  # noqa: E402

log = logging.getLogger(__name__)


def layer_shell_ok() -> bool:
    return GtkLayerShell is not None and GtkLayerShell.is_supported()


class AvatarWindow(Gtk.Window):
    def __init__(self, cfg: Config, on_click, on_menu_action):
        super().__init__(title="Wednesday")
        self.cfg = cfg
        self.on_click = on_click
        self.on_menu_action = on_menu_action
        self.model = AvatarModel()
        self.renderer = Renderer()
        self.ui_file = cfg.paths.state_dir / "ui.json"
        saved = self._load_ui_state()
        self.scale = float(saved.get("scale", cfg.get("avatar.scale", 1.0)))
        self.margin_x = int(saved.get("margin_x", cfg.get("avatar.margin_x", 24)))
        self.margin_y = int(saved.get("margin_y", cfg.get("avatar.margin_y", 24)))
        self.layered = layer_shell_ok()
        self._drag: tuple[float, float, int, int] | None = None
        self._dragged = False
        self._last_frame = time.monotonic()
        self._frames: list[float] = []
        self._timer: int | None = None
        self._fps = 0

        self.set_app_paintable(True)
        visual = self.get_screen().get_rgba_visual()
        if visual is not None:
            self.set_visual(visual)
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_accept_focus(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)

        if self.layered:
            GtkLayerShell.init_for_window(self)
            GtkLayerShell.set_namespace(self, "wednesday-avatar")
            GtkLayerShell.set_layer(self, GtkLayerShell.Layer.OVERLAY)
            GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.BOTTOM, True)
            GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.RIGHT, True)
            GtkLayerShell.set_keyboard_mode(self, GtkLayerShell.KeyboardMode.NONE)
            GtkLayerShell.set_exclusive_zone(self, 0)
            self._apply_margins()
        else:
            log.warning("layer-shell unavailable — using a normal keep-above window")
            self.set_keep_above(True)
            self.set_type_hint(Gdk.WindowTypeHint.UTILITY)

        self.area = Gtk.DrawingArea()
        self.area.connect("draw", self._on_draw)
        self.add(self.area)
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK |
                        Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.SCROLL_MASK |
                        Gdk.EventMask.SMOOTH_SCROLL_MASK | Gdk.EventMask.ENTER_NOTIFY_MASK |
                        Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self.connect("button-press-event", self._on_press)
        self.connect("button-release-event", self._on_release)
        self.connect("motion-notify-event", self._on_motion)
        self.connect("scroll-event", self._on_scroll)
        self.connect("enter-notify-event", lambda *_: self._hover(True))
        self.connect("leave-notify-event", lambda *_: self._hover(False))
        self.connect("realize", lambda *_: self._update_input_region())
        self._resize()
        self._schedule()

    # ------------------------------------------------------------------ persistence
    def _load_ui_state(self) -> dict:
        try:
            return json.loads(self.ui_file.read_text())
        except (OSError, ValueError):
            return {}

    def _save_ui_state(self) -> None:
        try:
            self.ui_file.write_text(json.dumps({"scale": round(self.scale, 2), "margin_x": self.margin_x,
                                                "margin_y": self.margin_y}))
        except OSError:
            pass

    # ------------------------------------------------------------------ geometry
    @property
    def size(self) -> tuple[int, int]:
        return int(W * self.scale), int(H * self.scale)

    def _resize(self) -> None:
        w, h = self.size
        self.area.set_size_request(w, h)
        self.set_size_request(w, h)
        self.resize(w, h)
        self._update_input_region()

    def _apply_margins(self) -> None:
        if self.layered:
            GtkLayerShell.set_margin(self, GtkLayerShell.Edge.RIGHT, self.margin_x)
            GtkLayerShell.set_margin(self, GtkLayerShell.Edge.BOTTOM, self.margin_y)

    def _monitor_size(self) -> tuple[int, int]:
        display = Gdk.Display.get_default()
        win = self.get_window()
        mon = display.get_monitor_at_window(win) if win else display.get_primary_monitor() or display.get_monitor(0)
        geo = mon.get_geometry()
        return geo.width, geo.height

    def _update_input_region(self) -> None:
        """Only her silhouette (and a visible speech bubble) receives clicks."""
        if not self.get_realized():
            return
        if self.model.ghost:
            region = cairo.Region()  # empty → everything passes through
        else:
            s = self.scale
            rects = [cairo.RectangleInt(int(x * s), int(y * s), int(w * s), int(h * s)) for x, y, w, h in hit_regions()]
            if self.model.bubble:
                rects.append(cairo.RectangleInt(int(14 * s), 0, int(212 * s), int(82 * s)))
            region = cairo.Region(rects)
        self.input_shape_combine_region(region)

    def set_ghost(self, ghost: bool) -> None:
        self.model.ghost = ghost
        self._update_input_region()
        self.area.queue_draw()

    # ------------------------------------------------------------------ animation loop
    def _target_fps(self) -> int:
        if not self.get_visible():
            return 0
        if self.model.state == "SLEEPING" and not self.model.animating:
            return int(self.cfg.get("avatar.fps_sleep", 2))
        if self.model.animating:
            return int(self.cfg.get("avatar.fps_active", 30))
        return int(self.cfg.get("avatar.fps_idle", 12))

    def _schedule(self) -> None:
        """Adaptive frame rate: 30 fps while animating, 12 idle, 2 asleep, 0 hidden."""
        fps = self._target_fps()
        if fps == self._fps and self._timer is not None:
            return
        if self._timer is not None:
            GLib.source_remove(self._timer)
            self._timer = None
        self._fps = fps
        if fps > 0:
            self._timer = GLib.timeout_add(int(1000 / fps), self._tick)

    def _tick(self) -> bool:
        now = time.monotonic()
        dt = min(0.25, now - self._last_frame)
        self._last_frame = now
        had_bubble = self.model.bubble is not None
        self.renderer.update(self.model, dt)
        if had_bubble != (self.model.bubble is not None):
            self._update_input_region()
        self.area.queue_draw()
        self._frames.append(now)
        self._frames = [f for f in self._frames if now - f < 5]
        if self._target_fps() != self._fps:
            self._timer = None
            self._fps = -1
            self._schedule()
            return False
        return True

    def kick(self) -> None:
        """Call after any model change so the frame rate adapts immediately."""
        self._schedule()
        self._update_input_region()
        self.area.queue_draw()

    @property
    def measured_fps(self) -> float:
        return len(self._frames) / 5.0

    def _on_draw(self, _w, cr) -> bool:
        self.renderer.draw(cr, self.model, self.scale)
        return True

    # ------------------------------------------------------------------ input
    def _hover(self, inside: bool) -> None:
        log.info("pointer %s avatar", "entered" if inside else "left")
        self.model.hover = inside
        self.kick()

    def _on_press(self, _w, ev) -> bool:
        if ev.button == 1:
            self._drag = (ev.x, ev.y, self.margin_x, self.margin_y)
            self._dragged = False
        elif ev.button == 3:
            self._menu(ev)
        return True

    def _on_motion(self, _w, ev) -> bool:
        if not self._drag or not self.layered:
            return False
        px, py, _, _ = self._drag
        dx, dy = ev.x - px, ev.y - py
        if not self._dragged and abs(dx) + abs(dy) < 6:
            return True
        self._dragged = True
        # Anchored bottom-right: moving right/down means shrinking the margins. The surface follows
        # the pointer, so the pointer's surface-local position stays ~constant while dragging.
        mw, mh = self._monitor_size()
        w, h = self.size
        self.margin_x = int(max(0, min(mw - w, self.margin_x - dx)))
        self.margin_y = int(max(0, min(mh - h, self.margin_y - dy)))
        self._apply_margins()
        return True

    def _on_release(self, _w, ev) -> bool:
        if ev.button == 1 and self._drag:
            if self._dragged:
                self._save_ui_state()
                self.on_menu_action("moved")
            else:
                self.on_click()
            self._drag = None
        return True

    def _on_scroll(self, _w, ev) -> bool:
        if not ev.state & Gdk.ModifierType.CONTROL_MASK:
            return False
        direction = ev.direction
        delta = 0.0
        if direction == Gdk.ScrollDirection.UP:
            delta = 0.08
        elif direction == Gdk.ScrollDirection.DOWN:
            delta = -0.08
        elif direction == Gdk.ScrollDirection.SMOOTH:
            delta = -ev.delta_y * 0.05
        self.scale = max(0.5, min(1.8, self.scale + delta))
        self._resize()
        self._save_ui_state()
        self.on_menu_action("moved")
        return True

    def _menu(self, ev) -> None:
        menu = Gtk.Menu()
        items = [
            ("Open chat", "panel"),
            ("Push to talk", "talk"),
            (None, None),
            ("Speak replies aloud", "speak"),
            ("Ghost mode (click-through)", "ghost"),
            ("Free the GPU (unload model)", "unload"),
            ("Emergency stop", "halt"),
            (None, None),
            ("Hide avatar", "hide"),
            ("Reset position & size", "reset"),
            ("Quit Wednesday", "quit"),
        ]
        for label, action in items:
            if label is None:
                menu.append(Gtk.SeparatorMenuItem())
                continue
            it = Gtk.MenuItem(label=label)
            it.connect("activate", lambda _i, a=action: self.on_menu_action(a))
            menu.append(it)
        menu.show_all()
        menu.popup_at_pointer(ev)
        self._keep_menu = menu  # keep a reference while it's open

    def reset_geometry(self) -> None:
        self.scale = float(self.cfg.get("avatar.scale", 1.0))
        self.margin_x = int(self.cfg.get("avatar.margin_x", 24))
        self.margin_y = int(self.cfg.get("avatar.margin_y", 24))
        self._apply_margins()
        self._resize()
        self._save_ui_state()
