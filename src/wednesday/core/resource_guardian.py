"""Resource Guardian: measures what the machine actually exposes, enforces admission policy.

Rules of honesty
- A metric we cannot read is reported as unavailable (None), never guessed.
- Thermal limits come from the hardware: the NVIDIA driver's "GPU Slowdown Temp" and the
  kernel coretemp "high" threshold, minus a configurable margin. No invented universal numbers.
- We never kill or renice processes we did not start.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field

import psutil

from wednesday.config import Config
from wednesday.core.event_bus import EventBus

log = logging.getLogger(__name__)


@dataclass
class Snapshot:
    ts: float = 0.0
    cpu_percent: float | None = None
    ram_total_mb: int | None = None
    ram_available_mb: int | None = None
    process_rss_mb: int | None = None          # backend + llama-server + ui (ours only)
    gpu_name: str | None = None
    gpu_util: float | None = None
    vram_used_mb: int | None = None
    vram_total_mb: int | None = None
    gpu_temp_c: float | None = None
    gpu_slowdown_c: float | None = None
    cpu_temp_c: float | None = None
    cpu_high_c: float | None = None
    active_inference: int = 0
    queue_length: int = 0
    avatar_fps: float | None = None
    pressure: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


class ResourceGuardian:
    def __init__(self, cfg: Config, bus: EventBus):
        self.cfg = cfg
        self.bus = bus
        self.snap = Snapshot()
        self.active = False            # orchestrator flips this while working → faster polling
        self.active_inference = 0
        self.queue_length = 0
        self.avatar_fps: float | None = None
        self.our_pids: set[int] = set()
        self._nvsmi = shutil.which("nvidia-smi")
        self._gpu_static: dict | None = None
        self._task: asyncio.Task | None = None
        self._hot_since: float | None = None

    # ------------------------------------------------------------------ sampling
    def _gpu_static_info(self) -> dict:
        if self._gpu_static is not None:
            return self._gpu_static
        info: dict = {}
        if self._nvsmi:
            try:
                q = subprocess.run([self._nvsmi, "-q", "-d", "TEMPERATURE"], capture_output=True, text=True, timeout=5)
                m = re.search(r"GPU Slowdown Temp\s*:\s*([\d.]+)\s*C", q.stdout)
                if m:
                    info["slowdown"] = float(m.group(1))
            except (OSError, subprocess.TimeoutExpired):
                pass
        self._gpu_static = info
        return info

    def _sample_gpu(self, s: Snapshot) -> None:
        if not self._nvsmi:
            return
        try:
            out = subprocess.run(
                [self._nvsmi, "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                 "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5,
            ).stdout.strip().splitlines()
        except (OSError, subprocess.TimeoutExpired):
            return
        if not out:
            return
        parts = [p.strip() for p in out[0].split(",")]
        if len(parts) == 5:
            s.gpu_name = parts[0]
            s.gpu_util = _num(parts[1])
            s.vram_used_mb = _int(parts[2])
            s.vram_total_mb = _int(parts[3])
            s.gpu_temp_c = _num(parts[4])
        s.gpu_slowdown_c = self._gpu_static_info().get("slowdown")

    @staticmethod
    def _sample_cpu_temp(s: Snapshot) -> None:
        try:
            temps = psutil.sensors_temperatures()
        except (AttributeError, OSError):
            return
        for entry in temps.get("coretemp", []):
            if entry.label.startswith("Package"):
                s.cpu_temp_c = entry.current
                s.cpu_high_c = entry.high or entry.critical
                return

    def _rss_ours(self) -> int | None:
        total = 0
        for pid in list(self.our_pids):
            try:
                total += psutil.Process(pid).memory_info().rss
            except psutil.Error:
                self.our_pids.discard(pid)
        return total // (1 << 20) if total else None

    def sample(self) -> Snapshot:
        s = Snapshot(ts=time.time())
        s.cpu_percent = psutil.cpu_percent(interval=None)
        vm = psutil.virtual_memory()
        s.ram_total_mb = vm.total // (1 << 20)
        s.ram_available_mb = vm.available // (1 << 20)
        s.process_rss_mb = self._rss_ours()
        self._sample_gpu(s)
        self._sample_cpu_temp(s)
        s.active_inference = self.active_inference
        s.queue_length = self.queue_length
        s.avatar_fps = self.avatar_fps
        s.pressure = self._pressure(s)
        self.snap = s
        return s

    # ------------------------------------------------------------------ policy
    def _pressure(self, s: Snapshot) -> list[str]:
        p = []
        if s.ram_available_mb is not None and s.ram_available_mb < int(self.cfg.get("resources.min_free_ram_mb")):
            p.append(f"low memory: {s.ram_available_mb} MB available")
        if s.gpu_temp_c is not None and s.gpu_slowdown_c:
            limit = s.gpu_slowdown_c - float(self.cfg.get("resources.gpu_temp_margin_c"))
            if s.gpu_temp_c >= limit:
                p.append(f"GPU at {s.gpu_temp_c:.0f}°C (driver slowdown point {s.gpu_slowdown_c:.0f}°C)")
        if s.cpu_temp_c is not None and s.cpu_high_c:
            limit = s.cpu_high_c - float(self.cfg.get("resources.cpu_temp_margin_c"))
            if s.cpu_temp_c >= limit:
                p.append(f"CPU at {s.cpu_temp_c:.0f}°C (kernel 'high' threshold {s.cpu_high_c:.0f}°C)")
        return p

    def admit(self, kind: str = "inference") -> tuple[bool, str]:
        """May a new expensive job start? Uses a fresh sample, not a stale one."""
        s = self.sample()
        if self.queue_length >= int(self.cfg.get("resources.max_queue")):
            return False, f"I'm already juggling {self.queue_length} requests. One at a time, please."
        if s.pressure:
            return False, "Resource guardian says no: " + "; ".join(s.pressure)
        return True, ""

    # ------------------------------------------------------------------ loop
    def start(self) -> None:
        psutil.cpu_percent(interval=None)  # prime the CPU counter
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        while True:
            try:
                s = await asyncio.to_thread(self.sample)
                self.bus.publish("resources", **s.as_dict())
                if s.pressure:
                    if self._hot_since is None:
                        self._hot_since = time.time()
                    elif time.time() - self._hot_since > 60:
                        self.bus.publish("warning", message="Sustained pressure: " + "; ".join(s.pressure))
                        self._hot_since = time.time()
                else:
                    self._hot_since = None
            except Exception:  # never let monitoring crash the backend
                log.exception("resource sample failed")
            idle = self.cfg.get("resources.poll_idle_s")
            busy = self.cfg.get("resources.poll_active_s")
            await asyncio.sleep(float(busy if self.active else idle))

    def report(self) -> dict:
        """Which protections are live, and which can't work on this machine."""
        s = self.sample()
        g = self._gpu_static_info()
        return {
            "snapshot": s.as_dict(),
            "protections": {
                "single_model_process": "active",
                "single_inference_slot": "active (llama-server --parallel 1, backend semaphore)",
                "bounded_queue": f"active (max {self.cfg.get('resources.max_queue')})",
                "low_ram_admission": f"active (< {self.cfg.get('resources.min_free_ram_mb')} MB available → refuse)",
                "gpu_thermal_admission": (f"active (driver slowdown {g['slowdown']:.0f}°C − margin "
                                          f"{self.cfg.get('resources.gpu_temp_margin_c')}°C)")
                if g.get("slowdown") else "UNAVAILABLE (nvidia-smi does not report a slowdown temperature)",
                "cpu_thermal_admission": (f"active (coretemp high {s.cpu_high_c:.0f}°C − margin "
                                          f"{self.cfg.get('resources.cpu_temp_margin_c')}°C)")
                if s.cpu_high_c else "UNAVAILABLE (no coretemp package sensor)",
                "idle_model_unload": f"active ({self.cfg.get('model.idle_unload_minutes')} min)"
                if self.cfg.get("model.idle_unload_minutes") else "disabled in config",
                "adaptive_avatar_fps": "active (UI reports fps; idle/sleep frame rates in config)",
                "kills_unrelated_processes": "never",
            },
        }


def _num(x: str) -> float | None:
    try:
        return float(x)
    except ValueError:
        return None


def _int(x: str) -> int | None:
    v = _num(x)
    return int(v) if v is not None else None
