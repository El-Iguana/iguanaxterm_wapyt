"""
Server-side folder copies, and the registry that tracks them.

A plain module, not a BFF one: pytincture re-executes a BFF module on every
call, so a registry defined in ``downloads.py`` was empty again by the time
the browser asked for a job's status. See ``pool.py``.
"""
from __future__ import annotations

import os
import posixpath
import stat as stat_module
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from services.auth import current_user_id
from services.db import DATA_DIR, fetch_session
from services.paths import is_safe_name

_CHUNK = 256 * 1024

# Finished jobs stay visible this long, so a poll that races the end still
# sees the outcome.
_KEEP_FINISHED_SECONDS = 600


def download_root() -> Path:
    return Path(os.environ.get("GANXTERM_DOWNLOAD_DIR") or DATA_DIR / "downloads")


def user_root(user: dict) -> Path:
    """
    This user's downloads directory, created on first use.

    Named after the username so it reads sensibly on the host, falling back to
    the id for a name that is not a safe single path segment.
    """
    name = str((user or {}).get("username") or "")
    if not is_safe_name(name) or name.startswith("."):
        name = f"user-{current_user_id(user)}"
    root = download_root() / name
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


# ── Jobs ──────────────────────────────────────────────────────────────────────


class _Job:
    def __init__(self, user_id: int, session_id: int, remote: str, local: Path, root: Path) -> None:
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

    def view(self) -> dict:
        return {
            "id": self.id,
            "state": self.state,
            "error": self.error,
            "folder": self.local.relative_to(self.root).as_posix(),
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
                    (job.local / relative).mkdir(parents=True, exist_ok=True)
                else:
                    files.append((child, relative, int(attr.st_size or 0)))
                    job.files_total += 1
                    job.bytes_total += int(attr.st_size or 0)

        job.state = "copying"
        for remote, relative, _size in files:
            if job.cancel.is_set():
                break
            job.current = relative
            target = job.local / relative
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
    local = free_name(root, name)
    local.mkdir(parents=True)
    job = _Job(user_id, int(session_id), remote.rstrip("/") or "/", local, root)
    with _jobs_lock:
        _jobs[job.id] = job
    threading.Thread(target=_run, args=(job, profile), daemon=True,
                     name=f"server-download-{job.id[:8]}").start()
    return job


def _owned_job(user_id: int, job_id: str) -> Optional[_Job]:
    with _jobs_lock:
        job = _jobs.get(str(job_id))
    return job if job is not None and job.user_id == user_id else None
