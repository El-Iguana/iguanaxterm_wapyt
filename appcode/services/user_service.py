"""
BFF: account management.

Who manages whom (#35):

- **Admin:** every account, any role. Only Admins change roles.
- **Power User:** only the Users they created (``created_by``), and only
  with the User role. Other accounts don't exist as far as they can tell:
  reaching one by id answers "User not found".
- Nobody makes an account above their own role.

Management methods are checked twice over: pytincture checks the ``roles``
claim that ``auth.authenticate`` set, and ``auth.policy_hook`` checks
``min_role``. Inside, the caller's role is read **from the database**, not
the session, so a demotion takes effect on the next call rather than at the
next sign-in. :meth:`change_own_password` is deliberately outside all this —
every user changes their own password.
"""
from __future__ import annotations

import sqlite3

from pytincture.dataclass import backend_for_frontend, bff_policy

from services.auth import current_user_id
from services.db import admin_count, current_role, get_db, hash_password, set_role, verify_password
from services.roles import ADMIN, LABELS, POWER, USER, at_least, is_role, role_of

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

    def _caller_role(self, conn: sqlite3.Connection) -> str:
        """The caller's role as the database has it now; "" if the account is gone."""
        return current_role(conn, self._user_id)

    def _managed(self, conn: sqlite3.Connection, user_id: int):
        """
        The target account's row if the caller may manage it, else None.

        None covers both "no such account" and "not yours", so a Power User
        can't probe for accounts by id.
        """
        caller = self._caller_role(conn)
        row = conn.execute(
            "SELECT id, username, role, created_by FROM users WHERE id = ?", (int(user_id),)
        ).fetchone()
        if row is None:
            return None
        if caller == ADMIN:
            return row
        if caller == POWER and row["role"] == USER and row["created_by"] == self._user_id:
            return row
        return None

    @bff_policy(min_role="power", roles={"power"})
    def list(self) -> list:
        with get_db() as conn:
            caller = self._caller_role(conn)
            if caller == ADMIN:
                where, params = "", ()
            elif caller == POWER:
                where, params = "WHERE u.created_by = ? AND u.role = 'user' ", (self._user_id,)
            else:
                return []
            rows = conn.execute(
                "SELECT u.id, u.username, u.role, u.created_by, u.created_at, "
                "       c.username AS created_by_name, COUNT(s.id) AS session_count "
                "FROM users u LEFT JOIN sessions s ON s.user_id = u.id "
                "LEFT JOIN users c ON c.id = u.created_by "
                f"{where}GROUP BY u.id ORDER BY u.username COLLATE NOCASE",
                params,
            ).fetchall()
            # Which of the caller's site lists each account has (#40).
            list_where = "" if caller == ADMIN else "WHERE l.owner_id = ? "
            list_params = () if caller == ADMIN else (self._user_id,)
            memberships: dict = {}
            for member in conn.execute(
                "SELECT m.user_id, l.name FROM site_list_members m "
                f"JOIN site_lists l ON l.id = m.list_id {list_where}"
                "ORDER BY l.name COLLATE NOCASE",
                list_params,
            ):
                memberships.setdefault(member["user_id"], []).append(member["name"])
        return [
            {
                "id": row["id"],
                "username": row["username"],
                "role": row["role"],
                "lists": memberships.get(row["id"], []),
                "created_by": row["created_by"],
                "created_by_name": row["created_by_name"] or "",
                "created_at": (row["created_at"] or "")[:10],
                "session_count": row["session_count"],
            }
            for row in rows
        ]

    @bff_policy(min_role="power", roles={"power"})
    def create(self, username: str, password: str, role: str = USER) -> dict:
        name = (username or "").strip()
        role = role or USER
        errors: dict = {}
        if not name:
            errors["username"] = "Username is required"
        if len(password or "") < MIN_PASSWORD_LENGTH:
            errors["password"] = f"Must be at least {MIN_PASSWORD_LENGTH} characters"
        if errors:
            return {"ok": False, "errors": errors}

        try:
            with get_db() as conn:
                caller = self._caller_role(conn)
                if not is_role(role):
                    return {"ok": False, "errors": {"role": "Unknown role"}}
                # Power Users make Users only. That is also "nobody makes an
                # account above their own role", since Admins may make anything.
                if caller != ADMIN and role != USER:
                    return {"ok": False, "errors": {"role": f"You can only create {LABELS[USER]}s"}}
                if not at_least(caller, POWER):
                    return {"ok": False, "error": "Not allowed"}
                cursor = conn.execute(
                    # They did not choose it: asked to change it when they sign in.
                    "INSERT INTO users (username, pw_hash, role, is_admin, created_by, must_change_pw) "
                    "VALUES (?, ?, ?, ?, ?, 1)",
                    (name, hash_password(password), role, int(role == ADMIN), self._user_id),
                )
                new_id = cursor.lastrowid
        except sqlite3.IntegrityError:
            return {"ok": False, "errors": {"username": "That username is taken"}}
        return {"ok": True, "id": new_id}

    @bff_policy(min_role="power", roles={"power"})
    def delete_preview(self, user_id: int) -> dict:
        """What deleting an account takes with it, for the confirm dialog."""
        with get_db() as conn:
            row = self._managed(conn, user_id)
            if row is None:
                return {"ok": False, "error": "User not found"}
            sites = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE user_id = ?", (row["id"],)
            ).fetchone()[0]
            users = conn.execute(
                "SELECT COUNT(*) FROM users WHERE created_by = ?", (row["id"],)
            ).fetchone()[0]
            lists = conn.execute(
                "SELECT COUNT(*) FROM site_lists WHERE owner_id = ?", (row["id"],)
            ).fetchone()[0]
        return {
            "ok": True,
            "username": row["username"],
            "role": row["role"],
            "sites": sites,
            # Accounts and site lists this one created; they move to whoever
            # deletes it.
            "users": users,
            "lists": lists,
        }

    @bff_policy(min_role="power", roles={"power"})
    def delete(self, user_id: int) -> dict:
        target = int(user_id)
        if target == self._user_id:
            return {"ok": False, "error": "You cannot delete your own account"}

        with get_db() as conn:
            row = self._managed(conn, target)
            if row is None:
                return {"ok": False, "error": "User not found"}
            if row["role"] == ADMIN and admin_count(conn, excluding=target) == 0:
                return {"ok": False, "error": "That is the last administrator"}

            # The accounts this one created move to whoever deletes it, so
            # nobody is left without a manager by surprise.
            conn.execute(
                "UPDATE users SET created_by = ? WHERE created_by = ?", (self._user_id, target)
            )
            # Their site lists too, so the people assigned them keep the
            # sites that are still there (the deleted account's own sites go
            # with it, and drop out of every list).
            conn.execute(
                "UPDATE site_lists SET owner_id = ? WHERE owner_id = ?", (self._user_id, target)
            )
            # Drop any pooled SFTP connections before the rows go away.
            session_ids = [
                r["id"]
                for r in conn.execute(
                    "SELECT id FROM sessions WHERE user_id = ?", (target,)
                ).fetchall()
            ]
            conn.execute("DELETE FROM users WHERE id = ?", (target,))

        from services.live import recheck
        from services.pool import sftp_pool, transfer_pool

        from services import drive

        for session_id in session_ids:
            sftp_pool.close(session_id)
            transfer_pool.close(session_id)
            drive.remove_site(session_id)         # RDP drive folders (#76)
        drive.remove_user(target)
        # Their own open terminals and anything on their sites (#41).
        recheck()
        return {"ok": True}

    @bff_policy(min_role="admin", roles={"admin"})
    def set_role(self, user_id: int, role: str) -> dict:
        target = int(user_id)
        if not is_role(role):
            return {"ok": False, "error": "Unknown role"}
        if target == self._user_id and role != ADMIN:
            return {"ok": False, "error": "You cannot remove your own admin rights"}
        with get_db() as conn:
            if self._caller_role(conn) != ADMIN:
                return {"ok": False, "error": "Not allowed"}
            if role != ADMIN and admin_count(conn, excluding=target) == 0:
                return {"ok": False, "error": "That is the last administrator"}
            if not set_role(conn, target, role):
                return {"ok": False, "error": "User not found"}
        from services.live import recheck

        recheck()
        return {"ok": True}

    @bff_policy(min_role="power", roles={"power"})
    def reset_password(self, user_id: int, new_password: str) -> dict:
        if len(new_password or "") < MIN_PASSWORD_LENGTH:
            return {
                "ok": False,
                "errors": {
                    "new_password": f"Must be at least {MIN_PASSWORD_LENGTH} characters"
                },
            }
        target = int(user_id)
        with get_db() as conn:
            if target != self._user_id and self._managed(conn, target) is None:
                return {"ok": False, "error": "User not found"}
            if target == self._user_id and not self._caller_role(conn):
                return {"ok": False, "error": "User not found"}
            conn.execute(
                # Set by someone else, so the person is asked to change it.
                # Resetting your own account here counts as choosing it.
                "UPDATE users SET pw_hash = ?, must_change_pw = ? WHERE id = ?",
                (hash_password(new_password), int(target != self._user_id), target),
            )
        return {"ok": True}
