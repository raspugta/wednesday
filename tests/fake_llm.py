"""A scripted stand-in for llama-server's OpenAI-compatible streaming endpoint."""
import asyncio
import json

from aiohttp import web


class FakeLLM:
    def __init__(self):
        self.script: list = []   # each item: {"content": str} or {"tool": (name, args)} or {"slow": n}
        self.requests: list = []

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/health", self.health)
        app.router.add_post("/v1/chat/completions", self.chat)
        return app

    async def health(self, request):
        return web.json_response({"status": "ok"})

    async def chat(self, request: web.Request):
        body = await request.json()
        self.requests.append(body)
        step = self.script.pop(0) if self.script else {"content": "[calm] ok"}
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)

        async def send(delta, finish=None):
            chunk = {"choices": [{"delta": delta, "finish_reason": finish}]}
            await resp.write(f"data: {json.dumps(chunk)}\n\n".encode())

        if "slow" in step:
            for i in range(step["slow"]):
                await send({"content": f"word{i} "})
                await asyncio.sleep(0.05)
        elif "tool" in step:
            name, args = step["tool"]
            await send({"tool_calls": [{"index": 0, "id": "call1", "type": "function",
                                        "function": {"name": name, "arguments": ""}}]})
            await send({"tool_calls": [{"index": 0, "function": {"arguments": json.dumps(args)}}]}, "tool_calls")
        else:
            for piece in [step["content"][i:i + 5] for i in range(0, len(step["content"]), 5)]:
                await send({"content": piece})
            await send({}, "stop")
        await resp.write(b"data: [DONE]\n\n")
        return resp
