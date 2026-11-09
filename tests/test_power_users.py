"""Power Users manage the Users they created, and nobody else (#35)."""
from __future__ import annotations

import pytest

ADMIN = {"user_id": 1, "username": "admin", "role": "admin"}


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


def svc(user_id: int, role: str):
    from services.user_service import UserService

    return UserService({"user_id": user_id, "username": f"u{user_id}", "role": role})


def row(db, user_id: int):
    with db.get_db() as conn:
        return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


@pytest.fixture()
def world(db):
    """Two Power Users, each with a User; one User made by the Admin."""
    admin = svc(1, "admin")
    pam = admin.create("pam", "pams-password", "power")["id"]
    pete = admin.create("pete", "petes-password", "power")["id"]
    ann = admin.create("ann", "anns-password", "user")["id"]
    pam_svc = svc(pam, "power")
    ula = pam_svc.create("ula", "ulas-password")["id"]
    vic = svc(pete, "power").create("vic", "vics-password")["id"]
    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO sessions (user_id, name, host) VALUES (?, 'pam-box', 'pam.lan')", (pam,)
        )
    return {"admin": admin, "pam": pam, "pete": pete, "ann": ann, "ula": ula, "vic": vic,
            "pam_svc": pam_svc}


def test_a_power_user_creates_users_they_own(db, world):
    ula = row(db, world["ula"])
    assert (ula["role"], ula["created_by"], ula["must_change_pw"]) == ("user", world["pam"], 1)


@pytest.mark.parametrize("target_role", ["power", "admin", "root"])
def test_a_power_user_cannot_create_anything_but_a_user(db, world, target_role):
    result = world["pam_svc"].create("climber", "climbers-pw", target_role)
    assert not result["ok"] and "role" in result["errors"]
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM users WHERE username = 'climber'").fetchone()[0] == 0


def test_a_power_user_lists_only_their_own_users(world):
    names = [user["username"] for user in world["pam_svc"].list()]
    assert names == ["ula"]


def test_an_admin_lists_everyone_with_who_made_them(world):
    rows = {user["username"]: user for user in world["admin"].list()}
    assert set(rows) == {"admin", "pam", "pete", "ann", "ula", "vic"}
    assert rows["ula"]["created_by_name"] == "pam"
    assert rows["admin"]["created_by_name"] == ""


@pytest.mark.parametrize("target", ["vic", "pete", "ann", "admin"])
def test_a_power_user_cannot_reach_other_accounts_by_id(db, world, target):
    pam = world["pam_svc"]
    target_id = 1 if target == "admin" else world[target]
    before = row(db, target_id)["pw_hash"]
    for result in (
        pam.reset_password(target_id, "hijacked-password"),
        pam.delete(target_id),
        pam.delete_preview(target_id),
    ):
        assert result == {"ok": False, "error": "User not found"}
    assert row(db, target_id)["pw_hash"] == before


def test_a_power_user_resets_and_deletes_their_own_user(db, world):
    pam = world["pam_svc"]
    assert pam.reset_password(world["ula"], "a-new-password")["ok"]
    assert row(db, world["ula"])["must_change_pw"] == 1
    assert pam.delete_preview(world["ula"])["ok"]
    assert pam.delete(world["ula"])["ok"]
    assert row(db, world["ula"]) is None


def test_only_admins_change_roles(db, world):
    pam = world["pam_svc"]
    assert pam.set_role(world["ula"], "power") == {"ok": False, "error": "Not allowed"}
    assert row(db, world["ula"])["role"] == "user"


def test_a_power_user_who_became_a_user_loses_control_at_once(db, world):
    # The session still says "power"; the database is what counts.
    world["admin"].set_role(world["pam"], "user")
    pam = world["pam_svc"]
    assert pam.list() == []
    assert not pam.create("late", "late-password")["ok"]
    assert pam.delete(world["ula"])["error"] == "User not found"


def test_deleting_a_power_user_moves_their_users_to_the_admin(db, world):
    admin = world["admin"]
    preview = admin.delete_preview(world["pam"])
    assert (preview["sites"], preview["users"], preview["role"]) == (1, 1, "power")
    assert admin.delete(world["pam"])["ok"]
    assert row(db, world["ula"])["created_by"] == 1
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions WHERE host = 'pam.lan'").fetchone()[0] == 0


def test_a_user_cannot_manage_anyone(db, world):
    ula = svc(world["ula"], "user")
    assert ula.list() == []
    assert not ula.create("x", "x-password-1")["ok"]
    assert ula.delete(world["vic"])["error"] == "User not found"


def test_nobody_deletes_themselves(world):
    assert "own account" in world["pam_svc"].delete(world["pam"])["error"]
