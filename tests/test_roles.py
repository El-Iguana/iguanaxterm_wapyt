"""Roles (#34): the migration, login claims, min_role, and the last-admin guards."""
from __future__ import annotations

import io
import sqlite3

import pytest

from services import roles
from services.auth import authenticate, policy_hook

ADMIN = {"user_id": 1, "username": "admin", "role": "admin", "is_admin": True}


@pytest.fixture()
def db(tmp_path, monkeypatch):
    from services import db

    monkeypatch.delenv("GANXTERM_ADMIN_PASS", raising=False)
    monkeypatch.delenv("GANXTERM_ADMIN_USER", raising=False)
    saved = (db.DATA_DIR, db.DB_PATH, db.KEY_PATH, db.SESSION_KEY_PATH, db._fernet)
    db.DATA_DIR, db.DB_PATH = tmp_path, tmp_path / "iguanaxterm.db"
    db.KEY_PATH, db.SESSION_KEY_PATH, db._fernet = tmp_path / "k", tmp_path / "s", None
    yield db
    db.DATA_DIR, db.DB_PATH, db.KEY_PATH, db.SESSION_KEY_PATH, db._fernet = saved


def role(db, username: str) -> tuple:
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT role, is_admin FROM users WHERE username = ?", (username,)
        ).fetchone()
    return row["role"], row["is_admin"]


def make_22x_database(db) -> None:
    """The users and sessions tables as 2.2.x left them, with one saved site."""
    db.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db.DB_PATH)
    conn.executescript(
        """CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            pw_hash TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0,
            must_change_pw INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL, folder TEXT NOT NULL DEFAULT '',
            type TEXT NOT NULL DEFAULT 'ssh', host TEXT NOT NULL,
            port INTEGER NOT NULL DEFAULT 22, username TEXT NOT NULL DEFAULT '',
            password TEXT NOT NULL DEFAULT '', private_key TEXT NOT NULL DEFAULT '',
            passphrase TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',
            host_key TEXT NOT NULL DEFAULT '', via_session_id INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')));"""
    )
    conn.executemany(
        "INSERT INTO users (username, pw_hash, is_admin) VALUES (?, ?, ?)",
        [
            ("admin", db.hash_password("admins-own-pw"), 1),
            ("carol", db.hash_password("carols-own-pw"), 0),
        ],
    )
    conn.execute(
        "INSERT INTO sessions (user_id, name, host, username) VALUES (2, 'box', 'box.lan', 'carol')"
    )
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------
# roles.py
# --------------------------------------------------------------------------


def test_each_role_claims_itself_and_everything_below():
    assert roles.roles_claim("admin") == ["admin", "power", "user"]
    assert roles.roles_claim("power") == ["power", "user"]
    assert roles.roles_claim("user") == ["user"]
    assert roles.roles_claim("root") == []


def test_a_session_from_before_roles_gets_no_new_rights():
    assert roles.role_of({"user_id": 1, "is_admin": True}) == "admin"
    assert roles.role_of({"user_id": 2, "is_admin": False}) == "user"
    assert roles.role_of({"user_id": 2}) == "user"
    assert roles.role_of({"user_id": 2, "role": "bogus", "is_admin": False}) == "user"


# --------------------------------------------------------------------------
# Migration
# --------------------------------------------------------------------------


def test_a_22x_database_keeps_admins_and_makes_everyone_else_a_power_user(db):
    make_22x_database(db)
    db.init_db()
    assert role(db, "admin") == ("admin", 1)
    assert role(db, "carol") == ("power", 0)
    # Carol's site is untouched and still hers.
    from services.access import resolve_site

    site, access = resolve_site(1, 2)
    assert site["host"] == "box.lan" and access["via"] == "owner"
    # A second start changes nothing.
    db.init_db()
    assert role(db, "carol") == ("power", 0)


def test_the_migration_adds_created_by_empty(db):
    make_22x_database(db)
    db.init_db()
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM users WHERE created_by IS NULL").fetchone()[0] == 2


def test_a_fresh_install_seeds_an_admin(db):
    db.init_db()
    assert role(db, "admin") == ("admin", 1)


def test_the_database_refuses_an_unknown_role(db):
    db.init_db()
    with pytest.raises(sqlite3.IntegrityError):
        with db.get_db() as conn:
            conn.execute("UPDATE users SET role = 'root' WHERE username = 'admin'")


def test_the_database_refuses_an_unknown_role_after_migration(db):
    make_22x_database(db)
    db.init_db()
    with pytest.raises(sqlite3.IntegrityError):
        with db.get_db() as conn:
            conn.execute("UPDATE users SET role = 'root' WHERE username = 'carol'")


# --------------------------------------------------------------------------
# Login claims
# --------------------------------------------------------------------------


@pytest.mark.parametrize("which", ["admin", "power", "user"])
def test_login_claims_per_role(db, which):
    from services.user_service import UserService

    db.init_db()
    assert UserService(ADMIN).create("dana", "danas-password", role=which)["ok"]
    claims = authenticate("dana", "danas-password")
    assert claims["role"] == which
    assert claims["roles"] == roles.roles_claim(which)
    assert claims["is_admin"] is (which == "admin")
    assert authenticate("dana", "wrong-password") is None


def test_login_claims_after_migration(db):
    make_22x_database(db)
    db.init_db()
    assert authenticate("carol", "carols-own-pw")["roles"] == ["power", "user"]
    assert authenticate("admin", "admins-own-pw")["roles"] == ["admin", "power", "user"]


# --------------------------------------------------------------------------
# policy_hook
# --------------------------------------------------------------------------


def caller(which: str) -> dict:
    return {"user_id": 7, "role": which, "is_admin": which == "admin"}


@pytest.mark.parametrize("who", roles.ROLES)
@pytest.mark.parametrize("minimum", roles.ROLES)
def test_min_role_for_every_pair(who, minimum):
    allowed = roles.ROLES.index(who) >= roles.ROLES.index(minimum)
    assert policy_hook(caller(who), {"min_role": minimum}) is allowed


@pytest.mark.parametrize("who", roles.ROLES)
def test_admin_required_still_means_admin(who):
    assert policy_hook(caller(who), {"admin_required": True}) is (who == "admin")


def test_an_unknown_min_role_denies_everyone():
    for who in roles.ROLES:
        assert policy_hook(caller(who), {"min_role": "superuser"}) is False


def test_no_user_is_denied():
    assert policy_hook({}, {}) is False
    assert policy_hook({"role": "admin"}, {"min_role": "user"}) is False


def test_an_old_admin_session_still_passes():
    assert policy_hook({"user_id": 1, "is_admin": True}, {"min_role": "admin"}) is True
    assert policy_hook({"user_id": 2, "is_admin": False}, {"min_role": "power"}) is False


# --------------------------------------------------------------------------
# UserService
# --------------------------------------------------------------------------


def test_me_reports_the_role(db):
    from services.user_service import UserService

    db.init_db()
    me = UserService({"user_id": 2, "username": "p", "role": "power"}).me()
    assert me["role"] == "power" and "is_admin" not in me


def test_create_records_who_made_the_account(db):
    from services.user_service import UserService

    db.init_db()
    result = UserService(ADMIN).create("erin", "erins-password", role="user")
    with db.get_db() as conn:
        row = conn.execute("SELECT created_by, role FROM users WHERE id = ?", (result["id"],)).fetchone()
    assert (row["created_by"], row["role"]) == (1, "user")


def test_create_defaults_to_user_and_refuses_unknown_roles(db):
    from services.user_service import UserService

    db.init_db()
    admin = UserService(ADMIN)
    assert admin.create("plain", "plain-password")["ok"]
    assert role(db, "plain") == ("user", 0)
    assert admin.create("boss", "boss-password", "admin")["ok"]
    assert role(db, "boss") == ("admin", 1)
    assert admin.create("odd", "odd-password", role="root")["errors"]["role"]


def test_list_returns_roles(db):
    from services.user_service import UserService

    db.init_db()
    admin = UserService(ADMIN)
    admin.create("frank", "franks-password", role="user")
    rows = {row["username"]: row for row in admin.list()}
    assert rows["admin"]["role"] == "admin" and "is_admin" not in rows["admin"]
    assert rows["frank"]["role"] == "user" and rows["frank"]["created_by"] == 1


def test_set_role_and_the_last_admin_guards(db):
    from services.user_service import UserService

    db.init_db()
    admin = UserService(ADMIN)
    gina = admin.create("gina", "ginas-password", role="user")["id"]

    assert admin.set_role(gina, "power")["ok"]
    assert role(db, "gina") == ("power", 0)
    assert admin.set_role(gina, "root")["error"] == "Unknown role"
    assert admin.set_role(999, "user")["error"] == "User not found"

    # You can't demote or delete yourself, so the only Admin stays one. (The
    # last-admin guard behind that is what manage.py set-role relies on.)
    assert "own admin" in admin.set_role(1, "power")["error"]
    assert "own account" in admin.delete(1)["error"]
    # A session that claims Admin counts for nothing once the database disagrees.
    other_admin = UserService({"user_id": gina, "role": "admin", "is_admin": True})
    assert other_admin.set_role(1, "user")["error"] == "Not allowed"
    assert other_admin.delete(1)["error"] == "User not found"

    # With a second admin, the first can be demoted.
    assert admin.set_role(gina, "admin")["ok"]
    assert role(db, "gina") == ("admin", 1)
    assert other_admin.set_role(1, "power")["ok"]
    assert role(db, "admin") == ("power", 0)
    assert other_admin.set_role(1, "admin")["ok"]
    assert other_admin.set_role(gina, "user")["error"].startswith("You cannot")
    assert admin.set_role(gina, "power")["ok"]
    assert role(db, "gina") == ("power", 0)


def test_method_policies():
    from services.user_service import UserService

    original = UserService._pytincture_bff_original
    assert original.set_role._bff_policy == {"min_role": "admin", "roles": {"admin"}}
    for name in ("list", "create", "delete", "delete_preview", "reset_password"):
        policy = getattr(original, name)._bff_policy
        assert policy == {"min_role": "power", "roles": {"power"}}, name
    assert not hasattr(original, "set_admin")


# --------------------------------------------------------------------------
# manage.py
# --------------------------------------------------------------------------


def test_manage_set_role_and_users(db, capsys):
    import manage

    db.init_db()
    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO users (username, pw_hash) VALUES ('hal', ?)", (db.hash_password("x" * 8),)
        )
    assert manage.main(["set-role", "hal", "power"]) == 0
    assert role(db, "hal") == ("power", 0)
    assert manage.main(["set-role", "admin", "user"]) == 1  # the last admin
    assert manage.main(["set-role", "nobody", "user"]) == 1
    capsys.readouterr()
    assert manage.main(["users"]) == 0
    out = capsys.readouterr().out
    assert "hal" in out and "power" in out and "admin" in out


def test_manage_reset_password_create_makes_an_admin(db, monkeypatch):
    import manage

    db.init_db()
    monkeypatch.setattr("sys.stdin", io.StringIO("ivys-password\n"))
    assert manage.main(["reset-password", "ivy", "--create", "--password-stdin"]) == 0
    assert role(db, "ivy") == ("admin", 1)
