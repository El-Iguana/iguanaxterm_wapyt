"""
The streaming multipart parser behind the upload route.

The route parses the body itself so it can authenticate before reading a
byte and never hold the file. What matters is that the fields and the file
come back intact however the network happens to split the body.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "appcode"))

from fastapi import HTTPException  # noqa: E402
from python_multipart.multipart import MultipartParser  # noqa: E402

from services.transfer import _MAX_FIELD_BYTES, _UploadParts  # noqa: E402

BOUNDARY = "----wapytBoundary7MA4YWxkTrZu0gW"


def _body(fields: dict, filename: str, payload: bytes) -> bytes:
    """What the browser's FormData sends: fields first, then the file."""
    out = b""
    for name, value in fields.items():
        out += (
            f"--{BOUNDARY}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        ).encode()
    out += (
        f"--{BOUNDARY}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8")
    return out + payload + f"\r\n--{BOUNDARY}--\r\n".encode()


def _feed(body: bytes, chunk: int) -> tuple[_UploadParts, bytes, int]:
    """Parse ``body`` in ``chunk``-sized reads, draining as the route does."""
    parts = _UploadParts()
    parser = MultipartParser(BOUNDARY, parts.callbacks())
    received = b""
    most_pending = 0
    for start in range(0, len(body), chunk):
        parser.write(body[start:start + chunk])
        most_pending = max(most_pending, sum(len(p) for p in parts.pending))
        received += b"".join(parts.pending)
        parts.pending.clear()
    parser.finalize()
    return parts, received, most_pending


@pytest.mark.parametrize("chunk", [1, 7, 4096, 65536, 10**9])
def test_fields_and_file_survive_any_split(chunk):
    payload = os.urandom(300 * 1024) if chunk > 1 else os.urandom(3000)
    body = _body({"path": "/media/Photos", "relative_path": "Amber/IMG_0001.jpg"},
                 "IMG_0001.jpg", payload)
    parts, received, _ = _feed(body, chunk)

    assert parts.fields == {"path": "/media/Photos", "relative_path": "Amber/IMG_0001.jpg"}
    assert parts.filename == "IMG_0001.jpg"
    assert parts.file_started and parts.file_ended
    assert received == payload


def test_memory_is_bounded_by_the_read_not_the_file():
    payload = os.urandom(8 * 1024 * 1024)
    _, received, most_pending = _feed(_body({"path": "/"}, "big.bin", payload), 65536)
    assert received == payload
    # One read, plus what the parser held back from the previous one because it
    # might have been the start of the boundary. Bounded either way; what
    # matters is that it does not grow with the file.
    assert most_pending <= 65536 + len(BOUNDARY) + 8


def test_payload_that_looks_like_a_boundary_prefix_is_kept():
    payload = b"\r\n--" + BOUNDARY[:-3].encode() + b"\r\n--not-it"
    _, received, _ = _feed(_body({"path": "/"}, "tricky.bin", payload), 5)
    assert received == payload


def test_non_ascii_filename():
    parts, _, _ = _feed(_body({"path": "/"}, "Ámbér 🐱.jpg", b"x"), 64)
    assert parts.filename == "Ámbér 🐱.jpg"


def test_an_oversized_field_is_refused():
    body = _body({"path": "a" * (_MAX_FIELD_BYTES + 1)}, "f", b"x")
    with pytest.raises(HTTPException) as caught:
        _feed(body, 4096)
    assert caught.value.status_code == 400
