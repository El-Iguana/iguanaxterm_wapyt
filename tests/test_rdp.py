"""
RDP sites and the relay (#55-#57, #60, #61), without guacd: a fake guacd on a
local socket plays the other end and records what the relay sends it.
"""
from __future__ import annotations

import asyncio
import json
import socket
import struct
import threading

import pytest
from fastapi import FastAPI

from _asgi import WebSocket, ws_refusal_code
from _world import svc, world  # noqa: F401  (fixture)
from services import guac, rdp_cert

CSRF = "t"


class _FakeSession:
    def __init__(self, app, user: dict) -> None:
        self.app, self.user = app, user

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            scope["session"] = {"user": self.user, "csrf_token": CSRF}
        await self.app(scope, receive, send)


def rdp_app(user_id: int, role: str):
    from services.rdp_ws import router

    app = FastAPI()
    app.include_router(router)
    return _FakeSession(app, {"user_id": user_id, "role": role})


ARGS = ["args", "VERSION_1_5_0", "hostname", "port", "domain", "username", "password",
        "security", "ignore-cert", "cert-fingerprints", "resize-method", "server-layout"]


class FakeGuacd:
    """Accepts one connection and plays guacd's side of the handshake."""

    def __init__(self, after_ready: str = "", error: tuple | None = None) -> None:
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(1)
        self.address = "127.0.0.1:%d" % self.server.getsockname()[1]
        self.received: list[list[str]] = []
        self.after_ready = after_ready
        self.error = error
        self.conn = None
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        conn, _ = self.server.accept()
        self.conn = conn
        splitter = guac.ByteSplitter()
        try:
            while True:
                data = conn.recv(65536)
                if not data:
                    return
                for _raw, elements in splitter.feed(data):
                    self.received.append(elements)
                    if elements[0] == "select":
                        conn.sendall(guac.encode(*ARGS).encode())
                    elif elements[0] == "connect":
                        if self.error:
                            conn.sendall(guac.encode("error", *self.error).encode())
                            conn.close()
                            return
                        conn.sendall((guac.encode("ready", "$abc") + self.after_ready).encode())
        except OSError:
            pass

    def opcodes(self) -> list[str]:
        return [elements[0] for elements in self.received]

    def connect(self) -> list[str]:
        return next(e for e in self.received if e[0] == "connect")

    def close(self) -> None:
        for s in (self.conn, self.server):
            try:
                s and s.close()
            except OSError:
                pass


@pytest.fixture()
def rdp_site(world, monkeypatch):
    """pam's RDP site, legacy security (no certificate probe) unless a test changes it."""
    from services.session_service import SessionService

    db, ids = world
    monkeypatch.setenv("GANXTERM_GUACD", "127.0.0.1:1")
    pam = svc(SessionService, ids["pam"], "power")
    result = pam.save(
        name="win", host="10.0.0.5", port=3389, session_type="rdp",
        username="CORP\\pam", password="rdp-secret-pw",
        options={"security": "rdp", "keyboard": "de-de-qwertz", "console": True},
    )
    assert result["ok"], result
    return db, ids, result["id"]


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# The site type (#55, #56)
# --------------------------------------------------------------------------


def test_rdp_is_off_without_guacd(world, monkeypatch):
    from services.session_service import SessionService

    _db, ids = world
    monkeypatch.delenv("GANXTERM_GUACD", raising=False)
    pam = svc(SessionService, ids["pam"], "power")
    assert pam.features() == {"rdp": False}
    result = pam.save(name="w", host="h", port=3389, session_type="rdp")
    assert not result["ok"] and "guacd" in result["errors"]["session_type"]
    monkeypatch.setenv("GANXTERM_GUACD", "guacd:4822")
    assert pam.features() == {"rdp": True}


def test_options_round_trip_and_no_secrets(rdp_site):
    from services.session_service import SessionService

    _db, ids, site = rdp_site
    pam = svc(SessionService, ids["pam"], "power")
    got = pam.get(site)
    assert got["options"] == {"domain": "", "security": "rdp", "resize": "display-update",
                              "keyboard": "de-de-qwertz", "console": True}
    listed = next(row for row in pam.list() if row["id"] == site)
    assert listed["options"] == got["options"]
    assert "rdp-secret-pw" not in json.dumps([got, pam.list()])


@pytest.mark.parametrize("bad,field", [
    ({"security": "kerberos"}, "security"),
    ({"resize": "stretch"}, "resize"),
    ({"keyboard": "klingon"}, "keyboard"),
    ({"domain": "CORP\\x"}, "domain"),
    ({"domain": "a b"}, "domain"),
])
def test_options_are_validated(world, monkeypatch, bad, field):
    from services.session_service import SessionService

    _db, ids = world
    monkeypatch.setenv("GANXTERM_GUACD", "guacd:4822")
    result = svc(SessionService, ids["pam"], "power").save(
        name="w", host="h", port=3389, session_type="rdp", options=bad)
    assert not result["ok"] and field in result["errors"]


def test_unknown_option_keys_are_dropped(world, monkeypatch):
    from services.session_service import SessionService

    _db, ids = world
    monkeypatch.setenv("GANXTERM_GUACD", "guacd:4822")
    pam = svc(SessionService, ids["pam"], "power")
    site = pam.save(name="w", host="h", port=3389, session_type="rdp",
                    options={"password": "sneaky", "domain": "CORP"})["id"]
    assert "password" not in pam.get(site)["options"]


def test_rdp_may_tunnel_and_type_change_drops_the_pin(world, monkeypatch):
    from services.session_service import SessionService

    db, ids = world
    monkeypatch.setenv("GANXTERM_GUACD", "guacd:4822")
    pam = svc(SessionService, ids["pam"], "power")
    site = pam.save(name="w", host="127.0.0.1", port=3390, session_type="rdp",
                    via_session_id=ids["pam-ssh"])["id"]
    assert pam.get(site)["via_session_id"] == ids["pam-ssh"]
    with db.get_db() as conn:
        conn.execute("UPDATE sessions SET host_key = 'rdp-sha256:aa' WHERE id = ?", (site,))
    pam.save(session_id=site, name="w", host="127.0.0.1", port=3390, session_type="vnc")
    with db.get_db() as conn:
        assert conn.execute("SELECT host_key FROM sessions WHERE id = ?", (site,)).fetchone()[0] == ""


def test_split_login_and_params():
    from services.rdp_ws import guacd_params, split_login

    assert split_login("CORP\\pam", "") == ("pam", "CORP")
    assert split_login("CORP\\pam", "OTHER") == ("pam", "OTHER")
    assert split_login("pam@corp.example", "") == ("pam@corp.example", "")
    params = guacd_params(
        {"username": "CORP\\pam", "password": "pw",
         "options": json.dumps({"security": "nla", "resize": "none", "console": True})},
        "10.0.0.5", 3389,
    )
    assert params["username"] == "pam" and params["domain"] == "CORP"
    assert params["security"] == "nla" and params["resize-method"] == ""
    assert params["console"] == "true" and params["server-layout"] == "en-us-qwerty"


# --------------------------------------------------------------------------
# The certificate (#61)
# --------------------------------------------------------------------------


def test_connection_request_bytes():
    request = rdp_cert.connection_request()
    assert request[:4] == b"\x03\x00\x00\x13"               # TPKT, 19 bytes
    assert request[5] == 0xE0                               # X.224 Connection Request
    assert struct.unpack("<BBHI", request[11:19]) == (1, 0, 8, 3)   # TLS | CredSSP


def _confirm(kind: int, value: int) -> bytes:
    neg = struct.pack("<BBHI", kind, 0, 8, value)
    x224 = bytes([len(neg) + 6, 0xD0, 0, 0, 0, 0, 0]) + neg
    return struct.pack(">BBH", 3, 0, len(x224) + 4) + x224


def test_parse_confirm():
    assert rdp_cert.parse_confirm(_confirm(2, rdp_cert.PROTOCOL_SSL)) == 1
    assert rdp_cert.parse_confirm(_confirm(2, rdp_cert.PROTOCOL_HYBRID)) == 2
    with pytest.raises(rdp_cert.CertProbeError, match="refused TLS"):
        rdp_cert.parse_confirm(_confirm(3, 5))
    with pytest.raises(rdp_cert.CertProbeError):
        rdp_cert.parse_confirm(b"HTTP/1.1 400 Bad Request\r\n")


def test_guacd_fingerprint_format():
    pin = "rdp-sha256:" + "ab" * 32
    assert rdp_cert.guacd_fingerprint(pin) == "sha256:" + ":".join(["ab"] * 32)


class _Closeable:
    def close(self):
        pass


@pytest.fixture()
def probe_returns(monkeypatch):
    seen = {"value": "rdp-sha256:" + "11" * 32}

    def _probe(_stream, _host=""):
        if isinstance(seen["value"], Exception):
            raise seen["value"]
        return seen["value"]

    monkeypatch.setattr(rdp_cert, "probe", _probe)
    return seen


def test_first_connect_pins_then_same_passes_then_changed_refuses(rdp_site, probe_returns):
    from services import ssh
    from services.access import resolve_site
    from services.rdp_ws import _check_certificate

    db, ids, site = rdp_site
    with db.get_db() as conn:
        conn.execute("UPDATE sessions SET options = ? WHERE id = ?",
                     (json.dumps({"security": "any"}), site))
    profile = resolve_site(site, ids["pam"])[0]
    assert _check_certificate(profile, _Closeable) == probe_returns["value"]
    pinned = resolve_site(site, ids["pam"])[0]
    assert pinned["host_key"] == probe_returns["value"]
    assert _check_certificate(pinned, _Closeable) == probe_returns["value"]
    probe_returns["value"] = "rdp-sha256:" + "22" * 32
    with pytest.raises(ssh.HostKeyChanged):
        _check_certificate(pinned, _Closeable)
    assert resolve_site(site, ids["pam"])[0]["host_key"] == "rdp-sha256:" + "11" * 32
    # A pinned server that stops offering TLS is a downgrade.
    probe_returns["value"] = rdp_cert.NoCertificate("plain RDP")
    with pytest.raises(ssh.HostKeyChanged):
        _check_certificate(pinned, _Closeable)


def test_legacy_rdp_security_has_no_certificate_to_check(rdp_site, probe_returns):
    from services.access import resolve_site
    from services.rdp_ws import _check_certificate

    _db, ids, site = rdp_site
    probe_returns["value"] = AssertionError("must not probe")
    assert _check_certificate(resolve_site(site, ids["pam"])[0], _Closeable) is None


# --------------------------------------------------------------------------
# The relay (#57)
# --------------------------------------------------------------------------


def test_the_route_is_404_without_guacd_or_access(rdp_site, monkeypatch):
    _db, ids, site = rdp_site
    assert ws_refusal_code(rdp_app(ids["ula"], "user"), f"/ws/rdp/{site}") == 4404   # not assigned
    assert ws_refusal_code(rdp_app(ids["pam"], "power"), f"/ws/rdp/{ids['pam-ssh']}") == 4404  # not RDP
    monkeypatch.delenv("GANXTERM_GUACD")
    assert ws_refusal_code(rdp_app(ids["pam"], "power"), f"/ws/rdp/{site}") == 4404


def test_handshake_order_login_kept_server_side_and_no_renegotiation(rdp_site, monkeypatch):
    _db, ids, site = rdp_site
    fake = FakeGuacd(after_ready=guac.encode("sync", "1"))
    monkeypatch.setenv("GANXTERM_GUACD", fake.address)

    async def _go():
        async with WebSocket(rdp_app(ids["pam"], "power"), f"/ws/rdp/{site}?w=1280&h=720&dpi=120") as ws:
            first = await ws.receive(raw=True)
            await ws.to_app.put({"type": "websocket.receive", "text":
                                 guac.encode("", "ping", "123")
                                 + guac.encode("select", "ssh")
                                 + guac.encode("connect", "evil.example")
                                 + guac.encode("sync", "1")
                                 + guac.encode("size", 800, 600, 96)})
            echo = await ws.receive(raw=True)
            await asyncio.sleep(0.5)
            return first, echo

    try:
        first, echo = run(_go())
    finally:
        fake.close()
    assert first.startswith("0.,36.") and "4.sync,1.1;" in first
    assert "rdp-secret-pw" not in first
    assert echo == guac.encode("", "ping", "123")            # keepalive answered here
    assert fake.opcodes()[:7] == ["select", "size", "audio", "video", "image", "timezone", "connect"]
    assert fake.received[0] == ["select", "rdp"]
    assert fake.received[1] == ["size", "1280", "720", "120"]
    connect = dict(zip(ARGS[1:], fake.connect()[1:]))
    assert connect["hostname"] == "10.0.0.5" and connect["port"] == "3389"
    assert connect["username"] == "pam" and connect["domain"] == "CORP"
    assert connect["password"] == "rdp-secret-pw" and connect["security"] == "rdp"
    assert connect["server-layout"] == "de-de-qwertz"
    after = fake.received[7:]
    assert ["select", "ssh"] not in after and not any(e[0] == "connect" for e in after)
    assert ["sync", "1"] in after and ["size", "800", "600", "96"] in after
    assert not any(e[0] == "" for e in after)                 # pings never reach guacd


def test_guacd_errors_are_put_in_words(rdp_site, monkeypatch):
    _db, ids, site = rdp_site
    fake = FakeGuacd(error=("Security negotiation failed (wrong security type?)", "769"))
    monkeypatch.setenv("GANXTERM_GUACD", fake.address)

    async def _go():
        async with WebSocket(rdp_app(ids["pam"], "power"), f"/ws/rdp/{site}") as ws:
            return await ws.receive(raw=True)

    try:
        message = run(_go())
    finally:
        fake.close()
    elements = [e for _raw, e in guac.Splitter().feed(message)]
    error = next(e for e in elements if e[0] == "error")
    assert "login was refused" in error[1] and error[2] == "769"


def test_unreachable_guacd_is_explained(rdp_site, monkeypatch):
    _db, ids, site = rdp_site
    monkeypatch.setenv("GANXTERM_GUACD", "127.0.0.1:1")

    async def _go():
        async with WebSocket(rdp_app(ids["pam"], "power"), f"/ws/rdp/{site}") as ws:
            return await ws.receive(raw=True)

    message = run(_go())
    assert "guacd" in message and "error" in message


def test_a_shared_rdp_site_works_through_its_list(rdp_site, monkeypatch):
    from _world import make_list

    _db, ids, site = rdp_site
    make_list(ids["pam"], "power", "Desktops", [site], [ids["ula"]])
    fake = FakeGuacd()
    monkeypatch.setenv("GANXTERM_GUACD", fake.address)

    async def _go():
        async with WebSocket(rdp_app(ids["ula"], "user"), f"/ws/rdp/{site}") as ws:
            return await ws.receive(raw=True)

    try:
        first = run(_go())
    finally:
        fake.close()
    assert first.startswith("0.,36.") and "rdp-secret-pw" not in first
    assert dict(zip(ARGS[1:], fake.connect()[1:]))["password"] == "rdp-secret-pw"


def test_guacd_status(monkeypatch):
    from services import guacd

    monkeypatch.delenv("GANXTERM_GUACD", raising=False)
    assert guacd.status() == "off"
    monkeypatch.setenv("GANXTERM_GUACD", "127.0.0.1:1")
    assert guacd.status() == "unreachable"
    fake = FakeGuacd()
    monkeypatch.setenv("GANXTERM_GUACD", fake.address)
    try:
        assert guacd.status() == "ready"
    finally:
        fake.close()
    assert guacd.local_address_towards("127.0.0.1") == "127.0.0.1"
