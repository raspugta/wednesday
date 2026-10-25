"""Tool registry: OpenAI-style schemas + validated, permission-gated executors.

Flow for every call the model makes:
    validate args against schema → Permissions decision → (ask human if APPROVAL) → execute
    → result string (always returned to the model, including BLOCKED/DENIED explanations)

The model never gets a raw shell, never sees config files, and cannot reach this code except
through the structured call path below.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from wednesday.agent.permissions import Decision, Level, Permissions
from wednesday.config import Config
from wednesday.core.event_bus import EventBus
from wednesday.core.resource_guardian import ResourceGuardian
from wednesday.memory.store import KINDS, MemoryStore
from wednesday.tools import files, shell, web

log = logging.getLogger(__name__)

ApprovalFn = Callable[[dict], Awaitable[bool]]


@dataclass
class ToolContext:
    cfg: Config
    perms: Permissions
    bus: EventBus
    memory: MemoryStore | None
    guardian: ResourceGuardian
    request_approval: ApprovalFn
    is_online: Callable[[], bool]


class ToolError(Exception):
    pass


def _fn(name: str, desc: str, props: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required}}}


S = {"type": "string"}
I = {"type": "integer"}
B = {"type": "boolean"}

SCHEMAS: dict[str, dict] = {
    "read_file": _fn("read_file", "Read a text file (with line numbers).",
                     {"path": S, "start_line": I, "max_lines": I}, ["path"]),
    "list_dir": _fn("list_dir", "List a directory.", {"path": S, "show_hidden": B}, ["path"]),
    "search_files": _fn("search_files", "Find files by name glob under a directory, optionally only those containing some text.",
                        {"directory": S, "name_glob": S, "contains": S}, ["directory"]),
    "write_file": _fn("write_file", "Create or overwrite a text file (needs user approval).",
                      {"path": S, "content": S, "append": B}, ["path", "content"]),
    "run_command": _fn("run_command",
                       "Run ONE shell command (pipes allowed, no ; && > or $()). Read-only commands run immediately; "
                       "others need user approval; dangerous ones are blocked.",
                       {"command": S, "cwd": S, "timeout_s": I}, ["command"]),
    "web_search": _fn("web_search", "Search the internet. Returns titles, links and snippets.", {"query": S}, ["query"]),
    "read_webpage": _fn("read_webpage", "Fetch a public web page and return its main text.", {"url": S}, ["url"]),
    "remember": _fn("remember", "Save something worth keeping long-term (preference, project fact, decision).",
                    {"content": S, "kind": {"type": "string", "enum": list(KINDS)}, "project": S}, ["content"]),
    "recall": _fn("recall", "Search long-term memory.", {"query": S}, ["query"]),
    "plan_task": _fn("plan_task", "Break a multi-step goal into a tracked checklist.",
                     {"title": S, "steps": {"type": "array", "items": S}}, ["title", "steps"]),
    "update_task": _fn("update_task", "Mark a step of a tracked task.",
                       {"task_id": I, "step": I,
                        "status": {"type": "string", "enum": ["in_progress", "done", "skipped", "failed"]}},
                       ["task_id", "step", "status"]),
    "system_status": _fn("system_status", "Current CPU/RAM/GPU/VRAM/temperature readings of this laptop.", {}, []),
    "open_in_browser": _fn("open_in_browser", "Open a URL or local file in the user's default app (needs approval).",
                           {"target": S}, ["target"]),
}

ONLINE_TOOLS = {"web_search", "read_webpage"}


def tool_schemas(online: bool, memory_enabled: bool) -> list[dict]:
    out = []
    for name, schema in SCHEMAS.items():
        if name in ONLINE_TOOLS and not online:
            continue
        if name in ("remember", "recall") and not memory_enabled:
            continue
        out.append(schema)
    return out


def _validate(name: str, args: Any) -> dict:
    if name not in SCHEMAS:
        raise ToolError(f"unknown tool '{name}'")
    if not isinstance(args, dict):
        raise ToolError("arguments must be a JSON object")
    spec = SCHEMAS[name]["function"]["parameters"]
    for req in spec["required"]:
        if req not in args or args[req] in (None, ""):
            raise ToolError(f"missing required argument '{req}'")
    clean = {}
    for key, val in args.items():
        if key not in spec["properties"]:
            continue  # ignore hallucinated extras rather than failing
        t = spec["properties"][key]["type"]
        try:
            if t == "string":
                val = str(val)
                if len(val) > 200_000:
                    raise ToolError(f"'{key}' is too long")
            elif t == "integer":
                val = int(val)
            elif t == "boolean":
                val = val if isinstance(val, bool) else str(val).lower() in ("1", "true", "yes")
            elif t == "array":
                if isinstance(val, str):
                    val = [s for s in val.split("\n") if s.strip()]
                val = [str(v) for v in val]
        except (TypeError, ValueError) as e:
            raise ToolError(f"'{key}' should be a {t}") from e
        enum = spec["properties"][key].get("enum")
        if enum and val not in enum:
            raise ToolError(f"'{key}' must be one of {enum}")
        clean[key] = val
    return clean


def parse_arguments(raw: str) -> dict:
    raw = (raw or "").strip() or "{}"
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ToolError(f"arguments were not valid JSON: {e.msg}") from e


def describe_call(name: str, args: dict) -> str:
    """One-line human description for the UI and approval cards."""
    a = args
    return {
        "read_file": lambda: f"read {a.get('path')}",
        "list_dir": lambda: f"list {a.get('path')}",
        "search_files": lambda: f"search {a.get('directory')} for {a.get('name_glob') or '*'} {a.get('contains') or ''}".strip(),
        "write_file": lambda: f"{'append to' if a.get('append') else 'write'} {a.get('path')} ({len(a.get('content', ''))} chars)",
        "run_command": lambda: f"run `{a.get('command')}`" + (f" in {a.get('cwd')}" if a.get("cwd") else ""),
        "web_search": lambda: f"search the web for “{a.get('query')}”",
        "read_webpage": lambda: f"read {a.get('url')}",
        "remember": lambda: f"remember: {a.get('content', '')[:80]}",
        "recall": lambda: f"recall “{a.get('query')}”",
        "plan_task": lambda: f"plan “{a.get('title')}” ({len(a.get('steps', []))} steps)",
        "update_task": lambda: f"task {a.get('task_id')} step {a.get('step')} → {a.get('status')}",
        "system_status": lambda: "check system status",
        "open_in_browser": lambda: f"open {a.get('target')}",
    }.get(name, lambda: name)()


class ToolExecutor:
    def __init__(self, ctx: ToolContext):
        self.ctx = ctx

    async def _gate(self, name: str, args: dict, decision: Decision, details: str = "") -> str | None:
        """Returns an error string for the model if the action may not proceed."""
        if decision.level == Level.BLOCKED:
            self.ctx.bus.publish("tool_blocked", tool=name, summary=describe_call(name, args), reason=decision.reason)
            return f"BLOCKED by Wednesday's permission policy: {decision.reason}"
        if decision.level == Level.APPROVAL:
            ok = await self.ctx.request_approval({
                "tool": name, "summary": describe_call(name, args), "reason": decision.reason,
                "destructive": decision.destructive, "details": details[:4000],
            })
            if not ok:
                return "DENIED: the user did not approve this action. Do not retry it; ask what they'd prefer."
        return None

    async def execute(self, name: str, raw_args: str) -> str:
        try:
            args = _validate(name, parse_arguments(raw_args))
            if name in ONLINE_TOOLS and not self.ctx.is_online():
                return "ERROR: Wednesday is in offline mode, so web tools are unavailable."
            handler = getattr(self, f"_t_{name}")
            return await handler(args)
        except ToolError as e:
            return f"ERROR: {e}"
        except web.WebError as e:
            return f"ERROR (web): {e}"
        except (OSError, ValueError, KeyError) as e:
            return f"ERROR: {type(e).__name__}: {e}"

    # ------------------------------------------------------------------ files
    async def _t_read_file(self, a: dict) -> str:
        d = self.ctx.perms.check_read(a["path"])
        if err := await self._gate("read_file", a, d):
            return err
        path = self.ctx.perms.resolve(a["path"])
        return await asyncio.to_thread(files.read_file, path, a.get("start_line", 1), a.get("max_lines", 200))

    async def _t_list_dir(self, a: dict) -> str:
        d = self.ctx.perms.check_read(a["path"])
        if err := await self._gate("list_dir", a, d):
            return err
        return await asyncio.to_thread(files.list_dir, self.ctx.perms.resolve(a["path"]), a.get("show_hidden", False))

    async def _t_search_files(self, a: dict) -> str:
        d = self.ctx.perms.check_read(a["directory"])
        if err := await self._gate("search_files", a, d):
            return err
        perms = self.ctx.perms
        return await asyncio.to_thread(
            files.search_files, perms.resolve(a["directory"]), a.get("name_glob") or "*", a.get("contains", ""),
            40, lambda p: perms.check_read(str(p)).allowed)

    async def _t_write_file(self, a: dict) -> str:
        d = self.ctx.perms.check_write(a["path"])
        path = self.ctx.perms.resolve(a["path"])
        preview = a["content"] if len(a["content"]) < 3000 else a["content"][:3000] + "\n[...]"
        if path.exists() and not a.get("append"):
            preview = f"⚠ OVERWRITES existing file ({path.stat().st_size} bytes)\n\n" + preview
        if err := await self._gate("write_file", a, d, preview):
            return err
        return await asyncio.to_thread(files.write_file, path, a["content"], a.get("append", False))

    # ------------------------------------------------------------------ commands
    @staticmethod
    def normalize_command(cmd: str, cwd: str | None) -> tuple[str, str | None, bool]:
        """Translate two idioms small models always produce into their structured equivalents:
        'cd DIR && CMD' → cwd=DIR, and a trailing '2>/dev/null' → discard stderr. The result
        still goes through the full permission check; nothing else is rewritten."""
        quiet = False
        m = re.match(r"^\s*cd\s+(\S+)\s*&&\s*(.+)$", cmd)
        if m and "&&" not in m.group(2):
            cwd, cmd = m.group(1), m.group(2)
        if re.search(r"\s2>\s*/dev/null\s*$", cmd):
            cmd, quiet = re.sub(r"\s2>\s*/dev/null\s*$", "", cmd), True
        return cmd.strip(), cwd, quiet

    async def _t_run_command(self, a: dict) -> str:
        cmd, cwd, quiet = self.normalize_command(a["command"].strip(), a.get("cwd"))
        a = {**a, "command": cmd, "cwd": cwd}
        decision, stages = self.ctx.perms.check_command(cmd, a.get("cwd"))
        if err := await self._gate("run_command", a, decision, cmd):
            return err
        cwd = self.ctx.perms.resolve(a.get("cwd") or "~")
        timeout = max(1, min(int(a.get("timeout_s") or 30), 300))
        res = await shell.run_pipeline(stages, cwd, timeout)
        if quiet:
            res["stderr"] = ""
        return shell.format_result(cmd, res)

    async def _t_open_in_browser(self, a: dict) -> str:
        target = a["target"].strip()
        if target.startswith(("http://", "https://")):
            d = Decision(Level.APPROVAL, "opens a page in your browser")
        else:
            d = self.ctx.perms.check_read(target)
            if d.level == Level.READ:
                d = Decision(Level.APPROVAL, "opens a file in its default application")
            target = str(self.ctx.perms.resolve(target))
        if err := await self._gate("open_in_browser", a, d, target):
            return err
        opener = shutil.which("xdg-open")
        if not opener:
            return "ERROR: xdg-open is not installed"
        subprocess.Popen([opener, target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, env=shell.clean_env())
        return f"Opened {target} (handed to xdg-open; I can't see the window)."

    # ------------------------------------------------------------------ web
    async def _t_web_search(self, a: dict) -> str:
        cfg = self.ctx.cfg
        self.ctx.bus.publish("network", direction="out", what=f"web search: {a['query'][:80]}")
        results = await web.search(a["query"], provider=cfg.get("web.provider"),
                                   max_results=int(cfg.get("web.max_results")),
                                   timeout=float(cfg.get("web.timeout_s")), searxng_url=cfg.get("web.searxng_url"))
        if not results:
            return "No results."
        body = "\n\n".join(f"{i}. {r.title}\n   {r.url}\n   {r.snippet[:300]}" for i, r in enumerate(results, 1))
        return web.wrap_untrusted("web search", body) + \
            "\n(Use read_webpage on the most relevant link if snippets aren't enough. Cite sources.)"

    async def _t_read_webpage(self, a: dict) -> str:
        cfg = self.ctx.cfg
        self.ctx.bus.publish("network", direction="out", what=f"fetch {a['url'][:100]}")
        page = await web.fetch_page(a["url"], timeout=float(cfg.get("web.timeout_s")),
                                    max_chars=int(cfg.get("web.max_page_chars")))
        more = "\n[page truncated]" if page["truncated"] else ""
        return web.wrap_untrusted(f"{page['title']} <{page['url']}>", page["text"] + more)

    # ------------------------------------------------------------------ memory & tasks
    def _mem(self) -> MemoryStore:
        if not self.ctx.memory:
            raise ToolError("persistent memory is disabled")
        return self.ctx.memory

    async def _t_remember(self, a: dict) -> str:
        mem_id = self._mem().add(a["content"], a.get("kind", "note"), a.get("project", ""), source="assistant")
        self.ctx.bus.publish("memory_saved", id=mem_id, content=a["content"][:200], kind=a.get("kind", "note"))
        return f"Saved to memory (#{mem_id}). The user can review or delete it in the Memory tab."

    async def _t_recall(self, a: dict) -> str:
        hits = self._mem().search(a["query"], 6)
        if not hits:
            return "Nothing relevant in memory."
        return "\n".join(f"#{h['id']} [{h['kind']}{'/' + h['project'] if h['project'] else ''}] {h['content']}" for h in hits)

    async def _t_plan_task(self, a: dict) -> str:
        t = self._mem().create_task(a["title"], a["steps"]) if self.ctx.memory else None
        if not t:
            return "ERROR: tasks need memory enabled"
        self.ctx.bus.publish("task", task=t)
        steps = "\n".join(f"{i}. {s['text']}" for i, s in enumerate(t["steps"], 1))
        return f"Task #{t['id']} created:\n{steps}"

    async def _t_update_task(self, a: dict) -> str:
        t = self._mem().update_step(a["task_id"], a["step"], a["status"])
        self.ctx.bus.publish("task", task=t)
        return f"Task #{t['id']} is now {t['status']}."

    # ------------------------------------------------------------------ system
    async def _t_system_status(self, a: dict) -> str:
        s = await asyncio.to_thread(self.ctx.guardian.sample)
        def f(v, unit=""):
            return "unavailable" if v is None else f"{v:.0f}{unit}" if isinstance(v, float) else f"{v}{unit}"
        return (f"CPU {f(s.cpu_percent, '%')}, CPU temp {f(s.cpu_temp_c, '°C')}; "
                f"RAM available {f(s.ram_available_mb, ' MB')} of {f(s.ram_total_mb, ' MB')}; "
                f"GPU {s.gpu_name or 'unavailable'}: util {f(s.gpu_util, '%')}, VRAM {f(s.vram_used_mb)}/{f(s.vram_total_mb, ' MB')}, "
                f"temp {f(s.gpu_temp_c, '°C')}; pressure: {', '.join(s.pressure) or 'none'}")
