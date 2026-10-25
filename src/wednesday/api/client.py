"""Client for the local API, shared by the CLI and the desktop UI."""

from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

import aiohttp

from wednesday.config import Config


class BackendDown(RuntimeError):
    pass


class Client:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.base = cfg.base_url
        self._session: aiohttp.ClientSession | None = None

    def _token(self) -> str:
        try:
            return self.cfg.paths.token_file.read_text().strip()
        except OSError as e:
            raise BackendDown("no API token — is the backend running?") from e

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token()}"}

    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        return self._session

    async def close(self) -> None:
        if self._session:
            await self._session.close()

    async def request(self, method: str, path: str, payload: dict | None = None, timeout: float = 30) -> dict:
        s = await self.session()
        try:
            async with s.request(method, self.base + path, json=payload, headers=self._headers(),
                                 timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                data = await r.json(content_type=None)
                if r.status >= 400:
                    raise RuntimeError((data or {}).get("error") or f"HTTP {r.status}")
                return data
        except (aiohttp.ClientConnectorError, ConnectionRefusedError) as e:
            raise BackendDown("backend is not running") from e

    async def get(self, path: str, **kw) -> dict:
        return await self.request("GET", path, **kw)

    async def post(self, path: str, payload: dict | None = None, **kw) -> dict:
        return await self.request("POST", path, payload or {}, **kw)

    async def healthy(self) -> bool:
        s = await self.session()
        try:
            async with s.get(self.base + "/api/health", timeout=aiohttp.ClientTimeout(total=2)) as r:
                return r.status == 200
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            return False

    async def events(self) -> AsyncIterator[dict]:
        """Yields backend events until the connection drops (raises BackendDown)."""
        s = await self.session()
        try:
            async with s.ws_connect(self.base + "/api/events", headers=self._headers(), heartbeat=20,
                                    timeout=aiohttp.ClientWSTimeout(ws_close=5)) as ws:
                self.ws = ws
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        yield json.loads(msg.data)
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
        except (aiohttp.ClientError, OSError) as e:
            raise BackendDown(str(e)) from e
        finally:
            self.ws = None
        raise BackendDown("event stream closed")

    ws = None

    async def send_ws(self, data: dict) -> None:
        if self.ws is not None and not self.ws.closed:
            await self.ws.send_str(json.dumps(data))
