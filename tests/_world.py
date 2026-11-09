"""
A small population for the site-list tests (#37-#41).

    admin (Admin)
    pam   (Power User, made by admin)  sites: pam-ssh, pam-ftp, pam-vnc (tunnels via pam-ssh)
    pete  (Power User, made by admin)  sites: pete-ssh
    ula   (User, made by pam)
    una   (User, made by pam)
    vic   (User, made by pete)

No lists exist; each test builds the ones it needs.
"""
from __future__ import annotations

import pytest


def svc(cls, user_id: int, role: str):
    return cls({"user_id": user_id, "username": f"u{user_id}", "role": role})


@pytest.fixture()
def world(tmp_path, monkeypatch):
    from services import db
    from services.session_service import SessionService
    from services.user_service import UserService

    monkeypatch.delenv("GANXTERM_ADMIN_PASS", raising=False)
    monkeypatch.delenv("GANXTERM_ADMIN_USER", raising=False)
    saved = (db.DATA_DIR, db.DB_PATH, db.KEY_PATH, db.SESSION_KEY_PATH, db._fernet)
    db.DATA_DIR, db.DB_PATH = tmp_path, tmp_path / "iguanaxterm.db"
    db.KEY_PATH, db.SESSION_KEY_PATH, db._fernet = tmp_path / "k", tmp_path / "s", None
    db.init_db()

    admin = svc(UserService, 1, "admin")
    ids = {"admin": 1}
    ids["pam"] = admin.create("pam", "pams-password", "power")["id"]
    ids["pete"] = admin.create("pete", "petes-password", "power")["id"]
    pam_users = svc(UserService, ids["pam"], "power")
    ids["ula"] = pam_users.create("ula", "ulas-password")["id"]
    ids["una"] = pam_users.create("una", "unas-password")["id"]
    ids["vic"] = svc(UserService, ids["pete"], "power").create("vic", "vics-password")["id"]

    pam_sites = svc(SessionService, ids["pam"], "power")
    ids["pam-ssh"] = pam_sites.save(
        name="pam-ssh", host="127.0.0.1", port=1, username="root", password="s3cret-pw"
    )["id"]
    ids["pam-ftp"] = pam_sites.save(
        name="pam-ftp", host="127.0.0.1", port=1, session_type="ftp", password="ftp-pw"
    )["id"]
    ids["pam-vnc"] = pam_sites.save(
        name="pam-vnc", host="10.9.9.9", port=5900, session_type="vnc",
        password="vnc-pw", via_session_id=ids["pam-ssh"],
    )["id"]
    ids["pete-ssh"] = svc(SessionService, ids["pete"], "power").save(
        name="pete-ssh", host="127.0.0.1", port=1, password="petes-site-pw"
    )["id"]
    yield db, ids
    db.DATA_DIR, db.DB_PATH, db.KEY_PATH, db.SESSION_KEY_PATH, db._fernet = saved


def make_list(owner_id: int, owner_role: str, name: str, sites=(), members=(), files=False) -> int:
    from services.site_list_service import SiteListService

    lists = svc(SiteListService, owner_id, owner_role)
    list_id = lists.save(name=name, allow_files=files)["id"]
    assert lists.set_items(list_id, list(sites))["ok"]
    assert lists.set_members(list_id, list(members))["ok"]
    return list_id
