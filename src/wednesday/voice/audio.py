"""Voice I/O: push-to-talk speech recognition and interruptible speech output.

Audio goes through PipeWire's own CLI tools (pw-record / pw-cat) instead of a Python audio
library: no extra native deps, correct default-device handling, and killing the process is a
guaranteed, instant way to stop recording or playback.

Privacy: recordings are written to ~/.local/state/wednesday/tmp (mode 0700), transcribed, and
deleted immediately. Nothing records unless you press the mic button; recording also stops
by itself after voice.max_record_seconds.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import subprocess
import time
import uuid
import wave
from pathlib import Path

import numpy as np

from wednesday.config import Config
from wednesday.core import persona
from wednesday.core.event_bus import EventBus
from wednesday.core.state import AvatarState, StateController
from wednesday.core.text import for_speech

log = logging.getLogger(__name__)

ENVELOPE_MS = 40


def envelope(samples: np.ndarray, rate: int, frame_ms: int = ENVELOPE_MS) -> list[float]:
    """Per-frame loudness 0..1 used to drive the avatar's mouth in sync with audio."""
    n = max(1, int(rate * frame_ms / 1000))
    frames = len(samples) // n
    if frames == 0:
        return []
    x = samples[: frames * n].astype(np.float32).reshape(frames, n) / 32768.0
    rms = np.sqrt((x ** 2).mean(axis=1))
    peak = float(rms.max()) or 1.0
    return [round(float(v), 3) for v in np.clip(rms / peak, 0, 1)]


class VoiceUnavailable(RuntimeError):
    pass


class Speaker:
    """Sentence queue → Piper synthesis (CPU) → pw-cat playback, pipelined and interruptible."""

    def __init__(self, cfg: Config, bus: EventBus, states: StateController):
        self.cfg = cfg
        self.bus = bus
        self.states = states
        self._voice = None
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._player: subprocess.Popen | None = None
        self._task: asyncio.Task | None = None
        self._generation = 0  # bumps on stop() so stale synth results are discarded

    @property
    def available(self) -> bool:
        return self.cfg.data_path(self.cfg.get("voice.tts_voice")).exists() and shutil.which("pw-cat") is not None

    def _load(self):
        if self._voice is None:
            try:
                from piper import PiperVoice
            except ImportError as e:
                raise VoiceUnavailable("piper-tts is not installed (./wednesday setup --voice)") from e
            path = self.cfg.data_path(self.cfg.get("voice.tts_voice"))
            if not path.exists():
                raise VoiceUnavailable(f"voice model missing: {path}")
            self._voice = PiperVoice.load(str(path))
        return self._voice

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())
        # Load the voice in the background so the first spoken reply has no 3 s delay.
        asyncio.get_running_loop().run_in_executor(None, self._preload)

    def _preload(self) -> None:
        try:
            self._load()
        except Exception as e:
            log.warning("voice preload failed: %s", e)

    def feed(self, text: str) -> None:
        spoken = for_speech(text)
        if spoken:
            self._queue.put_nowait(spoken)

    def flush(self) -> None:
        pass  # sentences are queued as they complete; nothing buffered here

    def stop(self) -> None:
        self._generation += 1
        while not self._queue.empty():
            self._queue.get_nowait()
        if self._player and self._player.poll() is None:
            self._player.kill()
        self._finish()

    def _finish(self) -> None:
        if self.states.speaking_audio:
            self.states.speaking_audio = False
            self.bus.publish("speech", active=False)
            if self.states.state == AvatarState.SPEAKING:
                self.states.settle()

    def _synth(self, text: str) -> tuple[bytes, int, list[float], str]:
        voice = self._load()
        pcm = []
        rate = 22050
        for chunk in voice.synthesize(text):
            pcm.append(chunk.audio_int16_array)
            rate = chunk.sample_rate
        samples = np.concatenate(pcm) if pcm else np.zeros(0, dtype=np.int16)
        return samples.tobytes(), rate, envelope(samples, rate), text

    async def _run(self) -> None:
        while True:
            try:
                text = await self._queue.get()
                gen = self._generation
                result = await asyncio.to_thread(self._synth, text)
                while gen == self._generation:
                    # Synthesise the next sentence while this one plays → no gaps between sentences.
                    nxt = None
                    if not self._queue.empty():
                        nxt = asyncio.create_task(asyncio.to_thread(self._synth, self._queue.get_nowait()))
                    await self._play(*result, gen)
                    if nxt is None:
                        break
                    result = await nxt
                if self._queue.empty():
                    self._finish()
            except asyncio.CancelledError:
                raise
            except VoiceUnavailable as e:
                self.bus.publish("warning", message=str(e))
                self.stop()
            except Exception:
                log.exception("speech failed")
                self._finish()

    async def _play(self, audio: bytes, rate: int, env: list[float], text: str, gen: int) -> None:
        if gen != self._generation:
            return
        args = ["pw-cat", "--playback", "--raw", "--rate", str(rate), "--channels", "1", "--format", "s16"]
        if dev := self.cfg.get("voice.output_device"):
            args += ["--target", str(dev)]
        self.states.speaking_audio = True
        self.states.set(AvatarState.SPEAKING)
        self.bus.publish("speech", active=True, envelope=env, frame_ms=ENVELOPE_MS,
                         duration=len(audio) / 2 / rate, text=text[:200])
        self._player = subprocess.Popen([*args, "-"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            await asyncio.to_thread(self._feed_player, self._player, audio)
            await asyncio.to_thread(self._player.wait)
        finally:
            self._player = None

    @staticmethod
    def _feed_player(proc: subprocess.Popen, audio: bytes) -> None:
        try:
            proc.stdin.write(audio)
            proc.stdin.close()
        except (BrokenPipeError, ValueError):
            pass  # killed mid-sentence by stop()

    async def say(self, text: str) -> None:
        self.feed(text)


class Listener:
    """Push-to-talk: start() begins recording, stop() transcribes and returns the text."""

    def __init__(self, cfg: Config, bus: EventBus, states: StateController):
        self.cfg = cfg
        self.bus = bus
        self.states = states
        self._model = None
        self._model_used = 0.0
        self._rec: subprocess.Popen | None = None
        self._file: Path | None = None
        self._auto_stop: asyncio.TimerHandle | None = None
        self.on_auto_stop = None  # coroutine fn, set by the backend

    @property
    def recording(self) -> bool:
        return self._rec is not None

    @property
    def available(self) -> bool:
        return self.cfg.data_path(self.cfg.get("voice.stt_model_dir")).exists() and shutil.which("pw-record") is not None

    def start(self) -> None:
        if self._rec:
            return
        if not shutil.which("pw-record"):
            raise VoiceUnavailable("pw-record not found (PipeWire tools)")
        self._file = self.cfg.paths.tmp_dir / f"ptt-{uuid.uuid4().hex}.wav"
        args = ["pw-record", "--rate", "16000", "--channels", "1", "--format", "s16"]
        if dev := self.cfg.get("voice.input_device"):
            args += ["--target", str(dev)]
        self._rec = subprocess.Popen([*args, str(self._file)], stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, start_new_session=True)
        self.states.listening = True
        self.states.wake()
        self.states.set(AvatarState.LISTENING, "curious")
        self.bus.publish("listening", active=True, max_seconds=self.cfg.get("voice.max_record_seconds"))
        loop = asyncio.get_running_loop()
        self._auto_stop = loop.call_later(float(self.cfg.get("voice.max_record_seconds")),
                                          lambda: asyncio.ensure_future(self._auto()))

    async def _auto(self) -> None:
        if self._rec and self.on_auto_stop:
            await self.on_auto_stop()

    def _end_recording(self) -> Path | None:
        if self._auto_stop:
            self._auto_stop.cancel()
        rec, self._rec = self._rec, None
        self.states.listening = False
        self.bus.publish("listening", active=False)
        if rec is None:
            return None
        try:
            rec.send_signal(signal.SIGINT)  # lets pw-record finalise the WAV header
            rec.wait(timeout=3)
        except subprocess.TimeoutExpired:
            rec.kill()
        return self._file

    def cancel(self) -> None:
        f = self._end_recording()
        if f:
            f.unlink(missing_ok=True)
        self.states.settle()

    def _load_model(self):
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as e:
                raise VoiceUnavailable("faster-whisper is not installed (./wednesday setup --voice)") from e
            path = self.cfg.data_path(self.cfg.get("voice.stt_model_dir"))
            if not path.exists():
                raise VoiceUnavailable(f"speech model missing: {path}")
            # CPU int8: keeps the GPU's 4 GB entirely for the language model.
            self._model = WhisperModel(str(path), device="cpu", compute_type="int8",
                                       cpu_threads=int(self.cfg.get("voice.stt_threads")))
        self._model_used = time.time()
        return self._model

    @staticmethod
    def read_wav(path: Path) -> np.ndarray:
        """16 kHz mono s16 WAV → float32 [-1, 1]. Decoding ourselves avoids faster-whisper's PyAV
        path, which breaks with newer PyAV releases (unexpected 'metadata_errors' argument)."""
        with wave.open(str(path), "rb") as w:
            if w.getsampwidth() != 2:
                raise ValueError("expected 16-bit audio")
            data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
            if w.getnchannels() > 1:
                data = data.reshape(-1, w.getnchannels()).mean(axis=1).astype(np.int16)
            rate = w.getframerate()
        audio = data.astype(np.float32) / 32768.0
        if rate != 16000:
            n = int(len(audio) * 16000 / rate)
            audio = np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio).astype(np.float32)
        return audio

    def _transcribe(self, path: Path) -> str:
        model = self._load_model()
        segments, _info = model.transcribe(
            self.read_wav(path), language="en", beam_size=1, vad_filter=True, condition_on_previous_text=False,
            # Priming with the user's vocabulary fixes e.g. "ROS two workspace" → "artist who works pays".
            initial_prompt=self.cfg.get("voice.vocabulary") or None)
        return " ".join(s.text.strip() for s in segments).strip()

    async def stop_and_transcribe(self) -> str:
        f = self._end_recording()
        if not f:
            return ""
        self.states.set(AvatarState.THINKING, "curious", "transcribing")
        try:
            if not f.exists() or f.stat().st_size < 3200:  # < 0.1 s of audio
                return ""
            t0 = time.monotonic()
            text = await asyncio.to_thread(self._transcribe, f)
            log.info("transcribed %.1fs of audio in %.2fs", (f.stat().st_size - 44) / 32000, time.monotonic() - t0)
            return text
        finally:
            f.unlink(missing_ok=True)  # never keep raw audio
            if not self.states.busy:
                self.states.settle()

    def maybe_unload(self, idle_s: float = 600) -> None:
        if self._model is not None and not self._rec and time.time() - self._model_used > idle_s:
            self._model = None
            log.info("unloaded speech recognition model (idle)")


def cleanup_stale_audio(tmp_dir: Path) -> None:
    for f in tmp_dir.glob("ptt-*.wav"):
        try:
            f.unlink()
        except OSError:
            pass
    os.makedirs(tmp_dir, exist_ok=True)
