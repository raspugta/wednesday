"""Runs the asyncio API client on a background thread and marshals events into GTK.

The UI must survive the backend dying (and vice versa): this bridge reconnects forever with
backoff, and tells the UI about connect/disconnect so the avatar can show "brain offline".
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Any, Callable, Coroutine

from gi.repository import GLib

from wednesday.api.client import BackendDown, Client
from wednesday.config import Config

log = logging.getLogger(__name__)


class Bridge:
    def __init__(self, cfg: Config, on_event: Callable[[dict], None], on_connection: Callable[[bool], None]):
        self.cfg = cfg
        self.client = Client(cfg)
        self.on_event = on_event
        self.on_connection = on_connection
        self.loop = asyncio.new_event_loop()
        self.connected = False
        self.fps_source: Callable[[], float] | None = None
        self._thread = threading.Thread(target=self._run, name="wednesday-bridge", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.create_task(self._events_forever())
        self.loop.create_task(self._metrics_forever())
        self.loop.run_forever()

    def _set_connected(self, value: bool) -> None:
        if value != self.connected:
            self.connected = value
            GLib.idle_add(self.on_connection, value)

    async def _events_forever(self) -> None:
        delay = 0.5
        while True:
            try:
                async for ev in self.client.events():
                    self._set_connected(True)
                    delay = 0.5
                    GLib.idle_add(self.on_event, ev)
            except BackendDown:
                pass
            except Exception:
                log.exception("event stream error")
            self._set_connected(False)
            await asyncio.sleep(delay)
            delay = min(delay * 1.6, 5.0)

    async def _metrics_forever(self) -> None:
        while True:
            await asyncio.sleep(10)
            if self.connected and self.fps_source:
                try:
                    await self.client.send_ws({"type": "ui_metrics", "fps": round(self.fps_source(), 1),
                                               "pid": os.getpid()})
                except Exception:
                    pass

    def call(self, coro_fn: Callable[[], Coroutine], done: Callable[[Any], None] | None = None,
             error: Callable[[str], None] | None = None) -> None:
        """Run an API coroutine off the GTK thread; deliver result/error back on the GTK thread."""
        async def runner():
            try:
                res = await coro_fn()
                if done:
                    GLib.idle_add(done, res)
            except Exception as e:  # BackendDown, RuntimeError from API, ...
                msg = "Backend is offline — start it with: wednesday" if isinstance(e, BackendDown) else str(e)
                if error:
                    GLib.idle_add(error, msg)
                else:
                    log.warning("api call failed: %s", msg)
        asyncio.run_coroutine_threadsafe(runner(), self.loop)

    # convenience wrappers
    def post(self, path: str, payload: dict | None = None, done=None, error=None, timeout: float = 30) -> None:
        self.call(lambda: self.client.post(path, payload, timeout=timeout), done, error)

    def get(self, path: str, done=None, error=None) -> None:
        self.call(lambda: self.client.get(path), done, error)

    def delete(self, path: str, done=None, error=None) -> None:
        self.call(lambda: self.client.request("DELETE", path), done, error)
