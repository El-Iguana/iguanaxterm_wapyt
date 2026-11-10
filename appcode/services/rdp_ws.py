"""
WebSocket relay for RDP desktops (#57, #60, #61).

    browser (guacamole-common-js) ⇄ /ws/rdp/<id> ⇄ this ⇄ guacd ⇄ RDP server

**The login never reaches the browser.** IguanaXterm does the Guacamole
handshake with guacd itself (``guacd.handshake``), then hands the browser the
tunnel id that ``Guacamole.WebSocketTunnel`` expects and relays whole
instructions both ways. After that the browser can't renegotiate: a
``select`` or ``connect`` from it is dropped, so it can't change the host or
the login.

Before guacd connects, :func:`_check_certificate` reads the server's TLS
certificate and pins it like an SSH host key; guacd is then told to accept
exactly that certificate (``cert-fingerprints``), so a changed one is refused
by guacd too, not only by us.

A site that tunnels through an SSH site gets a **one-shot listener** that
guacd dials (:class:`_OneShotTunnel`): guacd makes its own TCP connection, so
it needs an address, unlike VNC where IguanaXterm speaks over the channel
itself.
"""
from __future__ import annotations

import asyncio
import json
import logging
import socket
import threading
import time
import uuid
from typing import Optional
from urllib.parse import parse_qs

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from services import drive, guac, guacd, live, rdp_cert
from services import ssh as ssh_helpers
from services.access import host_key_message, pin_gateway_key, pin_host_key, resolve_site
from services.auth import current_user_id
from services.paths import RDP_EFFECTS, RDP_PERFORMANCE, rdp_look, session_caps
from services.terminal_ws import CLOSE_FORBIDDEN, CLOSE_NOT_FOUND, CLOSE_UNAUTHENTICATED, _same_origin

logger = logging.getLogger("iguanaxterm.rdp")

router = APIRouter()

_READ_SIZE = 65536
_TUNNEL_ACCEPT_SECONDS = 15
_CONNECT_TIMEOUT = 10

# Guacamole status codes the relay itself reports.
_UPSTREAM_ERROR = "512"
_UNREACHABLE = "519"
_FORBIDDEN = "771"
_GUACD_NOP_SECONDS = 2.0    # how often an idle desktop checks guacd is still there (#87)
# guacd ends a session it means to end with one of these; anything else is a crash.
_GUACD_ENDINGS = ("error", "disconnect")
_LOST = ("The connection to the remote desktop was lost unexpectedly (guacd ended the "
         "session without saying why). The desktop usually keeps running: "
         "Reconnect to pick up where you left off.")

RDP_SECURITY = ("any", "nla", "tls", "rdp")
RDP_RESIZE = ("display-update", "reconnect", "none")
PERFORMANCE = tuple(key for key, _label in RDP_PERFORMANCE)
EFFECT_KEYS = tuple(key for key, _param, _label in RDP_EFFECTS)

# What the pane can play (#73): raw PCM, which guacamole-common-js's
# RawAudioPlayer handles in every browser with Web Audio. guacd picks one.
AUDIO_TYPES = ("audio/L16", "audio/L8")

# The printer Windows sees when a site prints to PDF (#77).
PRINTER_NAME = "IguanaXterm PDF"


def rdp_options(profile: dict) -> dict:
    """The site's RDP settings, with defaults for anything unset."""
    try:
        stored = json.loads(profile.get("options") or "{}")
    except (TypeError, ValueError):
        stored = {}
    if not isinstance(stored, dict):
        stored = {}
    return {
        "domain": str(stored.get("domain") or ""),
        "security": stored.get("security") if stored.get("security") in RDP_SECURITY else "any",
        "resize": stored.get("resize") if stored.get("resize") in RDP_RESIZE else "display-update",
        "keyboard": str(stored.get("keyboard") or "en-us-qwerty"),
        "console": bool(stored.get("console")),
        # Sites saved before #73 have no "audio" key: they get sound.
        "audio": bool(stored.get("audio", True)),
        "performance": stored.get("performance") if stored.get("performance") in PERFORMANCE
        else "balanced",
        # Every physical pixel on a high-DPI screen (#74), opt-in; the pane reads it.
        "hidpi": bool(stored.get("hidpi", False)),
        **{key: stored[key] for key in ("color_depth", *EFFECT_KEYS) if key in stored},
        # Remote Desktop Gateway (#75); no host means none.
        "gateway_host": str(stored.get("gateway_host") or ""),
        "gateway_port": _clamp(stored.get("gateway_port"), 1, 65535, 443),
        "gateway_domain": str(stored.get("gateway_domain") or ""),
        "gateway_same_login": bool(stored.get("gateway_same_login", True)),
        "gateway_username": str(stored.get("gateway_username") or ""),
        "desktop_thumbprint": str(stored.get("desktop_thumbprint") or ""),
        # The shared drive (#76): off unless asked for; both ways by default.
        "drive": bool(stored.get("drive", False)),
        "drive_upload": bool(stored.get("drive_upload", True)),
        "drive_download": bool(stored.get("drive_download", True)),
        # Print to a PDF the browser downloads (#77); off unless asked for.
        "printing": bool(stored.get("printing", False)),
    }


def split_login(username: str, domain: str) -> tuple[str, str]:
    r"""
    ``DOMAIN\user`` becomes ``(user, DOMAIN)``. ``user@domain`` is passed as is
    (FreeRDP takes a UPN). A domain set in the site's settings wins.
    """
    if "\\" in username:
        given_domain, _, user = username.partition("\\")
        return user, domain or given_domain
    return username, domain


def guacd_params(profile: dict, hostname: str, port: int) -> dict:
    """The connect parameters, minus the certificate ones."""
    options = rdp_options(profile)
    username, domain = split_login(profile.get("username") or "", options["domain"])
    look = rdp_look(options)
    params = {
        "hostname": hostname,
        "port": str(port),
        "username": username,
        "password": profile.get("password") or "",
        "domain": domain,
        "security": options["security"],
        "resize-method": "" if options["resize"] == "none" else options["resize"],
        "server-layout": options["keyboard"],
        "console": "true" if options["console"] else "",
        "disable-audio": "" if options["audio"] else "true",
        # Performance (#74): colour depth and the effects that cost bandwidth.
        "color-depth": look["color_depth"],
        **{param: "true" if look[key] else "" for key, param, _label in RDP_EFFECTS},
        # Guacamole's own file channel stays shut: files move only through
        # IguanaXterm's Files, where the caps and one-way switches live (#76).
        "disable-download": "true",
        "disable-upload": "true",
        # Printing (#77): guacd's Ghostscript turns the job into a PDF and
        # streams it to the pane, which downloads it.
        "enable-printing": "true" if options["printing"] else "",
        "printer-name": PRINTER_NAME if options["printing"] else "",
    }
    if options["gateway_host"]:
        # The gateway's login is the desktop's unless the site says otherwise.
        if options["gateway_same_login"]:
            gw_user, gw_domain = username, domain
            gw_password = profile.get("password") or ""
        else:
            gw_user, gw_domain = split_login(options["gateway_username"],
                                             options["gateway_domain"])
            gw_password = profile.get("gateway_password") or ""
        params.update({
            "gateway-hostname": options["gateway_host"],
            "gateway-port": str(options["gateway_port"]),
            "gateway-domain": gw_domain,
            "gateway-username": gw_user,
            "gateway-password": gw_password,
        })
    return params


def _clamp(value, low: int, high: int, default: int) -> int:
    try:
        return max(low, min(high, int(float(value))))
    except (TypeError, ValueError):
        return default


# ── The SSH tunnel (#60) ─────────────────────────────────────────────────────


class _OneShotTunnel:
    """
    A local listener guacd dials once, piped to an SSH ``direct-tcpip`` channel.

    It accepts **one** connection, only from guacd's address, within
    ``_TUNNEL_ACCEPT_SECONDS``; then the listener is closed. Everything is
    torn down with :meth:`close`.
    """

    def __init__(self, client, target: tuple[str, int], bind_host: str, allowed_peer: str) -> None:
        self.client = client
        self.target = target
        self.allowed_peer = allowed_peer
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind((bind_host, 0))
        self.listener.listen(1)
        self.listener.settimeout(_TUNNEL_ACCEPT_SECONDS)
        self.address = self.listener.getsockname()
        self._closed = threading.Event()
        self._thread = threading.Thread(target=self._serve, name="rdp-tunnel", daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        conn = channel = None
        try:
            while True:
                conn, peer = self.listener.accept()
                if peer[0] == self.allowed_peer or self.allowed_peer == "*":
                    break
                logger.warning("rdp tunnel: refused a connection from %s", peer[0])
                conn.close()
                conn = None
        except OSError:
            return                           # timed out, or closed before guacd came
        finally:
            try:
                self.listener.close()        # one connection only
            except OSError:
                pass
        try:
            channel = self.client.get_transport().open_channel(
                "direct-tcpip", self.target, ("127.0.0.1", 0), timeout=_CONNECT_TIMEOUT
            )
        except Exception as exc:
            logger.info("rdp tunnel: channel to %s:%s failed: %s", *self.target, exc)
            conn.close()
            return
        self._pipe(conn, channel)

    def _pipe(self, conn: socket.socket, channel) -> None:
        def _one_way(read, write) -> None:
            try:
                while not self._closed.is_set():
                    data = read(_READ_SIZE)
                    if not data:
                        break
                    write(data)
            except Exception:
                pass
            finally:
                self.close()

        conn.settimeout(None)
        channel.settimeout(None)
        self._conn, self._channel = conn, channel
        threading.Thread(target=_one_way, args=(channel.recv, conn.sendall), daemon=True).start()
        _one_way(conn.recv, channel.sendall)

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        for closeable in (self.listener, getattr(self, "_conn", None),
                          getattr(self, "_channel", None), self.client):
            try:
                if closeable is not None:
                    closeable.close()
            except Exception:
                pass


def _open_tunnel_client(profile: dict):
    """The SSH client for a site's tunnel, resolved as the site's owner (#38)."""
    via = int(profile.get("via_session_id") or 0)
    resolved = resolve_site(via, int(profile["user_id"]))
    tunnel = resolved[0] if resolved else None
    if tunnel is None or not session_caps(tunnel.get("type")).get("tunnel"):
        raise guacd.GuacdError(
            "The SSH session this desktop tunnels through no longer exists.", _UNREACHABLE
        )
    try:
        client = ssh_helpers.connect(tunnel, on_learn_host_key=pin_host_key(via))
    except ssh_helpers.HostKeyChanged:
        raise
    except Exception as exc:
        raise guacd.GuacdError(f"SSH login to {tunnel['name']} failed: {exc}", _UNREACHABLE) from exc
    return client, tunnel


# ── The certificate (#61) ────────────────────────────────────────────────────


def _check_certificate(profile: dict, open_stream) -> Optional[str]:
    """
    Read the server's certificate and hold it against the pin.

    Returns the pin to hand guacd, or None for plain RDP security (no
    certificate exists). Pins on first use. Raises ``HostKeyChanged`` for a
    different certificate, or for a pinned server that stops offering TLS
    (a downgrade).
    """
    if rdp_options(profile)["security"] == "rdp":
        return None
    pinned = profile.get("host_key") or ""
    stream = open_stream()
    try:
        seen = rdp_cert.probe(stream, profile.get("host") or "")
    except rdp_cert.NoCertificate:
        if rdp_cert.is_rdp_pin(pinned):
            raise ssh_helpers.HostKeyChanged(profile["host"], pinned, "no TLS offered")
        return None
    except (rdp_cert.CertProbeError, OSError) as exc:
        raise guacd.GuacdError(f"Couldn't check the RDP server's certificate: {exc}",
                               _UNREACHABLE) from exc
    finally:
        try:
            stream.close()
        except Exception:
            pass
    if not pinned:
        pin_host_key(int(profile["id"]))(seen)
    elif pinned != seen:
        raise ssh_helpers.HostKeyChanged(profile["host"], pinned, seen)
    return seen


def _check_gateway(profile: dict) -> str:
    """
    The gateway's certificate against its pin (#75), like the desktop's
    (#61) but over plain TLS: a gateway speaks HTTPS. Pins on first use.
    Returns the pin; raises HostKeyChanged for a different one.
    """
    options = rdp_options(profile)
    host, port = options["gateway_host"], options["gateway_port"]
    try:
        seen = rdp_cert.probe_tls(host, port, timeout=_CONNECT_TIMEOUT)
    except rdp_cert.CertProbeError as exc:
        raise guacd.GuacdError(
            f"Couldn't reach the Remote Desktop Gateway at {host}:{port} ({exc}).",
            _UNREACHABLE,
        ) from exc
    pinned = profile.get("gateway_key") or ""
    if not pinned:
        pin_gateway_key(int(profile["id"]))(seen)
    elif pinned != seen:
        raise ssh_helpers.HostKeyChanged(f"{host} (the gateway)", pinned, seen)
    return seen


def _drive_params(profile: dict, user_id: Optional[int]) -> dict:
    """
    The shared drive (#76), in the connecting person's own folder. Made here,
    by IguanaXterm, so it has IguanaXterm's owner and mode; guacd only uses it.
    """
    if not (user_id and rdp_options(profile)["drive"] and drive.enabled()):
        return {}
    try:
        drive.folder(user_id, profile["id"], create=True)
    except (OSError, ValueError) as exc:
        logger.warning("rdp: no drive for site %s: %s", profile.get("id"), exc)
        return {}
    return {
        "enable-drive": "true",
        "drive-name": drive.DRIVE_NAME,
        "drive-path": drive.guacd_folder(user_id, profile["id"]),
        "create-drive-path": "",
    }


def _prepare_gateway(profile: dict, width: int, height: int, dpi: int,
                     user_id: Optional[int] = None):
    """
    A desktop behind a Remote Desktop Gateway (#75). guacd reaches it through
    the gateway, so IguanaXterm can't read the desktop's certificate itself:

    - the gateway's certificate is pinned, and guacd is told to accept exactly
      it (FreeRDP checks ``cert-fingerprints`` for the gateway's TLS too);
    - the desktop's is checked only if the site has its thumbprint. Without
      one guacd must ignore certificates, and then the gateway's pin is
      enforced only by the check just before connecting.
    """
    options = rdp_options(profile)
    if int(profile.get("via_session_id") or 0):
        raise guacd.GuacdError(
            "This desktop is set to use both a Remote Desktop Gateway and an SSH tunnel. "
            "Edit it and choose Directly.", _UNREACHABLE,
        )
    gateway_pin = _check_gateway(profile)
    params = guacd_params(profile, profile.get("host") or "", int(profile.get("port") or 3389))
    prints = [rdp_cert.guacd_fingerprint(gateway_pin)]
    if options["desktop_thumbprint"]:
        prints.append(options["desktop_thumbprint"])
    params["cert-fingerprints"] = ",".join(prints)
    params["ignore-cert"] = "false" if options["desktop_thumbprint"] else "true"
    params.update(_drive_params(profile, user_id))
    audio = AUDIO_TYPES if options["audio"] else ()
    sock, _connection_id, splitter, trailing = guacd.handshake(
        params, width, height, dpi, audio=audio
    )
    return sock, splitter, trailing, None


# ── The route ────────────────────────────────────────────────────────────────


def _prepare(profile: dict, width: int, height: int, dpi: int,
             user_id: Optional[int] = None):
    """
    Everything before the browser is involved: the tunnel, the certificate,
    and the guacd handshake. Runs on a worker thread (it blocks).
    Returns (socket, splitter, trailing instructions, tunnel or None).
    """
    if rdp_options(profile)["gateway_host"]:
        return _prepare_gateway(profile, width, height, dpi, user_id)
    host = profile.get("host") or ""
    port = int(profile.get("port") or 3389)
    tunnel = None
    client = None
    try:
        if int(profile.get("via_session_id") or 0):
            client, tunnel_site = _open_tunnel_client(profile)

            def _channel():
                try:
                    return client.get_transport().open_channel(
                        "direct-tcpip", (host, port), ("127.0.0.1", 0), timeout=_CONNECT_TIMEOUT
                    )
                except Exception as exc:
                    raise guacd.GuacdError(
                        f"{tunnel_site['name']} couldn't reach {host}:{port}. Is the RDP "
                        f"server running there? ({exc})", _UNREACHABLE,
                    ) from exc

            pin = _check_certificate(profile, _channel)
            guacd_host = guacd.address()[0]
            bind = guacd.local_address_towards(guacd_host)
            peer = "127.0.0.1" if bind == "127.0.0.1" else _resolve(guacd_host)
            tunnel = _OneShotTunnel(client, (host, port), bind, peer)
            client = None                     # the tunnel owns it now
            dial_host, dial_port = tunnel.address
        else:
            def _socket():
                try:
                    return socket.create_connection((host, port), timeout=_CONNECT_TIMEOUT)
                except OSError as exc:
                    raise guacd.GuacdError(
                        f"Could not reach the RDP server at {host}:{port} ({exc}).", _UNREACHABLE
                    ) from exc

            pin = _check_certificate(profile, _socket)
            dial_host, dial_port = host, port

        params = guacd_params(profile, dial_host, dial_port)
        params.update(_drive_params(profile, user_id))
        if pin:
            params["cert-fingerprints"] = rdp_cert.guacd_fingerprint(pin)
            params["ignore-cert"] = "false"
        elif rdp_options(profile)["security"] != "rdp":
            # The server chose plain RDP security: nothing to verify.
            params["ignore-cert"] = "true"
        audio = AUDIO_TYPES if rdp_options(profile)["audio"] else ()
        sock, _connection_id, splitter, trailing = guacd.handshake(
            params, width, height, dpi, audio=audio
        )
        return sock, splitter, trailing, tunnel
    except BaseException:
        if tunnel is not None:
            tunnel.close()
        if client is not None:
            client.close()
        raise


def _resolve(host: str) -> str:
    try:
        return socket.getaddrinfo(host, 4822, socket.AF_INET)[0][4][0]
    except OSError:
        return "*"


# guacd codes worth a pointer to the gateway when there is one (#75):
# unreachable, refused, unavailable, and a refused login.
_GATEWAY_HINT_CODES = {"519", "516", "520", "769"}


def with_gateway_hint(message: str, code: str, gateway: str) -> str:
    """Point at the gateway when a failure may be its doing (#75)."""
    if gateway and code in _GATEWAY_HINT_CODES and "Gateway" not in message:
        return (f"{message} This desktop is reached through the Remote Desktop Gateway "
                f"{gateway}: check its address and login too.")
    return message


def _readable(raw: str, elements: list[str], gateway: str = "") -> str:
    """guacd's "error" put in words for the page (guac.status_message)."""
    if elements and elements[0] == "error":
        code = elements[2] if len(elements) > 2 else _UPSTREAM_ERROR
        text = elements[1] if len(elements) > 1 else ""
        message = with_gateway_hint(guac.status_message(code, text), code, gateway)
        return guac.encode("error", message, code)
    return raw


async def _refuse(ws: WebSocket, message: str, code: str) -> None:
    """Tell guacamole-common-js why, the way guacd would, then close."""
    try:
        await ws.send_text(guac.tunnel_uuid(str(uuid.uuid4())) + guac.encode("error", message, code))
        await ws.close()
    except Exception:
        pass


@router.websocket("/ws/rdp/{session_id}")
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
    if not guacd.enabled():
        await websocket.close(code=CLOSE_NOT_FOUND)
        return
    # The one access check (#38): owned, or assigned through a site list.
    resolved = resolve_site(int(session_id), user_id)
    if resolved is None or resolved[0].get("type") != "rdp":
        await websocket.close(code=CLOSE_NOT_FOUND)
        return
    profile, access = resolved

    offered = websocket.scope.get("subprotocols") or []
    await websocket.accept(subprotocol="guacamole" if "guacamole" in offered else None)

    query = parse_qs(websocket.scope.get("query_string", b"").decode())
    width = _clamp((query.get("w") or [None])[0], 320, 8192, 1024)
    height = _clamp((query.get("h") or [None])[0], 240, 8192, 768)
    dpi = _clamp((query.get("dpi") or [None])[0], 48, 384, 96)

    loop = asyncio.get_running_loop()
    try:
        sock, splitter, trailing, tunnel = await loop.run_in_executor(
            None, _prepare, profile, width, height, dpi, user_id
        )
    except ssh_helpers.HostKeyChanged as exc:
        await _refuse(websocket, host_key_message(exc, access), _FORBIDDEN)
        return
    except guacd.GuacdError as exc:
        await _refuse(websocket, with_gateway_hint(
            exc.message, exc.code, rdp_options(profile)["gateway_host"]), exc.code)
        return
    except Exception as exc:
        logger.exception("rdp: connecting failed")
        await _refuse(websocket, f"Could not connect: {exc}", _UPSTREAM_ERROR)
        return

    stop = threading.Event()
    gateway = rdp_options(profile)["gateway_host"]

    def _close_upstream() -> None:
        stop.set()
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass
        if tunnel is not None:
            tunnel.close()

    async def _revoke(reason: str) -> None:
        try:
            await websocket.send_text(guac.encode("error", reason, _FORBIDDEN))
            await websocket.close(code=CLOSE_FORBIDDEN)
        except Exception:
            pass
        _close_upstream()

    try:
        await websocket.send_text(guac.tunnel_uuid(str(uuid.uuid4())) + "".join(trailing))
    except Exception:
        _close_upstream()
        return
    sock.settimeout(None)

    def _pump() -> None:
        """guacd -> browser, whole instructions per message, on a worker thread."""
        # guacd sent "error" or "disconnect" before closing. Instructions that
        # came in right behind "ready" went out with the tunnel uuid, so count them.
        said_why = any(elements and elements[0] in _GUACD_ENDINGS
                       for _raw, elements in guac.Splitter().feed("".join(trailing)))
        try:
            while not stop.is_set():
                data = sock.recv(_READ_SIZE)
                if not data:
                    break
                ready = splitter.feed(data)
                if ready:
                    said_why = said_why or any(
                        elements and elements[0] in _GUACD_ENDINGS for _raw, elements in ready)
                    text = "".join(_readable(raw, elements, gateway) for raw, elements in ready)
                    asyncio.run_coroutine_threadsafe(websocket.send_text(text), loop).result()
        except Exception as exc:
            logger.debug("rdp pump ended: %s", exc)
        finally:
            if not stop.is_set():
                if not said_why:
                    # guacd went away without a word: its session process died
                    # (#87). Say so, or the page shows a frozen picture and
                    # then a bare "disconnected".
                    logger.warning("rdp: guacd ended session %s without a reason", session_id)
                asyncio.run_coroutine_threadsafe(_end(None if said_why else _LOST), loop)

    async def _end(reason: Optional[str]) -> None:
        try:
            if reason:
                await websocket.send_text(guac.encode("error", reason, _UPSTREAM_ERROR))
            await websocket.close()
        except Exception:
            pass

    pump = threading.Thread(target=_pump, name="rdp-pump", daemon=True)
    pump.start()
    token = live.register(
        user_id, int(session_id), "desktop",
        lambda reason: asyncio.run_coroutine_threadsafe(_revoke(reason), loop),
    )

    inbound = guac.Splitter()
    last_nop = 0.0
    try:
        while True:
            message = await websocket.receive_text()
            outgoing = []
            for raw, elements in inbound.feed(message):
                opcode = elements[0]
                if opcode == "":
                    # The tunnel's own keepalive: answer it here, guacd never sees it.
                    if len(elements) > 1 and elements[1] == "ping":
                        await websocket.send_text(raw)
                        # ...but guacd hears a "nop" now and then. Its main
                        # process relays between us and the session's process,
                        # and notices that one died only when it has something
                        # to pass on. An idle page sends nothing, so a crashed
                        # session froze the picture until the next mouse move
                        # (#87). This makes the drop show within seconds.
                        now = time.monotonic()
                        if now - last_nop >= _GUACD_NOP_SECONDS:
                            last_nop = now
                            outgoing.append(guac.encode("nop"))
                    continue
                if opcode in guac.CLIENT_FORBIDDEN:
                    logger.warning("rdp: dropped a client %r after the handshake", opcode)
                    continue
                outgoing.append(raw)
            if outgoing:
                await loop.run_in_executor(None, sock.sendall, "".join(outgoing).encode())
    except (WebSocketDisconnect, RuntimeError):
        pass
    except guac.ProtocolError as exc:
        logger.info("rdp: bad instruction from the browser: %s", exc)
    except Exception as exc:
        logger.debug("rdp relay ended: %s", exc)
    finally:
        live.unregister(token)
        _close_upstream()
        try:
            await websocket.close()
        except Exception:
            pass
