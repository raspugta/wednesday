"""Runs already-approved commands without a shell.

Pipelines are wired up process-to-process (no /bin/sh), each command runs in its own process
group so cancellation kills the whole pipeline, secrets are stripped from the environment,
and output is capped.
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
from pathlib import Path

MAX_OUTPUT = 12_000
SECRET_ENV = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH|COOKIE|SESSION)", re.I)

_running: set[asyncio.subprocess.Process] = set()


def clean_env() -> dict:
    env = {k: v for k, v in os.environ.items() if not SECRET_ENV.search(k)}
    env["GIT_PAGER"] = "cat"
    env["PAGER"] = "cat"
    env["TERM"] = "dumb"
    env.setdefault("LANG", "C.UTF-8")
    return env


async def run_pipeline(stages: list[list[str]], cwd: Path, timeout: float = 30.0) -> dict:
    procs: list[asyncio.subprocess.Process] = []
    env = clean_env()
    prev_read = None
    try:
        for i, argv in enumerate(stages):
            last = i == len(stages) - 1
            if not last:
                r, w = os.pipe()
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=str(cwd), env=env,
                stdin=prev_read if prev_read is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE if last else w,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            if prev_read is not None:
                os.close(prev_read)
            if not last:
                os.close(w)
                prev_read = r
            procs.append(proc)
            _running.add(proc)
        last = procs[-1]
        try:
            out, err = await asyncio.wait_for(last.communicate(), timeout)
            timed_out = False
        except asyncio.TimeoutError:
            timed_out = True
            out, err = b"", b""
        for p in procs:
            if p.returncode is None:
                _kill(p)
                await p.wait()
        stderr_all = err.decode(errors="replace")
        for p in procs[:-1]:
            if p.stderr:
                stderr_all = (await p.stderr.read()).decode(errors="replace") + stderr_all
        stdout = out.decode(errors="replace")
        truncated = len(stdout) > MAX_OUTPUT
        return {
            "exit_code": None if timed_out else last.returncode,
            "timed_out": timed_out,
            "stdout": stdout[:MAX_OUTPUT] + ("\n[... output truncated]" if truncated else ""),
            "stderr": stderr_all[-3000:],
        }
    except FileNotFoundError as e:
        return {"exit_code": 127, "timed_out": False, "stdout": "", "stderr": f"command not found: {e.filename}"}
    except asyncio.CancelledError:
        for p in procs:
            _kill(p)
        raise
    finally:
        for p in procs:
            _running.discard(p)


def _kill(p: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def kill_all() -> int:
    """Emergency stop: kill every command Wednesday started. Never touches other processes."""
    n = 0
    for p in list(_running):
        if p.returncode is None:
            _kill(p)
            n += 1
    return n


def format_result(command: str, res: dict) -> str:
    head = f"$ {command}\n"
    if res["timed_out"]:
        head += "[timed out — process killed]\n"
    else:
        head += f"[exit code {res['exit_code']}]\n"
    body = res["stdout"].rstrip()
    if res["stderr"].strip():
        body += ("\n" if body else "") + "[stderr]\n" + res["stderr"].strip()
    return head + (body or "(no output)")
