"""
Read an RDP server's TLS certificate, to pin it on first use (#61).

Most RDP servers present a self-signed certificate, so checking it against a
CA fails and "ignore the certificate" is the usual workaround, which is the
man-in-the-middle hole host-key pinning closes for SSH. So before guacd
connects, IguanaXterm opens its own short connection and does what an RDP
client does first:

1. X.224 Connection Request carrying an RDP Negotiation Request for TLS and
   CredSSP (MS-RDPBCGR 2.2.1.1);
2. read the Connection Confirm: the server picks TLS, CredSSP, or plain RDP
   security (or refuses);
3. if it picked TLS or CredSSP, start TLS and read the certificate.

The SHA-256 of the certificate is pinned in the site's ``host_key`` as
``rdp-sha256:<hex>``, and guacd is told to accept exactly that certificate
(its ``cert-fingerprints`` parameter). The TLS runs over a MemoryBIO, so the
same code works on a socket and on an SSH tunnel channel.
"""
from __future__ import annotations

import hashlib
import socket
import ssl
import struct
import warnings
from typing import Optional

PREFIX = "rdp-sha256:"

PROTOCOL_RDP = 0x0
PROTOCOL_SSL = 0x1
PROTOCOL_HYBRID = 0x2

_TIMEOUT = 10


class CertProbeError(Exception):
    """The server didn't answer like an RDP server."""


class NoCertificate(Exception):
    """The server uses plain RDP security: there is no certificate to pin."""


def connection_request(protocols: int = PROTOCOL_SSL | PROTOCOL_HYBRID) -> bytes:
    """TPKT + X.224 Connection Request + RDP_NEG_REQ."""
    neg = struct.pack("<BBHI", 0x01, 0x00, 8, protocols)        # RDP_NEG_REQ
    x224 = bytes([len(neg) + 6, 0xE0, 0, 0, 0, 0, 0]) + neg      # LI, CR, dst, src, class
    return struct.pack(">BBH", 3, 0, len(x224) + 4) + x224       # TPKT


def parse_confirm(data: bytes) -> int:
    """
    The protocol the server selected, from its Connection Confirm.

    Raises CertProbeError for anything that isn't one, or for a negotiation
    failure (the server refuses every protocol offered).
    """
    if len(data) < 11 or data[0] != 3:
        raise CertProbeError("The server didn't answer like an RDP server.")
    if data[5] != 0xD0:
        raise CertProbeError("The server refused the RDP connection.")
    if len(data) < 19:
        return PROTOCOL_RDP          # no negotiation response: old server, plain RDP
    kind, _flags, _length, value = struct.unpack("<BBHI", data[11:19])
    if kind == 0x02:                 # RDP_NEG_RSP
        return value
    if kind == 0x03:                 # RDP_NEG_FAILURE
        raise CertProbeError(f"The server refused TLS and CredSSP (failure code {value}).")
    raise CertProbeError("The server sent an unexpected negotiation reply.")


def fingerprint(der: bytes) -> str:
    """How a certificate is pinned: ``rdp-sha256:`` and lowercase hex."""
    return PREFIX + hashlib.sha256(der).hexdigest()


def guacd_fingerprint(pin: str) -> str:
    """
    The pin as guacd's ``cert-fingerprints`` wants it.

    guacd hands it to FreeRDP, which compares ``sha256:`` and the colon-separated
    hex digest.
    """
    digest = pin[len(PREFIX):] if pin.startswith(PREFIX) else pin
    return "sha256:" + ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def probe_tls(host: str, port: int, timeout: float = 10.0) -> str:
    """
    The pin of a plain TLS server's certificate: an RD Gateway (#75), which
    speaks HTTPS, so no X.224 first. Nothing is verified here; the caller
    compares the pin.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, int(port)), timeout=timeout) as raw:
            with context.wrap_socket(raw, server_hostname=host or None) as tls:
                der = tls.getpeercert(binary_form=True)
    except (OSError, ssl.SSLError) as exc:
        raise CertProbeError(str(exc)) from exc
    if not der:
        raise CertProbeError("no certificate")
    return fingerprint(der)


def thumbprint(value: Optional[str]) -> str:
    """
    A certificate thumbprint a person typed or pasted, as FreeRDP compares it:
    ``sha1:aa:bb:…`` (the Thumbprint Windows shows) or ``sha256:…``. Spaces and
    colons are ignored. "" for nothing; ValueError for anything else (#75).
    """
    digits = "".join(ch for ch in (value or "") if ch not in " :\t\u200e").lower()
    if not digits:
        return ""
    if any(ch not in "0123456789abcdef" for ch in digits):
        raise ValueError("not hex")
    names = {40: "sha1", 64: "sha256"}
    if len(digits) not in names:
        raise ValueError("not a SHA-1 or SHA-256 thumbprint")
    return names[len(digits)] + ":" + ":".join(digits[i:i + 2] for i in range(0, len(digits), 2))


def is_rdp_pin(value: Optional[str]) -> bool:
    return bool(value) and value.startswith(PREFIX)


class _Stream:
    """recv/sendall over a socket or a paramiko channel, with a deadline."""

    def __init__(self, stream) -> None:
        self.stream = stream
        try:
            stream.settimeout(_TIMEOUT)
        except Exception:
            pass

    def send(self, data: bytes) -> None:
        self.stream.sendall(data)

    def recv(self, size: int = 65536) -> bytes:
        data = self.stream.recv(size)
        if not data:
            raise CertProbeError("The server closed the connection during the certificate check.")
        return data

    def recv_tpkt(self) -> bytes:
        data = b""
        while len(data) < 4:
            data += self.recv(4 - len(data))
        length = struct.unpack(">H", data[2:4])[0]
        while len(data) < length:
            data += self.recv(length - len(data))
        return data


def probe(stream, hostname: str = "") -> str:
    """
    The server's certificate pin, over an open socket or channel.

    Raises NoCertificate for plain RDP security, CertProbeError for anything
    that isn't a working RDP server. The stream is left for the caller to close.
    """
    io = _Stream(stream)
    io.send(connection_request())
    selected = parse_confirm(io.recv_tpkt())
    if not selected & (PROTOCOL_SSL | PROTOCOL_HYBRID):
        raise NoCertificate("This server uses plain RDP security; there is no certificate to pin.")

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    # Not a CA check on purpose: the pin is the check (see the module docstring).
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    # Old RDP servers still speak TLS 1.0; this connection only reads the
    # certificate and sends nothing secret.
    # (Windows Server 2008 R2 offers only TLS 1.0 by default.) Python flags
    # TLSv1 as deprecated; here it is deliberate.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        try:
            context.minimum_version = ssl.TLSVersion.TLSv1
            context.set_ciphers("DEFAULT:@SECLEVEL=0")
        except (ValueError, ssl.SSLError):
            pass
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    tls = context.wrap_bio(incoming, outgoing, server_hostname=hostname or None)
    while True:
        try:
            tls.do_handshake()
            break
        except ssl.SSLWantReadError:
            pending = outgoing.read()
            if pending:
                io.send(pending)
            incoming.write(io.recv())
        except ssl.SSLError as exc:
            raise CertProbeError(f"TLS with the RDP server failed: {exc.reason or exc}") from exc
    pending = outgoing.read()
    if pending:
        io.send(pending)
    der = tls.getpeercert(binary_form=True)
    if not der:
        raise CertProbeError("The RDP server sent no certificate.")
    return fingerprint(der)
