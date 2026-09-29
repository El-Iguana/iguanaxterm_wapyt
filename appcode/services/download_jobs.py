"""
Server-side folder copies, and the registry that tracks them.

A plain module, not a BFF one: pytincture re-executes a BFF module on every
call, so a registry defined in ``downloads.py`` was empty again by the time
the browser asked for a job's status. See ``pool.py``.
"""
from __future__ import annotations

import os
import posixpath
import sys
import stat as stat_module
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from services.auth import current_user_id
from services.db import DATA_DIR, fetch_session
from services.paths import LocalNames, is_safe_name, windows_safe_name

# The downloads folder is on this machine's disk. On a native Windows install
# (tools/windows) that is Windows: a remote Linux name such as "a:b", "CON" or
# "..\\x" would fail there, or -- a backslash being a separator -- walk out of
# the folder. Each segment is cleaned as the browser-side download does.
_LOCAL_WINDOWS = sys.platform == "win32"
_LOCAL_CASE_INSENSITIVE = sys.platform in ("win32", "darwin")


def local_segment(name: str) -> str:
    """One remote name as a single path segment on this machine's disk."""
    return windows_safe_name(name) if _LOCAL_WINDOWS else name


def local_names() -> LocalNames:
    """Remote relative path -> local one, for one folder copy."""
    return LocalNames(windows=_LOCAL_WINDOWS, case_insensitive=_LOCAL_CASE_INSENSITIVE)

_CHUNK = 256 * 1024

# Finished jobs stay visible this long, so a poll that races the end still
# sees the outcome.
_KEEP_FINISHED_SECONDS = 600


def download_root() -> Path:
    return Path(os.environ.get("GANXTERM_DOWNLOAD_DIR") or DATA_DIR / "downloads")


def display_root() -> str:
    """
    Where the downloads folder is *for the person*, for messages only.

    Inside the container it is ``/downloads``, which means nothing on the
    desktop. The run script passes the host folder it mounted there as
    ``GANXTERM_DOWNLOAD_HOST_DIR`` (``~/Downloads/IguanaXterm``); without it
    (running outside a container) the real path is the one to show.
    """
    return (os.environ.get("GANXTERM_DOWNLOAD_HOST_DIR") or str(download_root())).rstrip("/")


def user_dir_name(user: dict) -> str:
    """This user's folder name inside the downloads folder."""
    name = str((user or {}).get("username") or "")
    if not is_safe_name(name) or name.startswith("."):
        name = f"user-{current_user_id(user)}"
    return local_segment(name)


def user_root(user: dict) -> Path:
    """
    This user's downloads directory, created on first use.

    Named after the username so it reads sensibly on the host, falling back to
    the id for a name that is not a safe single path segment.
    """
    root = download_root() / user_dir_name(user)
    root.mkdir(parents=True, exist_ok=True)
    return root


def resolve_inside(root: Path, relative: str) -> Path:
    """
    ``relative`` under ``root``, or ``PermissionError``.

    Resolved, symlinks included, before the containment check -- a link in
    the downloads directory must not become a way out of it.
    """
    root = root.resolve()
    target = (root / (relative or "").lstrip("/")).resolve()
    if target != root and root not in target.parents:
        raise PermissionError("Path is outside the downloads folder")
    return target


def free_name(parent: Path, name: str) -> Path:
    """``name``, or ``name (2)``... if something already has it."""
    candidate, n = parent / name, 1
    while candidate.exists():
        n += 1
        candidate = parent / f"{name} ({n})"
    return candidate


def local_path(folder: Path, local_relative: str) -> Path:
    """
    A mapped, ``/``-separated relative path under ``folder``, checked to stay
    inside it (``resolve_inside``) as a second line behind the name cleaning.
    """
    try:
        return resolve_inside(folder, local_relative)
    except PermissionError as exc:
        raise ValueError(f"Not saved: {local_relative!r} would leave the folder") from exc


# ── Jobs ──────────────────────────────────────────────────────────────────────


class _Job:
    def __init__(self, user_id: int, session_id: int, remote: str, local: Path, root: Path,
                 shown_root: str = "") -> None:
        self.id = uuid.uuid4().hex
        self.user_id = user_id
        self.session_id = session_id
        self.remote = remote
        self.local = local
        self.root = root
        self.state = "walking"          # walking, copying, done, failed, cancelled
        self.error = ""
        self.files_total = 0
        self.bytes_total = 0
        self.files_done = 0
        self.bytes_done = 0
        self.current = ""
        self.failed: list[dict] = []
        self.cancel = threading.Event()
        self.finished_at: Optional[float] = None
        self.shown_root = shown_root  # the user's folder as the person sees it

    def view(self) -> dict:
        return {
            "id": self.id,
            "state": self.state,
            "error": self.error,
            "folder": self.local.relative_to(self.root).as_posix(),
            "location": f"{self.shown_root}/{self.local.relative_to(self.root).as_posix()}",
            "files_total": self.files_total,
            "bytes_total": self.bytes_total,
            "files_done": self.files_done,
            "bytes_done": self.bytes_done,
            "current": self.current,
            "failed": self.failed[:20],
            "failed_count": len(self.failed),
        }


_jobs: dict[str, _Job] = {}
_jobs_lock = threading.Lock()


def _prune() -> None:
    cutoff = time.monotonic() - _KEEP_FINISHED_SECONDS
    with _jobs_lock:
        for job_id in [k for k, j in _jobs.items() if j.finished_at and j.finished_at < cutoff]:
            _jobs.pop(job_id, None)


def _run(job: _Job, profile: dict) -> None:
    """The copy itself, on its own thread."""
    from services.pool import transfer_pool

    try:
        conn = transfer_pool.acquire(job.user_id, job.session_id, profile)

        # Walk first, so the progress bar has a real total.
        files: list[tuple[str, str, int]] = []   # remote, relative, size
        names = local_names()
        pending = [job.remote]
        while pending and not job.cancel.is_set():
            current = pending.pop(0)
            try:
                with conn.lock:
                    conn.last_used = time.monotonic()
                    entries = conn.sftp.listdir_attr(current)
            except Exception as exc:
                if current == job.remote:
                    raise  # the folder itself is unreadable: nothing to save
                # One unreadable subfolder costs that subfolder, not the job.
                job.failed.append({"path": posixpath.relpath(current, job.remote) + "/",
                                   "error": str(exc)})
                continue
            for attr in entries:
                if not is_safe_name(attr.filename):
                    continue
                child = posixpath.join(current, attr.filename)
                relative = posixpath.relpath(child, job.remote)
                if stat_module.S_ISDIR(attr.st_mode or 0):
                    pending.append(child)
                    local_path(job.local, names.map_dir(relative)).mkdir(parents=True, exist_ok=True)
                else:
                    files.append((child, relative, int(attr.st_size or 0)))
                    job.files_total += 1
                    job.bytes_total += int(attr.st_size or 0)

        job.state = "copying"
        for remote, relative, _size in files:
            if job.cancel.is_set():
                break
            job.current = relative
            target = local_path(job.local, names.map(relative))
            partial = target.with_name(target.name + ".part")
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                with conn.lock:
                    conn.last_used = time.monotonic()
                    source = conn.sftp.open(remote, "rb")
                    try:
                        with open(partial, "wb") as sink:
                            while not job.cancel.is_set():
                                chunk = source.read(_CHUNK)
                                if not chunk:
                                    break
                                sink.write(chunk)
                                job.bytes_done += len(chunk)
                    finally:
                        source.close()
                if job.cancel.is_set():
                    partial.unlink(missing_ok=True)
                    break
                partial.replace(target)
                job.files_done += 1
            except Exception as exc:
                partial.unlink(missing_ok=True)
                if job.cancel.is_set():
                    # Cutting a transfer short makes FTP answer "426 aborted";
                    # that is the cancel working, not a failed file.
                    break
                job.failed.append({"path": relative, "error": str(exc)})

        job.current = ""
        if job.cancel.is_set():
            job.state = "cancelled"
        elif job.failed and not job.files_done:
            job.state = "failed"
            job.error = job.failed[0]["error"]
        else:
            job.state = "done"
    except Exception as exc:
        job.state = "failed"
        job.error = str(exc)
    finally:
        job.finished_at = time.monotonic()


def start_job(user: dict, session_id: int, remote: str) -> _Job:
    user_id = current_user_id(user)
    profile = fetch_session(int(session_id), user_id)
    if profile is None:
        raise PermissionError("Session not found")
    name = posixpath.basename(remote.rstrip("/")) or "download"
    if not is_safe_name(name):
        raise ValueError("That folder name cannot be saved")
    root = user_root(user)
    local = free_name(root, local_segment(name))
    local.mkdir(parents=True)
    job = _Job(user_id, int(session_id), remote.rstrip("/") or "/", local, root,
               shown_root=f"{display_root()}/{user_dir_name(user)}")
    with _jobs_lock:
        _jobs[job.id] = job
    threading.Thread(target=_run, args=(job, profile), daemon=True,
                     name=f"server-download-{job.id[:8]}").start()
    return job


def _owned_job(user_id: int, job_id: str) -> Optional[_Job]:
    with _jobs_lock:
        job = _jobs.get(str(job_id))
    return job if job is not None and job.user_id == user_id else None
