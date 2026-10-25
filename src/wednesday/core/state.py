"""Avatar state machine. Lives in the backend so every client (avatar, panel, CLI) agrees.

States are driven only by real backend activity:
  LISTENING  – microphone is actually recording
  THINKING   – waiting for the model's first token / model is reasoning
  SPEAKING   – reply text is streaming, or TTS audio is playing
  WORKING    – a tool is executing (or waiting for your approval)
  SUCCESS    – brief confirmation after a job that used tools finished OK
  ERROR      – brief indication after a failure
  IDLE       – nothing happening
  SLEEPING   – idle for a while, or the model is unloaded
"""

from __future__ import annotations

import asyncio
import time
from enum import Enum

from wednesday.core.event_bus import EventBus


class AvatarState(str, Enum):
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    THINKING = "THINKING"
    SPEAKING = "SPEAKING"
    WORKING = "WORKING"
    SUCCESS = "SUCCESS"
    ERROR = "ERROR"
    SLEEPING = "SLEEPING"


TRANSIENT = {AvatarState.SUCCESS: 1.8, AvatarState.ERROR: 3.5}


class StateController:
    def __init__(self, bus: EventBus, sleep_after_s: float = 480):
        self.bus = bus
        self.state = AvatarState.IDLE
        self.mood = "calm"
        self.detail = ""
        self.sleep_after_s = sleep_after_s
        self.last_activity = time.time()
        self._revert: asyncio.TimerHandle | None = None
        self.listening = False
        self.speaking_audio = False
        self.busy = False

    def touch(self) -> None:
        self.last_activity = time.time()

    def set(self, state: AvatarState, mood: str | None = None, detail: str = "") -> None:
        if self._revert:
            self._revert.cancel()
            self._revert = None
        if mood:
            self.mood = mood
        changed = state != self.state or mood is not None or detail != self.detail
        self.state = state
        self.detail = detail
        if state not in (AvatarState.IDLE, AvatarState.SLEEPING):
            self.touch()
        if changed:
            self.bus.publish("state", state=state.value, mood=self.mood, detail=detail)
        if state in TRANSIENT:
            loop = asyncio.get_running_loop()
            self._revert = loop.call_later(TRANSIENT[state], self.settle)

    def set_mood(self, mood: str) -> None:
        if mood != self.mood:
            self.mood = mood
            self.bus.publish("state", state=self.state.value, mood=mood, detail=self.detail)

    def settle(self) -> None:
        """Return to the resting state appropriate for what is still going on."""
        if self.listening:
            self.set(AvatarState.LISTENING)
        elif self.speaking_audio:
            self.set(AvatarState.SPEAKING)
        elif self.busy:
            self.set(AvatarState.THINKING)
        else:
            self.set(AvatarState.IDLE)

    def maybe_sleep(self, model_loaded: bool) -> None:
        if self.state != AvatarState.IDLE or self.busy or self.listening or self.speaking_audio:
            return
        idle_for = time.time() - self.last_activity
        if idle_for > self.sleep_after_s or (not model_loaded and idle_for > 60):
            self.set(AvatarState.SLEEPING, "sleepy")

    def wake(self) -> bool:
        if self.state == AvatarState.SLEEPING:
            self.set(AvatarState.IDLE, "calm")
            return True
        return False
