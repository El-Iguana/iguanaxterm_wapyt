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
from typing import Iterator, Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse

from services.auth import current_user_id
from services.db import fetch_session
from services.paths import is_safe_name
from services import ftp as ftp_helpers
from services import ssh as ssh_helpers
from services.sftp_service import SFTPPool

router = APIRouter(prefix="/files")

# 256 KiB balances syscall overhead against peak memory per in-flight transfer.
_CHUNK = 256 * 1024

# Separate from sftp_service.sftp_pool on purpose — see the module docstring.
transfer_pool = SFTPPool()


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


@router.post("/{session_id}/upload")
async def upload(
    request: Request,
    session_id: int,
    path: str = Form(...),
    file: UploadFile = File(...),
    relative_path: Optional[str] = Form(None),
) -> dict:
    """
    Stream one uploaded file to the remote directory.

    ``relative_path`` carries a folder-relative position for a directory
    upload, and the intervening directories are created as needed.
    """
    _require_csrf(request)
    user_id, profile = _require_profile(request, session_id)

    base = (path or "/").rstrip("/") or "/"
    if relative_path:
        safe = _safe_relative(relative_path)
        remote_path = posixpath.join(base, safe)
        parent = posixpath.dirname(remote_path)
    else:
        name = posixpath.basename(file.filename or "")
        if not is_safe_name(name):
            raise HTTPException(status_code=400, detail="Invalid filename")
        remote_path = posixpath.join(base, name)
        parent = base

    loop = asyncio.get_running_loop()
    try:
        conn = await loop.run_in_executor(
            None, lambda: transfer_pool.acquire(user_id, int(session_id), profile)
        )
    except (ssh_helpers.SSHUnavailable, ftp_helpers.FTPUnavailable) as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    def _prepare():
        if parent != base:
            _mkdir_p(conn.sftp, parent)
        handle = conn.sftp.open(remote_path, "wb")
        handle.set_pipelined(True)
        return handle

    written = 0
    # Read from the client on the event loop, write to SFTP on a worker —
    # never the whole file in memory, unlike the original's `await file.read()`.
    with conn.lock:
        try:
            handle = await loop.run_in_executor(None, _prepare)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        try:
            while True:
                chunk = await file.read(_CHUNK)
                if not chunk:
                    break
                await loop.run_in_executor(None, handle.write, chunk)
                written += len(chunk)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        finally:
            await loop.run_in_executor(None, handle.close)

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
