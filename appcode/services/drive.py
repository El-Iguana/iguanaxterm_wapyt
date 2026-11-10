"""
The RDP drive (#76): a folder per person per site that guacd shows in
Windows as a drive called "IguanaXterm", and that IguanaXterm's own file
browser shows here. Files go in and out through IguanaXterm's uploads and
downloads, never through Guacamole's file channel, so the usual limits,
resume and server-side folder saves all apply.

    GANXTERM_DRIVE_DIR        the drives' root, as IguanaXterm sees it.
                              Unset = no drives (the setting is hidden).
    GANXTERM_GUACD_DRIVE_DIR  the same folder as guacd sees it (default: the
                              same path, which is how the containers mount it)
    GANXTERM_DRIVE_MAX_BYTES  cap per folder (default 2 GiB)

The folder is ``<root>/<user_id>/<session_id>``, built from ids only and
checked with realpath, so nothing a person types can move it. guacd and
IguanaXterm must write as the same uid (the containers run guacd as
10001:999), or one couldn't change what the other wrote.

A plain module, not a BFF one: nothing here may be rebuilt per call.
"""
from __future__ import annotations

import errno
import os
import posixpath
import shutil
import stat as stat_mode
from typing import Optional

import paramiko

from services import guacd

DRIVE_NAME = "IguanaXterm"
DEFAULT_MAX_BYTES = 2 * 1024 ** 3


def root() -> Optional[str]:
    value = os.environ.get("GANXTERM_DRIVE_DIR", "").strip()
    return os.path.realpath(value) if value else None


def enabled() -> bool:
    """Drives need both a folder and guacd."""
    return bool(root()) and guacd.enabled()


def max_bytes() -> int:
    try:
        value = int(os.environ.get("GANXTERM_DRIVE_MAX_BYTES", "") or DEFAULT_MAX_BYTES)
    except ValueError:
        return DEFAULT_MAX_BYTES
    return max(1, value)


def _ids(user_id, session_id) -> tuple[str, str]:
    user, site = int(user_id), int(session_id)
    if user <= 0 or site <= 0:
        raise ValueError("bad id")
    return str(user), str(site)


def folder(user_id, session_id, create: bool = False) -> str:
    """This person's folder for this site. Raises ValueError if it would escape."""
    base = root()
    if not base:
        raise PermissionError("Drives aren't set up on this server")
    user, site = _ids(user_id, session_id)
    path = os.path.join(base, user, site)
    if create:
        os.makedirs(path, mode=0o770, exist_ok=True)
    real = os.path.realpath(path)
    if not real.startswith(base + os.sep):
        raise ValueError("drive folder outside the drives root")
    return real


def guacd_folder(user_id, session_id) -> str:
    """The same folder as guacd sees it."""
    base = os.environ.get("GANXTERM_GUACD_DRIVE_DIR", "").strip() or root() or ""
    user, site = _ids(user_id, session_id)
    return posixpath.join(base.rstrip("/") or "/", user, site)


def used_bytes(path: str) -> int:
    total = 0
    for parent, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(parent, name)).st_size
            except OSError:
                pass
    return total


def remove_site(session_id) -> None:
    """Everyone's folder for a deleted site."""
    base = root()
    if not base or not os.path.isdir(base):
        return
    site = str(int(session_id))
    for user in os.listdir(base):
        if user.isdigit():
            shutil.rmtree(os.path.join(base, user, site), ignore_errors=True)


def remove_user(user_id) -> None:
    """A deleted account's folders."""
    base = root()
    if base:
        shutil.rmtree(os.path.join(base, str(int(user_id))), ignore_errors=True)


# ── The file browser's view of a drive ──────────────────────────────────────


class DriveFull(IOError):
    pass


class _Transport:
    def __init__(self, owner: "DriveFiles") -> None:
        self.owner = owner

    def is_active(self) -> bool:
        return not self.owner.closed


class _Handle:
    """A local file with the slice of paramiko's SFTPFile the transfers use."""

    def __init__(self, raw, budget: Optional[int] = None) -> None:
        self.raw = raw
        self.budget = budget

    def read(self, size: int = -1) -> bytes:
        return self.raw.read(size)

    def write(self, data: bytes) -> int:
        if self.budget is not None:
            if len(data) > self.budget:
                raise DriveFull(errno.ENOSPC, f"The drive is full (its cap is "
                                              f"{max_bytes() // (1024 * 1024)} MiB)")
            self.budget -= len(data)
        return self.raw.write(data)

    def seek(self, offset: int, whence: int = 0) -> int:
        return self.raw.seek(offset, whence)

    def prefetch(self, *_args) -> None:
        pass

    def set_pipelined(self, *_args) -> None:
        pass

    def close(self) -> None:
        self.raw.close()


class DriveFiles:
    """
    A drive folder answering the calls the file browser and transfers make
    on paramiko's SFTPClient, like ``ftp.FTPFiles`` does for FTP.

    ``can_upload`` False refuses every change from IguanaXterm (write, mkdir,
    rename, delete); ``can_download`` False refuses reads. Listing always
    works. Every path is resolved inside the folder; ``..`` and links can't
    leave it.
    """

    def __init__(self, base: str, can_upload: bool = True, can_download: bool = True) -> None:
        self.base = os.path.realpath(base)
        self.can_upload = can_upload
        self.can_download = can_download
        self.closed = False

    # pool plumbing
    def get_transport(self) -> _Transport:
        return _Transport(self)

    def open_sftp(self) -> "DriveFiles":
        return self

    def close(self) -> None:
        self.closed = True

    # paths
    def _local(self, path: str) -> str:
        relative = posixpath.normpath("/" + (path or "/").replace("\\", "/")).lstrip("/")
        local = os.path.realpath(os.path.join(self.base, *[p for p in relative.split("/") if p]))
        if local != self.base and not local.startswith(self.base + os.sep):
            raise PermissionError(errno.EACCES, "Outside the drive", path)
        return local

    def _write_allowed(self, path: str) -> None:
        if not self.can_upload:
            raise PermissionError(errno.EACCES, "This site's drive doesn't accept files "
                                                "from IguanaXterm", path)

    def normalize(self, path: str) -> str:
        clean = "/" + posixpath.normpath("/" + (path or "/").replace("\\", "/")).lstrip("/")
        self._local(clean)
        return clean

    def stat(self, path: str) -> paramiko.SFTPAttributes:
        local = self._local(path)
        return paramiko.SFTPAttributes.from_stat(os.stat(local), posixpath.basename(local))

    def listdir_attr(self, path: str = "/") -> list:
        local = self._local(path)
        entries = []
        for name in sorted(os.listdir(local)):
            try:
                st = os.lstat(os.path.join(local, name))
            except OSError:
                continue
            if stat_mode.S_ISLNK(st.st_mode):
                continue                     # links could point out of the drive
            entries.append(paramiko.SFTPAttributes.from_stat(st, name))
        return entries

    def mkdir(self, path: str, mode: int = 0o770) -> None:
        self._write_allowed(path)
        os.mkdir(self._local(path), mode)

    def rmdir(self, path: str) -> None:
        self._write_allowed(path)
        local = self._local(path)
        if local == self.base:
            raise PermissionError(errno.EACCES, "That's the drive itself", path)
        os.rmdir(local)

    def remove(self, path: str) -> None:
        self._write_allowed(path)
        os.remove(self._local(path))

    def rename(self, old_path: str, new_path: str) -> None:
        self._write_allowed(old_path)
        os.rename(self._local(old_path), self._local(new_path))

    def open(self, path: str, mode: str = "rb", bufsize: int = -1) -> _Handle:
        local = self._local(path)
        if "r" in mode and "+" not in mode:
            if not self.can_download:
                raise PermissionError(errno.EACCES, "This site's drive doesn't let files "
                                                    "out to IguanaXterm", path)
            return _Handle(open(local, "rb"))
        self._write_allowed(path)
        replaced = os.path.getsize(local) if os.path.isfile(local) else 0
        budget = max_bytes() - (used_bytes(self.base) - replaced)
        if budget <= 0:
            raise DriveFull(errno.ENOSPC, f"The drive is full (its cap is "
                                          f"{max_bytes() // (1024 * 1024)} MiB)")
        return _Handle(open(local, "wb"), budget=budget)


def has_files(session_type: Optional[str], options: Optional[dict]) -> bool:
    """Whether a site's pane gets Files: its type's, or an RDP site's drive."""
    from services.paths import session_caps

    if session_caps(session_type)["files"]:
        return True
    return session_type == "rdp" and bool((options or {}).get("drive")) and enabled()


def connect(profile: dict, user_id: int) -> DriveFiles:
    """The file browser's client for this person's drive on an RDP site."""
    from services.rdp_ws import rdp_options

    options = rdp_options(profile)
    if not (enabled() and options["drive"]):
        raise PermissionError("This connection has no file browser")
    return DriveFiles(folder(user_id, profile["id"], create=True),
                      can_upload=options["drive_upload"],
                      can_download=options["drive_download"])
