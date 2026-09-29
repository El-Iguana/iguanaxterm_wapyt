"""Server-side folder copies on a Windows disk (native install, tools/windows)."""
from __future__ import annotations

import os

import pytest

from services import download_jobs


@pytest.fixture
def on_windows(monkeypatch):
    monkeypatch.setattr(download_jobs, "_LOCAL_WINDOWS", True)
    monkeypatch.setattr(download_jobs, "_LOCAL_CASE_INSENSITIVE", True)


def test_remote_names_cannot_leave_the_folder(on_windows, tmp_path):
    names = download_jobs.local_names()
    for remote in ("..\\escape.txt", "c:evil.txt", "sub\\..\\..\\x", "a/..\\..\\b.txt"):
        target = download_jobs.local_path(tmp_path, names.map(remote))
        assert tmp_path.resolve() in target.parents, remote


def test_names_windows_refuses_are_cleaned_and_collisions_numbered(on_windows):
    names = download_jobs.local_names()
    assert names.map("logs/a:b.txt") == "logs/a_b.txt"
    assert names.map("logs/a_b.txt") == "logs/a_b (2).txt"
    assert names.map("CON.log") == "CON_.log"
    assert names.map("trailing. ") == "trailing"
    assert names.map("Report.txt") == "Report.txt"
    assert names.map("report.txt") == "report (2).txt"  # one name on NTFS
    assert names.map_dir("dir:1") == "dir_1"


def test_folder_and_user_names_are_single_segments(on_windows):
    assert download_jobs.local_segment("backup:2026") == "backup_2026"
    assert download_jobs.user_dir_name({"user_id": 7, "username": "ops:team"}) == "ops_team"


def test_linux_keeps_remote_names(monkeypatch):
    monkeypatch.setattr(download_jobs, "_LOCAL_WINDOWS", False)
    monkeypatch.setattr(download_jobs, "_LOCAL_CASE_INSENSITIVE", False)
    names = download_jobs.local_names()
    assert names.map("logs/a:b.txt") == "logs/a:b.txt"
    assert download_jobs.local_segment("a:b") == "a:b"


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unavailable")
def test_a_link_inside_the_folder_is_not_a_way_out(tmp_path):
    folder = tmp_path / "copy"
    folder.mkdir()
    try:
        (folder / "link").symlink_to(tmp_path.parent, target_is_directory=True)
    except OSError:
        pytest.skip("no symlink privilege")
    with pytest.raises(ValueError):
        download_jobs.local_path(folder, "link/x.txt")
