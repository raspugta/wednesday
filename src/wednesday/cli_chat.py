"""Terminal chat client — a complete fallback when the avatar isn't running."""

from __future__ import annotations

import asyncio
import os
import sys

from wednesday.api.client import BackendDown, Client
from wednesday.config import Config

D, CY, GO, RE, GR, B, X = "\033[2m", "\033[96m", "\033[33m", "\033[91m", "\033[92m", "\033[1m", "\033[0m"
if not sys.stdout.isatty():
    D = CY = GO = RE = GR = B = X = ""

MOOD_ICON = {"calm": "·", "amused": "☺", "smug": "😏", "curious": "?", "concerned": "!", "proud": "★",
             "annoyed": "¬", "sheepish": "…", "warm": "♥", "sleepy": "z", "bored": "~"}


async def _event_pump(c: Client, q: asyncio.Queue) -> None:
    while True:
        try:
            async for ev in c.events():
                await q.put(ev)
        except BackendDown:
            await q.put({"type": "_down"})
            await asyncio.sleep(1)


async def _prompt(text: str) -> str:
    return await asyncio.get_running_loop().run_in_executor(None, input, text)


async def _run_job(c: Client, q: asyncio.Queue, text: str, session: str, think: str | None) -> None:
    res = await c.post("/api/chat", {"text": text, "session": session, "think": think, "source": "cli"})
    job = res["job"]
    in_thought = False
    started = False
    while True:
        ev = await q.get()
        t = ev["type"]
        if ev.get("job") not in (None, job) and t != "quip":
            continue  # includes approvals: only ever answer approvals for our own request
        if t == "thinking":
            if not in_thought:
                print(f"{D}(thinking) ", end="")
                in_thought = True
            print(ev["text"], end="", flush=True)
        elif t == "token":
            if in_thought:
                print(X)
                in_thought = False
            if not started:
                print(f"{CY}{B}Wednesday{X} ", end="")
                started = True
            print(ev["text"], end="", flush=True)
        elif t == "tool_start":
            print(f"\n{GO}⚙ {ev['summary']}{X}", flush=True)
            started = False
        elif t == "tool_end":
            print(f"  {GR + '✓' if ev['ok'] else RE + '✗'}{X} {D}{ev['preview'][:160].splitlines()[0] if ev['preview'] else ''}{X}")
        elif t == "approval_request":
            print(f"\n{GO}{B}Approval needed:{X} {ev['summary']}")
            if ev.get("reason"):
                print(f"  {D}why: {ev['reason']}{X}")
            if ev.get("destructive"):
                print(f"  {RE}⚠ this can modify or delete data{X}")
            if ev.get("details") and ev["details"] != ev["summary"]:
                print(D + "\n".join("  │ " + line for line in ev["details"].splitlines()[:25]) + X)
            try:
                ans = (await _prompt(f"  Allow? [y/N] ")).strip().lower()
            except EOFError:
                ans = "n"  # no interactive input → never approve by default
                print("n (no input)")
            await c.post("/api/approve", {"id": ev["id"], "approved": ans in ("y", "yes")})
        elif t == "reply_done":
            stats = ev.get("stats") or {}
            mood = ev.get("mood", "calm")
            print(f"\n{D}  {MOOD_ICON.get(mood, '·')} {mood} · {ev['elapsed_s']}s"
                  + (f" · {stats.get('gen_tps')} tok/s" if stats.get("gen_tps") else "")
                  + (" · thought first" if ev.get("thinking") else "") + X)
            return
        elif t == "error":
            print(f"\n{RE}✗ {ev['message']}{X}")
            if ev.get("quip"):
                print(f"{D}  {ev['quip']}{X}")
            return
        elif t == "cancelled":
            print(f"\n{D}{ev.get('quip') or '(cancelled)'}{X}")
            return
        elif t == "_down":
            print(f"\n{RE}Lost connection to the backend.{X}")
            return


async def _ensure(c: Client) -> bool:
    if await c.healthy():
        return True
    print(f"{GO}Wednesday isn't running. Start her with: ./wednesday{X}")
    return False


HELP = f"""{D}commands: /think auto|fast|deep · /online on|off · /speak on|off · /reset · /stop · /status · /quit
Ctrl+C while she's talking cancels the reply.{X}"""


async def repl(cfg: Config, think: str | None = None) -> int:
    c = Client(cfg)
    if not await _ensure(c):
        return 1
    q: asyncio.Queue = asyncio.Queue()
    pump = asyncio.create_task(_event_pump(c, q))
    session = f"cli-{os.getpid()}"
    st = await c.get("/api/status")
    print(f"{CY}{B}Wednesday{X} {D}— {st['model']['state']} · {'online' if st['settings']['online'] else 'offline'}"
          f" · /help for commands{X}")
    try:
        while True:
            try:
                text = (await _prompt(f"{B}you ›{X} ")).strip()
            except EOFError:
                break
            if not text:
                continue
            if text.startswith("/"):
                parts = text[1:].split()
                cmd, arg = parts[0], (parts[1] if len(parts) > 1 else "")
                if cmd in ("quit", "exit", "q"):
                    break
                if cmd == "help":
                    print(HELP)
                elif cmd == "reset":
                    await c.post("/api/reset", {"session": session})
                    print(f"{D}Fresh start. I've already forgotten your last mistake.{X}")
                elif cmd == "stop":
                    print(await c.post("/api/stop"))
                elif cmd == "status":
                    print(await c.get("/api/status"))
                elif cmd == "think" and arg in ("auto", "fast", "deep"):
                    think = arg
                    print(f"{D}thinking mode: {arg}{X}")
                elif cmd in ("online", "speak") and arg in ("on", "off"):
                    print(await c.post("/api/settings", {cmd: arg == "on"}))
                else:
                    print(HELP)
                continue
            while not q.empty():  # drop stale events from before this question
                q.get_nowait()
            task = asyncio.create_task(_run_job(c, q, text, session, think))
            try:
                await asyncio.shield(task)
            except KeyboardInterrupt:
                await c.post("/api/cancel", {})
            except RuntimeError as e:
                print(f"{RE}{e}{X}")
    except KeyboardInterrupt:
        pass
    finally:
        pump.cancel()
        await c.close()
    print(f"{D}Bye. I'll be here, judging quietly.{X}")
    return 0


async def ask(cfg: Config, text: str, think: str | None = None) -> int:
    c = Client(cfg)
    if not await _ensure(c):
        return 1
    q: asyncio.Queue = asyncio.Queue()
    pump = asyncio.create_task(_event_pump(c, q))
    try:
        await asyncio.sleep(0.15)  # let the event stream attach before submitting
        while not q.empty():
            q.get_nowait()
        await _run_job(c, q, text, f"ask-{os.getpid()}", think)
    except RuntimeError as e:
        print(f"{RE}{e}{X}")
        return 1
    finally:
        pump.cancel()
        await c.close()
    return 0
