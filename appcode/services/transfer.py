"""
Binary file transfer over SFTP: plain FastAPI routes, deliberately not BFF.

A JSON BFF would mean base64 — a third larger on the wire, and the whole
payload resident in Pyodide's heap on the way past. The dhxpyt rewrite did
exactly that. These routes stream instead, so a multi-gigabyte file costs a
buffer, not its own size, on either side.

The browser reaches them with an ordinary ``fetch``/``XMLHttpRequest`` carrying
the same session cookie, so they share the app's authentication.

Transfers run on their own connection pool. A queued folder download holds one
SFTP channel for as long as it takes, and sharing the interactive pool would
freeze the file browser behind it — which is what happened in the original app.
"""
from __future__ import annotations

import asyncio
import hmac
import posixpath
import time
from typing import Iterator, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from python_multipart.multipart import MultipartParser, parse_options_header
from starlette.requests import ClientDisconnect

from services.auth import current_user_id
from services.db import fetch_session
from services.paths import is_safe_name
from services import ftp as ftp_helpers
from services import ssh as ssh_helpers
from services.pool import transfer_pool

router = APIRouter(prefix="/files")

# 256 KiB balances syscall overhead against peak memory per in-flight transfer.
_CHUNK = 256 * 1024

# transfer_pool is separate from the interactive sftp_pool on purpose -- see
# the module docstring. Both live in pool.py.


def _require_user(request: Request) -> int:
    session = getattr(request, "session", None)
    user = session.get("user") if session else None
    user_id = current_user_id(user if isinstance(user, dict) else None)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user_id


def _require_csrf(request: Request) -> None:
    """
    Mirror pytincture's CSRF check for our own state-changing routes.

    pytincture validates CSRF inside its BFF handler, which these plain routes
    never pass through. The session cookie is SameSite=lax, so a cross-site
    POST is already blocked, but a route that writes to a remote host should
    not depend on a cookie attribute a deployment can override.
    """
    session = getattr(request, "session", None)
    expected = (session or {}).get("csrf_token", "")
    supplied = request.headers.get("x-csrf-token", "")
    if not expected or not supplied or not hmac.compare_digest(str(expected), supplied):
        raise HTTPException(status_code=403, detail="CSRF validation failed")


def _require_profile(request: Request, session_id: int) -> tuple[int, dict]:
    user_id = _require_user(request)
    profile = fetch_session(int(session_id), user_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return user_id, profile


def _safe_relative(relative: str) -> str:
    """
    Validate a client-supplied folder-relative path for a directory upload.

    ``webkitRelativePath`` is attacker-influencable in principle, so each
    segment is checked rather than trusting the joined string.
    """
    parts = [part for part in (relative or "").split("/") if part]
    if not parts:
        raise HTTPException(status_code=400, detail="Invalid path")
    for part in parts:
        if not is_safe_name(part):
            raise HTTPException(status_code=400, detail="Invalid path segment")
    return "/".join(parts)


@router.get("/{session_id}/download")
async def download(request: Request, session_id: int, path: str) -> StreamingResponse:
    """Stream one remote file to the browser."""
    user_id, profile = _require_profile(request, session_id)
    loop = asyncio.get_running_loop()

    def _open():
        conn = transfer_pool.acquire(user_id, int(session_id), profile)
        with conn.lock:
            attrs = conn.sftp.stat(path)
            handle = conn.sftp.open(path, "rb")
            handle.prefetch(attrs.st_size or 0)
            return conn, handle, int(attrs.st_size or 0)

    try:
        conn, handle, size = await loop.run_in_executor(None, _open)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found")
    except (ssh_helpers.SSHUnavailable, ftp_helpers.FTPUnavailable) as exc:
        # 503, not 400: the request was fine, the host was not.
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    def _stream() -> Iterator[bytes]:
        # The pooled channel is single-threaded; hold its lock for the whole
        # transfer so a concurrent operation cannot interleave on it.
        try:
            with conn.lock:
                while True:
                    chunk = handle.read(_CHUNK)
                    if not chunk:
                        break
                    yield chunk
        finally:
            try:
                handle.close()
            except Exception:
                pass
            conn.last_used = time.monotonic()

    filename = posixpath.basename(path) or "download"
    headers = {
        # RFC 5987 form so non-ASCII names survive. Only the anchor fallback
        # reads this; a picker download names the file from the handle.
        "Content-Disposition": f"attachment; filename*=UTF-8''{_quote(filename)}",
    }
    if size:
        # The browser needs this to show real download progress.
        headers["Content-Length"] = str(size)

    return StreamingResponse(
        _stream(), media_type="application/octet-stream", headers=headers
    )


# Non-file form fields are a path and a relative path; anything bigger is not
# ours. Bounds what one part can make the server hold before the file arrives.
_MAX_FIELD_BYTES = 16 * 1024


class _UploadParts:
    """
    Callbacks for python-multipart's push parser.

    Collects the small text fields, and queues the file part's bytes for the
    route to drain after each ``write()``. The queue never holds more than one
    network read's worth, so memory does not grow with the file.
    """

    def __init__(self) -> None:
        self.fields: dict[str, str] = {}
        self.filename: Optional[str] = None
        self.file_started = False
        self.file_ended = False
        self.pending: list[bytes] = []
        self._headers: dict[bytes, bytes] = {}
        self._field = b""
        self._value = b""
        self._name = ""
        self._buffer = bytearray()

    def callbacks(self) -> dict:
        return {
            "on_part_begin": self._part_begin,
            "on_header_field": self._header_field,
            "on_header_value": self._header_value,
            "on_header_end": self._header_end,
            "on_headers_finished": self._headers_finished,
            "on_part_data": self._part_data,
            "on_part_end": self._part_end,
        }

    def _part_begin(self) -> None:
        self._headers = {}
        self._name = ""
        self._buffer = bytearray()

    def _header_field(self, data: bytes, start: int, end: int) -> None:
        self._field += data[start:end]

    def _header_value(self, data: bytes, start: int, end: int) -> None:
        self._value += data[start:end]

    def _header_end(self) -> None:
        self._headers[self._field.lower()] = self._value
        self._field = self._value = b""

    def _headers_finished(self) -> None:
        _, options = parse_options_header(self._headers.get(b"content-disposition", b""))
        self._name = options.get(b"name", b"").decode("utf-8", "replace")
        if self._name == "file":
            self.filename = options.get(b"filename", b"").decode("utf-8", "replace")
            self.file_started = True

    def _part_data(self, data: bytes, start: int, end: int) -> None:
        if self._name == "file":
            self.pending.append(bytes(data[start:end]))
            return
        self._buffer += data[start:end]
        if len(self._buffer) > _MAX_FIELD_BYTES:
            raise HTTPException(status_code=400, detail="Form field too large")

    def _part_end(self) -> None:
        if self._name == "file":
            self.file_ended = True
        elif self._name:
            self.fields[self._name] = self._buffer.decode("utf-8", "replace")


@router.post("/{session_id}/upload")
async def upload(request: Request, session_id: int) -> dict:
    """
    Stream one uploaded file to the remote directory.

    The multipart body is parsed *here*, as it arrives, rather than by
    FastAPI's ``Form``/``File`` parameters. Those parse the whole body before
    the handler runs: the file is spooled to local disk first, and it happens
    before the CSRF and ownership checks below, so anyone could make the
    server buffer an arbitrarily large body. Now nothing is read until the
    caller is known, and the bytes go straight to the remote.

    This route is the one path exempt from the app-wide 2 MiB body limit (see
    ``service.py``); ``GANXTERM_MAX_UPLOAD_BYTES`` caps it instead.

    The form carries ``path`` and optionally ``relative_path`` (a
    folder-relative position for a directory upload, whose intervening
    directories are created as needed), then ``file``. wapyt sends the fields
    first, so the destination is known by the time the file's bytes arrive.
    """
    _require_csrf(request)
    user_id, profile = _require_profile(request, session_id)

    content_type, options = parse_options_header(request.headers.get("content-type"))
    boundary = options.get(b"boundary")
    if content_type != b"multipart/form-data" or not boundary:
        raise HTTPException(status_code=400, detail="Expected multipart/form-data")

    loop = asyncio.get_running_loop()
    try:
        conn = await loop.run_in_executor(
            None, lambda: transfer_pool.acquire(user_id, int(session_id), profile)
        )
    except (ssh_helpers.SSHUnavailable, ftp_helpers.FTPUnavailable) as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except ssh_helpers.HostKeyChanged as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    parts = _UploadParts()
    parser = MultipartParser(boundary, parts.callbacks())
    handle = None
    remote_path = ""
    written = 0
    finished = False

    def _destination() -> tuple[str, str, str]:
        base = (parts.fields.get("path") or "/").rstrip("/") or "/"
        relative = parts.fields.get("relative_path")
        if relative:
            remote = posixpath.join(base, _safe_relative(relative))
            return base, remote, posixpath.dirname(remote)
        name = posixpath.basename(parts.filename or "")
        if not is_safe_name(name):
            raise HTTPException(status_code=400, detail="Invalid filename")
        return base, posixpath.join(base, name), base

    def _prepare(base: str, remote: str, parent: str):
        if parent != base:
            _mkdir_p(conn.sftp, parent)
        opened = conn.sftp.open(remote, "wb")
        opened.set_pipelined(True)
        return opened

    # The pooled channel is single-threaded. Waiting for it on a worker keeps
    # the event loop free: a plain `with conn.lock:` here would stall every
    # other request for as long as a download on this session holds it.
    await loop.run_in_executor(None, conn.lock.acquire)
    try:
        async for chunk in request.stream():
            parser.write(chunk)
            if parts.file_started and handle is None:
                base, remote_path, parent = _destination()
                try:
                    handle = await loop.run_in_executor(None, _prepare, base, remote_path, parent)
                except Exception as exc:
                    raise HTTPException(status_code=400, detail=str(exc))
            if parts.pending:
                data = b"".join(parts.pending)
                parts.pending.clear()
                await loop.run_in_executor(None, handle.write, data)
                written += len(data)
                conn.last_used = time.monotonic()
        parser.finalize()

        if handle is None or not parts.file_ended:
            raise HTTPException(status_code=400, detail="No file in the upload")
        await loop.run_in_executor(None, handle.close)
        finished = True
    except HTTPException:
        raise
    except ClientDisconnect:
        # Cancelled in the queue, or the tab went away.
        raise HTTPException(status_code=499, detail="Upload cancelled")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    finally:
        if handle is not None and not finished:
            # A half-written file under the real name looks like a good copy.
            # Take it away; the queue shows the upload as failed.
            def _discard() -> None:
                try:
                    handle.close()
                except Exception:
                    pass
                try:
                    conn.sftp.remove(remote_path)
                except Exception:
                    pass

            await loop.run_in_executor(None, _discard)
        conn.last_used = time.monotonic()
        conn.lock.release()

    return {"ok": True, "path": remote_path, "bytes": written}


def _mkdir_p(sftp, directory: str) -> None:
    """Create a remote directory and any missing parents."""
    parts = [part for part in directory.split("/") if part]
    current = ""
    for part in parts:
        current = f"{current}/{part}"
        try:
            sftp.stat(current)
        except IOError:
            sftp.mkdir(current)


def _quote(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")
