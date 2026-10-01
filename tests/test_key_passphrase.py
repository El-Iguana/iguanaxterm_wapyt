"""A private key's passphrase: stored encrypted, never sent back, used to connect."""
from __future__ import annotations

import sqlite3

import paramiko
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

ADMIN = {"user_id": 1, "username": "admin", "is_admin": True}
PASSPHRASE = "correct horse"


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


def encrypted_key(kind: str = "ed25519", passphrase: str = PASSPHRASE) -> str:
    """An OpenSSH-format private key locked with ``passphrase``."""
    key = ed25519.Ed25519PrivateKey.generate() if kind == "ed25519" \
        else rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.BestAvailableEncryption(passphrase.encode()),
    ).decode()


def service():
    from services.session_service import SessionService

    return SessionService(_user=ADMIN)


def stored(db, session_id: int) -> dict:
    with db.get_db() as conn:
        return dict(conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone())


def new_session(**secrets) -> int:
    result = service().save(name="box", host="example.test", **secrets)
    assert result["ok"], result
    return result["id"]


# -- loading the key ------------------------------------------------------------

@pytest.mark.parametrize("kind", ["ed25519", "rsa"])
def test_an_encrypted_key_loads_with_its_passphrase(kind):
    from services.ssh import load_private_key

    assert isinstance(load_private_key(encrypted_key(kind), PASSPHRASE), paramiko.PKey)


@pytest.mark.parametrize("kind", ["ed25519", "rsa"])
def test_no_passphrase_and_a_wrong_one_say_which(kind):
    from services.ssh import load_private_key

    pem = encrypted_key(kind)
    with pytest.raises(paramiko.SSHException, match="needs its passphrase"):
        load_private_key(pem, "")
    with pytest.raises(paramiko.SSHException, match="passphrase for this private key is wrong"):
        load_private_key(pem, "not it")


def test_a_bad_key_is_still_a_format_error_with_a_passphrase():
    from services.ssh import load_private_key

    with pytest.raises(paramiko.SSHException, match="Unrecognised private key format"):
        load_private_key("-----BEGIN OPENSSH PRIVATE KEY-----\nnope\n-----END OPENSSH PRIVATE KEY-----\n",
                         "anything")


# -- storing it -------------------------------------------------------------------

def test_the_passphrase_is_encrypted_at_rest_and_decrypted_to_connect(db):
    from services.db import fetch_session

    db.init_db()
    pem = encrypted_key()
    session_id = new_session(private_key=pem, passphrase=PASSPHRASE)
    assert stored(db, session_id)["passphrase"].startswith("fernet:")
    profile = fetch_session(session_id, ADMIN["user_id"])
    assert profile["passphrase"] == PASSPHRASE
    from services.ssh import load_private_key

    load_private_key(profile["private_key"], profile["passphrase"])


def test_the_browser_only_learns_that_there_is_one(db):
    db.init_db()
    session_id = new_session(private_key=encrypted_key(), passphrase=PASSPHRASE)
    for row in (service().get(session_id), service().list()[0]):
        assert row["has_passphrase"] is True
        assert "passphrase" not in row and PASSPHRASE not in repr(row)


def test_saving_other_fields_keeps_it(db):
    from services.db import fetch_session

    db.init_db()
    session_id = new_session(private_key=encrypted_key(), passphrase=PASSPHRASE)
    assert service().save(session_id=session_id, name="renamed", host="example.test")["ok"]
    assert fetch_session(session_id, 1)["passphrase"] == PASSPHRASE


def test_a_new_key_drops_the_old_passphrase_unless_it_brings_one(db):
    from services.db import fetch_session

    db.init_db()
    session_id = new_session(private_key=encrypted_key(), passphrase=PASSPHRASE)
    service().save(session_id=session_id, name="box", host="example.test",
                   private_key=encrypted_key(passphrase="second"), passphrase="second")
    assert fetch_session(session_id, 1)["passphrase"] == "second"
    service().save(session_id=session_id, name="box", host="example.test",
                   private_key="-----BEGIN OPENSSH PRIVATE KEY-----\nunlocked\n")
    assert fetch_session(session_id, 1)["passphrase"] == ""
    assert service().get(session_id)["has_passphrase"] is False


def test_clearing_the_key_clears_the_passphrase(db):
    from services.db import fetch_session

    db.init_db()
    session_id = new_session(private_key=encrypted_key(), passphrase=PASSPHRASE)
    service().save(session_id=session_id, name="box", host="example.test", private_key="")
    assert fetch_session(session_id, 1)["passphrase"] == ""


def test_an_older_database_gains_the_column(db):
    """A 2.1.0 database has no passphrase column; starting 2.1.1 adds it."""
    db.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute(
            "CREATE TABLE sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, "
            "name TEXT NOT NULL, host TEXT NOT NULL, port INTEGER NOT NULL DEFAULT 22, "
            "username TEXT NOT NULL DEFAULT '', password TEXT NOT NULL DEFAULT '', "
            "private_key TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '', "
            "created_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        conn.execute("INSERT INTO sessions (user_id, name, host) VALUES (1, 'old', 'example.test')")
    db.init_db()
    row = stored(db, 1)
    assert row["passphrase"] == "" and row["name"] == "old"
