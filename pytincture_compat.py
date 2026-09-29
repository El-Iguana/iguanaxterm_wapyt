"""
Workarounds for pytincture on platforms it was not run on. **Server only.**

``apply()`` is called by ``service.py`` before the app is built, and by the
tests. Each patch is a no-op where the platform does not need it, and each
should go once pytincture fixes the cause upstream.

Windows (ROADMAP phase 38): ``safe_paths._open_relative_nofollow`` walks a
path through directory descriptors, starting with ``os.open(root_dir)``.
Windows cannot open a directory that way and raises ``PermissionError``,
before the function's own fallback (for platforms without ``dir_fd``) can
apply, so every contained read — BFF module sources, assets — failed. The
patch takes that fallback up front: resolve the path with pytincture's own
no-symlink, containment-checked ``resolve_contained_path``, then open it.
``open_contained_file`` still compares the opened file's identity with the
resolved path's afterwards, as before.
"""
from __future__ import annotations

import os

_applied = False


def _needs_path_fallback() -> bool:
    return os.open not in os.supports_dir_fd


def apply(force: bool = False) -> None:
    """Patch what this platform needs. ``force`` applies it anyway (tests)."""
    global _applied
    if _applied or not (force or _needs_path_fallback()):
        return
    from pytincture.backend import safe_paths

    def open_by_resolved_path(root: str, relative_path: str) -> int:
        normalized = safe_paths.normalize_relative_path(relative_path)
        path = safe_paths.resolve_contained_path(root, normalized)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        return os.open(path, flags)

    safe_paths._open_relative_nofollow = open_by_resolved_path
    _applied = True
