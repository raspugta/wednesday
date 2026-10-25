"""`wednesday` command line.

    wednesday              start everything (backend + avatar) — the one command you need
    wednesday chat         chat in the terminal (works without the avatar)
    wednesday ask "..."    one-shot question, prints the answer
    wednesday status       short status
    wednesday doctor       full diagnostics
    wednesday stop         stop everything cleanly
    wednesday toggle       show/hide the avatar      (bind this in Hyprland)
    wednesday panel        open/close the chat panel (bind this in Hyprland)
    wednesday talk         push-to-talk toggle       (bind this in Hyprland)
    wednesday halt         emergency stop: cancel generation, kill tool processes, stop speech
    wednesday logs         tail the logs
    wednesday backend|ui   run one component in the foreground (debugging)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from wednesday.config import PROJECT_DIR, Config

C = {"cyan": "\033[96m", "dim": "\033[2m", "gold": "\033[33m", "red": "\033[91m", "green": "\033[92m",
     "bold": "\033[1m", "off": "\033[0m"}
if not sys.stdout.isatty():
    C = {k: "" for k in C}


def say(msg: str, color: str = "cyan") -> None:
    print(f"{C[color]}{msg}{C['off']}")


def _pid(cfg: Config, name: str) -> int | None:
    f = cfg.paths.state_dir / f"{name}.pid"
    try:
        pid = int(f.read_text().strip())
        os.kill(pid, 0)
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes()
        if b"wednesday" not in cmd:
            raise ProcessLookupError
        return pid
    except (OSError, ValueError, ProcessLookupError):
        f.unlink(missing_ok=True)
        return None


def _spawn(cfg: Config, component: str) -> int:
    log = open(cfg.paths.log_dir / f"{component}.out", "ab")
    env = dict(os.environ, PYTHONPATH=str(PROJECT_DIR / "src"))
    p = subprocess.Popen([sys.executable, "-m", "wednesday", component], cwd=str(PROJECT_DIR),
                         stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         start_new_session=True, env=env)
    (cfg.paths.state_dir / f"{component}.pid").write_text(str(p.pid))
    return p.pid


async def _wait_healthy(cfg: Config, timeout: float = 20) -> bool:
    from wednesday.api.client import Client
    c = Client(cfg)
    try:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if await c.healthy():
                return True
            await asyncio.sleep(0.2)
        return False
    finally:
        await c.close()


def _check_setup() -> list[str]:
    from wednesday.core.model_manager import ModelManager
    from wednesday.core.event_bus import EventBus
    cfg = Config.load()
    return ModelManager(cfg, EventBus()).check_files()


# ---------------------------------------------------------------------------- commands
def cmd_start(cfg: Config, args) -> int:
    cfg.paths.ensure()
    problems = _check_setup()
    if problems:
        say("Wednesday isn't set up yet:", "gold")
        for p in problems:
            print("  - " + p)
        say("Run:  ./wednesday setup", "gold")
        return 1
    if _pid(cfg, "backend"):
        say("Backend already running.", "dim")
    else:
        _spawn(cfg, "backend")
        if not asyncio.run(_wait_healthy(cfg)):
            say("Backend failed to start — see: wednesday logs", "red")
            return 1
        say("● backend up  (http://127.0.0.1:%s, localhost only)" % cfg.get("server.port"), "green")
    if args.no_ui:
        return 0
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
        say("No graphical session detected — avatar skipped. Use `wednesday chat`.", "gold")
        return 0
    if _pid(cfg, "ui"):
        say("Avatar already running.", "dim")
    else:
        _spawn(cfg, "ui")
        say("● avatar up   (click her to chat · right-click for options)", "green")
    say("Wednesday is awake. Try not to make it weird.", "cyan")
    return 0


def _stop_pid(cfg: Config, name: str, timeout: float = 8) -> bool:
    pid = _pid(cfg, name)
    if not pid:
        return False
    os.kill(pid, signal.SIGTERM)
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        os.kill(pid, signal.SIGKILL)
    (cfg.paths.state_dir / f"{name}.pid").unlink(missing_ok=True)
    return True


def cmd_stop(cfg: Config, args) -> int:
    ui = _stop_pid(cfg, "ui", 4)
    be = _stop_pid(cfg, "backend", 12)
    if not (ui or be):
        say("Nothing was running.", "dim")
    else:
        say("Stopped. " + ("Avatar " if ui else "") + ("Backend (model unloaded)" if be else ""), "green")
    return 0


async def _api(cfg: Config, method: str, path: str, payload: dict | None = None, timeout: float = 30):
    from wednesday.api.client import Client
    c = Client(cfg)
    try:
        return await c.request(method, path, payload, timeout=timeout)
    finally:
        await c.close()


def _api_or_explain(cfg, method, path, payload=None, timeout=30):
    from wednesday.api.client import BackendDown
    try:
        return asyncio.run(_api(cfg, method, path, payload, timeout))
    except BackendDown:
        say("Wednesday isn't running. Start her with:  ./wednesday", "gold")
        sys.exit(1)
    except RuntimeError as e:
        say(f"Error: {e}", "red")
        sys.exit(1)


def cmd_status(cfg: Config, args) -> int:
    st = _api_or_explain(cfg, "GET", "/api/status")
    m, r, s = st["model"], st["resources"], st["settings"]
    def v(x, unit=""):
        return "n/a" if x is None else f"{x:.0f}{unit}" if isinstance(x, float) else f"{x}{unit}"
    say(f"Wednesday {st['version']} — {st['state'].lower()}, feeling {st['mood']}", "bold")
    print(f"  model    : {m['state']} · {m['profile']} on {m['device'] or '?'}"
          + (f" · last {m['stats'].get('gen_tps')} tok/s" if m.get("stats") else ""))
    print(f"  mode     : {'ONLINE' if s['online'] else 'OFFLINE'} · thinking={s['thinking']} · "
          f"speak={'on' if s['speak'] else 'off'} · memory={'on' if s['memory'] else 'off'} ({st['memory']['count']} items)")
    print(f"  voice    : input={'yes' if st['voice']['input'] else 'no'} output={'yes' if st['voice']['output'] else 'no'}")
    print(f"  machine  : CPU {v(r['cpu_percent'], '%')} · RAM free {v(r['ram_available_mb'], ' MB')} · "
          f"VRAM {v(r['vram_used_mb'])}/{v(r['vram_total_mb'], ' MB')} · GPU {v(r['gpu_temp_c'], '°C')} · "
          f"CPU {v(r['cpu_temp_c'], '°C')}")
    if r.get("pressure"):
        say("  pressure : " + "; ".join(r["pressure"]), "gold")
    return 0


def cmd_doctor(cfg: Config, args) -> int:
    script = PROJECT_DIR / "scripts" / "doctor.sh"
    rc = subprocess.call(["bash", str(script)])
    from wednesday.api.client import BackendDown
    try:
        d = asyncio.run(_api(cfg, "GET", "/api/diagnostics", timeout=20))
    except (BackendDown, RuntimeError):
        say("\n(backend not running — live diagnostics skipped; start with ./wednesday)", "dim")
        return rc
    say("\nLive backend diagnostics", "bold")
    for k, val in d["resource_guardian"]["protections"].items():
        mark = C["red"] + "✗" if "UNAVAILABLE" in val or "disabled" in val else C["green"] + "✓"
        print(f"  {mark}{C['off']} {k:24s} {val}")
    print(f"  internet reachable: {d['internet_reachable']} · memory integrity: {d['memory_integrity']} · "
          f"model files: {', '.join(d['model_files'])}")
    return rc


def cmd_simple_post(path: str, payload: dict | None = None, msg: str = ""):
    def run(cfg: Config, args) -> int:
        res = _api_or_explain(cfg, "POST", path, payload)
        if msg:
            say(msg if isinstance(msg, str) else msg(res), "dim")
        return 0
    return run


def cmd_talk(cfg: Config, args) -> int:
    res = _api_or_explain(cfg, "POST", "/api/voice/ptt", {"action": "toggle"}, timeout=120)
    if res.get("recording"):
        say("🎙  listening… run `wednesday talk` again to send", "cyan")
    elif res.get("text"):
        say(f"heard: {res['text']}", "dim")
    return 0


def cmd_logs(cfg: Config, args) -> int:
    files = [str(cfg.paths.log_dir / f) for f in ("backend.log", "ui.log", "llama-server.log") if (cfg.paths.log_dir / f).exists()]
    if not files:
        say("No logs yet.", "dim")
        return 0
    os.execvp("tail", ["tail", "-n", "40", *(["-f"] if args.follow else []), *files])


def cmd_ask(cfg: Config, args) -> int:
    from wednesday.cli_chat import ask
    return asyncio.run(ask(cfg, " ".join(args.text), think=args.think))


def cmd_chat(cfg: Config, args) -> int:
    from wednesday.cli_chat import repl
    return asyncio.run(repl(cfg, think=args.think))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="wednesday", description="Wednesday — your local AI companion")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("start", help="start backend + avatar (default)")
    p.add_argument("--no-ui", action="store_true", help="backend only")
    sub.add_parser("stop", help="stop everything")
    p = sub.add_parser("restart", help="stop, then start")
    p.add_argument("--no-ui", action="store_true")
    sub.add_parser("status")
    sub.add_parser("doctor", help="full diagnostics")
    p = sub.add_parser("chat", help="terminal chat")
    p.add_argument("--think", choices=["auto", "fast", "deep"])
    p = sub.add_parser("ask", help="one-shot question")
    p.add_argument("text", nargs="+")
    p.add_argument("--think", choices=["auto", "fast", "deep"])
    sub.add_parser("toggle", help="show/hide avatar")
    sub.add_parser("panel", help="open/close chat panel")
    sub.add_parser("ghost", help="toggle click-through ghost mode")
    sub.add_parser("talk", help="push-to-talk toggle")
    sub.add_parser("halt", help="emergency stop")
    sub.add_parser("unload", help="free the GPU now")
    p = sub.add_parser("logs")
    p.add_argument("-f", "--follow", action="store_true")
    sub.add_parser("backend", help="run backend in foreground")
    sub.add_parser("ui", help="run avatar UI in foreground")
    args = ap.parse_args(argv)
    cmd = args.cmd or "start"
    if cmd == "start" and not hasattr(args, "no_ui"):
        args.no_ui = False

    if cmd == "backend":
        from wednesday.backend import main as backend_main
        backend_main()
        return 0
    if cmd == "ui":
        from wednesday.ui.app import main as ui_main
        return ui_main()

    cfg = Config.load()
    cfg.paths.ensure()
    handlers = {
        "start": cmd_start, "stop": cmd_stop, "status": cmd_status, "doctor": cmd_doctor,
        "chat": cmd_chat, "ask": cmd_ask, "talk": cmd_talk, "logs": cmd_logs,
        "toggle": cmd_simple_post("/api/ui", {"command": "toggle_avatar"}),
        "panel": cmd_simple_post("/api/ui", {"command": "toggle_panel"}),
        "ghost": cmd_simple_post("/api/ui", {"command": "ghost"}, "Ghost mode toggled (clicks pass through her)."),
        "halt": cmd_simple_post("/api/stop", None, lambda r: f"Halted: {json.dumps(r)}"),
        "unload": cmd_simple_post("/api/model/unload", None, "Model unloaded. VRAM is yours again."),
    }
    if cmd == "restart":
        cmd_stop(cfg, args)
        return cmd_start(cfg, args)
    return handlers[cmd](cfg, args) or 0


if __name__ == "__main__":
    sys.exit(main())
