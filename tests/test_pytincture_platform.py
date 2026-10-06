"""
What pytincture_compat.py used to patch, now fixed upstream (1.0.0rc12/rc13):
pin these so a pytincture bump that regresses them fails here.
"""
from __future__ import annotations

import os

import pytest
from pytincture.backend import safe_paths


@pytest.fixture
def no_dir_fd(monkeypatch):
    """Windows' os.open has no dir_fd; simulate that on this platform."""
    monkeypatch.setattr(os, "supports_dir_fd", frozenset(os.supports_dir_fd) - {os.open})


def test_contained_read_without_dir_fd(no_dir_fd, tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_bytes(b"x = 1\r\n")
    result = safe_paths.read_contained_file(str(tmp_path), "pkg/mod.py")
    assert result.content == b"x = 1\r\n"  # binary: no newline translation


def test_escapes_and_symlinks_still_refused_without_dir_fd(no_dir_fd, tmp_path):
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


def test_service_worker_is_configurable_and_on_by_default():
    from pytincture import PytinctureConfig

    assert PytinctureConfig().enable_service_worker is True


def test_csrf_cookie_names_follow_the_cookie_namespace():
    """service.py's cookie_namespace and the browser's CSRF lookup must agree."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]

    def constant(path, name):
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == name for t in node.targets):
                return ast.literal_eval(node.value)
        raise AssertionError(f"{name} not found in {path}")

    namespace = constant(root / "service.py", "COOKIE_NAMESPACE")
    names = constant(root / "appcode" / "iguanaxterm.py", "_CSRF_COOKIES")
    assert set(names) == {f"__Host-{namespace}-csrf", f"{namespace}-dev-csrf"}
