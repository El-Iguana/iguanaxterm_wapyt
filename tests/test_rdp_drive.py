"""The RDP shared drive (#76): folders, the file browser's view, caps, cleanup."""
from __future__ import annotations

import json
import os

import pytest

from _world import svc, world  # noqa: F401  (fixture)


@pytest.fixture()
def drives(tmp_path, monkeypatch):
    root = tmp_path / "drives"
    root.mkdir()
    monkeypatch.setenv("GANXTERM_DRIVE_DIR", str(root))
    monkeypatch.setenv("GANXTERM_GUACD", "guacd:4822")
    return root


def test_drives_need_a_folder_and_guacd(tmp_path, monkeypatch):
    from services import drive

    monkeypatch.delenv("GANXTERM_DRIVE_DIR", raising=False)
    monkeypatch.setenv("GANXTERM_GUACD", "guacd:4822")
    assert not drive.enabled()
    monkeypatch.setenv("GANXTERM_DRIVE_DIR", str(tmp_path))
    assert drive.enabled()
    monkeypatch.delenv("GANXTERM_GUACD")
    assert not drive.enabled()


def test_the_folder_is_built_from_ids_only(drives, monkeypatch):
    from services import drive

    path = drive.folder(7, 42, create=True)
    assert path == os.path.join(os.path.realpath(drives), "7", "42") and os.path.isdir(path)
    for bad in (("0", 1), (1, -3), ("../1", 2), (1, "2/../../x")):
        with pytest.raises(ValueError):
            drive.folder(*bad)
    # guacd's view: the same layout under its own root.
    assert drive.guacd_folder(7, 42) == f"{os.path.realpath(drives)}/7/42"
    monkeypatch.setenv("GANXTERM_GUACD_DRIVE_DIR", "/drives")
    assert drive.guacd_folder(7, 42) == "/drives/7/42"


def test_a_planted_link_cant_move_the_folder(drives, tmp_path):
    from services import drive

    (drives / "7").mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    os.symlink(outside, drives / "7" / "42")
    with pytest.raises(ValueError):
        drive.folder(7, 42)


def test_the_file_browser_stays_inside_the_drive(drives, tmp_path):
    from services.drive import DriveFiles

    base = drives / "1" / "2"
    base.mkdir(parents=True)
    (tmp_path / "secret.txt").write_text("no")
    os.symlink(tmp_path / "secret.txt", base / "link.txt")
    files = DriveFiles(str(base))
    assert files.normalize(".") == "/" and files.normalize("/a/../b") == "/b"
    # ".." stops at the drive's top, so these look for /secret.txt inside it.
    for escape in ("../../secret.txt", "/../../../secret.txt"):
        assert files.normalize(escape) == "/secret.txt"
        with pytest.raises(FileNotFoundError):
            files.open(escape, "rb")
    with pytest.raises(PermissionError):
        files.open("link.txt", "rb")                # a link out is refused
    # Links aren't listed: they could point anywhere.
    assert [a.filename for a in files.listdir_attr("/")] == []
    with pytest.raises(PermissionError):
        files.rmdir("/")


def test_the_file_browser_round_trip(drives):
    from services.drive import DriveFiles

    base = drives / "1" / "2"
    base.mkdir(parents=True)
    files = DriveFiles(str(base))
    files.mkdir("/docs")
    handle = files.open("/docs/a.txt", "wb")
    handle.set_pipelined(True)
    handle.write(b"hello drive")
    handle.close()
    assert (base / "docs" / "a.txt").read_bytes() == b"hello drive"
    listed = {a.filename: a for a in files.listdir_attr("/docs")}
    assert listed["a.txt"].st_size == 11
    assert files.stat("/docs").st_mode & 0o040000
    reader = files.open("/docs/a.txt", "rb")
    reader.seek(6)
    assert reader.read() == b"drive"
    reader.close()
    files.rename("/docs/a.txt", "/docs/b.txt")
    files.remove("/docs/b.txt")
    files.rmdir("/docs")
    assert files.get_transport().is_active()
    files.close()
    assert not files.get_transport().is_active()


def test_the_cap_counts_the_whole_drive(drives, monkeypatch):
    from services.drive import DriveFiles, DriveFull

    monkeypatch.setenv("GANXTERM_DRIVE_MAX_BYTES", "100")
    base = drives / "1" / "2"
    base.mkdir(parents=True)
    (base / "already.bin").write_bytes(b"x" * 60)
    files = DriveFiles(str(base))
    handle = files.open("/new.bin", "wb")
    handle.write(b"y" * 40)                         # exactly up to the cap
    with pytest.raises(DriveFull):
        handle.write(b"z")
    handle.close()
    # Replacing a file counts its old size as free.
    handle = files.open("/already.bin", "wb")
    handle.write(b"w" * 60)
    handle.close()
    (base / "more.bin").write_bytes(b"m" * 10)       # Windows can write past it
    with pytest.raises(DriveFull):
        files.open("/another.bin", "wb")


def test_one_way_drives(drives):
    from services.drive import DriveFiles

    base = drives / "1" / "2"
    base.mkdir(parents=True)
    (base / "report.pdf").write_bytes(b"%PDF")
    no_upload = DriveFiles(str(base), can_upload=False)
    assert [a.filename for a in no_upload.listdir_attr("/")] == ["report.pdf"]
    assert no_upload.open("/report.pdf", "rb").read() == b"%PDF"
    for change in (lambda f: f.open("/x", "wb"), lambda f: f.mkdir("/d"),
                   lambda f: f.remove("/report.pdf"),
                   lambda f: f.rename("/report.pdf", "/r.pdf")):
        with pytest.raises(PermissionError):
            change(no_upload)
    no_download = DriveFiles(str(base), can_download=False)
    with pytest.raises(PermissionError):
        no_download.open("/report.pdf", "rb")
    no_download.open("/in.txt", "wb").close()


def _rdp_site(pam, **options) -> int:
    result = pam.save(name="win", host="10.0.0.5", port=3389, session_type="rdp",
                      options={"security": "rdp", **options})
    assert result["ok"], result
    return result["id"]


def test_guacd_gets_the_connecting_persons_folder(world, drives):
    from services.access import resolve_site
    from services.rdp_ws import _drive_params, guacd_params
    from services.session_service import SessionService

    _db, ids = world
    pam = svc(SessionService, ids["pam"], "power")
    off = _rdp_site(pam)
    assert _drive_params(resolve_site(off, ids["pam"])[0], ids["pam"]) == {}
    on = _rdp_site(pam, drive=True)
    params = _drive_params(resolve_site(on, ids["pam"])[0], ids["pam"])
    assert params["enable-drive"] == "true" and params["drive-name"] == "IguanaXterm"
    assert params["drive-path"].endswith(f"/{ids['pam']}/{on}")
    assert os.path.isdir(params["drive-path"])
    # Guacamole's own file channel is always shut.
    plain = guacd_params({"username": "u"}, "h", 3389)
    assert plain["disable-download"] == "true" and plain["disable-upload"] == "true"


def test_rdp_sites_with_a_drive_have_files(world, drives, monkeypatch):
    from services.session_service import SessionService

    _db, ids = world
    pam = svc(SessionService, ids["pam"], "power")
    plain = _rdp_site(pam)
    with_drive = _rdp_site(pam, drive=True, drive_upload=False)
    rows = {row["id"]: row for row in pam.list()}
    assert rows[plain]["files"] is False and rows[with_drive]["files"] is True
    assert pam.get(with_drive)["options"]["drive_upload"] is False
    assert pam.features() == {"rdp": True, "rdp_drive": True}
    monkeypatch.delenv("GANXTERM_DRIVE_DIR")
    assert {row["id"]: row for row in pam.list()}[with_drive]["files"] is False
    assert pam.features()["rdp_drive"] is False


def test_the_pool_opens_the_drive_for_rdp(world, drives):
    from services.access import resolve_site
    from services.drive import DriveFiles
    from services.pool import _dial
    from services.session_service import SessionService

    _db, ids = world
    pam = svc(SessionService, ids["pam"], "power")
    site = _rdp_site(pam, drive=True, drive_download=False)
    client = _dial(resolve_site(site, ids["pam"])[0], site, ids["pam"])
    assert isinstance(client, DriveFiles) and client.can_download is False
    assert client.base.endswith(os.sep + os.path.join(str(ids["pam"]), str(site)))
    no_drive = _rdp_site(pam)
    with pytest.raises(PermissionError):
        _dial(resolve_site(no_drive, ids["pam"])[0], no_drive, ids["pam"])


def test_deleting_a_site_or_a_person_removes_their_folders(world, drives):
    from services import drive
    from services.session_service import SessionService
    from services.user_service import UserService

    _db, ids = world
    pam = svc(SessionService, ids["pam"], "power")
    site = _rdp_site(pam, drive=True)
    mine = drive.folder(ids["pam"], site, create=True)
    theirs = drive.folder(ids["ula"], site, create=True)       # a person it's shared with
    other = drive.folder(ids["pam"], site + 1000, create=True)
    open(os.path.join(mine, "f.txt"), "w").close()
    assert pam.delete(site)["ok"]
    assert not os.path.exists(mine) and not os.path.exists(theirs) and os.path.isdir(other)

    kept = _rdp_site(pam, drive=True)
    folder = drive.folder(ids["pam"], kept, create=True)
    admin = svc(UserService, ids["admin"], "admin")
    assert admin.delete(ids["pam"])["ok"], "admin deletes pam"
    assert not os.path.exists(folder)
    assert not os.path.exists(os.path.join(drives, str(ids["pam"])))


def test_options_round_trip_json(world, drives):
    from services.rdp_ws import rdp_options

    assert rdp_options({"options": "{}"})["drive"] is False
    got = rdp_options({"options": json.dumps({"drive": True, "drive_download": False})})
    assert got["drive"] and got["drive_upload"] and not got["drive_download"]
