"""
A real guacd against a stand-in Remote Desktop Gateway (#75).

There's no RD Gateway here, but the part IguanaXterm decides can still be
measured: whether guacd's FreeRDP honours ``cert-fingerprints`` for the
**gateway's** TLS. The stand-in is a TLS server with a throwaway certificate.
When FreeRDP accepts the certificate it goes on to send its gateway request
over HTTPS (``RDG_OUT_DATA`` or ``RPC_IN_DATA``); when it refuses, it hangs up
during the handshake and nothing arrives.

    IX_GUACD=127.0.0.1:4823 uv run --with pytest python -m pytest tests/test_rdp_gateway_live.py

guacd must reach 127.0.0.1 on this machine, so run it on the host network
(the smoke README's ``ix-guacd-test``).
"""
from __future__ import annotations

import os
import socket
import ssl
import threading
import time

import pytest

from services import guacd, rdp_cert
from test_rdp import _self_signed

GUACD = os.environ.get("IX_GUACD", "")


def _up(address: str) -> bool:
    try:
        host, port = address.rsplit(":", 1)
        socket.create_connection((host, int(port)), timeout=1).close()
        return True
    except (OSError, ValueError):
        return False


pytestmark = pytest.mark.skipif(not (GUACD and _up(GUACD)),
                                reason="needs a guacd on the host network (IX_GUACD)")


class RecordingGateway:
    """A TLS server that records the first bytes a client sends after the handshake."""

    def __init__(self, cert_path, key_path) -> None:
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(cert_path, key_path)
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(8)
        self.port = self.server.getsockname()[1]
        self.requests: list[bytes] = []
        self.handshakes_failed = 0
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._one, args=(conn,), daemon=True).start()

    def _one(self, conn: socket.socket) -> None:
        conn.settimeout(10)
        try:
            with self.context.wrap_socket(conn, server_side=True) as tls:
                data = tls.recv(4096)
                if data:
                    self.requests.append(data)
        except (OSError, ssl.SSLError):
            self.handshakes_failed += 1

    def close(self) -> None:
        self.server.close()


@pytest.fixture()
def stand_in(tmp_path):
    cert, key, der = _self_signed(tmp_path, "rdgw-live")
    gateway = RecordingGateway(cert, key)
    yield gateway, rdp_cert.fingerprint(der)
    gateway.close()


def _connect(monkeypatch, gateway, fingerprints: str, ignore: str, seconds: float = 8.0) -> None:
    monkeypatch.setenv("GANXTERM_GUACD", GUACD)
    params = {
        "hostname": "desk.invalid", "port": "3389", "username": "u", "password": "p",
        "security": "any", "gateway-hostname": "127.0.0.1",
        "gateway-port": str(gateway.port), "gateway-username": "u", "gateway-password": "p",
        "cert-fingerprints": fingerprints, "ignore-cert": ignore,
    }
    try:
        sock, *_rest = guacd.handshake(params, 1024, 768, 96)
        sock.close()
    except guacd.GuacdError:
        pass                    # the gateway is a stand-in: guacd gives up after TLS
    end = time.time() + seconds
    while time.time() < end and not gateway.requests and not gateway.handshakes_failed:
        time.sleep(0.2)


def test_guacd_accepts_a_gateway_whose_certificate_is_pinned(monkeypatch, stand_in):
    gateway, pin = stand_in
    _connect(monkeypatch, gateway, rdp_cert.guacd_fingerprint(pin), "false")
    assert gateway.requests, "FreeRDP never got past the gateway's TLS"
    assert gateway.requests[0].split(b" ", 1)[0] in (b"RDG_OUT_DATA", b"RPC_IN_DATA",
                                                     b"RPC_OUT_DATA", b"GET", b"POST")


def test_guacd_refuses_a_gateway_whose_certificate_is_not(monkeypatch, stand_in):
    gateway, _pin = stand_in
    wrong = rdp_cert.guacd_fingerprint("rdp-sha256:" + "00" * 32)
    _connect(monkeypatch, gateway, wrong + "," + rdp_cert.thumbprint("ab" * 20), "false")
    assert not gateway.requests, "FreeRDP sent its gateway request to an unpinned certificate"


def test_with_ignore_cert_guacd_accepts_any_gateway(monkeypatch, stand_in):
    """Why a site without the desktop's thumbprint relies on our own check."""
    gateway, _pin = stand_in
    _connect(monkeypatch, gateway, rdp_cert.guacd_fingerprint("rdp-sha256:" + "00" * 32), "true")
    assert gateway.requests
