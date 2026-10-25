"""Compact control panel: chat, approvals, tasks, memory, system status.

A layer-shell surface placed beside the avatar. Keyboard mode ON_DEMAND: it takes keyboard
focus only when you click into it, and Esc closes it.
"""

from __future__ import annotations

import html
import re
import subprocess
import time

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

from wednesday.ui.avatar import GtkLayerShell, layer_shell_ok  # noqa: E402
from wednesday.ui.bridge import Bridge  # noqa: E402

PANEL_W, PANEL_H = 420, 600

CSS = b"""
* { font-family: "Inter", "Noto Sans", "Cantarell", sans-serif; }
.panel { background-color: rgb(9, 13, 26); border: 1px solid rgba(94, 231, 255, 0.28);
         border-radius: 16px; }
.header { padding: 12px 14px 6px 14px; }
.title { font-size: 17px; font-weight: 700; color: #e8f4ff; letter-spacing: 0.5px; }
.subtitle { font-size: 11px; color: #8fa3c0; }
.chip { font-size: 10px; padding: 2px 8px; border-radius: 9px; background-color: rgba(94,231,255,0.10);
        color: #9fefff; border: 1px solid rgba(94,231,255,0.25); }
.chip.offline { background-color: rgba(255,170,90,0.10); color: #ffc58a; border-color: rgba(255,170,90,0.35); }
.chip.gold { background-color: rgba(214,178,106,0.12); color: #e7cf98; border-color: rgba(214,178,106,0.35); }
.chip.bad { background-color: rgba(255,110,90,0.12); color: #ff9a8a; border-color: rgba(255,110,90,0.4); }
button.chipbtn { padding: 1px 8px; min-height: 0; border-radius: 9px; font-size: 10px; background: rgba(94,231,255,0.08);
        color: #bfefff; border: 1px solid rgba(94,231,255,0.25); box-shadow: none; }
button.chipbtn:checked { background: rgba(94,231,255,0.25); color: #ffffff; }
.meters { font-size: 10px; color: #7f93b3; padding: 0 14px 6px 14px; }
.meters.warn { color: #ffb07a; }
stackswitcher button { padding: 3px 10px; min-height: 0; font-size: 11px; background: transparent; color: #8fa3c0;
        border: none; border-bottom: 2px solid transparent; border-radius: 0; box-shadow: none; }
stackswitcher button:checked { color: #e8f4ff; border-bottom-color: #5ee7ff; }
scrolledwindow, viewport, list, row { background: transparent; }
combobox button { padding: 1px 8px; min-height: 0; border-radius: 9px; font-size: 10px; background: rgba(94,231,255,0.08);
        color: #bfefff; border: 1px solid rgba(94,231,255,0.25); box-shadow: none; }
combobox arrow { min-width: 10px; min-height: 10px; color: #9fefff; }
menu, .menu { background-color: #0d1426; color: #dbe7f7; }
menuitem:hover { background-color: rgba(94,231,255,0.18); }
row { padding: 0; }
row:hover, row:selected { background: transparent; }
.msg { padding: 8px 11px; border-radius: 12px; margin: 3px 10px; font-size: 12.5px; }
.msg.user { background-color: rgba(214,178,106,0.14); color: #f4ecd9; border: 1px solid rgba(214,178,106,0.25); }
.msg.bot { background-color: rgba(94,231,255,0.06); color: #dbe7f7; border: 1px solid rgba(94,231,255,0.15); }
.msg.err { background-color: rgba(255,110,90,0.10); color: #ffc0b5; border: 1px solid rgba(255,110,90,0.35); }
.who { font-size: 10px; font-weight: 700; color: #5ee7ff; }
.who.user { color: #d6b26a; }
.meta { font-size: 9.5px; color: #6c7f9e; }
.thought { font-size: 11px; color: #8696b4; font-style: italic; }
.tool { font-size: 11px; color: #c9b27c; margin: 1px 18px; }
.tool.bad { color: #ff9a8a; }
.approval { background-color: rgba(214,178,106,0.10); border: 1px solid rgba(214,178,106,0.55);
            border-radius: 12px; padding: 10px; margin: 6px 10px; }
.approval.danger { border-color: rgba(255,110,90,0.75); background-color: rgba(255,110,90,0.08); }
.approval-title { font-weight: 700; color: #f1dcaa; font-size: 12px; }
.approval-detail { font-family: monospace; font-size: 10.5px; color: #cfd8e8; background: rgba(0,0,0,0.3);
                   padding: 6px; border-radius: 6px; }
.danger-text { color: #ff9a8a; font-size: 11px; font-weight: 700; }
button.approve { background: rgba(94,231,255,0.18); color: #e8fbff; border: 1px solid rgba(94,231,255,0.5);
                 border-radius: 8px; box-shadow: none; }
button.deny { background: rgba(255,255,255,0.04); color: #c6cfdf; border: 1px solid rgba(255,255,255,0.15);
              border-radius: 8px; box-shadow: none; }
.inputbar { padding: 8px 10px 10px 10px; }
entry { background-color: rgba(255,255,255,0.05); color: #eef6ff; border: 1px solid rgba(94,231,255,0.25);
        border-radius: 10px; padding: 7px 10px; caret-color: #5ee7ff; box-shadow: none; }
entry:focus { border-color: rgba(94,231,255,0.7); }
button.round { border-radius: 10px; min-width: 34px; min-height: 30px; padding: 0 6px; background: rgba(94,231,255,0.12);
               color: #dff9ff; border: 1px solid rgba(94,231,255,0.35); box-shadow: none; }
button.round.recording { background: rgba(255,90,90,0.35); border-color: rgba(255,120,120,0.8); }
button.round.stop { background: rgba(255,140,100,0.25); border-color: rgba(255,160,120,0.7); }
button.flat { background: transparent; border: none; color: #8fa3c0; box-shadow: none; padding: 2px 6px; min-height: 0; }
button.flat:hover { color: #ffffff; }
.section { font-size: 11px; font-weight: 700; color: #8fa3c0; margin: 8px 12px 2px 12px; }
.item { font-size: 11.5px; color: #d3deef; margin: 2px 12px; }
.mono { font-family: monospace; font-size: 10.5px; color: #b9c7de; }
.empty { color: #6c7f9e; font-size: 11.5px; margin: 20px; }
"""


def md_to_pango(text: str) -> str:
    """Tiny markdown → Pango markup: code blocks, inline code, bold, italics, links, headings."""
    out = []
    parts = re.split(r"```[^\n]*\n?(.*?)```", text, flags=re.S)
    for i, part in enumerate(parts):
        if i % 2 == 1:
            out.append('<span font_family="monospace" size="small" background="#050a16" foreground="#bfe9ff">'
                       + html.escape(part.rstrip("\n")) + "</span>")
            continue
        if "```" in part:  # unterminated block while streaming
            pre, code = part.split("```", 1)
            out.append(_inline(pre))
            code = code.split("\n", 1)[1] if "\n" in code else ""
            out.append('<span font_family="monospace" size="small" background="#050a16" foreground="#bfe9ff">'
                       + html.escape(code) + "</span>")
            continue
        out.append(_inline(part))
    return "".join(out)


def _inline(t: str) -> str:
    t = html.escape(t)
    t = re.sub(r"`([^`\n]+)`", r'<span font_family="monospace" background="#0b1630" foreground="#9fefff">\1</span>', t)
    t = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"(?<![*\w])\*([^*\n]+)\*(?!\w)", r"<i>\1</i>", t)
    t = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', t)
    t = re.sub(r"^#{1,4}\s*(.+)$", r"<b>\1</b>", t, flags=re.M)
    t = re.sub(r"^(\s*)[-*] ", r"\1• ", t, flags=re.M)
    return t


def _label(text: str = "", css: str | None = None, markup: bool = False, wrap: bool = True,
           selectable: bool = False, xalign: float = 0.0) -> Gtk.Label:
    lbl = Gtk.Label()
    if markup:
        lbl.set_markup(text)
    else:
        lbl.set_text(text)
    lbl.set_xalign(xalign)
    if wrap:
        lbl.set_line_wrap(True)
        lbl.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        lbl.set_max_width_chars(48)
    lbl.set_selectable(selectable)
    if css:
        for c in css.split():
            lbl.get_style_context().add_class(c)
    return lbl


def _open_link(_label, uri) -> bool:
    # A click on a link is your own action, so opening it directly is fine.
    subprocess.Popen(["xdg-open", uri], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return True


class StreamingMessage:
    """One assistant reply being streamed: label updates are batched to ≤ 25 per second."""

    def __init__(self, panel: "Panel"):
        self.panel = panel
        self.text = ""
        self.thoughts = ""
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        self.box.get_style_context().add_class("msg")
        self.box.get_style_context().add_class("bot")
        head = Gtk.Box(spacing=6)
        head.pack_start(_label("WEDNESDAY", "who", wrap=False), False, False, 0)
        self.meta = _label("", "meta", wrap=False)
        head.pack_end(self.meta, False, False, 0)
        self.box.pack_start(head, False, False, 0)
        self.thought_exp = Gtk.Expander(label="thinking…")
        self.thought_exp.get_style_context().add_class("thought")
        self.thought_lbl = _label("", "thought", selectable=True)
        self.thought_exp.add(self.thought_lbl)
        self.thought_exp.set_no_show_all(True)
        self.box.pack_start(self.thought_exp, False, False, 0)
        self.body = _label("…", markup=False, selectable=True)
        self.body.connect("activate-link", _open_link)
        self.box.pack_start(self.body, False, False, 0)
        self._dirty = False
        self._timer = None

    def add_text(self, t: str) -> None:
        self.text += t
        self._mark()

    def add_thought(self, t: str) -> None:
        self.thoughts += t
        if not self.thought_exp.get_visible():
            self.thought_exp.set_no_show_all(False)
            self.thought_exp.show_all()
        self._mark()

    def retract(self) -> None:
        self.text = ""
        self._mark()

    def _mark(self) -> None:
        self._dirty = True
        if self._timer is None:
            self._timer = GLib.timeout_add(40, self._flush)

    def _flush(self) -> bool:
        self._timer = None
        if self._dirty:
            self._dirty = False
            self.body.set_markup(md_to_pango(self.text) if self.text else "…")
            if self.thoughts:
                tail = self.thoughts[-1500:]
                self.thought_lbl.set_text(("…" if len(self.thoughts) > 1500 else "") + tail)
            self.panel.scroll_to_bottom()
        return False

    def finish(self, meta: str) -> None:
        self._flush()
        if self.thoughts:
            self.thought_exp.set_label(f"thought for a moment ({len(self.thoughts)} chars)")
        self.meta.set_text(meta)


class Panel(Gtk.Window):
    def __init__(self, bridge: Bridge, avatar):
        super().__init__(title="Wednesday — chat")
        self.bridge = bridge
        self.avatar = avatar
        self.current: StreamingMessage | None = None
        self.current_job: str | None = None
        self.busy = False
        self.recording = False
        self.settings: dict = {}
        self.layered = layer_shell_ok()
        self._approval_widgets: dict[str, Gtk.Widget] = {}

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider,
                                                 Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.set_app_paintable(True)
        visual = self.get_screen().get_rgba_visual()
        if visual is not None:
            self.set_visual(visual)
        self.set_decorated(False)
        self.set_default_size(PANEL_W, PANEL_H)
        self.set_size_request(PANEL_W, PANEL_H)
        self.connect("draw", self._clear_bg)
        if self.layered:
            GtkLayerShell.init_for_window(self)
            GtkLayerShell.set_namespace(self, "wednesday-panel")
            GtkLayerShell.set_layer(self, GtkLayerShell.Layer.OVERLAY)
            GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.BOTTOM, True)
            GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.RIGHT, True)
            GtkLayerShell.set_keyboard_mode(self, GtkLayerShell.KeyboardMode.ON_DEMAND)
        else:
            self.set_keep_above(True)
        self.connect("key-press-event", self._on_key)
        self._build()

    # ------------------------------------------------------------------ layout
    def _clear_bg(self, _w, cr) -> bool:
        # Clear to transparent so the rounded .panel corners show the desktop. save/restore is
        # essential: leaving CLEAR set would make every child widget draw as an eraser.
        cr.save()
        cr.set_source_rgba(0, 0, 0, 0)
        cr.set_operator(0)  # CAIRO_OPERATOR_CLEAR
        cr.paint()
        cr.restore()
        return False

    def place(self) -> None:
        """Sit to the left of the avatar, bottoms aligned; flip above her if there's no room."""
        if not self.layered:
            return
        aw, ah = self.avatar.size
        mx, my = self.avatar.margin_x, self.avatar.margin_y
        try:
            mon_w, mon_h = self.avatar._monitor_size()
        except Exception:
            mon_w, mon_h = 1920, 1080
        right = mx + aw + 8
        bottom = max(8, my)
        if right + PANEL_W > mon_w:  # not enough room on the left → stack above her
            right = max(8, mx)
            bottom = my + ah + 8
        bottom = min(bottom, max(8, mon_h - PANEL_H - 8))
        GtkLayerShell.set_margin(self, GtkLayerShell.Edge.RIGHT, right)
        GtkLayerShell.set_margin(self, GtkLayerShell.Edge.BOTTOM, bottom)

    def _build(self) -> None:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.get_style_context().add_class("panel")
        self.add(outer)

        # header
        header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        header.get_style_context().add_class("header")
        row = Gtk.Box(spacing=8)
        row.pack_start(_label("Wednesday", "title", wrap=False), False, False, 0)
        self.mood_lbl = _label("calm", "subtitle", wrap=False)
        row.pack_start(self.mood_lbl, False, False, 0)
        close = Gtk.Button(label="✕")
        close.get_style_context().add_class("flat")
        close.set_tooltip_text("Close (Esc)")
        close.connect("clicked", lambda *_: self.hide())
        row.pack_end(close, False, False, 0)
        header.pack_start(row, False, False, 0)

        chips = Gtk.Box(spacing=5)
        self.model_chip = _label("model …", "chip", wrap=False)
        chips.pack_start(self.model_chip, False, False, 0)
        self.online_btn = Gtk.ToggleButton(label="ONLINE")
        self.online_btn.get_style_context().add_class("chipbtn")
        self.online_btn.set_tooltip_text("Web tools on/off. Your model always runs locally.")
        self._online_handler = self.online_btn.connect("toggled", self._toggle_online)
        chips.pack_start(self.online_btn, False, False, 0)
        self.speak_btn = Gtk.ToggleButton(label="🔇 voice")
        self.speak_btn.get_style_context().add_class("chipbtn")
        self.speak_btn.set_tooltip_text("Read replies aloud (local Piper TTS)")
        self._speak_handler = self.speak_btn.connect("toggled", self._toggle_speak)
        chips.pack_start(self.speak_btn, False, False, 0)
        self.think_combo = Gtk.ComboBoxText()
        for key, label in (("auto", "think: auto"), ("fast", "think: fast"), ("deep", "think: deep")):
            self.think_combo.append(key, label)
        self.think_combo.set_active_id("auto")
        self.think_combo.set_tooltip_text("auto: reason only when needed · fast: always instant · deep: always reason")
        self._think_handler = self.think_combo.connect("changed", self._think_changed)
        chips.pack_end(self.think_combo, False, False, 0)
        header.pack_start(chips, False, False, 0)
        outer.pack_start(header, False, False, 0)

        self.meters = _label("CPU –  ·  RAM –  ·  VRAM –  ·  GPU –", "meters", wrap=False)
        outer.pack_start(self.meters, False, False, 0)

        # tabs
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_transition_duration(120)
        switcher = Gtk.StackSwitcher()
        switcher.set_stack(self.stack)
        switcher.set_halign(Gtk.Align.CENTER)
        outer.pack_start(switcher, False, False, 2)

        # chat tab
        self.chat_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.chat_scroll = Gtk.ScrolledWindow()
        self.chat_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.chat_scroll.add(self.chat_list)
        self.chat_list.pack_start(_label("Ask me anything. I run on your GPU, not someone's cloud.\n"
                                         "Tip: I'll ask before touching files or running anything that matters.",
                                         "empty"), False, False, 0)
        self.stack.add_titled(self.chat_scroll, "chat", "Chat")

        # tasks tab
        self.tasks_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sw.add(self.tasks_box)
        self.stack.add_titled(sw, "tasks", "Tasks")

        # memory tab
        mem = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        bar = Gtk.Box(spacing=6)
        bar.set_margin_start(10)
        bar.set_margin_end(10)
        bar.set_margin_top(6)
        self.mem_search = Gtk.Entry(placeholder_text="search memory…")
        self.mem_search.connect("activate", lambda *_: self.refresh_memory())
        bar.pack_start(self.mem_search, True, True, 0)
        add = Gtk.Button(label="＋ note")
        add.get_style_context().add_class("round")
        add.set_tooltip_text("Save the search text as a note")
        add.connect("clicked", self._add_note)
        bar.pack_start(add, False, False, 0)
        mem.pack_start(bar, False, False, 0)
        self.mem_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sw.add(self.mem_box)
        mem.pack_start(sw, True, True, 0)
        self.stack.add_titled(mem, "memory", "Memory")

        # system tab
        sysbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        btns = Gtk.Box(spacing=6)
        btns.set_margin_start(10)
        btns.set_margin_top(8)
        for label, action in (("Load model", "load"), ("Free GPU", "unload"), ("Emergency stop", "halt"),
                              ("New chat", "reset")):
            b = Gtk.Button(label=label)
            b.get_style_context().add_class("deny" if action != "halt" else "approve")
            b.connect("clicked", lambda _b, a=action: self._system_action(a))
            btns.pack_start(b, False, False, 0)
        sysbox.pack_start(btns, False, False, 0)
        self.sys_lbl = _label("", "mono", selectable=True)
        self.sys_lbl.set_margin_start(12)
        self.sys_lbl.set_margin_end(12)
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sw.add(self.sys_lbl)
        sysbox.pack_start(sw, True, True, 0)
        self.stack.add_titled(sysbox, "system", "System")
        self.stack.connect("notify::visible-child-name", self._tab_changed)
        outer.pack_start(self.stack, True, True, 0)

        # input bar
        bar = Gtk.Box(spacing=6)
        bar.get_style_context().add_class("inputbar")
        self.entry = Gtk.Entry(placeholder_text="Ask Wednesday…")
        self.entry.connect("activate", lambda *_: self._send())
        bar.pack_start(self.entry, True, True, 0)
        self.mic = Gtk.Button(label="🎙")
        self.mic.get_style_context().add_class("round")
        self.mic.set_tooltip_text("Push to talk: click to start, click again to send")
        self.mic.connect("clicked", self._mic)
        bar.pack_start(self.mic, False, False, 0)
        self.send = Gtk.Button(label="➤")
        self.send.get_style_context().add_class("round")
        self.send.connect("clicked", lambda *_: self._stop() if self.busy else self._send())
        bar.pack_start(self.send, False, False, 0)
        outer.pack_start(bar, False, False, 0)
        self.footer = _label("", "meta", wrap=False, xalign=0.5)
        self.footer.set_margin_bottom(6)
        outer.pack_start(self.footer, False, False, 0)

    # ------------------------------------------------------------------ show / hide
    def toggle(self) -> None:
        if self.get_visible():
            self.hide()
        else:
            self.open()

    def open(self) -> None:
        self.place()
        self.show_all()
        self.present()
        if self.layered:
            # Grab the keyboard just long enough for the entry to receive focus, then go back to
            # on-demand so the panel never holds your keyboard hostage.
            GtkLayerShell.set_keyboard_mode(self, GtkLayerShell.KeyboardMode.EXCLUSIVE)
            GLib.timeout_add(350, self._relax_keyboard)
        self.entry.grab_focus()
        if not self.chat_list.get_children()[1:]:
            self.bridge.get("/api/history", done=self._load_history)

    def _relax_keyboard(self) -> bool:
        if self.layered:
            GtkLayerShell.set_keyboard_mode(self, GtkLayerShell.KeyboardMode.ON_DEMAND)
        return False

    def _on_key(self, _w, ev) -> bool:
        if ev.keyval == Gdk.KEY_Escape:
            if self.busy:
                self._stop()
            else:
                self.hide()
            return True
        return False

    def scroll_to_bottom(self) -> None:
        def go():
            adj = self.chat_scroll.get_vadjustment()
            adj.set_value(adj.get_upper() - adj.get_page_size())
            return False
        GLib.idle_add(go)

    # ------------------------------------------------------------------ chat
    def _append(self, widget: Gtk.Widget) -> None:
        kids = self.chat_list.get_children()
        if len(kids) == 1 and "empty" in kids[0].get_style_context().list_classes():
            self.chat_list.remove(kids[0])
        self.chat_list.pack_start(widget, False, False, 0)
        widget.show_all()
        if len(self.chat_list.get_children()) > 200:  # bound UI memory on long sessions
            self.chat_list.remove(self.chat_list.get_children()[0])
        self.scroll_to_bottom()

    def _user_bubble(self, text: str, via: str = "") -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.get_style_context().add_class("msg")
        box.get_style_context().add_class("user")
        box.pack_start(_label("YOU" + (f" · {via}" if via else ""), "who user", wrap=False), False, False, 0)
        box.pack_start(_label(text, selectable=True), False, False, 0)
        self._append(box)

    def _note(self, text: str, css: str = "tool") -> None:
        self._append(_label(text, css))

    def _load_history(self, res: dict) -> None:
        for m in res.get("messages", [])[-20:]:
            if m["role"] == "user":
                self._user_bubble(m["content"])
            else:
                sm = StreamingMessage(self)
                self._append(sm.box)
                sm.add_text(m["content"])
                sm.finish("earlier")

    def _send(self) -> None:
        text = self.entry.get_text().strip()
        if not text:
            return
        self.entry.set_text("")
        self._user_bubble(text)
        self.bridge.post("/api/chat", {"text": text, "session": "desktop"},
                         error=lambda msg: self._error_bubble(msg))

    def _stop(self) -> None:
        self.bridge.post("/api/cancel", {})

    def _mic(self, *_):
        self.bridge.post("/api/voice/ptt", {"action": "toggle"}, timeout=120,
                         error=lambda msg: self._error_bubble(msg))

    def _error_bubble(self, msg: str, quip: str | None = None) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.get_style_context().add_class("msg")
        box.get_style_context().add_class("err")
        box.pack_start(_label(msg, selectable=True), False, False, 0)
        if quip:
            box.pack_start(_label(quip, "meta"), False, False, 0)
        self._append(box)

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        ctx = self.send.get_style_context()
        if busy:
            self.send.set_label("■")
            ctx.add_class("stop")
            self.send.set_tooltip_text("Stop (Esc)")
        else:
            self.send.set_label("➤")
            ctx.remove_class("stop")
            self.send.set_tooltip_text("Send")

    # ------------------------------------------------------------------ approvals
    def _approval_card(self, ev: dict) -> None:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        card.get_style_context().add_class("approval")
        if ev.get("destructive"):
            card.get_style_context().add_class("danger")
        card.pack_start(_label("Wednesday wants to " + ev.get("summary", "do something"), "approval-title"),
                        False, False, 0)
        if ev.get("reason"):
            card.pack_start(_label("why it needs approval: " + ev["reason"], "meta"), False, False, 0)
        if ev.get("destructive"):
            card.pack_start(_label("⚠ This can modify or delete data.", "danger-text"), False, False, 0)
        details = ev.get("details") or ""
        if details and details != ev.get("summary"):
            lbl = _label(details[:3000], "approval-detail", selectable=True)
            sw = Gtk.ScrolledWindow()
            sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            sw.set_max_content_height(150)
            sw.set_propagate_natural_height(True)
            sw.add(lbl)
            card.pack_start(sw, False, False, 0)
        btns = Gtk.Box(spacing=8)
        yes = Gtk.Button(label="Approve")
        yes.get_style_context().add_class("approve")
        no = Gtk.Button(label="Deny")
        no.get_style_context().add_class("deny")
        status = _label("", "meta", wrap=False)
        for b, ok in ((yes, True), (no, False)):
            b.connect("clicked", lambda _b, ok=ok: self._answer(ev["id"], ok, yes, no, status))
        btns.pack_start(yes, False, False, 0)
        btns.pack_start(no, False, False, 0)
        btns.pack_end(status, False, False, 0)
        card.pack_start(btns, False, False, 0)
        if ev.get("quip"):
            card.pack_start(_label(ev["quip"], "meta"), False, False, 0)
        self._approval_widgets[ev["id"]] = (yes, no, status)
        self._append(card)
        if not self.get_visible():
            self.open()  # an approval must never be invisible
        self.avatar.model.say("I need your approval for something.", "info")
        self.avatar.kick()

    def _answer(self, aid: str, ok: bool, yes, no, status) -> None:
        yes.set_sensitive(False)
        no.set_sensitive(False)
        status.set_text("approved" if ok else "denied")
        self.bridge.post("/api/approve", {"id": aid, "approved": ok})

    # ------------------------------------------------------------------ events
    def handle(self, ev: dict) -> None:
        t = ev["type"]
        if t == "hello":
            st = ev["status"]
            self._apply_settings(st["settings"])
            self._model_chip(st["model"])
            self._resources(st["resources"])
        elif t == "settings":
            self._apply_settings(ev)
        elif t == "model":
            self._model_chip(ev)
        elif t == "resources":
            self._resources(ev)
        elif t == "state":
            self.mood_lbl.set_text(f"{ev['mood']} · {ev['state'].lower()}" + (f" · {ev['detail']}" if ev.get("detail") else ""))
        elif t == "job_queued":
            if ev.get("source") == "voice":
                self._user_bubble(ev["text"], "voice")
            elif ev.get("session") != "desktop":
                return
            self._set_busy(True)
        elif t == "job_started":
            if ev.get("session") != "desktop":
                return
            self.current_job = ev["job"]
            self.current = StreamingMessage(self)
            self._append(self.current.box)
        elif t in ("token", "thinking", "retract") and self.current and ev.get("job") == self.current_job:
            if t == "token":
                self.current.add_text(ev["text"])
            elif t == "thinking":
                self.current.add_thought(ev["text"])
            else:
                self.current.retract()
        elif t == "tool_start" and ev.get("job") == self.current_job:
            self._note(f"⚙ {ev['summary']}…")
        elif t == "tool_end" and ev.get("job") == self.current_job:
            first = (ev.get("preview") or "").strip().splitlines()[:1]
            self._note(("✓ " if ev["ok"] else "✗ ") + ev["summary"] + (f" — {first[0][:120]}" if first and not ev["ok"] else ""),
                       "tool" if ev["ok"] else "tool bad")
            # the next tokens belong to a fresh bubble after a tool call
            self.current = StreamingMessage(self)
            self._append(self.current.box)
        elif t == "approval_request" and ev.get("session") in (None, "desktop"):
            self._approval_card(ev)
        elif t == "approval_resolved":
            w = self._approval_widgets.pop(ev["id"], None)
            if w:
                w[0].set_sensitive(False)
                w[1].set_sensitive(False)
                w[2].set_text("approved" if ev["approved"] else "denied")
        elif t == "reply_done" and ev.get("job") == self.current_job:
            st = ev.get("stats") or {}
            meta = f"{ev['elapsed_s']}s" + (f" · {st.get('gen_tps')} tok/s" if st.get("gen_tps") else "")
            if self.current:
                if not self.current.text.strip():
                    # tool-only round left an empty bubble; drop it
                    self.chat_list.remove(self.current.box)
                else:
                    self.current.finish(meta)
            self.current = None
            self._set_busy(False)
        elif t in ("error", "cancelled") and ev.get("job") in (None, self.current_job):
            if self.current and not self.current.text.strip():
                self.chat_list.remove(self.current.box)
            elif self.current:
                self.current.finish("interrupted" if t == "cancelled" else "failed")
            self.current = None
            if t == "error":
                self._error_bubble(ev.get("message", "error"), ev.get("quip"))
            else:
                self._note(ev.get("quip") or "(stopped)")
            self._set_busy(False)
        elif t == "memory_saved":
            self._note(f"📌 remembered: {ev['content'][:100]}  (Memory tab to review/delete)")
        elif t == "network":
            self._note(f"🌐 online: {ev['what']}")
        elif t == "tool_blocked":
            self._note(f"⛔ blocked: {ev['summary']}", "tool bad")
        elif t == "task":
            if self.stack.get_visible_child_name() == "tasks":
                self.refresh_tasks()
        elif t == "listening":
            self.recording = ev["active"]
            ctx = self.mic.get_style_context()
            (ctx.add_class if self.recording else ctx.remove_class)("recording")
            self.mic.set_label("● REC" if self.recording else "🎙")
        elif t == "warning":
            self._note("⚠ " + ev["message"], "tool bad")
        elif t == "emergency_stop":
            self._note("■ emergency stop: generation cancelled, tool processes killed, speech stopped")
            self._set_busy(False)

    def _apply_settings(self, s: dict) -> None:
        self.settings = s
        with self.online_btn.handler_block(self._online_handler):
            self.online_btn.set_active(bool(s.get("online")))
            self.online_btn.set_label("ONLINE" if s.get("online") else "OFFLINE")
        with self.speak_btn.handler_block(self._speak_handler):
            self.speak_btn.set_active(bool(s.get("speak")))
            self.speak_btn.set_label("🔊 voice" if s.get("speak") else "🔇 voice")
        with self.think_combo.handler_block(self._think_handler):
            self.think_combo.set_active_id(s.get("thinking", "auto"))

    def _model_chip(self, m: dict) -> None:
        state = m.get("state", "?")
        icon = {"ready": "●", "loading": "◌", "unloaded": "○", "error": "✗", "unloading": "◌"}.get(state, "?")
        txt = {"ready": "brain ready", "loading": "waking up…", "unloaded": "asleep (GPU free)",
               "error": "brain error", "unloading": "unloading…"}.get(state, state)
        self.model_chip.set_text(f"{icon} {txt}")
        ctx = self.model_chip.get_style_context()
        for c in ("bad", "gold"):
            ctx.remove_class(c)
        if state == "error":
            ctx.add_class("bad")
            self.model_chip.set_tooltip_text(m.get("detail", ""))
        elif state != "ready":
            ctx.add_class("gold")
        else:
            self.model_chip.set_tooltip_text(f"{m.get('profile')} on {m.get('device')} · loaded in {m.get('load_seconds')}s")
        stats = m.get("stats") or {}
        self.footer.set_text(f"local · {m.get('profile', '')}" + (f" · {stats.get('gen_tps')} tok/s" if stats.get("gen_tps") else ""))

    def _resources(self, r: dict) -> None:
        def v(x, unit=""):
            return "–" if x is None else f"{x:.0f}{unit}"
        vram = "–" if r.get("vram_used_mb") is None else f"{r['vram_used_mb'] / 1024:.1f}/{r['vram_total_mb'] / 1024:.1f}G"
        ram = "–" if r.get("ram_available_mb") is None else f"{r['ram_available_mb'] / 1024:.1f}G free"
        self.meters.set_text(f"CPU {v(r.get('cpu_percent'), '%')} {v(r.get('cpu_temp_c'), '°')}  ·  RAM {ram}  ·  "
                             f"VRAM {vram}  ·  GPU {v(r.get('gpu_util'), '%')} {v(r.get('gpu_temp_c'), '°')}")
        ctx = self.meters.get_style_context()
        (ctx.add_class if r.get("pressure") else ctx.remove_class)("warn")
        self.meters.set_tooltip_text("; ".join(r.get("pressure") or []) or "no resource pressure")

    # ------------------------------------------------------------------ header controls
    def _toggle_online(self, btn) -> None:
        self.bridge.post("/api/settings", {"online": btn.get_active()})

    def _toggle_speak(self, btn) -> None:
        self.bridge.post("/api/settings", {"speak": btn.get_active()})
        if not btn.get_active():
            self.bridge.post("/api/voice/stop", {})

    def _think_changed(self, combo) -> None:
        self.bridge.post("/api/settings", {"thinking": combo.get_active_id()})

    def _system_action(self, action: str) -> None:
        if action == "load":
            self.bridge.post("/api/model/load")
        elif action == "unload":
            self.bridge.post("/api/model/unload", error=lambda m: self._note("✗ " + m, "tool bad"))
        elif action == "halt":
            self.bridge.post("/api/stop")
        elif action == "reset":
            self.bridge.post("/api/reset", {"session": "desktop"})
            for c in self.chat_list.get_children():
                self.chat_list.remove(c)
            self._note("New conversation. I've already forgotten your last mistake.")
        GLib.timeout_add(600, lambda: (self.refresh_system(), False)[1])

    # ------------------------------------------------------------------ side tabs
    def _tab_changed(self, *_):
        name = self.stack.get_visible_child_name()
        {"memory": self.refresh_memory, "tasks": self.refresh_tasks, "system": self.refresh_system}.get(name, lambda: None)()

    @staticmethod
    def _clear(box: Gtk.Box) -> None:
        for c in box.get_children():
            box.remove(c)

    def refresh_memory(self) -> None:
        q = self.mem_search.get_text().strip()
        path = "/api/memory" + (f"?q={GLib.uri_escape_string(q, None, False)}" if q else "")
        self.bridge.get(path, done=self._show_memory, error=lambda m: None)

    def _show_memory(self, res: dict) -> None:
        self._clear(self.mem_box)
        items = res.get("items", [])
        if not items:
            self.mem_box.pack_start(_label("Nothing remembered yet. I only keep what's worth keeping.", "empty"), False, False, 0)
        for it in items:
            row = Gtk.Box(spacing=6)
            row.set_margin_start(12)
            row.set_margin_end(8)
            txt = f"<b>{html.escape(it['kind'])}</b>  {html.escape(it['content'][:400])}"
            row.pack_start(_label(txt, "item", markup=True, selectable=True), True, True, 0)
            rm = Gtk.Button(label="forget")
            rm.get_style_context().add_class("flat")
            rm.connect("clicked", lambda _b, i=it["id"]: self.bridge.delete(f"/api/memory/{i}", done=lambda r: self.refresh_memory()))
            row.pack_end(rm, False, False, 0)
            self.mem_box.pack_start(row, False, False, 2)
        self.mem_box.show_all()

    def _add_note(self, *_):
        text = self.mem_search.get_text().strip()
        if text:
            self.bridge.post("/api/memory", {"content": text, "kind": "note"}, done=lambda r: self.refresh_memory())
            self.mem_search.set_text("")

    def refresh_tasks(self) -> None:
        self.bridge.get("/api/tasks", done=self._show_tasks, error=lambda m: None)

    def _show_tasks(self, res: dict) -> None:
        self._clear(self.tasks_box)
        tasks = res.get("tasks", [])
        if not tasks:
            self.tasks_box.pack_start(_label("No tasks. Ask me to plan something big and I'll break it down.", "empty"), False, False, 0)
        icons = {"pending": "○", "in_progress": "◐", "done": "●", "skipped": "–", "failed": "✗"}
        for t in tasks:
            head = Gtk.Box(spacing=6)
            head.pack_start(_label(f"#{t['id']} {t['title']}  ({t['status']})", "section"), True, True, 0)
            rm = Gtk.Button(label="delete")
            rm.get_style_context().add_class("flat")
            rm.connect("clicked", lambda _b, i=t["id"]: self.bridge.delete(f"/api/tasks/{i}", done=lambda r: self.refresh_tasks()))
            head.pack_end(rm, False, False, 0)
            self.tasks_box.pack_start(head, False, False, 0)
            for i, s in enumerate(t["steps"], 1):
                self.tasks_box.pack_start(_label(f"{icons.get(s['status'], '?')}  {i}. {s['text']}", "item"), False, False, 0)
        self.tasks_box.show_all()

    def refresh_system(self) -> None:
        self.bridge.get("/api/diagnostics", done=self._show_system, error=lambda m: self.sys_lbl.set_text(m))

    def _show_system(self, d: dict) -> None:
        st = d["status"]
        m = st["model"]
        lines = [f"model     {m['state']} · {m['profile']} on {m['device']}",
                 f"          loaded in {m['load_seconds']}s · last {m.get('stats') or {}}",
                 f"internet  {'reachable' if d['internet_reachable'] else 'unreachable'} · "
                 f"web tools {'ON' if st['settings']['online'] else 'OFF'}",
                 f"voice     input={st['voice']['input']} output={st['voice']['output']}",
                 f"memory    {st['memory']['count']} items · integrity {d['memory_integrity']}",
                 f"api       {d['api']['bind']} ({d['api']['auth']})", "", "protections:"]
        for k, v in d["resource_guardian"]["protections"].items():
            lines.append(f"  {k}: {v}")
        self.sys_lbl.set_text("\n".join(lines))
