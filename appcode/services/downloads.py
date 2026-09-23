"""
Folder downloads saved on the server, for browsers that cannot pick a folder.

Chrome and Edge let the page write a folder download wherever the person
chooses (File System Access). Firefox cannot, and no browser can on a
non-secure origin. There the folder is copied into the server's downloads
directory instead -- ``GANXTERM_DOWNLOAD_DIR``, one subdirectory per user --
and fetched from there file by file, or straight off the disk when the server
is the machine in front of you.

**A job, not a stream.** A ``@bff_stream`` would be the obvious shape, but
pytincture caps a stream at 300 s and 30 s between items, and a folder of
photos over FTP outlasts both. The copy runs on its own thread; the browser
starts it, polls ``status``, and may ``cancel``. Closing the tab does not stop
it, which is the point of saving to the server.

A file is written as ``name.part`` and renamed when complete, so a cancelled
or failed copy never leaves a truncated file under the real name.
"""
from __future__ import annotations

import shutil
import stat as stat_module
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pytincture.dataclass import backend_for_frontend, bff_policy

from services.auth import current_user_id
from services.download_jobs import (  # noqa: F401  (re-exported for tests)
    _owned_job,
    _prune,
    download_root,
    free_name,
    resolve_inside,
    start_job,
    user_root,
)
from services.paths import file_icon, format_mode, format_mtime, format_size


# ── BFF ───────────────────────────────────────────────────────────────────────


def describe_local(path: Path, root: Path) -> dict:
    """A downloads-folder entry in the same row shape as a remote listing."""
    info = path.lstat()
    is_dir = stat_module.S_ISDIR(info.st_mode)
    is_link = stat_module.S_ISLNK(info.st_mode)
    return {
        "id": "/" + path.relative_to(root).as_posix(),
        "name": path.name,
        "icon": file_icon(path.name, is_dir=is_dir, is_link=is_link),
        "is_dir": is_dir,
        "is_link": is_link,
        "size": "" if is_dir else format_size(info.st_size),
        "size_bytes": 0 if is_dir else int(info.st_size),
        "modified": format_mtime(info.st_mtime),
        "mtime": int(info.st_mtime),
        "permissions": format_mode(info.st_mode),
    }


@backend_for_frontend
@bff_policy(application="iguanaxterm")
class DownloadService:
    def __init__(self, _user: dict = None) -> None:
        self._user = _user or {}
        self._user_id = current_user_id(self._user)

    def start(self, session_id: int, path: str) -> dict:
        """Begin copying a remote folder into this user's downloads folder."""
        if not self._user_id:
            return {"ok": False, "error": "Not authenticated"}
        _prune()
        try:
            job = start_job(self._user, int(session_id), path)
        except (PermissionError, ValueError, OSError) as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "job": job.view()}

    def status(self, job_id: str) -> dict:
        job = _owned_job(self._user_id, job_id)
        if job is None:
            return {"ok": False, "error": "No such download"}
        return {"ok": True, "job": job.view()}

    def cancel(self, job_id: str) -> dict:
        job = _owned_job(self._user_id, job_id)
        if job is None:
            return {"ok": False, "error": "No such download"}
        job.cancel.set()
        return {"ok": True}

    def list(self, path: str = "") -> dict:
        """One directory of this user's downloads folder."""
        if not self._user_id:
            return {"ok": False, "error": "Not authenticated", "entries": [], "path": "/"}
        root = user_root(self._user).resolve()
        try:
            target = resolve_inside(root, path)
            entries = [
                describe_local(child, root)
                for child in target.iterdir()
                if not child.name.endswith(".part")   # a copy still in flight
            ]
        except (PermissionError, OSError) as exc:
            return {"ok": False, "error": str(exc), "entries": [], "path": path or "/"}
        entries.sort(key=lambda item: (not item["is_dir"], item["name"].lower()))
        relative = target.relative_to(root).as_posix()
        return {"ok": True, "path": "/" if relative == "." else "/" + relative,
                "entries": entries}

    def delete(self, paths: list) -> dict:
        if not self._user_id:
            return {"ok": False, "error": "Not authenticated"}
        root = user_root(self._user).resolve()
        removed, failed = [], []
        for path in paths or []:
            try:
                target = resolve_inside(root, path)
                if target == root:
                    raise PermissionError("Refusing to delete the downloads folder itself")
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
                else:
                    target.unlink()
                removed.append(path)
            except Exception as exc:
                failed.append({"path": path, "error": str(exc)})
        return {"ok": not failed, "removed": removed, "failed": failed}


# ── Fetching a saved file ─────────────────────────────────────────────────────

router = APIRouter(prefix="/downloads")


@router.get("/file")
async def saved_file(request: Request, path: str) -> FileResponse:
    """Stream one file from the caller's downloads folder to the browser."""
    session = getattr(request, "session", None)
    user = session.get("user") if session else None
    if not current_user_id(user if isinstance(user, dict) else None):
        raise HTTPException(status_code=401, detail="Not authenticated")
    root = user_root(user).resolve()
    try:
        target = resolve_inside(root, path)
    except PermissionError:
        raise HTTPException(status_code=404, detail="File not found")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(target, filename=target.name, media_type="application/octet-stream")
