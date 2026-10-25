"""Tiny asyncio pub/sub used to fan backend events out to the UI, CLI and voice.

Every event is a plain JSON-serialisable dict with at least a "type" key. Subscribers get a
bounded queue; a slow subscriber loses its *oldest* events rather than blocking the backend.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from typing import Any

log = logging.getLogger(__name__)


class EventBus:
    def __init__(self, queue_size: int = 2000):
        self._subs: set[asyncio.Queue] = set()
        self._queue_size = queue_size
        self._seq = itertools.count(1)
        # Last event of selected types, replayed to new subscribers so a freshly started
        # avatar immediately shows the current state instead of guessing.
        self._sticky: dict[str, dict] = {}
        self.sticky_types = {"state", "status", "settings", "mood"}

    def publish(self, type_: str, **payload: Any) -> dict:
        event = {"type": type_, "seq": next(self._seq), "ts": time.time(), **payload}
        if type_ in self.sticky_types:
            self._sticky[type_] = event
        for q in list(self._subs):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(event)
        return event

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(self._queue_size)
        for ev in self._sticky.values():
            q.put_nowait(ev)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)
