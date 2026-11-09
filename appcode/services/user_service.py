"""
BFF: account management.

Admin-only methods are checked twice over: pytincture checks the ``roles``
claim that ``auth.authenticate`` set, and ``auth.policy_hook`` checks
``min_role``. :meth:`change_own_password` is deliberately outside that —
every user changes their own password.

Power Users managing their own Users is #35; until then every method here
except the self-service ones is Admin only.
"""
from __future__ import annotations

import sqlite3

from pytincture.dataclass import backend_for_frontend, bff_policy

from services.auth import current_user_id
from services.db import admin_count, get_db, hash_password, set_role, verify_password
from services.roles import ADMIN, POWER, is_role, role_of

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
        must_change = False
        if self._user_id:
            with get_db() as conn:
                row = conn.execute(
                    "SELECT must_change_pw FROM users WHERE id = ?", (self._user_id,)
                ).fetchone()
            must_change = bool(row and row["must_change_pw"])
        return {
            "user_id": self._user_id,
            "username": self._user.get("username", ""),
            "role": role_of(self._user),
            # Kept until the UI reads `role` (#35).
            "is_admin": role_of(self._user) == ADMIN,
            # A password this person did not choose (the install default, or
            # one an administrator set): the UI asks for a new one on load.
            "must_change_password": must_change,
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
        if new_password == current_password:
            return {
                "ok": False,
                "errors": {"new_password": "Choose a password different from the current one"},
            }

        with get_db() as conn:
            conn.execute(
                "UPDATE users SET pw_hash = ?, must_change_pw = 0 WHERE id = ?",
                (hash_password(new_password), self._user_id),
            )
        return {"ok": True}

    # ------------------------------------------------------------------
    # Administration
    # ------------------------------------------------------------------

    @bff_policy(min_role="admin", roles={"admin"})
    def list(self) -> list:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT u.id, u.username, u.role, u.created_by, u.created_at, "
                "       COUNT(s.id) AS session_count "
                "FROM users u LEFT JOIN sessions s ON s.user_id = u.id "
                "GROUP BY u.id ORDER BY u.username COLLATE NOCASE"
            ).fetchall()
        return [
            {
                "id": row["id"],
                "username": row["username"],
                "role": row["role"],
                "is_admin": row["role"] == ADMIN,
                "created_by": row["created_by"],
                "created_at": (row["created_at"] or "")[:10],
                "session_count": row["session_count"],
            }
            for row in rows
        ]

    @bff_policy(min_role="admin", roles={"admin"})
    def create(
        self, username: str, password: str, is_admin: bool = False, role: str = ""
    ) -> dict:
        """
        Create an account. ``role`` wins when given; otherwise the older
        ``is_admin`` flag picks Admin or Power User, which is what the Users
        screen meant by "not an administrator" before roles existed.
        """
        name = (username or "").strip()
        role = role or (ADMIN if is_admin else POWER)
        errors: dict = {}
        if not is_role(role):
            errors["role"] = "Unknown role"
        if not name:
            errors["username"] = "Username is required"
        if len(password or "") < MIN_PASSWORD_LENGTH:
            errors["password"] = f"Must be at least {MIN_PASSWORD_LENGTH} characters"
        if errors:
            return {"ok": False, "errors": errors}

        try:
            with get_db() as conn:
                cursor = conn.execute(
                    # They did not choose it: asked to change it when they sign in.
                    "INSERT INTO users (username, pw_hash, role, is_admin, created_by, must_change_pw) "
                    "VALUES (?, ?, ?, ?, ?, 1)",
                    (name, hash_password(password), role, int(role == ADMIN), self._user_id or None),
                )
                new_id = cursor.lastrowid
        except sqlite3.IntegrityError:
            return {"ok": False, "errors": {"username": "That username is taken"}}
        return {"ok": True, "id": new_id}

    @bff_policy(min_role="admin", roles={"admin"})
    def delete(self, user_id: int) -> dict:
        target = int(user_id)
        if target == self._user_id:
            return {"ok": False, "error": "You cannot delete your own account"}

        with get_db() as conn:
            target_row = conn.execute(
                "SELECT role FROM users WHERE id = ?", (target,)
            ).fetchone()
            if target_row is None:
                return {"ok": False, "error": "User not found"}
            if target_row["role"] == ADMIN and admin_count(conn, excluding=target) == 0:
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

    @bff_policy(min_role="admin", roles={"admin"})
    def set_role(self, user_id: int, role: str) -> dict:
        target = int(user_id)
        if not is_role(role):
            return {"ok": False, "error": "Unknown role"}
        if target == self._user_id and role != ADMIN:
            return {"ok": False, "error": "You cannot remove your own admin rights"}
        with get_db() as conn:
            if role != ADMIN and admin_count(conn, excluding=target) == 0:
                return {"ok": False, "error": "That is the last administrator"}
            if not set_role(conn, target, role):
                return {"ok": False, "error": "User not found"}
        return {"ok": True}

    @bff_policy(min_role="admin", roles={"admin"})
    def set_admin(self, user_id: int, is_admin: bool) -> dict:
        """The Users screen's "Toggle admin" until #35: Admin, or back to Power User."""
        return self.set_role(user_id, ADMIN if is_admin else POWER)

    @bff_policy(min_role="admin", roles={"admin"})
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
                # Set by an administrator, so the person is asked to change it.
                # Resetting your own account here counts as choosing it.
                "UPDATE users SET pw_hash = ?, must_change_pw = ? WHERE id = ?",
                (hash_password(new_password), int(int(user_id) != self._user_id), int(user_id)),
            )
        if cursor.rowcount == 0:
            return {"ok": False, "error": "User not found"}
        return {"ok": True}
