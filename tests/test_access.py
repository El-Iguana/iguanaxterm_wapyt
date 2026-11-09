"""
The one access check (#38): every connection path, owned or assigned sites.

The routes are driven for real through a test client, with a stand-in for
pytincture's session middleware; dialling is stubbed where a test needs to see
past the access check.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI

from _asgi import WebSocket, http, ws_refusal_code
from _world import make_list, svc, world  # noqa: F401  (fixture)

CSRF = "test-csrf-token"


class _FakeSession:
    """Puts a signed-in user in scope, as pytincture's middleware would."""

    def __init__(self, app, user: dict) -> None:
        self.app = app
        self.user = user

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            scope["session"] = {"user": self.user, "csrf_token": CSRF}
        await self.app(scope, receive, send)


def client_for(user_id: int, role: str):
    from services.terminal_ws import router as terminal_router
    from services.transfer import router as transfer_router
    from services.vnc_ws import router as vnc_router

    app = FastAPI()
    for router in (terminal_router, transfer_router, vnc_router):
        app.include_router(router)
    return _FakeSession(app, {"user_id": user_id, "role": role})


def first_message(app, path: str):
    """Open a WebSocket the route accepts and return the first thing it sends."""

    async def _go():
        async with WebSocket(app, path) as ws:
            return await ws.receive()

    return asyncio.run(_go())


class _Dialled(Exception):
    """Raised by the stub dialler: the access check let the call through."""


@pytest.fixture()
def no_dial(monkeypatch):
    """Record dials instead of making them, and learn a host key on the way."""
    from services import ftp, ssh

    dials = []

    def _connect(profile, on_learn_host_key=None, **_kw):
        dials.append(profile["id"])
        if on_learn_host_key:
            on_learn_host_key("ssh-ed25519 FIRSTKEY")
        raise _Dialled(f"dialled {profile['id']}")

    monkeypatch.setattr(ssh, "connect", _connect)
    monkeypatch.setattr(ftp, "connect", _connect)
    return dials


# --------------------------------------------------------------------------
# The rule
# --------------------------------------------------------------------------


def test_the_owner_gets_full_access(world):
    from services.access import resolve_site

    _db, ids = world
    profile, access = resolve_site(ids["pam-ssh"], ids["pam"])
    assert access == {"via": "owner", "files": True, "owner_id": ids["pam"]}
    assert profile["password"] == "s3cret-pw"


def test_nobody_else_without_a_list_admins_included(world):
    from services.access import resolve_site

    _db, ids = world
    for who in ("ula", "una", "vic", "pete", "admin"):
        assert resolve_site(ids["pam-ssh"], ids[who]) is None, who
    assert resolve_site(999, ids["pam"]) is None


def test_a_list_grants_its_members_and_no_one_else(world):
    from services.access import resolve_site

    _db, ids = world
    make_list(ids["pam"], "power", "Lab", [ids["pam-ssh"]], [ids["ula"]])
    profile, access = resolve_site(ids["pam-ssh"], ids["ula"])
    assert access["via"] == "list" and access["files"] is False
    assert profile["host"] == "127.0.0.1"
    assert resolve_site(ids["pam-ssh"], ids["una"]) is None
    assert resolve_site(ids["pam-ftp"], ids["ula"]) is None  # not in the list


def test_files_if_any_list_allows_them(world):
    from services.access import resolve_site

    _db, ids = world
    make_list(ids["pam"], "power", "No files", [ids["pam-ssh"]], [ids["ula"]])
    assert resolve_site(ids["pam-ssh"], ids["ula"])[1]["files"] is False
    make_list(ids["pam"], "power", "Files", [ids["pam-ssh"]], [ids["ula"]], files=True)
    assert resolve_site(ids["pam-ssh"], ids["ula"])[1]["files"] is True


def test_reachable_site_ids(world):
    from services.access import reachable_site_ids

    db, ids = world
    make_list(ids["pam"], "power", "Lab", [ids["pam-ssh"]], [ids["ula"]])
    with db.get_db() as conn:
        assert reachable_site_ids(conn, ids["ula"]) == {ids["pam-ssh"]}
        assert reachable_site_ids(conn, ids["pam"]) == {ids["pam-ssh"], ids["pam-ftp"], ids["pam-vnc"]}
        assert reachable_site_ids(conn, ids["vic"]) == set()


# --------------------------------------------------------------------------
# Every path refuses an unassigned site
# --------------------------------------------------------------------------


def test_terminal_ws_refuses_an_unassigned_site(world, no_dial):
    _db, ids = world
    client = client_for(ids["ula"], "user")
    assert ws_refusal_code(client, f"/ws/terminal/{ids['pam-ssh']}") == 4404
    assert no_dial == []


def test_vnc_ws_refuses_an_unassigned_site(world, no_dial):
    _db, ids = world
    client = client_for(ids["ula"], "user")
    assert ws_refusal_code(client, f"/ws/vnc/{ids['pam-vnc']}") == 4404
    assert no_dial == []


def test_sftp_bff_refuses_an_unassigned_site(world, no_dial):
    from services.sftp_service import SFTPService

    _db, ids = world
    sftp = svc(SFTPService, ids["ula"], "user")
    assert sftp.list_dir(ids["pam-ssh"])["error"] == "Session not found"
    assert sftp.retain(ids["pam-ssh"]) == {"ok": False, "error": "Session not found"}
    assert no_dial == []


@pytest.mark.parametrize("method,path", [
    ("get", "/files/{id}/download?path=/etc/hostname"),
    ("post", "/files/{id}/upload"),
])
def test_file_routes_refuse_an_unassigned_site(world, no_dial, method, path):
    _db, ids = world
    client = client_for(ids["ula"], "user")
    url = path.format(id=ids["pam-ssh"])
    status, _body = http(client, method, url, headers={"x-csrf-token": CSRF})
    assert status == 404
    assert no_dial == []


def test_folder_download_refuses_an_unassigned_site(world, no_dial):
    from services.download_jobs import start_job

    _db, ids = world
    with pytest.raises(PermissionError, match="Session not found"):
        start_job({"user_id": ids["ula"], "username": "ula"}, ids["pam-ssh"], "/etc")
    assert no_dial == []


# --------------------------------------------------------------------------
# allow_files off: files refused, terminal works
# --------------------------------------------------------------------------


def test_without_files_the_file_paths_refuse_and_the_terminal_dials(world, no_dial):
    from services.download_jobs import start_job
    from services.sftp_service import SFTPService

    _db, ids = world
    make_list(ids["pam"], "power", "Lab", [ids["pam-ssh"]], [ids["ula"]])
    client = client_for(ids["ula"], "user")
    refusal = "The file browser isn't available on this site"

    assert svc(SFTPService, ids["ula"], "user").list_dir(ids["pam-ssh"])["error"] == refusal
    for method, path in (("get", "/files/{id}/download?path=/x"), ("post", "/files/{id}/upload")):
        status, body = http(client, method, path.format(id=ids["pam-ssh"]),
                            headers={"x-csrf-token": CSRF})
        assert status == 403 and body["detail"] == refusal
    with pytest.raises(PermissionError, match="file browser"):
        start_job({"user_id": ids["ula"], "username": "ula"}, ids["pam-ssh"], "/etc")
    assert no_dial == []

    # The terminal gets through the access check and dials.
    message = first_message(client, f"/ws/terminal/{ids['pam-ssh']}")
    assert message["type"] == "error" and "dialled" in message["data"]
    assert no_dial == [ids["pam-ssh"]]


def test_with_files_the_file_browser_dials(world, no_dial):
    from services.sftp_service import SFTPService

    _db, ids = world
    make_list(ids["pam"], "power", "Lab", [ids["pam-ssh"]], [ids["ula"]], files=True)
    result = svc(SFTPService, ids["ula"], "user").list_dir(ids["pam-ssh"])
    assert "dialled" in result["error"] and no_dial == [ids["pam-ssh"]]


def test_a_shared_desktop_tunnels_as_its_owner(world, no_dial):
    # ula has pam-vnc but not the pam-ssh it tunnels through, and needn't.
    _db, ids = world
    make_list(ids["pam"], "power", "Desktops", [ids["pam-vnc"]], [ids["ula"]])
    client = client_for(ids["ula"], "user")

    async def _go():
        async with WebSocket(client, f"/ws/vnc/{ids['pam-vnc']}") as ws:
            await ws.receive()                    # the server's RFB greeting
            await ws.send_bytes(b"RFB 003.008\n")
            return await ws.receive()

    refusal = asyncio.run(_go())
    assert no_dial == [ids["pam-ssh"]]            # dialled the owner's tunnel
    assert b"no longer exists" not in refusal


# --------------------------------------------------------------------------
# Host keys on shared sites
# --------------------------------------------------------------------------


def host_key(db, session_id: int) -> str:
    with db.get_db() as conn:
        return conn.execute("SELECT host_key FROM sessions WHERE id = ?", (session_id,)).fetchone()[0]


def test_a_user_pins_a_shared_sites_host_key_on_first_connect(world, no_dial):
    db, ids = world
    make_list(ids["pam"], "power", "Lab", [ids["pam-ssh"]], [ids["ula"]])
    first_message(client_for(ids["ula"], "user"), f"/ws/terminal/{ids['pam-ssh']}")
    assert host_key(db, ids["pam-ssh"]) == "ssh-ed25519 FIRSTKEY"


def test_a_pin_is_never_overwritten(world):
    from services.access import pin_host_key

    db, ids = world
    pin_host_key(ids["pam-ssh"])("ssh-ed25519 ONE")
    pin_host_key(ids["pam-ssh"])("ssh-ed25519 TWO")
    assert host_key(db, ids["pam-ssh"]) == "ssh-ed25519 ONE"


def test_a_changed_key_blocks_the_user_and_sends_them_to_the_owner(world, monkeypatch):
    from services import ssh
    from services.access import SHARED_HOST_KEY_CHANGED
    from services.sftp_service import SFTPService

    _db, ids = world
    make_list(ids["pam"], "power", "Lab", [ids["pam-ssh"]], [ids["ula"]], files=True)

    def _changed(profile, **_kw):
        raise ssh.HostKeyChanged(profile["host"], "ssh-ed25519 OLD", "ssh-ed25519 NEW")

    monkeypatch.setattr(ssh, "connect", _changed)
    assert first_message(client_for(ids["ula"], "user"), f"/ws/terminal/{ids['pam-ssh']}") == {
        "type": "error", "data": SHARED_HOST_KEY_CHANGED}
    result = svc(SFTPService, ids["ula"], "user").list_dir(ids["pam-ssh"])
    assert result["host_key_changed"] and result["error"] == SHARED_HOST_KEY_CHANGED
    # The owner gets the full story, which says how to clear it.
    owner = first_message(client_for(ids["pam"], "power"), f"/ws/terminal/{ids['pam-ssh']}")
    assert "has changed" in owner["data"]


def test_only_the_owner_or_an_admin_clears_a_shared_pin(world):
    from services.access import pin_host_key
    from services.session_service import SessionService

    db, ids = world
    make_list(1, "admin", "Lab", [ids["pam-ssh"]], [ids["ula"], ids["pete"]])
    pin_host_key(ids["pam-ssh"])("ssh-ed25519 ONE")
    assert not svc(SessionService, ids["ula"], "user").clear_host_key(ids["pam-ssh"])["ok"]
    assert not svc(SessionService, ids["pete"], "power").clear_host_key(ids["pam-ssh"])["ok"]
    assert host_key(db, ids["pam-ssh"]) == "ssh-ed25519 ONE"
    assert svc(SessionService, 1, "admin").clear_host_key(ids["pam-ssh"])["ok"]
    assert host_key(db, ids["pam-ssh"]) == ""


# --------------------------------------------------------------------------
# Layouts
# --------------------------------------------------------------------------


def test_a_layout_restores_assigned_sites_and_drops_revoked_ones(world):
    from services.layout_service import LayoutService
    from services.site_list_service import SiteListService

    _db, ids = world
    lab = make_list(ids["pam"], "power", "Lab", [ids["pam-ssh"]], [ids["ula"]])
    layouts = svc(LayoutService, ids["ula"], "user")
    panes = [{"session_id": ids["pam-ssh"]}, {"session_id": ids["pete-ssh"]}]
    assert layouts.save("tabbed", panes)["ok"]
    assert [p["session_id"] for p in layouts.get()["panes"]] == [ids["pam-ssh"]]
    svc(SiteListService, ids["pam"], "power").set_members(lab, [])
    assert layouts.get()["panes"] == []


def test_no_module_reads_credentials_except_access():
    """resolve_site is the only way in: nothing else decrypts a site's secrets."""
    import pathlib
    import re

    services = pathlib.Path(__file__).resolve().parents[1] / "appcode" / "services"
    offenders = [
        path.name for path in services.glob("*.py")
        if path.name not in ("access.py", "db.py") and re.search(r"\bdecrypt\(", path.read_text())
    ]
    assert offenders == []
