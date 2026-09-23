"""
BFF: SFTP directory operations, plus the pooled connections they run on.

**Metadata only.** File contents never pass through this service: a JSON BFF
means base64, which inflates by a third and holds the whole payload in Pyodide's
heap. Bytes go through the plain streaming routes in ``transfer.py`` instead.
"""
from __future__ import annotations

import asyncio
import stat as stat_module
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

import paramiko
from pytincture.dataclass import backend_for_frontend, bff_policy, bff_stream

from services import ftp as ftp_helpers
from services import ssh as ssh_helpers
from services.auth import current_user_id
from services.db import fetch_session, get_db
from services.paths import file_icon, format_mode, format_mtime, format_size, is_safe_name

# SFTP calls are blocking; they run here rather than on the event loop.
_pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="sftp")

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


def describe_entry(attr: paramiko.SFTPAttributes, parent: str) -> dict:
    """
    One directory entry, formatted for the DataTable.

    Display strings are built here in Python — ``format_size`` and
    ``format_mtime`` are shared with the browser — and the raw values ride
    along as ``size_bytes`` / ``mtime`` so the table can sort on them.
    """
    mode = attr.st_mode or 0
    is_dir = stat_module.S_ISDIR(mode)
    is_link = stat_module.S_ISLNK(mode)
    name = attr.filename
    return {
        "id": f"{parent.rstrip('/')}/{name}" if parent != "/" else f"/{name}",
        "name": name,
        "icon": file_icon(name, is_dir=is_dir, is_link=is_link),
        "is_dir": is_dir,
        "is_link": is_link,
        "size": "" if is_dir else format_size(attr.st_size),
        "size_bytes": 0 if is_dir else int(attr.st_size or 0),
        "modified": format_mtime(attr.st_mtime),
        "mtime": int(attr.st_mtime or 0),
        "permissions": format_mode(mode),
    }


@backend_for_frontend
@bff_policy(application="iguanaxterm")
class SFTPService:
    def __init__(self, _user: dict = None) -> None:
        self._user = _user or {}
        self._user_id = current_user_id(self._user)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _session(self, session_id: int) -> Optional[dict]:
        if not self._user_id:
            return None
        return fetch_session(int(session_id), self._user_id)

    def _run(self, session_id: int, work: Callable[[paramiko.SFTPClient], Any]) -> Any:
        """
        Run one blocking SFTP operation.

        These methods are plain ``def`` on purpose: pytincture dispatches a
        synchronous BFF export to a worker thread, so blocking paramiko here is
        correct and needs no executor of our own. It also keeps the browser API
        uniform — every non-streaming call is ``await service.method_async()``.
        """
        session = self._session(session_id)
        if session is None:
            raise PermissionError("Session not found")
        conn = sftp_pool.acquire(self._user_id, int(session_id), session)
        with conn.lock:
            conn.last_used = time.monotonic()
            return work(conn.sftp)

    # ------------------------------------------------------------------
    # Operations
    # ------------------------------------------------------------------

    def list_dir(self, session_id: int, path: str = "") -> dict:
        """
        Entries in a directory. An empty or ``~`` path resolves to the login
        home directory rather than ``/``, which is where people expect to land.
        """
        def _work(sftp: paramiko.SFTPClient) -> dict:
            target = sftp.normalize(".") if path in ("", "~", ".") else path
            entries = [describe_entry(attr, target) for attr in sftp.listdir_attr(target)]
            entries.sort(key=lambda item: (not item["is_dir"], item["name"].lower()))
            return {"path": target, "entries": entries}

        try:
            return self._run(session_id, _work)
        except PermissionError as exc:
            return {"ok": False, "error": str(exc), "entries": [], "path": path}
        except ssh_helpers.HostKeyChanged as exc:
            return {"ok": False, "error": str(exc), "host_key_changed": True,
                    "entries": [], "path": path}
        except (ssh_helpers.SSHUnavailable, ftp_helpers.FTPUnavailable) as exc:
            return {"ok": False, "error": str(exc), "retryable": True,
                    "entries": [], "path": path}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "entries": [], "path": path}

    def mkdir(self, session_id: int, path: str, name: str) -> dict:
        if not is_safe_name(name):
            return {"ok": False, "error": "That name is not allowed"}

        def _work(sftp: paramiko.SFTPClient) -> dict:
            sftp.mkdir(f"{path.rstrip('/')}/{name}")
            return {"ok": True}

        return self._guarded(session_id, _work)

    def rename(self, session_id: int, old_path: str, new_name: str) -> dict:
        if not is_safe_name(new_name):
            return {"ok": False, "error": "That name is not allowed"}

        parent = old_path.rstrip("/").rsplit("/", 1)[0] or "/"
        new_path = f"{parent.rstrip('/')}/{new_name}" if parent != "/" else f"/{new_name}"

        def _work(sftp: paramiko.SFTPClient) -> dict:
            sftp.rename(old_path, new_path)
            return {"ok": True, "path": new_path}

        return self._guarded(session_id, _work)

    def delete(self, session_id: int, paths: list) -> dict:
        """Remove files and directories, recursively. Returns per-path results."""
        def _work(sftp: paramiko.SFTPClient) -> dict:
            removed, failed = [], []
            for target in paths:
                try:
                    _remove_recursive(sftp, target)
                    removed.append(target)
                except Exception as exc:
                    failed.append({"path": target, "error": str(exc)})
            return {"ok": not failed, "removed": removed, "failed": failed}

        return self._guarded(session_id, _work)

    @bff_stream()
    async def walk(self, session_id: int, path: str):
        """
        Stream every file under ``path``, deepest-first as they are found.

        The original walked the whole tree and returned one JSON blob, so a
        large directory looked frozen until it finished. Streaming lets the
        progress count move while the walk is still running.
        """
        session = self._session(session_id)
        if session is None:
            yield {"error": {"message": "Session not found"}}
            return

        loop = asyncio.get_event_loop()
        queue: list[str] = [path]
        total_bytes = 0
        total_files = 0

        while queue:
            current = queue.pop(0)

            def _listing(target: str = current) -> list:
                conn = sftp_pool.acquire(self._user_id, int(session_id), session)
                with conn.lock:
                    conn.last_used = time.monotonic()
                    return conn.sftp.listdir_attr(target)

            try:
                attrs = await loop.run_in_executor(_pool, _listing)
            except Exception as exc:
                yield {"error": {"message": f"{current}: {exc}"}}
                continue

            for attr in attrs:
                child = f"{current.rstrip('/')}/{attr.filename}"
                if stat_module.S_ISDIR(attr.st_mode or 0):
                    queue.append(child)
                else:
                    total_files += 1
                    total_bytes += int(attr.st_size or 0)
                    yield {
                        "path": child,
                        "size": int(attr.st_size or 0),
                        "files": total_files,
                        "bytes": total_bytes,
                    }

        yield {"done": True, "files": total_files, "bytes": total_bytes}

    def retain(self, session_id: int) -> dict:
        """
        Register a pane's interest in a session's SFTP channel.

        Called when a pane first opens its Files tab, and balanced by
        ``disconnect`` when that pane closes.
        """
        if self._session(session_id) is None:
            return {"ok": False, "error": "Session not found"}
        holds = sftp_pool.retain(self._user_id, int(session_id))
        return {"ok": True, "holds": holds}

    def disconnect(self, session_id: int) -> dict:
        """Drop this pane's hold; the channel closes when the last one goes."""
        holds = sftp_pool.release(self._user_id, int(session_id))
        return {"ok": True, "holds": holds}

    # ------------------------------------------------------------------

    def _guarded(self, session_id: int, work: Callable) -> dict:
        try:
            return self._run(session_id, work)
        except ssh_helpers.HostKeyChanged as exc:
            return {"ok": False, "error": str(exc), "host_key_changed": True}
        except (ssh_helpers.SSHUnavailable, ftp_helpers.FTPUnavailable) as exc:
            # Already phrased for a person; paramiko's own text is not.
            return {"ok": False, "error": str(exc), "retryable": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


def _remove_recursive(sftp: paramiko.SFTPClient, path: str) -> None:
    """
    Delete a file, or a directory and everything under it.

    The original wrapped the whole recursive walk in ``except IOError`` and
    fell back to ``remove(path)``, so a permission error on a grandchild ended
    as a confusing "cannot remove directory". Only the listing decides whether
    this is a directory.
    """
    try:
        entries = sftp.listdir_attr(path)
    except IOError:
        sftp.remove(path)  # not a directory
        return

    for attr in entries:
        child = f"{path.rstrip('/')}/{attr.filename}"
        if stat_module.S_ISDIR(attr.st_mode or 0):
            _remove_recursive(sftp, child)
        else:
            sftp.remove(child)
    sftp.rmdir(path)
