"""
Range requests for resumable downloads: the parsing rules, and FTP starting
part-way through a file (REST) the way a resumed download needs it to.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "appcode"))

from services import http_ranges as hr  # noqa: E402

SIZE = 1000


@pytest.mark.parametrize(
    "header, expected",
    [
        (None, None),
        ("", None),
        ("bytes=0-", (0, 999)),
        ("bytes=400-", (400, 999)),       # the resume case
        ("bytes=400-499", (400, 499)),
        ("bytes=400-5000", (400, 999)),   # an end past EOF is clamped
        ("bytes=-100", (900, 999)),       # suffix
        ("bytes=-5000", (0, 999)),
        ("BYTES = 10 - 20", (10, 20)),    # case and spacing tolerated
        ("items=0-10", None),             # another unit: whole file
        ("bytes=0-10,20-30", None),       # multiple ranges: whole file
        ("bytes=abc-", None),
        ("bytes=20-10", None),            # backwards
        ("bytes=-0", None),
        ("bytes=5", None),
    ],
)
def test_parse_range(header, expected):
    assert hr.parse_range(header, SIZE) == expected


@pytest.mark.parametrize("header", ["bytes=1000-", "bytes=5000-6000"])
def test_a_range_past_the_end_is_not_satisfiable(header):
    with pytest.raises(hr.RangeNotSatisfiable):
        hr.parse_range(header, SIZE)


def test_nothing_is_satisfiable_in_an_empty_file():
    with pytest.raises(hr.RangeNotSatisfiable):
        hr.parse_range("bytes=0-", 0)
    with pytest.raises(hr.RangeNotSatisfiable):
        hr.parse_range("bytes=-10", 0)


def test_etag_changes_with_size_or_mtime():
    base = hr.make_etag(1000, 1700000000)
    assert base.startswith('"') and base.endswith('"')
    assert hr.make_etag(1001, 1700000000) != base
    assert hr.make_etag(1000, 1700000001) != base


def test_if_range_with_an_etag_needs_an_exact_strong_match():
    etag = hr.make_etag(SIZE, 1700000000)
    assert hr.if_range_allows(None, etag, 1700000000)
    assert hr.if_range_allows(etag, etag, 1700000000)
    assert not hr.if_range_allows('"other"', etag, 1700000000)
    assert not hr.if_range_allows("W/" + etag, etag, 1700000000), "a weak tag never matches"


def test_if_range_with_a_date_needs_the_file_unchanged_since():
    mtime = 1700000000
    stamp = hr.last_modified(mtime)
    assert hr.if_range_allows(stamp, hr.make_etag(SIZE, mtime), mtime)
    assert not hr.if_range_allows(stamp, hr.make_etag(SIZE, mtime + 60), mtime + 60)
    assert not hr.if_range_allows(stamp, hr.make_etag(SIZE, 0), None), "no mtime, no trust"
    assert not hr.if_range_allows("not a date", hr.make_etag(SIZE, mtime), mtime)


# ── FTP from an offset ────────────────────────────────────────────────────────


@pytest.fixture()
def ftp_client(tmp_path):
    pytest.importorskip("pyftpdlib")
    from pyftpdlib.authorizers import DummyAuthorizer
    from pyftpdlib.handlers import FTPHandler
    from pyftpdlib.servers import FTPServer

    (tmp_path / "blob.bin").write_bytes(bytes(range(256)) * 4096)   # 1 MiB
    authorizer = DummyAuthorizer()
    authorizer.add_user("u", "p", str(tmp_path), perm="elradfmwMT")

    class Handler(FTPHandler):
        pass

    Handler.authorizer = authorizer
    server = FTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"timeout": 0.1}, daemon=True)
    thread.start()
    from services import ftp

    yield ftp.connect({"host": "127.0.0.1", "port": server.address[1],
                       "username": "u", "password": "p"}), tmp_path
    server.close_all()
    thread.join(timeout=5)


def test_ftp_download_starts_at_the_offset(ftp_client):
    from services.transfer import open_at

    client, root = ftp_client
    whole = (root / "blob.bin").read_bytes()
    handle = open_at(client, "/blob.bin", 700_001)
    got = handle.read(-1)
    handle.close()
    assert got == whole[700_001:]


def test_ftp_connection_survives_a_range_cut_short(ftp_client):
    """A range ends before the file does; closing early must not wedge the
    connection for the next request."""
    from services.transfer import open_at

    client, root = ftp_client
    handle = open_at(client, "/blob.bin", 100)
    assert handle.read(10) == (root / "blob.bin").read_bytes()[100:110]
    handle.close()
    assert [a.filename for a in client.listdir_attr("/")] == ["blob.bin"]
