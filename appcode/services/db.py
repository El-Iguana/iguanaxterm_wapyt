"""
SQLite storage and Fernet credential encryption. Server-side only.

Saved connection profiles hold SSH passwords and private keys, so they are
encrypted at rest with a key that lives outside the database. Losing
``secret.key`` means losing every stored credential — back it up, or pin it
with ``GANXTERM_SECRET_KEY``.
"""
from __future__ import annotations

import os
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

import bcrypt
from cryptography.fernet import Fernet

DATA_DIR = Path(os.environ.get("GANXTERM_DATA_DIR", Path(__file__).resolve().parents[2]))
DB_PATH = DATA_DIR / "iguanaxterm.db"
KEY_PATH = DATA_DIR / "secret.key"
SESSION_KEY_PATH = DATA_DIR / "session.key"

# Marks a value as encrypted so a database carried over from the original app,
# where credentials were plaintext, keeps working until init_db() rewrites it.
_CRED_PREFIX = "fernet:"

# bcrypt cost. 12 is ~250ms per verify, which is the point, but it also means
# login is the most expensive endpoint in the app — see the rate limiting in
# auth.py.
_BCRYPT_ROUNDS = 12

_fernet: Optional[Fernet] = None
_fernet_lock = threading.Lock()


def _load_fernet() -> Fernet:
    """
    Resolve the encryption key once per process.

    The original reloaded the key file on every encrypt and decrypt; this
    caches it behind a lock so concurrent SFTP threads cannot race on first use.
    """
    global _fernet
    if _fernet is not None:
        return _fernet
    with _fernet_lock:
        if _fernet is not None:
            return _fernet
        env_key = os.environ.get("GANXTERM_SECRET_KEY", "").strip()
        if env_key:
            _fernet = Fernet(env_key.encode())
            return _fernet
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if KEY_PATH.exists():
            _fernet = Fernet(KEY_PATH.read_bytes().strip())
            return _fernet
        key = Fernet.generate_key()
        # Write with the restrictive mode already in place: creating the file
        # world-readable and chmod-ing afterwards leaves a window where the key
        # is exposed.
        fd = os.open(KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, key)
        finally:
            os.close(fd)
        _fernet = Fernet(key)
        return _fernet


def session_secret() -> str:
    """
    The cookie-signing secret for pytincture's session middleware.

    Persisted rather than generated per boot: a fresh secret invalidates every
    signed cookie, so everyone would be logged out on each restart.
    ``GANXTERM_SESSION_SECRET`` overrides it, which is what a multi-replica
    deployment needs so replicas accept each other's cookies.
    """
    from_env = os.environ.get("GANXTERM_SESSION_SECRET", "").strip()
    if from_env:
        return from_env
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if SESSION_KEY_PATH.exists():
        existing = SESSION_KEY_PATH.read_text().strip()
        if existing:
            return existing
    secret = secrets.token_urlsafe(48)
    fd = os.open(SESSION_KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, secret.encode())
    finally:
        os.close(fd)
    return secret


def encrypt(value: str) -> str:
    if not value:
        return ""
    return _CRED_PREFIX + _load_fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    if not value:
        return ""
    if not value.startswith(_CRED_PREFIX):
        return value  # legacy plaintext, rewritten on next save
    return _load_fernet().decrypt(value[len(_CRED_PREFIX):].encode()).decode()


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(_BCRYPT_ROUNDS)).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except (ValueError, TypeError):
        return False


@contextmanager
def get_db() -> Iterator[sqlite3.Connection]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets the SFTP worker threads read while a write is in flight; the
    # original blocked readers on every write.
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    username   TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    pw_hash    TEXT    NOT NULL,
    is_admin   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name        TEXT    NOT NULL,
    folder      TEXT    NOT NULL DEFAULT '',
    type        TEXT    NOT NULL DEFAULT 'ssh',
    host        TEXT    NOT NULL,
    port        INTEGER NOT NULL DEFAULT 22,
    username    TEXT    NOT NULL DEFAULT '',
    password    TEXT    NOT NULL DEFAULT '',
    private_key TEXT    NOT NULL DEFAULT '',
    description TEXT    NOT NULL DEFAULT '',
    host_key    TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
"""


def init_db() -> None:
    """Create the schema, migrate an older database, and seed the first admin."""
    with get_db() as conn:
        conn.executescript(SCHEMA)

        # Columns added after the original 1.x schema.
        existing = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
        for column, definition in (
            ("type", "TEXT NOT NULL DEFAULT 'ssh'"),
            ("folder", "TEXT NOT NULL DEFAULT ''"),
            ("host_key", "TEXT NOT NULL DEFAULT ''"),
        ):
            if column not in existing:
                conn.execute(f"ALTER TABLE sessions ADD COLUMN {column} {definition}")

        if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            admin_user = os.environ.get("GANXTERM_ADMIN_USER", "admin")
            admin_pass = os.environ.get("GANXTERM_ADMIN_PASS", "changeme")
            conn.execute(
                "INSERT INTO users (username, pw_hash, is_admin) VALUES (?, ?, 1)",
                (admin_user, hash_password(admin_pass)),
            )
            banner = "=" * 58
            # The password is deliberately not echoed: the original printed it,
            # which puts it in the container log and anywhere that ships.
            print(
                f"\n{banner}\n"
                f"  Created the initial admin account: {admin_user!r}\n"
                f"  Using GANXTERM_ADMIN_PASS from the environment.\n"
                f"  Change it from the UI before exposing this service.\n"
                f"{banner}\n",
                flush=True,
            )

        _migrate_plaintext_credentials(conn)


def _migrate_plaintext_credentials(conn: sqlite3.Connection) -> None:
    """
    Encrypt any credential still stored as plaintext.

    Unlike the original, this only touches rows that actually need it, so a
    database that has already been migrated costs one indexed scan rather than
    a decrypt/re-encrypt of every row on every boot.
    """
    rows = conn.execute(
        "SELECT id, password, private_key FROM sessions "
        "WHERE (password <> '' AND password NOT LIKE 'fernet:%') "
        "   OR (private_key <> '' AND private_key NOT LIKE 'fernet:%')"
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE sessions SET password = ?, private_key = ? WHERE id = ?",
            (
                encrypt(row["password"]) if row["password"] else "",
                encrypt(row["private_key"]) if row["private_key"] else "",
                row["id"],
            ),
        )
    if rows:
        print(f"  Encrypted credentials for {len(rows)} saved session(s).", flush=True)


def fetch_session(session_id: int, user_id: int) -> Optional[dict]:
    """
    One profile with its credentials decrypted, or ``None``.

    Every caller scopes by ``user_id``; there is no unscoped read of this table
    anywhere in the app, which is what keeps one user's saved hosts out of
    another's reach.
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE id = ? AND user_id = ?",
            (session_id, user_id),
        ).fetchone()
    if not row:
        return None
    session = dict(row)
    session["password"] = decrypt(session.get("password") or "")
    session["private_key"] = decrypt(session.get("private_key") or "")
    return session
