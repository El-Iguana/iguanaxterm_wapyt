"""pytincture_compat: the page without the service worker; contained reads on Windows."""
from __future__ import annotations

import os

import pytest
from pytincture.backend import safe_paths

import pytincture_compat


@pytest.fixture
def path_fallback(monkeypatch):
    """The patch as Windows gets it, applied here to a platform that has dir_fd."""
    original = safe_paths._open_relative_nofollow
    monkeypatch.setattr(pytincture_compat, "_applied", False)
    pytincture_compat.apply(force=True)
    yield
    safe_paths._open_relative_nofollow = original


def test_reads_a_contained_file(path_fallback, tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_bytes(b"x = 1\r\n")
    result = safe_paths.read_contained_file(str(tmp_path), "pkg/mod.py")
    assert result.content == b"x = 1\r\n"  # binary: no newline translation


def test_still_refuses_escapes_and_symlinks(path_fallback, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "secret.txt").write_text("no", encoding="utf-8")
    with pytest.raises(safe_paths.UnsafePath):
        safe_paths.read_contained_file(str(root), "../secret.txt")
    if hasattr(os, "symlink"):
        try:
            (root / "link.txt").symlink_to(tmp_path / "secret.txt")
        except OSError:  # Windows without the symlink privilege
            return
        with pytest.raises(safe_paths.UnsafePath):
            safe_paths.read_contained_file(str(root), "link.txt")


def test_is_a_no_op_where_directory_descriptors_work():
    if os.open in os.supports_dir_fd:
        assert safe_paths._open_relative_nofollow.__module__ == "pytincture.backend.safe_paths"


def test_the_page_turns_the_service_worker_off():
    from pytincture.backend import app as app_module

    pytincture_compat.turn_off_service_worker(app_module)
    index = os.path.join(app_module.STATIC_PATH, "index.html")
    with app_module.open(index) as page:
        text = page.read()
    assert pytincture_compat.SERVICE_WORKER_OFF in text
    assert pytincture_compat.SERVICE_WORKER_ON not in text
    # Other files read through app.py are untouched.
    worker = os.path.join(app_module.STATIC_PATH, "sw.js")
    with app_module.open(worker, encoding="utf-8") as script, open(worker, encoding="utf-8") as original:
        assert script.read() == original.read()


def test_the_template_still_needs_the_patch():
    """Fails once pytincture stops hard-coding the worker on: then drop the patch."""
    from pytincture.backend import app as app_module

    with open(os.path.join(app_module.STATIC_PATH, "index.html"), encoding="utf-8") as page:
        assert pytincture_compat.SERVICE_WORKER_ON in page.read()


def test_it_reaches_the_backend_create_app_builds(tmp_path):
    """create_app() serves from its own copy of the backend module."""
    from pytincture import PytinctureConfig, create_app
    from pytincture.backend import app as imported

    application = create_app(PytinctureConfig(modules_path=str(tmp_path)))
    backend = application.state.pytincture_backend
    assert backend is not imported  # why patching the import is not enough
    pytincture_compat.apply_to_app(application)
    with backend.open(os.path.join(backend.STATIC_PATH, "index.html")) as page:
        assert pytincture_compat.SERVICE_WORKER_OFF in page.read()
