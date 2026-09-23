"""
Folder downloads saved on the server: the copy job, and the downloads folder.

The happy path runs against a real in-process FTP server. The failure paths
use a small fake, because an unreadable subfolder or a slow file is easier to
stage than to provoke.
"""
from __future__ import annotations

import os
import stat as stat_module
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "appcode"))

import paramiko  # noqa: E402

from services import download_jobs, downloads  # noqa: E402

USER = {"user_id": 1, "username": "alice"}


@pytest.fixture(autouse=True)
def download_dir(tmp_path, monkeypatch):
    root = tmp_path / "downloads"
    monkeypatch.setenv("GANXTERM_DOWNLOAD_DIR", str(root))
    return root


def _wait(job, timeout=15):
    deadline = time.monotonic() + timeout
    while job.finished_at is None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert job.finished_at is not None, f"job still {job.state}"
    return job


# ── Live, against pyftpdlib ───────────────────────────────────────────────────


@pytest.fixture()
def ftp_profile(tmp_path, monkeypatch):
    pytest.importorskip("pyftpdlib")
    from pyftpdlib.authorizers import DummyAuthorizer
    from pyftpdlib.handlers import FTPHandler
    from pyftpdlib.servers import FTPServer

    remote = tmp_path / "remote"
    (remote / "Amber" / "2026").mkdir(parents=True)
    (remote / "Amber" / "a.jpg").write_bytes(os.urandom(700 * 1024))
    (remote / "Amber" / "2026" / "b.jpg").write_bytes(b"b" * 10)
    (remote / "Amber" / "empty").mkdir()

    authorizer = DummyAuthorizer()
    authorizer.add_user("u", "p", str(remote), perm="elradfmwMT")

    class Handler(FTPHandler):
        pass

    Handler.authorizer = authorizer
    server = FTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"timeout": 0.1}, daemon=True)
    thread.start()
    profile = {"id": 9, "type": "ftp", "host": "127.0.0.1", "port": server.address[1],
               "username": "u", "password": "p", "host_key": ""}
    monkeypatch.setattr(download_jobs, "fetch_session", lambda sid, uid: profile)
    yield remote
    from services.pool import transfer_pool

    transfer_pool.close(9)   # the pool is module-global; do not leak into the next test
    server.close_all()
    thread.join(timeout=5)


def test_a_folder_is_copied_whole(ftp_profile, download_dir):
    job = _wait(downloads.start_job(USER, 9, "/Amber"))
    assert job.state == "done", job.error
    local = download_dir / "alice" / "Amber"
    assert (local / "a.jpg").read_bytes() == (ftp_profile / "Amber" / "a.jpg").read_bytes()
    assert (local / "2026" / "b.jpg").read_bytes() == b"b" * 10
    assert (local / "empty").is_dir(), "an empty folder is part of the tree too"
    view = job.view()
    assert (view["files_total"], view["files_done"]) == (2, 2)
    assert view["bytes_done"] == view["bytes_total"] == 700 * 1024 + 10
    assert view["folder"] == "Amber"
    assert not list(local.rglob("*.part"))


def test_saving_again_does_not_merge_into_the_first_copy(ftp_profile, download_dir):
    first = _wait(downloads.start_job(USER, 9, "/Amber"))
    second = _wait(downloads.start_job(USER, 9, "/Amber/"))
    assert second.state == "done", second.view()
    assert (first.view()["folder"], second.view()["folder"]) == ("Amber", "Amber (2)")
    assert (download_dir / "alice" / "Amber (2)" / "a.jpg").exists()


# ── Failure paths, against a fake ─────────────────────────────────────────────


class _FakeHandle:
    def __init__(self, data: bytes, delay: float) -> None:
        self._data, self._delay, self._at = data, delay, 0

    def read(self, size):
        time.sleep(self._delay)
        chunk = self._data[self._at:self._at + size]
        self._at += len(chunk)
        return chunk

    def close(self):
        pass


class _FakeSFTP:
    """/src holds ok.txt, slow.bin, and locked/ which cannot be listed."""

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay

    @staticmethod
    def _attr(name, is_dir, size=0):
        attr = paramiko.SFTPAttributes()
        attr.filename, attr.st_size = name, size
        attr.st_mode = (stat_module.S_IFDIR if is_dir else stat_module.S_IFREG) | 0o644
        return attr

    def listdir_attr(self, path):
        if path == "/src":
            return [self._attr("ok.txt", False, 2), self._attr("slow.bin", False, 5 * 256 * 1024),
                    self._attr("locked", True)]
        raise PermissionError(13, "Permission denied", path)

    def open(self, path, mode):
        if path.endswith("ok.txt"):
            return _FakeHandle(b"ok", 0)
        return _FakeHandle(b"x" * (5 * 256 * 1024), self.delay)


@pytest.fixture()
def fake_pool(monkeypatch):
    from services import transfer

    class _Conn:
        def __init__(self, sftp):
            self.sftp, self.lock, self.last_used = sftp, threading.Lock(), 0

    state = {"sftp": _FakeSFTP()}
    monkeypatch.setattr(transfer.transfer_pool, "acquire", lambda *a: _Conn(state["sftp"]))
    monkeypatch.setattr(download_jobs, "fetch_session", lambda sid, uid: {"id": sid})
    return state


def test_an_unreadable_subfolder_costs_only_itself(fake_pool, download_dir):
    job = _wait(downloads.start_job(USER, 1, "/src"))
    view = job.view()
    assert view["state"] == "done"
    assert view["files_done"] == 2
    assert [f["path"] for f in view["failed"]] == ["locked/"]


def test_cancel_leaves_no_partial_file(fake_pool, download_dir):
    fake_pool["sftp"] = _FakeSFTP(delay=0.3)   # ~1.5 s for slow.bin
    job = downloads.start_job(USER, 1, "/src")
    deadline = time.monotonic() + 5
    while job.current != "slow.bin" and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.4)
    job.cancel.set()
    _wait(job)

    local = download_dir / "alice" / "src"
    assert job.state == "cancelled"
    assert not (local / "slow.bin").exists(), "a truncated file under the real name"
    assert not list(local.rglob("*.part"))
    assert [f["path"] for f in job.failed] == ["locked/"], "cancelling is not a failure"
    assert (local / "ok.txt").read_bytes() == b"ok", "what finished before the cancel stays"


def test_an_unreadable_folder_fails_the_job(fake_pool):
    job = _wait(downloads.start_job(USER, 1, "/nope"))
    assert job.state == "failed" and "Permission denied" in job.error


# ── The downloads folder ──────────────────────────────────────────────────────


def test_paths_cannot_leave_the_downloads_folder(download_dir, tmp_path):
    root = downloads.user_root(USER)
    (tmp_path / "secret").write_text("x")
    (root / "escape").symlink_to(tmp_path)
    for bad in ("../../secret", "/../secret", "escape/secret"):
        with pytest.raises(PermissionError):
            downloads.resolve_inside(root, bad)
    assert downloads.resolve_inside(root, "/") == root.resolve()


def test_an_unsafe_username_gets_an_id_folder(download_dir):
    assert downloads.user_root({"user_id": 7, "username": "../evil"}).name == "user-7"
    assert downloads.user_root({"user_id": 7, "username": ".hidden"}).name == "user-7"


def test_service_lists_hides_parts_and_refuses_to_delete_the_root(download_dir):
    root = downloads.user_root(USER)
    (root / "Amber").mkdir()
    (root / "notes.txt").write_text("n")
    (root / "big.bin.part").write_text("in flight")
    service = downloads.DownloadService(USER)

    listing = service.list("")
    assert listing["path"] == "/"
    assert [e["name"] for e in listing["entries"]] == ["Amber", "notes.txt"]

    assert service.delete(["/"])["ok"] is False
    assert service.delete(["/notes.txt", "/Amber"])["removed"] == ["/notes.txt", "/Amber"]
    assert not (root / "Amber").exists()


def test_a_job_is_visible_only_to_its_owner(fake_pool):
    job = _wait(downloads.start_job(USER, 1, "/src"))
    mine = downloads.DownloadService(USER)
    theirs = downloads.DownloadService({"user_id": 2, "username": "bob"})
    assert mine.status(job.id)["ok"] is True
    assert theirs.status(job.id)["ok"] is False
    assert theirs.cancel(job.id)["ok"] is False
