"""
WebSocket relay for remote desktops: noVNC in the page, VNC on the far side.

The browser cannot open a TCP connection, so noVNC speaks RFB over a
WebSocket and this relays it -- the job websockify usually does, here inside
the app so it shares the login, the ownership checks and the SSH tunnels.

A VNC profile reaches its server one of two ways:

- **Direct**: TCP to ``host:port``. VNC itself is unencrypted, so this is for
  a trusted LAN.
- **Through an SSH session** (``via_session_id``): the relay logs in over SSH
  with that saved profile -- its key, its pinned host key -- and opens a
  ``direct-tcpip`` channel to ``host:port`` *as seen from that machine*,
  usually ``localhost:5900`` on a server that only listens locally.

Either way the VNC login happens here (``rfb.server_handshake``) and noVNC is
offered a server that needs none, so the password stays on the server.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import threading
from typing import Any, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from services import rfb
from services import ssh as ssh_helpers
from services.auth import current_user_id
from services.db import fetch_session
from services.paths import session_caps
from services.pool import _persist_host_key
from services.terminal_ws import CLOSE_FORBIDDEN, CLOSE_NOT_FOUND, CLOSE_UNAUTHENTICATED, _same_origin

logger = logging.getLogger("iguanaxterm.vnc")

router = APIRouter()

_CONNECT_TIMEOUT = 15
_READ_SIZE = 64 * 1024


class _Target:
    """The VNC server's byte stream, and whatever has to be closed with it."""

    def __init__(self, stream: Any, client: Any = None) -> None:
        self.stream = stream      # socket.socket or paramiko.Channel
        self.client = client      # the SSHClient carrying a tunnel, if any

    def close(self) -> None:
        for closeable in (self.stream, self.client):
            try:
                if closeable is not None:
                    closeable.close()
            except Exception:
                pass


def open_target(profile: dict, user_id: int) -> _Target:
    """Connect to the VNC server, directly or through the chosen SSH session."""
    host = profile.get("host") or "localhost"
    port = int(profile.get("port") or 5900)
    via = int(profile.get("via_session_id") or 0)

    if not via:
        try:
            sock = socket.create_connection((host, port), timeout=_CONNECT_TIMEOUT)
        except OSError as exc:
            raise rfb.RFBError(f"Could not reach the VNC server at {host}:{port} ({exc})") from exc
        sock.settimeout(_CONNECT_TIMEOUT)
        return _Target(sock)

    tunnel = fetch_session(via, user_id)
    if tunnel is None or not session_caps(tunnel.get("type")).get("tunnel"):
        raise rfb.RFBError("The SSH session this desktop tunnels through no longer exists")
    client = ssh_helpers.connect(
        tunnel, on_learn_host_key=_persist_host_key(via, user_id)
    )
    try:
        channel = client.get_transport().open_channel(
            "direct-tcpip", (host, port), ("127.0.0.1", 0), timeout=_CONNECT_TIMEOUT
        )
    except Exception as exc:
        client.close()
        raise rfb.RFBError(
            f"{tunnel['name']} could not reach {host}:{port} -- is the VNC server "
            f"running there? ({exc})"
        ) from exc
    channel.settimeout(_CONNECT_TIMEOUT)
    return _Target(channel, client)


class _Inbox:
    """Reads exact byte counts from a WebSocket that delivers arbitrary frames."""

    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self.buffer = b""

    async def read(self, size: int) -> bytes:
        while len(self.buffer) < size:
            self.buffer += await self.ws.receive_bytes()
        data, self.buffer = self.buffer[:size], self.buffer[size:]
        return data


@router.websocket("/ws/vnc/{session_id}")
async def desktop(websocket: WebSocket, session_id: int) -> None:
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
    if profile is None or not session_caps(profile.get("type")).get("desktop"):
        await websocket.close(code=CLOSE_NOT_FOUND)
        return

    # noVNC asks for the "binary" subprotocol; a server that does not echo it
    # back makes the browser drop the socket.
    offered = websocket.scope.get("subprotocols") or []
    await websocket.accept(subprotocol="binary" if "binary" in offered else None)

    loop = asyncio.get_running_loop()
    target: Optional[_Target] = None

    def _login() -> _Target:
        opened = open_target(profile, user_id)
        try:
            rfb.server_handshake(opened.stream, profile.get("password") or "")
        except BaseException:
            opened.close()
            raise
        opened.stream.settimeout(None)   # from here on a quiet desktop is normal
        return opened

    inbox = _Inbox(websocket)
    try:
        # The browser side first: noVNC waits for a greeting before anything.
        await websocket.send_bytes(rfb.CLIENT_VERSION)
        await inbox.read(12)
        try:
            target = await loop.run_in_executor(None, _login)
        except (rfb.RFBError, ssh_helpers.HostKeyChanged, ssh_helpers.SSHUnavailable) as exc:
            await websocket.send_bytes(rfb.refuse(str(exc)))
            await websocket.close()
            return
        except Exception as exc:
            await websocket.send_bytes(rfb.refuse(f"Could not connect: {exc}"))
            await websocket.close()
            return
        await websocket.send_bytes(rfb.offer_no_auth())
        await inbox.read(1)                          # noVNC picks "None"
        await websocket.send_bytes(rfb.security_ok())
    except WebSocketDisconnect:
        if target is not None:
            target.close()
        return

    stop = threading.Event()

    def _pump() -> None:
        """Server -> browser, on a worker thread (the stream blocks)."""
        try:
            while not stop.is_set():
                data = target.stream.recv(_READ_SIZE)
                if not data:
                    break
                asyncio.run_coroutine_threadsafe(websocket.send_bytes(data), loop).result()
        except Exception as exc:
            logger.debug("vnc pump ended: %s", exc)
        finally:
            if not stop.is_set():
                asyncio.run_coroutine_threadsafe(websocket.close(), loop)

    pump = threading.Thread(target=_pump, name="vnc-pump", daemon=True)
    pump.start()

    try:
        if inbox.buffer:   # anything noVNC sent right behind its security choice
            await loop.run_in_executor(None, target.stream.sendall, inbox.buffer)
        while True:
            data = await websocket.receive_bytes()
            await loop.run_in_executor(None, target.stream.sendall, data)
    except (WebSocketDisconnect, RuntimeError):
        pass
    except Exception as exc:
        logger.debug("vnc relay ended: %s", exc)
    finally:
        stop.set()
        target.close()
