"""Users can't create, edit or delete sites, or clear a pinned host key (#36)."""
from __future__ import annotations

import pytest


@pytest.fixture()
def db(tmp_path, monkeypatch):
    from services import db

    monkeypatch.delenv("GANXTERM_ADMIN_PASS", raising=False)
    monkeypatch.delenv("GANXTERM_ADMIN_USER", raising=False)
    saved = (db.DATA_DIR, db.DB_PATH, db.KEY_PATH, db.SESSION_KEY_PATH, db._fernet)
    db.DATA_DIR, db.DB_PATH = tmp_path, tmp_path / "iguanaxterm.db"
    db.KEY_PATH, db.SESSION_KEY_PATH, db._fernet = tmp_path / "k", tmp_path / "s", None
    db.init_db()
    yield db
    db.DATA_DIR, db.DB_PATH, db.KEY_PATH, db.SESSION_KEY_PATH, db._fernet = saved


def sessions(user_id: int, role: str):
    from services.session_service import SessionService

    return SessionService({"user_id": user_id, "username": f"u{user_id}", "role": role})


def add_account(db, name: str, role: str) -> int:
    with db.get_db() as conn:
        return conn.execute(
            "INSERT INTO users (username, pw_hash, role) VALUES (?, 'x', ?)", (name, role)
        ).lastrowid


def site_rows(db, user_id: int) -> list:
    with db.get_db() as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT name, host, folder, host_key FROM sessions WHERE user_id = ?", (user_id,)
            ).fetchall()
        ]


@pytest.fixture()
def demoted(db):
    """A Power User with a pinned site, then demoted to User by an Admin."""
    uid = add_account(db, "dee", "power")
    sid = sessions(uid, "power").save(name="box", host="box.lan", folder="Lab")["id"]
    with db.get_db() as conn:
        conn.execute("UPDATE sessions SET host_key = 'ssh-ed25519 AAAA' WHERE id = ?", (sid,))
        conn.execute("UPDATE users SET role = 'user' WHERE id = ?", (uid,))
    return uid, sid


def test_a_user_cannot_create_a_site(db):
    uid = add_account(db, "ula", "user")
    result = sessions(uid, "user").save(name="mine", host="mine.lan")
    assert result["ok"] is False and "not change" in result["error"]
    assert site_rows(db, uid) == []


def test_a_user_cannot_edit_delete_or_forget(db, demoted):
    uid, sid = demoted
    before = site_rows(db, uid)
    # The session still says "power": the database is what counts.
    for svc in (sessions(uid, "power"), sessions(uid, "user")):
        assert not svc.save(session_id=sid, name="renamed", host="evil.lan")["ok"]
        assert not svc.delete(sid)["ok"]
        assert not svc.clear_host_key(sid)["ok"]
    assert site_rows(db, uid) == before


def test_a_user_still_sees_their_sites_read_only(db, demoted):
    uid, _ = demoted
    rows = sessions(uid, "user").list()
    assert [(row["name"], row["editable"]) for row in rows] == [("box", False)]
    assert sessions(uid, "user").folders() == []


@pytest.mark.parametrize("role", ["power", "admin"])
def test_power_users_and_admins_still_edit(db, role):
    uid = add_account(db, f"ed-{role}", role)
    svc = sessions(uid, role)
    sid = svc.save(name="box", host="box.lan", folder="Lab")["id"]
    assert [row["editable"] for row in svc.list()] == [True]
    assert svc.folders() == ["Lab"]
    assert svc.save(session_id=sid, name="box2", host="box.lan")["ok"]
    assert svc.clear_host_key(sid)["ok"]
    assert svc.delete(sid)["ok"]
    assert site_rows(db, uid) == []
