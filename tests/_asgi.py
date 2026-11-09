"""
A tiny ASGI driver for route tests, so they need no extra package.

Starlette's TestClient wants httpx2, which the app doesn't otherwise use.
These helpers speak ASGI directly: one HTTP request, or a WebSocket you can
send to and receive from.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Optional


class Closed(Exception):
    """The server closed the WebSocket (or refused it)."""

    def __init__(self, code: int, reason: str = "") -> None:
        super().__init__(f"closed {code} {reason}")
        self.code = code
        self.reason = reason


def _scope(kind: str, path: str, headers: Optional[dict]) -> dict:
    raw_path, _, query = path.partition("?")
    return {
        "type": kind,
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "scheme": "ws" if kind == "websocket" else "http",
        "method": "GET",
        "path": raw_path,
        "raw_path": raw_path.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 80),
        "subprotocols": [],
    }


def http(app, method: str, path: str, headers: Optional[dict] = None, body: bytes = b"") -> tuple[int, Any]:
    """One request. Returns (status, parsed JSON body or raw bytes)."""

    async def _go():
        scope = _scope("http", path, headers)
        scope["method"] = method.upper()
        sent = {"done": False}

        async def receive():
            if sent["done"]:
                await asyncio.sleep(3600)
            sent["done"] = True
            return {"type": "http.request", "body": body, "more_body": False}

        status, chunks = 0, []

        async def send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))

        await app(scope, receive, send)
        data = b"".join(chunks)
        try:
            return status, json.loads(data)
        except ValueError:
            return status, data

    return asyncio.run(_go())


class WebSocket:
    """Drive one WebSocket route from a coroutine: ``await ws.receive()`` etc."""

    def __init__(self, app, path: str, headers: Optional[dict] = None) -> None:
        self.app = app
        self.scope = _scope("websocket", path, headers)
        self.to_app: asyncio.Queue = asyncio.Queue()
        self.from_app: asyncio.Queue = asyncio.Queue()
        self.task: Optional[asyncio.Task] = None

    async def __aenter__(self) -> "WebSocket":
        await self.to_app.put({"type": "websocket.connect"})
        self.task = asyncio.create_task(self.app(self.scope, self.to_app.get, self.from_app.put))
        first = await self._next()
        if first["type"] == "websocket.close":
            raise Closed(first.get("code", 1000), first.get("reason") or "")
        assert first["type"] == "websocket.accept", first
        return self

    async def __aexit__(self, *exc) -> None:
        await self.to_app.put({"type": "websocket.disconnect", "code": 1000})
        if self.task is not None:
            try:
                await asyncio.wait_for(self.task, 5)
            except Exception:
                self.task.cancel()

    async def _next(self, timeout: float = 10) -> dict:
        getter = asyncio.ensure_future(self.from_app.get())
        done, _ = await asyncio.wait({getter, self.task}, timeout=timeout,
                                     return_when=asyncio.FIRST_COMPLETED)
        if getter in done:
            return getter.result()
        getter.cancel()
        if self.task in done:
            self.task.result()   # re-raise anything the route raised
            raise Closed(1006, "the route returned without closing")
        raise TimeoutError("nothing from the route")

    async def receive(self, timeout: float = 10, raw: bool = False) -> Any:
        """The next frame: JSON-decoded text, raw text with ``raw``, or bytes."""
        message = await self._next(timeout)
        if message["type"] == "websocket.close":
            raise Closed(message.get("code", 1000), message.get("reason") or "")
        if message.get("text") is not None:
            return message["text"] if raw else json.loads(message["text"])
        return message.get("bytes")

    async def send_bytes(self, data: bytes) -> None:
        await self.to_app.put({"type": "websocket.receive", "bytes": data})

    async def send_json(self, data: Any) -> None:
        await self.to_app.put({"type": "websocket.receive", "text": json.dumps(data)})


def ws_refusal_code(app, path: str) -> int:
    """The close code of a WebSocket the route refuses before accepting."""

    async def _go():
        try:
            async with WebSocket(app, path):
                return 0
        except Closed as exc:
            return exc.code

    return asyncio.run(_go())
