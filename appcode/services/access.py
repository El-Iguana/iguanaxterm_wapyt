"""
Who may open a site: the one place that decides (#38). Server-side only.

Every connection path asks :func:`resolve_site` before it dials: the terminal
and desktop WebSockets, the desktop's SSH tunnel, the file browser, uploads,
downloads, and folder downloads saved on the server. Nothing else reads a
site's credentials.

The rule:

- **The owner** may open their site, with the file browser.
- **Anyone else** may open it only through a site list that contains it and
  has them as a member. They get the file browser if any such list allows
  files.
- Nobody else. **Admins get no implicit access** to other people's sites; they
  connect through lists like everyone else.

A plain module, not a BFF one, so ``services/live.py`` and the WebSocket
routes can import it (see CLAUDE.md, *A BFF module is re-executed on every
call*).
"""
from __future__ import annotations

import sqlite3
from typing import Callable, Optional

from services.db import decrypt, get_db

OWNER = "owner"
LIST = "list"


def site_access(conn: sqlite3.Connection, session_id: int, user_id: int) -> Optional[dict]:
    """
    How ``user_id`` may reach ``session_id``, without touching credentials.

    Returns ``{"via": "owner" | "list", "files": bool, "owner_id": int}``, or
    None when they may not reach it at all (or it doesn't exist).
    """
    if not user_id:
        return None
    row = conn.execute(
        "SELECT user_id FROM sessions WHERE id = ?", (int(session_id),)
    ).fetchone()
    if row is None:
        return None
    owner_id = row["user_id"]
    if owner_id == int(user_id):
        return {"via": OWNER, "files": True, "owner_id": owner_id}
    shared = conn.execute(
        "SELECT COUNT(*) AS lists, MAX(l.allow_files) AS files "
        "FROM site_list_items i "
        "JOIN site_lists l ON l.id = i.list_id "
        "JOIN site_list_members m ON m.list_id = i.list_id "
        "WHERE i.session_id = ? AND m.user_id = ?",
        (int(session_id), int(user_id)),
    ).fetchone()
    if not shared["lists"]:
        return None
    return {"via": LIST, "files": bool(shared["files"]), "owner_id": owner_id}


def resolve_site(session_id: int, user_id: int) -> Optional[tuple[dict, dict]]:
    """
    The site's profile with its credentials decrypted, and the access, or None.

    Callers that move files must also check ``access["files"]``; the terminal
    and the desktop don't.
    """
    with get_db() as conn:
        access = site_access(conn, session_id, user_id)
        if access is None:
            return None
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (int(session_id),)).fetchone()
    if row is None:
        return None
    profile = dict(row)
    for secret in ("password", "private_key", "passphrase"):
        profile[secret] = decrypt(profile.get(secret) or "")
    return profile, access


def resolve_files(session_id: int, user_id: int) -> tuple[Optional[dict], str]:
    """
    For the file paths: the profile, or None and why.

    The reason is "Session not found" for a site the person can't reach at all,
    so ids can't be probed, and a plain refusal when only files are withheld.
    """
    resolved = resolve_site(session_id, user_id)
    if resolved is None:
        return None, "Session not found"
    profile, access = resolved
    if not access["files"]:
        return None, "The file browser isn't available on this site"
    return profile, ""


def reachable_site_ids(conn: sqlite3.Connection, user_id: int) -> set:
    """Every site ``user_id`` may open: owned, or assigned through a list."""
    rows = conn.execute(
        "SELECT id FROM sessions WHERE user_id = ? "
        "UNION "
        "SELECT i.session_id FROM site_list_items i "
        "JOIN site_list_members m ON m.list_id = i.list_id WHERE m.user_id = ?",
        (int(user_id), int(user_id)),
    ).fetchall()
    return {int(row[0]) for row in rows}


def pin_host_key(session_id: int) -> Callable[[str], None]:
    """
    Record a first-seen host key on the site, whoever connected.

    No ``user_id`` in the match: a shared site gets the same trust-on-first-use
    protection as a private one, and a changed key then blocks everyone. Only
    an empty pin is ever written, so this can't overwrite a pinned key.
    """

    def _store(serialized: str) -> None:
        with get_db() as conn:
            conn.execute(
                "UPDATE sessions SET host_key = ? WHERE id = ? AND host_key = ''",
                (serialized, int(session_id)),
            )

    return _store


SHARED_HOST_KEY_CHANGED = (
    "The host key for this site changed. Ask the site's owner to check it."
)


def host_key_message(exc: Exception, access: dict) -> str:
    """
    What to tell someone whose site presented a different host key.

    The owner gets the full message, which tells them how to clear the pin. A
    person using a shared site can't clear it, so they're sent to the owner.
    """
    if access.get("via") == OWNER:
        return str(exc)
    return SHARED_HOST_KEY_CHANGED
