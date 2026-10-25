"""Owns the single local llama-server process: start, health, stream, unload.

Design notes
- Exactly one model process at a time. "Unloading" = terminating llama-server, which is the
  only way to guarantee every byte of VRAM goes back to the desktop/other apps.
- The server binds to 127.0.0.1 and gets a fresh random --api-key per launch, so other local
  processes (or a malicious web page poking localhost) cannot use it.
- We pick the NVIDIA Vulkan device *by name*: on this laptop the Intel iGPU enumerates first.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator

import aiohttp

from wednesday.config import Config
from wednesday.core.event_bus import EventBus

log = logging.getLogger(__name__)


class ModelError(RuntimeError):
    pass


@dataclass
class ModelStatus:
    state: str = "unloaded"          # unloaded | loading | ready | error | unloading
    profile: str = ""
    device: str = ""
    detail: str = ""
    loaded_at: float = 0.0
    load_seconds: float = 0.0
    last_used: float = 0.0
    pid: int | None = None
    stats: dict = field(default_factory=dict)  # last generation timings from llama-server

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


@dataclass
class StreamDelta:
    """One normalised chunk from the OpenAI-compatible stream."""
    content: str = ""
    reasoning: str = ""
    tool_calls: list | None = None
    finish_reason: str | None = None
    timings: dict | None = None


class ModelManager:
    def __init__(self, cfg: Config, bus: EventBus):
        self.cfg = cfg
        self.bus = bus
        self.status = ModelStatus(profile=cfg.get("model.active"))
        self._proc: subprocess.Popen | None = None
        self._api_key = ""
        self._lock = asyncio.Lock()
        self._session: aiohttp.ClientSession | None = None
        self._idle_task: asyncio.Task | None = None

    # ------------------------------------------------------------------ paths & device
    @property
    def runtime_dir(self) -> Path:
        return self.cfg.data_path(self.cfg.get("model.runtime_dir"))

    @property
    def server_bin(self) -> Path:
        return self.runtime_dir / "llama-server"

    @property
    def model_path(self) -> Path:
        return self.cfg.data_path("models") / self.cfg.model_profile["file"]

    @property
    def port(self) -> int:
        return int(self.cfg.get("model.port"))

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def list_devices(self) -> list[tuple[str, str]]:
        """[(id, description)] as reported by llama-server --list-devices."""
        try:
            out = subprocess.run(
                [str(self.server_bin), "--list-devices"], capture_output=True, text=True,
                timeout=20, env=self._env(),
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning("could not list devices: %s", e)
            return []
        devices = []
        for line in (out.stdout + out.stderr).splitlines():
            m = re.match(r"\s+(\w+\d+):\s+(.*)", line)
            if m:
                devices.append((m.group(1), m.group(2)))
        return devices

    def pick_device(self) -> str:
        want = str(self.cfg.get("model.device", "auto"))
        if want != "auto":
            return want
        for dev_id, desc in self.list_devices():
            if "nvidia" in desc.lower():
                return dev_id
        return "none"  # CPU fallback

    def _env(self) -> dict:
        env = dict(os.environ)
        env["LD_LIBRARY_PATH"] = f"{self.runtime_dir}:{env.get('LD_LIBRARY_PATH', '')}"
        return env

    # ------------------------------------------------------------------ lifecycle
    def _set(self, state: str, detail: str = "") -> None:
        self.status.state = state
        self.status.detail = detail
        self.bus.publish("model", **self.status.as_dict())

    def check_files(self) -> list[str]:
        problems = []
        if not self.server_bin.exists():
            problems.append(f"llama-server not found at {self.server_bin} (run: ./wednesday setup)")
        if not self.model_path.exists():
            problems.append(f"model file missing: {self.model_path} (run: ./wednesday setup)")
        return problems

    async def _port_in_use(self) -> bool:
        try:
            _, w = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", self.port), 1)
            w.close()
            return True
        except (OSError, asyncio.TimeoutError):
            return False

    async def ensure_loaded(self) -> None:
        if self.status.state == "ready" and self._proc and self._proc.poll() is None:
            return
        async with self._lock:
            if self.status.state == "ready" and self._proc and self._proc.poll() is None:
                return
            await self._start()

    @property
    def pid_file(self) -> Path:
        return self.cfg.paths.state_dir / "llama-server.pid"

    def reclaim_orphan(self) -> bool:
        """Kill a llama-server left behind by a crashed Wednesday — but only if the pidfile
        points at a process running *our* binary. Never touches anything else."""
        try:
            pid = int(self.pid_file.read_text().strip())
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")[0].decode()
        except (OSError, ValueError):
            self.pid_file.unlink(missing_ok=True)
            return False
        if cmdline != str(self.server_bin):
            self.pid_file.unlink(missing_ok=True)
            return False
        log.warning("reclaiming orphaned llama-server pid %s", pid)
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        for _ in range(30):
            if not Path(f"/proc/{pid}").exists():
                break
            time.sleep(0.1)
        self.pid_file.unlink(missing_ok=True)
        return True

    async def _start(self) -> None:
        problems = self.check_files()
        if problems:
            self._set("error", "; ".join(problems))
            raise ModelError(problems[0])
        await asyncio.to_thread(self.reclaim_orphan)
        if await self._port_in_use():
            # Never kill something we didn't start — it might be the user's own server.
            msg = f"port {self.port} is already in use by another process; change model.port"
            self._set("error", msg)
            raise ModelError(msg)

        device = await asyncio.to_thread(self.pick_device)
        self.status.device = device
        self.status.profile = self.cfg.get("model.active")
        self._api_key = secrets.token_urlsafe(24)
        ngl = str(self.cfg.get("model.gpu_layers")) if device != "none" else "0"
        args = [
            str(self.server_bin),
            "--model", str(self.model_path),
            "--alias", self.status.profile,
            "--host", "127.0.0.1",
            "--port", str(self.port),
            "--api-key", self._api_key,
            "--ctx-size", str(self.cfg.get("model.ctx_size")),
            "--threads", str(self.cfg.get("model.threads")),
            "--device", device,
            "--gpu-layers", ngl,
            "--parallel", "1",            # one conversation slot: no silent VRAM doubling
            "--cache-ram", "256",         # default is 8 GiB of host RAM for prompt caching
            "--flash-attn", "auto",
            "--jinja",
            "--reasoning-format", "deepseek",  # thinking arrives as reasoning_content
            # Small models over-think: cap the thought budget, then nudge them to answer.
            "--reasoning-budget", str(self.cfg.get("model.reasoning_budget", 512)),
            "--reasoning-budget-message", "\nOkay, I have enough. Time to answer clearly and concisely.\n",
            "--no-reasoning-preserve",    # old thoughts are not worth their context tokens
            "--no-webui",
            "--offline",                  # never let the runtime reach the network
        ]
        log_file = self.cfg.paths.log_dir / "llama-server.log"
        self._set("loading", f"{self.model_path.name} on {device}")
        t0 = time.monotonic()
        with open(log_file, "ab") as lf:
            lf.write(f"\n==== {time.ctime()} starting {self.model_path.name} on {device}\n".encode())
            self._proc = subprocess.Popen(
                args, stdout=lf, stderr=subprocess.STDOUT, env=self._env(),
                start_new_session=True,  # own process group so we can stop it cleanly
                preexec_fn=_die_with_parent,
            )
        self.status.pid = self._proc.pid
        self.pid_file.write_text(str(self._proc.pid))
        deadline = t0 + float(self.cfg.get("model.startup_timeout_s"))
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                tail = _tail(log_file, 12)
                self._proc = None
                self._set("error", f"llama-server exited during startup:\n{tail}")
                raise ModelError("llama-server exited during startup; see logs/llama-server.log")
            if await self._healthy():
                self.status.load_seconds = round(time.monotonic() - t0, 2)
                self.status.loaded_at = self.status.last_used = time.time()
                self._set("ready", f"{self.model_path.name} on {device}")
                log.info("model ready in %.1fs on %s", self.status.load_seconds, device)
                self._start_idle_watch()
                return
            await asyncio.sleep(0.25)
        await self.unload()
        self._set("error", "model startup timed out")
        raise ModelError("model startup timed out")

    async def _healthy(self) -> bool:
        try:
            s = await self._http()
            async with s.get(f"{self.url}/health", timeout=aiohttp.ClientTimeout(total=2)) as r:
                return r.status == 200
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return False

    async def unload(self, reason: str = "") -> None:
        proc, self._proc = self._proc, None
        if proc and proc.poll() is None:
            self._set("unloading", reason)
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            for _ in range(40):
                if proc.poll() is not None:
                    break
                await asyncio.sleep(0.1)
            else:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await asyncio.to_thread(proc.wait)
        self.status.pid = None
        self.pid_file.unlink(missing_ok=True)
        self._set("unloaded", reason)

    async def close(self) -> None:
        if self._idle_task:
            self._idle_task.cancel()
        await self.unload("shutdown")
        if self._session:
            await self._session.close()

    def _start_idle_watch(self) -> None:
        minutes = float(self.cfg.get("model.idle_unload_minutes") or 0)
        if minutes <= 0 or (self._idle_task and not self._idle_task.done()):
            return

        async def watch() -> None:
            while self._proc is not None:
                await asyncio.sleep(30)
                if self.busy:
                    continue
                if time.time() - self.status.last_used > minutes * 60:
                    log.info("unloading idle model after %.0f min", minutes)
                    await self.unload("idle")
                    return

        self._idle_task = asyncio.create_task(watch())

    busy = False  # set by the orchestrator while a generation is active

    # ------------------------------------------------------------------ inference
    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # force_close: llama-server closes streamed connections itself; reusing a pooled
            # keep-alive socket afterwards fails with ServerDisconnectedError.
            self._session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(force_close=True))
        return self._session

    async def stream_chat(
        self, messages: list[dict], tools: list[dict] | None = None, *,
        thinking: bool = False, max_tokens: int | None = None, temperature: float | None = None,
        stop: list[str] | None = None,
    ) -> AsyncIterator[StreamDelta]:
        """Stream a chat completion. Cancelling the consuming task closes the HTTP stream,
        which makes llama-server abort generation for that slot."""
        await self.ensure_loaded()
        sampling = dict((self.cfg.model_profile.get("sampling") or {}).get("think" if thinking else "fast") or
                        {"temperature": 0.7, "top_p": 0.8})
        if temperature is not None:
            sampling["temperature"] = temperature
        body: dict = {
            "model": self.status.profile,
            "messages": messages,
            "stream": True,
            "max_tokens": max_tokens or int(self.cfg.get("model.max_tokens")),
            "timings_per_token": False,
            **sampling,
        }
        if stop:
            body["stop"] = stop
        if self.cfg.model_profile.get("supports_thinking"):
            body["chat_template_kwargs"] = {"enable_thinking": bool(thinking)}
        if tools:
            body["tools"] = tools
            body["parallel_tool_calls"] = False
        s = await self._http()
        timeout = aiohttp.ClientTimeout(total=float(self.cfg.get("model.request_timeout_s")), sock_read=60)
        self.status.last_used = time.time()
        async with s.post(
            f"{self.url}/v1/chat/completions", json=body, timeout=timeout,
            headers={"Authorization": f"Bearer {self._api_key}"},
        ) as resp:
            if resp.status != 200:
                text = (await resp.text())[:500]
                raise ModelError(f"llama-server HTTP {resp.status}: {text}")
            async for raw in resp.content:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if "error" in chunk:
                    raise ModelError(str(chunk["error"])[:500])
                timings = chunk.get("timings")
                if timings:
                    self.status.stats = {
                        "prompt_tokens": timings.get("prompt_n"),
                        "prompt_tps": round(timings.get("prompt_per_second") or 0, 1),
                        "gen_tokens": timings.get("predicted_n"),
                        "gen_tps": round(timings.get("predicted_per_second") or 0, 1),
                    }
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    yield StreamDelta(
                        content=delta.get("content") or "",
                        reasoning=delta.get("reasoning_content") or "",
                        tool_calls=delta.get("tool_calls"),
                        finish_reason=choice.get("finish_reason"),
                        timings=timings,
                    )
        self.status.last_used = time.time()

    async def complete(self, messages: list[dict], *, max_tokens: int = 400) -> str:
        """Non-tool, non-thinking helper (summaries, titles)."""
        parts = []
        async for d in self.stream_chat(messages, thinking=False, max_tokens=max_tokens, temperature=0.3):
            parts.append(d.content)
        return "".join(parts).strip()


def _die_with_parent() -> None:
    """Runs in the child before exec: if the backend dies (even SIGKILL), the kernel sends
    llama-server SIGTERM, so a crashed backend can never strand ~1.5 GB of VRAM."""
    try:
        import ctypes
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG = 1
    except OSError:
        pass


def _tail(path: Path, n: int) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-n:])
    except OSError:
        return ""
