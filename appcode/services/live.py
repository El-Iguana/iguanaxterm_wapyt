"""
Live connections, so that taking access away takes effect at once (#41).

Terminals, desktops and folder downloads saved on the server register here
with a ``closer``. After anything that can take access away (a site removed
from a list, a member removed, a list deleted or its files switched off, a
site or an account deleted, a role changed) the code that made the change
calls :func:`recheck`, which asks ``access.site_access`` again for every live
entry and every pooled file-browser and transfer connection, and closes what
no longer passes.

Rechecking everything rather than working out the affected pairs keeps this
simple and hard to get wrong: there are only ever a handful of live
connections, and each check is one indexed query.

A plain module: its registry must outlive a BFF call (see CLAUDE.md, *A BFF
module is re-executed on every call*).
"""
from __future__ import annotations

import itertools
import logging
import threading
from typing import Callable

logger = logging.getLogger(__name__)

REVOKED = "Access to this site was removed."

# Kinds that move files: they also need the list's file permission.
FILE_KINDS = frozenset({"files", "job"})

_lock = threading.Lock()
_entries: dict[int, tuple[int, int, str, Callable[[str], None]]] = {}
_tokens = itertools.count(1)


def register(user_id: int, session_id: int, kind: str, closer: Callable[[str], None]) -> int:
    """
    Track one live connection. ``closer(reason)`` must end it, from any thread.
    Returns a token for :func:`unregister`.
    """
    token = next(_tokens)
    with _lock:
        _entries[token] = (int(user_id), int(session_id), kind, closer)
    return token


def unregister(token: int) -> None:
    with _lock:
        _entries.pop(token, None)


def count() -> int:
    with _lock:
        return len(_entries)


def _allowed(conn, user_id: int, session_id: int, kind: str) -> bool:
    from services.access import site_access

    access = site_access(conn, session_id, user_id)
    if access is None:
        return False
    return access["files"] if kind in FILE_KINDS else True


def recheck(reason: str = REVOKED) -> int:
    """
    Close every live connection whose person may no longer reach its site.
    Returns how many were closed (pooled connections included).
    """
    from services.db import get_db
    from services.pool import sftp_pool, transfer_pool

    with _lock:
        entries = list(_entries.items())
    pooled = [(pool, key) for pool in (sftp_pool, transfer_pool) for key in pool.keys()]
    if not entries and not pooled:
        return 0

    revoked: list[tuple[int, Callable[[str], None]]] = []
    closing_pool: list = []
    with get_db() as conn:
        for token, (user_id, session_id, kind, closer) in entries:
            if not _allowed(conn, user_id, session_id, kind):
                revoked.append((token, closer))
        for pool, (user_id, session_id) in pooled:
            if not _allowed(conn, user_id, session_id, "files"):
                closing_pool.append((pool, user_id, session_id))

    for token, closer in revoked:
        unregister(token)
        try:
            closer(reason)
        except Exception:
            logger.exception("closing a revoked connection failed")
    for pool, user_id, session_id in closing_pool:
        pool.close_for(user_id, session_id)
    return len(revoked) + len(closing_pool)
