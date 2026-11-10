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
        "security", "ignore-cert", "cert-fingerprints", "resize-method", "server-layout",
        "gateway-hostname", "gateway-port", "gateway-domain", "gateway-username",
        "gateway-password"]


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
    assert pam.features() == {"rdp": False, "rdp_drive": False}
    result = pam.save(name="w", host="h", port=3389, session_type="rdp")
    assert not result["ok"] and "guacd" in result["errors"]["session_type"]
    monkeypatch.setenv("GANXTERM_GUACD", "guacd:4822")
    assert pam.features()["rdp"] is True


def test_options_round_trip_and_no_secrets(rdp_site):
    from services.session_service import SessionService

    _db, ids, site = rdp_site
    pam = svc(SessionService, ids["pam"], "power")
    got = pam.get(site)
    assert got["options"] == {"domain": "", "security": "rdp", "resize": "display-update",
                              "keyboard": "de-de-qwertz", "console": True, "audio": True,
                              "performance": "balanced", "color_depth": "",
                              "wallpaper": False, "theming": True, "font_smoothing": True,
                              "window_drag": False, "composition": False, "animations": False,
                              "hidpi": False, "gateway_host": "", "gateway_port": 443,
                              "gateway_domain": "", "gateway_same_login": True,
                              "gateway_username": "", "desktop_thumbprint": "",
                              "drive": False, "drive_upload": True, "drive_download": True,
                              "printing": False}
    listed = next(row for row in pam.list() if row["id"] == site)
    assert listed["options"] == got["options"]
    assert "rdp-secret-pw" not in json.dumps([got, pam.list()])


@pytest.mark.parametrize("bad,field", [
    ({"security": "kerberos"}, "security"),
    ({"resize": "stretch"}, "resize"),
    ({"keyboard": "klingon"}, "keyboard"),
    ({"domain": "CORP\\x"}, "domain"),
    ({"domain": "a b"}, "domain"),
    ({"performance": "turbo"}, "performance"),
    ({"color_depth": "12"}, "color_depth"),
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


def test_audio_setting(world, monkeypatch):
    """#73: sound is on unless the site turns it off, including old sites."""
    from services.rdp_ws import guacd_params, rdp_options
    from services.session_service import SessionService

    # A site saved before #73 has no "audio" key.
    old = {"username": "u", "options": json.dumps({"security": "nla"})}
    assert rdp_options(old)["audio"] is True
    assert guacd_params(old, "h", 3389)["disable-audio"] == ""
    off = {"username": "u", "options": json.dumps({"audio": False})}
    assert rdp_options(off)["audio"] is False
    assert guacd_params(off, "h", 3389)["disable-audio"] == "true"

    _db, ids = world
    monkeypatch.setenv("GANXTERM_GUACD", "guacd:4822")
    pam = svc(SessionService, ids["pam"], "power")
    quiet = pam.save(name="q", host="h", port=3389, session_type="rdp",
                     options={"audio": False})["id"]
    assert pam.get(quiet)["options"]["audio"] is False
    loud = pam.save(name="l", host="h", port=3389, session_type="rdp", options={})["id"]
    assert pam.get(loud)["options"]["audio"] is True


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
    # #73: sound on by default, offered as raw PCM the pane can play.
    assert fake.received[2] == ["audio", "audio/L16", "audio/L8"]
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


# --------------------------------------------------------------------------
# Performance presets and high DPI (#74)
# --------------------------------------------------------------------------

EFFECT_PARAMS = ("enable-wallpaper", "enable-theming", "enable-font-smoothing",
                 "enable-full-window-drag", "enable-desktop-composition", "enable-menu-animations")


def _look(options: dict) -> dict:
    from services.rdp_ws import guacd_params

    params = guacd_params({"username": "u", "options": json.dumps(options)}, "h", 3389)
    return {name: params[name] for name in ("color-depth", *EFFECT_PARAMS)}


def test_presets_map_to_guacd_parameters():
    best = _look({"performance": "best"})
    assert best["color-depth"] == "32" and all(best[p] == "true" for p in EFFECT_PARAMS)
    slow = _look({"performance": "slow"})
    assert slow["color-depth"] == "16" and not any(slow[p] for p in EFFECT_PARAMS)
    balanced = _look({"performance": "balanced"})
    assert balanced["color-depth"] == ""
    assert [p for p in EFFECT_PARAMS if balanced[p]] == ["enable-theming", "enable-font-smoothing"]


def test_a_site_from_before_74_is_balanced():
    assert _look({"security": "nla"}) == _look({"performance": "balanced"})
    assert _look({"performance": "nonsense"}) == _look({"performance": "balanced"})


def test_custom_takes_the_sites_own_values_and_presets_ignore_them():
    own = {"color_depth": "8", "wallpaper": True, "theming": False, "font_smoothing": False}
    custom = _look({"performance": "custom", **own})
    assert custom["color-depth"] == "8"
    assert custom["enable-wallpaper"] == "true" and custom["enable-theming"] == ""
    assert custom["enable-font-smoothing"] == ""
    # A preset wins over leftover custom values.
    assert _look({"performance": "slow", **own}) == _look({"performance": "slow"})
    # Custom with nothing set is Balanced.
    assert _look({"performance": "custom"}) == _look({"performance": "balanced"})


def test_performance_and_hidpi_round_trip(world, monkeypatch):
    from services.rdp_ws import rdp_options
    from services.session_service import SessionService

    _db, ids = world
    monkeypatch.setenv("GANXTERM_GUACD", "guacd:4822")
    pam = svc(SessionService, ids["pam"], "power")
    site = pam.save(name="c", host="h", port=3389, session_type="rdp",
                    options={"performance": "custom", "color_depth": "24",
                             "animations": True, "hidpi": True})["id"]
    got = pam.get(site)["options"]
    assert got["performance"] == "custom" and got["color_depth"] == "24"
    assert got["animations"] is True and got["hidpi"] is True
    assert rdp_options({"options": json.dumps(got)})["hidpi"] is True
    assert rdp_options({"options": "{}"})["hidpi"] is False     # opt-in


# --------------------------------------------------------------------------
# Remote Desktop Gateway (#75)
# --------------------------------------------------------------------------


def _self_signed(tmp_path, name: str):
    """A throwaway certificate and key, and the certificate's DER."""
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
            .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / f"{name}.crt", tmp_path / f"{name}.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return cert_path, key_path, cert.public_bytes(serialization.Encoding.DER)


class FakeGateway:
    """A TLS server standing in for an RD Gateway: it only shows its certificate."""

    def __init__(self, cert_path, key_path) -> None:
        import ssl

        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(cert_path, key_path)
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(4)
        self.port = self.server.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            try:
                with self.context.wrap_socket(conn, server_side=True) as tls:
                    tls.recv(1)
            except OSError:
                pass

    def close(self) -> None:
        self.server.close()


@pytest.fixture()
def gateway(tmp_path):
    cert, key, der = _self_signed(tmp_path, "rdgw")
    fake = FakeGateway(cert, key)
    yield fake, rdp_cert.fingerprint(der)
    fake.close()


def _gateway_site(pam, port: int, **options) -> int:
    result = pam.save(
        name="behind", host="desk-07.corp.local", port=3389, session_type="rdp",
        username="CORP\\pam", password="desk-pw", gateway_password="gw-secret-pw",
        options={"gateway_host": "127.0.0.1", "gateway_port": port, **options},
    )
    assert result["ok"], result
    return result["id"]


def test_thumbprints_are_normalised_for_freerdp():
    windows = "‎" + "AB CD EF 01 23 45 67 89 AB CD EF 01 23 45 67 89 AB CD EF 01"
    assert rdp_cert.thumbprint(windows) == "sha1:" + ":".join(
        ["ab", "cd", "ef", "01", "23", "45", "67", "89"] * 2 + ["ab", "cd", "ef", "01"])
    assert rdp_cert.thumbprint("aa" * 32).startswith("sha256:aa:aa")
    assert rdp_cert.thumbprint("  ") == ""
    for bad in ("xyz", "ab" * 19, "ab" * 33):
        with pytest.raises(ValueError):
            rdp_cert.thumbprint(bad)


def test_gateway_login_is_the_desktops_unless_the_site_says_otherwise():
    from services.rdp_ws import guacd_params

    profile = {"username": "CORP\\pam", "password": "desk-pw", "gateway_password": "gw-pw",
               "options": json.dumps({"gateway_host": "rdgw.example.com"})}
    params = guacd_params(profile, "desk", 3389)
    assert params["gateway-hostname"] == "rdgw.example.com" and params["gateway-port"] == "443"
    assert (params["gateway-username"], params["gateway-domain"],
            params["gateway-password"]) == ("pam", "CORP", "desk-pw")
    profile["options"] = json.dumps({"gateway_host": "rdgw.example.com", "gateway_port": 8443,
                                     "gateway_same_login": False,
                                     "gateway_username": "EDGE\\gw-user"})
    params = guacd_params(profile, "desk", 3389)
    assert params["gateway-port"] == "8443"
    assert (params["gateway-username"], params["gateway-domain"],
            params["gateway-password"]) == ("gw-user", "EDGE", "gw-pw")
    # No gateway, no gateway parameters.
    assert "gateway-hostname" not in guacd_params({"username": "u"}, "desk", 3389)


@pytest.mark.parametrize("bad,field", [
    ({"gateway_host": "rd gw"}, "gateway_host"),
    ({"gateway_host": "rdgw", "gateway_port": 70000}, "gateway_port"),
    ({"gateway_host": "rdgw", "gateway_domain": "A\\B"}, "gateway_domain"),
    ({"gateway_host": "rdgw", "desktop_thumbprint": "not-hex"}, "desktop_thumbprint"),
])
def test_gateway_options_are_validated(world, monkeypatch, bad, field):
    from services.session_service import SessionService

    _db, ids = world
    monkeypatch.setenv("GANXTERM_GUACD", "guacd:4822")
    result = svc(SessionService, ids["pam"], "power").save(
        name="w", host="h", port=3389, session_type="rdp", options=bad)
    assert not result["ok"] and field in result["errors"]


def test_a_gateway_and_an_ssh_tunnel_are_refused_together(world, monkeypatch):
    from services.session_service import SessionService

    _db, ids = world
    monkeypatch.setenv("GANXTERM_GUACD", "guacd:4822")
    result = svc(SessionService, ids["pam"], "power").save(
        name="w", host="h", port=3389, session_type="rdp", via_session_id=ids["pam-ssh"],
        options={"gateway_host": "rdgw"})
    assert not result["ok"] and "via_session_id" in result["errors"]


def test_the_gateway_password_is_stored_encrypted_and_never_listed(world, monkeypatch):
    from services.access import resolve_site
    from services.session_service import SessionService

    db, ids = world
    monkeypatch.setenv("GANXTERM_GUACD", "guacd:4822")
    pam = svc(SessionService, ids["pam"], "power")
    site = _gateway_site(pam, 443)
    got = pam.get(site)
    assert got["has_gateway_password"] is True
    assert "gw-secret-pw" not in json.dumps([got, pam.list()])
    with db.get_db() as conn:
        stored = conn.execute("SELECT gateway_password FROM sessions WHERE id = ?",
                              (site,)).fetchone()[0]
    assert stored and "gw-secret-pw" not in stored
    assert resolve_site(site, ids["pam"])[0]["gateway_password"] == "gw-secret-pw"
    # Omitted keeps it; "" clears it.
    pam.save(session_id=site, name="behind", host="desk-07.corp.local", port=3389,
             session_type="rdp", options={"gateway_host": "127.0.0.1"})
    assert resolve_site(site, ids["pam"])[0]["gateway_password"] == "gw-secret-pw"
    pam.save(session_id=site, name="behind", host="desk-07.corp.local", port=3389,
             session_type="rdp", options={"gateway_host": "127.0.0.1"}, gateway_password="")
    assert pam.get(site)["has_gateway_password"] is False


def test_the_gateway_pin_follows_the_gateway_and_forget_clears_it(world, monkeypatch):
    from services.session_service import SessionService

    db, ids = world
    monkeypatch.setenv("GANXTERM_GUACD", "guacd:4822")
    pam = svc(SessionService, ids["pam"], "power")
    site = _gateway_site(pam, 443)

    def pin(value=None):
        with db.get_db() as conn:
            if value is not None:
                conn.execute("UPDATE sessions SET gateway_key = ? WHERE id = ?", (value, site))
            return conn.execute("SELECT gateway_key FROM sessions WHERE id = ?",
                                (site,)).fetchone()[0]

    def save(**options):
        assert pam.save(session_id=site, name="behind", host="desk-07.corp.local", port=3389,
                        session_type="rdp", options=options)["ok"]

    pin("rdp-sha256:aa")
    save(gateway_host="127.0.0.1", gateway_port=443, performance="slow")
    assert pin() == "rdp-sha256:aa"                     # same gateway: kept
    assert next(r for r in pam.list() if r["id"] == site)["has_host_key"]
    save(gateway_host="127.0.0.1", gateway_port=8443)
    assert pin() == ""                                  # another port: re-pin
    pin("rdp-sha256:aa")
    save(gateway_host="rdgw2")
    assert pin() == ""                                  # another host
    pin("rdp-sha256:aa")
    assert pam.clear_host_key(site)["ok"] and pin() == ""


def test_connecting_through_a_gateway_pins_it_and_hands_guacd_the_prints(
        world, monkeypatch, gateway):
    from services import ssh
    from services.access import resolve_site
    from services.rdp_ws import _prepare
    from services.session_service import SessionService

    fake_gw, gw_pin = gateway
    db, ids = world
    monkeypatch.setenv("GANXTERM_GUACD", "guacd:4822")
    pam = svc(SessionService, ids["pam"], "power")
    site = _gateway_site(pam, fake_gw.port)

    def connect():
        fake = FakeGuacd()
        monkeypatch.setenv("GANXTERM_GUACD", fake.address)
        try:
            sock, *_rest = _prepare(resolve_site(site, ids["pam"])[0], 1024, 768, 96)
            sock.close()
            return dict(zip(ARGS[1:], fake.connect()[1:]))
        finally:
            fake.close()

    # Without the desktop's thumbprint: the gateway is pinned, guacd ignores
    # the desktop's certificate (it can't be read from here).
    sent = connect()
    assert resolve_site(site, ids["pam"])[0]["gateway_key"] == gw_pin
    assert sent["cert-fingerprints"] == rdp_cert.guacd_fingerprint(gw_pin)
    assert sent["ignore-cert"] == "true"
    assert sent["hostname"] == "desk-07.corp.local"
    assert sent["gateway-hostname"] == "127.0.0.1" and sent["gateway-password"] == "desk-pw"

    # With it: both prints, and guacd checks them.
    thumb = "AB" * 20
    assert pam.save(session_id=site, name="behind", host="desk-07.corp.local", port=3389,
                    session_type="rdp",
                    options={"gateway_host": "127.0.0.1", "gateway_port": fake_gw.port,
                             "desktop_thumbprint": thumb})["ok"]
    sent = connect()
    assert sent["cert-fingerprints"] == (rdp_cert.guacd_fingerprint(gw_pin) + ","
                                         + rdp_cert.thumbprint(thumb))
    assert sent["ignore-cert"] == "false"

    # A different gateway certificate is refused before guacd is asked.
    with db.get_db() as conn:
        conn.execute("UPDATE sessions SET gateway_key = ? WHERE id = ?",
                     ("rdp-sha256:" + "00" * 32, site))
    with pytest.raises(ssh.HostKeyChanged):
        _prepare(resolve_site(site, ids["pam"])[0], 1024, 768, 96)


def test_an_unreachable_gateway_is_explained(world, monkeypatch):
    from services import guacd
    from services.access import resolve_site
    from services.rdp_ws import _prepare
    from services.session_service import SessionService

    _db, ids = world
    monkeypatch.setenv("GANXTERM_GUACD", "guacd:4822")
    pam = svc(SessionService, ids["pam"], "power")
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()
    site = _gateway_site(pam, port)
    with pytest.raises(guacd.GuacdError) as caught:
        _prepare(resolve_site(site, ids["pam"])[0], 1024, 768, 96)
    assert "Remote Desktop Gateway at 127.0.0.1" in caught.value.message


def test_gateway_errors_point_at_the_gateway():
    from services.rdp_ws import with_gateway_hint

    assert "Gateway rdgw" in with_gateway_hint("The login was refused.", "769", "rdgw")
    assert with_gateway_hint("The login was refused.", "769", "") == "The login was refused."
    assert with_gateway_hint("The session ended.", "525", "rdgw") == "The session ended."


# --------------------------------------------------------------------------
# Print to PDF (#77)
# --------------------------------------------------------------------------


def test_printing_is_off_unless_the_site_asks(world, monkeypatch):
    from services.rdp_ws import PRINTER_NAME, guacd_params, rdp_options
    from services.session_service import SessionService

    off = guacd_params({"username": "u", "options": "{}"}, "h", 3389)
    assert off["enable-printing"] == "" and off["printer-name"] == ""
    on = guacd_params({"username": "u", "options": json.dumps({"printing": True})}, "h", 3389)
    assert on["enable-printing"] == "true" and on["printer-name"] == PRINTER_NAME == "IguanaXterm PDF"
    # Printing doesn't open Guacamole's file channel for anything else.
    assert on["disable-download"] == "true" and on["disable-upload"] == "true"

    _db, ids = world
    monkeypatch.setenv("GANXTERM_GUACD", "guacd:4822")
    pam = svc(SessionService, ids["pam"], "power")
    site = pam.save(name="p", host="h", port=3389, session_type="rdp",
                    options={"printing": True})["id"]
    assert pam.get(site)["options"]["printing"] is True
    assert rdp_options({"options": json.dumps(pam.get(site)["options"])})["printing"] is True
