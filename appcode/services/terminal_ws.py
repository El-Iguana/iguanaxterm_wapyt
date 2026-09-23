"""
WebSocket relay for SSH and Telnet terminals.

Mounted on the pytincture app in ``service.py``. Authentication comes from the
pytincture session cookie, not a token in the query string — the original put a
full session credential in every proxy log, browser history entry and Referer
header.

Neither transport polls. The original ticked at 50 Hz for SSH
(``await asyncio.sleep(0.02)`` around ``recv_ready``) and 10 Hz for Telnet,
which cost up to 20 ms of added echo latency per keystroke and a constant wakeup
per open session. Here SSH blocks in a reader thread and Telnet uses asyncio
streams; both idle at zero cost and deliver as soon as bytes land.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any, Optional
from urllib.parse import urlsplit

import paramiko
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from services import ssh as ssh_helpers
from services import telnet as telnet_proto
from services.auth import current_user_id
from services.db import fetch_session, get_db

logger = logging.getLogger("iguanaxterm.terminal")

router = APIRouter()

CLOSE_UNAUTHENTICATED = 4401
CLOSE_NOT_FOUND = 4404
CLOSE_FORBIDDEN = 4403

_READ_SIZE = 32768


async def _send(ws: WebSocket, kind: str, data: Any = "") -> None:
    await ws.send_text(json.dumps({"type": kind, "data": data}))


def _same_origin(ws: WebSocket) -> bool:
    """
    Reject a cross-origin WebSocket handshake.

    WebSockets are not subject to CORS, and pytincture's Origin and
    Fetch-Metadata checks cover BFF calls but not a raw socket route. The
    session cookie defaults to SameSite=lax, which blocks the obvious attack,
    but a route that hands out an interactive shell should not rely on a
    cookie attribute that a deployment can override.
    """
    origin = ws.headers.get("origin")
    if not origin:
        # Non-browser clients send no Origin. Browsers always do, so an absent
        # header cannot be a hostile page.
        return True
    host = ws.headers.get("host", "")
    return urlsplit(origin).netloc == host


def _persist_host_key(session_id: int, user_id: int):
    def _store(serialized: str) -> None:
        with get_db() as conn:
            conn.execute(
                "UPDATE sessions SET host_key = ? "
                "WHERE id = ? AND user_id = ? AND host_key = ''",
                (serialized, session_id, user_id),
            )

    return _store


@router.websocket("/ws/terminal/{session_id}")
async def terminal(websocket: WebSocket, session_id: int) -> None:
    if not _same_origin(websocket):
        await websocket.close(code=CLOSE_FORBIDDEN)
        return

    session_data = getattr(websocket, "session", None)
    user = session_data.get("user") if session_data else None
    user_id = current_user_id(user if isinstance(user, dict) else None)
    if not user_id:
        await websocket.close(code=CLOSE_UNAUTHENTICATED)
        return

    profile = fetch_session(int(session_id), user_id)
    if profile is None:
        await websocket.close(code=CLOSE_NOT_FOUND)
        return

    await websocket.accept()

    if profile.get("type") == "telnet":
        await _relay_telnet(websocket, profile)
    else:
        await _relay_ssh(websocket, profile, user_id)


# ── SSH ───────────────────────────────────────────────────────────────────────


async def _relay_ssh(ws: WebSocket, profile: dict, user_id: int) -> None:
    loop = asyncio.get_running_loop()

    def _open() -> tuple[paramiko.SSHClient, paramiko.Channel]:
        client = ssh_helpers.connect(
            profile, on_learn_host_key=_persist_host_key(profile["id"], user_id)
        )
        channel = client.invoke_shell(term="xterm-256color", width=80, height=24)
        # Blocking reads: this channel is consumed by a dedicated thread. The
        # dhxpyt rewrite set settimeout(0) here, which makes recv() raise
        # immediately whenever no data is buffered — the read loop exited on
        # the first empty poll and every session died the moment it opened.
        channel.settimeout(None)
        return client, channel

    try:
        client, channel = await loop.run_in_executor(None, _open)
    except ssh_helpers.HostKeyChanged as exc:
        await _send(ws, "error", str(exc))
        await ws.close(code=CLOSE_FORBIDDEN)
        return
    except Exception as exc:
        await _send(ws, "error", f"Connection failed: {exc}")
        await ws.close()
        return

    await _send(ws, "connected", profile["host"])

    stop = threading.Event()

    def _reader() -> None:
        """Blocking reads on a worker thread, handed to the loop as they land."""
        try:
            while not stop.is_set():
                data = channel.recv(_READ_SIZE)
                if not data:
                    break
                text = data.decode("utf-8", errors="replace")
                asyncio.run_coroutine_threadsafe(_send(ws, "output", text), loop)
        except Exception:
            pass
        finally:
            if not stop.is_set():
                asyncio.run_coroutine_threadsafe(
                    _send(ws, "disconnected", "Remote session ended"), loop
                )

    reader = threading.Thread(target=_reader, name="ssh-reader", daemon=True)
    reader.start()

    try:
        while True:
            message = json.loads(await ws.receive_text())
            kind = message.get("type")
            if kind == "input":
                channel.sendall(message.get("data", "").encode())
            elif kind == "resize":
                channel.resize_pty(
                    width=int(message.get("cols", 80)),
                    height=int(message.get("rows", 24)),
                )
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.debug("ssh relay ended: %s", exc)
    finally:
        stop.set()
        try:
            channel.close()
        except Exception:
            pass
        client.close()
        reader.join(timeout=2)


# ── Telnet ────────────────────────────────────────────────────────────────────


async def _relay_telnet(ws: WebSocket, profile: dict) -> None:
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(profile["host"], int(profile["port"] or 23)),
            timeout=15,
        )
    except Exception as exc:
        await _send(ws, "error", f"Telnet connection failed: {exc}")
        await ws.close()
        return

    writer.write(telnet_proto.initial_negotiation())
    await writer.drain()
    await _send(ws, "connected", profile["host"])

    async def _pump() -> None:
        buffer = b""
        try:
            while True:
                chunk = await reader.read(_READ_SIZE)
                if not chunk:
                    await _send(ws, "disconnected", "Connection closed")
                    break
                display, reply, buffer = telnet_proto.process(buffer + chunk)
                if reply:
                    writer.write(reply)
                    await writer.drain()
                if display:
                    await _send(
                        ws, "output", display.decode("utf-8", errors="replace")
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("telnet pump ended: %s", exc)

    pump = asyncio.create_task(_pump())

    try:
        while True:
            message = json.loads(await ws.receive_text())
            kind = message.get("type")
            if kind == "input":
                writer.write(message.get("data", "").encode())
                await writer.drain()
            elif kind == "resize":
                # The original ignored resize for Telnet entirely. NAWS is
                # exactly the Telnet mechanism for it.
                writer.write(
                    telnet_proto.naws(
                        int(message.get("cols", 80)), int(message.get("rows", 24))
                    )
                )
                await writer.drain()
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.debug("telnet relay ended: %s", exc)
    finally:
        pump.cancel()
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
