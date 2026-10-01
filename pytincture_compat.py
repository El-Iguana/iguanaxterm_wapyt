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

Every platform (pytincture 1.0.0rc10 and rc11): the page template turns on
pytincture's service worker (``enableServiceWorker: true``, hard-coded in
``frontend/index.html``). The worker is registered with the scope
``/<app>/``, but the app's page is ``/<app>``, outside it, so the worker never
controls the page, and the loader waits out its 5-second timeout for that
control on every load (``waitForServiceWorkerControl``). A worker that never
controls the page caches nothing either. The patch serves the page with the
worker off: loads take ~2.4 s instead of ~7.5 s, and the loader unregisters a
worker an earlier load left behind. Drop it once the template or the scope
is fixed upstream. **It is applied to the app**, by ``apply_to_app`` after
``create_app()``: that loads its own private copy of
``pytincture.backend.app``, so patching the imported module reaches nothing.
"""
from __future__ import annotations

import io
import os

_applied = False

SERVICE_WORKER_ON = "enableServiceWorker: true"
SERVICE_WORKER_OFF = "enableServiceWorker: false"


def _needs_path_fallback() -> bool:
    return os.open not in os.supports_dir_fd


def apply(force: bool = False) -> None:
    """
    Patch what this platform needs, before the app is built. ``force``
    applies it anyway (tests).
    """
    _apply_path_fallback(force)


def apply_to_app(application) -> None:
    """Patch the backend ``create_app()`` built for this app (see above)."""
    turn_off_service_worker(application.state.pytincture_backend)


def turn_off_service_worker(app_module) -> None:
    """Serve the page of this backend module with the service worker off."""
    if getattr(app_module, "_compat_service_worker_off", False):
        return
    index_path = os.path.join(app_module.STATIC_PATH, "index.html")

    def open_page(file, *args, **kwargs):
        # The page handler reads the template with a bare open(); this module
        # global shadows the builtin for app.py alone.
        if isinstance(file, str) and os.path.normpath(file) == os.path.normpath(index_path) \
                and "b" not in (args[0] if args else kwargs.get("mode", "r")):
            # UTF-8 explicitly: the template is, and Windows' default (cp1252)
            # fails on it unless Python runs in UTF-8 mode.
            with open(file, encoding="utf-8") as handle:
                return io.StringIO(handle.read().replace(SERVICE_WORKER_ON, SERVICE_WORKER_OFF))
        return open(file, *args, **kwargs)

    app_module.open = open_page
    app_module._compat_service_worker_off = True


def _apply_path_fallback(force: bool) -> None:
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
