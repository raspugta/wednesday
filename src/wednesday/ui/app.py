"""Desktop UI process: avatar + panel. Independent of the backend — either can crash or restart
without taking the other down."""

from __future__ import annotations

import logging
import os
import signal
import sys

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

from wednesday.backend import setup_logging  # noqa: E402
from wednesday.config import Config  # noqa: E402
from wednesday.ui.avatar import AvatarWindow  # noqa: E402
from wednesday.ui.bridge import Bridge  # noqa: E402
from wednesday.ui.panel import Panel  # noqa: E402

log = logging.getLogger("wednesday.ui")


class App:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.avatar = AvatarWindow(cfg, on_click=self.toggle_panel, on_menu_action=self.menu_action)
        self.bridge = Bridge(cfg, self.on_event, self.on_connection)
        self.bridge.fps_source = lambda: self.avatar.measured_fps
        self.panel = Panel(self.bridge, self.avatar)
        self.ghost = False
        self.speak = False
        self.avatar.model.connected = False
        self.avatar.model.say("Connecting to my brain…", "info", 4)

    def run(self) -> int:
        self.avatar.show_all()
        self.bridge.start()
        for sig in (signal.SIGINT, signal.SIGTERM):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, self.quit)
        Gtk.main()
        return 0

    def quit(self, *_):
        self.bridge.stop()
        Gtk.main_quit()
        return False

    # ------------------------------------------------------------------ actions
    def toggle_panel(self) -> None:
        self.panel.toggle()

    def menu_action(self, action: str) -> None:
        b = self.bridge
        if action == "panel":
            self.panel.open()
        elif action == "talk":
            b.post("/api/voice/ptt", {"action": "toggle"}, timeout=120, error=self._warn)
        elif action == "speak":
            b.post("/api/settings", {"speak": not self.speak})
        elif action == "ghost":
            self.set_ghost(not self.ghost)
        elif action == "unload":
            b.post("/api/model/unload", error=self._warn)
        elif action == "halt":
            b.post("/api/stop")
        elif action == "hide":
            self.avatar.hide()
            self.panel.hide()
        elif action == "reset":
            self.avatar.reset_geometry()
            self.panel.place()
        elif action == "moved":
            if self.panel.get_visible():
                self.panel.place()
        elif action == "quit":
            # Quit the whole assistant, not just the face.
            b.post("/api/shutdown", done=lambda _r: self.quit(), error=lambda _m: self.quit())

    def set_ghost(self, ghost: bool) -> None:
        self.ghost = ghost
        self.avatar.set_ghost(ghost)
        self.avatar.model.say("Ghost mode on — clicks pass through me. `wednesday ghost` to undo."
                              if ghost else "Solid again.", "info", 4)
        self.avatar.kick()

    def _warn(self, msg: str) -> None:
        self.avatar.model.say(msg, "warn")
        self.avatar.kick()

    # ------------------------------------------------------------------ backend events
    def on_connection(self, connected: bool) -> bool:
        m = self.avatar.model
        m.connected = connected
        if not connected:
            m.set_state("IDLE", "sleepy")
            m.stop_speech()
            m.say("My brain is offline. I'll wait. (start it: wednesday)", "warn", 6)
        self.avatar.kick()
        return False

    def on_event(self, ev: dict) -> bool:
        m = self.avatar.model
        t = ev["type"]
        if t == "hello":
            st = ev["status"]
            m.connected = True
            m.set_state(st["state"], st["mood"])
            self.speak = st["settings"].get("speak", False)
            if m.bubble and m.bubble.text.startswith(("Connecting", "My brain")):
                m.bubble = None
        elif t == "state":
            m.set_state(ev["state"], ev.get("mood"))
        elif t == "quip" and ev.get("text"):
            m.say(ev["text"])
        elif t == "token":
            m.token()
        elif t == "speech":
            if ev.get("active"):
                m.start_speech(ev.get("envelope") or [], ev.get("frame_ms", 40), ev.get("duration", 0))
            else:
                m.stop_speech()
        elif t == "settings":
            self.speak = ev.get("speak", False)
        elif t == "error":
            m.say(ev.get("quip") or "Something went wrong.", "warn")
        elif t == "cancelled" and ev.get("quip"):
            m.say(ev["quip"])
        elif t == "warning":
            m.say(ev["message"], "warn")
        elif t == "transcript":
            m.say(f"“{ev['text']}”", "info", 3)
        elif t == "reply_done" and not self.panel.get_visible() and ev.get("text") and ev.get("session") == "desktop":
            # Panel closed (e.g. a voice question): show the start of the answer in her bubble.
            m.say(ev["text"].split("\n")[0][:140], "quip", 8)
        elif t == "ui":
            self._ui_command(ev["command"])
        elif t == "shutdown":
            m.connected = False
        self.panel.handle(ev)
        self.avatar.kick()
        return False

    def _ui_command(self, cmd: str) -> None:
        if cmd == "toggle_avatar":
            if self.avatar.get_visible():
                self.avatar.hide()
                self.panel.hide()
            else:
                self.avatar.show_all()
        elif cmd == "toggle_panel":
            if not self.avatar.get_visible():
                self.avatar.show_all()
            self.panel.toggle()
        elif cmd == "show_panel":
            self.avatar.show_all()
            self.panel.open()
        elif cmd == "ghost":
            self.set_ghost(not self.ghost)
        elif cmd == "quit_ui":
            self.quit()


def main() -> int:
    cfg = Config.load()
    setup_logging(cfg, "ui")
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
        log.error("no graphical session")
        return 1
    (cfg.paths.state_dir / "ui.pid").write_text(str(os.getpid()))
    try:
        return App(cfg).run()
    finally:
        (cfg.paths.state_dir / "ui.pid").unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
