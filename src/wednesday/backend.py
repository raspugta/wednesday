"""Backend process: wires every component together and serves the local API."""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import signal
import time

from aiohttp import web

from wednesday import __version__
from wednesday.api.server import make_app, write_token
from wednesday.config import Config
from wednesday.core import persona
from wednesday.core.event_bus import EventBus
from wednesday.core.model_manager import ModelError, ModelManager
from wednesday.core.orchestrator import Orchestrator
from wednesday.core.resource_guardian import ResourceGuardian
from wednesday.core.state import AvatarState, StateController
from wednesday.memory.store import MemoryStore
from wednesday.tools import web as webtools

log = logging.getLogger("wednesday")


def setup_logging(cfg: Config, name: str) -> None:
    cfg.paths.ensure()
    handler = logging.handlers.RotatingFileHandler(cfg.paths.log_dir / f"{name}.log", maxBytes=2_000_000, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    if os.isatty(2):
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(console)
    root.setLevel(cfg.get("logging.level", "INFO"))
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


class Backend:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.bus = EventBus()
        self.states = StateController(self.bus, float(cfg.get("avatar.sleep_after_minutes")) * 60)
        self.guardian = ResourceGuardian(cfg, self.bus)
        self.model = ModelManager(cfg, self.bus)
        self.memory: MemoryStore | None = None
        try:
            self.memory = MemoryStore(cfg.paths.db_file)
        except Exception:
            log.exception("memory store unavailable — continuing without persistent memory")
        self.orchestrator = Orchestrator(cfg, self.bus, self.model, self.guardian, self.memory, self.states)
        self.speaker = None
        self.listener = None
        self._init_voice()
        webtools.set_contact(cfg.get("web.contact", ""))
        self._stop = asyncio.Event()
        self._online_cache: tuple[float, bool] = (0.0, False)
        self.started = time.time()

    def _init_voice(self) -> None:
        if not self.cfg.get("voice.enabled"):
            return
        try:
            from wednesday.voice.audio import Listener, Speaker, cleanup_stale_audio
        except ImportError as e:
            log.warning("voice disabled: %s", e)
            return
        cleanup_stale_audio(self.cfg.paths.tmp_dir)
        sp, li = Speaker(self.cfg, self.bus, self.states), Listener(self.cfg, self.bus, self.states)
        self.speaker = sp if sp.available else None
        self.listener = li if li.available else None
        if self.listener:
            self.listener.on_auto_stop = lambda: self.push_to_talk("stop")
        self.orchestrator.speaker = self.speaker

    # ------------------------------------------------------------------ actions
    async def load_model(self) -> None:
        try:
            await self.model.ensure_loaded()
            self.guardian.our_pids.add(self.model.status.pid or 0)
            if self.orchestrator.current is None:
                secs = await self.orchestrator.warm_up()
                log.info("prompt cache warmed in %ss", secs)
        except ModelError as e:
            self.bus.publish("error", message=str(e), quip=persona.quip("error", self.orchestrator.humor))
            self.states.set(AvatarState.ERROR, "sheepish")

    async def push_to_talk(self, action: str) -> dict:
        if not self.listener:
            raise RuntimeError("voice input unavailable (run ./wednesday setup --voice)")
        if action == "toggle":
            action = "stop" if self.listener.recording else "start"
        if action == "start":
            if self.speaker:
                self.speaker.stop()  # barge-in: talking over Wednesday shuts her up
            self.listener.start()
            return {"recording": True}
        if action == "cancel":
            self.listener.cancel()
            return {"recording": False}
        text = await self.listener.stop_and_transcribe()
        if not text:
            self.bus.publish("quip", text="I didn't catch that. Try again — closer to the mic.")
            self.states.settle()
            return {"recording": False, "text": ""}
        self.bus.publish("transcript", text=text)
        job = self.orchestrator.submit(text, "desktop", source="voice")
        return {"recording": False, "text": text, "job": job.id}

    def request_shutdown(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------ status
    def status(self) -> dict:
        s = self.guardian.snap
        if time.time() - s.ts > 3:  # never report a stale snapshot
            s = self.guardian.sample()
        return {
            "version": __version__,
            "uptime_s": round(time.time() - self.started),
            "state": self.states.state.value,
            "mood": self.states.mood,
            "model": self.model.status.as_dict(),
            "settings": self.orchestrator.settings,
            "busy": self.orchestrator.current is not None,
            "queue": self.orchestrator.queue.qsize(),
            "voice": {"input": self.listener is not None, "output": self.speaker is not None,
                      "recording": bool(self.listener and self.listener.recording)},
            "memory": {"enabled": self.memory is not None, "count": self.memory.count() if self.memory else 0},
            "resources": s.as_dict(),
            "online_reachable": self._online_cache[1],
        }

    async def diagnostics(self) -> dict:
        online = await webtools.is_online()
        self._online_cache = (time.time(), online)
        report = self.guardian.report()
        return {
            "status": self.status(),
            "resource_guardian": report,
            "internet_reachable": online,
            "model_files": self.model.check_files() or ["ok"],
            "memory_integrity": self.memory.integrity_ok() if self.memory else "disabled",
            "api": {"bind": f"{self.cfg.get('server.host')}:{self.cfg.get('server.port')}", "auth": "bearer token"},
        }

    # ------------------------------------------------------------------ lifecycle
    async def run(self) -> None:
        host, port = self.cfg.get("server.host"), int(self.cfg.get("server.port"))
        if host not in ("127.0.0.1", "localhost", "::1"):
            log.warning("API bound to %s — reachable from other machines. Only do this on purpose.", host)
        token = write_token(self.cfg.paths.token_file)
        app = make_app(self, token)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()
        pid_file = self.cfg.paths.state_dir / "backend.pid"
        pid_file.write_text(str(os.getpid()))
        self.guardian.our_pids.add(os.getpid())
        log.info("Wednesday %s listening on http://%s:%s", __version__, host, port)

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._stop.set)

        self.guardian.start()
        self.orchestrator.start()
        if self.speaker:
            self.speaker.start()
        bg = [asyncio.create_task(self.orchestrator.housekeeping()), asyncio.create_task(self._voice_housekeeping())]
        self.bus.publish("quip", text=persona.quip("startup", self.orchestrator.humor))
        self.states.set(AvatarState.IDLE, "calm")
        if self.cfg.get("model.preload"):
            bg.append(asyncio.create_task(self.load_model()))
        asyncio.create_task(self._probe_online())

        await self._stop.wait()
        log.info("shutting down")
        self.bus.publish("quip", text=persona.quip("shutdown", self.orchestrator.humor))
        self.bus.publish("shutdown")
        await asyncio.sleep(0.3)  # let the farewell reach the avatar
        for t in bg:
            t.cancel()
        if self.listener:
            self.listener.cancel()
        if self.speaker:
            self.speaker.stop()
        await self.orchestrator.stop()
        await self.guardian.stop()
        await self.model.close()
        await runner.cleanup()
        if self.memory:
            self.memory.close()
        pid_file.unlink(missing_ok=True)
        log.info("bye")

    async def _probe_online(self) -> None:
        self._online_cache = (time.time(), await webtools.is_online())

    async def _voice_housekeeping(self) -> None:
        while True:
            await asyncio.sleep(60)
            if self.listener:
                self.listener.maybe_unload()


def main() -> None:
    cfg = Config.load()
    setup_logging(cfg, "backend")
    asyncio.run(Backend(cfg).run())


if __name__ == "__main__":
    main()
