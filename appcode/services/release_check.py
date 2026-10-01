"""
Is there a newer IguanaXterm release? **Server side**: the browser cannot reach
GitHub itself (pytincture's CSP only allows ``connect-src 'self'``). A plain
module, not a BFF, so the cache survives pytincture re-executing BFF modules
on every call; ``about_service.AboutService`` asks it.

One unauthenticated request to GitHub's "latest release" API, shared by every
user of this server and remembered for ``CHECK_TTL`` (a failure for
``FAILURE_TTL``), so the 60-requests-an-hour limit is never near. Nothing is
sent but the request itself and a ``IguanaXterm/<version>`` User-Agent.

``GANXTERM_UPDATE_CHECK=off`` (or ``0``/``false``/``no``) turns it off, for
machines that must not call out.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request

from services.about import REPO, RELEASES_URL, VERSION, is_newer, parse_version

LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
CHECK_TTL = 6 * 3600
FAILURE_TTL = 30 * 60
TIMEOUT = 5

_cache: dict = {}
_lock = threading.Lock()


def checks_enabled() -> bool:
    return os.environ.get("GANXTERM_UPDATE_CHECK", "on").strip().lower() not in (
        "off", "0", "false", "no")


def _fetch() -> dict:
    """GitHub's latest release, as JSON. Raises on any failure."""
    request = urllib.request.Request(LATEST_URL, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"IguanaXterm/{VERSION}",
    })
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310 - fixed https URL
        return json.loads(response.read(256 * 1024).decode("utf-8"))


def latest_release(now: float | None = None) -> dict:
    """The check's result, from the cache while it is fresh."""
    if not checks_enabled():
        return {"current": VERSION, "checked": False, "reason": "off"}
    now = time.time() if now is None else now
    with _lock:
        if _cache and now < _cache["until"]:
            return dict(_cache["result"])
    try:
        body = _fetch()
        tag = str(body.get("tag_name") or "")
        if parse_version(tag) is None:
            raise ValueError(f"unexpected release tag {tag!r}")
        latest = tag.lstrip("v")
        result = {
            "current": VERSION, "checked": True, "latest": latest,
            "newer": is_newer(latest), "url": str(body.get("html_url") or RELEASES_URL),
            "published": str(body.get("published_at") or "")[:10],
        }
        ttl = CHECK_TTL
    except Exception as exc:  # noqa: BLE001 - offline is normal; say so, don't fail
        result = {"current": VERSION, "checked": False, "reason": "unreachable",
                  "error": f"{type(exc).__name__}: {exc}"[:200]}
        ttl = FAILURE_TTL
    with _lock:
        _cache.update(until=now + ttl, result=result)
    return dict(result)
