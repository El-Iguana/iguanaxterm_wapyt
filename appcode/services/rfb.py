"""
The VNC (RFB) handshake, done on the server so the VNC password never
reaches the browser.

noVNC speaks RFB over a WebSocket. Left to itself it would do the VNC login
in the page, which means sending the stored password to the browser -- the one
thing this app never does with a credential. So the relay logs in to the VNC
server here, then offers noVNC a server that needs no authentication, and
from then on copies bytes both ways untouched. Everything after the security
handshake (ClientInit, ServerInit, framebuffer updates) is the same in RFB
3.3, 3.7 and 3.8, which is what lets the two sides negotiate different
versions.

Supported server security types: None (1) and VNC Authentication (2) -- what
x11vnc, wayvnc, krfb and a TigerVNC told ``SecurityTypes=VncAuth`` use. TLS
and VeNCrypt are refused with a message naming them; behind an SSH tunnel
they add nothing but a second layer of encryption.
"""
from __future__ import annotations

import struct
from typing import Protocol

from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
from cryptography.hazmat.primitives.ciphers import Cipher, modes

SECURITY_NONE = 1
SECURITY_VNC_AUTH = 2

# For a readable refusal. RFC 6143 section 7.1.2, plus the common extensions.
_SECURITY_NAMES = {
    5: "RA2", 6: "RA2ne", 16: "Tight", 17: "Ultra", 18: "TLS", 19: "VeNCrypt",
    20: "GTK-VNC SASL", 21: "MD5 hash", 22: "Colin Dean xvp", 30: "Apple DH",
    113: "MSLogonII", 129: "Unix login",
}

# What the relay tells noVNC it is.
CLIENT_VERSION = b"RFB 003.008\n"


class RFBError(Exception):
    """The VNC server refused, or spoke something this relay cannot."""


class Stream(Protocol):
    def recv(self, size: int) -> bytes: ...
    def sendall(self, data: bytes) -> None: ...


def recv_exact(stream: Stream, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = stream.recv(size - len(data))
        if not chunk:
            raise RFBError("The VNC server closed the connection during the handshake")
        data += chunk
    return data


def _read_reason(stream: Stream) -> str:
    (length,) = struct.unpack(">I", recv_exact(stream, 4))
    return recv_exact(stream, min(length, 4096)).decode("utf-8", "replace")


def vnc_auth_response(password: str, challenge: bytes) -> bytes:
    """
    The 16-byte answer to a VNC Authentication challenge.

    DES in ECB mode, keyed with the first 8 bytes of the password (zero-padded)
    **with each byte's bits reversed** -- the historical quirk every VNC server
    expects. Anything past 8 characters is ignored, by every server, by design.
    """
    raw = password.encode("latin-1", "replace")[:8].ljust(8, b"\0")
    key = bytes(int(f"{byte:08b}"[::-1], 2) for byte in raw)
    # Single DES, spelled as TripleDES with the key three times: E-D-E under
    # one key cancels to one encryption. cryptography is dropping the 8-byte
    # form, and single DES has no other home there.
    encryptor = Cipher(TripleDES(key * 3), modes.ECB()).encryptor()
    return encryptor.update(challenge) + encryptor.finalize()


def server_handshake(stream: Stream, password: str = "") -> tuple[int, int]:
    """
    Log in to a VNC server as far as the end of its security handshake.

    Returns the ``(major, minor)`` version agreed with the server. Raises
    ``RFBError`` with a message fit to show a person.
    """
    greeting = recv_exact(stream, 12)
    if not greeting.startswith(b"RFB "):
        raise RFBError("That port is not a VNC server")
    try:
        major, minor = int(greeting[4:7]), int(greeting[8:11])
    except ValueError:
        raise RFBError("That port is not a VNC server") from None
    # Apple announces 3.889; anything from 3.8 up behaves as 3.8 here.
    minor = 8 if minor >= 8 else 7 if minor == 7 else 3
    stream.sendall(b"RFB 003.%03d\n" % minor)

    if minor == 3:
        (chosen,) = struct.unpack(">I", recv_exact(stream, 4))
        if chosen == 0:
            raise RFBError(f"The VNC server refused the connection: {_read_reason(stream)}")
        offered = [chosen]
    else:
        (count,) = recv_exact(stream, 1)
        if count == 0:
            raise RFBError(f"The VNC server refused the connection: {_read_reason(stream)}")
        offered = list(recv_exact(stream, count))
        if password and SECURITY_VNC_AUTH in offered:
            chosen = SECURITY_VNC_AUTH
        elif SECURITY_NONE in offered:
            chosen = SECURITY_NONE
        elif SECURITY_VNC_AUTH in offered:
            chosen = SECURITY_VNC_AUTH
        else:
            names = ", ".join(_SECURITY_NAMES.get(t, str(t)) for t in offered)
            raise RFBError(
                f"The VNC server only offers {names}. IguanaXterm supports no "
                "password and VNC password login; configure the server for VNC "
                "password (and reach it through an SSH tunnel for encryption)."
            )
        stream.sendall(bytes([chosen]))

    if chosen == SECURITY_VNC_AUTH:
        if not password:
            raise RFBError("The VNC server wants a password; add one to this session")
        challenge = recv_exact(stream, 16)
        stream.sendall(vnc_auth_response(password, challenge))
    elif chosen != SECURITY_NONE:
        names = _SECURITY_NAMES.get(chosen, str(chosen))
        raise RFBError(f"The VNC server insists on {names}, which IguanaXterm does not support")

    # SecurityResult: always after VNC auth; after None only from 3.8.
    if chosen == SECURITY_VNC_AUTH or minor == 8:
        (result,) = struct.unpack(">I", recv_exact(stream, 4))
        if result != 0:
            reason = _read_reason(stream) if minor == 8 else ""
            raise RFBError(
                "The VNC server rejected the password" + (f": {reason}" if reason else "")
            )
    return 3, minor


# ── The browser side ──────────────────────────────────────────────────────────
# The relay plays an RFB 3.8 server that offers only "None". These are the
# bytes it sends noVNC; what noVNC sends back is read by the WebSocket code.

def offer_no_auth() -> bytes:
    """Security types: exactly one, None."""
    return bytes([1, SECURITY_NONE])


def security_ok() -> bytes:
    return struct.pack(">I", 0)


def refuse(reason: str) -> bytes:
    """
    A 3.8 security-type list of zero entries plus a reason. noVNC shows the
    reason, so a failed login to the real server reads as one in the page.
    """
    encoded = reason.encode("utf-8")
    return bytes([0]) + struct.pack(">I", len(encoded)) + encoded
