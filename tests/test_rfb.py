"""
The VNC handshake the relay performs so the password never reaches the page.

A fake VNC server runs on the far end of a socketpair. The DES answer itself
is proven against a real x11vnc in tests/smoke/vnc_smoke.py -- a fake that
checked it with this module's own DES would only be agreeing with itself.
"""
from __future__ import annotations

import socket
import struct
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "appcode"))

from services import rfb  # noqa: E402

CHALLENGE = bytes(range(16))


def _serve(script):
    """Run ``script(server_socket)`` against a fresh client socket."""
    ours, theirs = socket.socketpair()
    seen = {}

    def _run():
        try:
            script(theirs, seen)
        finally:
            theirs.close()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return ours, seen, thread


def _reason(text):
    data = text.encode()
    return struct.pack(">I", len(data)) + data


def test_38_no_password():
    def server(s, seen):
        s.sendall(b"RFB 003.008\n")
        seen["version"] = rfb.recv_exact(s, 12)
        s.sendall(bytes([1, rfb.SECURITY_NONE]))
        seen["chosen"] = rfb.recv_exact(s, 1)
        s.sendall(struct.pack(">I", 0))

    sock, seen, thread = _serve(server)
    assert rfb.server_handshake(sock) == (3, 8)
    thread.join(2)
    assert seen == {"version": b"RFB 003.008\n", "chosen": bytes([1])}


def test_38_vnc_password_is_answered_and_accepted():
    def server(s, seen):
        s.sendall(b"RFB 003.008\n")
        rfb.recv_exact(s, 12)
        s.sendall(bytes([2, rfb.SECURITY_NONE, rfb.SECURITY_VNC_AUTH]))
        seen["chosen"] = rfb.recv_exact(s, 1)
        s.sendall(CHALLENGE)
        seen["answer"] = rfb.recv_exact(s, 16)
        s.sendall(struct.pack(">I", 0))

    sock, seen, thread = _serve(server)
    rfb.server_handshake(sock, "hunter2")
    thread.join(2)
    assert seen["chosen"] == bytes([rfb.SECURITY_VNC_AUTH]), "password given: prefer VNC auth"
    assert seen["answer"] == rfb.vnc_auth_response("hunter2", CHALLENGE)


def test_a_rejected_password_says_so_with_the_reason():
    def server(s, seen):
        s.sendall(b"RFB 003.008\n")
        rfb.recv_exact(s, 12)
        s.sendall(bytes([1, rfb.SECURITY_VNC_AUTH]))
        rfb.recv_exact(s, 1)
        s.sendall(CHALLENGE)
        rfb.recv_exact(s, 16)
        s.sendall(struct.pack(">I", 1) + _reason("Authentication failed"))

    sock, _, _ = _serve(server)
    with pytest.raises(rfb.RFBError, match="rejected the password: Authentication failed"):
        rfb.server_handshake(sock, "wrong")


def test_33_server_chooses_vnc_auth_itself():
    def server(s, seen):
        s.sendall(b"RFB 003.003\n")
        seen["version"] = rfb.recv_exact(s, 12)
        s.sendall(struct.pack(">I", rfb.SECURITY_VNC_AUTH))
        s.sendall(CHALLENGE)
        seen["answer"] = rfb.recv_exact(s, 16)
        s.sendall(struct.pack(">I", 0))

    sock, seen, thread = _serve(server)
    assert rfb.server_handshake(sock, "pw") == (3, 3)
    thread.join(2)
    assert seen["version"] == b"RFB 003.003\n"


def test_37_none_has_no_security_result():
    """In 3.7 a None login ends without a SecurityResult; waiting for one hangs."""
    def server(s, seen):
        s.sendall(b"RFB 003.007\n")
        rfb.recv_exact(s, 12)
        s.sendall(bytes([1, rfb.SECURITY_NONE]))
        rfb.recv_exact(s, 1)
        s.sendall(b"\x01")    # ClientInit territory: must not be read as a result

    sock, _, _ = _serve(server)
    assert rfb.server_handshake(sock) == (3, 7)
    assert sock.recv(1) == b"\x01"


def test_apple_3889_is_treated_as_38():
    def server(s, seen):
        s.sendall(b"RFB 003.889\n")
        seen["version"] = rfb.recv_exact(s, 12)
        s.sendall(bytes([1, rfb.SECURITY_NONE]))
        rfb.recv_exact(s, 1)
        s.sendall(struct.pack(">I", 0))

    sock, seen, thread = _serve(server)
    rfb.server_handshake(sock)
    thread.join(2)
    assert seen["version"] == b"RFB 003.008\n"


def test_unsupported_security_names_the_types():
    def server(s, seen):
        s.sendall(b"RFB 003.008\n")
        rfb.recv_exact(s, 12)
        s.sendall(bytes([2, 19, 18]))   # VeNCrypt, TLS -- TigerVNC's default

    sock, _, _ = _serve(server)
    with pytest.raises(rfb.RFBError, match="VeNCrypt, TLS"):
        rfb.server_handshake(sock)


def test_password_required_but_missing():
    def server(s, seen):
        s.sendall(b"RFB 003.008\n")
        rfb.recv_exact(s, 12)
        s.sendall(bytes([1, rfb.SECURITY_VNC_AUTH]))
        rfb.recv_exact(s, 1)

    sock, _, _ = _serve(server)
    with pytest.raises(rfb.RFBError, match="wants a password"):
        rfb.server_handshake(sock, "")


def test_server_refusal_reason_is_passed_on():
    def server(s, seen):
        s.sendall(b"RFB 003.008\n")
        rfb.recv_exact(s, 12)
        s.sendall(bytes([0]) + _reason("Too many security failures"))

    sock, _, _ = _serve(server)
    with pytest.raises(rfb.RFBError, match="Too many security failures"):
        rfb.server_handshake(sock)


def test_not_a_vnc_server():
    def server(s, seen):
        s.sendall(b"SSH-2.0-OpenSSH_9.9\r\n")

    sock, _, _ = _serve(server)
    with pytest.raises(rfb.RFBError, match="not a VNC server"):
        rfb.server_handshake(sock)


def test_the_password_is_cut_at_eight_characters():
    """Every VNC server ignores characters past the eighth; so must we."""
    assert rfb.vnc_auth_response("12345678", CHALLENGE) == rfb.vnc_auth_response(
        "12345678-and-more", CHALLENGE)
    assert rfb.vnc_auth_response("a", CHALLENGE) != rfb.vnc_auth_response("b", CHALLENGE)


def test_refusal_frame_is_a_38_empty_security_list():
    frame = rfb.refuse("nope")
    assert frame == b"\x00" + struct.pack(">I", 4) + b"nope"
