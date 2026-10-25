"""Local HTTP + WebSocket API (documented in docs/PROTOCOL.md).

Access control, in order:
  1. bound to 127.0.0.1 by default (server.host) — not reachable from the network;
  2. Host header must be 127.0.0.1:<port> or localhost:<port> — defeats DNS rebinding;
  3. any request carrying an Origin header is rejected — browsers always send one for
     cross-site fetches and WebSockets, our own clients never do, so web pages can't drive it;
  4. bearer token from ~/.local/state/wednesday/token (0600, regenerated every start).
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import secrets
from typing import TYPE_CHECKING

from aiohttp import WSMsgType, web

if TYPE_CHECKING:
    from wednesday.backend import Backend

log = logging.getLogger(__name__)

PUBLIC = {"/api/health"}


def write_token(path) -> str:
    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(token)
    tmp.chmod(0o600)
    tmp.replace(path)
    return token


def make_app(backend: "Backend", token: str) -> web.Application:
    port = backend.cfg.get("server.port")
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}

    @web.middleware
    async def guard(request: web.Request, handler):
        if request.host not in allowed_hosts:
            return web.json_response({"error": "bad host"}, status=403)
        if "Origin" in request.headers:
            return web.json_response({"error": "browser origins are not allowed"}, status=403)
        if request.path not in PUBLIC:
            auth = request.headers.get("Authorization", "")
            given = auth[7:] if auth.startswith("Bearer ") else ""
            if not hmac.compare_digest(given.encode(), token.encode()):
                return web.json_response({"error": "unauthorized"}, status=401)
        try:
            return await handler(request)
        except (ValueError, KeyError, TypeError) as e:
            return web.json_response({"error": str(e)}, status=400)
        except RuntimeError as e:
            return web.json_response({"error": str(e)}, status=409)

    app = web.Application(middlewares=[guard], client_max_size=2 * 1024 * 1024)
    b = backend

    async def body(request: web.Request) -> dict:
        if not request.can_read_body:
            return {}
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError("JSON object expected")
        return data

    # ------------------------------------------------------------------ basics
    async def health(_):
        return web.json_response({"ok": True, "name": "wednesday"})

    async def status(_):
        return web.json_response(b.status())

    async def diagnostics(_):
        return web.json_response(await b.diagnostics())

    # ------------------------------------------------------------------ chat
    async def chat(request):
        d = await body(request)
        job = b.orchestrator.submit(str(d.get("text", "")), str(d.get("session") or "desktop"),
                                    d.get("think"), str(d.get("source") or "panel"))
        return web.json_response({"job": job.id})

    async def cancel(request):
        d = await body(request)
        return web.json_response({"cancelled": b.orchestrator.cancel(d.get("job"))})

    async def stop_all(_):
        if b.listener:
            b.listener.cancel()
        return web.json_response(await b.orchestrator.emergency_stop())

    async def approve(request):
        d = await body(request)
        ok = b.orchestrator.resolve_approval(str(d["id"]), bool(d.get("approved")))
        return web.json_response({"ok": ok})

    async def reset(request):
        d = await body(request)
        b.orchestrator.reset(str(d.get("session") or "desktop"))
        return web.json_response({"ok": True})

    async def history(request):
        conv = b.orchestrator.conversations.get(request.query.get("session", "desktop"))
        msgs = [] if conv is None else [
            {"role": m["role"], "content": m.get("content") or ""} for m in conv.messages
            if m["role"] in ("user", "assistant") and (m.get("content") or "").strip()]
        return web.json_response({"messages": msgs[-60:], "summary": conv.summary if conv else ""})

    async def settings(request):
        d = await body(request)
        return web.json_response(b.orchestrator.update_settings(**{
            k: d.get(k) for k in ("online", "speak", "thinking", "memory")}))

    # ------------------------------------------------------------------ memory & tasks
    async def memory_list(request):
        if not b.memory:
            return web.json_response({"items": [], "enabled": False})
        q = request.query.get("q", "").strip()
        items = b.memory.search(q, 50) if q else b.memory.list(200)
        return web.json_response({"items": items, "enabled": b.orchestrator.settings["memory"]})

    async def memory_add(request):
        d = await body(request)
        if not b.memory:
            raise RuntimeError("memory is disabled")
        mid = b.memory.add(str(d["content"]), str(d.get("kind") or "note"), str(d.get("project") or ""),
                           pinned=bool(d.get("pinned")), source="user")
        return web.json_response({"id": mid})

    async def memory_delete(request):
        if not b.memory:
            raise RuntimeError("memory is disabled")
        mid = request.match_info["id"]
        if mid == "all":
            return web.json_response({"deleted": b.memory.clear()})
        return web.json_response({"deleted": b.memory.delete(int(mid))})

    async def tasks(_):
        return web.json_response({"tasks": b.memory.list_tasks() if b.memory else []})

    async def task_delete(request):
        return web.json_response({"deleted": bool(b.memory and b.memory.delete_task(int(request.match_info["id"])))})

    # ------------------------------------------------------------------ model
    async def model_load(_):
        asyncio.create_task(b.load_model())
        return web.json_response({"ok": True})

    async def model_unload(_):
        if b.orchestrator.current:
            raise RuntimeError("busy — stop the current request first")
        await b.model.unload("requested")
        return web.json_response({"ok": True})

    # ------------------------------------------------------------------ voice
    async def voice_ptt(request):
        d = await body(request)
        action = d.get("action", "toggle")
        return web.json_response(await b.push_to_talk(action))

    async def voice_say(request):
        d = await body(request)
        if not b.speaker:
            raise RuntimeError("voice output unavailable")
        await b.speaker.say(str(d["text"])[:2000])
        return web.json_response({"ok": True})

    async def voice_stop(_):
        if b.speaker:
            b.speaker.stop()
        return web.json_response({"ok": True})

    # ------------------------------------------------------------------ ui control
    async def ui_command(request):
        d = await body(request)
        cmd = str(d.get("command", ""))
        if cmd not in ("toggle_avatar", "toggle_panel", "show_panel", "ghost", "quit_ui"):
            raise ValueError("unknown ui command")
        b.bus.publish("ui", command=cmd)
        return web.json_response({"ok": True, "listeners": b.bus.subscriber_count})

    async def shutdown(_):
        asyncio.get_running_loop().call_later(0.2, b.request_shutdown)
        return web.json_response({"ok": True})

    # ------------------------------------------------------------------ events
    async def events(request):
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=64 * 1024)
        await ws.prepare(request)
        q = b.bus.subscribe()
        await ws.send_json({"type": "hello", "status": b.status()})

        async def pump():
            while True:
                ev = await q.get()
                await ws.send_str(json.dumps(ev, default=str))

        sender = asyncio.create_task(pump())
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                if data.get("type") == "ui_metrics":
                    fps = data.get("fps")
                    b.guardian.avatar_fps = float(fps) if isinstance(fps, (int, float)) else None
                    if pid := data.get("pid"):
                        b.guardian.our_pids.add(int(pid))
        finally:
            sender.cancel()
            b.bus.unsubscribe(q)
        return ws

    r = app.router
    r.add_get("/api/health", health)
    r.add_get("/api/status", status)
    r.add_get("/api/diagnostics", diagnostics)
    r.add_post("/api/chat", chat)
    r.add_post("/api/cancel", cancel)
    r.add_post("/api/stop", stop_all)
    r.add_post("/api/approve", approve)
    r.add_post("/api/reset", reset)
    r.add_get("/api/history", history)
    r.add_post("/api/settings", settings)
    r.add_get("/api/memory", memory_list)
    r.add_post("/api/memory", memory_add)
    r.add_delete("/api/memory/{id}", memory_delete)
    r.add_get("/api/tasks", tasks)
    r.add_delete("/api/tasks/{id}", task_delete)
    r.add_post("/api/model/load", model_load)
    r.add_post("/api/model/unload", model_unload)
    r.add_post("/api/voice/ptt", voice_ptt)
    r.add_post("/api/voice/say", voice_say)
    r.add_post("/api/voice/stop", voice_stop)
    r.add_post("/api/ui", ui_command)
    r.add_post("/api/shutdown", shutdown)
    r.add_get("/api/events", events)
    return app
