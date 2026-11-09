"""
BFF: site lists (#37).

A site list is a named set of saved sites that its owner assigns to people.
Who may do what:

- **Admin:** every list. Adds any site, and assigns to any account.
- **Power User:** their own lists. Adds only sites they own, and assigns only
  to the Users they created.
- **User:** nothing here.

The caller's role is read from the database on every call (as in
``UserService``), so a demotion applies at once. Lists the caller can't manage
answer "List not found", so ids can't be probed. Nothing here returns a
credential: sites are described by id, name, folder, type and host only.

This module decides who may *edit* lists. Who may *connect* through one is
``services/access.py``, and nowhere else.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Optional

from pytincture.dataclass import backend_for_frontend, bff_policy

from services.auth import current_user_id
from services.db import current_role, get_db

MAX_NAME_LENGTH = 80


def _recheck_live_access() -> None:
    """Close live connections that a change here just took away (#41)."""
    from services.live import recheck

    recheck()


@backend_for_frontend
@bff_policy(application="iguanaxterm")
class SiteListService:
    def __init__(self, _user: dict = None) -> None:
        self._user = _user or {}
        self._user_id = current_user_id(self._user)

    # ------------------------------------------------------------------
    # Permission helpers
    # ------------------------------------------------------------------

    def _role(self, conn: sqlite3.Connection) -> str:
        return current_role(conn, self._user_id)

    def _managed_list(self, conn: sqlite3.Connection, list_id: Any):
        """The list's row if the caller may manage it, else None."""
        try:
            list_id = int(list_id)
        except (TypeError, ValueError):
            return None
        row = conn.execute("SELECT * FROM site_lists WHERE id = ?", (list_id,)).fetchone()
        if row is None:
            return None
        role = self._role(conn)
        if role == "admin" or (role == "power" and row["owner_id"] == self._user_id):
            return row
        return None

    def _pickable_sites(self, conn: sqlite3.Connection) -> list:
        """Sites the caller may put in a list: their own, or every one for an Admin."""
        role = self._role(conn)
        if role == "admin":
            where, params = "", ()
        elif role == "power":
            where, params = "WHERE s.user_id = ? ", (self._user_id,)
        else:
            return []
        rows = conn.execute(
            "SELECT s.id, s.name, s.folder, s.type, s.host, s.user_id, u.username AS owner "
            "FROM sessions s JOIN users u ON u.id = s.user_id "
            f"{where}ORDER BY u.username COLLATE NOCASE, s.folder COLLATE NOCASE, "
            "s.name COLLATE NOCASE",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def _pickable_members(self, conn: sqlite3.Connection) -> list:
        """Accounts the caller may assign lists to: their Users, or anyone for an Admin."""
        role = self._role(conn)
        if role == "admin":
            where, params = "", ()
        elif role == "power":
            where, params = "WHERE created_by = ? AND role = 'user' ", (self._user_id,)
        else:
            return []
        rows = conn.execute(
            f"SELECT id, username, role FROM users {where}ORDER BY username COLLATE NOCASE",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def _describe(self, conn: sqlite3.Connection, row) -> dict:
        items = conn.execute(
            "SELECT s.id, s.name, s.folder, s.type, s.host "
            "FROM site_list_items i JOIN sessions s ON s.id = i.session_id "
            "WHERE i.list_id = ? ORDER BY s.folder COLLATE NOCASE, s.name COLLATE NOCASE",
            (row["id"],),
        ).fetchall()
        members = conn.execute(
            "SELECT u.id, u.username, u.role "
            "FROM site_list_members m JOIN users u ON u.id = m.user_id "
            "WHERE m.list_id = ? ORDER BY u.username COLLATE NOCASE",
            (row["id"],),
        ).fetchall()
        owner = conn.execute(
            "SELECT username FROM users WHERE id = ?", (row["owner_id"],)
        ).fetchone()
        return {
            "id": row["id"],
            "name": row["name"],
            "description": row["description"],
            "allow_files": bool(row["allow_files"]),
            "owner_id": row["owner_id"],
            "owner": owner["username"] if owner else "",
            "sites": [dict(item) for item in items],
            "members": [dict(member) for member in members],
        }

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    @bff_policy(min_role="power", roles={"power"})
    def list(self) -> list:
        """The lists the caller manages, with counts for the table."""
        with get_db() as conn:
            role = self._role(conn)
            if role == "admin":
                where, params = "", ()
            elif role == "power":
                where, params = "WHERE l.owner_id = ? ", (self._user_id,)
            else:
                return []
            rows = conn.execute(
                "SELECT l.id, l.name, l.description, l.allow_files, l.owner_id, "
                "       u.username AS owner, "
                "       (SELECT COUNT(*) FROM site_list_items i WHERE i.list_id = l.id) AS site_count, "
                "       (SELECT COUNT(*) FROM site_list_members m WHERE m.list_id = l.id) AS member_count "
                "FROM site_lists l JOIN users u ON u.id = l.owner_id "
                f"{where}ORDER BY l.name COLLATE NOCASE",
                params,
            ).fetchall()
        return [{**dict(row), "allow_files": bool(row["allow_files"])} for row in rows]

    @bff_policy(min_role="power", roles={"power"})
    def get(self, list_id: int) -> dict:
        with get_db() as conn:
            row = self._managed_list(conn, list_id)
            if row is None:
                return {"ok": False, "error": "List not found"}
            return {"ok": True, **self._describe(conn, row)}

    @bff_policy(min_role="power", roles={"power"})
    def candidates(self) -> dict:
        """What the editor may offer: sites to add and accounts to assign."""
        with get_db() as conn:
            return {"sites": self._pickable_sites(conn), "members": self._pickable_members(conn)}

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    @bff_policy(min_role="power", roles={"power"})
    def save(
        self,
        list_id: Optional[int] = None,
        name: str = "",
        description: str = "",
        allow_files: bool = False,
    ) -> dict:
        """Create a list, or rename / redescribe / switch files on an existing one."""
        name = (name or "").strip()
        if not name:
            return {"ok": False, "errors": {"name": "A name is required"}}
        if len(name) > MAX_NAME_LENGTH:
            return {"ok": False, "errors": {"name": f"At most {MAX_NAME_LENGTH} characters"}}
        with get_db() as conn:
            if self._role(conn) not in ("admin", "power"):
                return {"ok": False, "error": "Not allowed"}
            if list_id:
                row = self._managed_list(conn, list_id)
                if row is None:
                    return {"ok": False, "error": "List not found"}
                conn.execute(
                    "UPDATE site_lists SET name = ?, description = ?, allow_files = ? WHERE id = ?",
                    (name, description or "", int(bool(allow_files)), row["id"]),
                )
                new_id = row["id"]
                files_removed = bool(row["allow_files"]) and not allow_files
            else:
                new_id = conn.execute(
                    "INSERT INTO site_lists (owner_id, name, description, allow_files) "
                    "VALUES (?, ?, ?, ?)",
                    (self._user_id, name, description or "", int(bool(allow_files))),
                ).lastrowid
                files_removed = False
        if files_removed:
            _recheck_live_access()
        return {"ok": True, "id": new_id}

    @bff_policy(min_role="power", roles={"power"})
    def delete_preview(self, list_id: int) -> dict:
        """How many people lose access, for the confirm dialog."""
        with get_db() as conn:
            row = self._managed_list(conn, list_id)
            if row is None:
                return {"ok": False, "error": "List not found"}
            members = conn.execute(
                "SELECT COUNT(*) FROM site_list_members WHERE list_id = ?", (row["id"],)
            ).fetchone()[0]
        return {"ok": True, "name": row["name"], "members": members}

    @bff_policy(min_role="power", roles={"power"})
    def delete(self, list_id: int) -> dict:
        with get_db() as conn:
            row = self._managed_list(conn, list_id)
            if row is None:
                return {"ok": False, "error": "List not found"}
            conn.execute("DELETE FROM site_lists WHERE id = ?", (row["id"],))
        _recheck_live_access()
        return {"ok": True}

    @bff_policy(min_role="power", roles={"power"})
    def set_items(self, list_id: int, session_ids: list) -> dict:
        """Replace the list's sites. Every id must be one the caller may add."""
        with get_db() as conn:
            row = self._managed_list(conn, list_id)
            if row is None:
                return {"ok": False, "error": "List not found"}
            allowed = {site["id"] for site in self._pickable_sites(conn)}
            wanted = _ids(session_ids)
            # A site already in the list that the caller couldn't add now (an
            # Admin's site in a list an Admin later handed to a Power User,
            # say) is kept as it is rather than dropped silently.
            current = {
                r[0] for r in conn.execute(
                    "SELECT session_id FROM site_list_items WHERE list_id = ?", (row["id"],)
                )
            }
            refused = wanted - allowed - current
            if refused:
                return {"ok": False, "error": "Some of those sites can't be added to this list"}
            # Not "NOT IN (NULL)" for an empty set: in SQL that matches nothing,
            # so clearing a list would have removed no one.
            for gone in current - wanted:
                conn.execute(
                    "DELETE FROM site_list_items WHERE list_id = ? AND session_id = ?",
                    (row["id"], gone),
                )
            conn.executemany(
                "INSERT OR IGNORE INTO site_list_items (list_id, session_id) VALUES (?, ?)",
                [(row["id"], sid) for sid in wanted],
            )
            removed = bool(current - wanted)
        if removed:
            _recheck_live_access()
        return {"ok": True}

    @bff_policy(min_role="power", roles={"power"})
    def set_members(self, list_id: int, user_ids: list) -> dict:
        """Replace who the list is assigned to. Every id must be one the caller may assign."""
        with get_db() as conn:
            row = self._managed_list(conn, list_id)
            if row is None:
                return {"ok": False, "error": "List not found"}
            allowed = {member["id"] for member in self._pickable_members(conn)}
            wanted = _ids(user_ids)
            current = {
                r[0] for r in conn.execute(
                    "SELECT user_id FROM site_list_members WHERE list_id = ?", (row["id"],)
                )
            }
            refused = wanted - allowed - current
            if refused:
                return {"ok": False, "error": "Some of those accounts can't be assigned this list"}
            # Not "NOT IN (NULL)" for an empty set: in SQL that matches nothing,
            # so clearing a list would have removed no one.
            for gone in current - wanted:
                conn.execute(
                    "DELETE FROM site_list_members WHERE list_id = ? AND user_id = ?",
                    (row["id"], gone),
                )
            conn.executemany(
                "INSERT OR IGNORE INTO site_list_members (list_id, user_id) VALUES (?, ?)",
                [(row["id"], uid) for uid in wanted],
            )
            removed = bool(current - wanted)
        if removed:
            _recheck_live_access()
        return {"ok": True}

    # ------------------------------------------------------------------
    # From the Users screen (#40)
    # ------------------------------------------------------------------

    @bff_policy(min_role="power", roles={"power"})
    def lists_for(self, user_id: int) -> dict:
        """The caller's lists, and which of them ``user_id`` has."""
        with get_db() as conn:
            if int(user_id) not in {m["id"] for m in self._pickable_members(conn)}:
                return {"ok": False, "error": "User not found"}
            lists = self.list()
            assigned = {
                r[0] for r in conn.execute(
                    "SELECT list_id FROM site_list_members WHERE user_id = ?", (int(user_id),)
                )
            }
        return {
            "ok": True,
            "lists": [{"id": item["id"], "name": item["name"]} for item in lists],
            "assigned": sorted(assigned & {item["id"] for item in lists}),
        }

    @bff_policy(min_role="power", roles={"power"})
    def assign(self, user_id: int, list_ids: list) -> dict:
        """
        Set which of the caller's lists ``user_id`` has. Lists the caller
        doesn't manage are left exactly as they are.
        """
        target = int(user_id)
        with get_db() as conn:
            if target not in {m["id"] for m in self._pickable_members(conn)}:
                return {"ok": False, "error": "User not found"}
            mine = {item["id"] for item in self.list()}
            wanted = _ids(list_ids)
            if wanted - mine:
                return {"ok": False, "error": "List not found"}
            current = {
                r[0] for r in conn.execute(
                    "SELECT list_id FROM site_list_members WHERE user_id = ?", (target,)
                )
            } & mine
            for list_id in current - wanted:
                conn.execute(
                    "DELETE FROM site_list_members WHERE list_id = ? AND user_id = ?",
                    (list_id, target),
                )
            conn.executemany(
                "INSERT OR IGNORE INTO site_list_members (list_id, user_id) VALUES (?, ?)",
                [(list_id, target) for list_id in wanted],
            )
            removed = bool(current - wanted)
        if removed:
            _recheck_live_access()
        return {"ok": True}


def _ids(values: Any) -> set:
    """A set of positive ints from whatever the browser sent."""
    out = set()
    for value in values or []:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            out.add(number)
    return out
