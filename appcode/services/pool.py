"""
The connection pools, and anything else that must outlive a single call.

**Not a BFF module, on purpose.** pytincture re-executes a BFF module's source
on every call (``backend/app.py``: ``prepare_call`` -> ``_load_source_module``),
so a module-level object in ``sftp_service.py`` was a new, empty pool on every
request. Every file-browser click dialled a fresh SSH connection and nothing
ever closed it: one Files open and five refreshes left 7 logins and ~14 live
sshd sessions on the test target. This module is imported normally and cached
in ``sys.modules``, so its objects are real singletons.

Rule for this app: **state that must persist lives here (or in another plain
module), never at module level in a file with ``@backend_for_frontend``.**
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

import paramiko

from services import ftp as ftp_helpers
from services import ssh as ssh_helpers
from services.db import get_db


# SFTP calls are blocking; they run here rather than on the event loop.
walk_executor = ThreadPoolExecutor(max_workers=16, thread_name_prefix="sftp")

# Drop a pooled connection that has gone unused for this long.
_IDLE_TIMEOUT_SECONDS = 300


class _PooledConnection:
    __slots__ = ("client", "sftp", "lock", "last_used")

    def __init__(self, client: paramiko.SSHClient, sftp: paramiko.SFTPClient) -> None:
        self.client = client
        self.sftp = sftp
        self.lock = threading.Lock()
        self.last_used = time.monotonic()


class SFTPPool:
    """
    One live SFTP channel per saved session, reused across calls.

    Keyed by ``(user_id, session_id)``. The session id alone would be enough
    today because every lookup is already ownership-scoped, but keying on both
    means a future unscoped read cannot hand one user another's open channel.
    """

    def __init__(self) -> None:
        self._connections: dict[tuple[int, int], _PooledConnection] = {}
        self._keys: dict[int, tuple[int, int]] = {}
        # How many open panes still want each connection. A session can be open
        # in several panes at once, and the first one closed must not pull the
        # channel out from under the others.
        self._refs: dict[tuple[int, int], int] = {}
        self._guard = threading.Lock()
        # One lock per session, held across the connect itself. Without it two
        # concurrent acquires for the same session both miss the cache and both
        # dial out; one connection is then overwritten in the dict and leaks,
        # and the extra dial adds to exactly the connection pressure that makes
        # a server reset the banner.
        self._dialing: dict[tuple[int, int], threading.Lock] = {}

    def _dial_lock(self, key: tuple[int, int]) -> threading.Lock:
        with self._guard:
            return self._dialing.setdefault(key, threading.Lock())

    def _evict_idle(self) -> None:
        cutoff = time.monotonic() - _IDLE_TIMEOUT_SECONDS
        # A held lock means a transfer is running on the channel right now.
        # last_used is stamped when a transfer starts, so a large one looks
        # idle after five minutes, and evicting it would cut it off mid-file.
        stale = [
            key
            for key, conn in self._connections.items()
            if conn.last_used < cutoff and not conn.lock.locked()
        ]
        for key in stale:
            conn = self._connections.pop(key, None)
            self._keys.pop(key[1], None)
            self._dialing.pop(key, None)
            # A browser that was closed or crashed never released its hold, so
            # idle eviction is what stops a stale refcount pinning a channel
            # open for the life of the process.
            self._refs.pop(key, None)
            if conn is not None:
                try:
                    conn.client.close()
                except Exception:
                    pass

    def _live(self, key: tuple[int, int]) -> Optional[_PooledConnection]:
        """A pooled connection that is still up, or None after discarding it."""
        with self._guard:
            self._evict_idle()
            conn = self._connections.get(key)
            if conn is None:
                return None
            transport = conn.client.get_transport()
            # is_active() is a local flag check. The original issued a full
            # listdir(".") round trip before *every* operation.
            if transport is not None and transport.is_active():
                conn.last_used = time.monotonic()
                return conn
            self._connections.pop(key, None)
        try:
            conn.client.close()
        except Exception:
            pass
        return None

    def acquire(self, user_id: int, session_id: int, session: dict) -> _PooledConnection:
        key = (user_id, session_id)
        conn = self._live(key)
        if conn is not None:
            return conn

        # Serialise dialling per session. The check is repeated inside the lock
        # because another caller may have connected while this one waited.
        with self._dial_lock(key):
            conn = self._live(key)
            if conn is not None:
                return conn

            client = _dial(session, session_id, user_id)
            conn = _PooledConnection(client, client.open_sftp())
            with self._guard:
                self._connections[key] = conn
                self._keys[session_id] = key
            return conn

    def retain(self, user_id: int, session_id: int) -> int:
        """Register interest in a session's channel. Balanced by release()."""
        key = (int(user_id), int(session_id))
        with self._guard:
            self._refs[key] = self._refs.get(key, 0) + 1
            return self._refs[key]

    def release(self, user_id: int, session_id: int) -> int:
        """
        Drop one hold, closing the channel only when the last one goes.

        This is what ``disconnect`` means now. Closing outright was correct
        while one session could only be open once; with a pane per connection,
        the same session can be open several times over and the first pane
        closed would have killed the rest.
        """
        key = (int(user_id), int(session_id))
        with self._guard:
            remaining = self._refs.get(key, 0) - 1
            if remaining > 0:
                self._refs[key] = remaining
                return remaining
            self._refs.pop(key, None)
            conn = self._connections.pop(key, None)
            self._keys.pop(key[1], None)
            self._dialing.pop(key, None)
        if conn is not None:
            try:
                conn.client.close()
            except Exception:
                pass
        return 0

    def close(self, session_id: int) -> None:
        """
        Force the channel shut whatever is holding it.

        Deleting a session or a user must not leave a live channel to it, so
        this ignores refcounts rather than waiting them out.
        """
        with self._guard:
            key = self._keys.pop(int(session_id), None)
            if key:
                self._refs.pop(key, None)
            conn = self._connections.pop(key, None) if key else None
        if conn is not None:
            try:
                conn.client.close()
            except Exception:
                pass

    def close_all(self) -> None:
        with self._guard:
            connections = list(self._connections.values())
            self._connections.clear()
            self._keys.clear()
            self._refs.clear()
        for conn in connections:
            try:
                conn.client.close()
            except Exception:
                pass


sftp_pool = SFTPPool()


def _dial(session: dict, session_id: int, user_id: int):
    """
    A connected client for the file browser, whatever the protocol.

    An FTP connection answers the same calls as paramiko's (see ``ftp.py``),
    so nothing past this point needs to know which one it got.
    """
    if session.get("type") == "ftp":
        return ftp_helpers.connect(
            session, on_learn_host_key=_persist_host_key(session_id, user_id)
        )
    if session.get("type") == "telnet":
        raise PermissionError("Telnet sessions have no file browser")
    return ssh_helpers.connect(
        session, on_learn_host_key=_persist_host_key(session_id, user_id)
    )


def _persist_host_key(session_id: int, user_id: int) -> Callable[[str], None]:
    """Record a first-seen host key on the owning profile."""

    def _store(serialized: str) -> None:
        with get_db() as conn:
            conn.execute(
                "UPDATE sessions SET host_key = ? "
                "WHERE id = ? AND user_id = ? AND host_key = ''",
                (serialized, session_id, user_id),
            )

    return _store


# Transfers run on their own pool: a queued folder download holds one channel
# for as long as it takes, and sharing the interactive pool would freeze the
# file browser behind it. See transfer.py.
transfer_pool = SFTPPool()
