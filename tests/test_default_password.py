"""The first admin's default password and the reminder to change it."""
from __future__ import annotations

import sqlite3

import pytest

ADMIN = {"user_id": 1, "username": "admin", "is_admin": True}


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


def flag(db, username: str) -> int:
    with db.get_db() as conn:
        return conn.execute(
            "SELECT must_change_pw FROM users WHERE username = ?", (username,)
        ).fetchone()["must_change_pw"]


def test_first_admin_gets_change_me_and_is_reminded(db):
    from services.user_service import UserService

    db.init_db()
    with db.get_db() as conn:
        row = conn.execute("SELECT pw_hash FROM users WHERE username = 'admin'").fetchone()
    assert db.verify_password("change_me", row["pw_hash"])
    assert UserService(ADMIN).me()["must_change_password"] is True


def test_a_password_from_the_environment_is_reminded_too(db, monkeypatch):
    monkeypatch.setenv("GANXTERM_ADMIN_PASS", "from-the-env-file")
    db.init_db()
    assert flag(db, "admin") == 1


def test_changing_your_own_password_stops_the_reminder(db):
    from services.user_service import UserService

    db.init_db()
    me = UserService(ADMIN)
    same = me.change_own_password("change_me", "change_me")
    assert not same["ok"] and "new_password" in same["errors"]
    assert me.change_own_password("change_me", "a-better-password")["ok"]
    assert me.me()["must_change_password"] is False


def test_passwords_an_admin_sets_are_reminded(db):
    from services.user_service import UserService

    db.init_db()
    admin = UserService(ADMIN)
    assert admin.create("bob", "temporary-1")["ok"]
    assert flag(db, "bob") == 1
    UserService({"user_id": 2, "username": "bob"}).change_own_password("temporary-1", "bobs-own-pw")
    assert flag(db, "bob") == 0
    assert admin.reset_password(2, "temporary-2")["ok"]
    assert flag(db, "bob") == 1
    # Resetting your own account from the Users panel is choosing it.
    assert admin.reset_password(1, "admins-choice")["ok"]
    assert flag(db, "admin") == 0


def test_manage_reset_password_is_a_choice(db, monkeypatch):
    import io

    import manage

    db.init_db()
    monkeypatch.setattr("sys.stdin", io.StringIO("chosen-at-console\n"))
    assert manage.main(["reset-password", "admin", "--password-stdin"]) == 0
    assert flag(db, "admin") == 0


def test_an_older_database_is_migrated(db):
    # The users table as it was before the flag, with two accounts.
    db.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db.DB_PATH)
    conn.executescript(
        """CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            pw_hash TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')));"""
    )
    conn.executemany(
        "INSERT INTO users (username, pw_hash, is_admin) VALUES (?, ?, 1)",
        [("admin", db.hash_password("changeme")), ("carol", db.hash_password("carols-own"))],
    )
    conn.commit()
    conn.close()

    db.init_db()
    assert flag(db, "admin") == 1  # still on the old default
    assert flag(db, "carol") == 0
