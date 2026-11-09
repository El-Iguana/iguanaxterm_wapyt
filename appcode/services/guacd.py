"""
guacd, Apache Guacamole's proxy, which does the RDP work (#55, #57).

``GANXTERM_GUACD`` is its address, ``host:port``. Unset means **RDP is off**:
the type is hidden, saving one is refused, and the route answers 404. That is
the native Windows install, since guacd is Linux-only.

guacd has **no authentication**: anyone who reaches its port can make it
connect anywhere. compose.yaml never publishes it, and on host networking it
is bound to 127.0.0.1. A plain module (not a BFF one).
"""
from __future__ import annotations

import os
import socket
from typing import Optional

from services import guac

_CHECK_TIMEOUT = 2.0
_CONNECT_TIMEOUT = 10.0


def address() -> Optional[tuple[str, int]]:
    """``(host, port)`` from GANXTERM_GUACD, or None when RDP is off."""
    value = os.environ.get("GANXTERM_GUACD", "").strip()
    if not value:
        return None
    host, _, port = value.rpartition(":")
    if not host:
        host, port = value, "4822"
    try:
        return host.strip("[]"), int(port)
    except ValueError:
        return None


def enabled() -> bool:
    return address() is not None


def status() -> str:
    """
    ``"ready"``, ``"unreachable"`` or ``"off"``, for About and ``manage.py
    health``. "ready" means something at that address answered "select rdp"
    with "args", i.e. it really is a guacd that can do RDP.
    """
    where = address()
    if where is None:
        return "off"
    try:
        with socket.create_connection(where, timeout=_CHECK_TIMEOUT) as sock:
            sock.settimeout(_CHECK_TIMEOUT)
            sock.sendall(guac.encode("select", "rdp").encode())
            splitter = guac.ByteSplitter()
            for _ in range(20):
                data = sock.recv(65536)
                if not data:
                    break
                for _raw, elements in splitter.feed(data):
                    if elements[0] == "args":
                        return "ready"
                    if elements[0] == "error":
                        return "unreachable"
    except (OSError, guac.ProtocolError):
        pass
    return "unreachable"


class GuacdError(Exception):
    """guacd refused or ended the handshake; ``message`` is for a person."""

    def __init__(self, message: str, code: str = "512") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def local_address_towards(host: str) -> str:
    """
    This machine's address on the route to guacd: where guacd can reach a
    listener of ours (#60). 127.0.0.1 with host networking; the container's
    own address on a compose network. No packet is sent.
    """
    try:
        target = socket.getaddrinfo(host, 4822, socket.AF_INET, socket.SOCK_DGRAM)[0][4][0]
    except OSError:
        return "127.0.0.1"
    if target.startswith("127."):
        return "127.0.0.1"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect((target, 4822))
            return probe.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def handshake(params: dict, width: int, height: int, dpi: int,
              timezone: str = "UTC") -> tuple[socket.socket, str, guac.ByteSplitter, list[str]]:
    """
    Open guacd and make the RDP connection, IguanaXterm side (#57).

    1. ``select rdp``; 2. read ``args``; 3. ``size``, ``audio``, ``video``,
    ``image``, ``timezone``; 4. ``connect`` with the values in the order
    guacd asked for them; 5. wait for ``ready``.

    Returns the socket, the connection id, the splitter (it may hold bytes
    guacd sent right behind ``ready``) and those instructions, already split.
    The login travels only on this socket, never to the browser.
    """
    where = address()
    if where is None:
        raise GuacdError("RDP isn't set up on this server.")
    try:
        sock = socket.create_connection(where, timeout=_CONNECT_TIMEOUT)
    except OSError as exc:
        raise GuacdError(f"The RDP service (guacd) isn't reachable: {exc}") from exc
    sock.settimeout(_CONNECT_TIMEOUT)
    splitter = guac.ByteSplitter()

    def _expect(opcode: str) -> tuple[list[str], list[str]]:
        while True:
            data = sock.recv(65536)
            if not data:
                raise GuacdError("The RDP service closed the connection.")
            ready = splitter.feed(data)
            for index, (_raw, elements) in enumerate(ready):
                if elements[0] == opcode:
                    return elements, [raw for raw, _e in ready[index + 1:]]
                if elements[0] == "error":
                    raise GuacdError(
                        guac.status_message(elements[2] if len(elements) > 2 else None,
                                            elements[1] if len(elements) > 1 else ""),
                        elements[2] if len(elements) > 2 else "512",
                    )

    try:
        sock.sendall(guac.encode("select", "rdp").encode())
        args, _ = _expect("args")
        for instruction in (
            guac.encode("size", width, height, dpi),
            guac.encode("audio"),
            guac.encode("video"),
            # Not image/webp: Chrome decodes it through VideoFrame, and
            # guacamole-common-js 1.6 leaves those for the garbage collector,
            # which Chrome reports as an error on every frame.
            guac.encode("image", "image/png", "image/jpeg"),
            guac.encode("timezone", timezone),
        ):
            sock.sendall(instruction.encode())
        sock.sendall(guac.encode("connect", *guac.connect_values(args[1:], params)).encode())
        ready, trailing = _expect("ready")
    except (OSError, guac.ProtocolError) as exc:
        sock.close()
        raise GuacdError(f"The RDP service (guacd) failed during the handshake: {exc}") from exc
    except GuacdError:
        sock.close()
        raise
    return sock, (ready[1] if len(ready) > 1 else ""), splitter, trailing
