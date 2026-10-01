"""
What IguanaXterm is and where it lives: the version and the project's links.
**Both sides**: the About dialog shows them in the browser, and the server's
release check (``about_service``) compares against ``VERSION``.

``VERSION`` must match ``pyproject.toml`` — ``tests/test_about.py`` fails a
release bump that changes one and not the other.
"""
from __future__ import annotations

import re

__all__ = ["VERSION", "REPO", "REPO_URL", "WIKI_URL", "RELEASES_URL", "LICENSE",
           "parse_version", "is_newer"]

VERSION = "2.1.0"

REPO = "El-Iguana/iguanaxterm_wapyt"
REPO_URL = f"https://github.com/{REPO}"
WIKI_URL = f"{REPO_URL}/wiki"
RELEASES_URL = f"{REPO_URL}/releases"
LICENSE = "MIT"

_VERSION = re.compile(r"^v?(\d+)\.(\d+)(?:\.(\d+))?$")


def parse_version(text: str) -> tuple[int, int, int] | None:
    """``"v2.1.0"`` → ``(2, 1, 0)``; ``None`` for anything else (pre-releases too)."""
    match = _VERSION.match(str(text or "").strip())
    if not match:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch or 0)


def is_newer(latest: str, current: str = VERSION) -> bool:
    """Whether ``latest`` is a later release than ``current``."""
    a, b = parse_version(latest), parse_version(current)
    return bool(a and b and a > b)
