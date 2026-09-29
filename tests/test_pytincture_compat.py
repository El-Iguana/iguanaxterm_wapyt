"""pytincture_compat: contained reads without directory descriptors (Windows)."""
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
