"""
The FTP adapter: listing parsers, and the paramiko-shaped surface end to end.

The live half runs against an in-process pyftpdlib server and is skipped when
pyftpdlib is not installed:

    uv run --with pytest --with pyftpdlib python -m pytest tests/test_ftp.py -q
"""
from __future__ import annotations

import stat as stat_module
import sys
import threading
from calendar import timegm
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "appcode"))

from services.ftp import attrs_from_facts, attrs_from_list_line  # noqa: E402


# ── Parsers ───────────────────────────────────────────────────────────────────


def test_mlsd_directory_with_mixed_case_facts():
    # This NAS (SmbFTPD) advertises UNIX.mode in capitals.
    attr = attrs_from_facts(
        "Photos", {"type": "dir", "modify": "20260101120000", "UNIX.mode": "0755"}
    )
    assert stat_module.S_ISDIR(attr.st_mode)
    assert attr.st_mode & 0o7777 == 0o755
    assert attr.st_mtime == timegm((2026, 1, 1, 12, 0, 0, 0, 0, 0))


def test_mlsd_file_size_and_fractional_time():
    attr = attrs_from_facts("a.txt", {"type": "file", "size": "12", "modify": "20260102030405.123"})
    assert stat_module.S_ISREG(attr.st_mode)
    assert attr.st_size == 12
    assert attr.st_mtime == timegm((2026, 1, 2, 3, 4, 5, 0, 0, 0))


def test_mlsd_skips_self_and_parent():
    assert attrs_from_facts(".", {"type": "cdir"}) is None
    assert attrs_from_facts("..", {"type": "pdir"}) is None


def test_mlsd_symlink():
    attr = attrs_from_facts("link", {"type": "OS.unix=symlink"})
    assert stat_module.S_ISLNK(attr.st_mode)


def test_list_line_file_with_spaces_in_name():
    attr = attrs_from_list_line("-rw-r--r--   1 owner group   2048 Mar  4  2024 my file.txt")
    assert attr.filename == "my file.txt"
    assert attr.st_size == 2048
    assert attr.st_mode & 0o7777 == 0o644
    assert attr.st_mtime == timegm((2024, 3, 4, 0, 0, 0, 0, 0, 0))


def test_list_line_recent_date_takes_this_year():
    now = timegm((2026, 9, 23, 0, 0, 0, 0, 0, 0))
    attr = attrs_from_list_line("drwxr-xr-x 2 o g 4096 Sep 20 08:15 logs", now)
    assert stat_module.S_ISDIR(attr.st_mode)
    assert attr.st_mtime == timegm((2026, 9, 20, 8, 15, 0, 0, 0, 0))


def test_list_line_date_after_today_is_last_year():
    now = timegm((2026, 1, 5, 0, 0, 0, 0, 0, 0))
    attr = attrs_from_list_line("-rw-r--r-- 1 o g 1 Dec 30 23:59 old", now)
    assert attr.st_mtime == timegm((2025, 12, 30, 23, 59, 0, 0, 0, 0))


def test_list_line_symlink_drops_target():
    attr = attrs_from_list_line("lrwxrwxrwx 1 o g 7 Jan  1 00:00 current -> v2.0.0")
    assert attr.filename == "current"
    assert stat_module.S_ISLNK(attr.st_mode)


@pytest.mark.parametrize("line", ["total 12", "", "01-02-24  10:00AM  <DIR>  dos"])
def test_list_line_ignores_what_it_cannot_read(line):
    assert attrs_from_list_line(line) is None


# ── Live, against pyftpdlib ───────────────────────────────────────────────────


@pytest.fixture()
def server(tmp_path):
    pytest.importorskip("pyftpdlib")
    from pyftpdlib.authorizers import DummyAuthorizer
    from pyftpdlib.handlers import FTPHandler
    from pyftpdlib.servers import FTPServer

    root = tmp_path / "root"
    root.mkdir()
    authorizer = DummyAuthorizer()
    authorizer.add_user("alice", "secret", str(root), perm="elradfmwMT")

    class Handler(FTPHandler):
        pass

    Handler.authorizer = authorizer
    ftpd = FTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=ftpd.serve_forever, kwargs={"timeout": 0.1}, daemon=True)
    thread.start()
    yield {"host": "127.0.0.1", "port": ftpd.address[1], "root": root, "handler": Handler}
    ftpd.close_all()
    thread.join(timeout=5)


def _client(server):
    from services import ftp

    return ftp.connect(
        {"host": server["host"], "port": server["port"],
         "username": "alice", "password": "secret"}
    )


def test_bad_login_is_ftp_unavailable(server):
    from services import ftp

    with pytest.raises(ftp.FTPUnavailable):
        ftp.connect({"host": server["host"], "port": server["port"],
                     "username": "alice", "password": "wrong"})


def test_upload_list_download_rename_delete(server):
    from services.sftp_service import _remove_recursive, describe_entry
    from services.transfer import _mkdir_p

    client = _client(server)
    assert client.normalize(".") == "/"

    _mkdir_p(client, "/a/b")
    payload = b"x" * (600 * 1024)  # more than one 256 KiB chunk
    handle = client.open("/a/b/data.bin", "wb")
    handle.write(payload)
    handle.close()

    entries = {attr.filename: attr for attr in client.listdir_attr("/a/b")}
    assert entries["data.bin"].st_size == len(payload)
    row = describe_entry(entries["data.bin"], "/a/b")
    assert row["id"] == "/a/b/data.bin" and not row["is_dir"]

    assert client.stat("/a/b/data.bin").st_size == len(payload)
    assert stat_module.S_ISDIR(client.stat("/a").st_mode)
    with pytest.raises(FileNotFoundError):
        client.stat("/nope")

    handle = client.open("/a/b/data.bin", "rb")
    received = b""
    while chunk := handle.read(256 * 1024):
        received += chunk
    handle.close()
    assert received == payload

    # The connection is usable again after a transfer has finished.
    client.rename("/a/b/data.bin", "/a/b/renamed.bin")
    assert [a.filename for a in client.listdir_attr("/a/b")] == ["renamed.bin"]

    with pytest.raises(IOError):
        client.listdir_attr("/a/b/renamed.bin")  # a file is not a directory

    _remove_recursive(client, "/a")
    assert client.listdir_attr("/") == []
    assert not (server["root"] / "a").exists()


def test_listing_waits_for_an_open_transfer(server):
    """A command during RETR must queue behind it, not interleave with it."""
    (server["root"] / "f.txt").write_bytes(b"hello")
    client = _client(server)

    handle = client.open("/f.txt", "rb")
    listed = threading.Event()
    threading.Thread(
        target=lambda: (client.listdir_attr("/"), listed.set()), daemon=True
    ).start()
    assert not listed.wait(0.5), "listing ran while the transfer held the connection"

    assert handle.read(-1) == b"hello"
    handle.close()
    assert listed.wait(5), "listing never ran after the transfer closed"


def test_reconnects_after_the_server_drops_the_connection(server):
    import time

    # A NAS drops an idle control connection with "421 ... timed out" and
    # hangs up. pyftpdlib's own idle timeout does exactly that.
    server["handler"].timeout = 1
    client = _client(server)
    client.listdir_attr("/")
    time.sleep(2)

    before = client._ftp
    assert client.listdir_attr("/") == []
    assert client._ftp is not before, "listing succeeded without reconnecting?"


def test_pool_dials_ftp_for_an_ftp_profile(server):
    from services.sftp_service import SFTPPool

    pool = SFTPPool()
    profile = {"type": "ftp", "host": server["host"], "port": server["port"],
               "username": "alice", "password": "secret"}
    conn = pool.acquire(1, 9, profile)
    assert conn.sftp.listdir_attr("/") == []
    assert pool.acquire(1, 9, profile) is conn  # reused, not re-dialled
    pool.close(9)
    assert conn.client.closed


# ── FTPS (explicit AUTH TLS), pinned on first use ─────────────────────────────


def _self_signed(directory: Path) -> Path:
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "nas.local")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    path = directory / "cert.pem"
    path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                          serialization.NoEncryption())
        + cert.public_bytes(serialization.Encoding.PEM)
    )
    return path


@pytest.fixture()
def tls_server(tmp_path):
    pytest.importorskip("pyftpdlib")
    pytest.importorskip("OpenSSL")  # pyftpdlib's TLS handler is built on pyOpenSSL
    from pyftpdlib.authorizers import DummyAuthorizer
    from pyftpdlib.handlers import TLS_FTPHandler
    from pyftpdlib.servers import FTPServer

    root = tmp_path / "root"
    root.mkdir()
    authorizer = DummyAuthorizer()
    authorizer.add_user("alice", "secret", str(root), perm="elradfmwMT")

    class Handler(TLS_FTPHandler):
        certfile = str(_self_signed(tmp_path))
        # What the NAS does: "504 TLS/SSL protection required" on a plain login.
        tls_control_required = True
        tls_data_required = True

    Handler.authorizer = authorizer
    ftpd = FTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=ftpd.serve_forever, kwargs={"timeout": 0.1}, daemon=True)
    thread.start()
    yield {"host": "127.0.0.1", "port": ftpd.address[1], "root": root}
    ftpd.close_all()
    thread.join(timeout=5)


def _profile(server, pin=""):
    return {"host": server["host"], "port": server["port"],
            "username": "alice", "password": "secret", "host_key": pin}


def test_tls_is_used_and_the_certificate_pinned(tls_server):
    from services import ftp

    learned = []
    client = ftp.connect(_profile(tls_server), on_learn_host_key=learned.append)
    assert client.secure
    assert len(learned) == 1 and learned[0].startswith("tls-sha256 ")

    # Transfers ride the encrypted data channel.
    handle = client.open("/x.bin", "wb")
    handle.write(b"secret bytes")
    handle.close()
    assert (tls_server["root"] / "x.bin").read_bytes() == b"secret bytes"
    handle = client.open("/x.bin", "rb")
    assert handle.read(-1) == b"secret bytes"
    handle.close()

    # Reconnecting with the learned pin succeeds and learns nothing new.
    again = []
    ftp.connect(_profile(tls_server, learned[0]), on_learn_host_key=again.append)
    assert again == []


def test_a_different_certificate_is_refused(tls_server):
    from services import ftp
    from services.ssh import HostKeyChanged

    with pytest.raises(HostKeyChanged):
        ftp.connect(_profile(tls_server, "tls-sha256 " + "0" * 64))


def test_a_pinned_server_that_stops_offering_tls_is_refused(server):
    """A downgrade: plain FTP where a certificate was pinned."""
    from services import ftp
    from services.ssh import HostKeyChanged

    with pytest.raises(HostKeyChanged):
        ftp.connect({**_profile(server, "tls-sha256 " + "0" * 64)})


def test_plain_server_still_works_unpinned(server):
    from services import ftp

    client = ftp.connect(_profile(server))
    assert not client.secure
    assert client.listdir_attr("/") == []
