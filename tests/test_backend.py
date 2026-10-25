"""Backend integration tests against a scripted fake model server (no GPU needed)."""
import asyncio
import socket

import aiohttp
import pytest
from aiohttp import web

from fake_llm import FakeLLM
from wednesday.backend import Backend
from wednesday.api.server import make_app


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
async def stack(tmp_cfg, tmp_path):
    fake = FakeLLM()
    runner = web.AppRunner(fake.app())
    await runner.setup()
    llm_port = free_port()
    await web.TCPSite(runner, "127.0.0.1", llm_port).start()

    cfg = tmp_cfg
    cfg.set("voice.enabled", False)
    cfg.set("model.preload", False)
    cfg.set("web.online", False)
    cfg.set("server.port", free_port())
    cfg.permissions["write_roots"] = [str(tmp_path / "ws")]
    cfg.permissions["read_roots"] = [str(tmp_path)]
    (tmp_path / "ws").mkdir()
    b = Backend(cfg)
    # Point the model manager at the fake server instead of launching llama-server.
    b.model.status.state = "ready"
    b.model._api_key = "test"

    class FakeProc:
        pid = 1

        def poll(self):
            return None
    b.model._proc = FakeProc()
    b.model.status.profile = "fake"
    type(b.model).url = property(lambda self: f"http://127.0.0.1:{llm_port}")
    token = "t0ken"
    api = web.AppRunner(make_app(b, token))
    await api.setup()
    await web.TCPSite(api, "127.0.0.1", cfg.get("server.port")).start()
    b.orchestrator.start()
    base = f"http://127.0.0.1:{cfg.get('server.port')}"
    session = aiohttp.ClientSession(headers={"Authorization": f"Bearer {token}"})
    yield b, fake, base, session, tmp_path
    await session.close()
    await b.orchestrator.stop()
    if b.model._session:
        await b.model._session.close()
    b.model._proc = None
    await api.cleanup()
    await runner.cleanup()


async def collect(b, until=("reply_done", "error", "cancelled"), timeout=10):
    q = b.bus.subscribe()
    events = []
    async def go():
        while True:
            ev = await q.get()
            events.append(ev)
            if ev["type"] in until:
                return
    await asyncio.wait_for(go(), timeout)
    b.bus.unsubscribe(q)
    return events


# --------------------------------------------------------------------------- API security
async def test_requires_token(stack):
    b, fake, base, session, _ = stack
    async with aiohttp.ClientSession() as anon:
        async with anon.get(base + "/api/status") as r:
            assert r.status == 401
        async with anon.get(base + "/api/health") as r:
            assert r.status == 200


async def test_rejects_browser_origin(stack):
    b, fake, base, session, _ = stack
    async with session.get(base + "/api/status", headers={"Origin": "https://evil.example"}) as r:
        assert r.status == 403


async def test_rejects_foreign_host_header(stack):
    b, fake, base, session, _ = stack
    async with session.get(base + "/api/status", headers={"Host": "evil.example"}) as r:
        assert r.status == 403


# --------------------------------------------------------------------------- conversation
async def test_chat_streams_and_parses_mood(stack):
    b, fake, base, session, _ = stack
    fake.script = [{"content": "[amused] Hello there, human."}]
    waiter = asyncio.create_task(collect(b))
    await asyncio.sleep(0.05)
    async with session.post(base + "/api/chat", json={"text": "hi"}) as r:
        assert r.status == 200
    events = await waiter
    tokens = "".join(e["text"] for e in events if e["type"] == "token")
    done = events[-1]
    assert done["type"] == "reply_done" and done["mood"] == "amused"
    assert tokens == "Hello there, human." and "[amused]" not in tokens
    states = [e["state"] for e in events if e["type"] == "state"]
    assert "THINKING" in states and "SPEAKING" in states


async def test_system_prompt_prefix_is_stable(stack):
    """KV-cache reuse depends on the system prompt not changing between turns."""
    b, fake, base, session, _ = stack
    fake.script = [{"content": "[calm] one"}, {"content": "[calm] two"}]
    for text in ("first", "second"):
        w = asyncio.create_task(collect(b))
        await asyncio.sleep(0.05)
        await session.post(base + "/api/chat", json={"text": text})
        await w
    s1, s2 = fake.requests[0]["messages"][0], fake.requests[1]["messages"][0]
    assert s1 == s2
    assert "[context" in fake.requests[1]["messages"][-1]["content"]
    # the stored history stays clean (no context block)
    assert all("[context" not in m["content"] for m in b.orchestrator.conversations["desktop"].messages
               if m["role"] == "user")


async def test_tool_call_blocked_secret(stack):
    b, fake, base, session, tmp = stack
    fake.script = [{"tool": ("read_file", {"path": "~/.ssh/id_rsa"})}, {"content": "[sheepish] Blocked."}]
    w = asyncio.create_task(collect(b))
    await asyncio.sleep(0.05)
    await session.post(base + "/api/chat", json={"text": "read the file ~/.ssh/id_rsa"})
    events = await w
    end = [e for e in events if e["type"] == "tool_end"][0]
    assert not end["ok"] and "BLOCKED" in end["preview"]
    tool_msg = fake.requests[1]["messages"][-1]
    assert tool_msg["role"] == "tool" and "BLOCKED" in tool_msg["content"]


async def test_write_needs_approval_and_deny_works(stack):
    b, fake, base, session, tmp = stack
    target = tmp / "ws" / "x.txt"
    fake.script = [{"tool": ("write_file", {"path": str(target), "content": "hi"})}, {"content": "[calm] ok"}]
    q = b.bus.subscribe()
    await session.post(base + "/api/chat", json={"text": "write a file x.txt"})
    while True:
        ev = await asyncio.wait_for(q.get(), 5)
        if ev["type"] == "approval_request":
            break
    assert ev["tool"] == "write_file" and ev["job"]
    async with session.post(base + "/api/approve", json={"id": ev["id"], "approved": False}) as r:
        assert (await r.json())["ok"]
    while ev["type"] != "reply_done":
        ev = await asyncio.wait_for(q.get(), 5)
    assert not target.exists()
    assert "DENIED" in fake.requests[1]["messages"][-1]["content"]


async def test_write_approved(stack):
    b, fake, base, session, tmp = stack
    target = tmp / "ws" / "y.txt"
    fake.script = [{"tool": ("write_file", {"path": str(target), "content": "hello"})}, {"content": "[proud] done"}]
    q = b.bus.subscribe()
    await session.post(base + "/api/chat", json={"text": "write a file y.txt"})
    while True:
        ev = await asyncio.wait_for(q.get(), 5)
        if ev["type"] == "approval_request":
            await session.post(base + "/api/approve", json={"id": ev["id"], "approved": True})
        if ev["type"] == "reply_done":
            break
    assert target.read_text() == "hello"


async def test_harmless_command_runs(stack):
    b, fake, base, session, tmp = stack
    fake.script = [{"tool": ("run_command", {"command": "echo wednesday-was-here", "cwd": str(tmp)})},
                   {"content": "[calm] It printed."}]
    w = asyncio.create_task(collect(b))
    await asyncio.sleep(0.05)
    await session.post(base + "/api/chat", json={"text": "run echo"})
    events = await w
    end = [e for e in events if e["type"] == "tool_end"][0]
    assert end["ok"] and "wednesday-was-here" in end["preview"]


async def test_cancel_generation(stack):
    b, fake, base, session, _ = stack
    fake.script = [{"slow": 200}]
    q = b.bus.subscribe()
    await session.post(base + "/api/chat", json={"text": "write an essay"})
    while (await asyncio.wait_for(q.get(), 5))["type"] != "token":
        pass
    async with session.post(base + "/api/cancel", json={}) as r:
        assert (await r.json())["cancelled"]
    ev = await asyncio.wait_for(q.get(), 5)
    while ev["type"] not in ("cancelled", "reply_done"):
        ev = await asyncio.wait_for(q.get(), 5)
    assert ev["type"] == "cancelled"
    assert b.orchestrator.current is None


async def test_offline_mode_hides_web_tools(stack):
    b, fake, base, session, _ = stack
    fake.script = [{"content": "[calm] offline"}]
    w = asyncio.create_task(collect(b))
    await asyncio.sleep(0.05)
    await session.post(base + "/api/chat", json={"text": "search the web for news"})
    await w
    names = [t["function"]["name"] for t in fake.requests[0].get("tools", [])]
    assert "web_search" not in names and "read_file" in names
    assert await b.orchestrator.tools.execute("web_search", '{"query": "x"}') == \
        "ERROR: Wednesday is in offline mode, so web tools are unavailable."


async def test_memory_api_roundtrip(stack):
    b, fake, base, session, _ = stack
    async with session.post(base + "/api/memory", json={"content": "I use ROS 2 Jazzy", "kind": "preference"}) as r:
        mid = (await r.json())["id"]
    async with session.get(base + "/api/memory?q=jazzy") as r:
        assert (await r.json())["items"][0]["id"] == mid
    async with session.delete(base + f"/api/memory/{mid}") as r:
        assert (await r.json())["deleted"]


async def test_emergency_stop(stack):
    b, fake, base, session, _ = stack
    fake.script = [{"slow": 200}]
    q = b.bus.subscribe()
    await session.post(base + "/api/chat", json={"text": "long"})
    while (await asyncio.wait_for(q.get(), 5))["type"] != "token":
        pass
    async with session.post(base + "/api/stop", json={}) as r:
        res = await r.json()
    assert res["cancelled"] is True
