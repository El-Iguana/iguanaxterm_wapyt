"""
BFF: saved connection profiles.

Every query is scoped by the caller's own ``user_id``. There is no unscoped
read of the ``sessions`` table anywhere in the app.

Credentials never travel back to the browser. The original shipped the
decrypted password and private key to the client purely to prefill the edit
form; this returns ``has_password`` / ``has_private_key`` flags instead, and
:meth:`save` only overwrites a stored secret when a replacement is supplied.
That also gives "leave unchanged" and "clear it" distinct representations,
which the original could not express.
"""
from __future__ import annotations

from typing import Any, Optional

from pytincture.dataclass import backend_for_frontend, bff_policy

from services.auth import current_user_id
from services.db import encrypt, get_db
from services.paths import SESSION_TYPES

# Columns that are safe to hand to the browser.
_PUBLIC_COLUMNS = (
    "id, user_id, name, folder, type, host, port, username, description, created_at"
)

_SENTINEL_UNCHANGED = "\x00unchanged\x00"


@backend_for_frontend
@bff_policy(application="iguanaxterm")
class SessionService:
    def __init__(self, _user: dict = None) -> None:
        self._user = _user or {}
        self._user_id = current_user_id(self._user)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def list(self) -> list:
        """Every profile the caller owns, ordered for the sidebar tree."""
        if not self._user_id:
            return []
        with get_db() as conn:
            rows = conn.execute(
                f"SELECT {_PUBLIC_COLUMNS}, "
                "  (password <> '') AS has_password, "
                "  (private_key <> '') AS has_private_key, "
                "  (host_key <> '') AS has_host_key "
                "FROM sessions WHERE user_id = ? "
                "ORDER BY folder COLLATE NOCASE, name COLLATE NOCASE",
                (self._user_id,),
            ).fetchall()
        return [
            {
                **dict(row),
                "has_password": bool(row["has_password"]),
                "has_private_key": bool(row["has_private_key"]),
                "has_host_key": bool(row["has_host_key"]),
            }
            for row in rows
        ]

    def get(self, session_id: int) -> dict:
        """One profile, without its secrets — enough to prefill the editor."""
        if not self._user_id:
            return {}
        with get_db() as conn:
            row = conn.execute(
                f"SELECT {_PUBLIC_COLUMNS}, "
                "  (password <> '') AS has_password, "
                "  (private_key <> '') AS has_private_key "
                "FROM sessions WHERE id = ? AND user_id = ?",
                (int(session_id), self._user_id),
            ).fetchone()
        if row is None:
            return {}
        return {
            **dict(row),
            "has_password": bool(row["has_password"]),
            "has_private_key": bool(row["has_private_key"]),
        }

    def folders(self) -> list:
        """Distinct folder names, for the editor's suggestions."""
        if not self._user_id:
            return []
        with get_db() as conn:
            rows = conn.execute(
                "SELECT DISTINCT folder FROM sessions "
                "WHERE user_id = ? AND folder <> '' ORDER BY folder COLLATE NOCASE",
                (self._user_id,),
            ).fetchall()
        return [row["folder"] for row in rows]

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def save(
        self,
        session_id: Optional[int] = None,
        name: str = "",
        host: str = "",
        port: int = 22,
        username: str = "",
        session_type: str = "ssh",
        folder: str = "",
        description: str = "",
        password: str = _SENTINEL_UNCHANGED,
        private_key: str = _SENTINEL_UNCHANGED,
    ) -> dict:
        """
        Create or update a profile.

        ``password`` and ``private_key`` are three-state: omitted leaves the
        stored value alone, ``""`` clears it, anything else replaces it.
        """
        if not self._user_id:
            return {"ok": False, "error": "Not authenticated"}

        errors = self._validate(name, host, port, session_type)
        if errors:
            return {"ok": False, "errors": errors}

        name = name.strip()
        host = host.strip()
        folder = folder.strip()
        port = int(port)

        with get_db() as conn:
            if session_id:
                owned = conn.execute(
                    "SELECT id FROM sessions WHERE id = ? AND user_id = ?",
                    (int(session_id), self._user_id),
                ).fetchone()
                if owned is None:
                    return {"ok": False, "error": "Session not found"}

                assignments = [
                    "name = ?", "host = ?", "port = ?", "username = ?",
                    "type = ?", "folder = ?", "description = ?",
                ]
                values: list[Any] = [
                    name, host, port, username, session_type, folder, description,
                ]
                if password != _SENTINEL_UNCHANGED:
                    assignments.append("password = ?")
                    values.append(encrypt(password))
                if private_key != _SENTINEL_UNCHANGED:
                    assignments.append("private_key = ?")
                    values.append(encrypt(private_key))
                # Host or port changed means the pinned key belongs to a
                # different endpoint; drop it so the next connect re-pins.
                assignments.append(
                    "host_key = CASE WHEN host = ? AND port = ? THEN host_key ELSE '' END"
                )
                values.extend([host, port])

                values.extend([int(session_id), self._user_id])
                conn.execute(
                    f"UPDATE sessions SET {', '.join(assignments)} "
                    "WHERE id = ? AND user_id = ?",
                    values,
                )
                new_id = int(session_id)
            else:
                cursor = conn.execute(
                    "INSERT INTO sessions "
                    "(user_id, name, host, port, username, type, folder, description, "
                    " password, private_key) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        self._user_id, name, host, port, username, session_type,
                        folder, description,
                        encrypt("" if password == _SENTINEL_UNCHANGED else password),
                        encrypt("" if private_key == _SENTINEL_UNCHANGED else private_key),
                    ),
                )
                new_id = cursor.lastrowid

        return {"ok": True, "id": new_id}

    def delete(self, session_id: int) -> dict:
        if not self._user_id:
            return {"ok": False, "error": "Not authenticated"}
        with get_db() as conn:
            cursor = conn.execute(
                "DELETE FROM sessions WHERE id = ? AND user_id = ?",
                (int(session_id), self._user_id),
            )
        if cursor.rowcount == 0:
            return {"ok": False, "error": "Session not found"}

        from services.sftp_service import sftp_pool

        sftp_pool.close(int(session_id))
        return {"ok": True}

    def clear_host_key(self, session_id: int) -> dict:
        """
        Forget the pinned host key so the next connection re-pins.

        The deliberate escape hatch for a legitimately rebuilt server, which is
        otherwise indistinguishable from a machine-in-the-middle.
        """
        if not self._user_id:
            return {"ok": False, "error": "Not authenticated"}
        with get_db() as conn:
            conn.execute(
                "UPDATE sessions SET host_key = '' WHERE id = ? AND user_id = ?",
                (int(session_id), self._user_id),
            )
        return {"ok": True}

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    @staticmethod
    def _validate(name: str, host: str, port: Any, session_type: str) -> dict:
        """
        Server-side validation. The Form widget checks the same things in the
        browser for the person typing; this is the copy that counts.
        """
        errors: dict = {}
        if not (name or "").strip():
            errors["name"] = "Name is required"
        if not (host or "").strip():
            errors["host"] = "Host is required"
        try:
            port_value = int(port)
            if not 1 <= port_value <= 65535:
                raise ValueError
        except (TypeError, ValueError):
            errors["port"] = "Port must be between 1 and 65535"
        if session_type not in SESSION_TYPES:
            errors["session_type"] = "Type must be one of " + ", ".join(SESSION_TYPES)
        return errors
