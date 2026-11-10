"""
BFF: saved connection profiles.

Writes are scoped by the caller's own ``user_id``. :meth:`list` adds the
sites shared with the caller through site lists (#39), read-only and without
credentials. Opening a site is ``services/access.py``'s job, not this one's.

Credentials never travel back to the browser. The original shipped the
decrypted password and private key to the client purely to prefill the edit
form; this returns ``has_password`` / ``has_private_key`` /
``has_passphrase`` flags instead, and
:meth:`save` only overwrites a stored secret when a replacement is supplied.
That also gives "leave unchanged" and "clear it" distinct representations,
which the original could not express.

**Users can't create, edit or delete sites (#36)**, or clear a pinned host
key. :meth:`save`, :meth:`delete` and :meth:`clear_host_key` check the
caller's role in the database, not the session, so this holds for a
hand-made call from the browser console and for a demotion made a moment
ago. :meth:`list` marks each site ``editable`` so the UI can hide what the
server would refuse.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from pytincture.dataclass import backend_for_frontend, bff_policy

from services.auth import current_user_id
from services.db import current_role, encrypt, get_db
from services.roles import ADMIN, POWER, at_least
from services import guacd
from services.paths import RDP_KEYBOARDS, RDP_RESIZE, RDP_SECURITY, SESSION_TYPES, session_caps

# Columns that are safe to hand to the browser.
_PUBLIC_COLUMNS = (
    "id, user_id, name, folder, type, host, port, username, description, "
    "via_session_id, options, created_at"
)

_SENTINEL_UNCHANGED = "\x00unchanged\x00"

_READ_ONLY = "Your account can use sites but not change them"


def _parse_options(value: Any) -> dict:
    """A row's options column as a dict ({} for anything unreadable)."""
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def clean_rdp_options(options: Any) -> tuple[dict, dict]:
    """
    Validate an RDP site's settings (#56). Returns (options, errors); unknown
    keys are dropped, so nothing else (and never a secret) gets stored.
    """
    given = options if isinstance(options, dict) else {}
    errors: dict = {}
    domain = str(given.get("domain") or "").strip()
    if len(domain) > 255 or any(c in domain for c in "\\/@ \t"):
        errors["domain"] = "A domain name, like CORP (no slashes, @ or spaces)"
    security = given.get("security") or "any"
    if security not in {key for key, _label in RDP_SECURITY}:
        errors["security"] = "Unknown security mode"
    resize = given.get("resize") or "display-update"
    if resize not in {key for key, _label in RDP_RESIZE}:
        errors["resize"] = "Unknown resize mode"
    keyboard = given.get("keyboard") or "en-us-qwerty"
    if keyboard not in {key for key, _label in RDP_KEYBOARDS}:
        errors["keyboard"] = "Unknown keyboard layout"
    return {
        "domain": domain,
        "security": security,
        "resize": resize,
        "keyboard": keyboard,
        "console": bool(given.get("console")),
        "audio": bool(given.get("audio", True)),     # play the desktop's sound (#73)
    }, errors


@backend_for_frontend
@bff_policy(application="iguanaxterm")
class SessionService:
    def __init__(self, _user: dict = None) -> None:
        self._user = _user or {}
        self._user_id = current_user_id(self._user)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def features(self) -> dict:
        """What this server can connect to: RDP only where guacd is set up (#55)."""
        return {"rdp": guacd.enabled()}

    def list(self) -> list:
        """
        Every site the caller may open, ordered for the sidebar tree: their
        own first, then the ones shared with them through site lists (#39).

        Each row says what the UI may offer: ``editable`` (Edit, Delete),
        ``can_forget`` (Forget host key) and ``files`` (the file browser).
        A shared row carries ``shared``, ``list_name`` and ``owner``, and never
        a credential or the owner's tunnel site.
        """
        if not self._user_id:
            return []
        with get_db() as conn:
            role = current_role(conn, self._user_id)
            editable = at_least(role, POWER)
            rows = conn.execute(
                f"SELECT {_PUBLIC_COLUMNS}, "
                "  (password <> '') AS has_password, "
                "  (private_key <> '') AS has_private_key, "
                "  (passphrase <> '') AS has_passphrase, "
                "  (host_key <> '') AS has_host_key "
                "FROM sessions WHERE user_id = ? "
                "ORDER BY folder COLLATE NOCASE, name COLLATE NOCASE",
                (self._user_id,),
            ).fetchall()
            # A site in several of the caller's lists shows once, under the
            # first list by name, with files if any of those lists allows them.
            # A site the caller owns shows once, as their own.
            shared = conn.execute(
                "SELECT s.id, s.user_id, s.name, s.folder, s.type, s.host, s.port, "
                "       s.username, s.description, s.created_at, s.options, "
                "       (s.host_key <> '') AS has_host_key, "
                "       MIN(l.name COLLATE NOCASE) AS list_name, "
                "       MAX(l.allow_files) AS allow_files, "
                "       o.username AS owner "
                "FROM site_list_members m "
                "JOIN site_lists l ON l.id = m.list_id "
                "JOIN site_list_items i ON i.list_id = l.id "
                "JOIN sessions s ON s.id = i.session_id "
                "JOIN users o ON o.id = s.user_id "
                "WHERE m.user_id = ? AND s.user_id <> ? "
                "GROUP BY s.id "
                "ORDER BY list_name COLLATE NOCASE, s.name COLLATE NOCASE",
                (self._user_id, self._user_id),
            ).fetchall()
        own = [
            {
                **dict(row),
                "has_password": bool(row["has_password"]),
                "has_private_key": bool(row["has_private_key"]),
                "has_passphrase": bool(row["has_passphrase"]),
                "has_host_key": bool(row["has_host_key"]),
                "options": _parse_options(row["options"]),
                "editable": editable,
                "can_forget": editable,
                "files": bool(session_caps(row["type"])["files"]),
                "shared": False,
            }
            for row in rows
        ]
        assigned = [
            {
                "id": row["id"],
                "user_id": row["user_id"],
                "name": row["name"],
                "folder": row["folder"],
                "type": row["type"],
                "host": row["host"],
                "port": row["port"],
                "username": row["username"],
                "description": row["description"],
                "created_at": row["created_at"],
                "via_session_id": 0,
                "options": _parse_options(row["options"]),
                "has_host_key": bool(row["has_host_key"]),
                "editable": False,
                # Only the owner or an Admin clears a shared site's pin.
                "can_forget": role == ADMIN,
                "files": bool(row["allow_files"]) and bool(session_caps(row["type"])["files"]),
                "shared": True,
                "list_name": row["list_name"],
                "owner": row["owner"],
            }
            for row in shared
        ]
        return own + assigned

    def get(self, session_id: int) -> dict:
        """One profile, without its secrets — enough to prefill the editor."""
        if not self._user_id:
            return {}
        with get_db() as conn:
            row = conn.execute(
                f"SELECT {_PUBLIC_COLUMNS}, "
                "  (password <> '') AS has_password, "
                "  (private_key <> '') AS has_private_key, "
                "  (passphrase <> '') AS has_passphrase "
                "FROM sessions WHERE id = ? AND user_id = ?",
                (int(session_id), self._user_id),
            ).fetchone()
        if row is None:
            return {}
        return {
            **dict(row),
            "options": _parse_options(row["options"]),
            "has_password": bool(row["has_password"]),
            "has_private_key": bool(row["has_private_key"]),
            "has_passphrase": bool(row["has_passphrase"]),
        }

    def folders(self) -> list:
        """Distinct folder names, for the editor's suggestions. None for Users."""
        if not self._user_id:
            return []
        with get_db() as conn:
            if not self._can_edit(conn):
                return []
            rows = conn.execute(
                "SELECT DISTINCT folder FROM sessions "
                "WHERE user_id = ? AND folder <> '' ORDER BY folder COLLATE NOCASE",
                (self._user_id,),
            ).fetchall()
        return [row["folder"] for row in rows]

    def _can_edit(self, conn) -> bool:
        """Power Users and Admins edit their own sites; Users edit none."""
        return at_least(current_role(conn, self._user_id), POWER)

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
        via_session_id: Optional[int] = 0,
        passphrase: str = _SENTINEL_UNCHANGED,
        options: Any = None,
    ) -> dict:
        """
        Create or update a profile.

        ``password``, ``private_key`` and ``passphrase`` are three-state:
        omitted leaves the stored value alone, ``""`` clears it, anything else
        replaces it. A key that is replaced or cleared takes its passphrase
        with it, unless a new one comes in the same call.
        """
        if not self._user_id:
            return {"ok": False, "error": "Not authenticated"}
        with get_db() as conn:
            if not self._can_edit(conn):
                return {"ok": False, "error": _READ_ONLY}

        errors = self._validate(name, host, port, session_type)
        stored_options: dict = {}
        if session_type == "rdp":
            if not guacd.enabled():
                return {"ok": False, "errors": {
                    "session_type": "RDP isn't set up on this server (no guacd)"}}
            stored_options, option_errors = clean_rdp_options(options)
            errors.update(option_errors)
        if errors:
            return {"ok": False, "errors": errors}

        if private_key != _SENTINEL_UNCHANGED and passphrase == _SENTINEL_UNCHANGED:
            passphrase = ""  # the stored passphrase belonged to the old key

        name = name.strip()
        host = host.strip()
        folder = folder.strip()
        port = int(port)
        # Only a desktop (VNC, RDP) rides a tunnel; any other type drops it.
        via = int(via_session_id or 0) if session_caps(session_type)["desktop"] else 0

        with get_db() as conn:
            if via:
                tunnel = conn.execute(
                    "SELECT type FROM sessions WHERE id = ? AND user_id = ?",
                    (via, self._user_id),
                ).fetchone()
                if (
                    tunnel is None
                    or via == int(session_id or 0)
                    or not SESSION_TYPES.get(tunnel["type"], {}).get("tunnel")
                ):
                    return {"ok": False, "errors": {
                        "via_session_id": "Choose one of your SSH or SFTP sessions"}}
            if session_id:
                owned = conn.execute(
                    "SELECT id FROM sessions WHERE id = ? AND user_id = ?",
                    (int(session_id), self._user_id),
                ).fetchone()
                if owned is None:
                    return {"ok": False, "error": "Session not found"}

                assignments = [
                    "name = ?", "host = ?", "port = ?", "username = ?",
                    "type = ?", "folder = ?", "description = ?", "via_session_id = ?",
                    "options = ?",
                ]
                values: list[Any] = [
                    name, host, port, username, session_type, folder, description, via,
                    json.dumps(stored_options),
                ]
                if password != _SENTINEL_UNCHANGED:
                    assignments.append("password = ?")
                    values.append(encrypt(password))
                if private_key != _SENTINEL_UNCHANGED:
                    assignments.append("private_key = ?")
                    values.append(encrypt(private_key))
                if passphrase != _SENTINEL_UNCHANGED:
                    assignments.append("passphrase = ?")
                    values.append(encrypt(passphrase))
                # Host, port or type changed means the pin belongs to a different
                # endpoint (or protocol: an SSH key is no RDP certificate); drop
                # it so the next connect re-pins.
                assignments.append(
                    "host_key = CASE WHEN host = ? AND port = ? AND type = ? "
                    "THEN host_key ELSE '' END"
                )
                values.extend([host, port, session_type])

                values.extend([int(session_id), self._user_id])
                conn.execute(
                    f"UPDATE sessions SET {', '.join(assignments)} "
                    "WHERE id = ? AND user_id = ?",
                    values,
                )
                new_id = int(session_id)
                # Cached channels were dialled with the old host, port and
                # credentials. Without this they keep serving the file browser
                # and transfers until they sit idle for five minutes.
                from services.pool import sftp_pool, transfer_pool

                sftp_pool.close(new_id)
                transfer_pool.close(new_id)
            else:
                cursor = conn.execute(
                    "INSERT INTO sessions "
                    "(user_id, name, host, port, username, type, folder, description, "
                    " via_session_id, options, password, private_key, passphrase) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        self._user_id, name, host, port, username, session_type,
                        folder, description, via, json.dumps(stored_options),
                        encrypt("" if password == _SENTINEL_UNCHANGED else password),
                        encrypt("" if private_key == _SENTINEL_UNCHANGED else private_key),
                        encrypt("" if passphrase == _SENTINEL_UNCHANGED else passphrase),
                    ),
                )
                new_id = cursor.lastrowid

        return {"ok": True, "id": new_id}

    def delete(self, session_id: int) -> dict:
        if not self._user_id:
            return {"ok": False, "error": "Not authenticated"}
        with get_db() as conn:
            if not self._can_edit(conn):
                return {"ok": False, "error": _READ_ONLY}
            cursor = conn.execute(
                "DELETE FROM sessions WHERE id = ? AND user_id = ?",
                (int(session_id), self._user_id),
            )
        if cursor.rowcount == 0:
            return {"ok": False, "error": "Session not found"}

        from services.pool import sftp_pool, transfer_pool

        sftp_pool.close(int(session_id))
        transfer_pool.close(int(session_id))
        # Anyone it was shared with loses their open terminal or desktop too.
        from services.live import recheck

        recheck()
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
            if not self._can_edit(conn):
                return {"ok": False, "error": _READ_ONLY}
            # The owner clears their own site's pin. An Admin may clear any
            # site's, since a changed key on a shared site blocks everyone
            # who uses it (#38).
            if current_role(conn, self._user_id) == ADMIN:
                cursor = conn.execute(
                    "UPDATE sessions SET host_key = '' WHERE id = ?", (int(session_id),)
                )
            else:
                cursor = conn.execute(
                    "UPDATE sessions SET host_key = '' WHERE id = ? AND user_id = ?",
                    (int(session_id), self._user_id),
                )
        if cursor.rowcount == 0:
            return {"ok": False, "error": "Session not found"}
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
