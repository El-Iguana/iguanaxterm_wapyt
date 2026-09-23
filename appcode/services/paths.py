"""
Path and formatting helpers shared by the browser UI and the server.

This module is imported on both sides. In the original app these were
reimplementations in JavaScript (``joinPath``, ``parentPath``, ``fmtSize``,
``fmtDate``, ``fileIcon``); here they are ordinary Python running under Pyodide
in the browser and CPython on the server, so there is one implementation, one
set of edge cases, and they are unit-testable without a browser.

Keep this module dependency-free: it must import cleanly under Pyodide, which
means standard library only.
"""
from __future__ import annotations

import posixpath
from datetime import datetime, timezone
from typing import Iterable

# ── Remote paths ──────────────────────────────────────────────────────────────
# Remote hosts are POSIX, so posixpath is correct regardless of the OS running
# this code. os.path would use backslashes when the server runs on Windows.


def join_path(base: str, name: str) -> str:
    """Join a remote directory and a child name."""
    return posixpath.join(base or "/", name)


def parent_path(path: str) -> str:
    """The parent of a remote path; ``/`` is its own parent."""
    normalized = (path or "/").rstrip("/")
    if not normalized:
        return "/"
    parent = posixpath.dirname(normalized)
    return parent or "/"


def breadcrumbs(path: str) -> list[dict]:
    """
    Split a remote path into ``[{"label", "path"}]`` crumbs, root first.

    >>> [c["label"] for c in breadcrumbs("/var/log/nginx")]
    ['/', 'var', 'log', 'nginx']
    """
    crumbs = [{"label": "/", "path": "/"}]
    current = ""
    for part in (path or "/").strip("/").split("/"):
        if not part:
            continue
        current = f"{current}/{part}"
        crumbs.append({"label": part, "path": current})
    return crumbs


def is_safe_name(name: str) -> bool:
    """Reject names that would escape their directory or confuse the remote."""
    if not name or name in (".", ".."):
        return False
    return "/" not in name and "\0" not in name


# ── Display formatting ────────────────────────────────────────────────────────


def format_size(num_bytes: int | None) -> str:
    """Human-readable byte count. Sort on the raw value, not this string."""
    size = float(num_bytes or 0)
    if size < 1024:
        return f"{int(size)} B"
    for unit in ("KB", "MB", "GB", "TB"):
        size /= 1024.0
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
    return f"{size:.1f} TB"


def format_mtime(epoch: int | None) -> str:
    """Local-time ``YYYY-MM-DD HH:MM`` for an epoch, or ``""`` for falsy input."""
    if not epoch:
        return ""
    try:
        moment = datetime.fromtimestamp(int(epoch), tz=timezone.utc).astimezone()
    except (OverflowError, OSError, ValueError):
        return ""
    return moment.strftime("%Y-%m-%d %H:%M")


def format_mode(mode: int | None) -> str:
    """Octal permission bits, e.g. ``0755``."""
    if not mode:
        return ""
    return format(mode & 0o7777, "04o")


# ── Icons ─────────────────────────────────────────────────────────────────────
# MDI class names only. wapyt renders these as a class, never as text, so an
# unmapped value degrades to blank space rather than typesetting its own name.

_EXTENSION_ICONS = {
    "mdi-language-python": {"py", "pyi", "pyw"},
    "mdi-language-javascript": {"js", "mjs", "cjs", "jsx"},
    "mdi-language-typescript": {"ts", "tsx"},
    "mdi-language-html5": {"html", "htm"},
    "mdi-language-css3": {"css", "scss", "sass", "less"},
    "mdi-code-json": {"json", "jsonl"},
    "mdi-console": {"sh", "bash", "zsh", "fish", "ps1"},
    "mdi-cog": {"conf", "cfg", "ini", "toml", "yaml", "yml", "env"},
    "mdi-file-document-outline": {"txt", "md", "rst", "log", "csv"},
    "mdi-file-image": {"png", "jpg", "jpeg", "gif", "svg", "webp", "bmp", "ico"},
    "mdi-zip-box": {"zip", "gz", "tgz", "bz2", "xz", "tar", "7z", "rar", "zst"},
    "mdi-file-pdf-box": {"pdf"},
    "mdi-database": {"db", "sqlite", "sqlite3", "sql"},
    "mdi-key": {"pem", "key", "crt", "cer", "pub"},
    "mdi-music": {"mp3", "wav", "flac", "ogg", "m4a"},
    "mdi-movie": {"mp4", "mkv", "avi", "mov", "webm"},
}

_ICON_BY_EXTENSION = {
    extension: icon
    for icon, extensions in _EXTENSION_ICONS.items()
    for extension in extensions
}


def file_icon(name: str, is_dir: bool = False, is_link: bool = False) -> str:
    """MDI class for a remote entry."""
    if is_dir:
        return "mdi-folder"
    if is_link:
        return "mdi-link-variant"
    extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return _ICON_BY_EXTENSION.get(extension, "mdi-file-outline")


def session_icon(session_type: str) -> str:
    """MDI class for a saved connection profile."""
    return "mdi-console-network" if session_type == "telnet" else "mdi-server"


# ── Client-side filtering ─────────────────────────────────────────────────────
# Running in the browser, so a large listing can be filtered without a round
# trip. This is the payoff of having real Python client-side: fnmatch and the
# rest of the stdlib instead of a JavaScript reimplementation.


def glob_filter(entries: Iterable[dict], pattern: str, key: str = "name") -> list[dict]:
    """
    Entries whose ``key`` matches a shell glob (``*.log``), case-insensitively.

    An empty pattern returns everything, so this is safe to call on every
    keystroke.
    """
    from fnmatch import fnmatch

    needle = (pattern or "").strip()
    if not needle:
        return list(entries)
    if not any(char in needle for char in "*?["):
        needle = f"*{needle}*"
    lowered = needle.lower()
    return [
        entry for entry in entries if fnmatch(str(entry.get(key, "")).lower(), lowered)
    ]
