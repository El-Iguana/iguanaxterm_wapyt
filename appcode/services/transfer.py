"""
Binary file transfer over SFTP: plain FastAPI routes, deliberately not BFF.

A JSON BFF would mean base64 — a third larger on the wire, and the whole
payload resident in Pyodide's heap on the way past. The dhxpyt rewrite did
exactly that. These routes stream instead, so a multi-gigabyte file costs a
buffer, not its own size, on either side.

The browser reaches them with an ordinary ``fetch``/anchor click carrying the
same session cookie, so they share the app's authentication.
"""
from __future__ import annotations

import asyncio
import posixpath
from typing import Iterator, Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse

from services.auth import current_user_id
from services.db import fetch_session
from services.paths import is_safe_name
from services.sftp_service import sftp_pool

router = APIRouter(prefix="/files")

# 256 KiB balances syscall overhead against peak memory per in-flight transfer.
_CHUNK = 256 * 1024


def _require_user(request: Request) -> int:
    session = getattr(request, "session", None)
    user = session.get("user") if session else None
    user_id = current_user_id(user if isinstance(user, dict) else None)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user_id


def _require_profile(request: Request, session_id: int) -> tuple[int, dict]:
    user_id = _require_user(request)
    profile = fetch_session(int(session_id), user_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return user_id, profile


@router.get("/{session_id}/download")
async def download(request: Request, session_id: int, path: str) -> StreamingResponse:
    """Stream one remote file to the browser."""
    user_id, profile = _require_profile(request, session_id)
    loop = asyncio.get_running_loop()

    def _open():
        conn = sftp_pool.acquire(user_id, int(session_id), profile)
        with conn.lock:
            attrs = conn.sftp.stat(path)
            handle = conn.sftp.open(path, "rb")
            handle.prefetch(attrs.st_size or 0)
            return conn, handle, int(attrs.st_size or 0)

    try:
        conn, handle, size = await loop.run_in_executor(None, _open)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    def _stream() -> Iterator[bytes]:
        # The pooled channel is single-threaded; hold its lock for the whole
        # transfer so a concurrent listing cannot interleave on it.
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
        # RFC 5987 form so non-ASCII names survive.
        "Content-Disposition": (
            f"attachment; filename*=UTF-8''{_quote(filename)}"
        ),
    }
    if size:
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
) -> dict:
    """Stream one uploaded file to the remote directory."""
    user_id, profile = _require_profile(request, session_id)

    name = posixpath.basename(file.filename or "")
    if not is_safe_name(name):
        raise HTTPException(status_code=400, detail="Invalid filename")
    remote_path = posixpath.join(path or "/", name)

    loop = asyncio.get_running_loop()
    conn = await loop.run_in_executor(
        None, lambda: sftp_pool.acquire(user_id, int(session_id), profile)
    )

    def _open_remote():
        handle = conn.sftp.open(remote_path, "wb")
        handle.set_pipelined(True)
        return handle

    written = 0
    # Read from the client on the event loop, write to SFTP on a worker —
    # never the whole file in memory, unlike the original's `await file.read()`.
    with conn.lock:
        handle = await loop.run_in_executor(None, _open_remote)
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


def _quote(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")
