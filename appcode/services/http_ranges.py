"""
HTTP range requests for resumable downloads (RFC 9110, sections 13-14).

Pure functions, so the edge cases can be tested without a server. The
download route uses them to answer ``Range: bytes=N-`` with 206 Partial
Content, which is what lets an interrupted download pick up where it
stopped -- the page's own retry, and the browser's download manager for the
fallback path, both rely on it.

A resume must never splice two versions of a file together. So every
response carries a validator (an ``ETag`` built from size and mtime, plus
``Last-Modified``), and a resume sends it back in ``If-Range``: if the file
changed in between, the range is ignored and the whole new file is sent.
"""
from __future__ import annotations

from email.utils import formatdate, parsedate_to_datetime
from typing import Optional


class RangeNotSatisfiable(Exception):
    """416: the range starts at or past the end of the file."""


def make_etag(size: int, mtime: Optional[float]) -> str:
    """
    A strong validator from what both SFTP and FTP can tell us.

    Size and mtime to the second: a file rewritten within the same second to
    the same length would slip past it, which is the limit of what the remote
    exposes. That is the same trade every static file server makes.
    """
    return f'"{int(size):x}-{int(mtime or 0):x}"'


def last_modified(mtime: Optional[float]) -> Optional[str]:
    return formatdate(float(mtime), usegmt=True) if mtime else None


def if_range_allows(if_range: Optional[str], etag: str, mtime: Optional[float]) -> bool:
    """
    Whether a ``Range`` may be honoured given the request's ``If-Range``.

    No ``If-Range``: yes. An entity tag: only an exact strong match (a weak
    ``W/`` tag never matches for ranges). A date: only when the file has not
    changed since, and we know its mtime at all.
    """
    if not if_range:
        return True
    value = if_range.strip()
    if value.startswith(('"', "W/")):
        return value == etag
    if not mtime:
        return False
    try:
        since = parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError):
        return False
    return int(mtime) <= int(since)


def parse_range(header: Optional[str], size: int) -> Optional[tuple[int, int]]:
    """
    The single byte range requested, as inclusive ``(start, end)``.

    ``None`` means "send the whole file": no header, a unit other than bytes,
    several ranges (legal, but nobody resuming a download asks for them, and
    multipart/byteranges is not worth its complexity here), or anything
    malformed -- a server may always ignore Range. ``RangeNotSatisfiable``
    for a well-formed range that starts at or beyond the end.
    """
    if not header:
        return None
    unit, _, spec = header.strip().partition("=")
    if unit.strip().lower() != "bytes" or "," in spec:
        return None
    first, dash, last = spec.strip().partition("-")
    if not dash:
        return None
    first, last = first.strip(), last.strip()
    try:
        if first == "":                       # suffix: the last N bytes
            length = int(last)
            if length <= 0:
                return None
            if size == 0:
                raise RangeNotSatisfiable
            return max(0, size - length), size - 1
        start = int(first)
        end = int(last) if last else size - 1
    except ValueError:
        return None
    if start < 0 or (last and end < start):
        return None
    if start >= size:
        raise RangeNotSatisfiable
    return start, min(end, size - 1)
