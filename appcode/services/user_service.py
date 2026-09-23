"""
BFF: account management.

``admin_required`` is enforced twice over: pytincture checks the ``roles``
claim that ``auth.authenticate`` set, and ``auth.policy_hook`` checks the
``is_admin`` claim. :meth:`change_own_password` is deliberately outside that —
every user changes their own password.
"""
from __future__ import annotations

import sqlite3

from pytincture.dataclass import backend_for_frontend, bff_policy

from services.auth import current_user_id
from services.db import get_db, hash_password, verify_password

MIN_PASSWORD_LENGTH = 8


@backend_for_frontend
@bff_policy(application="iguanaxterm")
class UserService:
    def __init__(self, _user: dict = None) -> None:
        self._user = _user or {}
        self._user_id = current_user_id(self._user)

    # ------------------------------------------------------------------
    # Self-service
    # ------------------------------------------------------------------

    def me(self) -> dict:
        return {
            "user_id": self._user_id,
            "username": self._user.get("username", ""),
            "is_admin": bool(self._user.get("is_admin")),
        }

    def change_own_password(self, current_password: str, new_password: str) -> dict:
        if not self._user_id:
            return {"ok": False, "error": "Not authenticated"}
        if len(new_password or "") < MIN_PASSWORD_LENGTH:
            return {
                "ok": False,
                "errors": {
                    "new_password": f"Must be at least {MIN_PASSWORD_LENGTH} characters"
                },
            }

        with get_db() as conn:
            row = conn.execute(
                "SELECT pw_hash FROM users WHERE id = ?", (self._user_id,)
            ).fetchone()
        if row is None or not verify_password(current_password, row["pw_hash"]):
            return {
                "ok": False,
                "errors": {"current_password": "Current password is incorrect"},
            }

        with get_db() as conn:
            conn.execute(
                "UPDATE users SET pw_hash = ? WHERE id = ?",
                (hash_password(new_password), self._user_id),
            )
        return {"ok": True}

    # ------------------------------------------------------------------
    # Administration
    # ------------------------------------------------------------------

    @bff_policy(admin_required=True, roles={"admin"})
    def list(self) -> list:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT u.id, u.username, u.is_admin, u.created_at, "
                "       COUNT(s.id) AS session_count "
                "FROM users u LEFT JOIN sessions s ON s.user_id = u.id "
                "GROUP BY u.id ORDER BY u.username COLLATE NOCASE"
            ).fetchall()
        return [
            {
                "id": row["id"],
                "username": row["username"],
                "is_admin": bool(row["is_admin"]),
                "created_at": (row["created_at"] or "")[:10],
                "session_count": row["session_count"],
            }
            for row in rows
        ]

    @bff_policy(admin_required=True, roles={"admin"})
    def create(self, username: str, password: str, is_admin: bool = False) -> dict:
        name = (username or "").strip()
        errors: dict = {}
        if not name:
            errors["username"] = "Username is required"
        if len(password or "") < MIN_PASSWORD_LENGTH:
            errors["password"] = f"Must be at least {MIN_PASSWORD_LENGTH} characters"
        if errors:
            return {"ok": False, "errors": errors}

        try:
            with get_db() as conn:
                cursor = conn.execute(
                    "INSERT INTO users (username, pw_hash, is_admin) VALUES (?, ?, ?)",
                    (name, hash_password(password), int(bool(is_admin))),
                )
                new_id = cursor.lastrowid
        except sqlite3.IntegrityError:
            return {"ok": False, "errors": {"username": "That username is taken"}}
        return {"ok": True, "id": new_id}

    @bff_policy(admin_required=True, roles={"admin"})
    def delete(self, user_id: int) -> dict:
        target = int(user_id)
        if target == self._user_id:
            return {"ok": False, "error": "You cannot delete your own account"}

        with get_db() as conn:
            remaining_admins = conn.execute(
                "SELECT COUNT(*) FROM users WHERE is_admin = 1 AND id <> ?", (target,)
            ).fetchone()[0]
            is_target_admin = conn.execute(
                "SELECT is_admin FROM users WHERE id = ?", (target,)
            ).fetchone()
            if is_target_admin is None:
                return {"ok": False, "error": "User not found"}
            if is_target_admin["is_admin"] and remaining_admins == 0:
                return {"ok": False, "error": "That is the last administrator"}

            # Drop any pooled SFTP connections before the rows go away.
            session_ids = [
                row["id"]
                for row in conn.execute(
                    "SELECT id FROM sessions WHERE user_id = ?", (target,)
                ).fetchall()
            ]
            conn.execute("DELETE FROM users WHERE id = ?", (target,))

        from services.sftp_service import sftp_pool

        for session_id in session_ids:
            sftp_pool.close(session_id)
        return {"ok": True}

    @bff_policy(admin_required=True, roles={"admin"})
    def set_admin(self, user_id: int, is_admin: bool) -> dict:
        target = int(user_id)
        if target == self._user_id and not is_admin:
            return {"ok": False, "error": "You cannot remove your own admin rights"}
        with get_db() as conn:
            if not is_admin:
                remaining = conn.execute(
                    "SELECT COUNT(*) FROM users WHERE is_admin = 1 AND id <> ?",
                    (target,),
                ).fetchone()[0]
                if remaining == 0:
                    return {"ok": False, "error": "That is the last administrator"}
            conn.execute(
                "UPDATE users SET is_admin = ? WHERE id = ?",
                (int(bool(is_admin)), target),
            )
        return {"ok": True}

    @bff_policy(admin_required=True, roles={"admin"})
    def reset_password(self, user_id: int, new_password: str) -> dict:
        if len(new_password or "") < MIN_PASSWORD_LENGTH:
            return {
                "ok": False,
                "errors": {
                    "new_password": f"Must be at least {MIN_PASSWORD_LENGTH} characters"
                },
            }
        with get_db() as conn:
            cursor = conn.execute(
                "UPDATE users SET pw_hash = ? WHERE id = ?",
                (hash_password(new_password), int(user_id)),
            )
        if cursor.rowcount == 0:
            return {"ok": False, "error": "User not found"}
        return {"ok": True}
