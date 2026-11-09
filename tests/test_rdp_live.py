"""
The RDP relay against a real guacd and the xrdp target (#57, #60, #61).

Skipped unless both are running (tests/smoke/README.md, "RDP smoke"):

    IX_GUACD=127.0.0.1:4823 IX_RDP=127.0.0.1:3389 IX_RDP_SSH=127.0.0.1:2224 \\
        uv run --with pytest python -m pytest tests/test_rdp_live.py -q

IX_RDP_SSH is the target's own sshd; through it, 127.0.0.1:3390 is an xrdp
port that is reachable only from inside the target.
"""
from __future__ import annotations

import asyncio
import os
import socket

import pytest

from _asgi import WebSocket
from _world import svc, world  # noqa: F401  (fixture)
from services import guac
from test_rdp import rdp_app


def _up(address: str) -> bool:
    try:
        host, port = address.rsplit(":", 1)
        socket.create_connection((host, int(port)), timeout=1).close()
        return True
    except (OSError, ValueError):
        return False


GUACD = os.environ.get("IX_GUACD", "")
RDP = os.environ.get("IX_RDP", "")
RDP_SSH = os.environ.get("IX_RDP_SSH", "")

pytestmark = pytest.mark.skipif(
    not (GUACD and RDP and _up(GUACD) and _up(RDP)),
    reason="needs a guacd (IX_GUACD) and the xrdp target (IX_RDP)",
)


def _draws(app, site: int, seconds: float = 20) -> tuple[set, str]:
    """Connect through the relay and collect opcodes until something is drawn."""

    async def _go():
        seen, text = set(), ""
        async with WebSocket(app, f"/ws/rdp/{site}?w=1024&h=768&dpi=96") as ws:
            splitter = guac.Splitter()
            loop = asyncio.get_running_loop()
            end = loop.time() + seconds
            while loop.time() < end:
                frame = await ws.receive(timeout=seconds, raw=True)
                text += frame[:2000]
                for _raw, elements in splitter.feed(frame):
                    seen.add(elements[0])
                    if elements[0] == "sync":
                        await ws.to_app.put({"type": "websocket.receive",
                                             "text": guac.encode("sync", elements[1])})
                    if elements[0] == "error":
                        return seen, text
                if {"img", "cfill"} & seen:
                    return seen, text
        return seen, text

    return asyncio.run(_go())


@pytest.fixture()
def live(world, monkeypatch):
    monkeypatch.setenv("GANXTERM_GUACD", GUACD)
    return world


def _pin(db, site):
    with db.get_db() as conn:
        return conn.execute("SELECT host_key FROM sessions WHERE id = ?", (site,)).fetchone()[0]


def test_the_relay_draws_a_real_desktop_and_pins_its_certificate(live):
    from services.session_service import SessionService

    db, ids = live
    host, port = RDP.rsplit(":", 1)
    site = svc(SessionService, ids["pam"], "power").save(
        name="xrdp", host=host, port=int(port), session_type="rdp",
        username="testuser", password="testpass")["id"]
    seen, text = _draws(rdp_app(ids["pam"], "power"), site)
    assert {"img", "cfill"} & seen, text[:500]
    assert "testpass" not in text
    assert _pin(db, site).startswith("rdp-sha256:")
    # Connecting again with the pin in place still works.
    seen, _ = _draws(rdp_app(ids["pam"], "power"), site)
    assert {"img", "cfill"} & seen


def test_a_wrong_pin_is_refused_by_us_before_guacd(live):
    from services.session_service import SessionService

    db, ids = live
    host, port = RDP.rsplit(":", 1)
    site = svc(SessionService, ids["pam"], "power").save(
        name="xrdp", host=host, port=int(port), session_type="rdp",
        username="testuser", password="testpass")["id"]
    with db.get_db() as conn:
        conn.execute("UPDATE sessions SET host_key = ? WHERE id = ?", ("rdp-sha256:" + "00" * 32, site))
    seen, text = _draws(rdp_app(ids["pam"], "power"), site, seconds=10)
    assert "error" in seen and "has changed" in text


@pytest.mark.skipif(not (RDP_SSH and _up(RDP_SSH)), reason="needs the target's sshd (IX_RDP_SSH)")
def test_the_tunnel_reaches_an_rdp_port_only_ssh_can_see(live):
    from services.session_service import SessionService

    db, ids = live
    ssh_host, ssh_port = RDP_SSH.rsplit(":", 1)
    pam = svc(SessionService, ids["pam"], "power")
    jump = pam.save(name="jump", host=ssh_host, port=int(ssh_port),
                    username="testuser", password="testpass")["id"]
    site = pam.save(name="hidden", host="127.0.0.1", port=3390, session_type="rdp",
                    username="testuser", password="testpass", via_session_id=jump)["id"]
    seen, text = _draws(rdp_app(ids["pam"], "power"), site)
    assert {"img", "cfill"} & seen, text[:500]
    assert _pin(db, site).startswith("rdp-sha256:")
